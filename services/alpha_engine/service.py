"""AlphaEngineService — shadow-mode forecast generation + ranking (ADR-031).

Run with ``python -m services.alpha_engine.service``.

ADVISORY ONLY. The service:
  * generates raw forecasts from AlphaModels (wired in P3),
  * costs them (net_edge_bps), ranks them cross-sectionally,
  * persists ALL forecasts to ``{prefix}-alpha-forecasts``,
  * publishes only publish-eligible opportunities to ``alpha.opportunities``.

It NEVER publishes to signals.* / orders.*, never places orders, never writes the
kill switch. When the kill switch is active it pauses all output (store writes and
publishing) until cleared. P2 wires the full runtime; with no models registered yet
the rank loop simply produces nothing and the service is healthy on its port.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
import signal as signal_module

from alpha_engine.cost.cost_model import CostModel
from alpha_engine.gates.kill_switch_gate import KillSwitchGate
from alpha_engine.labeling.eod_rollup import EodRollupTask
from alpha_engine.labeling.outcome_labeler import AlphaOutcomeLabeler, DynamoCandleReader
from alpha_engine.models.registry import AlphaModelRegistry
from alpha_engine.publishers.alpha_shadow_publisher import AlphaShadowPublisher
from alpha_engine.ranking.ranker import AlphaRanker
from alpha_engine.store._dynamo_json import ist_trade_date
from alpha_engine.store.forecast_store import ForecastStore
from alpha_engine.store.performance_store import PerformanceStore
from alpha_engine.store.registry_store import AlphaRegistryStore
from alpha_engine.universe.universe_resolver import UniverseResolver
from shared.aws.clients import get_dynamodb_resource
from shared.config.settings import get_settings
from shared.health.health_server import HealthServer
from shared.logging.logger import get_logger
from shared.models.alpha import AlphaForecast
from shared.utils.helpers import utc_now

logger = get_logger(__name__, service_name="alpha_engine")

_DEFAULT_PREFIX = "quantembrace-development"

# Model timeframe -> candle-cache interval string (matches data_ingestion).
_TIMEFRAME_TO_INTERVAL: dict[str, str] = {"1m": "minute", "5m": "5minute", "15m": "15minute"}
_CANDLE_POLL_SECONDS = 0.5
_IST = timezone(timedelta(hours=5, minutes=30))
_EOD_IST_MINUTE = 15 * 60 + 35  # 15:35 IST — after MIS square-off, market closed
_EOD_CHECK_SECONDS = 60.0


class AlphaEngineService:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._cfg = self._settings.alpha
        self._prefix = os.environ.get("DYNAMODB_TABLE_PREFIX", _DEFAULT_PREFIX)
        # model_version stamp: explicit override, else the build/boot UTC date so
        # every forecast is versioned (ADR-031 #2).
        self._model_version = self._cfg.model_version or utc_now().strftime("%Y-%m-%d")

        self._running = False
        self._cost = CostModel()
        self._ranker: AlphaRanker | None = None
        self._gate: KillSwitchGate | None = None
        self._publisher: AlphaShadowPublisher | None = None
        self._forecast_store: ForecastStore | None = None
        self._registry: AlphaRegistryStore | None = None
        self._health: HealthServer | None = None
        self._tasks: list[asyncio.Task] = []

        # Models + candle consumer (P3). Forecasts buffer between the candle loop
        # (producer) and the rank cycle (consumer).
        self._models: list = []
        self._candle_consumer = None
        self._universe = UniverseResolver()
        self._pending: list[AlphaForecast] = []

        # Labeling + rollups (P4).
        self._performance_store: PerformanceStore | None = None
        self._labeler: AlphaOutcomeLabeler | None = None
        self._eod: EodRollupTask | None = None
        self._eod_last_run_date: str | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        health_port = int(
            os.environ.get("QE_HEALTH_CHECK_PORT", os.environ.get("HEALTH_CHECK_PORT", "8087"))
        )
        self._health = HealthServer(port=health_port, service_name="alpha_engine")
        self._health.set_ready(False)
        await self._health.start()

        if not self._cfg.enabled:
            logger.warning("alpha_engine.disabled ALPHA_ENABLED=false — idle (healthy)")
            self._running = True
            self._health.set_ready(True)
            return

        kafka_bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "")
        if not kafka_bootstrap:
            raise RuntimeError("KAFKA_BOOTSTRAP_SERVERS is not set. Kafka is mandatory.")
        region = self._settings.aws.region

        resource = get_dynamodb_resource()
        self._gate = KillSwitchGate(table=resource.Table(f"{self._prefix}-risk-state"))
        self._forecast_store = ForecastStore(table=resource.Table(f"{self._prefix}-alpha-forecasts"))
        self._registry = AlphaRegistryStore(table=resource.Table(f"{self._prefix}-alpha-registry"))

        self._publisher = AlphaShadowPublisher(kafka_bootstrap, aws_region=region)
        await self._publisher.start()

        self._ranker = AlphaRanker(
            top_n=self._cfg.top_n,
            min_net_edge_bps=self._cfg.min_net_edge_bps,
            forecast_ttl_seconds=self._cfg.forecast_ttl_seconds,
        )

        # Build the shadow models (mirrors of the live strategies) + candle feed.
        builder = AlphaModelRegistry(
            self._registry,
            symbols=list(self._settings.strategy.watchlist_nse),
            horizons_minutes=list(self._cfg.horizons_minutes),
            universe_resolver=self._universe.resolve,
        )
        self._models = builder.build_models(list(self._cfg.models), self._model_version)
        for model in self._models:
            await model.initialize()
        self._candle_consumer = self._build_candle_consumer(resource)

        # Labeling + EOD rollups (P4).
        self._performance_store = PerformanceStore(
            table=resource.Table(f"{self._prefix}-alpha-performance")
        )
        candle_reader = DynamoCandleReader(table=resource.Table(f"{self._prefix}-candle-cache"))
        self._labeler = AlphaOutcomeLabeler(self._forecast_store, candle_reader, market="NSE")
        self._eod = EodRollupTask(self._forecast_store, self._performance_store, market="NSE")

        self._health.add_check("publisher_ready", lambda: self._publisher is not None)
        self._health.add_check("stores_ready", lambda: self._forecast_store is not None)
        self._health.add_check("models_loaded", lambda: len(self._models) > 0)

        self._running = True
        self._tasks.append(asyncio.create_task(self._candle_loop(), name="alpha_candle_loop"))
        self._tasks.append(
            asyncio.create_task(self._rank_cycle_loop(), name="alpha_rank_cycle")
        )
        self._tasks.append(asyncio.create_task(self._labeler_loop(), name="alpha_labeler"))
        self._tasks.append(asyncio.create_task(self._eod_loop(), name="alpha_eod_rollup"))
        self._health.set_ready(True)
        logger.info(
            "alpha_engine.started model_version=%s publish_enabled=%s min_net_edge_bps=%.1f",
            self._model_version, self._cfg.publish_enabled, self._cfg.min_net_edge_bps,
        )

    async def run(self) -> None:
        while self._running:
            await asyncio.sleep(0.5)

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._tasks.clear()
        if self._publisher is not None:
            await self._publisher.stop()
        if self._health is not None:
            self._health.set_ready(False)
            await self._health.stop()
        logger.info("alpha_engine.stopped")

    # ── Rank cycle ────────────────────────────────────────────────────────────

    async def _rank_cycle_loop(self) -> None:
        while self._running:
            try:
                await self._run_one_cycle(utc_now())
            except Exception:
                logger.exception("alpha_engine.rank_cycle_error")
            await asyncio.sleep(self._cfg.rank_interval_seconds)

    async def _run_one_cycle(self, cycle_ts: datetime) -> None:
        """Cost -> rank -> persist (all) -> publish (eligible). Paused on kill switch."""
        forecasts = await self._collect_forecasts()
        if not forecasts:
            return
        assert self._ranker is not None and self._gate is not None

        costed = [self._cost.apply(f) for f in forecasts]
        result = self._ranker.rank(costed, cycle_ts)

        if await self._gate.is_active():
            logger.warning(
                "alpha_engine.cycle_paused_kill_switch cycle=%s n=%d (no store/publish)",
                result.cycle_id, len(result.opportunities),
            )
            return

        # Persist every forecast (published or suppressed) for research.
        assert self._forecast_store is not None
        for opp in result.opportunities:
            self._forecast_store.put_opportunity(opp)

        # Publish only the publish-eligible top-N.
        if self._cfg.publish_enabled and self._publisher is not None:
            for opp in result.published_opportunities:
                await self._publisher.publish_opportunity(opp)

        logger.info(
            "alpha_engine.cycle cycle=%s forecasts=%d published=%d expired=%d",
            result.cycle_id, len(result.opportunities),
            len(result.published_opportunities), result.expired_count,
        )

    async def _collect_forecasts(self) -> list[AlphaForecast]:
        """Drain forecasts buffered by the candle loop since the last cycle."""
        pending, self._pending = self._pending, []
        return pending

    # ── Candle loop (producer of raw forecasts) ───────────────────────────────

    def _build_candle_consumer(self, resource):
        # Cross-boundary import (sanctioned): reuse the strategy_engine candle
        # consumer read-only against the shared candle-cache. Promotion to
        # services/shared/ is [PLANNED] at cutover.
        from strategy_engine.consumers.dynamo_candle_consumer import DynamoCandleConsumer

        return DynamoCandleConsumer(resource.Table(f"{self._prefix}-candle-cache"))

    async def _candle_loop(self) -> None:
        while self._running:
            try:
                await self._poll_candles_once()
            except Exception:
                logger.exception("alpha_engine.candle_loop_error")
            await asyncio.sleep(_CANDLE_POLL_SECONDS)

    async def _poll_candles_once(self) -> None:
        if self._candle_consumer is None:
            return
        candles = await asyncio.to_thread(self._candle_consumer.poll_new_candles)
        for candle in candles:
            if candle.data_quality != "NORMAL":
                continue
            bar = candle.to_bar()
            for model in self._models:
                if _TIMEFRAME_TO_INTERVAL.get(model.timeframe) != candle.interval:
                    continue
                forecasts = await model.on_bar(bar)
                # Stamp the deterministic candle trace_id onto each forecast
                # (forecast_id is derived from identity fields, so it is unchanged).
                for forecast in forecasts:
                    self._pending.append(replace(forecast, trace_id=candle.trace_id))

    # ── Labeling + EOD rollup (P4) ────────────────────────────────────────────

    async def _labeler_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._cfg.labeler_interval_seconds)
            if not self._running or self._labeler is None:
                continue
            try:
                await asyncio.to_thread(self._labeler.label_matured, utc_now())
            except Exception:
                logger.exception("alpha_engine.labeler_error")

    async def _eod_loop(self) -> None:
        """Fire the EOD rollup once per IST trading day, at/after 15:35 IST."""
        while self._running:
            await asyncio.sleep(_EOD_CHECK_SECONDS)
            if not self._running or self._eod is None:
                continue
            now_ist = utc_now().astimezone(_IST)
            today = now_ist.date().isoformat()
            ist_minute = now_ist.hour * 60 + now_ist.minute
            if ist_minute < _EOD_IST_MINUTE or self._eod_last_run_date == today:
                continue
            try:
                # Final labeling pass, then roll up the day.
                if self._labeler is not None:
                    await asyncio.to_thread(self._labeler.label_matured, utc_now())
                await asyncio.to_thread(self._eod.run, ist_trade_date(utc_now()))
                self._eod_last_run_date = today
            except Exception:
                logger.exception("alpha_engine.eod_rollup_error")


def main() -> None:
    """Run the alpha_engine service (``python -m services.alpha_engine.service``)."""
    service = AlphaEngineService()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run() -> None:
        await service.start()
        try:
            await service.run()
        finally:
            await service.stop()

    def _shutdown(sig: int, frame: object) -> None:
        logger.info("alpha_engine.signal_received sig=%s", sig)
        service._running = False

    signal_module.signal(signal_module.SIGTERM, _shutdown)
    signal_module.signal(signal_module.SIGINT, _shutdown)

    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
