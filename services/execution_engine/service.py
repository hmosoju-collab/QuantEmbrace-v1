"""Execution Engine Service.

Receives ONLY risk-approved signals and translates them into broker orders.
Routes to the correct broker based on instrument market (Zerodha for NSE, Alpaca for US).

CRITICAL: This service NEVER receives signals directly from the strategy engine.
All signals must pass through the risk engine first.

Signal flow:
    signals.approved (Kafka, consumer group execution-v1)
        → execute_approved_signal()
        → broker.place_order()
        → orders.events (Kafka, key = order_id)
"""

import asyncio
import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Optional

from execution_engine.brokers.alpaca_broker import AlpacaBroker
from execution_engine.brokers.base_broker import BrokerClient, NonRetryableBrokerError
from execution_engine.brokers.zerodha_broker import ZerodhaBrokerClient
from execution_engine.consumers.kafka_approved_consumer import (
    ApprovedSignalEvent,
    KafkaApprovedConsumer,
)
from execution_engine.consumers.kafka_kill_switch_listener import (
    KafkaExecutionKillSwitchListener,
)
from execution_engine.orders.order import (
    Market,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    StoredOrder,
)
from execution_engine.orders.order_manager import OrderManager
from execution_engine.polling.bulk_order_poller import BulkOrderPoller
from execution_engine.polling.live_quote_poller import LiveQuotePoller
from execution_engine.polling.position_monitor import PositionMonitor
from execution_engine.publishers.kafka_order_events_publisher import (
    KafkaOrderEventsPublisher,
)
from execution_engine.monitors.orphan_detector import OrphanDetector
from execution_engine.retry.retry_handler import RetryHandler
from shared.config.settings import AppSettings, get_settings
from shared.health.health_server import HealthServer
from shared.health.loop_health import LoopHealthTracker
from shared.kafka.retry_replayer import KafkaRetryReplayer
from shared.logging.logger import get_logger
from shared.metrics.cloudwatch_metrics import get_metrics_client
from shared.risk_state import attr_bool, attr_string, kill_switch_key
from shared.zerodha.market_phase import MarketPhaseGovernor
from shared.zerodha.rate_limiter import EndpointClass, Priority, ZerodhaRateLimiter

logger = get_logger(__name__, service_name="execution_engine")

# CloudWatch metrics — namespace shared with monitoring/main.tf alarm definitions.
# Emits:  OrderPlacementLatencyMs  (P99 alarm at configurable threshold)
#         OrdersSubmitted           (counter per market)
#         OrderPlacementErrors      (counter)
_metrics = get_metrics_client(namespace="QuantEmbrace/Trading")

# Phase 8 (ADR-015 F4): consecutive ORDER_PLACE failures before consumer is gated.
# After this many consecutive broker failures the consumer loop pauses intake until
# the backoff window expires, preventing a flood of orders against an unhealthy broker.
_PLACEMENT_PAUSE_THRESHOLD_FAILURES: int = 3
_PLACEMENT_PAUSE_BACKOFF_SECONDS:    float = 30.0


