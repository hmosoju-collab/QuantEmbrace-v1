"""
EnrichmentWatchdog — monitors ai_engine lag and manages topic fallback.

Phase 6 design (ADR-014 §5.7):
  The risk_engine normally consumes ``signals.enriched`` (v4.0, published by
  ai_engine).  If ai_engine is slow or down, enriched signals stop arriving and
  the risk_engine would stop processing signals.

  The watchdog prevents this by:
  1. Measuring Kafka consumer-group lag for ``aiengine-v1`` on ``signals.pending``.
  2. When lag exceeds LAG_THRESHOLD for WINDOW consecutive checks:
       → risk_engine switches to consuming ``signals.pending`` directly (fallback).
       → CloudWatch alarm fires: QuantEmbrace/RiskEngine/EnrichmentFallbackActive = 1.
  3. When lag returns to 0 and ai_engine resumes:
       → risk_engine switches back to ``signals.enriched`` after RECOVERY_WINDOW checks.

  Tunable via DynamoDB strategy-config:
    - LAG_THRESHOLD          (messages)   default: 10
    - WINDOW                 (checks)     default: 2
    - RECOVERY_WINDOW        (checks)     default: 5

  Manual override:
    Operator can force fallback via DynamoDB flag
    enrichment_required=False on PK=ENRICHMENT_CONFIG, SK=GLOBAL.

Topic switch:
  The switch is communicated to ``RiskEngineService`` via a shared
  ``EnrichmentState`` object.  The service reads ``state.use_enriched``
  before each poll cycle to decide which consumer to use.

Lag measurement:
  Uses boto3 MSK describe-consumer-groups if available.
  Falls back to no-op (no fallback triggered) when MSK API is unavailable.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from shared.kafka.config import get_kafka_auth_config
from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="risk_engine")

# Default tuning parameters
_DEFAULT_LAG_THRESHOLD:    int = 10   # messages
_DEFAULT_WINDOW:           int = 2    # consecutive breaching checks before fallback
_DEFAULT_RECOVERY_WINDOW:  int = 5    # consecutive clear checks before re-enable
_CHECK_INTERVAL_SECONDS:   float = 0.5  # watchdog poll interval


@dataclass
class EnrichmentState:
    """
    Shared mutable state between EnrichmentWatchdog and RiskEngineService.

    risk_engine reads ``use_enriched`` before each signal poll to decide
    which Kafka topic to consume from.
    """
    use_enriched: bool = True   # True → signals.enriched, False → signals.pending
    fallback_activated_at: Optional[datetime] = None
    fallback_count: int = 0
    consecutive_lag_breaches: int = field(default=0, repr=False)
    consecutive_clear_checks:  int = field(default=0, repr=False)


class EnrichmentWatchdog:
    """
    Background task that monitors ai_engine lag and manages topic switching.

    Args:
        state:          Shared EnrichmentState (read by RiskEngineService).
        kafka_bootstrap: MSK bootstrap servers for AdminClient lag queries.
        aws_region:     AWS region.
        dynamo_client:  boto3 DynamoDB client for config reads.
        config_table:   DynamoDB table with lag threshold config.
        metrics_client: boto3 CloudWatch client for alarm metrics.
    """

    def __init__(
        self,
        state:           EnrichmentState,
        kafka_bootstrap: str = "",
        aws_region:      str = "ap-south-1",
        dynamo_client:   Optional[Any] = None,
        config_table:    Optional[str] = None,
        metrics_client:  Optional[Any] = None,
    ) -> None:
        self._state            = state
        self._kafka_bootstrap  = kafka_bootstrap
        self._aws_region       = aws_region
        self._dynamo           = dynamo_client
        self._config_table     = config_table
        self._metrics          = metrics_client
        self._running:   bool  = False
        self._task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

        # Tunable parameters — read from DynamoDB on each cycle if available
        self._lag_threshold:   int = _DEFAULT_LAG_THRESHOLD
        self._window:          int = _DEFAULT_WINDOW
        self._recovery_window: int = _DEFAULT_RECOVERY_WINDOW

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the watchdog background task."""
        self._running = True
        self._task = asyncio.create_task(
            self._watch_loop(), name="enrichment_watchdog"
        )
        logger.info(
            "enrichment_watchdog.started",
            lag_threshold=self._lag_threshold,
            window=self._window,
        )

    async def stop(self) -> None:
        """Stop the watchdog."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(
            "enrichment_watchdog.stopped",
            fallback_count=self._state.fallback_count,
        )

    # ── Core watch loop ───────────────────────────────────────────────────────

    async def _watch_loop(self) -> None:
        """
        Check lag every 500ms.  Switch topics when thresholds are crossed.
        """
        while self._running:
            await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
            if not self._running:
                break
            try:
                await self._check_once()
            except Exception as exc:
                logger.warning("enrichment_watchdog.check_error", error=str(exc))

    async def _check_once(self) -> None:
        """
        One lag check cycle.  Updates state based on measured lag.
        """
        # Refresh tuning params from DynamoDB (non-fatal)
        await self._refresh_config()

        # Check for operator manual override
        if await self._is_manually_disabled():
            if self._state.use_enriched:
                logger.info("enrichment_watchdog.manual_override_fallback")
                self._activate_fallback("operator_override")
            return

        # Measure aiengine-v1 lag on signals.pending
        lag = await self._measure_lag()

        if lag is None:
            # Cannot measure — preserve current state
            return

        if self._state.use_enriched:
            # Currently using signals.enriched — check for fallback trigger
            if lag >= self._lag_threshold:
                self._state.consecutive_lag_breaches += 1
                self._state.consecutive_clear_checks = 0
                logger.debug(
                    "enrichment_watchdog.lag_breach",
                    lag=lag,
                    consecutive=self._state.consecutive_lag_breaches,
                    threshold=self._window,
                )
                if self._state.consecutive_lag_breaches >= self._window:
                    self._activate_fallback(f"lag={lag}>threshold={self._lag_threshold}")
            else:
                self._state.consecutive_lag_breaches = 0
        else:
            # Currently using signals.pending (fallback mode) — check for recovery
            if lag == 0:
                self._state.consecutive_clear_checks += 1
                logger.debug(
                    "enrichment_watchdog.lag_clear",
                    consecutive=self._state.consecutive_clear_checks,
                    threshold=self._recovery_window,
                )
                if self._state.consecutive_clear_checks >= self._recovery_window:
                    self._activate_recovery()
            else:
                self._state.consecutive_clear_checks = 0

    def _activate_fallback(self, reason: str) -> None:
        """Switch risk_engine to signals.pending (fallback mode)."""
        self._state.use_enriched            = False
        self._state.fallback_activated_at   = datetime.now(timezone.utc)
        self._state.fallback_count         += 1
        self._state.consecutive_lag_breaches = 0
        logger.warning(
            "enrichment_fallback.activated",
            reason=reason,
            fallback_count=self._state.fallback_count,
        )
        self._publish_fallback_metric(1)

    def _activate_recovery(self) -> None:
        """Switch risk_engine back to signals.enriched (normal mode)."""
        self._state.use_enriched           = True
        self._state.fallback_activated_at  = None
        self._state.consecutive_clear_checks = 0
        logger.info("enrichment_fallback.recovered")
        self._publish_fallback_metric(0)

    # ── Lag measurement ───────────────────────────────────────────────────────

    async def _measure_lag(self) -> Optional[int]:
        """
        Measure aiengine-v1 consumer group lag on signals.pending.

        Returns total lag (messages behind) across all partitions,
        or None if measurement fails.

        Uses boto3 MSK client's ``list_client_vpc_connections`` is not the
        right API.  The correct approach is to use confluent-kafka AdminClient
        to query consumer group offsets.  Implemented here with a simplified
        confluent-kafka AdminClient call wrapped in asyncio.to_thread.
        """
        if not self._kafka_bootstrap:
            return None

        try:
            return await asyncio.to_thread(self._measure_lag_sync)
        except Exception as exc:
            logger.debug("enrichment_watchdog.lag_measure_failed", error=str(exc))
            return None

    def _measure_lag_sync(self) -> Optional[int]:
        """
        Synchronous lag measurement via confluent-kafka AdminClient.

        Returns total lag across all partitions of signals.pending for
        the aiengine-v1 consumer group.
        """
        try:
            from confluent_kafka import Consumer
            from confluent_kafka.admin import AdminClient

            region = self._aws_region
            admin_conf = {
                "bootstrap.servers": self._kafka_bootstrap,
                **get_kafka_auth_config(region),
            }

            admin = AdminClient(admin_conf)

            # List consumer group details for aiengine-v1
            futures = admin.list_consumer_group_offsets(["aiengine-v1"])  # type: ignore[attr-defined]
            result = futures.get("aiengine-v1", None)
            if result is None:
                return 0

            result_value = result.result()

            # Get high-water marks for comparison
            total_lag = 0
            for tp, committed in result_value.items():
                # Query end offsets via a temporary consumer
                consumer = Consumer({**admin_conf, "group.id": "__lag_probe__"})
                try:
                    low, high = consumer.get_watermark_offsets(tp, timeout=2.0)
                    committed_offset = committed.offset if committed else low
                    lag = max(0, high - committed_offset)
                    total_lag += lag
                finally:
                    consumer.close()

            return total_lag

        except Exception:
            # confluent-kafka unavailable or query failed
            return None

    # ── DynamoDB config + override ────────────────────────────────────────────

    async def _refresh_config(self) -> None:
        """Read tuning parameters from DynamoDB (non-fatal)."""
        if not self._dynamo or not self._config_table:
            return
        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._config_table,
                Key={"PK": {"S": "ENRICHMENT_CONFIG"}, "SK": {"S": "GLOBAL"}},
            )
            item = response.get("Item", {})
            if item:
                lag_raw = item.get("lag_threshold", {}).get("N")
                win_raw = item.get("window",        {}).get("N")
                rec_raw = item.get("recovery_window", {}).get("N")
                if lag_raw: self._lag_threshold   = int(lag_raw)
                if win_raw: self._window          = int(win_raw)
                if rec_raw: self._recovery_window = int(rec_raw)
        except Exception:
            pass

    async def _is_manually_disabled(self) -> bool:
        """Check DynamoDB for operator manual override (enrichment_required=False)."""
        if not self._dynamo or not self._config_table:
            return False
        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._config_table,
                Key={"PK": {"S": "ENRICHMENT_CONFIG"}, "SK": {"S": "GLOBAL"}},
            )
            item = response.get("Item", {})
            required = item.get("enrichment_required", {}).get("BOOL", True)
            return not required
        except Exception:
            return False

    # ── CloudWatch metric ──────────────────────────────────────────────────────

    def _publish_fallback_metric(self, value: int) -> None:
        """Publish EnrichmentFallbackActive metric to CloudWatch."""
        if not self._metrics:
            return
        try:
            self._metrics.put_metric_data(
                Namespace="QuantEmbrace/RiskEngine",
                MetricData=[{
                    "MetricName": "EnrichmentFallbackActive",
                    "Value":      float(value),
                    "Unit":       "Count",
                    "Dimensions": [{"Name": "Service", "Value": "risk_engine"}],
                }],
            )
        except Exception:
            pass
