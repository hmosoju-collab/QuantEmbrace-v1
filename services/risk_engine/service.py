"""
Risk Engine Service — the mandatory gatekeeper between Strategy and Execution.

Every trading signal MUST pass through this service before reaching the
Execution Engine. There is no bypass path. If the Risk Engine is down,
trading halts — this is by design.

Signal flow (Phase 6, normal mode):
    signals.enriched (Kafka) → validate → signals.approved (Kafka)

Signal flow (Phase 6, fallback mode — ai_engine lagging):
    signals.pending  (Kafka) → validate → signals.approved (Kafka)

The EnrichmentWatchdog monitors ai_engine's Kafka consumer group lag. When
aiengine-v1 lag exceeds the threshold, it activates fallback mode and
switches risk_engine to consume signals.pending directly. Recovery is
automatic when ai_engine catches up.

Consumes (Phase 6):
    signals.enriched (consumer group risk-v1) — primary: SIGNAL_ENRICHED (v4.0)
    signals.pending  (consumer group risk-v1) — fallback: SIGNAL_PENDING  (v3.0)
    orders.events    (consumer group risk-v1) — ORDER_FILLED/REJECTED for P&L
    risk.kill-switch (consumer group risk-v1) — immediate kill-switch propagation

Produces:
    signals.approved — SIGNAL_APPROVED events for the execution engine.

Required environment variables:
    KAFKA_BOOTSTRAP_SERVERS — MSK Serverless bootstrap endpoint (mandatory).
                              Service will refuse to start if not set.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from botocore.exceptions import ClientError

from risk_engine.analytics.risk_analytics_engine import RiskAnalyticsEngine
from risk_engine.cache.kill_switch_cache import KillSwitchCache
from risk_engine.consumers.enrichment_watchdog import EnrichmentState, EnrichmentWatchdog
from risk_engine.watchdogs.kafka_lag_watchdog import KafkaLagWatchdog
from risk_engine.consumers.kafka_enriched_consumer import (
    EnrichedSignalEvent,
    KafkaEnrichedConsumer,
)
from risk_engine.consumers.kafka_order_events_consumer import (
    KafkaOrderEventsConsumer,
    OrderFillEvent,
)
from risk_engine.consumers.kafka_signal_consumer import KafkaSignalConsumer
from risk_engine.context.risk_context_builder import RiskContextBuilder
from risk_engine.killswitch.auto_triggers import KillSwitchMonitor
from risk_engine.killswitch.kafka_kill_switch_listener import KafkaKillSwitchListener
from risk_engine.killswitch.killswitch import KillSwitch
from risk_engine.limits.risk_limits import RiskLimits, RiskValidationResult
from risk_engine.publishers.kafka_approved_publisher import KafkaApprovedPublisher
from risk_engine.registry.registry import InstrumentRegistry
from risk_engine.validators.exposure_validator import ExposureValidator
from risk_engine.validators.entry_block_validator import EntryBlockValidator
from risk_engine.validators.liquidity_validator import LiquidityValidator
from risk_engine.validators.loss_validator import DailyLossValidator
from risk_engine.validators.margin_validator import MarginValidator
from risk_engine.validators.position_validator import PositionValidator
from risk_engine.validators.sector_validator import SectorConcentrationValidator
from risk_engine.validators.signal_age_validator import SignalAgeValidator
from risk_engine.validators.slippage_validator import SlippageValidator
from risk_engine.validators.reconciliation_validator import ReconciliationValidator
from risk_engine.validators.spread_gate_validator import SpreadGateValidator
from shared.config.settings import AppSettings, get_settings
from shared.health.health_server import HealthServer
from shared.kafka.retry_replayer import KafkaRetryReplayer
from shared.logging.logger import get_logger, set_correlation_id
from shared.models.signal import Signal, SignalStatus
from shared.risk_state import attr_string, nav_key, risk_decision_key
from shared.utils.helpers import utc_now

logger = get_logger(__name__, service_name="risk_engine")

_RISK_DECISION_PUBLISH_PENDING = "PENDING"
_RISK_DECISION_PUBLISH_PUBLISHED = "PUBLISHED"


class RiskDecisionStatus(str, Enum):
    """Outcome of a risk validation pass."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass
class RiskDecision:
    """
    Immutable record of a risk validation decision.

    Every decision is persisted to S3 for audit. The ``risk_decision_id``
    is attached to the SIGNAL_APPROVED event so that any trade can be traced
    back to the risk check that approved it.
    """

    risk_decision_id: str
    signal_id: str
    status: RiskDecisionStatus
    reason: str
    validator_results: list[RiskValidationResult] = field(default_factory=list)
    timestamp: datetime = field(default_factory=utc_now)
    enriched: bool = False  # True when signal came via signals.enriched (ai_engine annotated)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the decision for JSON/S3 storage."""
        return {
            "risk_decision_id": self.risk_decision_id,
            "signal_id": self.signal_id,
            "status": self.status.value,
            "reason": self.reason,
            "enriched": self.enriched,
            "validator_results": [
                {
                    "approved": vr.approved,
                    "validator_name": vr.validator_name,
                    "reason": vr.reason,
                    "details": vr.details,
                }
                for vr in self.validator_results
            ],
            "timestamp": self.timestamp.isoformat(),
        }


