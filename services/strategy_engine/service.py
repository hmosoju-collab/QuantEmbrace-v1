"""
Strategy Engine Service — orchestrates tick and candle strategy execution.

Phase 3 architecture (ADR-013):
    Three concurrent loops run via asyncio.gather():
    1. _kafka_processing_loop()   — consumes ticks.nse / ticks.us (strategy-v1),
                                    dispatches to TICK runners (MomentumStrategy).
    2. _candle_processing_loop()  — polls DynamoDB candle-cache every 500ms,
                                    dispatches to CANDLE runners (ORB, Scalp1m,
                                    VWAPReversion, IntradayTrend15m, PreCloseMomentum).
    3. _config_refresh_loop()     — reads DynamoDB strategy-config every 60s,
                                    hot-reloads enabled/paper_trade/circuit_breaker
                                    params into all StrategyRunners without restart.

Each strategy runs inside a StrategyRunner that provides:
    - independent failure isolation (dual-threshold circuit breaker)
    - paper_trade flag stamping (all strategies start paper_trade=True)
    - max_signals_per_day cap
    - DynamoDB hot-reload via StrategyConfigLoader

CRITICAL: This service NEVER places orders directly. All signals go through
the Risk Engine for validation before reaching the Execution Engine.

Required environment variables:
    KAFKA_BOOTSTRAP_SERVERS — MSK Serverless bootstrap endpoint (mandatory).
                              Service will refuse to start if not set.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import time
from typing import Any, Optional

from shared.aws.clients import get_dynamodb_resource
from shared.config.settings import AppSettings, get_settings
from shared.entry_block_reader import EntryBlockReader, EntryBlockState
from shared.health.health_server import HealthServer
from shared.health.loop_health import LoopHealthTracker
from shared.kafka.retry_replayer import KafkaRetryReplayer
from shared.logging.logger import get_logger, set_correlation_id
from shared.metrics.cloudwatch_metrics import get_metrics_client
from shared.models.signal import Signal
from shared.risk_state import attr_bool, kill_switch_resource_key
from shared.zerodha.market_phase import MarketPhase
from strategy_engine.config.strategy_config_loader import StrategyConfigLoader
from strategy_engine.consumers.dynamo_candle_consumer import DynamoCandleConsumer
from strategy_engine.consumers.kafka_tick_consumer import KafkaTickConsumer, TickEvent
from strategy_engine.publishers.kafka_signal_publisher import KafkaSignalPublisher
from strategy_engine.runners.strategy_runner import (
    InterfaceType,
    StrategyConfig,
    StrategyRunner,
)
from strategy_engine.strategies.base_strategy import StrategyState
from strategy_engine.strategies.intraday_trend_15m_strategy import (
    IntradayTrend15mStrategy,
)
from strategy_engine.strategies.momentum_strategy import MomentumStrategy
from strategy_engine.strategies.orb_strategy import ORBStrategy
from strategy_engine.strategies.preclose_momentum_strategy import (
    PreCloseMomentumStrategy,
)
from strategy_engine.strategies.scalp_1m_strategy import Scalp1mStrategy
from strategy_engine.strategies.vwap_reversion_strategy import VWAPReversionStrategy
from strategy_engine.universe.instrument_loader import InstrumentLoader

logger = get_logger(__name__, service_name="strategy_engine")

# CloudWatch metrics — namespace shared with monitoring alarms.
# Emits: TickToSignalLatencyMs  (tick → signal published)
#        CandleToSignalLatencyMs (candle → signal published)
#        SignalsGenerated        (counter)
#        PaperSignalsGenerated   (counter)
_metrics = get_metrics_client(namespace="QuantEmbrace/Trading")

# Candle poll interval — 500ms matches data_ingestion write cadence.
_CANDLE_POLL_INTERVAL_S: float = 0.5

# Config refresh interval — 60s matches DynamoDB hot-reload design (ADR-013 §9).
_CONFIG_REFRESH_INTERVAL_S: float = 60.0
_STRATEGY_STATE_GLOBAL_SYMBOL: str = "__GLOBAL__"
_STRATEGY_STATE_SCHEMA_VERSION: str = "1"


class StrategyEngineService:
    """
    Main strategy engine that orchestrates tick and candle strategy execution.

    Phase 3 lifecycle:
        1. start()               — validates config, loads instrument universe,
                                   registers all 6 strategies as StrategyRunners,
                                   restores state from DynamoDB, starts Kafka
                                   consumer/publisher, begins 3-loop processing.
        2. _kafka_processing_loop()   — TICK runners (MomentumStrategy).
        3. _candle_processing_loop()  — CANDLE runners (5 strategies).
        4. _config_refresh_loop()     — 60s hot-reload from DynamoDB.
        5. stop()                — drains in-flight ticks, persists all strategy
                                   states to DynamoDB, stops Kafka I/O.

    Restart-safety:
        Strategy indicator states (moving average windows, candle accumulators)
        are saved to DynamoDB on shutdown and restored on startup so warm-up
        periods are not repeated after an EC2 instance replacement.
    """

    def __init__(
        self,
        settings: Optional[AppSettings] = None,
        instruments_config_path: Optional[str] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._running = False
        self._shutdown_event = asyncio.Event()

        # ── StrategyRunner registry ────────────────────────────────────────────
        # All runners — used for state persistence and config reload registration.
        self._runners: list[StrategyRunner] = []
        # Partitioned for efficient dispatch:
        self._tick_runners: list[StrategyRunner] = []  # InterfaceType.TICK
        self._candle_runners: list[StrategyRunner] = []  # InterfaceType.CANDLE

        # ── Instrument universe loader ─────────────────────────────────────────
        config_path = (
            os.environ.get("INSTRUMENTS_CONFIG_PATH")
            or instruments_config_path
            or Path(__file__).parent.parent.parent.parent / "configs" / "instruments.yaml"
        )
        self._instrument_loader = InstrumentLoader(config_path=config_path)

        # ── Health/readiness server ────────────────────────────────────────────
        self._health_server = HealthServer(
            port=getattr(self._settings, "health_check_port", 8080),
            service_name="strategy_engine",
        )

        # ── Kill switch DynamoDB fast-path ─────────────────────────────────────
        self._inflight_ticks: int = 0
        self._ks_active: bool = False
        self._ks_checked_at: float = 0.0
        self._ks_poll_interval: float = getattr(
            self._settings.risk, "kill_switch_poll_interval_seconds", 1.0
        )
        self._ks_table: str = self._settings.aws.dynamodb_table_risk_state
        # Cached DynamoDB Table object for the kill switch check.
        # Initialised in start() once DynamoDB is available; avoids creating a
        # new boto3 Session + resource on every 1s cache miss (B-003 fix).
        self._ks_dynamo_table: Optional[object] = None

        # ── Entry-block fast-path (Phase 5) ───────────────────────────────────
        # Mirrors the kill-switch caching pattern. EntryBlockReader is initialised
        # in start() using the low-level boto3 DynamoDB client. TTL is kept short
        # (default 5s) so a safe_actions BLOCK_NEW_ENTRIES write takes effect
        # within 5s without hammering DynamoDB on every signal.
        #
        # Fail-closed mode: True for live, False for paper (env-resolved in start()).
        self._entry_block_reader: Optional[EntryBlockReader] = None
        # Counters exposed for tests and CloudWatch
        self._entry_blocked_total: int = 0
        self._entry_block_read_failure_total: int = 0

        # ── Kafka I/O — initialised in start() ────────────────────────────────
        self._kafka_consumer: Optional[KafkaTickConsumer] = None
        self._kafka_signal_publisher: Optional[KafkaSignalPublisher] = None
        self._retry_replayer: Optional[KafkaRetryReplayer] = None

        # ── Phase 3 components — initialised in start() ───────────────────────
        self._dynamo_candle_consumer: Optional[DynamoCandleConsumer] = None
        self._config_loader: Optional[StrategyConfigLoader] = None

    # ── Runner registration ───────────────────────────────────────────────────

    def register_runner(self, runner: StrategyRunner) -> None:
        """
        Register a StrategyRunner with the engine.

        Partitions the runner into _tick_runners or _candle_runners based on
        its InterfaceType so each processing loop only iterates its own subset.

        Args:
            runner: Fully constructed StrategyRunner instance.
        """
        self._runners.append(runner)
        if runner.interface_type == InterfaceType.TICK:
            self._tick_runners.append(runner)
        else:
            self._candle_runners.append(runner)
        logger.info(
            "strategy_engine.runner_registered",
            strategy=runner.name,
            interface=runner.interface_type.value,
        )

    # ── Public lifecycle API ──────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start the strategy engine.

        Steps:
            1.  Validate KAFKA_BOOTSTRAP_SERVERS.
            2.  Start health server (liveness immediate, readiness after init).
            3.  Load instrument universe from instruments.yaml.
            4.  Register all 6 strategies as StrategyRunners.
            5.  Restore strategy indicator state from DynamoDB (warm-start).
            6.  Initialise DynamoCandleConsumer (candle-cache poller).
            7.  Initialise StrategyConfigLoader (DynamoDB hot-reload).
            8.  Start Kafka tick consumer (strategy-v1) and signal publisher.
            9.  Register OS signal handlers for EC2 instance termination.
            10. Register health checks and mark service ready.
            11. Run 3 concurrent processing loops via asyncio.gather().

        Raises:
            RuntimeError: If KAFKA_BOOTSTRAP_SERVERS is not set.
        """
        set_correlation_id()

        # 1. Validate mandatory configuration
        kafka_bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
        if not kafka_bootstrap:
            raise RuntimeError(
                "KAFKA_BOOTSTRAP_SERVERS is not set. "
                "This service requires MSK Serverless Kafka to operate. "
                "Set KAFKA_BOOTSTRAP_SERVERS to the MSK bootstrap endpoint and restart."
            )

        # 2. Start health server
        await self._health_server.start()

        # 3. Load instrument universe — YAML first (metadata), then optionally
        #    override NSE symbol list from the live NSE CSV universe so the
        #    strategy trades the same symbols the execution_engine validates.
        self._instrument_loader.load()
        _use_live = os.environ.get("UNIVERSE_USE_LIVE_API", "false").strip().lower() in ("true", "1", "yes")
        if _use_live:
            _mode_str = os.environ.get("UNIVERSE_MODE", "PAPER_SAFE_START")
            try:
                from shared.universe.order_validator import (  # noqa: PLC0415
                    build_validator_for_today as _build_universe_validator,
                )
                from shared.universe.modes import UniverseMode as _UniverseMode  # noqa: PLC0415
                _mode = _UniverseMode.from_string(_mode_str)
                # Resolve configs dir relative to __file__ — same pattern as
                # InstrumentLoader, handles /configs vs /app/configs mount differences.
                _configs_dir = Path(__file__).parent.parent.parent.parent / "configs"
                _modes_path = str(_configs_dir / "universe_modes.yaml")
                _validator = _build_universe_validator(
                    mode=_mode,
                    modes_config_path=_modes_path,
                    fail_if_no_snapshot=False,
                    use_live_api=True,
                )
                _snap = getattr(_validator, "snapshot", None)
                _approved = getattr(_snap, "approved_symbols", None)
                if _approved:
                    self._instrument_loader.sync_nse_from_universe(_approved)
                    logger.info(
                        "strategy_engine.universe_synced_from_live mode=%s symbols=%d",
                        _mode_str, len(_approved),
                    )
                else:
                    logger.warning(
                        "strategy_engine.universe_live_empty mode=%s — using instruments.yaml",
                        _mode_str,
                    )
            except Exception as _uv_exc:
                logger.warning(
                    "strategy_engine.universe_live_fetch_failed error=%s — using instruments.yaml",
                    _uv_exc,
                )
        logger.info("Instrument universe:\n%s", self._instrument_loader.summary())

        # 4. Register all strategies as StrategyRunners (tick + candle)
        if not self._runners:
            self._register_strategies_from_config()

        logger.info(
            "strategy_engine.runners_registered",
            total=len(self._runners),
            tick=len(self._tick_runners),
            candle=len(self._candle_runners),
        )
        missing_intervals = self._missing_configured_candle_intervals()
        if missing_intervals:
            raise RuntimeError(
                "Strategy candle intervals are not produced by data_ingestion: "
                f"{sorted(missing_intervals)}. Configure STRATEGY_CANDLE_INTERVALS "
                "to include every registered candle strategy interval."
            )

        # 5. Restore strategy state from DynamoDB for warm-start
        for runner in self._runners:
            saved_state = await self._load_strategy_state(runner.name)
            await runner._strategy.initialize(saved_state)  # noqa: SLF001
            logger.info(
                "strategy_engine.strategy_initialized",
                strategy=runner.name,
                state_restored=saved_state is not None,
            )

        # 6. DynamoCandleConsumer — polls candle-cache for CANDLE runners
        dynamo_resource = get_dynamodb_resource()
        # Cache the kill switch DynamoDB Table object once so _is_kill_switch_active()
        # reuses it on every 1s poll instead of creating a new Session + resource (B-003 fix).
        self._ks_dynamo_table = dynamo_resource.Table(self._ks_table)

        # Phase 5: initialise entry-block reader using the low-level boto3 client.
        # Fail-closed when RISK_PROFILE != "paper" (live modes block entries on read failure).
        import boto3  # local import to stay consistent with existing lazy-import pattern
        _risk_profile = os.environ.get("RISK_PROFILE", "paper").lower()
        _entry_block_fail_closed = _risk_profile != "paper"
        self._entry_block_reader = EntryBlockReader(
            dynamo_client=boto3.client(
                "dynamodb",
                endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
                region_name=os.environ.get("AWS_DEFAULT_REGION", "ap-south-1"),
            ),
            risk_state_table=self._ks_table,
            cache_ttl_seconds=float(
                os.environ.get("ENTRY_BLOCK_CACHE_TTL_SECONDS", "5")
            ),
            fail_closed_on_error=_entry_block_fail_closed,
        )
        logger.info(
            "strategy_engine.entry_block_reader_ready table=%s fail_closed=%s",
            self._ks_table, _entry_block_fail_closed,
        )
        candle_table_name = f"{self._settings.aws.dynamodb_table_prefix}-candle-cache"
        candle_table = dynamo_resource.Table(candle_table_name)
        self._dynamo_candle_consumer = DynamoCandleConsumer(
            dynamo_table=candle_table,
            lookback_minutes=3,
            startup_lookback_minutes=0,
            phase_check_fn=self._get_market_phase,
        )
        logger.info(
            "strategy_engine.candle_consumer_ready",
            table=candle_table_name,
        )

        # 7. StrategyConfigLoader — hot-reload from DynamoDB strategy-config
        strategy_config_table_name = f"{self._settings.aws.dynamodb_table_prefix}-strategy-config"
        strategy_config_table = dynamo_resource.Table(strategy_config_table_name)
        strategy_env = getattr(
            self._settings.environment,
            "value",
            self._settings.environment,
        )
        self._config_loader = StrategyConfigLoader(
            dynamo_table=strategy_config_table,
            env=strategy_env,
        )
        for runner in self._runners:
            self._config_loader.register(runner)
        logger.info(
            "strategy_engine.config_loader_ready",
            table=strategy_config_table_name,
            runners=len(self._runners),
        )

        # 8. Kafka tick consumer + signal publisher
        self._kafka_consumer = KafkaTickConsumer(
            bootstrap_servers=kafka_bootstrap,
            aws_region=self._settings.aws.region,
        )
        await self._kafka_consumer.start()

        self._kafka_signal_publisher = KafkaSignalPublisher(
            bootstrap_servers=kafka_bootstrap,
            aws_region=self._settings.aws.region,
        )
        await self._kafka_signal_publisher.start()

        self._retry_replayer = KafkaRetryReplayer(
            bootstrap_servers=kafka_bootstrap,
            aws_region=self._settings.aws.region,
            source_topics=["ticks.nse", "ticks.us"],
            source_service="strategy_engine",
            consumer_group="strategy-retry-v1",
        )
        await self._retry_replayer.start()

        logger.info("Kafka consumer started (consumer=strategy-v1, topics=[ticks.nse, ticks.us])")
        logger.info("Kafka signal publisher started (topic=signals.pending)")
        logger.info("Kafka retry replayer started (topics=[ticks.nse.retry, ticks.us.retry])")

        # 9. OS signal handlers for EC2 instance termination
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        # 10. Health checks + readiness
        self._health_server.add_check(
            "runners_loaded",
            lambda: len(self._runners) > 0,
        )
        self._health_server.add_check(
            "kafka_consumer_ready",
            lambda: self._kafka_consumer is not None,
        )
        self._health_server.add_check(
            "kafka_publisher_ready",
            lambda: self._kafka_signal_publisher is not None,
        )
        self._health_server.add_check(
            "kafka_retry_replayer_ready",
            lambda: self._retry_replayer is not None,
        )
        self._health_server.add_check(
            "candle_consumer_ready",
            lambda: self._dynamo_candle_consumer is not None,
        )
        # 11. Initial config load — apply DynamoDB strategy-config before serving
        #     traffic. Without this, runners use default thresholds for the first
        #     60 s (until _config_refresh_loop fires). This matters for paper_trade
        #     flags and circuit-breaker thresholds set by operators.
        await self._config_loader.refresh_all()
        logger.info("strategy_engine.initial_config_loaded runners=%d", len(self._runners))

        self._health_server.set_ready(True)

        self._running = True
        logger.info(
            "Strategy Engine Service started (Phase 3) — " "%d tick runners, %d candle runners",
            len(self._tick_runners),
            len(self._candle_runners),
        )

        # 12. Three concurrent processing loops plus an inline kill-switch listener.
        #     Loop A — Kafka tick path (dispatch_tick per registered tick runner)
        #     Loop B — DynamoDB candle path (500ms poll, dispatch_bar per candle runner)
        #     Loop C — Config refresh (60s hot-reload from strategy-config DynamoDB)
        #     Kill switch — consumed inline within loop A via KafkaTickConsumer
        #
        # B-002: LoopHealthTrackers emit service.loop_running / service.loop_crash_total
        # per loop so any crash is visible in CloudWatch immediately.
        # return_exceptions=True so ALL loops report before we re-raise the first crash
        # (keeps crash visibility for every concurrent loop, not just the first to die).
        _lh_kafka   = LoopHealthTracker("kafka_loop",   _metrics, service_name="strategy_engine")
        _lh_candle  = LoopHealthTracker("candle_loop",  _metrics, service_name="strategy_engine")
        _lh_config  = LoopHealthTracker("config_loop",  _metrics, service_name="strategy_engine")
        _lh_retry   = LoopHealthTracker("retry_loop",   _metrics, service_name="strategy_engine")
        results = await asyncio.gather(
            _lh_kafka.run(self._kafka_processing_loop()),
            _lh_candle.run(self._candle_processing_loop()),
            _lh_config.run(self._config_refresh_loop()),
            _lh_retry.run(self._retry_replayer.run()),
            return_exceptions=True,
        )
        first_exc: Optional[BaseException] = None
        for _result in results:
            if isinstance(_result, BaseException) and not isinstance(_result, asyncio.CancelledError):
                logger.critical(
                    "strategy_engine.processing_loop_crashed error=%s — "
                    "service restart required; signals may have been dropped",
                    repr(_result),
                )
                if first_exc is None:
                    first_exc = _result
        if first_exc is not None:
            raise first_exc

    async def stop(self) -> None:
        """
        Gracefully stop the strategy engine.

        Drains in-flight ticks, persists all strategy states to DynamoDB,
        then stops Kafka I/O.
        """
        if not self._running:
            return

        logger.info("Stopping Strategy Engine Service")
        self._running = False
        self._health_server.set_ready(False)

        # Drain in-flight ticks before saving state
        _drain_deadline = time.monotonic() + 5.0
        while self._inflight_ticks > 0 and time.monotonic() < _drain_deadline:
            await asyncio.sleep(0.05)
        if self._inflight_ticks > 0:
            logger.warning(
                "strategy_engine.stop: %d in-flight tick(s) still processing "
                "after 5s drain timeout — proceeding with state save",
                self._inflight_ticks,
            )

        # Persist all strategy states via their runners
        for runner in self._runners:
            state = runner._strategy.get_state()  # noqa: SLF001
            await self._save_strategy_state(runner.name, state)

        # Stop Kafka I/O
        if self._kafka_consumer is not None:
            await self._kafka_consumer.stop()
        if self._kafka_signal_publisher is not None:
            await self._kafka_signal_publisher.stop()
        if self._retry_replayer is not None:
            await self._retry_replayer.stop()

        await self._health_server.stop()
        self._shutdown_event.set()
        logger.info("Strategy Engine Service stopped")

    # ── Tick dispatch ─────────────────────────────────────────────────────────

    async def process_tick(
        self,
        symbol: str,
        price: float,
        volume: int,
        timestamp: Any,
        suppress_signals: bool = False,
        trace_id: str = "",
    ) -> list[Signal]:
        """
        Fan a single tick out to all TICK runners watching that symbol.

        Each runner provides its own circuit-breaker, paper_trade stamping,
        and daily cap — failures in one runner do not affect others.

        Args:
            symbol:           Trading symbol (e.g. "RELIANCE", "AAPL").
            price:            Last traded price.
            volume:           Tick volume.
            timestamp:        Exchange timestamp (datetime or ISO string).
            suppress_signals: When True, runners receive the tick for indicator
                              accumulation but signals are discarded (gap warm-up).
            trace_id:         Trace ID from the originating tick event, propagated
                              into SIGNAL_PENDING events for end-to-end tracing.

        Returns:
            List of signals emitted during this tick. Empty if suppress_signals=True.
        """
        self._inflight_ticks += 1
        signals: list[Signal] = []

        try:
            for runner in self._tick_runners:
                if symbol not in runner._strategy.symbols:  # noqa: SLF001
                    continue

                if suppress_signals:
                    # Feed tick for indicator accumulation but skip signal generation.
                    await runner._strategy.on_tick(symbol, price, volume, timestamp)  # noqa: SLF001
                    continue

                # Phase 5: check entry-block flag before dispatch so _signals_today
                # is not incremented when entries are blocked.
                entry_blocked, eb_state = await self._is_entry_blocked()
                if entry_blocked:
                    # Update indicators without generating / counting a signal.
                    await runner._strategy.on_tick(symbol, price, volume, timestamp)  # noqa: SLF001
                    self._entry_blocked_total += 1
                    logger.info(
                        "strategy_engine.entry_blocked strategy=%s symbol=%s "
                        "reason=%s source=%s action_id=%s",
                        runner.name, symbol,
                        eb_state.reason, eb_state.source, eb_state.action_id,
                    )
                    _metrics.record_count(
                        "EntryBlocked",
                        dimensions={"Strategy": runner.name},
                    )
                    continue

                signal_out = await runner.dispatch_tick(
                    symbol=symbol,
                    price=price,
                    volume=volume,
                    timestamp=timestamp,
                )
                if signal_out is not None:
                    signals.append(signal_out)
                    published = await self._publish_signal(signal_out, trace_id=trace_id)
                    if not published:
                        raise RuntimeError(
                            f"Signal {signal_out.signal_id} could not be enqueued to Kafka"
                        )
        finally:
            self._inflight_ticks -= 1

        return signals

    # ── Loop 1: Kafka tick consumer ───────────────────────────────────────────

    async def _kafka_processing_loop(self) -> None:
        """
        Poll Kafka for ticks (ticks.nse / ticks.us) and dispatch to TICK runners.

        KafkaTickConsumer.poll_tick() is synchronous (confluent-kafka Consumer.poll
        blocks for up to poll_timeout_seconds). asyncio.to_thread() offloads this
        so the event loop remains responsive to the candle and config loops.
        """
        logger.info("Kafka processing loop started")
        while self._running:
            if self._shutdown_event.is_set():
                break

            if await self._is_kill_switch_active():
                logger.warning(
                    "Kill switch ACTIVE — Kafka tick consumption paused. " "Re-checking in %.1fs.",
                    self._ks_poll_interval,
                )
                await asyncio.sleep(self._ks_poll_interval)
                continue

            try:
                tick: Optional[TickEvent] = await asyncio.to_thread(
                    self._kafka_consumer.poll_tick  # type: ignore[arg-type]
                )
                if tick is None:
                    continue

                if await self._is_kill_switch_active():
                    logger.warning(
                        "Kill switch activated — discarding tick %s:%s",
                        tick.market,
                        tick.symbol,
                    )
                    continue

                try:
                    await self._handle_kafka_tick(tick)
                    self._kafka_consumer.commit(tick)  # type: ignore[union-attr]
                except Exception as exc:
                    routed = self._kafka_consumer.publish_retry(  # type: ignore[union-attr]
                        tick,
                        reason=str(exc),
                        error_type="strategy_tick_processing_failed",
                    )
                    if routed:
                        self._kafka_consumer.commit(tick)  # type: ignore[union-attr]
                    else:
                        raise

            except Exception:
                logger.exception("Error in Kafka processing loop — backing off 1s")
                await asyncio.sleep(1)

    async def _handle_kafka_tick(self, tick: TickEvent) -> None:
        """Dispatch a Kafka tick to TICK runners and emit latency metrics."""
        _t0 = time.perf_counter()
        if tick.gap_detected:
            logger.warning(
                "strategy_engine.reconnect_gap_tick_suppressed "
                "symbol=%s market=%s sequence_id=%s trace_id=%s",
                tick.symbol,
                tick.market,
                tick.sequence_id,
                tick.trace_id,
            )
            _metrics.record_count(
                "ReconnectGapTicksSuppressed",
                dimensions={"Market": tick.market},
            )

        signals = await self.process_tick(
            symbol=tick.symbol,
            price=tick.price,
            volume=tick.volume,
            timestamp=tick.timestamp,
            suppress_signals=tick.gap_detected,
            trace_id=tick.trace_id,
        )

        if signals:
            _metrics.record_latency(
                "TickToSignalLatencyMs",
                _t0,
                dimensions={"Market": tick.market},
            )
            _metrics.record_count(
                "SignalsGenerated",
                value=float(len(signals)),
                dimensions={"Market": tick.market},
            )
            asyncio.create_task(_metrics.flush())

    # ── Loop 2: DynamoDB candle consumer (Phase 3) ────────────────────────────

    async def _candle_processing_loop(self) -> None:
        """
        Poll DynamoDB candle-cache every 500ms and dispatch to CANDLE runners.

        Each CANDLE runner checks internally whether the candle's interval
        matches its expected interval (strategy.candle_interval class attribute).
        Mismatched intervals return None from dispatch_bar() immediately.

        DynamoCandleConsumer.poll_new_candles() is synchronous (boto3 Scan).
        asyncio.to_thread() offloads this so the event loop stays responsive.

        Each candle is dispatched to ALL CANDLE runners — runners for mismatched
        intervals (e.g. a 15m candle sent to a 1m strategy) return None quickly.
        This avoids the complexity of per-interval fan-out routing at the service
        level while keeping dispatch O(N) over the number of candle runners.
        """
        if not self._candle_runners:
            logger.info("No CANDLE runners registered — candle processing loop idle")
            while self._running and not self._shutdown_event.is_set():
                await asyncio.sleep(1.0)
            return

        logger.info(
            "Candle processing loop started — %d candle runners, polling every %.0fms",
            len(self._candle_runners),
            _CANDLE_POLL_INTERVAL_S * 1000,
        )

        while self._running:
            if self._shutdown_event.is_set():
                break

            if await self._is_kill_switch_active():
                await asyncio.sleep(self._ks_poll_interval)
                continue

            try:
                candles = await asyncio.to_thread(
                    self._dynamo_candle_consumer.poll_new_candles  # type: ignore[union-attr]
                )

                for candle in candles:
                    bar = candle.to_bar()
                    _t0 = time.perf_counter()
                    signals_from_candle: list[Signal] = []

                    for runner in self._candle_runners:
                        # Only dispatch if the strategy's candle_interval matches.
                        strategy_interval = getattr(
                            runner._strategy,  # noqa: SLF001
                            "candle_interval",
                            "minute",
                        )
                        if strategy_interval != candle.interval:
                            continue

                        # Only dispatch if this strategy watches this symbol.
                        if bar.symbol not in runner._strategy.symbols:  # noqa: SLF001
                            continue

                        # Phase 5: check entry-block before dispatch so
                        # _signals_today is not incremented when blocked.
                        entry_blocked, eb_state = await self._is_entry_blocked()
                        if entry_blocked:
                            self._entry_blocked_total += 1
                            logger.info(
                                "strategy_engine.entry_blocked strategy=%s symbol=%s "
                                "interval=%s reason=%s source=%s action_id=%s",
                                runner.name, bar.symbol, candle.interval,
                                eb_state.reason, eb_state.source, eb_state.action_id,
                            )
                            _metrics.record_count(
                                "EntryBlocked",
                                dimensions={"Strategy": runner.name},
                            )
                            # B-001 — standardised drop metric for entry_block
                            _metrics.record_count(
                                "strategy.candle_signal_dropped_total",
                                dimensions={"Market": candle.market, "Strategy": runner.name},
                            )
                            _metrics.record_count(
                                "strategy.candle_signal_drop_reason_total",
                                dimensions={"Market": candle.market, "Reason": "entry_block"},
                            )
                            continue

                        signal_out = await runner.dispatch_bar(bar)
                        if signal_out is not None:
                            # Stamp generated_at with the candle's close time (NOT poll
                            # time) so signal_id is deterministic across restarts.
                            # signal_id = sha256(strategy|symbol|direction|price|generated_at)
                            # — using poll time would break dedup on replay (ADR-013 §7.4).
                            signal_out.generated_at = candle.candle_close_time
                            # B-001 — standardised metric: signal generated
                            _metrics.record_count(
                                "strategy.candle_signal_generated_total",
                                dimensions={"Market": candle.market, "Strategy": runner.name},
                            )
                            published = await self._publish_signal(
                                signal_out,
                                trace_id=candle.trace_id,
                            )
                            if not published:
                                logger.critical(
                                    "strategy_engine.candle_signal_publish_failed "
                                    "signal_id=%s strategy=%s symbol=%s — "
                                    "Kafka producer unavailable; signal lost",
                                    signal_out.signal_id,
                                    signal_out.strategy_name,
                                    signal_out.symbol,
                                )
                                # B-001 — standardised drop metrics
                                _metrics.record_count(
                                    "CandleSignalPublishFailed",
                                    dimensions={"Market": candle.market},
                                )
                                _metrics.record_count(
                                    "strategy.candle_signal_dropped_total",
                                    dimensions={"Market": candle.market, "Strategy": runner.name},
                                )
                                _metrics.record_count(
                                    "strategy.candle_signal_drop_reason_total",
                                    dimensions={"Market": candle.market, "Reason": "publish_failed"},
                                )
                                continue
                            # B-001 — standardised metric: signal published
                            _metrics.record_count(
                                "strategy.candle_signal_published_total",
                                dimensions={"Market": candle.market, "Strategy": runner.name},
                            )
                            signals_from_candle.append(signal_out)

                    if signals_from_candle:
                        _metrics.record_latency(
                            "CandleToSignalLatencyMs",
                            _t0,
                            dimensions={"Market": candle.market, "Interval": candle.interval},
                        )
                        _metrics.record_count(
                            "SignalsGenerated",
                            value=float(len(signals_from_candle)),
                            dimensions={"Market": candle.market},
                        )
                        paper_count = sum(1 for s in signals_from_candle if s.paper_trade)
                        if paper_count:
                            _metrics.record_count(
                                "PaperSignalsGenerated",
                                value=float(paper_count),
                                dimensions={"Market": candle.market},
                            )
                        asyncio.create_task(_metrics.flush())

            except Exception as _loop_exc:  # noqa: BLE001
                logger.exception(
                    "strategy_engine.candle_loop_exception — backing off 1s. "
                    "error_type=%s", type(_loop_exc).__name__,
                )
                # B-001 — drop metric for loop exception (signals in-flight for this
                # candle batch are lost; count as 1 drop with reason=loop_exception).
                _metrics.record_count(
                    "strategy.candle_signal_dropped_total",
                    dimensions={"Market": "UNKNOWN"},
                )
                _metrics.record_count(
                    "strategy.candle_signal_drop_reason_total",
                    dimensions={"Market": "UNKNOWN", "Reason": "loop_exception"},
                )
                await asyncio.sleep(1.0)
                continue

            await asyncio.sleep(_CANDLE_POLL_INTERVAL_S)

    # ── Loop 3: Config hot-reload (Phase 3) ───────────────────────────────────

    async def _config_refresh_loop(self) -> None:
        """
        Refresh all StrategyRunner configs from DynamoDB every 60 seconds.

        Calls StrategyConfigLoader.refresh_all() which reads strategy-config
        rows and applies any changes to live StrategyRunner instances.
        Changes take effect within one refresh cycle without a service restart.

        Configuration that can be changed live:
            - enabled (enable/disable a strategy instantly)
            - paper_trade (promote strategy from paper to live)
            - max_signals_per_day (tighten or relax daily signal cap)
            - circuit_breaker_threshold_consecutive (tighten failure tolerance)
            - circuit_breaker_threshold_rate (tighten failure rate tolerance)
            - circuit_breaker_reset (force-reset an OPEN circuit to CLOSED)
        """
        logger.info(
            "Config refresh loop started — polling every %.0fs",
            _CONFIG_REFRESH_INTERVAL_S,
        )

        while self._running:
            if self._shutdown_event.is_set():
                break

            await asyncio.sleep(_CONFIG_REFRESH_INTERVAL_S)

            if not self._running:
                break

            try:
                await self._config_loader.refresh_all()  # type: ignore[union-attr]
            except Exception:
                logger.exception("Error in config refresh loop — will retry next cycle")

    # ── Kill switch check ─────────────────────────────────────────────────────

    async def _is_kill_switch_active(self) -> bool:
        """
        Check DynamoDB for kill switch state with a 1-second TTL cache.

        Returns True if active. On DynamoDB error, returns the last known state
        (fail-safe: if we last knew it was active, stay halted).
        """
        now = time.monotonic()
        if (now - self._ks_checked_at) < self._ks_poll_interval:
            return self._ks_active

        try:
            # Use the cached Table object (initialised in start()) to avoid
            # creating a new boto3 Session + DynamoDB resource on every 1s cache miss.
            table = self._ks_dynamo_table or get_dynamodb_resource().Table(self._ks_table)
            response = await asyncio.to_thread(
                table.get_item,
                Key=kill_switch_resource_key(),
                ProjectionExpression="active, #status",
                ExpressionAttributeNames={"#status": "status"},
            )
            item = response.get("Item")
            active = bool(item and attr_bool(item, "active", False))
            if active != self._ks_active:
                logger.warning(
                    "Kill switch state changed: %s → %s",
                    "ACTIVE" if self._ks_active else "INACTIVE",
                    "ACTIVE" if active else "INACTIVE",
                )
            self._ks_active = active
            self._ks_checked_at = now
        except Exception:
            logger.exception(
                "Failed to read kill switch state from DynamoDB — " "using last known state: %s",
                "ACTIVE" if self._ks_active else "INACTIVE",
            )

        return self._ks_active

    # ── Entry-block check (Phase 5) ───────────────────────────────────────────

    async def _is_entry_blocked(self) -> tuple[bool, "EntryBlockState"]:
        """Check the ENTRY_BLOCK/GLOBAL flag with a short TTL cache.

        Returns (blocked, state). On read failure the EntryBlockReader applies
        its configured fail_closed / fail_open policy.

        This method applies to NEW ENTRY signals only. It must NEVER be consulted
        for exit orders, closeout orders, TEE, MIS, reconciliation, or the
        kill switch — those paths do not call _publish_signal() at all.
        """
        if self._entry_block_reader is None:
            return False, EntryBlockState.allow()

        state = await asyncio.to_thread(self._entry_block_reader.read)
        if not state.read_ok:
            self._entry_block_read_failure_total += 1
        return state.blocked, state

    # ── Signal publisher ──────────────────────────────────────────────────────

    async def _publish_signal(self, signal_out: Signal, trace_id: str = "") -> bool:
        """
        Publish a signal to the Risk Engine via signals.pending Kafka topic.

        Args:
            signal_out: The trading signal to publish.
            trace_id:   Propagated trace ID (tick or candle trace_id).
        """
        logger.info(
            "Publishing signal: strategy=%s direction=%s symbol=%s "
            "qty=%d confidence=%.2f paper_trade=%s trace_id=%s",
            signal_out.strategy_name,
            signal_out.direction.value,
            signal_out.symbol,
            signal_out.quantity,
            signal_out.confidence,
            signal_out.paper_trade,
            trace_id or "(none)",
        )

        try:
            return await self._kafka_signal_publisher.publish(signal_out, trace_id=trace_id)
        except Exception:
            logger.exception(
                "Failed to publish signal %s to Kafka (signals.pending) — signal lost",
                signal_out.signal_id,
            )
            return False

    # ── DynamoDB state persistence ─────────────────────────────────────────────

    async def _save_strategy_state(
        self,
        strategy_name: str,
        state: Any,
        symbol: str = _STRATEGY_STATE_GLOBAL_SYMBOL,
    ) -> None:
        """Persist strategy indicator state to DynamoDB for restart recovery."""
        try:
            table_name = f"{self._settings.aws.dynamodb_table_prefix}-strategy-state"
            await asyncio.to_thread(
                self._dynamo_put_state,
                table_name,
                strategy_name,
                symbol,
                state,
            )
            logger.info(
                "Saved state for strategy: %s symbol=%s",
                strategy_name,
                symbol,
            )
        except Exception:
            logger.exception(
                "Failed to save state for strategy %s symbol=%s — restart will cold-start",
                strategy_name,
                symbol,
            )

    def _dynamo_put_state(
        self,
        table_name: str,
        strategy_name: str,
        symbol: str,
        state: Any,
    ) -> None:
        """Synchronous DynamoDB put — called via asyncio.to_thread."""
        dynamodb = get_dynamodb_resource()
        table = dynamodb.Table(table_name)
        payload = self._serialize_strategy_state(strategy_name, state)
        table.put_item(
            Item={
                "strategy_name": strategy_name,
                "symbol": symbol,
                "state": json.dumps(payload, default=str, sort_keys=True),
                "schema_version": _STRATEGY_STATE_SCHEMA_VERSION,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def _load_strategy_state(
        self,
        strategy_name: str,
        symbol: str = _STRATEGY_STATE_GLOBAL_SYMBOL,
    ) -> StrategyState | None:
        """
        Load persisted strategy state from DynamoDB.

        Returns None (cold start) if no state exists.
        """
        try:
            table_name = f"{self._settings.aws.dynamodb_table_prefix}-strategy-state"
            result = await asyncio.to_thread(
                self._dynamo_get_state,
                table_name,
                strategy_name,
                symbol,
            )
            if result:
                return self._deserialize_strategy_state(strategy_name, result.get("state"))
            return None
        except Exception:
            logger.warning(
                "Could not load state for strategy %s symbol=%s — cold start",
                strategy_name,
                symbol,
            )
            return None

    def _dynamo_get_state(
        self,
        table_name: str,
        strategy_name: str,
        symbol: str,
    ) -> Optional[dict]:
        """Synchronous DynamoDB get — called via asyncio.to_thread."""
        dynamodb = get_dynamodb_resource()
        table = dynamodb.Table(table_name)
        response = table.get_item(
            Key={
                "strategy_name": strategy_name,
                "symbol": symbol,
            }
        )
        return response.get("Item")

    @staticmethod
    def _serialize_strategy_state(strategy_name: str, state: Any) -> dict[str, Any]:
        """Convert StrategyState/dataclass/dict state to a JSON object."""
        if isinstance(state, StrategyState):
            payload = asdict(state)
        elif is_dataclass(state):
            payload = asdict(state)
        elif isinstance(state, dict):
            payload = dict(state)
        else:
            raise TypeError(f"Unsupported strategy state type: {type(state).__name__}")

        payload["strategy_name"] = str(payload.get("strategy_name") or strategy_name)
        last_signal_time = payload.get("last_signal_time")
        if isinstance(last_signal_time, datetime):
            payload["last_signal_time"] = last_signal_time.astimezone(timezone.utc).isoformat()
        return payload

    @staticmethod
    def _deserialize_strategy_state(
        strategy_name: str,
        raw_state: Any,
    ) -> StrategyState | None:
        """Convert persisted JSON state to the StrategyState object expected by strategies."""
        if raw_state is None:
            return None
        if isinstance(raw_state, StrategyState):
            return raw_state
        if isinstance(raw_state, str):
            payload = json.loads(raw_state)
        elif isinstance(raw_state, dict):
            payload = raw_state
        else:
            raise TypeError(f"Unsupported persisted strategy state type: {type(raw_state).__name__}")
        if not isinstance(payload, dict):
            raise TypeError("Persisted strategy state must decode to a JSON object")

        raw_last_signal_time = payload.get("last_signal_time")
        last_signal_time = None
        if raw_last_signal_time:
            last_signal_time = datetime.fromisoformat(str(raw_last_signal_time))
            if last_signal_time.tzinfo is None:
                last_signal_time = last_signal_time.replace(tzinfo=timezone.utc)

        return StrategyState(
            strategy_name=str(payload.get("strategy_name") or strategy_name),
            positions=dict(payload.get("positions") or {}),
            indicators=dict(payload.get("indicators") or {}),
            last_signal_time=last_signal_time,
            custom_state=dict(payload.get("custom_state") or {}),
        )

    # ── Market phase helper ────────────────────────────────────────────────────

    def _get_market_phase(self) -> Optional[MarketPhase]:
        """
        Return current IST market phase for the DynamoCandleConsumer phase check.

        Returns None if the MarketPhase check raises (fail-open: allow polling).
        """
        try:
            return MarketPhase.current()
        except Exception:
            return None

    def _missing_configured_candle_intervals(self) -> set[str]:
        """Return candle strategy intervals not present in strategy.candle_intervals."""
        produced = set(getattr(self._settings.strategy, "candle_intervals", ["minute"]))
        required = {
            getattr(runner._strategy, "candle_interval", "minute")  # noqa: SLF001
            for runner in self._candle_runners
        }
        return required - produced

    # ── Strategy registration ─────────────────────────────────────────────────

    def _register_strategies_from_config(self) -> None:
        """
        Register all 6 strategies as StrategyRunners from instruments.yaml.

        Strategy assignments (Phase 3):
            TICK  : MomentumStrategy — one per market (NSE + US), all active symbols.
            CANDLE: ORBStrategy, Scalp1mStrategy, VWAPReversionStrategy — NSE 1m candles.
            CANDLE: IntradayTrend15mStrategy — NSE 15m candles.
            CANDLE: PreCloseMomentumStrategy — NSE 5m candles.

        All strategies start with paper_trade=True (default StrategyConfig).
        Operators promote to live via the DynamoDB strategy-config table.

        To change which stocks are traded:
            Edit configs/instruments.yaml — set active: true/false.
            Restart the strategy engine — no code change required.
        """
        # ── TICK: MomentumStrategy per market ─────────────────────────────────
        for market in ("NSE", "US"):
            instruments = self._instrument_loader.get_active_instruments(market)
            if not instruments:
                logger.warning(
                    "No active instruments for %s — check configs/instruments.yaml",
                    market,
                )
                continue

            symbols = [i.symbol for i in instruments]
            first = instruments[0]
            sp = first.strategy_params

            momentum = MomentumStrategy(
                name=f"{market.lower()}_momentum_v1",
                symbols=symbols,
                market=market,
                short_window=getattr(sp, "short_window", 20),
                long_window=getattr(sp, "long_window", 50),
                min_confidence=getattr(sp, "min_confidence", 0.65),
            )
            self.register_runner(
                StrategyRunner(
                    strategy=momentum,
                    interface_type=InterfaceType.TICK,
                    config=StrategyConfig(),  # paper_trade=True by default
                )
            )
            logger.info(
                "strategy_engine.registered TICK: %s (%d symbols)",
                momentum.name,
                len(symbols),
            )

        # ── CANDLE: NSE-only strategies ────────────────────────────────────────
        nse_instruments = self._instrument_loader.get_active_instruments("NSE")
        if not nse_instruments:
            logger.warning("No active NSE instruments — candle strategies will not trade")
            return

        nse_symbols = [i.symbol for i in nse_instruments]

        # ORB — 1m candles, opening range breakout
        orb = ORBStrategy(
            name="nse_orb_15m",
            symbols=nse_symbols,
            market="NSE",
        )
        self.register_runner(
            StrategyRunner(
                strategy=orb,
                interface_type=InterfaceType.CANDLE,
                config=StrategyConfig(),
            )
        )
        logger.info(
            "strategy_engine.registered CANDLE: %s (%d symbols, interval=minute)",
            orb.name,
            len(nse_symbols),
        )

        # Scalp 1m — EMA(9/21) crossover with volume confirmation
        scalp = Scalp1mStrategy(
            name="nse_scalp_1m",
            symbols=nse_symbols,
            market="NSE",
        )
        self.register_runner(
            StrategyRunner(
                strategy=scalp,
                interface_type=InterfaceType.CANDLE,
                config=StrategyConfig(),
            )
        )
        logger.info(
            "strategy_engine.registered CANDLE: %s (%d symbols, interval=minute)",
            scalp.name,
            len(nse_symbols),
        )

        # VWAP Reversion — mean-reversion to intraday VWAP, 1m candles
        vwap = VWAPReversionStrategy(
            name="nse_vwap_reversion",
            symbols=nse_symbols,
            market="NSE",
        )
        self.register_runner(
            StrategyRunner(
                strategy=vwap,
                interface_type=InterfaceType.CANDLE,
                config=StrategyConfig(),
            )
        )
        logger.info(
            "strategy_engine.registered CANDLE: %s (%d symbols, interval=minute)",
            vwap.name,
            len(nse_symbols),
        )

        # Intraday Trend 15m — EMA crossover + ADX confirmation, 15m candles
        trend_15m = IntradayTrend15mStrategy(
            name="nse_intraday_trend_15m",
            symbols=nse_symbols,
            market="NSE",
        )
        self.register_runner(
            StrategyRunner(
                strategy=trend_15m,
                interface_type=InterfaceType.CANDLE,
                config=StrategyConfig(),
            )
        )
        logger.info(
            "strategy_engine.registered CANDLE: %s (%d symbols, interval=15minute)",
            trend_15m.name,
            len(nse_symbols),
        )

        # Pre-Close Momentum — directional bias in PRE_CLOSE window, 5m candles
        preclose = PreCloseMomentumStrategy(
            name="nse_preclose_momentum",
            symbols=nse_symbols,
            market="NSE",
        )
        self.register_runner(
            StrategyRunner(
                strategy=preclose,
                interface_type=InterfaceType.CANDLE,
                config=StrategyConfig(),
            )
        )
        logger.info(
            "strategy_engine.registered CANDLE: %s (%d symbols, interval=5minute)",
            preclose.name,
            len(nse_symbols),
        )

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """Whether the service is currently running."""
        return self._running

    @property
    def runner_count(self) -> dict[str, int]:
        """Count of registered runners by interface type."""
        return {
            "total": len(self._runners),
            "tick": len(self._tick_runners),
            "candle": len(self._candle_runners),
        }


async def main() -> None:
    """Entry point for the Strategy Engine Service."""
    service = StrategyEngineService()
    await service.start()


if __name__ == "__main__":
    asyncio.run(main())