class ExecutionService:
    """Main execution engine service.

    Responsibilities:
        - Consume risk-approved signals from Kafka (signals.approved, group execution-v1)
        - Route orders to the correct broker (Zerodha for NSE, Alpaca for US)
        - Track order lifecycle in DynamoDB
        - Publish ORDER_FILLED / ORDER_REJECTED events to Kafka (orders.events)
        - Handle retries, partial fills, and circuit breaking

    This service MUST NOT:
        - Generate trading signals (that's strategy_engine)
        - Override risk decisions (that's risk_engine)
        - Receive signals from any source other than risk_engine
    """

    def __init__(self, settings: Optional[AppSettings] = None) -> None:
        self._settings = settings or get_settings()
        self._running = False

        self._zerodha: Optional[ZerodhaBrokerClient] = None
        self._alpaca: Optional[AlpacaBroker] = None
        self._order_manager: Optional[OrderManager] = None
        self._dynamo = None

        # ── Kafka consumers / publishers ──────────────────────────────────────
        self._kafka_consumer: Optional[KafkaApprovedConsumer] = None
        self._kafka_order_publisher: Optional[KafkaOrderEventsPublisher] = None
        self._kill_switch_listener: Optional[KafkaExecutionKillSwitchListener] = None
        self._retry_replayer: Optional[KafkaRetryReplayer] = None
        self._kill_switch_active: bool = False
        self._kill_switch_reason: str = ""
        self._ks_checked_at: float = 0.0
        self._ks_poll_interval: float = getattr(
            getattr(self._settings, "risk", None),
            "kill_switch_poll_interval_seconds",
            1.0,
        )
        self._ack_unknown_recheck_delay_seconds: float = getattr(
            self._settings.execution,
            "ack_unknown_recheck_delay_seconds",
            1.0,
        )

        # ── Fill tracking (ADR-012) ───────────────────────────────────────────
        # BulkOrderPoller: O(1) replacement — ONE kite.orders() call per cycle
        # regardless of open order count. Adaptive interval: 2000ms idle →
        # 300ms during PRE_CLOSE / heavy load.
        self._bulk_poller: Optional[BulkOrderPoller] = None
        self._position_monitor: Optional[PositionMonitor] = None
        self._position_drift_kill_switch: Optional[object] = None

        # LiveQuotePoller: batch bid/ask via kite.quote() every 2s.
        # Writes QUOTE#{market}#{symbol}/LATEST to DynamoDB prices table so
        # RiskContextBuilder._fetch_live_spread_bps() returns real spread data
        # and SpreadGateValidator can reject wide-spread signals.
        self._live_quote_poller: Optional[LiveQuotePoller] = None

        # MarketPhaseGovernor: IST clock → broadcasts phase transitions to pollers.
        self._phase_governor: Optional[MarketPhaseGovernor] = None

        # ZerodhaRateLimiter: token bucket 10 tok/sec, 15 burst.
        # Replaces asyncio.Semaphore(8) which was a concurrency limit, not a rate limit.
        rl_cfg = getattr(self._settings, "zerodha_rate_limit", None)
        self._nse_rate_limiter: ZerodhaRateLimiter = ZerodhaRateLimiter(
            capacity=getattr(rl_cfg, "capacity_per_second", 10),
            burst_capacity=getattr(rl_cfg, "burst_capacity", 10),
            quote_capacity=getattr(rl_cfg, "quote_capacity_per_second", 1),
            historical_capacity=getattr(rl_cfg, "historical_capacity_per_second", 3),
            order_capacity=getattr(rl_cfg, "order_capacity_per_second", 10),
            other_capacity=getattr(rl_cfg, "other_capacity_per_second", 10),
            critical_reserved_tokens=getattr(rl_cfg, "critical_reserved_tokens", 2),
            max_orders_per_second=getattr(rl_cfg, "max_orders_per_second", 10),
            max_orders_per_minute=getattr(rl_cfg, "max_orders_per_minute", 400),
            max_orders_per_day=getattr(rl_cfg, "max_orders_per_day", 5000),
        )

        # Serialise same-signal order handling inside a single worker process.
        # Cross-process dedup is still enforced by DynamoDB signal reservation.
        self._signal_locks: dict[str, asyncio.Lock] = {}
        self._health_server = HealthServer(
            port=getattr(self._settings, "health_check_port", 8080),
            service_name="execution_engine",
        )

        # US / Alpaca: semaphore for concurrency cap (token-bucket is inside AlpacaBroker).
        self._us_semaphore: asyncio.Semaphore = asyncio.Semaphore(20)

        # Phase 8 (ADR-015 F8): orphan position detector (alert-only, no auto-flatten).
        self._orphan_detector: Optional[OrphanDetector] = None

        # Universe order validator — enforces hard rule: no order outside approved snapshot.
        # Built from configs/universe_modes.yaml at startup using UNIVERSE_MODE env var.
        # None = permissive for paper mode; for live mode validate() will block all orders.
        self._universe_validator: Optional[object] = None
        # Stored for the daily snapshot refresh loop — same mode used at startup.
        self._universe_mode_str: str = "PAPER_SAFE_START"

        # Phase 2: Trade Exit Engine — stop-loss / take-profit monitor (bypass signal pipeline).
        self._trade_exit_engine: Optional[object] = None

        # Phase 3.1: Startup position reconciliation report (populated during start()).
        self._reconciliation_report: Optional[object] = None

        # Monitoring: shared LiveCounters instance populated by TEE, router, MIS, and recon.
        # Flushed to JSON every 60s for paper_trading_monitor.py to consume.
        from services.shared.monitoring import LiveCounters as _LC  # noqa: PLC0415
        self._live_counters: _LC = _LC()

        # Phase 8 (ADR-015 F4): placement-pause consumer gate for ORDER_PLACE circuit.
        # When consecutive broker placement failures reach the threshold, new signals
        # are held in the Kafka consumer (offset uncommitted) until the backoff expires.
        # This prevents a flood of rejected orders against an unhealthy Zerodha session.
        self._placement_consecutive_failures: int = 0
        self._placement_paused: bool = False
        self._placement_paused_until: float = 0.0  # monotonic deadline

        self._retry_handler = RetryHandler(
            max_retries=self._settings.execution.max_retries,
            base_delay=self._settings.execution.retry_base_delay,
            max_delay=self._settings.execution.retry_max_delay,
        )

    async def start(self) -> None:
        """Start the execution service.

        Initializes Kafka consumer/publisher, broker connections, reconciles
        state with brokers and DynamoDB, then begins processing approved signals.

        Raises:
            RuntimeError: If KAFKA_BOOTSTRAP_SERVERS is not set or broker
                          credentials are missing.
        """
        logger.info("execution_service.starting")
        await self._health_server.start()

        # ── Kafka bootstrap (mandatory) ───────────────────────────────────────
        kafka_bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
        if not kafka_bootstrap:
            raise RuntimeError(
                "KAFKA_BOOTSTRAP_SERVERS is not set. "
                "The execution engine requires Kafka connectivity to consume "
                "approved signals from signals.approved and publish fill events "
                "to orders.events. Set KAFKA_BOOTSTRAP_SERVERS to the MSK "
                "Serverless bootstrap server string (port 9098)."
            )

        aws_region = os.environ.get("AWS_REGION", "ap-south-1")

        self._kafka_consumer = KafkaApprovedConsumer(
            bootstrap_servers=kafka_bootstrap,
            aws_region=aws_region,
        )
        await self._kafka_consumer.start()

        self._kafka_order_publisher = KafkaOrderEventsPublisher(
            bootstrap_servers=kafka_bootstrap,
            aws_region=aws_region,
        )
        await self._kafka_order_publisher.start()

        self._kill_switch_listener = KafkaExecutionKillSwitchListener(
            bootstrap_servers=kafka_bootstrap,
            aws_region=aws_region,
            on_activate=self._handle_kill_switch_activation,
            on_clear=self._handle_kill_switch_clear,
        )
        await self._kill_switch_listener.start()

        self._retry_replayer = KafkaRetryReplayer(
            bootstrap_servers=kafka_bootstrap,
            aws_region=aws_region,
            source_topics=["signals.approved"],
            source_service="execution_engine",
            consumer_group="execution-retry-v1",
        )
        await self._retry_replayer.start()

        from shared.aws.clients import get_dynamodb_client  # noqa: PLC0415

        # Fail fast with clear messages if broker secrets are absent.
        # (Secrets are injected by EC2 instance profile / Secrets Manager.)
        if self._settings.zerodha is None:
            raise RuntimeError(
                "Zerodha config is not available. Ensure ZERODHA_API_KEY and "
                "ZERODHA_API_SECRET are present in the environment."
            )
        if self._settings.alpaca is None:
            raise RuntimeError(
                "Alpaca config is not available. Ensure ALPACA_API_KEY and "
                "ALPACA_API_SECRET are present in the environment."
            )

        # Wire DynamoDB first so ZerodhaBrokerClient can read the token from
        # LocalStack/DynamoDB during connect() instead of falling back to the
        # (potentially stale) ZERODHA_ACCESS_TOKEN env var.
        dynamo = get_dynamodb_client()
        self._dynamo = dynamo

        # Both constructors accept `settings` — not individual credential kwargs.
        # The brokers load credentials internally (Secrets Manager → env var fallback).
        self._zerodha = ZerodhaBrokerClient(settings=self._settings, dynamo_client=dynamo)
        self._alpaca = AlpacaBroker(settings=self._settings)

        # Establish broker connections and authenticate before reconciliation.
        await self._zerodha.connect()
        await self._alpaca.connect()
        self._order_manager = OrderManager(
            dynamo_client=dynamo,
            orders_table=self._settings.aws.dynamodb_table_orders,
            positions_table=self._settings.aws.dynamodb_table_positions,
            risk_state_table=self._settings.aws.dynamodb_table_risk_state,
        )

        # Load durable DynamoDB kill-switch state before any reconciliation or
        # Kafka processing. This is the fallback path used when Kafka/SNS fanout
        # is unhealthy.
        await self._refresh_durable_kill_switch_state(force=True, cancel_open_orders=True)

        # Reconcile state on startup — critical for restart safety
        await self._reconcile_state()

        # Phase 3.1: Position-level consistency check. Runs before TEE starts so
        # any ZERO_QTY_OPEN or UNMANAGED positions are repaired/alerted before
        # the exit engine begins its first polling cycle.
        await self._run_startup_position_reconciliation()

        # Populate LiveCounters from reconciliation report (if available).
        if self._reconciliation_report is not None:
            r = self._reconciliation_report
            self._live_counters.recon_ran     = True
            self._live_counters.recon_mode    = getattr(r, "mode", "paper")
            self._live_counters.recon_mismatches   = getattr(r, "total_mismatches", 0)
            self._live_counters.recon_repairs       = getattr(r, "repaired", 0)
            self._live_counters.recon_criticals     = getattr(r, "alerted", 0)
            for m in getattr(r, "mismatches", []):
                mtype = getattr(getattr(m, "mismatch_type", None), "value", "")
                if mtype == "ZERO_QTY_OPEN":
                    self._live_counters.recon_zero_qty_open += 1
                elif mtype == "STALE_EXIT_LOCK":
                    self._live_counters.recon_stale_exit_lock += 1
                elif mtype == "QTY_DIRECTION":
                    self._live_counters.recon_qty_direction += 1
                elif mtype == "UNMANAGED":
                    self._live_counters.recon_unmanaged += 1

        from execution_engine.mis_square_off import MISSquareOffManager  # noqa: PLC0415

        mis_manager = MISSquareOffManager(
            zerodha_broker=self._zerodha,
            order_manager=self._order_manager,
            dynamo_client=get_dynamodb_client(),
            positions_table=self._settings.aws.dynamodb_table_positions,
            kill_switch_table=self._settings.aws.dynamodb_table_risk_state,
            live_counters=self._live_counters,
            paper_trading=getattr(self._settings.execution, "paper_trading", True),
        )
        self._mis_manager = mis_manager

        dynamo = self._dynamo or get_dynamodb_client()
        self._dynamo = dynamo
        rl_cfg = self._settings.zerodha_rate_limit

        # ── ADR-012: Token bucket rate limiter (replaces Semaphore(8)) ────────
        self._nse_rate_limiter = ZerodhaRateLimiter(
            capacity=rl_cfg.capacity_per_second,
            burst_capacity=rl_cfg.burst_capacity,
            quote_capacity=rl_cfg.quote_capacity_per_second,
            historical_capacity=rl_cfg.historical_capacity_per_second,
            order_capacity=rl_cfg.order_capacity_per_second,
            other_capacity=rl_cfg.other_capacity_per_second,
            critical_reserved_tokens=rl_cfg.critical_reserved_tokens,
            max_orders_per_second=rl_cfg.max_orders_per_second,
            max_orders_per_minute=rl_cfg.max_orders_per_minute,
            max_orders_per_day=rl_cfg.max_orders_per_day,
        )
        await self._nse_rate_limiter.start()  # launches background drain loop

        # ── ADR-012: Market phase governor ────────────────────────────────────
        self._phase_governor = MarketPhaseGovernor()
        self._phase_governor.add_listener(self._nse_rate_limiter.set_market_phase)

        # ── ADR-012: Bulk order poller (O(1) — replaces O(N) per-order polling) ─
        self._bulk_poller = BulkOrderPoller(
            zerodha=self._zerodha,
            order_manager=self._order_manager,
            dynamo_client=dynamo,
            fills_table=self._settings.aws.dynamodb_table_fills,
            rate_limiter=self._nse_rate_limiter,
            phase_governor=self._phase_governor,
            kafka_publisher=self._kafka_order_publisher,  # Phase 2: publish fills to orders.events
            protective_stop_callback=self._maybe_place_protective_stop,
            settings=self._settings,
        )

        # ── Universe order validator ───────────────────────────────────────────
        # Build before LiveQuotePoller so the approved-symbol snapshot is ready
        # for the watchlist derivation below.
        try:
            from shared.universe.modes import UniverseMode as _UniverseMode  # noqa: PLC0415
            from shared.universe.order_validator import (  # noqa: PLC0415
                build_validator_for_today as _build_universe_validator,
            )
            _universe_mode_str = os.environ.get("UNIVERSE_MODE", "PAPER_SAFE_START")
            self._universe_mode_str = _universe_mode_str
            _universe_mode = _UniverseMode.from_string(_universe_mode_str)
            _use_live_api = os.environ.get("UNIVERSE_USE_LIVE_API", "false").strip().lower() in ("true", "1", "yes")
            self._universe_use_live_api: bool = _use_live_api
            self._universe_validator = _build_universe_validator(
                mode=_universe_mode,
                fail_if_no_snapshot=_universe_mode.is_live,
                use_live_api=_use_live_api,
            )
            _snap = getattr(self._universe_validator, "snapshot", None)
            _snap_size = getattr(_snap, "size", 0) if _snap is not None else 0
            logger.info(
                "execution_service.universe_validator_ready mode=%s approved=%d",
                _universe_mode.value, _snap_size,
            )
        except Exception as _uv_exc:
            if _universe_mode.is_live:
                raise RuntimeError(
                    f"FATAL: Universe validator init failed in LIVE mode "
                    f"(UNIVERSE_MODE={_universe_mode.value}) — halting service startup. "
                    f"Fix the config/YAML error and restart. error={_uv_exc}"
                ) from _uv_exc
            logger.error(
                "execution_service.universe_validator_init_failed error=%s — "
                "validator disabled; paper orders will proceed, live orders blocked at validate()",
                _uv_exc,
            )
            self._universe_validator = None

        # ── PHASE4-FU-001: Live quote poller — writes spread data to DynamoDB ──
        # Instrument list derived from the universe snapshot (same source the
        # UniverseOrderValidator uses) so all three — strategy_engine signals,
        # universe hard gate, and spread polling — track the same symbol set.
        # Falls back to STRATEGY_WATCHLIST_NSE env var if no snapshot is available.
        _lqp_snap = getattr(self._universe_validator, "snapshot", None) if self._universe_validator else None
        _lqp_symbols = getattr(_lqp_snap, "approved_symbols", None) if _lqp_snap else None
        if _lqp_symbols:
            nse_instruments = [f"NSE:{sym}" for sym in sorted(_lqp_symbols)]
            logger.info(
                "live_quote_poller.watchlist_from_universe symbols=%d", len(nse_instruments)
            )
        elif self._settings.strategy.watchlist_nse:
            nse_instruments = [f"NSE:{sym}" for sym in self._settings.strategy.watchlist_nse]
            logger.info(
                "live_quote_poller.watchlist_from_settings symbols=%d", len(nse_instruments)
            )
        else:
            nse_instruments = []
            logger.warning("live_quote_poller.watchlist_empty — no universe snapshot or STRATEGY_WATCHLIST_NSE")
        # Zerodha's kite.quote() only supports exchange:symbol keys for Indian
        # venues. US spread quotes need a separate Alpaca-backed writer.
        watchlist = nse_instruments

        self._live_quote_poller = LiveQuotePoller(
            zerodha=self._zerodha,
            instruments=watchlist,
            rate_limiter=self._nse_rate_limiter,
            phase_governor=self._phase_governor,
            max_spread_bps=self._settings.risk.max_spread_bps,
            dynamo_client=dynamo,
            prices_table=self._settings.aws.dynamodb_table_prices,
            settings=self._settings,
        )

        _is_paper = getattr(self._settings.execution, "paper_trading", True)
        if not _is_paper and getattr(rl_cfg, "position_monitor_enabled", True):
            from risk_engine.killswitch.killswitch import KillSwitch  # noqa: PLC0415

            self._position_drift_kill_switch = KillSwitch(
                dynamo_client=dynamo,
                table_name=self._settings.aws.dynamodb_table_risk_state,
                settings=self._settings,
            )
            self._position_monitor = PositionMonitor(
                zerodha=self._zerodha,
                order_manager=self._order_manager,
                rate_limiter=self._nse_rate_limiter,
                phase_governor=self._phase_governor,
                auto_sync=False,
                kill_switch=self._position_drift_kill_switch,
                activate_kill_switch_on_drift=True,
                on_drift=self._handle_position_drift_local_halt,
                settings=self._settings,
            )

        # Phase 2: Trade Exit Engine — monitors stop-loss / take-profit for all
        # managed positions. Exits bypass the signal pipeline entirely.
        from execution_engine.exit.exit_order_router import ExitOrderRouter, TradingMode  # noqa: PLC0415
        from execution_engine.monitors.trade_exit_engine import TradeExitEngine           # noqa: PLC0415

        _tee_mode = TradingMode.PAPER if getattr(
            self._settings.execution, "paper_trading", False
        ) else TradingMode.LIVE
        _exit_router = ExitOrderRouter(
            mode=_tee_mode,
            dynamo_client=dynamo,
            positions_table=self._settings.aws.dynamodb_table_positions,
            order_manager=self._order_manager,
            zerodha_broker=self._zerodha,
            kafka_publisher=self._kafka_order_publisher,
            live_trading_enabled=getattr(self._settings.execution, "live_trading_enabled", False),
            live_counters=self._live_counters,
        )
        self._trade_exit_engine = TradeExitEngine(
            dynamo_client=dynamo,
            positions_table=self._settings.aws.dynamodb_table_positions,
            router=_exit_router,
            prices_table=self._settings.aws.dynamodb_table_prices,
            live_counters=self._live_counters,
        )

        # Phase 8 (ADR-015 F8): orphan detector — scan FILLED entries without active SL.
        self._orphan_detector = OrphanDetector(
            order_manager=self._order_manager,
            metrics=get_metrics_client(),
        )

        # Register readiness checks only after every dependency object exists.
        # /ready stays 503 until the runtime loops below have actually started.
        self._health_server.add_check(
            "zerodha_connected",
            lambda: self._zerodha is not None and getattr(self._zerodha, "_connected", False),
        )
        self._health_server.add_check(
            "alpaca_connected",
            lambda: self._alpaca is not None and getattr(self._alpaca, "_connected", False),
        )
        self._health_server.add_check(
            "order_manager_ready",
            lambda: self._order_manager is not None,
        )
        self._health_server.add_check(
            "kafka_consumer_running",
            lambda: self._kafka_consumer is not None and self._kafka_consumer._running,
        )
        self._health_server.add_check(
            "kafka_order_publisher_running",
            lambda: (
                self._kafka_order_publisher is not None and self._kafka_order_publisher._running
            ),
        )
        self._health_server.add_check(
            "kill_switch_listener_running",
            lambda: (
                self._kill_switch_listener is not None
                and self._kill_switch_listener.running
            ),
        )
        self._health_server.add_check(
            "kafka_retry_replayer_running",
            lambda: self._retry_replayer is not None,
        )
        self._health_server.add_check(
            "rate_limiter_ready",
            lambda: (self._nse_rate_limiter is not None and self._nse_rate_limiter.is_running),
        )
        self._health_server.add_check(
            "bulk_poller_running",
            lambda: self._bulk_poller is not None and self._bulk_poller._running,
        )
        self._health_server.add_check(
            "live_quote_poller_running",
            lambda: self._live_quote_poller is not None and self._live_quote_poller._running,
        )
        if not _is_paper and getattr(rl_cfg, "position_monitor_enabled", True):
            self._health_server.add_check(
                "position_monitor_running",
                lambda: self._position_monitor is not None and self._position_monitor._running,
            )
        self._health_server.add_check(
            "phase_governor_running",
            lambda: self._phase_governor is not None and self._phase_governor._running,
        )
        self._health_server.add_check(
            "orphan_detector_ready",
            lambda: self._orphan_detector is not None,
        )

        # Mark service as up in LiveCounters before launching runtime tasks.
        self._live_counters.execution_engine_up = True

        # Run core loops concurrently:
        #   1. _kafka_processing_loop  — main order execution loop (signals.approved)
        #   2. _margin_refresh_loop    — feeds risk engine with fresh margin data
        #   3. mis_manager.run()       — closes NSE MIS before 15:15 IST
        #   4. _phase_governor.run()   — IST phase transitions → broadcast to pollers
        #   5. _bulk_poller.start()    — O(1) fill detection (ADR-012)
        #   6. _live_quote_poller.start() — bid/ask spread cache + DynamoDB write (Phase 4)
        #   7. _position_monitor.start() — broker-vs-Dynamo position drift kill switch
        #   8. _retry_replayer.run()   — drain signals.approved.retry back to primary
        #   9. _trade_exit_engine.run() — stop-loss/TP exits (bypass signal pipeline)
        #  10. _monitoring_flush_loop  — serialize LiveCounters to JSON every 60s
        #  11. _zerodha.start_token_refresh_loop() — hot-swap daily token at 02:00 UTC
        #  12. _universe_snapshot_refresh_loop — rebuild approved-symbol set at midnight IST
        self._running = True
        runtime_tasks = [
            asyncio.create_task(self._kafka_processing_loop(), name="execution-kafka"),
            asyncio.create_task(
                self._kill_switch_listener.listen(), name="execution-kill-switch"
            ),
            asyncio.create_task(
                self._durable_kill_switch_poll_loop(),
                name="execution-durable-kill-switch",
            ),
            asyncio.create_task(self._retry_replayer.run(), name="execution-retry-replayer"),
            asyncio.create_task(self._margin_refresh_loop(), name="execution-margin-refresh"),
            asyncio.create_task(mis_manager.run(), name="execution-mis-square-off"),
            asyncio.create_task(self._phase_governor.run(), name="execution-phase-governor"),
            asyncio.create_task(self._bulk_poller.start(), name="execution-bulk-poller"),
            asyncio.create_task(self._live_quote_poller.start(), name="execution-live-quotes"),
            asyncio.create_task(self._trade_exit_engine.run(), name="execution-tee"),
            asyncio.create_task(self._monitoring_flush_loop(), name="execution-monitoring-flush"),
            asyncio.create_task(
                self._universe_snapshot_refresh_loop(), name="execution-universe-refresh"
            ),
        ]
        if self._position_monitor is not None:
            runtime_tasks.append(
                asyncio.create_task(
                    self._position_monitor.start(),
                    name="execution-position-monitor",
                )
            )
        if self._zerodha is not None:
            runtime_tasks.append(
                asyncio.create_task(
                    self._zerodha.start_token_refresh_loop(),
                    name="execution-zerodha-token-refresh",
                )
            )
        if self._orphan_detector is not None:
            runtime_tasks.append(
                asyncio.create_task(
                    self._orphan_detector.run(),
                    name="execution-orphan-detector",
                )
            )

        # B-002: add done-callbacks so any task crash is logged CRITICAL immediately
        # rather than silently disappearing until the gather propagates the exception.
        def _on_task_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                lh = LoopHealthTracker(task.get_name() or "unknown", _metrics, service_name="execution_engine")
                lh.record_crash(exc)
                logger.critical(
                    "execution_engine.task_crashed task=%s error_type=%s error=%s — "
                    "service restart required",
                    task.get_name(), type(exc).__name__, repr(exc),
                )

        for _t in runtime_tasks:
            _t.add_done_callback(_on_task_done)

        try:
            await self._wait_until_runtime_ready(runtime_tasks)
            self._health_server.set_ready(True)
            logger.info("execution_service.started")
            await asyncio.gather(*runtime_tasks)
        finally:
            self._health_server.set_ready(False)

    async def _wait_until_runtime_ready(
        self,
        runtime_tasks: list[asyncio.Task],
        timeout_seconds: float = 5.0,
    ) -> None:
        """
        Wait until execution runtime loops have started before reporting ready.

        If a poller or governor fails during startup, readiness remains false and
        the exception is surfaced instead of advertising a half-started service.
        """
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for task in runtime_tasks:
                if not task.done():
                    continue
                if task.cancelled():
                    raise RuntimeError(
                        "Execution runtime task was cancelled during startup: " f"{task.get_name()}"
                    )
                exc = task.exception()
                if exc is not None:
                    raise RuntimeError(
                        "Execution runtime task failed during startup: " f"{task.get_name()}"
                    ) from exc

            if (
                self._bulk_poller is not None
                and self._bulk_poller._running
                and self._live_quote_poller is not None
                and self._live_quote_poller._running
                and (
                    self._position_monitor is None
                    or self._position_monitor._running
                )
                and self._phase_governor is not None
                and self._phase_governor._running
            ):
                return

            await asyncio.sleep(0.05)

        raise TimeoutError("Execution runtime did not become ready within startup timeout")

    async def stop(self) -> None:
        """Gracefully stop the execution service.

        Stops consuming new signals, waits for in-flight orders to settle,
        then shuts down Kafka and broker connections.
        """
        logger.info("execution_service.stopping")
        self._running = False
        self._health_server.set_ready(False)

        # Stop Kafka consumer — no new signals while draining
        if self._kafka_consumer is not None:
            await self._kafka_consumer.stop()
        if self._kill_switch_listener is not None:
            await self._kill_switch_listener.stop()
        if self._retry_replayer is not None:
            await self._retry_replayer.stop()

        # Stop pollers — no new fills while in-flight order counting is happening
        if self._bulk_poller is not None:
            await self._bulk_poller.stop()
        if self._live_quote_poller is not None:
            await self._live_quote_poller.stop()
        if self._orphan_detector is not None:
            await self._orphan_detector.stop()
        if self._position_monitor is not None:
            await self._position_monitor.stop()
        if self._phase_governor is not None:
            await self._phase_governor.stop()
        if self._nse_rate_limiter is not None:
            await self._nse_rate_limiter.stop()

        # Drain Kafka order events publisher before closing
        if self._kafka_order_publisher is not None:
            await self._kafka_order_publisher.stop()

        # Stop the MIS square-off manager if it's running
        if hasattr(self, "_mis_manager") and self._mis_manager is not None:
            self._mis_manager.stop()

        # Wait for in-flight orders to reach terminal state
        if self._order_manager:
            await self._order_manager.wait_for_inflight_orders(timeout_seconds=30)

        # Cleanly close broker connections
        if self._zerodha:
            await self._zerodha.disconnect()
        if self._alpaca:
            await self._alpaca.disconnect()

        await self._health_server.stop()
        logger.info("execution_service.stopped")

    async def execute_approved_signal(
        self,
        signal_id: str,
        risk_decision_id: str,
        trace_id: str,
        symbol: str,
        side: OrderSide,
        quantity: float,
        order_type: OrderType,
        market: Market,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        product_type: Optional[ProductType] = None,
        take_profit: Optional[float] = None,
        expires_at: Optional[datetime] = None,
    ) -> OrderResponse:
        """Execute a risk-approved signal by placing an order with the broker.

        Args:
            signal_id: Original signal identifier from strategy engine.
            risk_decision_id: Risk approval ID — proves this signal was validated.
            trace_id: Trace ID propagated from originating tick event.
            symbol: Trading symbol (e.g., 'RELIANCE', 'AAPL').
            side: BUY or SELL.
            quantity: Number of shares/units to trade.
            order_type: MARKET, LIMIT, STOP_LOSS, etc.
            market: NSE or US — determines which broker to route to.
            limit_price: Limit price for LIMIT orders.
            stop_price: Stop price for stop-loss orders.
            product_type: Broker product type (MIS/CNC/NRML/DAY).
            take_profit: Protective target carried for audit/outbox metadata.

        Returns:
            OrderResponse with order ID and initial status.

        Raises:
            ValueError: If risk_decision_id is missing (signal not risk-approved).
        """
        if not risk_decision_id:
            raise ValueError(
                "Cannot execute signal without risk_decision_id. "
                "All signals MUST pass through risk engine first."
            )
        if expires_at is not None and datetime.now(timezone.utc) > expires_at:
            raise ValueError(
                f"Approved signal {signal_id} expired at {expires_at.isoformat()} "
                "before broker execution"
            )
        await self._refresh_durable_kill_switch_state(
            force=False,
            cancel_open_orders=True,
        )
        if self._kill_switch_active:
            raise RuntimeError(
                f"Execution kill switch active; refusing new broker order for "
                f"signal_id={signal_id}. reason={self._kill_switch_reason}"
            )

        # Hard universe validation rule: symbol must be in the approved snapshot.
        # Paper mode: permissive if no snapshot (warns). Live mode: BLOCKS if not approved.
        if self._universe_validator is not None:
            _uv = self._universe_validator.validate(
                symbol=symbol, market=market.value if hasattr(market, "value") else str(market)
            )
            if not _uv.approved:
                raise ValueError(
                    f"Universe validation rejected {market}:{symbol} — {_uv.reason}. "
                    f"signal_id={signal_id} risk_decision_id={risk_decision_id}. "
                    "Add symbol to universe_modes.yaml or request manual override."
                )

        signal_lock = self._signal_locks.setdefault(signal_id, asyncio.Lock())
        async with signal_lock:
            # ── Step 1: Check for an existing order for this signal ──────────────
            #
            # Three cases:
            #
            # A. PENDING — a previous attempt wrote the DynamoDB record but the
            #    broker call failed or the process crashed before it completed.
            #    The order is stranded: the dedup check would normally block retry,
            #    and reconciliation skips PENDING rows (no broker_order_id yet).
            #    Fix: reuse the existing order_id and retry broker placement
            #    directly, bypassing submit_order (already written).
            #
            # B. Any active or terminal state (PLACED / PARTIALLY_FILLED / FILLED /
            #    REJECTED / CANCELLED) — this is a genuine Kafka duplicate (consumer
            #    rebalance or at-least-once redelivery). Return the existing record
            #    without touching the broker.
            #
            # C. Not found — first-time submission. Fall through to the atomic
            #    write + broker placement path below.

            existing = await self._order_manager.get_order_by_signal(signal_id)

            if existing is not None:
                if existing.status == OrderStatus.PENDING:
                    # Case A: retry the broker call for a stranded PENDING order.
                    # Rebuild OrderRequest reusing the stored order_id so that
                    # record_order() updates the correct DynamoDB item.
                    order_request = OrderRequest(
                        order_id=existing.order_id,
                        signal_id=signal_id,
                        risk_decision_id=risk_decision_id,
                        symbol=symbol,
                        side=side,
                        quantity=quantity,
                        order_type=order_type,
                        market=market,
                        limit_price=limit_price,
                        stop_price=stop_price,
                        product_type=product_type or existing.product_type,
                        metadata={
                            **existing.metadata,
                            "take_profit": take_profit,
                            "expires_at": expires_at.isoformat() if expires_at else None,
                            "broker_idempotency_key": (
                                existing.broker_idempotency_key
                                or OrderManager.broker_idempotency_key(existing.order_id)
                            ),
                        },
                        created_at=datetime.now(timezone.utc),
                    )
                    logger.info(
                        "execution_service.retrying_pending_order",
                        signal_id=signal_id,
                        order_id=existing.order_id,
                    )
                    # Skip submit_order — DynamoDB record + signal reservation
                    # already exist from the first attempt.
                    broker = self._get_broker(market)
                    response = await self._place_order_with_broker_idempotency(
                        broker=broker,
                        order_request=order_request,
                        market=market,
                        symbol=symbol,
                    )
                    await self._order_manager.record_order(response)
                    await self._maybe_place_protective_stop(
                        parent_order=order_request,
                        entry_response=response,
                        filled_quantity=response.filled_quantity,
                        fill_id=self._protective_fill_key(response.order_id, response.filled_quantity),
                    )
                    logger.info(
                        "execution_service.pending_order_placed",
                        order_id=response.order_id,
                        signal_id=signal_id,
                        market=market.value,
                    )
                    self._signal_locks.pop(signal_id, None)
                    return response
                elif existing.status == OrderStatus.ACK_UNKNOWN:
                    order_request = self._rebuild_order_request_from_existing(
                        existing=existing,
                        risk_decision_id=risk_decision_id,
                        limit_price=limit_price,
                        stop_price=stop_price,
                        take_profit=take_profit,
                        expires_at=expires_at,
                    )
                    broker = self._get_broker(market)
                    response = await self._recover_ack_unknown_order(
                        broker=broker,
                        order_request=order_request,
                        symbol=symbol,
                        reason="approved signal replay found ACK_UNKNOWN order",
                    )
                    await self._order_manager.record_order(response)
                    await self._maybe_place_protective_stop(
                        parent_order=order_request,
                        entry_response=response,
                        filled_quantity=response.filled_quantity,
                        fill_id=self._protective_fill_key(
                            response.order_id,
                            response.filled_quantity,
                        ),
                    )
                    self._signal_locks.pop(signal_id, None)
                    return response
                else:
                    # Case B: Kafka duplicate (consumer rebalance / at-least-once).
                    logger.warning(
                        "execution_service.duplicate_signal",
                        signal_id=signal_id,
                        existing_order_id=existing.order_id,
                        existing_status=existing.status.value,
                    )
                    self._signal_locks.pop(signal_id, None)
                    return existing

            # ── Step 2: First-time submission ─────────────────────────────────
            # Build a fresh OrderRequest (new order_id generated by default_factory).
            order_request = OrderRequest(
                signal_id=signal_id,
                risk_decision_id=risk_decision_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=order_type,
                market=market,
                limit_price=limit_price,
                stop_price=stop_price,
                product_type=product_type or (
                    ProductType.MIS if market == Market.NSE else ProductType.DAY
                ),
                metadata={
                    "take_profit": take_profit,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                },
                created_at=datetime.now(timezone.utc),
            )
            self._order_manager.attach_broker_idempotency(order_request)

            # Atomically write the order record + signal reservation.
            # submit_order uses transact_write_items so concurrent consumers that
            # each generate a different order_id for the same signal_id still race
            # on the SIGNAL#{signal_id} reservation — only one wins.
            submitted = await self._order_manager.submit_order(order_request)
            if not submitted:
                # Another consumer won the signal_id reservation race.
                existing = await self._order_manager.get_order_by_signal(signal_id)
                if existing:
                    logger.warning(
                        "execution_service.concurrent_duplicate",
                        signal_id=signal_id,
                        winning_order_id=existing.order_id,
                    )
                    self._signal_locks.pop(signal_id, None)
                    return existing
                # Transaction cancelled AND no record found — should never happen,
                # but raise so the Kafka processing loop can log and continue.
                raise RuntimeError(
                    f"submit_order transaction cancelled but no record found for "
                    f"signal_id={signal_id}."
                )

            # ── Step 3: Place order with broker ───────────────────────────────
            broker = self._get_broker(market)

            response = await self._place_order_with_broker_idempotency(
                broker=broker,
                order_request=order_request,
                market=market,
                symbol=symbol,
            )

            await self._order_manager.record_order(response)
            await self._maybe_place_protective_stop(
                parent_order=order_request,
                entry_response=response,
                filled_quantity=response.filled_quantity,
                fill_id=self._protective_fill_key(response.order_id, response.filled_quantity),
            )

            logger.info(
                "execution_service.order_placed",
                order_id=response.order_id,
                symbol=symbol,
                side=side.value,
                quantity=quantity,
                market=market.value,
                risk_decision_id=risk_decision_id,
            )

            self._signal_locks.pop(signal_id, None)
            return response

    async def _place_order_with_broker_idempotency(
        self,
        *,
        broker: BrokerClient,
        order_request: OrderRequest,
        market: Market,
        symbol: str,
        allow_when_kill_switch_active: bool = False,
        placement_reason: str = "entry",
    ) -> OrderResponse:
        """
        Place a broker order through a replay-safe idempotency wrapper.

        The method first asks the broker whether an order with the same client
        idempotency key already exists. If the placement call raises after the
        broker request may have crossed the network boundary, the order is
        marked ``ACK_UNKNOWN`` and only a delayed broker tag scan may recover it.
        It never blindly submits a second broker order.
        """
        idempotency_key = order_request.metadata.get("broker_idempotency_key")
        if not idempotency_key:
            self._order_manager.attach_broker_idempotency(order_request)
            idempotency_key = order_request.metadata["broker_idempotency_key"]

        if self._kill_switch_active and not allow_when_kill_switch_active:
            raise RuntimeError(
                "Execution kill switch active before broker placement; "
                f"order_id={order_request.order_id}"
            )
        if self._kill_switch_active and allow_when_kill_switch_active:
            logger.critical(
                "execution_service.kill_switch_allows_risk_reducing_order",
                order_id=order_request.order_id,
                symbol=symbol,
                placement_reason=placement_reason,
                kill_switch_reason=self._kill_switch_reason,
            )

        existing = await broker.find_order_by_client_order_id(
            str(idempotency_key),
            order_request,
        )
        if existing is not None and existing.broker_order_id:
            logger.warning(
                "execution_service.broker_order_recovered_by_idempotency",
                order_id=order_request.order_id,
                broker_order_id=existing.broker_order_id,
                broker_idempotency_key=idempotency_key,
            )
            return existing

        if market == Market.NSE:
            priority = (
                Priority.CRITICAL
                if placement_reason in {"protective_stop", "square_off", "kill_switch"}
                else Priority.HIGH
            )
            await self._nse_rate_limiter.acquire(
                priority,
                EndpointClass.ORDER_PLACE,
            )

        try:
            if market == Market.NSE:
                response = await broker.place_order(order_request)
            else:
                async with self._us_semaphore:
                    response = await broker.place_order(order_request)
            # Successful broker call: reset the placement failure streak.
            self._placement_consecutive_failures = 0
            return response
        except NonRetryableBrokerError:
            await self._order_manager.update_order_status(
                order_id=order_request.order_id,
                new_status=OrderStatus.REJECTED,
                broker_message="Non-retryable broker rejection during placement",
            )
            # NonRetryable = broker understood the request; not a connectivity failure.
            # Reset the streak so a single bad order doesn't trigger circuit pause.
            self._placement_consecutive_failures = 0
            raise
        except Exception as exc:
            await self._order_manager.mark_order_ack_unknown(
                order_id=order_request.order_id,
                broker_message=(
                    f"Broker placement acknowledgement unknown after {placement_reason}: {exc}"
                ),
            )
            # Transient failure (timeout, network error, broker unhealthy) — increment
            # the consecutive failure counter and open the placement pause circuit.
            self._record_placement_failure(market)
            return await self._recover_ack_unknown_order(
                broker=broker,
                order_request=order_request,
                symbol=symbol,
                reason=f"{placement_reason} placement exception: {exc}",
            )

    def _rebuild_order_request_from_existing(
        self,
        *,
        existing: StoredOrder,
        risk_decision_id: str,
        limit_price: Optional[float],
        stop_price: Optional[float],
        take_profit: Optional[float],
        expires_at: Optional[datetime],
    ) -> OrderRequest:
        """Reconstruct a broker request from a persisted reserved order row."""
        metadata = {
            **(existing.metadata or {}),
            "take_profit": take_profit,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "broker_idempotency_key": (
                existing.broker_idempotency_key
                or OrderManager.broker_idempotency_key(existing.order_id)
            ),
        }
        return OrderRequest(
            order_id=existing.order_id,
            signal_id=existing.signal_id,
            risk_decision_id=risk_decision_id or existing.risk_decision_id,
            symbol=existing.symbol,
            side=existing.side,
            quantity=existing.quantity,
            order_type=existing.order_type,
            market=existing.market,
            limit_price=limit_price if limit_price is not None else existing.limit_price,
            stop_price=stop_price if stop_price is not None else existing.stop_price,
            product_type=existing.product_type,
            metadata=metadata,
            created_at=existing.order_created_at or datetime.now(timezone.utc),
        )

    async def _recover_ack_unknown_order(
        self,
        *,
        broker: BrokerClient,
        order_request: OrderRequest,
        symbol: str,
        reason: str,
    ) -> OrderResponse:
        """
        Re-scan broker history for an ambiguous order without placing a new one.

        This is the only allowed automated path out of ACK_UNKNOWN. If the broker
        tag is not visible, the order remains ACK_UNKNOWN and source processing
        must retry later or require operator intervention.
        """
        idempotency_key = order_request.metadata.get("broker_idempotency_key")
        if not idempotency_key:
            self._order_manager.attach_broker_idempotency(order_request)
            idempotency_key = order_request.metadata["broker_idempotency_key"]

        delay = max(0.0, self._ack_unknown_recheck_delay_seconds)
        if delay > 0:
            await asyncio.sleep(delay)

        existing = await broker.find_order_by_client_order_id(
            str(idempotency_key),
            order_request,
        )
        if existing is not None and existing.broker_order_id:
            logger.critical(
                "execution_service.ack_unknown_recovered",
                order_id=order_request.order_id,
                broker_order_id=existing.broker_order_id,
                symbol=symbol,
                reason=reason,
            )
            return existing

        raise RuntimeError(
            "ACK_UNKNOWN unresolved; refusing blind second broker placement "
            f"order_id={order_request.order_id} symbol={symbol} reason={reason}"
        )

    @staticmethod
    def _protective_fill_key(order_id: str, filled_quantity: float) -> str:
        raw = f"{order_id}|{filled_quantity:.8f}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _calculate_delta_fill_price(
        *,
        prior_qty: float,
        prior_avg: float,
        new_qty: float,
        new_avg: float,
    ) -> float:
        """Infer the latest fill slice price from cumulative broker fill fields."""
        delta_qty = new_qty - prior_qty
        if delta_qty <= 0:
            return new_avg
        prior_cost = prior_qty * prior_avg
        new_cost = new_qty * new_avg
        return max(0.0, (new_cost - prior_cost) / delta_qty)

    @staticmethod
    def _is_protective_order(order: OrderRequest | StoredOrder) -> bool:
        metadata = getattr(order, "metadata", {}) or {}
        return bool(
            getattr(order, "is_protective", False)
            or metadata.get("protective_type")
            or str(getattr(order, "order_id", "")).startswith("SL-")
        )

    @staticmethod
    def _is_paper_order(order: OrderRequest | StoredOrder) -> bool:
        """True if ``order`` is a simulated paper order that must never reach a real broker.

        Paper orders are written by ``_handle_paper_order`` with a synthetic
        ``broker_order_id``/``order_id`` of the form ``PAPER-...`` and
        ``metadata["paper_trade"]=True``.  Any one of those signals is
        sufficient; all three are checked so a record missing one field
        (e.g. a legacy paper record) is still recognised as paper.

        This guard preserves paper/live isolation during startup
        reconciliation: querying a real broker for a ``PAPER-...`` id would
        either error or leak a paper identifier into a live broker API call.
        """
        if str(getattr(order, "broker_order_id", "") or "").startswith("PAPER-"):
            return True
        metadata = getattr(order, "metadata", {}) or {}
        if bool(metadata.get("paper_trade", False)):
            return True
        return str(getattr(order, "order_id", "") or "").startswith("PAPER-")

    async def _maybe_place_protective_stop(
        self,
        *,
        parent_order: OrderRequest | StoredOrder,
        entry_response: OrderResponse | StoredOrder,
        filled_quantity: float,
        fill_id: str,
    ) -> OrderResponse | None:
        """
        Place a broker-side protective stop for the newly confirmed entry fill.

        A child order ID is deterministic per parent/fill_id, so replaying the
        same fill cannot create another stop. The child is persisted before the
        broker call and linked back to the parent after broker acknowledgement.
        """
        if self._is_protective_order(parent_order):
            logger.debug(
                "execution_service.protective_child_fill_not_reprotected",
                parent_order_id=entry_response.order_id,
                fill_id=fill_id,
            )
            return None

        stop_price = getattr(parent_order, "stop_price", None)
        if stop_price is None or stop_price <= 0 or filled_quantity <= 0:
            return None

        if self._kill_switch_active:
            logger.critical(
                "execution_service.protective_stop_placing_during_kill_switch",
                parent_order_id=entry_response.order_id,
                fill_id=fill_id,
                kill_switch_reason=self._kill_switch_reason,
            )

        protective_side = (
            OrderSide.SELL if parent_order.side == OrderSide.BUY else OrderSide.BUY
        )
        child_order_id = f"SL-{entry_response.order_id}-{fill_id}"
        child_signal_id = f"{parent_order.signal_id}#SL#{fill_id}"
        metadata = {
            "parent_order_id": entry_response.order_id,
            "parent_broker_order_id": entry_response.broker_order_id,
            "protective_type": "STOP_LOSS",
            "entry_fill_id": fill_id,
            "take_profit": getattr(parent_order, "metadata", {}).get("take_profit"),
        }
        child_request = OrderRequest(
            order_id=child_order_id,
            signal_id=child_signal_id,
            risk_decision_id=parent_order.risk_decision_id,
            symbol=parent_order.symbol,
            side=protective_side,
            quantity=filled_quantity,
            order_type=OrderType.STOP_LOSS_MARKET,
            market=parent_order.market,
            stop_price=stop_price,
            product_type=parent_order.product_type,
            metadata=metadata,
            created_at=datetime.now(timezone.utc),
        )
        self._order_manager.attach_broker_idempotency(child_request)

        child_created = await self._order_manager.submit_child_order(
            child_request,
            parent_order_id=entry_response.order_id,
            protective_type="STOP_LOSS",
        )
        if not child_created:
            existing_child = await self._order_manager.get_stored_order(child_order_id)
            return existing_child

        broker = self._get_broker(parent_order.market)
        try:
            child_response = await self._place_order_with_broker_idempotency(
                broker=broker,
                order_request=child_request,
                market=parent_order.market,
                symbol=parent_order.symbol,
                allow_when_kill_switch_active=True,
                placement_reason="protective_stop",
            )
        except Exception as exc:
            await self._handle_protective_stop_failure(
                parent_order=parent_order,
                entry_response=entry_response,
                failed_child_order_id=child_order_id,
                filled_quantity=filled_quantity,
                fill_id=fill_id,
                reason=str(exc),
            )
            return None
        await self._order_manager.record_order(child_response)
        await self._order_manager.link_protective_child(
            parent_order_id=entry_response.order_id,
            child_order_id=child_response.order_id,
            child_broker_order_id=child_response.broker_order_id,
            protective_type="STOP_LOSS",
            protected_quantity=filled_quantity,
        )
        logger.info(
            "execution_service.protective_stop_placed",
            parent_order_id=entry_response.order_id,
            child_order_id=child_response.order_id,
            child_broker_order_id=child_response.broker_order_id,
            quantity=filled_quantity,
            stop_price=stop_price,
        )
        return child_response

    async def _handle_protective_stop_failure(
        self,
        *,
        parent_order: OrderRequest | StoredOrder,
        entry_response: OrderResponse | StoredOrder,
        failed_child_order_id: str,
        filled_quantity: float,
        fill_id: str,
        reason: str,
    ) -> None:
        """Fail closed when a filled entry cannot be protected by a child stop."""
        halt_reason = (
            "PROTECTIVE_STOP_FAILED "
            f"parent_order_id={entry_response.order_id} child_order_id={failed_child_order_id} "
            f"symbol={parent_order.symbol} qty={filled_quantity} reason={reason}"
        )
        logger.critical(
            "execution_service.protective_stop_failed_flattening",
            parent_order_id=entry_response.order_id,
            failed_child_order_id=failed_child_order_id,
            symbol=parent_order.symbol,
            filled_quantity=filled_quantity,
            reason=reason,
        )

        await self._handle_kill_switch_activation(
            reason=halt_reason,
            activated_by="protective_stop_failure",
        )

        flatten_order_id = ""
        flatten_broker_order_id = ""
        try:
            if self._order_manager is not None:
                await self._order_manager.update_order_status(
                    order_id=failed_child_order_id,
                    new_status=OrderStatus.REJECTED,
                    broker_message=(
                        "Protective stop could not be confirmed; emergency flatten attempted"
                    ),
                )

            flatten_response = await self._place_emergency_flatten_order(
                parent_order=parent_order,
                entry_response=entry_response,
                filled_quantity=filled_quantity,
                fill_id=fill_id,
            )
            if flatten_response is not None:
                flatten_order_id = flatten_response.order_id
                flatten_broker_order_id = flatten_response.broker_order_id
        except Exception as exc:
            logger.critical(
                "execution_service.emergency_flatten_failed",
                parent_order_id=entry_response.order_id,
                symbol=parent_order.symbol,
                filled_quantity=filled_quantity,
                error=str(exc),
            )
        finally:
            if self._order_manager is not None:
                await self._order_manager.mark_protection_failed(
                    parent_order_id=entry_response.order_id,
                    reason=halt_reason,
                    flatten_order_id=flatten_order_id,
                    flatten_broker_order_id=flatten_broker_order_id,
                )

    async def _place_emergency_flatten_order(
        self,
        *,
        parent_order: OrderRequest | StoredOrder,
        entry_response: OrderResponse | StoredOrder,
        filled_quantity: float,
        fill_id: str,
    ) -> OrderResponse | None:
        """Place a deterministic market order to flatten an unprotected fill."""
        if filled_quantity <= 0:
            return None

        flatten_side = OrderSide.SELL if parent_order.side == OrderSide.BUY else OrderSide.BUY
        flatten_order_id = f"FLATTEN-{entry_response.order_id}-{fill_id}"
        metadata = {
            "parent_order_id": entry_response.order_id,
            "parent_broker_order_id": entry_response.broker_order_id,
            "protective_type": "EMERGENCY_FLATTEN",
            "entry_fill_id": fill_id,
            "reason": "protective_stop_failed",
        }
        flatten_request = OrderRequest(
            order_id=flatten_order_id,
            signal_id=f"{parent_order.signal_id}#FLATTEN#{fill_id}",
            risk_decision_id=parent_order.risk_decision_id,
            symbol=parent_order.symbol,
            side=flatten_side,
            quantity=filled_quantity,
            order_type=OrderType.MARKET,
            market=parent_order.market,
            product_type=parent_order.product_type,
            metadata=metadata,
            created_at=datetime.now(timezone.utc),
        )
        self._order_manager.attach_broker_idempotency(flatten_request)

        created = await self._order_manager.submit_child_order(
            flatten_request,
            parent_order_id=entry_response.order_id,
            protective_type="EMERGENCY_FLATTEN",
        )
        if not created:
            existing = await self._order_manager.get_stored_order(flatten_order_id)
            if existing is not None and existing.broker_order_id:
                return existing

        broker = self._get_broker(parent_order.market)
        response = await self._place_order_with_broker_idempotency(
            broker=broker,
            order_request=flatten_request,
            market=parent_order.market,
            symbol=parent_order.symbol,
            allow_when_kill_switch_active=True,
            placement_reason="square_off",
        )
        await self._order_manager.record_order(response)
        logger.critical(
            "execution_service.emergency_flatten_placed",
            parent_order_id=entry_response.order_id,
            flatten_order_id=response.order_id,
            flatten_broker_order_id=response.broker_order_id,
            symbol=parent_order.symbol,
            quantity=filled_quantity,
        )
        return response

    def _get_broker(self, market: Market) -> BrokerClient:
        """Route to the correct broker based on market.

        Args:
            market: NSE for Indian equities, US for American equities.

        Returns:
            The appropriate broker client instance.
        """
        if market == Market.NSE:
            return self._zerodha
        elif market == Market.US:
            return self._alpaca
        else:
            raise ValueError(f"Unsupported market: {market}")

    def _get_market_semaphore(self, market: Market):
        """Return the backpressure guard for the given market.

        NSE uses the ZerodhaRateLimiter (token bucket, 10 tok/sec, 15 burst).
        US uses a concurrency semaphore (token-bucket is inside AlpacaBroker).

        Args:
            market: NSE or US.

        Returns:
            An async context manager that throttles the broker API call.
        """
        return self._nse_rate_limiter if market == Market.NSE else self._us_semaphore

    async def _reconcile_state(self) -> None:
        """Reconcile local state with brokers on startup.

        Two separate passes over open orders:

        Pass 1 — PENDING orders (stranded before broker placement):
            These exist in DynamoDB but were never submitted to the broker
            (the service crashed or timed out between ``submit_order`` and
            ``broker.place_order``).  Re-route them through
            ``execute_approved_signal`` which detects the PENDING status and
            retries broker placement using the existing order_id.

        Pass 2 — PLACED / PARTIALLY_FILLED orders (placed but not confirmed):
            Query the broker for the current status and update DynamoDB if the
            broker has advanced the order to a terminal state while we were down.
        """
        logger.info("execution_service.reconciling_state")

        open_orders = await self._order_manager.get_open_orders()
        pending_count = 0
        placed_count = 0
        paper_skipped = 0

        for order in open_orders:
            if order.status == OrderStatus.ACK_UNKNOWN:
                try:
                    broker = self._get_broker(order.market)
                    order_request = self._rebuild_order_request_from_existing(
                        existing=order,
                        risk_decision_id=order.risk_decision_id,
                        limit_price=order.limit_price,
                        stop_price=order.stop_price,
                        take_profit=order.metadata.get("take_profit"),
                        expires_at=None,
                    )
                    response = await self._recover_ack_unknown_order(
                        broker=broker,
                        order_request=order_request,
                        symbol=order.symbol,
                        reason="startup reconciliation",
                    )
                    await self._order_manager.record_order(response)
                    logger.critical(
                        "execution_service.reconciled_ack_unknown",
                        order_id=order.order_id,
                        broker_order_id=response.broker_order_id,
                        symbol=order.symbol,
                    )
                except Exception as exc:
                    logger.critical(
                        "execution_service.ack_unknown_unresolved_startup",
                        order_id=order.order_id,
                        symbol=order.symbol,
                        error=str(exc),
                    )
                continue

            if order.status == OrderStatus.PENDING:
                # Pass 1: re-attempt broker placement for stranded PENDING orders.
                pending_count += 1
                try:
                    if self._is_protective_order(order):
                        await self._place_pending_protective_order(order)
                        continue

                    if not order.signal_id or not order.risk_decision_id:
                        logger.error(
                            "execution_service.pending_order_missing_ids",
                            order_id=order.order_id,
                            symbol=order.symbol,
                            note="signal_id or risk_decision_id is empty — "
                            "marking REJECTED to prevent infinite retry",
                        )
                        await self._order_manager.update_order_status(
                            order_id=order.order_id,
                            new_status=OrderStatus.REJECTED,
                            broker_message="Corrupt PENDING record: missing signal_id/risk_decision_id",
                        )
                        continue

                    expires_raw = order.metadata.get("expires_at")
                    expires_at = None
                    if expires_raw:
                        try:
                            expires_at = datetime.fromisoformat(str(expires_raw))
                            if expires_at.tzinfo is None:
                                expires_at = expires_at.replace(tzinfo=timezone.utc)
                        except ValueError:
                            expires_at = None

                    await self.execute_approved_signal(
                        signal_id=order.signal_id,
                        risk_decision_id=order.risk_decision_id,
                        trace_id="",
                        symbol=order.symbol,
                        side=order.side,
                        quantity=order.quantity,
                        order_type=order.order_type,
                        market=order.market,
                        limit_price=order.limit_price,
                        stop_price=order.stop_price,
                        product_type=order.product_type,
                        take_profit=order.metadata.get("take_profit"),
                        expires_at=expires_at,
                    )
                except Exception as exc:
                    logger.error(
                        "execution_service.pending_retry_error",
                        order_id=order.order_id,
                        error=str(exc),
                    )

            elif order.broker_order_id:
                # Pass 2: check broker status for already-placed orders.
                #
                # Paper-isolation guard (Phase 2.1 / Q3): a paper order is
                # simulated and was never placed with a real broker.  Its
                # broker_order_id is a synthetic "PAPER-..." token.  Calling
                # _get_broker(...).get_order_status(...) for it would query a
                # real broker with a paper identifier — breaking the strict
                # paper/live isolation invariant.  Skip it entirely.
                if self._is_paper_order(order):
                    paper_skipped += 1
                    logger.debug(
                        "execution_service.reconcile_skip_paper_order",
                        order_id=order.order_id,
                        broker_order_id=order.broker_order_id,
                        status=order.status.value,
                    )
                    continue

                placed_count += 1
                try:
                    broker = self._get_broker(order.market)
                    current_status = await broker.get_order_status(order.broker_order_id)

                    if current_status.new_status != order.status:
                        await self._order_manager.update_order_status(
                            order_id=order.order_id,
                            new_status=current_status.new_status,
                            filled_quantity=current_status.filled_quantity,
                            average_price=current_status.avg_fill_price,
                        )
                        logger.info(
                            "execution_service.reconciled_order",
                            order_id=order.order_id,
                            old_status=order.status.value,
                            new_status=current_status.new_status.value,
                            filled_quantity=current_status.filled_quantity,
                        )

                        cumulative_qty = current_status.filled_quantity or 0.0
                        fill_price = current_status.avg_fill_price or 0.0
                        prior_qty = order.filled_quantity or 0.0
                        delta_qty = max(0.0, cumulative_qty - prior_qty)

                        if (
                            current_status.new_status
                            in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
                            and delta_qty > 0
                        ):
                            delta_price = self._calculate_delta_fill_price(
                                prior_qty=prior_qty,
                                prior_avg=order.avg_fill_price or 0.0,
                                new_qty=cumulative_qty,
                                new_avg=fill_price,
                            )
                            await self._order_manager.apply_fill_to_position(
                                symbol=order.symbol,
                                side=order.side,
                                filled_quantity=delta_qty,
                                avg_fill_price=delta_price,
                                last_price=delta_price,
                                order_id=order.order_id,
                                signal_id=order.signal_id,
                                risk_decision_id=order.risk_decision_id,
                                order_type=order.order_type.value,
                                market_str=order.market.value,
                            )
                            await self._maybe_place_protective_stop(
                                parent_order=order,
                                entry_response=order,
                                filled_quantity=delta_qty,
                                fill_id=self._protective_fill_key(
                                    order.order_id,
                                    cumulative_qty,
                                ),
                            )
                            logger.info(
                                "execution_service.reconcile_position_updated",
                                symbol=order.symbol,
                                side=order.side.value,
                                filled_quantity=delta_qty,
                                avg_fill_price=delta_price,
                            )

                except Exception as exc:
                    logger.error(
                        "execution_service.reconciliation_error",
                        order_id=order.order_id,
                        error=str(exc),
                    )

        logger.info(
            "execution_service.reconciliation_complete",
            total_open=len(open_orders),
            pending_retried=pending_count,
            placed_checked=placed_count,
            paper_skipped=paper_skipped,
        )

    # ── Phase 3.1: Startup position reconciliation ────────────────────────────

    async def _run_startup_position_reconciliation(self) -> None:
        """
        Run PositionReconciliationService once during startup, before TEE begins
        its first polling cycle.

        Mode behaviour:
          paper   — scan + auto-repair safe mismatches (ZERO_QTY_OPEN).
                    UNMANAGED positions log CRITICAL; STALE_EXIT_LOCK logs WARNING.
          live    — scan only; all mismatches log CRITICAL, no auto-repair.
                    Live mode remains gated in Phase 3 — this path logs but does
                    not block startup unless reconciliation_strict_startup=True.
          backtest — skipped entirely.

        Controlled by ExecutionConfig fields:
          reconciliation_enabled          (master switch)
          reconciliation_run_on_startup   (startup-only gate)
          reconciliation_strict_startup   (False = warn and continue; True = raise)
        """
        from execution_engine.reconciliation.reconciliation import (  # noqa: PLC0415
            PositionReconciliationService,
        )

        cfg = self._settings.execution
        enabled = getattr(cfg, "reconciliation_enabled", True)
        run_on_startup = getattr(cfg, "reconciliation_run_on_startup", True)
        strict = getattr(cfg, "reconciliation_strict_startup", False)
        is_paper = getattr(cfg, "paper_trading", True)
        is_backtest = getattr(cfg, "backtest_mode", False)

        if not enabled or not run_on_startup:
            logger.info(
                "execution_service.startup_reconciliation_skipped",
                reason="disabled_by_config",
            )
            return

        if is_backtest:
            logger.info(
                "execution_service.startup_reconciliation_skipped",
                reason="backtest_mode",
            )
            return

        mode = "paper" if is_paper else "live"
        logger.info("execution_service.startup_reconciliation_started", mode=mode)

        reconciler = PositionReconciliationService(
            dynamo_client=self._dynamo,
            positions_table=self._settings.aws.dynamodb_table_positions,
            mode=mode,
        )

        try:
            report = await reconciler.run()
            self._reconciliation_report = report

            # A report is safe-to-start when it is clean, or when every mismatch
            # was either repaired (paper) or alerted (live).  An unrepaired CRITICAL
            # mismatch that was NOT alerted would indicate a reconciler bug — treat
            # that as unsafe but do not crash unless strict mode is requested.
            safe = report.clean or (report.repaired + report.alerted == report.total_mismatches)

            logger.info(
                "execution_service.startup_reconciliation_completed",
                mode=mode,
                mismatch_count=report.total_mismatches,
                repaired_count=report.repaired,
                critical_count=report.alerted,
                clean=report.clean,
                safe_to_start_trade_exit_engine=safe,
            )

            if not report.clean:
                logger.warning(
                    "execution_service.startup_reconciliation_mismatches_found",
                    mode=mode,
                    details=[
                        {
                            "symbol": m.symbol,
                            "type": m.mismatch_type.value,
                            "repaired": m.repaired,
                            "detail": m.detail,
                        }
                        for m in report.mismatches
                    ],
                )
                if strict and not safe:
                    raise RuntimeError(
                        f"Startup reconciliation found {report.total_mismatches} unresolvable "
                        f"mismatches and reconciliation_strict_startup=True."
                    )

        except RuntimeError:
            raise
        except Exception as exc:
            logger.error(
                "execution_service.startup_reconciliation_failed",
                mode=mode,
                error=str(exc),
            )
            if strict:
                raise RuntimeError(
                    f"Startup reconciliation failed and reconciliation_strict_startup=True: {exc}"
                ) from exc

    async def _place_pending_protective_order(self, order: StoredOrder) -> None:
        """Place a previously reserved protective child order during recovery."""
        if not order.stop_price or order.stop_price <= 0 or order.quantity <= 0:
            logger.error(
                "execution_service.pending_protective_order_invalid",
                order_id=order.order_id,
                symbol=order.symbol,
                stop_price=order.stop_price,
                quantity=order.quantity,
            )
            return

        metadata = dict(order.metadata or {})
        parent_order_id = order.parent_order_id or str(metadata.get("parent_order_id") or "")
        if not parent_order_id:
            logger.error(
                "execution_service.pending_protective_order_missing_parent",
                order_id=order.order_id,
                symbol=order.symbol,
            )
            return

        order_request = OrderRequest(
            order_id=order.order_id,
            signal_id=order.signal_id,
            risk_decision_id=order.risk_decision_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            order_type=order.order_type,
            market=order.market,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            product_type=order.product_type,
            metadata=metadata,
            created_at=order.order_created_at or datetime.now(timezone.utc),
        )
        self._order_manager.attach_broker_idempotency(order_request)

        broker = self._get_broker(order.market)
        response = await self._place_order_with_broker_idempotency(
            broker=broker,
            order_request=order_request,
            market=order.market,
            symbol=order.symbol,
            allow_when_kill_switch_active=True,
            placement_reason="pending_protective_reconcile",
        )
        await self._order_manager.record_order(response)
        await self._order_manager.link_protective_child(
            parent_order_id=parent_order_id,
            child_order_id=response.order_id,
            child_broker_order_id=response.broker_order_id,
            protective_type=order.protective_type or "STOP_LOSS",
            protected_quantity=order.quantity,
        )
        logger.critical(
            "execution_service.pending_protective_order_recovered",
            order_id=response.order_id,
            broker_order_id=response.broker_order_id,
            parent_order_id=parent_order_id,
            symbol=order.symbol,
            kill_switch_active=self._kill_switch_active,
        )

    # ── Kafka processing loop ─────────────────────────────────────────────────

    async def _kafka_processing_loop(self) -> None:
        """
        Poll signals.approved for SIGNAL_APPROVED events and execute each one.

        Runs continuously until stop() is called. Uses asyncio.to_thread() to
        call the synchronous confluent-kafka Consumer.poll() without blocking
        the event loop.

        Idempotency:
            execute_approved_signal() deduplicates by signal_id using a
            DynamoDB conditional write, so Kafka redeliveries are safe.

        On fill: publishes ORDER_FILLED / ORDER_PARTIAL to orders.events so
            the risk engine can update real-time P&L and position state.
        """
        logger.info("execution_service.kafka_processing_loop.started")

        while self._running:
            # Phase 8 (ADR-015 F4): placement-pause gate.
            # When the ORDER_PLACE circuit is open (consecutive broker failures), hold
            # new signals in the consumer buffer until the backoff window expires.
            # The source offset is NOT committed so signals are re-delivered on recovery.
            if self._placement_paused:
                if time.monotonic() >= self._placement_paused_until:
                    self._placement_paused = False
                    self._placement_consecutive_failures = 0
                    logger.info(
                        "execution_service.placement_pause_cleared "
                        "— resuming new signal intake"
                    )
                else:
                    await asyncio.sleep(0.5)
                    continue

            try:
                approved: Optional[ApprovedSignalEvent] = await asyncio.to_thread(
                    self._kafka_consumer.poll_approved
                )
                if approved is None:
                    continue

                try:
                    if datetime.now(timezone.utc) > approved.expires_at:
                        routed = self._kafka_consumer.publish_dlq(
                            approved,
                            reason=(
                                f"Approved signal expired at {approved.expires_at.isoformat()} "
                                "before execution processing"
                            ),
                            error_type="stale_approved_signal",
                            details={
                                "signal_id": approved.signal_id,
                                "risk_decision_id": approved.risk_decision_id,
                                "raw_topic": approved.raw_topic,
                                "raw_offset": approved.raw_offset,
                            },
                        )
                        if routed:
                            self._kafka_consumer.commit(approved)
                        continue

                    ok = await self._handle_approved_signal_event(approved)
                    if ok:
                        self._kafka_consumer.commit(approved)
                    else:
                        routed = self._kafka_consumer.publish_retry(
                            approved,
                            reason="Approved signal handling failed",
                            error_type="execution_processing_failed",
                            details={
                                "signal_id": approved.signal_id,
                                "risk_decision_id": approved.risk_decision_id,
                                "raw_topic": approved.raw_topic,
                                "raw_offset": approved.raw_offset,
                            },
                        )
                        if routed:
                            self._kafka_consumer.commit(approved)

                except Exception as exc:
                    routed = self._kafka_consumer.publish_retry(
                        approved,
                        reason=str(exc),
                        error_type="execution_processing_failed",
                        details={
                            "signal_id": approved.signal_id,
                            "risk_decision_id": approved.risk_decision_id,
                            "raw_topic": approved.raw_topic,
                            "raw_offset": approved.raw_offset,
                        },
                    )
                    if routed:
                        self._kafka_consumer.commit(approved)
                    else:
                        raise

            except Exception:
                logger.exception("execution_service.kafka_processing_loop.unexpected_error")
                await asyncio.sleep(1)

        logger.info("execution_service.kafka_processing_loop.stopped")

    async def _handle_approved_signal_event(self, approved: ApprovedSignalEvent) -> bool:
        """
        Execute a single SIGNAL_APPROVED event from Kafka and publish the result
        to orders.events.

        Phase 3: If approved.paper_trade is True, route to _handle_paper_order()
        which logs a simulated fill without touching any live broker API.

        Args:
            approved: Parsed ApprovedSignalEvent from KafkaApprovedConsumer.
        """
        _t0 = time.perf_counter()

        if datetime.now(timezone.utc) > approved.expires_at:
            logger.warning(
                "execution_service.stale_approved_signal_rejected",
                signal_id=approved.signal_id,
                risk_decision_id=approved.risk_decision_id,
                expires_at=approved.expires_at.isoformat(),
            )
            return True

        side = OrderSide(approved.direction.upper())
        market = Market(approved.market.upper())

        # ── Phase 3: paper_trade routing ──────────────────────────────────────
        if approved.paper_trade:
            try:
                await self._handle_paper_order(approved, side, market)
                _metrics.record_count(
                    "PaperOrdersSimulated",
                    dimensions={"Market": market.value},
                )
                asyncio.create_task(_metrics.flush())
            except Exception:
                logger.exception(
                    "execution_service.paper_order_error",
                    signal_id=approved.signal_id,
                    symbol=approved.symbol,
                )
                return False
            return True  # paper path is complete — never falls through to live broker

        try:
            product_type = ProductType(approved.product_type)
            response = await self.execute_approved_signal(
                signal_id=approved.signal_id,
                risk_decision_id=approved.risk_decision_id,
                trace_id=approved.trace_id,
                symbol=approved.symbol,
                side=side,
                quantity=approved.quantity,
                order_type=OrderType.MARKET,
                market=market,
                limit_price=approved.price_at_signal,
                stop_price=approved.stop_loss,
                product_type=product_type,
                take_profit=approved.take_profit,
                expires_at=approved.expires_at,
            )

            # Publish ORDER_FILLED / ORDER_PARTIAL to orders.events so the
            # risk engine P&L and position state reflect this fill.
            if response.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                immediate_fill_qty = float(getattr(response, "filled_quantity", 0.0) or 0.0)
                immediate_fill_price = float(
                    getattr(response, "avg_fill_price", approved.price_at_signal) or 0.0
                )
                if immediate_fill_qty > 0:
                    await self._order_manager.apply_fill_to_position(
                        symbol=approved.symbol,
                        side=side,
                        filled_quantity=immediate_fill_qty,
                        avg_fill_price=immediate_fill_price,
                        last_price=immediate_fill_price,
                        order_id=response.order_id,
                        signal_id=approved.signal_id,
                        risk_decision_id=approved.risk_decision_id,
                        order_type=OrderType.MARKET.value,
                        market_str=market.value,
                        signal_price=approved.price_at_signal,
                    )
                await self._kafka_order_publisher.publish_fill(
                    order_id=response.order_id,
                    signal_id=approved.signal_id,
                    risk_decision_id=approved.risk_decision_id,
                    trace_id=approved.trace_id,
                    symbol=approved.symbol,
                    market=approved.market,
                    direction=approved.direction,
                    quantity_ordered=int(approved.quantity),
                    quantity_filled=int(immediate_fill_qty),
                    avg_fill_price=immediate_fill_price,
                    broker_order_id=response.broker_order_id or response.order_id,
                    strategy_id=approved.strategy_id,
                    product_type=approved.product_type,
                    expires_at=approved.expires_at.isoformat(),
                    stop_loss=approved.stop_loss,
                    take_profit=approved.take_profit,
                )
            elif response.status == OrderStatus.REJECTED:
                await self._kafka_order_publisher.publish_rejection(
                    order_id=response.order_id,
                    signal_id=approved.signal_id,
                    risk_decision_id=approved.risk_decision_id,
                    trace_id=approved.trace_id,
                    symbol=approved.symbol,
                    market=approved.market,
                    direction=approved.direction,
                    quantity_ordered=int(approved.quantity),
                    broker_order_id=response.broker_order_id or response.order_id,
                    reject_reason=getattr(response, "broker_message", "REJECTED"),
                    strategy_id=approved.strategy_id,
                    product_type=approved.product_type,
                    expires_at=approved.expires_at.isoformat(),
                    stop_loss=approved.stop_loss,
                    take_profit=approved.take_profit,
                )

            # Emit latency + count metrics
            _metrics.record_latency(
                "OrderPlacementLatencyMs",
                _t0,
                dimensions={"Market": market.value},
            )
            _metrics.record_count(
                "OrdersSubmitted",
                dimensions={"Market": market.value},
            )
            asyncio.create_task(_metrics.flush())
            return True

        except Exception:
            logger.exception(
                "execution_service.signal_handling_error",
                signal_id=approved.signal_id,
                symbol=approved.symbol,
                market=approved.market,
            )
            _metrics.record_count(
                "OrderPlacementErrors",
                dimensions={"Market": market.value},
            )
            asyncio.create_task(_metrics.flush())
            return False

    # ── Phase 5: Paper order simulator ───────────────────────────────────────

    def _paper_probability_hit(
        self,
        approved: "ApprovedSignalEvent",
        *,
        salt: str,
        probability: float,
    ) -> bool:
        """Deterministically decide a simulated paper outcome for replay safety."""
        if probability <= 0:
            return False
        if probability >= 1:
            return True
        import random  # noqa: PLC0415

        seed = getattr(
            self._settings.execution,
            "paper_random_seed",
            "quantembrace-paper-v1",
        )
        material = f"{seed}|{approved.signal_id}|{approved.risk_decision_id}|{salt}"
        return random.Random(material).random() < probability

    def _paper_fill_price(
        self,
        approved: "ApprovedSignalEvent",
        side: "OrderSide",
    ) -> float:
        """
        Simulate an adverse executable price.

        BUY crosses half-spread and positive slippage upward; SELL crosses
        downward.  ``paper_market_open_gap_bps`` is an additional adverse shock
        so gap testing cannot accidentally improve paper fills.
        """
        cfg = self._settings.execution
        adverse_bps = (
            float(getattr(cfg, "paper_slippage_bps", 5.0))
            + float(getattr(cfg, "paper_spread_bps", 10.0)) / 2.0
            + float(getattr(cfg, "paper_market_open_gap_bps", 0.0))
        )
        direction = 1.0 if side == OrderSide.BUY else -1.0
        return round(float(approved.price_at_signal) * (1.0 + direction * adverse_bps / 10_000.0), 4)

    def _paper_partial_quantity(self, approved: "ApprovedSignalEvent") -> float:
        """Return a deterministic partial-fill quantity less than the order size."""
        qty = float(approved.quantity)
        if qty <= 1:
            return qty
        min_pct = float(getattr(self._settings.execution, "paper_partial_fill_min_pct", 0.25))
        min_pct = min(max(min_pct, 0.01), 0.99)
        raw = f"{approved.signal_id}|partial_qty|{getattr(self._settings.execution, 'paper_random_seed', '')}"
        bucket = int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
        fill_pct = min_pct + (0.95 - min_pct) * bucket
        return max(1.0, min(qty - 1.0, int(qty * fill_pct)))

    # ── Phase 3/5: Paper order handler ───────────────────────────────────────

    async def _handle_paper_order(
        self,
        approved: "ApprovedSignalEvent",
        side: "OrderSide",
        market: "Market",
    ) -> None:
        """
        Simulate order execution for paper_trade=True signals.

        Behaviour:
            - Applies configurable latency, spread, slippage, gap, partial fill,
              reject, and circuit-lock simulation.
            - Publishes a synthetic ORDER_FILLED / ORDER_PARTIAL / ORDER_REJECTED event with
              paper=True in the metadata so the risk engine tracks paper P&L
            - Records the simulated order in DynamoDB orders table
            - Does NOT call Zerodha or Alpaca
            - Does NOT consume any ZerodhaRateLimiter tokens

        This gives the risk engine real paper P&L tracking — the drawdown monitor
        sees paper fills just like live fills (they carry the same orders.events
        schema, differentiated only by the paper=True metadata field).

        Args:
            approved: Parsed SIGNAL_APPROVED event.
            side: Resolved OrderSide (BUY/SELL).
            market: Resolved Market (NSE/US).
        """
        import uuid as _uuid  # noqa: PLC0415

        if self._order_manager is not None:
            existing = await self._order_manager.get_order_by_signal(approved.signal_id)
            if existing is not None:
                logger.warning(
                    "execution_service.paper_duplicate_signal",
                    signal_id=approved.signal_id,
                    existing_order_id=existing.order_id,
                    existing_status=existing.status.value,
                )
                return

        paper_order_id = f"PAPER-{_uuid.uuid4().hex[:16].upper()}"
        cfg = self._settings.execution
        latency_ms = int(getattr(cfg, "paper_latency_ms", 250))
        if latency_ms > 0:
            await asyncio.sleep(latency_ms / 1000.0)

        reject_reason: str | None = None
        if self._paper_probability_hit(
            approved,
            salt="circuit_lock",
            probability=float(getattr(cfg, "paper_circuit_lock_probability", 0.0)),
        ):
            reject_reason = "PAPER_CIRCUIT_LOCK"
        elif self._paper_probability_hit(
            approved,
            salt="reject",
            probability=float(getattr(cfg, "paper_reject_probability", 0.0)),
        ):
            reject_reason = "PAPER_REJECTED_BY_SIMULATOR"

        simulated_fill_price = self._paper_fill_price(approved, side)
        filled_quantity = float(approved.quantity)
        status = OrderStatus.FILLED
        if reject_reason is not None:
            filled_quantity = 0.0
            status = OrderStatus.REJECTED
        elif self._paper_probability_hit(
            approved,
            salt="partial",
            probability=float(getattr(cfg, "paper_partial_fill_probability", 0.0)),
        ):
            filled_quantity = self._paper_partial_quantity(approved)
            if filled_quantity < float(approved.quantity):
                status = OrderStatus.PARTIALLY_FILLED

        logger.info(
            "execution_service.paper_order_simulated",
            signal_id=approved.signal_id,
            risk_decision_id=approved.risk_decision_id,
            paper_order_id=paper_order_id,
            symbol=approved.symbol,
            market=market.value,
            side=side.value,
            quantity=approved.quantity,
            filled_quantity=filled_quantity,
            status=status.value,
            fill_price=simulated_fill_price,
            reject_reason=reject_reason,
        )

        # Record paper fill in DynamoDB orders table (PAPER_FILLED status).
        # Uses the same table as live orders so reconciliation tooling sees it.
        if self._order_manager is not None:
            try:
                from execution_engine.orders.order import (  # noqa: PLC0415
                    OrderRequest,
                    OrderResponse,
                )

                paper_req = OrderRequest(
                    order_id=paper_order_id,
                    signal_id=approved.signal_id,
                    risk_decision_id=approved.risk_decision_id,
                    symbol=approved.symbol,
                    side=side,
                    quantity=approved.quantity,
                    order_type=OrderType.MARKET,
                    market=market,
                    product_type=ProductType(approved.product_type),
                    stop_price=approved.stop_loss,
                    limit_price=approved.price_at_signal,
                    metadata={
                        "take_profit": approved.take_profit,
                        "paper_trade": True,
                        "paper_slippage_bps": getattr(cfg, "paper_slippage_bps", 5.0),
                        "paper_spread_bps": getattr(cfg, "paper_spread_bps", 10.0),
                        "paper_market_open_gap_bps": getattr(cfg, "paper_market_open_gap_bps", 0.0),
                    },
                    created_at=datetime.now(timezone.utc),
                )
                paper_resp = OrderResponse(
                    order_id=paper_order_id,
                    broker_order_id=paper_order_id,
                    signal_id=approved.signal_id,
                    risk_decision_id=approved.risk_decision_id,
                    status=status,
                    symbol=approved.symbol,
                    side=side,
                    quantity=approved.quantity,
                    filled_quantity=filled_quantity,
                    avg_fill_price=simulated_fill_price if filled_quantity > 0 else 0.0,
                    market=market,
                    broker_message=reject_reason or status.value,
                )
                submitted = await self._order_manager.submit_order(paper_req)
                if not submitted:
                    # Conditional write failed — another concurrent consumer won the
                    # signal_id reservation race.  Do NOT call record_order or apply_fill
                    # — doing so would overwrite the winning thread's order record and
                    # double-count the position.
                    logger.warning(
                        "execution_service.paper_duplicate_suppressed "
                        "signal_id=%s — submit_order conditional write lost race",
                        approved.signal_id,
                    )
                    return
                await self._order_manager.record_order(paper_resp)
            except Exception:
                logger.exception(
                    "execution_service.paper_order_dynamo_error",
                    signal_id=approved.signal_id,
                    paper_order_id=paper_order_id,
                )

        # Publish synthetic ORDER_FILLED to orders.events so risk engine
        # P&L tracking reflects paper fills exactly as it would live fills.
        if self._kafka_order_publisher is not None:
            try:
                if status == OrderStatus.REJECTED:
                    await self._kafka_order_publisher.publish_rejection(
                        order_id=paper_order_id,
                        signal_id=approved.signal_id,
                        risk_decision_id=approved.risk_decision_id,
                        trace_id=approved.trace_id,
                        symbol=approved.symbol,
                        market=approved.market,
                        direction=approved.direction,
                        quantity_ordered=int(approved.quantity),
                        broker_order_id=paper_order_id,
                        reject_reason=reject_reason or "PAPER_REJECTED",
                        strategy_id=approved.strategy_id,
                        product_type=approved.product_type,
                        expires_at=approved.expires_at.isoformat(),
                        stop_loss=approved.stop_loss,
                        take_profit=approved.take_profit,
                    )
                else:
                    await self._kafka_order_publisher.publish_fill(
                        order_id=paper_order_id,
                        signal_id=approved.signal_id,
                        risk_decision_id=approved.risk_decision_id,
                        trace_id=approved.trace_id,
                        symbol=approved.symbol,
                        market=approved.market,
                        direction=approved.direction,
                        quantity_ordered=int(approved.quantity),
                        quantity_filled=int(filled_quantity),
                        avg_fill_price=simulated_fill_price,
                        broker_order_id=paper_order_id,
                        fill_time=datetime.now(timezone.utc),
                        strategy_id=approved.strategy_id,
                        product_type=approved.product_type,
                        expires_at=approved.expires_at.isoformat(),
                        stop_loss=approved.stop_loss,
                        take_profit=approved.take_profit,
                    )
            except Exception:
                logger.exception(
                    "execution_service.paper_order_kafka_error",
                    signal_id=approved.signal_id,
                    paper_order_id=paper_order_id,
                )

        # Update positions table so MISSquareOffManager can square off paper
        # positions at 15:05 IST and orphan_detector tracks them correctly.
        if (
            self._order_manager is not None
            and filled_quantity > 0
            and status != OrderStatus.REJECTED
        ):
            try:
                await self._order_manager.apply_fill_to_position(
                    symbol=approved.symbol,
                    side=side,
                    filled_quantity=filled_quantity,
                    avg_fill_price=simulated_fill_price,
                    last_price=simulated_fill_price,
                    order_id=paper_order_id,
                    signal_id=approved.signal_id,
                    risk_decision_id=approved.risk_decision_id,
                    market_str=approved.market,
                )
            except Exception:
                logger.exception(
                    "execution_service.paper_position_update_error",
                    signal_id=approved.signal_id,
                    paper_order_id=paper_order_id,
                )

            # Phase 2: attach exit policy immediately after entry fill so the
            # Trade Exit Engine can monitor stop_price / take_profit on this position.
            # Exits bypass the signal pipeline — stop_price is carried directly from
            # the risk-approved signal and is never counted against signals_today.
            try:
                await self._order_manager.attach_exit_policy(
                    symbol=approved.symbol,
                    stop_price=approved.stop_loss,
                    take_profit=approved.take_profit,
                    policy_id=f"POLICY-{approved.signal_id}",
                )
            except Exception:
                logger.exception(
                    "execution_service.paper_attach_exit_policy_error",
                    signal_id=approved.signal_id,
                    paper_order_id=paper_order_id,
                )

    # ── Execution kill switch ────────────────────────────────────────────────

    async def _durable_kill_switch_poll_loop(self) -> None:
        """Poll DynamoDB kill-switch state so Kafka/SNS fanout is not required."""
        logger.info("execution_service.durable_kill_switch_poll_loop.started")
        while self._running:
            try:
                await self._refresh_durable_kill_switch_state(
                    force=True,
                    cancel_open_orders=True,
                )
            except Exception:
                logger.exception("execution_service.durable_kill_switch_poll_error")
            await asyncio.sleep(max(0.1, self._ks_poll_interval))
        logger.info("execution_service.durable_kill_switch_poll_loop.stopped")

    async def _refresh_durable_kill_switch_state(
        self,
        *,
        force: bool = False,
        cancel_open_orders: bool = True,
    ) -> bool:
        """
        Read canonical DynamoDB kill-switch state and activate local halt.

        This path is activation-only. Clearing execution requires the explicit
        execution kill-switch clear path, so a transient DynamoDB read of an
        inactive row cannot accidentally resume trading after a local halt.
        """
        now = time.monotonic()
        if not force and (now - self._ks_checked_at) < self._ks_poll_interval:
            return self._kill_switch_active
        self._ks_checked_at = now

        if self._dynamo is None:
            return self._kill_switch_active
        table_name = getattr(
            getattr(self._settings, "aws", None),
            "dynamodb_table_risk_state",
            "",
        )
        if not table_name:
            return self._kill_switch_active

        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=table_name,
                Key=kill_switch_key(),
                ProjectionExpression="active, #status, reason, activated_by",
                ExpressionAttributeNames={"#status": "status"},
            )
            item = response.get("Item")
        except Exception:
            logger.exception(
                "execution_service.durable_kill_switch_read_failed retaining_active=%s",
                self._kill_switch_active,
            )
            return self._kill_switch_active

        active = bool(item and attr_bool(item, "active", False))
        if not active:
            return self._kill_switch_active

        reason = attr_string(item, "reason", "Durable kill switch active")
        activated_by = attr_string(item, "activated_by", "dynamodb")
        if self._kill_switch_active:
            self._kill_switch_reason = self._kill_switch_reason or reason
            return True

        if cancel_open_orders:
            await self._handle_kill_switch_activation(
                reason=reason,
                activated_by=f"dynamodb:{activated_by}",
            )
        else:
            self._kill_switch_active = True
            self._kill_switch_reason = reason
        return True

    # ── Phase 8: Placement-pause circuit (ADR-015 F4) ────────────────────────

    def _record_placement_failure(self, market: "Market") -> None:
        """
        Track consecutive ORDER_PLACE transient failures.

        NSE-only: Zerodha sessions can temporarily reject placements during
        token expiry or connectivity glitches. US (Alpaca) errors are tracked
        separately via AlpacaBroker's internal circuit.

        When ``_PLACEMENT_PAUSE_THRESHOLD_FAILURES`` consecutive failures occur,
        the consumer loop is gated for ``_PLACEMENT_PAUSE_BACKOFF_SECONDS`` to
        avoid flooding a degraded broker with queued orders.
        """
        if market != Market.NSE:
            return

        self._placement_consecutive_failures += 1
        if (
            self._placement_consecutive_failures >= _PLACEMENT_PAUSE_THRESHOLD_FAILURES
            and not self._placement_paused
        ):
            self._placement_paused = True
            self._placement_paused_until = time.monotonic() + _PLACEMENT_PAUSE_BACKOFF_SECONDS
            logger.critical(
                "execution_service.placement_pause_activated "
                "consecutive_failures=%d threshold=%d backoff=%.0fs "
                "— halting new signal intake for %.0f seconds",
                self._placement_consecutive_failures,
                _PLACEMENT_PAUSE_THRESHOLD_FAILURES,
                _PLACEMENT_PAUSE_BACKOFF_SECONDS,
                _PLACEMENT_PAUSE_BACKOFF_SECONDS,
            )

    async def _handle_position_drift_local_halt(
        self,
        symbol: str,
        broker_qty: float,
        dynamo_qty: float,
    ) -> None:
        """PositionMonitor callback: halt execution locally before fanout."""
        await self._handle_kill_switch_activation(
            reason=(
                "POSITION_DRIFT_DETECTED "
                f"symbol={symbol} broker_qty={broker_qty} dynamo_qty={dynamo_qty}"
            ),
            activated_by="position_monitor",
        )

    async def _handle_kill_switch_activation(
        self,
        *,
        reason: str,
        activated_by: str,
    ) -> None:
        """Halt new orders immediately and cancel non-protective open orders."""
        self._kill_switch_active = True
        self._kill_switch_reason = reason
        logger.critical(
            "execution_service.kill_switch_active",
            reason=reason,
            activated_by=activated_by,
        )
        await self._cancel_open_orders_for_kill_switch(reason=reason)

    async def _handle_kill_switch_clear(
        self,
        *,
        reason: str,
        activated_by: str,
    ) -> None:
        """Resume new orders only after an explicit KILL_SWITCH_CLEARED event."""
        self._kill_switch_active = False
        self._kill_switch_reason = ""
        logger.critical(
            "execution_service.kill_switch_cleared",
            reason=reason,
            activated_by=activated_by,
        )

    async def _cancel_open_orders_for_kill_switch(self, *, reason: str) -> None:
        if self._order_manager is None:
            return
        open_orders = await self._order_manager.get_open_orders()
        cancel_tasks = [
            self._cancel_one_open_order_for_kill_switch(order, reason=reason)
            for order in open_orders
        ]
        if cancel_tasks:
            await asyncio.gather(*cancel_tasks, return_exceptions=True)

    async def _cancel_one_open_order_for_kill_switch(
        self,
        order: StoredOrder,
        *,
        reason: str,
    ) -> None:
        if self._is_protective_order(order):
            logger.critical(
                "execution_service.kill_switch_preserved_protective_order",
                order_id=order.order_id,
                broker_order_id=order.broker_order_id,
                symbol=order.symbol,
                status=order.status.value,
                reason=reason,
            )
            return

        if order.status == OrderStatus.PENDING and not order.broker_order_id:
            await self._order_manager.update_order_status(
                order_id=order.order_id,
                new_status=OrderStatus.CANCELLED,
                filled_quantity=order.filled_quantity,
                average_price=order.avg_fill_price,
                broker_message=f"Cancelled locally by execution kill switch: {reason}",
            )
            return

        if order.status == OrderStatus.ACK_UNKNOWN and not order.broker_order_id:
            logger.critical(
                "execution_service.kill_switch_ack_unknown_unresolved",
                order_id=order.order_id,
                symbol=order.symbol,
                reason=reason,
            )
            return

        if not order.broker_order_id:
            return

        broker = self._get_broker(order.market)

        async def _cancel_once():
            return await broker.cancel_order(order.broker_order_id)

        try:
            if order.market == Market.NSE:
                await self._nse_rate_limiter.acquire(
                    Priority.CRITICAL,
                    EndpointClass.ORDER_CONTROL,
                )
            update = await self._retry_handler.execute_with_retry(
                func=_cancel_once,
                operation_name=f"kill_switch_cancel_{order.symbol}",
            )
            await self._order_manager.update_order_status(
                order_id=order.order_id,
                new_status=OrderStatus.CANCELLED,
                filled_quantity=order.filled_quantity,
                average_price=order.avg_fill_price,
                broker_message=update.broker_message or f"Cancelled by kill switch: {reason}",
            )
            logger.critical(
                "execution_service.kill_switch_order_cancelled",
                order_id=order.order_id,
                broker_order_id=order.broker_order_id,
                symbol=order.symbol,
            )
        except Exception:
            logger.exception(
                "execution_service.kill_switch_cancel_failed",
                order_id=order.order_id,
                broker_order_id=order.broker_order_id,
                symbol=order.symbol,
            )

    # ── Monitoring counters flush loop ────────────────────────────────────────

    async def _monitoring_flush_loop(self) -> None:
        """
        Background loop: serialize LiveCounters to JSON every 60s.

        Output path is controlled by QE_MONITORING_COUNTERS_PATH (default
        /tmp/qe_live_counters.json).  paper_trading_monitor.py reads this file
        via --counters when running alongside a live execution service.
        """
        import dataclasses as _dc
        import json as _json

        output_path = os.environ.get(
            "QE_MONITORING_COUNTERS_PATH", "/tmp/qe_live_counters.json"
        )
        flush_interval = float(os.environ.get("QE_MONITORING_FLUSH_INTERVAL", "60"))

        while self._running:
            try:
                payload = _dc.asdict(self._live_counters)
                # strategy_statuses is a list of dataclass instances — convert them too
                payload["strategy_statuses"] = [
                    _dc.asdict(s) for s in (self._live_counters.strategy_statuses or [])
                ]
                tmp_path = output_path + ".tmp"
                with open(tmp_path, "w") as fh:
                    _json.dump(payload, fh, indent=2, default=str)
                os.replace(tmp_path, output_path)
            except Exception:
                logger.warning("monitoring_flush.write_failed", path=output_path, exc_info=True)
            await asyncio.sleep(flush_interval)

    # ── Margin refresh loop ───────────────────────────────────────────────────

    async def _universe_snapshot_refresh_loop(self) -> None:
        """
        Background loop: rebuild the universe order validator once per calendar day (IST).

        The snapshot is built at service startup for today's date. After midnight IST the
        date changes and the snapshot becomes stale. In paper mode stale snapshots only
        produce warnings; in live mode they block all orders. This loop rebuilds at
        00:01 IST each day so the validator always references today's approved symbols.

        The loop also re-reads UNIVERSE_MODE in case it was updated via env/config reload
        (though a full restart is the recommended upgrade path for mode changes).
        """
        import zoneinfo  # noqa: PLC0415

        _IST = zoneinfo.ZoneInfo("Asia/Kolkata")
        _CHECK_INTERVAL = 60.0  # seconds between date checks (low cost, just a date compare)

        logger.info("execution_service.universe_refresh_loop.started")

        _last_rebuild_date: Optional[str] = None

        while self._running:
            await asyncio.sleep(_CHECK_INTERVAL)
            try:
                today_ist = datetime.now(_IST).strftime("%Y-%m-%d")
                if _last_rebuild_date == today_ist:
                    continue

                from shared.universe.modes import UniverseMode as _UniverseMode  # noqa: PLC0415
                from shared.universe.order_validator import (  # noqa: PLC0415
                    build_validator_for_today as _build_universe_validator,
                )
                _mode_str = self._universe_mode_str
                _mode = _UniverseMode.from_string(_mode_str)
                new_validator = _build_universe_validator(
                    mode=_mode,
                    fail_if_no_snapshot=_mode.is_live,
                    use_live_api=getattr(self, "_universe_use_live_api", False),
                )
                self._universe_validator = new_validator
                _snap = getattr(new_validator, "snapshot", None)
                _snap_size = getattr(_snap, "size", 0) if _snap is not None else 0
                _last_rebuild_date = today_ist
                logger.info(
                    "execution_service.universe_snapshot_refreshed date=%s mode=%s approved=%d",
                    today_ist, _mode_str, _snap_size,
                )
            except Exception as _exc:
                logger.error(
                    "execution_service.universe_snapshot_refresh_failed error=%s", _exc
                )

    async def _margin_refresh_loop(self) -> None:
        """
        Background loop: fetch live margin from brokers every 5 seconds and
        write a snapshot to DynamoDB ``risk-state`` table.

        This keeps the risk engine's MarginValidator supplied with fresh data
        without requiring broker API calls in the risk validation hot path.
        The risk engine reads DynamoDB (fast, in-VPC) instead.

        DynamoDB key schema:
            PK = "MARGIN#NSE"  or  "MARGIN#US"
            SK = "CURRENT"
            available_cash   : Number
            used_margin      : Number
            collateral_value : Number
            refreshed_at     : String (UTC ISO-8601)
            TTL              : Number (Unix epoch + 60s — auto-expire stale data)
        """
        _REFRESH_INTERVAL = 5.0  # seconds between broker margin polls
        _MARGIN_TTL_SECONDS = 60  # DynamoDB item TTL — auto-expire stale data

        from shared.aws.clients import get_dynamodb_client  # noqa: PLC0415

        dynamo = get_dynamodb_client()
        table = self._settings.aws.dynamodb_table_risk_state

        logger.info("execution_service.margin_refresh_loop.started")

        _is_paper = getattr(self._settings.risk, "profile", "paper") == "paper"

        while self._running:
            try:
                now_iso = datetime.now(timezone.utc).isoformat()
                ttl_epoch = int(time.time()) + _MARGIN_TTL_SECONDS

                if _is_paper:
                    # Paper mode: derive available_cash from the NAV snapshot so the
                    # risk engine's MarginValidator sees real paper capital, not 0.
                    try:
                        nav_resp = await asyncio.to_thread(
                            dynamo.get_item,
                            TableName=table,
                            Key={"PK": {"S": "NAV#CURRENT"}, "SK": {"S": "STATE"}},
                            ConsistentRead=False,
                        )
                        nav_item = nav_resp.get("Item", {})
                        paper_nav = float(nav_item.get("portfolio_value", {}).get("N", "5000000"))
                    except Exception:
                        paper_nav = 5_000_000.0
                    for market in ("NSE", "US"):
                        await asyncio.to_thread(
                            dynamo.put_item,
                            TableName=table,
                            Item={
                                "PK": {"S": f"MARGIN#{market}"},
                                "SK": {"S": "CURRENT"},
                                "available_cash":   {"N": str(paper_nav)},
                                "used_margin":      {"N": "0"},
                                "collateral_value": {"N": "0"},
                                "refreshed_at":     {"S": now_iso},
                                "TTL":              {"N": str(ttl_epoch)},
                            },
                        )
                else:
                    # ── NSE margin (Zerodha) ─────────────────────────────────
                    if self._zerodha is not None:
                        try:
                            nse_margin = await self._zerodha.get_margins()
                            await asyncio.to_thread(
                                dynamo.put_item,
                                TableName=table,
                                Item={
                                    "PK": {"S": "MARGIN#NSE"},
                                    "SK": {"S": "CURRENT"},
                                    "available_cash": {"N": str(nse_margin.get("available_cash", 0))},
                                    "used_margin": {"N": str(nse_margin.get("used_margin", 0))},
                                    "collateral_value": {"N": str(nse_margin.get("collateral", 0))},
                                    "refreshed_at": {"S": now_iso},
                                    "TTL": {"N": str(ttl_epoch)},
                                },
                            )
                        except Exception:
                            logger.warning(
                                "execution_service.margin_refresh.nse_failed",
                                exc_info=True,
                            )

                    # ── US margin (Alpaca) ───────────────────────────────────
                    if self._alpaca is not None:
                        try:
                            us_margin = await self._alpaca.get_margins()
                            await asyncio.to_thread(
                                dynamo.put_item,
                                TableName=table,
                                Item={
                                    "PK": {"S": "MARGIN#US"},
                                    "SK": {"S": "CURRENT"},
                                    "available_cash": {"N": str(us_margin.get("available_cash", 0))},
                                    "used_margin": {"N": str(us_margin.get("used_margin", 0))},
                                    "collateral_value": {"N": str(us_margin.get("collateral", 0))},
                                    "refreshed_at": {"S": now_iso},
                                    "TTL": {"N": str(ttl_epoch)},
                                },
                            )
                        except Exception:
                            logger.warning(
                                "execution_service.margin_refresh.us_failed",
                                exc_info=True,
                            )

            except Exception:
                logger.exception("execution_service.margin_refresh_loop.unexpected_error")

            await asyncio.sleep(_REFRESH_INTERVAL)

        logger.info("execution_service.margin_refresh_loop.stopped")


async def main() -> None:
    """Entry point for the Execution Engine Service.

    Settings are loaded from environment variables. KAFKA_BOOTSTRAP_SERVERS
    must be set so the engine can consume from signals.approved and publish
    to orders.events.
    """
    settings = get_settings()
    service = ExecutionService(settings=settings)
    await service.start()


if __name__ == "__main__":
    asyncio.run(main())