class RiskEngineService:
    """
    Main risk validation service.

    Lifecycle:
        1. ``start()`` — validates config, loads kill switch state, initializes
           validators, starts Kafka consumer and approved publisher, begins loop.
        2. ``_kafka_processing_loop()`` — polls signals.pending (risk-v1),
           validates each signal, publishes approved signals to signals.approved.
        3. ``stop()`` — drains in-flight validations, stops Kafka consumer and
           publisher, persists state.

    Restart-safety: Kill switch state is in DynamoDB. In-flight signals that
    were not yet validated will be re-delivered by Kafka at-least-once semantics
    (resume from committed offset). Deterministic signal_id deduplication in
    DynamoDB prevents double-approval of replayed signals.
    """

    def __init__(
        self,
        settings: Optional[AppSettings] = None,
        dynamo_client: Any = None,
        s3_client: Any = None,
        sns_client: Any = None,
        sns_topic_arn: Optional[str] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._dynamo = dynamo_client
        self._s3 = s3_client
        self._sns = sns_client

        # Risk limits (loaded from settings)
        self._limits = RiskLimits.for_profile(
            getattr(self._settings.risk, "profile", "tiny-live"),
            portfolio_value=self._settings.portfolio_value,
        )
        explicit_risk_fields = getattr(self._settings.risk, "model_fields_set", set())
        for field_name in (
            "max_position_size_pct",
            "max_total_exposure_pct",
            "max_daily_loss_pct",
            "max_single_order_value",
            "max_open_orders",
            "max_position_per_symbol",
            "max_concurrent_positions",
            "max_sector_exposure_pct",
            "allow_leverage",
        ):
            if field_name in explicit_risk_fields:
                setattr(self._limits, field_name, getattr(self._settings.risk, field_name))

        # Kill switch — with SNS client for sub-5s propagation
        _topic = sns_topic_arn or getattr(self._settings.aws, "sns_kill_switch_topic_arn", "")
        self._kill_switch = KillSwitch(
            dynamo_client=self._dynamo,
            sns_client=self._sns,
            sns_topic_arn=_topic,
            settings=self._settings,
        )

        # Auto-trigger monitor — 4 background health checks.
        # In paper mode, broker pings only arrive on fills (no live broker).
        # Set broker_timeout_secs=inf so the connectivity monitor never fires
        # between sparse paper fills.
        _is_paper = getattr(self._settings.risk, "profile", "paper") == "paper"
        # ADR-021 Phase 1: split consumer lag from WebSocket producer staleness.
        # consumer_lag_stale_secs: how long without a signal from Kafka (default 300s).
        #   Tolerates consumer rebalancing. Replaces the 3600s workaround in .env.
        # producer_heartbeat_stale_secs: how stale the data_ingestion DynamoDB heartbeat
        #   can be (default 60s). Tight: a dead WebSocket means no prices at all.
        _consumer_lag_secs = getattr(
            self._settings.risk, "data_feed_stale_seconds", 300.0
        )
        self._kill_switch_monitor = KillSwitchMonitor(
            kill_switch=self._kill_switch,
            settings=self._settings,
            broker_timeout_secs=float("inf") if _is_paper else 30.0,
            consumer_lag_stale_secs=_consumer_lag_secs,
            producer_heartbeat_stale_secs=60.0,
            dynamo_client=self._dynamo,
            prices_table=getattr(self._settings.aws, "dynamodb_table_prices", None),
        )

        # Validators — executed in this exact sequence.
        # SignalAgeValidator MUST be first: O(1), rejects stale signals
        # before any DynamoDB reads by downstream validators.
        self._signal_age_validator = SignalAgeValidator(settings=self._settings)
        self._position_validator = PositionValidator(
            limits=self._limits,
            dynamo_client=self._dynamo,
            orders_table=self._settings.aws.dynamodb_table_orders,
            settings=self._settings,
        )
        self._exposure_validator = ExposureValidator(
            limits=self._limits,
            dynamo_client=self._dynamo,
            settings=self._settings,
        )
        self._loss_validator = DailyLossValidator(
            limits=self._limits,
            dynamo_client=self._dynamo,
            risk_state_table=self._settings.aws.dynamodb_table_risk_state,
            settings=self._settings,
        )
        self._margin_validator = MarginValidator(
            limits=self._limits,
            dynamo_client=self._dynamo,
            risk_state_table=self._settings.aws.dynamodb_table_risk_state,
            settings=self._settings,
        )
        self._slippage_validator = SlippageValidator(
            limits=self._limits,
            dynamo_client=self._dynamo,
            settings=self._settings,
        )

        # ── Phase 4: InstrumentRegistry + new validators ──────────────────────
        # Registry is optional — if instruments.yaml is missing, validators degrade
        # gracefully (UNKNOWN sector, spread/liquidity checks skipped with warnings).
        self._instrument_registry: Optional[InstrumentRegistry] = None
        try:
            self._instrument_registry = InstrumentRegistry.load()
        except (FileNotFoundError, ValueError) as exc:
            logger.warning(
                "InstrumentRegistry load failed — sector/spread/liquidity validators "
                "will use graceful-degradation mode: %s",
                exc,
            )

        self._spread_gate_validator = SpreadGateValidator(
            max_spread_bps=getattr(self._settings.risk, "max_spread_bps", 50.0),
        )
        self._sector_validator = SectorConcentrationValidator(
            limits=self._limits,
            instrument_registry=self._instrument_registry,
        )
        self._liquidity_validator = LiquidityValidator(
            max_order_adv_pct=getattr(self._settings.risk, "max_order_adv_pct", 1.0),
        )

        # ── Phase 8: ReconciliationValidator (ADR-015 F7) ────────────────────
        # Reads the reconciliation_required flag from DynamoDB (1s cache).
        # Rejects all non-closeout, non-paper signals while operator is
        # reconciling broker/DynamoDB/Kafka position drift.
        self._reconciliation_validator = ReconciliationValidator(
            dynamo_client=self._dynamo,
            risk_state_table=self._settings.aws.dynamodb_table_risk_state,
            settings=self._settings,
        )

        # ── Phase 6: EntryBlockValidator (defense-in-depth ENTRY_BLOCK check) ──
        # Primary enforcement is strategy_engine (blocks upstream signal production).
        # This validator provides a second wall inside risk_engine.
        # Closeout signals are always exempt.  Paper signals are exempt in paper
        # profile.  In live profiles, read failures cause fail-closed for entries.
        _risk_profile = os.environ.get("RISK_PROFILE", "paper").lower()
        self._entry_block_validator = EntryBlockValidator(
            dynamo_client=self._dynamo,
            risk_state_table=self._settings.aws.dynamodb_table_risk_state,
            risk_profile=_risk_profile,
            cache_ttl_seconds=5.0,
        )

        # ── Phase 4: KillSwitchCache (in-memory, 1s DynamoDB poll) ────────────
        # Wraps existing KillSwitch with a background poll loop so kill switch
        # state is always current even if Kafka/SNS propagation path fails.
        # is_active() returns the cached value synchronously (0ms).
        self._kill_switch_cache = KillSwitchCache(
            kill_switch=self._kill_switch,
            poll_interval_seconds=1.0,
        )

        # ── Phase 4: RiskContextBuilder (pre-fetch all validator inputs) ──────
        self._context_builder = RiskContextBuilder(
            dynamo_client=self._dynamo,
            limits=self._limits,
            settings=self._settings,
        )

        # ── Phase 4: RiskAnalyticsEngine (background portfolio analytics) ─────
        self._analytics_engine = RiskAnalyticsEngine(
            dynamo_client=self._dynamo,
            instrument_registry=self._instrument_registry,
            settings=self._settings,
        )

        # ── Phase 6: EnrichmentWatchdog + KafkaEnrichedConsumer ──────────────────
        # EnrichmentState is shared between the watchdog and the processing loop.
        # Watchdog flips state.use_enriched when ai_engine lag breaches threshold.
        # Processing loop reads state.use_enriched before each poll to decide
        # which topic to consume from.
        self._enrichment_state = EnrichmentState()
        self._enrichment_watchdog: Optional[EnrichmentWatchdog] = None
        self._kafka_enriched_consumer: Optional[KafkaEnrichedConsumer] = None

        # ── Phase 8: KafkaLagWatchdog (ADR-015 F6) ───────────────────────────
        # Monitors risk-v1 consumer group lag. Activates kill switch when lag
        # exceeds threshold for 3 consecutive seconds (risk engine overloaded).
        self._lag_watchdog: Optional[KafkaLagWatchdog] = None

        # Kafka consumer + publisher — initialized in start()
        self._kafka_consumer: Optional[KafkaSignalConsumer] = None
        self._kafka_publisher: Optional[KafkaApprovedPublisher] = None
        self._orders_consumer: Optional[KafkaOrderEventsConsumer] = None
        self._kill_switch_listener: Optional[KafkaKillSwitchListener] = None
        self._retry_replayer: Optional[KafkaRetryReplayer] = None

        self._running = False
        self._shutdown_event = asyncio.Event()
        self._health_server = HealthServer(
            port=getattr(self._settings, "health_check_port", 8080),
            service_name="risk_engine",
        )

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def kill_switch(self) -> KillSwitch:
        """Access the kill switch instance."""
        return self._kill_switch

    async def start(self) -> None:
        """
        Start the Risk Engine Service.

        Raises:
            RuntimeError: If KAFKA_BOOTSTRAP_SERVERS is not set.
        """
        set_correlation_id()
        logger.info("Starting Risk Engine Service")

        # Validate mandatory configuration
        kafka_bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
        if not kafka_bootstrap:
            raise RuntimeError(
                "KAFKA_BOOTSTRAP_SERVERS is not set. "
                "The risk engine requires MSK Serverless Kafka to operate. "
                "Set KAFKA_BOOTSTRAP_SERVERS to the MSK bootstrap endpoint and restart."
            )

        await self._health_server.start()

        # Phase 4: Start kill switch cache (background 1s DynamoDB poll).
        # Must start BEFORE load_state() so the initial seed read and the
        # polling loop both go through the same KillSwitch.load_state() path.
        await self._kill_switch_cache.start()
        logger.info("Kill switch cache started (poll_interval=1s)")

        # Restore kill switch state from DynamoDB
        await self._kill_switch.load_state()
        if self._kill_switch.active:
            logger.warning(
                "Kill switch is ACTIVE on startup — all signals will be rejected until deactivated"
            )

        # Rehydrate the daily realized P&L cache from DynamoDB.
        # Without this, a mid-day restart would reset the loss counter to zero.
        await self._loss_validator.rehydrate()
        logger.info("Daily P&L cache rehydrated from DynamoDB")

        # Start automatic kill-switch monitors (order rate, connectivity,
        # data staleness, strategy loss)
        await self._kill_switch_monitor.start()

        # Start Kafka consumer (signals.pending, risk-v1)
        self._kafka_consumer = KafkaSignalConsumer(
            bootstrap_servers=kafka_bootstrap,
            aws_region=self._settings.aws.region,
        )
        await self._kafka_consumer.start()
        logger.info("Kafka signal consumer started (group=risk-v1, topic=signals.pending)")

        # Start Kafka approved publisher (signals.approved)
        self._kafka_publisher = KafkaApprovedPublisher(
            bootstrap_servers=kafka_bootstrap,
            aws_region=self._settings.aws.region,
        )
        await self._kafka_publisher.start()
        logger.info("Kafka approved publisher started (topic=signals.approved)")

        # Start Kafka orders.events consumer (fills → P&L + position updates)
        self._orders_consumer = KafkaOrderEventsConsumer(
            bootstrap_servers=kafka_bootstrap,
            aws_region=self._settings.aws.region,
        )
        await self._orders_consumer.start()
        logger.info("Kafka orders consumer started (group=risk-v1, topic=orders.events)")

        # Start Kafka risk.kill-switch listener (fast propagation secondary path)
        self._kill_switch_listener = KafkaKillSwitchListener(
            bootstrap_servers=kafka_bootstrap,
            aws_region=self._settings.aws.region,
            kill_switch=self._kill_switch,
        )
        await self._kill_switch_listener.start()
        logger.info("Kafka kill-switch listener started (group=risk-v1, topic=risk.kill-switch)")

        self._retry_replayer = KafkaRetryReplayer(
            bootstrap_servers=kafka_bootstrap,
            aws_region=self._settings.aws.region,
            # Phase 7: signals.enriched.retry added — replays failed enriched
            # signals back to signals.enriched for re-processing by _enriched_processing_loop.
            source_topics=["signals.pending", "signals.enriched", "orders.events"],
            source_service="risk_engine",
            consumer_group="risk-retry-v1",
        )
        await self._retry_replayer.start()
        logger.info(
            "Kafka retry replayer started (topics=[signals.pending.retry, orders.events.retry])"
        )

        # ── Phase 6: KafkaEnrichedConsumer + EnrichmentWatchdog ──────────────
        # KafkaEnrichedConsumer is the primary signal path.  It subscribes to
        # signals.enriched (v4.0) in normal mode and signals.pending (v3.0
        # fallback) when the EnrichmentWatchdog detects ai_engine lag.
        # Both consumers are in the same consumer group (risk-v1) but read
        # different topics so partition assignment does not conflict.
        self._kafka_enriched_consumer = KafkaEnrichedConsumer(
            bootstrap_servers=kafka_bootstrap,
            aws_region=self._settings.aws.region,
        )
        await self._kafka_enriched_consumer.start()
        logger.info(
            "Kafka enriched consumer started (group=risk-v1, topic=signals.enriched)"
        )

        import boto3 as _boto3
        _cw_client = _boto3.client("cloudwatch", region_name=self._settings.aws.region)

        # Phase 2.1 Q1: hand the reconciliation validator the CloudWatch client so a
        # DynamoDB read failure of the reconciliation_required flag emits an alarmable
        # metric (ReconciliationFlagReadFailure). Fail-open behaviour is UNCHANGED —
        # this only adds observability. Reuses the client already built for the watchdog.
        if getattr(self, "_reconciliation_validator", None) is not None:
            self._reconciliation_validator._metrics = _cw_client

        self._enrichment_watchdog = EnrichmentWatchdog(
            state=self._enrichment_state,
            kafka_bootstrap=kafka_bootstrap,
            aws_region=self._settings.aws.region,
            dynamo_client=self._dynamo,
            config_table=getattr(
                self._settings.aws, "dynamodb_table_strategy_config", None
            ),
            metrics_client=_cw_client,
        )
        await self._enrichment_watchdog.start()
        logger.info(
            "Enrichment watchdog started "
            "(lag_threshold=%d, window=%d, recovery_window=%d)",
            self._enrichment_watchdog._lag_threshold,
            self._enrichment_watchdog._window,
            self._enrichment_watchdog._recovery_window,
        )

        # ── Phase 8: KafkaLagWatchdog (ADR-015 F6) ───────────────────────────
        # Consumer provider returns the active confluent-kafka Consumer object.
        # In enriched mode → KafkaEnrichedConsumer._consumer; in fallback mode
        # → KafkaSignalConsumer._consumer. The watchdog measures the consumer's
        # own assignment lag so it detects when risk_engine itself is falling behind.
        def _active_consumer_provider():
            if self._enrichment_state.use_enriched and self._kafka_enriched_consumer is not None:
                return self._kafka_enriched_consumer._consumer
            if self._kafka_consumer is not None:
                return self._kafka_consumer._consumer
            return None

        self._lag_watchdog = KafkaLagWatchdog(
            consumer_provider=_active_consumer_provider,
            kill_switch_activate=self._kill_switch.activate,
        )
        logger.info(
            "Kafka lag watchdog initialized (group=risk-v1, "
            "threshold=500 msgs, consecutive=3, interval=1s)"
        )

        # Register OS signal handlers for EC2 instance termination
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        # Health checks
        self._health_server.add_check(
            "kill_switch_loaded",
            lambda: True,  # load_state() completed above without raising
        )
        self._health_server.add_check(
            "dynamo_client_present",
            lambda: self._dynamo is not None,
        )
        self._health_server.add_check(
            "kafka_consumer_ready",
            lambda: self._kafka_consumer is not None,
        )
        self._health_server.add_check(
            "kafka_publisher_ready",
            lambda: self._kafka_publisher is not None,
        )
        self._health_server.add_check(
            "kafka_orders_consumer_ready",
            lambda: self._orders_consumer is not None,
        )
        self._health_server.add_check(
            "kafka_kill_switch_listener_ready",
            lambda: self._kill_switch_listener is not None,
        )
        self._health_server.add_check(
            "kafka_retry_replayer_ready",
            lambda: self._retry_replayer is not None,
        )
        self._health_server.add_check(
            "kafka_enriched_consumer_ready",
            lambda: self._kafka_enriched_consumer is not None,
        )
        self._health_server.add_check(
            "enrichment_watchdog_ready",
            lambda: self._enrichment_watchdog is not None,
        )
        self._health_server.add_check(
            "lag_watchdog_ready",
            lambda: self._lag_watchdog is not None,
        )
        self._health_server.set_ready(True)

        self._running = True
        logger.info(
            "Risk Engine Service started — "
            "consuming signals.enriched (primary, Phase 6), "
            "signals.pending (fallback + pre-Phase-6 path), "
            "orders.events, risk.kill-switch (all group=risk-v1)"
        )

        # Run eight loops concurrently:
        #   1. _enriched_processing_loop  — signals.enriched → validate → signals.approved  [Phase 6]
        #   2. _kafka_processing_loop     — signals.pending  → validate → signals.approved  (fallback)
        #   3. _nav_refresh_loop          — DynamoDB NAV refresh every 30s
        #   4. _orders_event_loop         — orders.events    → P&L + position updates
        #   5. _kill_switch_listener_loop — risk.kill-switch → immediate activation
        #   6. analytics_engine.start()   — background portfolio analytics (Phase 4)
        #   7. _retry_replayer.run()      — drain retry topics back to primary topics
        #   8. _lag_watchdog.run()        — risk-v1 lag monitor → kill switch on overload [Phase 8]
        # EnrichmentWatchdog runs as its own asyncio.Task (started above via watchdog.start()).
        lag_watchdog_coro = (
            self._lag_watchdog.run()
            if self._lag_watchdog is not None
            else asyncio.sleep(0)
        )
        await asyncio.gather(
            self._enriched_processing_loop(),
            self._kafka_processing_loop(),
            self._nav_refresh_loop(),
            self._orders_event_loop(),
            self._kill_switch_listener_loop(),
            self._analytics_engine.start(),
            self._retry_replayer.run(),
            lag_watchdog_coro,
        )

    async def stop(self) -> None:
        """Gracefully stop the Risk Engine Service."""
        if not self._running:
            return

        logger.info("Stopping Risk Engine Service")
        self._running = False
        self._health_server.set_ready(False)
        self._shutdown_event.set()

        await self._kill_switch_monitor.stop()
        await self._kill_switch_cache.stop()  # Phase 4
        await self._analytics_engine.stop()  # Phase 4

        # Phase 8: stop lag watchdog before consumers to avoid measuring lag
        # on a closed consumer.
        if self._lag_watchdog is not None:
            await self._lag_watchdog.stop()

        # Phase 6: stop watchdog before consumers so the watchdog doesn't
        # attempt a topic switch after the consumer is closed.
        if self._enrichment_watchdog is not None:
            await self._enrichment_watchdog.stop()
        if self._kafka_enriched_consumer is not None:
            await self._kafka_enriched_consumer.stop()

        if self._kill_switch_listener is not None:
            await self._kill_switch_listener.stop()
        if self._kafka_consumer is not None:
            await self._kafka_consumer.stop()
        if self._orders_consumer is not None:
            await self._orders_consumer.stop()
        if self._kafka_publisher is not None:
            await self._kafka_publisher.stop()
        if self._retry_replayer is not None:
            await self._retry_replayer.stop()

        await self._health_server.stop()
        logger.info("Risk Engine Service stopped")

    async def validate_signal(self, signal_obj: Signal) -> RiskDecision:
        """
        Run a signal through all risk validators.

        Validators execute in sequence:
            1. Signal age validator (O(1), no DB — rejects stale signals first).
            2. Kill switch check (instant reject if active).
            3. Position size validator (includes pending orders).
            4. Exposure validator.
            5. Daily loss validator.
            6. Margin validator.
            7. Slippage validator.

        If ANY validator rejects, the pipeline short-circuits and the signal
        is rejected. Every decision is logged to S3 for audit.

        Args:
            signal_obj: The trading signal to validate.

        Returns:
            RiskDecision with approval/rejection status and reasoning.
        """
        risk_decision_id = str(uuid.uuid4())
        validator_results: list[RiskValidationResult] = []

        # Record data-feed heartbeat at the earliest possible moment — even
        # before age validation — so the staleness clock is reset as long as
        # signals arrive from strategy_engine, regardless of whether they pass
        # age or kill-switch checks.  This prevents the staleness auto-trigger
        # from re-firing immediately after deactivation and during warmup gaps.
        self._kill_switch_monitor.record_data_tick(signal_obj.market)

        # 1. Signal age check — O(1), no DB reads.  Fast-path rejection before
        #    any DynamoDB I/O.
        age_result = await self._signal_age_validator.validate(signal_obj)
        if not age_result.approved:
            decision = RiskDecision(
                risk_decision_id=risk_decision_id,
                signal_id=signal_obj.signal_id,
                status=RiskDecisionStatus.REJECTED,
                reason=age_result.reason,
                validator_results=[age_result],
            )
            await self._log_decision(decision)
            return decision

        validator_results.append(age_result)

        # 2. Kill switch check — O(0), reads in-memory KillSwitchCache.
        #    Phase 4: replaced direct DynamoDB read with cached value (0ms).
        if self._kill_switch_cache.is_active():
            decision = RiskDecision(
                risk_decision_id=risk_decision_id,
                signal_id=signal_obj.signal_id,
                status=RiskDecisionStatus.REJECTED,
                reason=f"Kill switch is active: {self._kill_switch_cache.reason}",
                validator_results=[],
            )
            await self._log_decision(decision)
            logger.warning(
                "Signal %s REJECTED — kill switch active: %s",
                signal_obj.signal_id,
                self._kill_switch_cache.reason,
            )
            return decision

        # 2b. Entry-block check — Phase 6 defense-in-depth.
        #     Primary enforcement is strategy_engine (upstream).  This validator
        #     provides a second wall: if ENTRY_BLOCK/GLOBAL is active in DynamoDB,
        #     new-entry signals are rejected here regardless of kill-switch state.
        #     Closeout signals are always exempt.
        eb_result = await self._entry_block_validator.validate(signal_obj)
        if not eb_result.approved:
            decision = RiskDecision(
                risk_decision_id=risk_decision_id,
                signal_id=signal_obj.signal_id,
                status=RiskDecisionStatus.REJECTED,
                reason=eb_result.reason,
                validator_results=[eb_result],
            )
            await self._log_decision(decision)
            logger.warning(
                "Signal %s REJECTED — entry block active: %s",
                signal_obj.signal_id,
                eb_result.reason,
            )
            return decision
        validator_results.append(eb_result)

        # 3. Reconciliation check — O(1) cached (1s TTL), no extra DynamoDB round-trip.
        #    Phase 8 (ADR-015 F7): rejects non-closeout live signals when position
        #    drift has been detected and operator reconciliation is pending.
        recon_result = await self._reconciliation_validator.validate(signal_obj)
        validator_results.append(recon_result)
        if not recon_result.approved:
            decision = RiskDecision(
                risk_decision_id=risk_decision_id,
                signal_id=signal_obj.signal_id,
                status=RiskDecisionStatus.REJECTED,
                reason=recon_result.reason,
                validator_results=validator_results,
            )
            await self._log_decision(decision)
            return decision

        # 4. Pre-fetch all validator inputs in one parallel batch (Phase 4).
        #    3–4 concurrent DynamoDB reads; wall-clock ~5–8ms.
        #    All downstream validators read from this context — zero additional I/O.
        context = await self._context_builder.build(signal_obj)
        if context.risk_data_errors:
            result = RiskValidationResult(
                approved=bool(getattr(signal_obj, "paper_trade", False)),
                validator_name="risk_context_builder",
                reason=(
                    "PAPER_WARN_RISK_DATA_UNAVAILABLE: "
                    if getattr(signal_obj, "paper_trade", False)
                    else "LIVE_RISK_DATA_UNAVAILABLE: "
                )
                + "core risk context reads unavailable: "
                + ", ".join(context.risk_data_errors),
                details={
                    "risk_data_errors": list(context.risk_data_errors),
                    "context": context.to_dict(),
                },
            )
            validator_results.append(result)
            if not result.approved:
                decision = RiskDecision(
                    risk_decision_id=risk_decision_id,
                    signal_id=signal_obj.signal_id,
                    status=RiskDecisionStatus.REJECTED,
                    reason=result.reason,
                    validator_results=validator_results,
                )
                await self._log_decision(decision)
                logger.critical(
                    "Signal %s REJECTED — live risk context incomplete: %s",
                    signal_obj.signal_id,
                    context.risk_data_errors,
                )
                return decision

        # 5–12. Run validators in sequence using pre-fetched context where
        #       possible.  Legacy validators (position, exposure, loss, margin,
        #       slippage) still accept Signal objects; they are progressively
        #       migrated to RiskContext in Phase 5.
        #
        #   Pipeline (ADR-014 + Phase 8, 12 steps):
        #     5.  PositionValidator      — DynamoDB (uses own reads; TODO migrate Phase 5)
        #     6.  ExposureValidator      — DynamoDB scan (TODO: read from context Phase 5)
        #     7.  LiquidityValidator     — context.adv_20d           [NEW Phase 4]
        #     8.  SpreadGateValidator    — context.live_spread_bps   [NEW Phase 4]
        #     9.  SectorConcentrationValidator — context.analytics   [NEW Phase 4]
        #     10. MarginValidator        — in-memory cache
        #     11. SlippageValidator      — DynamoDB ADV read
        #     12. DailyLossValidator     — in-memory cache

        # Steps 4–5: legacy validators (still read their own DynamoDB)
        for validator in [self._position_validator, self._exposure_validator]:
            result = await validator.validate(signal_obj)
            validator_results.append(result)
            if not result.approved:
                decision = RiskDecision(
                    risk_decision_id=risk_decision_id,
                    signal_id=signal_obj.signal_id,
                    status=RiskDecisionStatus.REJECTED,
                    reason=result.reason,
                    validator_results=validator_results,
                )
                await self._log_decision(decision)
                logger.warning(
                    "Signal %s REJECTED by %s: %s",
                    signal_obj.signal_id,
                    result.validator_name,
                    result.reason,
                )
                return decision

        # Steps 6–8: new Phase 4 context-based validators (synchronous, 0ms)
        for context_validator in [
            self._liquidity_validator,
            self._spread_gate_validator,
            self._sector_validator,
        ]:
            result = context_validator.validate(context)
            validator_results.append(result)
            if not result.approved:
                decision = RiskDecision(
                    risk_decision_id=risk_decision_id,
                    signal_id=signal_obj.signal_id,
                    status=RiskDecisionStatus.REJECTED,
                    reason=result.reason,
                    validator_results=validator_results,
                )
                await self._log_decision(decision)
                logger.warning(
                    "Signal %s REJECTED by %s: %s",
                    signal_obj.signal_id,
                    result.validator_name,
                    result.reason,
                )
                return decision

        # Steps 9–11: remaining legacy async validators
        validators = [
            self._margin_validator,
            self._slippage_validator,
            self._loss_validator,
        ]

        for validator in validators:
            result = await validator.validate(signal_obj)
            validator_results.append(result)

            if not result.approved:
                decision = RiskDecision(
                    risk_decision_id=risk_decision_id,
                    signal_id=signal_obj.signal_id,
                    status=RiskDecisionStatus.REJECTED,
                    reason=result.reason,
                    validator_results=validator_results,
                )
                await self._log_decision(decision)
                logger.warning(
                    "Signal %s REJECTED by %s: %s",
                    signal_obj.signal_id,
                    result.validator_name,
                    result.reason,
                )

                # Auto-activate kill switch if daily loss limit breached
                if result.validator_name == DailyLossValidator.VALIDATOR_NAME:
                    await self._kill_switch.activate(
                        reason=f"Auto-triggered: {result.reason}",
                        activated_by="loss_validator",
                    )

                return decision

        # All validators passed
        decision = RiskDecision(
            risk_decision_id=risk_decision_id,
            signal_id=signal_obj.signal_id,
            status=RiskDecisionStatus.APPROVED,
            reason="All risk checks passed",
            validator_results=validator_results,
        )
        await self._log_decision(decision)

        logger.info(
            "Signal %s APPROVED (decision %s) — %s %s %s qty=%d",
            signal_obj.signal_id,
            risk_decision_id,
            signal_obj.strategy_name,
            signal_obj.direction.value,
            signal_obj.symbol,
            signal_obj.quantity,
        )

        return decision

    # ── Phase 6: Enriched signal loop ────────────────────────────────────────

    @staticmethod
    def _signal_from_enriched(event: "EnrichedSignalEvent") -> Signal:
        """
        Extract a mutable Signal from an EnrichedSignalEvent.

        EnrichedSignal is a frozen dataclass, so we reconstruct a Signal
        (plain dataclass, mutable) from its fields.  Enrichment metadata
        (regime, quality_score, filtered) is appended to signal.metadata
        so validators and audit logs have full context.

        Args:
            event: The enriched signal event from KafkaEnrichedConsumer.

        Returns:
            A Signal instance ready for validate_signal().
        """
        e = event.enriched
        metadata: dict[str, Any] = dict(e.metadata)
        # Carry enrichment annotations into metadata so audit log captures them.
        metadata.update({
            "strategy_id":        e.strategy_id,
            "product_type":       e.product_type,
            "expires_at":         e.expires_at.isoformat(),
            "regime":             e.regime,
            "regime_confidence":  str(e.regime_confidence),
            "quality_score":      str(e.quality_score),
            "filtered":           str(e.filtered),
            "schema_version":     e.schema_version,
            "enrichment_latency_ms": str(e.enrichment_latency_ms),
        })
        return Signal(
            signal_id       = e.signal_id,
            strategy_name   = e.strategy_name,
            symbol          = e.symbol,
            market          = e.market,
            direction       = e.direction,
            quantity        = e.quantity,
            price_at_signal = e.price_at_signal,
            confidence      = e.confidence,
            generated_at    = e.generated_at,
            status          = SignalStatus.PENDING,
            stop_loss       = e.stop_loss,
            take_profit     = e.take_profit,
            paper_trade     = e.paper_trade,
            metadata        = metadata,
        )

    async def _publish_approved_enriched(
        self,
        event: "EnrichedSignalEvent",
        signal_obj: Signal,
        risk_decision_id: str,
    ) -> None:
        """
        Durably publish an enriched signal that passed risk validation.

        Mirrors _publish_approved_signal() but takes an EnrichedSignalEvent
        (which wraps a frozen EnrichedSignal) rather than the mutable SignalEvent.
        Sets signal.status = APPROVED before publishing.

        Args:
            event:            The enriched signal event (source of trace_id).
            signal_obj:       Mutable Signal reconstructed via _signal_from_enriched().
            risk_decision_id: The approved risk decision ID for audit linkage.
        """
        signal_obj.status = SignalStatus.APPROVED
        await self._kafka_publisher.publish(  # type: ignore[union-attr]
            signal=signal_obj,
            risk_decision_id=risk_decision_id,
            trace_id=event.trace_id,
        )
        await self._mark_risk_decision_published(signal_obj.signal_id, risk_decision_id)
        self._kill_switch_monitor.record_order()

    async def _enriched_processing_loop(self) -> None:
        """
        Phase 6 primary loop: consume signals.enriched → validate → signals.approved.

        Active when ``_enrichment_state.use_enriched=True`` (the default).  When
        EnrichmentWatchdog detects that ai_engine is lagging it sets
        use_enriched=False and this loop sleeps, yielding to _kafka_processing_loop()
        which reads signals.pending directly.

        Schema handling:
            KafkaEnrichedConsumer transparently handles both v4.0
            (SIGNAL_ENRICHED from signals.enriched) and v3.0
            (SIGNAL_PENDING from signals.pending during subscribe_to() switch).

        Error policy (Phase 7):
            - Transient processing errors  → publish_retry() → signals.enriched.retry
              (auto-escalates to DLQ after max_retry_attempts=3 via KafkaFailurePublisher)
            - Expired signals              → publish_dlq()   → signals.enriched.dlq
            - Offset is committed only after retry/DLQ routing succeeds.
            - The consumer is never left stalled — on publish failure the offset
              is still committed and the error is logged.

        Loop structure:
            1. Sleep 0.5s if not in enriched mode (yield to fallback loop).
            2. Poll from _kafka_enriched_consumer (≤ 1s block).
            3. Dedup check against DynamoDB risk-decision table.
            4. Expiry check.
            5. validate_signal() through the full risk validator pipeline.
            6. Reserve + publish to signals.approved.
            7. Commit offset only after durable publish.
        """
        if self._kafka_enriched_consumer is None:
            logger.warning(
                "enriched_processing_loop: no enriched consumer — Phase 6 loop disabled"
            )
            return

        logger.info("enriched_processing_loop.started (risk-v1, signals.enriched)")

        while self._running:
            if self._shutdown_event.is_set():
                break

            # Yield to _kafka_processing_loop when fallback mode is active.
            if not self._enrichment_state.use_enriched:
                await asyncio.sleep(0.5)
                continue

            try:
                enriched_event: Optional[EnrichedSignalEvent] = await asyncio.to_thread(
                    self._kafka_enriched_consumer.poll_signal
                )
                if enriched_event is None:
                    continue

                try:
                    signal_obj = self._signal_from_enriched(enriched_event)

                    # ── Deduplication ─────────────────────────────────────────
                    existing_reservation = await self._get_risk_decision_reservation(
                        signal_obj.signal_id
                    )
                    if existing_reservation is not None:
                        publish_status  = attr_string(
                            existing_reservation, "publish_status", ""
                        )
                        risk_decision_id = attr_string(
                            existing_reservation, "risk_decision_id", ""
                        )
                        if (
                            publish_status == _RISK_DECISION_PUBLISH_PENDING
                            and risk_decision_id
                        ):
                            logger.warning(
                                "enriched_loop.replay_pending "
                                "signal_id=%s risk_decision_id=%s",
                                signal_obj.signal_id,
                                risk_decision_id,
                            )
                            await self._publish_approved_enriched(
                                enriched_event, signal_obj, risk_decision_id
                            )
                        else:
                            logger.warning(
                                "enriched_loop.duplicate_suppressed signal_id=%s "
                                "publish_status=%s",
                                signal_obj.signal_id,
                                publish_status or "LEGACY_UNKNOWN",
                            )
                        self._kafka_enriched_consumer.commit(enriched_event)
                        continue

                    # ── Expiry check ──────────────────────────────────────────
                    now = utc_now()
                    expires_at = enriched_event.expires_at
                    if now.tzinfo is None and expires_at.tzinfo is not None:
                        from datetime import timezone
                        now = now.replace(tzinfo=timezone.utc)

                    if now > expires_at:
                        decision = RiskDecision(
                            risk_decision_id=str(uuid.uuid4()),
                            signal_id=signal_obj.signal_id,
                            status=RiskDecisionStatus.REJECTED,
                            reason=(
                                f"Enriched signal expired at {expires_at.isoformat()} "
                                f"before risk processing at {now.isoformat()}"
                            ),
                            validator_results=[],
                        )
                        await self._log_decision(decision)
                        # Phase 7: route expired signals to DLQ (no retry value).
                        self._kafka_enriched_consumer.publish_dlq(
                            enriched_event,
                            reason=(
                                f"Signal expired at {expires_at.isoformat()} "
                                f"(processed at {now.isoformat()})"
                            ),
                            error_type="signal_expired",
                            details={
                                "signal_id":  signal_obj.signal_id,
                                "expires_at": expires_at.isoformat(),
                                "processed_at": now.isoformat(),
                            },
                        )
                        self._kafka_enriched_consumer.commit(enriched_event)
                        continue

                    # ── Validate ──────────────────────────────────────────────
                    decision = await self.validate_signal(signal_obj)
                    decision.enriched = True  # signal came via signals.enriched (ai_engine)

                    if decision.status == RiskDecisionStatus.APPROVED:
                        reserved = await self._reserve_risk_decision(decision)
                        if not reserved:
                            # Lost a concurrent conditional write — check what won.
                            existing = await self._get_risk_decision_reservation(
                                decision.signal_id
                            )
                            if existing is None:
                                raise RuntimeError(
                                    "Risk decision reservation conditional put failed "
                                    f"but no reservation readable for "
                                    f"signal_id={decision.signal_id}"
                                )
                            rdid = attr_string(existing, "risk_decision_id", "")
                            ps   = attr_string(existing, "publish_status", "")
                            if ps == _RISK_DECISION_PUBLISH_PENDING and rdid:
                                await self._publish_approved_enriched(
                                    enriched_event, signal_obj, rdid
                                )
                            else:
                                logger.warning(
                                    "enriched_loop.reservation_race_suppressed "
                                    "signal_id=%s publish_status=%s",
                                    decision.signal_id,
                                    ps or "LEGACY_UNKNOWN",
                                )
                        else:
                            await self._publish_approved_enriched(
                                enriched_event, signal_obj, decision.risk_decision_id
                            )

                    # Commit only after risk decision is logged and any publish
                    # is durably acknowledged by Kafka.
                    self._kafka_enriched_consumer.commit(enriched_event)

                except Exception as exc:
                    _sig_id = getattr(
                        getattr(enriched_event, "enriched", None),
                        "signal_id",
                        "unknown",
                    )
                    logger.exception(
                        "enriched_processing_loop.signal_error signal_id=%s — "
                        "routing to retry topic",
                        _sig_id,
                    )
                    # Phase 7: route transient failures to .retry (auto-escalates
                    # to DLQ after max_retry_attempts=3 via KafkaFailurePublisher).
                    self._kafka_enriched_consumer.publish_retry(
                        enriched_event,
                        reason=str(exc),
                        error_type="enriched_processing_failed",
                        details={"signal_id": _sig_id},
                    )
                    self._kafka_enriched_consumer.commit(enriched_event)

            except Exception:
                logger.exception(
                    "enriched_processing_loop.poll_error — backing off 1s"
                )
                await asyncio.sleep(1)

        logger.info("enriched_processing_loop.stopped")

    # ── Kafka processing loop ─────────────────────────────────────────────────

    async def _kafka_processing_loop(self) -> None:
        """
        Fallback loop: poll signals.pending (risk-v1) and validate signals.

        Active only when ``_enrichment_state.use_enriched=False`` (i.e., the
        EnrichmentWatchdog has detected that ai_engine is lagging and has
        switched off enriched mode).  In normal Phase 6 operation this loop
        yields immediately each iteration so _enriched_processing_loop() owns
        the CPU.

        Loop structure:
            1. Yield (sleep 0.5s) if enriched consumer is active.
            2. Check shutdown event.
            3. poll_signal() via asyncio.to_thread (blocks ≤ 1s).
            4. Validate signal through all risk validators.
            5. Publish approved signals to signals.approved.
        """
        logger.info("Kafka processing loop started (risk-v1, signals.pending — fallback path)")

        while self._running:
            if self._shutdown_event.is_set():
                break

            # Phase 6: yield to _enriched_processing_loop when enriched mode is active.
            # This keeps _kafka_consumer alive (heartbeats continue via confluent-kafka's
            # internal thread) while avoiding double-processing of signals.
            if (
                self._kafka_enriched_consumer is not None
                and self._enrichment_state.use_enriched
            ):
                await asyncio.sleep(0.5)
                continue

            try:
                from risk_engine.consumers.kafka_signal_consumer import SignalEvent

                signal_event: Optional[SignalEvent] = await asyncio.to_thread(
                    self._kafka_consumer.poll_signal  # type: ignore[union-attr]
                )
                if signal_event is None:
                    continue

                try:
                    existing_reservation = await self._get_risk_decision_reservation(
                        signal_event.signal.signal_id
                    )
                    if existing_reservation is not None:
                        await self._handle_existing_approval_reservation(
                            signal_event,
                            existing_reservation,
                        )
                        self._kafka_consumer.commit(signal_event)  # type: ignore[union-attr]
                        continue

                    now = utc_now()
                    expires_at = signal_event.expires_at
                    if now.tzinfo is None and expires_at.tzinfo is not None:
                        from datetime import timezone
                        now = now.replace(tzinfo=timezone.utc)

                    if now > expires_at:
                        result = RiskValidationResult(
                            approved=False,
                            validator_name="signal_expiry_validator",
                            reason=(
                                f"Signal expired at {expires_at.isoformat()} before risk "
                                f"processing at {now.isoformat()}"
                            ),
                            details={
                                "expires_at": expires_at.isoformat(),
                                "risk_received_at": now.isoformat(),
                                "raw_topic": signal_event.raw_topic,
                                "raw_offset": signal_event.raw_offset,
                            },
                        )
                        decision = RiskDecision(
                            risk_decision_id=str(uuid.uuid4()),
                            signal_id=signal_event.signal.signal_id,
                            status=RiskDecisionStatus.REJECTED,
                            reason=result.reason,
                            validator_results=[result],
                        )
                        await self._log_decision(decision)
                        routed = self._kafka_consumer.publish_dlq(  # type: ignore[union-attr]
                            signal_event,
                            reason=decision.reason,
                            error_type="stale_signal",
                            details=decision.to_dict(),
                        )
                        if routed:
                            self._kafka_consumer.commit(signal_event)  # type: ignore[union-attr]
                        continue

                    decision = await self.validate_signal(signal_event.signal)

                    if decision.status == RiskDecisionStatus.APPROVED:
                        reserved = await self._reserve_risk_decision(decision)
                        if not reserved:
                            existing_reservation = await self._get_risk_decision_reservation(
                                decision.signal_id
                            )
                            if existing_reservation is None:
                                raise RuntimeError(
                                    "Risk decision reservation conditional put failed "
                                    f"but no reservation was readable for signal_id={decision.signal_id}"
                                )
                            await self._handle_existing_approval_reservation(
                                signal_event,
                                existing_reservation,
                            )
                        else:
                            await self._publish_approved_signal(
                                signal_event,
                                decision.risk_decision_id,
                            )
                    elif (
                        decision.validator_results
                        and decision.validator_results[0].validator_name
                        in ("signal_age_validator", "signal_expiry_validator")
                    ):
                        routed = self._kafka_consumer.publish_dlq(  # type: ignore[union-attr]
                            signal_event,
                            reason=decision.reason,
                            error_type="stale_signal",
                            details=decision.to_dict(),
                        )
                        if not routed:
                            raise RuntimeError(
                                f"Failed to route stale signal {signal_event.signal.signal_id} to DLQ"
                            )

                    # Manual commit only after the risk decision has been logged and
                    # approved events have been durably acknowledged by Kafka and
                    # marked PUBLISHED, or rejection routing is done.
                    self._kafka_consumer.commit(signal_event)  # type: ignore[union-attr]

                except Exception as exc:
                    routed = self._kafka_consumer.publish_retry(  # type: ignore[union-attr]
                        signal_event,
                        reason=str(exc),
                        error_type="risk_processing_failed",
                        details={
                            "signal_id": signal_event.signal.signal_id,
                            "raw_topic": signal_event.raw_topic,
                            "raw_offset": signal_event.raw_offset,
                        },
                    )
                    if routed:
                        self._kafka_consumer.commit(signal_event)  # type: ignore[union-attr]
                    else:
                        raise

            except Exception:
                logger.exception("Error in Kafka processing loop — backing off 1s")
                await asyncio.sleep(1)

    # ── NAV refresh loop ──────────────────────────────────────────────────────

    async def _nav_refresh_loop(self) -> None:
        """
        Background loop: refresh portfolio NAV from DynamoDB every 30 seconds.

        The execution engine writes a NAV#CURRENT item to the risk-state table
        after every fill. This loop reads that item and updates
        self._limits.portfolio_value so percentage-based risk checks are always
        computed against the actual current NAV.

        Without this: a drawdown silently makes percentage limits looser than
        intended, compounding losses in a bad market.
        """
        _REFRESH_INTERVAL = 30.0
        _STALE_WARN_THRESHOLD = 120.0

        logger.info("nav_refresh_loop.started")

        while self._running:
            await asyncio.sleep(_REFRESH_INTERVAL)

            if not self._running:
                break

            try:
                if self._dynamo is None:
                    continue

                response = await asyncio.to_thread(
                    self._dynamo.get_item,
                    TableName=self._settings.aws.dynamodb_table_risk_state,
                    Key=nav_key(),
                    ConsistentRead=False,
                )
                item = response.get("Item")
                if item:
                    nav = float(item.get("portfolio_value", {}).get("N", "0"))
                    if nav > 0:
                        old_nav = self._limits.portfolio_value
                        self._limits.update_portfolio_value(nav)
                        if abs(nav - old_nav) / max(old_nav, 1) > 0.02:
                            logger.info(
                                "nav_refresh.updated",
                                old_nav=round(old_nav, 2),
                                new_nav=round(nav, 2),
                                change_pct=round((nav - old_nav) / old_nav * 100, 2),
                            )
                else:
                    staleness = self._limits.nav_staleness_seconds()
                    if staleness > _STALE_WARN_THRESHOLD:
                        logger.warning(
                            "nav_refresh.no_item_in_dynamodb — NAV may be stale "
                            "(staleness=%.0fs). Execution engine fill handler may "
                            "not be writing NAV updates.",
                            staleness,
                        )

            except Exception:
                logger.exception("nav_refresh_loop.error")

        logger.info("nav_refresh_loop.stopped")

    # ── Order events loop ─────────────────────────────────────────────────────

    async def _orders_event_loop(self) -> None:
        """
        Background loop: consume orders.events (risk-v1) to update P&L and
        position state after every broker fill or rejection.

        Responsibilities:
            ORDER_FILLED / ORDER_PARTIAL:
                - Record realized P&L in DailyLossValidator (idempotent via DynamoDB).
                - Trigger NAV update in RiskLimits (mark-to-market after fill).
            ORDER_REJECTED:
                - No P&L impact; the position reservation was already released
                  by PositionValidator via DynamoDB conditional write on rejection.

        Loop structure:
            1. Check shutdown event.
            2. poll_fill() via asyncio.to_thread (blocks ≤ 1s).
            3. Dispatch to handle_fill() / handle_rejection() based on event type.
        """
        logger.info("orders_event_loop.started (risk-v1, orders.events)")

        while self._running:
            if self._shutdown_event.is_set():
                break

            try:
                fill_event: Optional[OrderFillEvent] = await asyncio.to_thread(
                    self._orders_consumer.poll_fill  # type: ignore[union-attr]
                )
                if fill_event is None:
                    continue

                try:
                    if fill_event.is_fill:
                        await self._handle_fill(fill_event)
                    elif fill_event.is_rejected:
                        logger.info(
                            "orders_event_loop.order_rejected "
                            "(order_id=%s, signal_id=%s, reason=%r)",
                            fill_event.order_id,
                            fill_event.signal_id,
                            fill_event.reject_reason,
                        )
                        # Position reservation released by execution engine; no P&L action.

                    self._orders_consumer.commit(fill_event)  # type: ignore[union-attr]

                except Exception as exc:
                    routed = self._orders_consumer.publish_retry(  # type: ignore[union-attr]
                        fill_event,
                        reason=str(exc),
                        error_type="order_event_processing_failed",
                        details={
                            "order_id": fill_event.order_id,
                            "signal_id": fill_event.signal_id,
                            "raw_topic": fill_event.raw_topic,
                            "raw_offset": fill_event.raw_offset,
                        },
                    )
                    if routed:
                        self._orders_consumer.commit(fill_event)  # type: ignore[union-attr]
                    else:
                        raise

            except Exception:
                logger.exception("orders_event_loop.error — backing off 1s")
                await asyncio.sleep(1)

        logger.info("orders_event_loop.stopped")

    async def _handle_fill(self, fill_event: OrderFillEvent) -> None:
        """
        Update realized P&L and NAV after an ORDER_FILLED or ORDER_PARTIAL event.

        Updates:
            1. DailyLossValidator — records the notional value and direction of
               the fill so the daily loss counter stays accurate.
            2. RiskLimits — updates portfolio_value immediately after a fill so
               the next signal's percentage-based limits reflect current NAV.

        Args:
            fill_event: Parsed ORDER_FILLED or ORDER_PARTIAL event.
        """
        if fill_event.avg_fill_price is None or fill_event.quantity_filled == 0:
            logger.warning(
                "handle_fill.no_price_or_qty (order_id=%s, event_type=%s) — skipping P&L update",
                fill_event.order_id,
                fill_event.event_type,
            )
            return

        try:
            # Record P&L in DailyLossValidator (idempotent — DynamoDB conditional write).
            # For simplicity the realized P&L on a fill is recorded as 0 cost basis
            # (the validator tracks cumulative notional loss, not per-trade profit).
            # A negative fill for a SELL reduces unrealized exposure; we record the
            # gross notional so the loss limit is based on turnover, not net P&L.
            pnl_update = await self._loss_validator.record_fill(
                order_id=fill_event.order_id,
                symbol=fill_event.symbol,
                market=fill_event.market,
                direction=fill_event.direction,
                quantity=fill_event.quantity_filled,
                price=fill_event.avg_fill_price,
                fill_id=f"{fill_event.raw_topic}:{fill_event.raw_partition}:{fill_event.raw_offset}",
            )
            pnl_state = pnl_update["total_daily_pnl"]
            self._kill_switch_monitor.record_broker_ping()
            self._kill_switch_monitor.record_strategy_pnl(
                fill_event.strategy_id,
                pnl_state,
                self._limits.get_portfolio_value(),
            )

            if pnl_state < 0:
                max_loss_pct = self._limits.get_limit(
                    "max_daily_loss_pct", market=fill_event.market
                )
                portfolio_value = self._limits.get_portfolio_value()
                max_loss_value = portfolio_value * (max_loss_pct / 100.0)
                if abs(pnl_state) >= max_loss_value:
                    await self._kill_switch.activate(
                        reason=(
                            f"Auto-triggered: daily loss {pnl_state:,.2f} exceeds "
                            f"{max_loss_pct:.2f}% limit ({max_loss_value:,.2f})"
                        ),
                        activated_by="daily_loss_fill_accounting",
                    )

            logger.info(
                "handle_fill.pnl_recorded "
                "(order_id=%s, event_type=%s, %s %s qty=%d @ %.4f, notional=%.2f)",
                fill_event.order_id,
                fill_event.event_type,
                fill_event.direction,
                fill_event.symbol,
                fill_event.quantity_filled,
                fill_event.avg_fill_price,
                fill_event.notional_value,
            )

        except AttributeError:
            # DailyLossValidator may not expose record_fill() if running an older
            # version of the code during a rolling deploy.  Log and continue — the
            # NAV refresh loop will pick up DynamoDB state within 30s.
            logger.warning(
                "handle_fill.record_fill_not_available — "
                "DailyLossValidator.record_fill() not found; NAV refresh will self-correct"
            )
        except Exception:
            logger.exception(
                "handle_fill.record_fill_error (order_id=%s) — P&L state may lag",
                fill_event.order_id,
            )

    # ── Kill switch listener loop ─────────────────────────────────────────────

    async def _kill_switch_listener_loop(self) -> None:
        """
        Delegate to KafkaKillSwitchListener.listen() as an asyncio task.

        Runs until _running becomes False.  If the listener raises an unexpected
        exception the error is logged but does NOT crash the service — the
        DynamoDB-based kill switch remains active as the primary safety mechanism.
        """
        if self._kill_switch_listener is None:
            logger.warning("kill_switch_listener_loop: listener not initialised — skipping")
            return

        try:
            await self._kill_switch_listener.listen()
        except Exception:
            logger.exception(
                "kill_switch_listener_loop.fatal_error — "
                "Kafka kill-switch listener crashed; DynamoDB kill switch still active"
            )

    async def _publish_approved_signal(
        self,
        signal_event: Any,
        risk_decision_id: str,
    ) -> None:
        """
        Durably publish an approved signal, then mark its reservation PUBLISHED.

        If Kafka delivery fails or the publish-status update fails, the caller
        must not commit the source ``signals.pending`` offset. Replay will find
        the PENDING reservation and retry publishing with the same
        risk_decision_id.
        """
        signal_event.signal.status = SignalStatus.APPROVED
        await self._kafka_publisher.publish(  # type: ignore[union-attr]
            signal=signal_event.signal,
            risk_decision_id=risk_decision_id,
            trace_id=signal_event.trace_id,
        )
        await self._mark_risk_decision_published(
            signal_event.signal.signal_id,
            risk_decision_id,
        )
        self._kill_switch_monitor.record_order()

    async def _handle_existing_approval_reservation(
        self,
        signal_event: Any,
        reservation: dict[str, Any],
    ) -> None:
        """
        Handle replay of a signal that already has a risk decision reservation.

        PUBLISHED means the approved event was already durably handed to Kafka
        and replay can be committed. PENDING means the prior attempt reserved a
        decision but did not complete Kafka delivery/status marking, so replay
        must republish instead of suppressing.
        """
        signal_id = signal_event.signal.signal_id
        publish_status = attr_string(reservation, "publish_status", "")
        risk_decision_id = attr_string(reservation, "risk_decision_id", "")
        if not risk_decision_id:
            raise RuntimeError(
                f"Risk decision reservation for signal_id={signal_id} has no risk_decision_id"
            )

        if publish_status == _RISK_DECISION_PUBLISH_PENDING:
            logger.warning(
                "Risk decision reservation is PENDING; republishing approved signal "
                "signal_id=%s risk_decision_id=%s",
                signal_id,
                risk_decision_id,
            )
            await self._publish_approved_signal(signal_event, risk_decision_id)
            return

        if publish_status in ("", _RISK_DECISION_PUBLISH_PUBLISHED):
            # Empty status is a legacy reservation written before publish_status
            # existed. Treat it as published to avoid creating duplicate orders.
            logger.warning(
                "Duplicate risk decision suppressed for signal_id=%s "
                "risk_decision_id=%s publish_status=%s",
                signal_id,
                risk_decision_id,
                publish_status or "LEGACY_UNKNOWN",
            )
            return

        raise RuntimeError(
            f"Unknown risk decision publish_status={publish_status!r} "
            f"for signal_id={signal_id}"
        )

    async def _get_risk_decision_reservation(
        self,
        signal_id: str,
    ) -> dict[str, Any] | None:
        """Read an existing risk decision reservation for a signal, if present."""
        if self._dynamo is None:
            raise RuntimeError("DynamoDB client unavailable for risk decision lookup")

        response = await asyncio.to_thread(
            self._dynamo.get_item,
            TableName=self._settings.aws.dynamodb_table_risk_state,
            Key=risk_decision_key(signal_id),
        )
        return response.get("Item")

    async def _reserve_risk_decision(self, decision: RiskDecision) -> bool:
        """
        Persist the signal_id risk decision before publishing approval.

        DynamoDB conditional put gives replay safety for ``signals.pending``:
        the first approved decision wins. The row starts with
        ``publish_status=PENDING`` and is marked ``PUBLISHED`` only after Kafka
        acknowledges the approved event.
        """
        if self._dynamo is None:
            raise RuntimeError(
                "DynamoDB client unavailable; refusing to publish approval without risk dedup"
            )

        try:
            await asyncio.to_thread(
                self._dynamo.put_item,
                TableName=self._settings.aws.dynamodb_table_risk_state,
                Item={
                    **risk_decision_key(decision.signal_id),
                    "risk_decision_id": {"S": decision.risk_decision_id},
                    "signal_id": {"S": decision.signal_id},
                    "status": {"S": decision.status.value},
                    "publish_status": {"S": _RISK_DECISION_PUBLISH_PENDING},
                    "reason": {"S": decision.reason},
                    "decision_json": {
                        "S": json.dumps(decision.to_dict(), default=str)
                    },
                    "created_at": {"S": decision.timestamp.isoformat()},
                    "updated_at": {"S": decision.timestamp.isoformat()},
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    async def _mark_risk_decision_published(
        self,
        signal_id: str,
        risk_decision_id: str,
    ) -> None:
        """Mark a risk decision reservation after Kafka acknowledges delivery."""
        if self._dynamo is None:
            raise RuntimeError("DynamoDB client unavailable for risk decision publish mark")

        now = utc_now().isoformat()
        await asyncio.to_thread(
            self._dynamo.update_item,
            TableName=self._settings.aws.dynamodb_table_risk_state,
            Key=risk_decision_key(signal_id),
            UpdateExpression=(
                "SET publish_status = :published, "
                "published_at = :now, updated_at = :now"
            ),
            ConditionExpression="attribute_exists(PK) AND risk_decision_id = :risk_id",
            ExpressionAttributeValues={
                ":published": {"S": _RISK_DECISION_PUBLISH_PUBLISHED},
                ":now": {"S": now},
                ":risk_id": {"S": risk_decision_id},
            },
        )

    # ── Audit log ─────────────────────────────────────────────────────────────

    async def _log_decision(self, decision: RiskDecision) -> None:
        """
        Persist a risk decision to S3 for audit.

        Key: risk-audit/{YYYY-MM-DD}/{risk_decision_id}.json
        """
        if self._s3 is None:
            logger.debug("No S3 client — risk decision not persisted to audit log")
            return

        try:
            today = utc_now().strftime("%Y-%m-%d")
            key = f"risk-audit/{today}/{decision.risk_decision_id}.json"
            body = json.dumps(decision.to_dict(), default=str)

            await asyncio.to_thread(
                self._s3.put_object,
                Bucket=self._settings.aws.s3_bucket,
                Key=key,
                Body=body.encode("utf-8"),
                ContentType="application/json",
            )
        except Exception:
            logger.exception(
                "Failed to persist risk decision %s to S3",
                decision.risk_decision_id,
            )


async def main() -> None:
    """Entry point for the Risk Engine Service."""
    import boto3
    from shared.aws.clients import get_dynamodb_client, get_s3_client

    settings = get_settings()
    sns = boto3.client("sns", region_name=settings.aws.region)
    topic_arn = os.environ.get(
        "SNS_KILL_SWITCH_TOPIC_ARN",
        getattr(settings.aws, "sns_kill_switch_topic_arn", ""),
    )

    service = RiskEngineService(
        dynamo_client=get_dynamodb_client(),
        s3_client=get_s3_client(),
        sns_client=sns,
        sns_topic_arn=topic_arn,
    )
    await service.start()


if __name__ == "__main__":
    asyncio.run(main())
