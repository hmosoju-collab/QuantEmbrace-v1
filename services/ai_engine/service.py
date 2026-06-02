"""
AI Engine Service — Phase 6 Kafka-only implementation.

Replaces the FastAPI HTTP skeleton with a Kafka processing loop that:
  1. Consumes signals from ``signals.pending`` (consumer group: aiengine-v1)
  2. Enriches each signal with regime classification + quality scoring
  3. Publishes enriched signals to ``signals.enriched``

A lightweight asyncio HTTP server serves the ``/health`` endpoint on
``HEALTH_CHECK_PORT`` (default 8080).  No FastAPI dependency required.

Architecture (Phase 6, ADR-014):
  The ai_engine sits between strategy_engine and risk_engine:

    strategy_engine → signals.pending → [ai_engine] → signals.enriched → risk_engine

  ai_engine failure: risk_engine's EnrichmentWatchdog detects lag and
  falls back to signals.pending automatically within 60s.  Trading continues.

Service lifecycle:
    start()  — load models, start Kafka consumer/producer, start health server
    run()    — asyncio.gather([processing_loop, health_server, selector_scheduler])
    stop()   — flush producer, close consumer, shut down health server

Startup fails fast if:
    - KAFKA_BOOTSTRAP_SERVERS is not set (Kafka is mandatory)
    All other failures (model load, DynamoDB) degrade gracefully.

Environment variables:
    KAFKA_BOOTSTRAP_SERVERS  (required)
    AWS_REGION               (required)
    AWS_S3_MODEL_BUCKET      or AWS_S3_BUCKET (S3 bucket for model artifacts)
    AWS_DYNAMODB_TABLE_FEATURES  (Phase 5 feature store)
    AWS_DYNAMODB_TABLE_STRATEGY_CONFIG
    AWS_DYNAMODB_TABLE_REGIME_LOG
    HEALTH_CHECK_PORT        (default 8080)
    ANTHROPIC_API_KEY        (optional — for strategy selector)
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timezone
from typing import Optional

from shared.aws.clients import get_dynamodb_client
from shared.config.settings import get_settings
from shared.features.feature_reader import FeatureReader
from shared.logging.logger import get_logger

from ai_engine.agents.strategy_selector import StrategySelector
from ai_engine.classifiers.regime_classifier import RegimeClassifier
from ai_engine.consumers.kafka_signal_consumer import KafkaSignalConsumer
from ai_engine.enrichment.signal_enricher import SignalEnricher
from ai_engine.features.feature_pipeline import FeaturePipeline
from ai_engine.models.model_registry import ModelRegistry
from ai_engine.publishers.kafka_enriched_publisher import KafkaEnrichedPublisher
from ai_engine.scorers.signal_quality_scorer import SignalQualityScorer

logger = get_logger(__name__, service_name="ai_engine")

# Threshold cache refresh interval (clear per-strategy threshold cache every N signals)
_THRESHOLD_CACHE_REFRESH_SIGNALS: int = 100


class AIEngineService:
    """
    Main AI engine service — Kafka-only enrichment pipeline.

    Manages model loading, feature pipeline, Kafka consumer/producer,
    and the post-market strategy selector agent.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._running:  bool = False
        self._ready:    bool = False
        self._start_time: Optional[datetime] = None

        # Components — initialised in start()
        self._model_registry:  Optional[ModelRegistry]          = None
        self._feature_pipeline: Optional[FeaturePipeline]       = None
        self._regime_classifier: Optional[RegimeClassifier]     = None
        self._quality_scorer:  Optional[SignalQualityScorer]    = None
        self._signal_enricher: Optional[SignalEnricher]         = None
        self._consumer:        Optional[KafkaSignalConsumer]     = None
        self._publisher:       Optional[KafkaEnrichedPublisher]  = None
        self._strategy_selector: Optional[StrategySelector]     = None

        # Counters for health endpoint
        self._processed:  int = 0
        self._errors:     int = 0
        self._signal_count_since_cache_refresh: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialise all components. Raises RuntimeError if Kafka is not configured."""
        logger.info("ai_engine.starting")
        self._start_time = datetime.now(timezone.utc)

        kafka_bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "")
        if not kafka_bootstrap:
            raise RuntimeError(
                "KAFKA_BOOTSTRAP_SERVERS is not set. "
                "ai_engine cannot start without Kafka connectivity."
            )

        aws_region  = self._settings.aws.region
        dynamo      = get_dynamodb_client()

        # ── Feature pipeline (Phase 5 FeatureReader) ──────────────────────────
        feature_reader     = FeatureReader(
            dynamo_client  = dynamo,
            features_table = self._settings.aws.dynamodb_table_features,
        )
        self._feature_pipeline = FeaturePipeline(feature_reader=feature_reader)

        # ── Model registry (S3 + joblib + hot-reload) ─────────────────────────
        strategy_config_table = getattr(
            self._settings.aws, "dynamodb_table_strategy_config", None
        )
        self._model_registry = ModelRegistry(
            s3_bucket     = self._settings.aws.s3_model_bucket,
            region        = aws_region,
            dynamo_client = dynamo,
            version_table = strategy_config_table,
        )
        await self._model_registry.start()

        # Pre-load both models (fails gracefully to stub models)
        await asyncio.gather(
            self._model_registry.load_model("regime_classifier"),
            self._model_registry.load_model("signal_quality_scorer"),
        )

        # ── ML components ─────────────────────────────────────────────────────
        self._regime_classifier = RegimeClassifier(
            model_registry   = self._model_registry,
            feature_pipeline = self._feature_pipeline,
        )
        self._quality_scorer = SignalQualityScorer(
            model_registry   = self._model_registry,
            feature_pipeline = self._feature_pipeline,
        )

        # ── Regime log + enricher ──────────────────────────────────────────────
        regime_log_table = getattr(
            self._settings.aws, "dynamodb_table_regime_log", None
        )
        self._signal_enricher = SignalEnricher(
            regime_classifier     = self._regime_classifier,
            quality_scorer        = self._quality_scorer,
            dynamo_client         = dynamo,
            strategy_config_table = strategy_config_table,
            regime_log_table      = regime_log_table,
        )

        # ── Kafka consumer (aiengine-v1) ───────────────────────────────────────
        self._consumer = KafkaSignalConsumer(
            bootstrap_servers = kafka_bootstrap,
            aws_region        = aws_region,
        )
        await self._consumer.start()

        # ── Kafka producer (signals.enriched) ─────────────────────────────────
        self._publisher = KafkaEnrichedPublisher(
            bootstrap_servers = kafka_bootstrap,
            aws_region        = aws_region,
        )
        self._publisher.start()

        # ── Strategy selector agent (optional) ────────────────────────────────
        recommendations_table = getattr(
            self._settings.aws, "dynamodb_table_strategy_recommendations", None
        )
        fills_table = getattr(self._settings.aws, "dynamodb_table_fills", None)
        self._strategy_selector = StrategySelector(
            dynamo_client         = dynamo,
            strategy_config_table = strategy_config_table,
            fills_table           = fills_table,
            risk_state_table      = self._settings.aws.dynamodb_table_risk_state,
            regime_log_table      = regime_log_table,
            recommendations_table = recommendations_table,
        )

        self._ready = True
        logger.info("ai_engine.started", models=self._model_registry.loaded_versions())

    async def stop(self) -> None:
        """Gracefully shut down all components."""
        logger.info("ai_engine.stopping")
        self._running = False
        self._ready   = False

        if self._consumer:
            await self._consumer.stop()
        if self._publisher:
            self._publisher.stop()
        if self._model_registry:
            await self._model_registry.stop()

        logger.info(
            "ai_engine.stopped",
            processed=self._processed,
            errors=self._errors,
        )

    # ── Main loops ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Run the enrichment pipeline and auxiliary tasks concurrently.

        All tasks run until self._running is False (set by stop() or signal handler).
        """
        self._running = True

        health_port = int(os.environ.get("QE_HEALTH_CHECK_PORT", os.environ.get("HEALTH_CHECK_PORT", "8080")))

        await asyncio.gather(
            self._processing_loop(),
            self._health_server(health_port),
            self._strategy_selector_scheduler(),
            return_exceptions=True,
        )

    async def _processing_loop(self) -> None:
        """
        Core enrichment loop.

        Polls signals.pending, enriches each signal, publishes to signals.enriched.
        """
        logger.info("ai_engine.processing_loop.started")

        while self._running:
            try:
                event = await asyncio.to_thread(self._consumer.poll_signal)
                if event is None:
                    continue

                enriched = await self._signal_enricher.enrich(event)
                published = await asyncio.to_thread(self._publisher.publish, enriched)

                if published:
                    await asyncio.to_thread(self._consumer.commit, event)
                    self._processed += 1
                else:
                    logger.warning(
                        "ai_engine.publish_failed",
                        signal_id=enriched.signal_id,
                    )
                    self._errors += 1

                # Periodically clear threshold cache to pick up config changes
                self._signal_count_since_cache_refresh += 1
                if self._signal_count_since_cache_refresh >= _THRESHOLD_CACHE_REFRESH_SIGNALS:
                    self._signal_enricher.clear_threshold_cache()
                    self._signal_count_since_cache_refresh = 0

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._errors += 1
                logger.error("ai_engine.processing_loop.error", error=str(exc))
                await asyncio.sleep(0.1)  # brief back-off on unexpected error

        logger.info("ai_engine.processing_loop.stopped")

    async def _strategy_selector_scheduler(self) -> None:
        """
        Scheduler for the post-market strategy selector agent.

        Runs once per NSE trading session at ~15:45 IST (10:15 UTC).
        Implemented as a simple sleep loop checking wall-clock time.
        """
        _RUN_HOUR_UTC   = 10   # 15:45 IST = 10:15 UTC
        _RUN_MINUTE_UTC = 15
        _last_run_date: Optional[str] = None

        logger.info("ai_engine.strategy_selector_scheduler.started")

        while self._running:
            await asyncio.sleep(60)  # check every minute
            now = datetime.now(timezone.utc)

            if (
                now.hour == _RUN_HOUR_UTC
                and now.minute == _RUN_MINUTE_UTC
                and now.strftime("%Y-%m-%d") != _last_run_date
            ):
                _last_run_date = now.strftime("%Y-%m-%d")
                logger.info("ai_engine.strategy_selector.triggering", date=_last_run_date)
                try:
                    if self._strategy_selector:
                        await self._strategy_selector.run_post_market(_last_run_date)
                except Exception as exc:
                    logger.error("ai_engine.strategy_selector.error", error=str(exc))

        logger.info("ai_engine.strategy_selector_scheduler.stopped")

    # ── Health server ─────────────────────────────────────────────────────────

    async def _health_server(self, port: int) -> None:
        """
        Lightweight asyncio HTTP server for the /health endpoint.

        Avoids FastAPI dependency in the Kafka-only service.
        Returns 200 OK when service is ready, 503 when not.
        """
        async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                _request_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                # Read and discard headers
                while True:
                    line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                    if line in (b"\r\n", b"\n", b""):
                        break

                status = 200 if self._ready else 503
                body = json.dumps({
                    "status":    "healthy" if self._ready else "starting",
                    "service":   "ai_engine",
                    "processed": self._processed,
                    "errors":    self._errors,
                    "models":    self._model_registry.loaded_versions() if self._model_registry else {},
                    "uptime_s":  (
                        (datetime.now(timezone.utc) - self._start_time).total_seconds()
                        if self._start_time else 0
                    ),
                }).encode()

                response = (
                    f"HTTP/1.1 {status} {'OK' if status == 200 else 'Service Unavailable'}\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"Connection: close\r\n"
                    f"\r\n"
                ).encode() + body

                writer.write(response)
                await writer.drain()
            except Exception:
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(_handle, "0.0.0.0", port)
        logger.info("ai_engine.health_server.started", port=port)

        async with server:
            while self._running:
                await asyncio.sleep(1.0)

        logger.info("ai_engine.health_server.stopped")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """Run the ai_engine service (``python -m services.ai_engine.service``)."""
    service = AIEngineService()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run() -> None:
        await service.start()
        try:
            await service.run()
        finally:
            await service.stop()

    def _shutdown(sig: int, frame: object) -> None:
        logger.info("ai_engine.signal_received", sig=sig)
        service._running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
