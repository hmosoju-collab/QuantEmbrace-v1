"""
KafkaEnrichedConsumer — risk_engine consumer for signals.enriched (v4.0).

Parses SIGNAL_ENRICHED events (v4.0) from the ``signals.enriched`` topic.
Falls back to parsing SIGNAL_PENDING (v3.0) with enrichment defaults when
the ``EnrichmentWatchdog`` switches the topic back to ``signals.pending``.

Consumer group: ``risk-v1`` (same group, different topic)
  The risk_engine uses a SINGLE consumer group offset cursor regardless of
  which topic it reads from.  When switching from signals.enriched to
  signals.pending, the cursor reset is handled by the service layer
  (re-subscribing the consumer to the new topic).

Dual-schema parsing (Phase 6 migration support):
  - schema_version="4.0", event_type="SIGNAL_ENRICHED"  →  full v4.0 parse
  - schema_version="3.0", event_type="SIGNAL_PENDING"   →  v3.0 parse with
    enrichment fields defaulted (regime="unknown", quality_score=0.5, filtered=False)

This allows rolling deployment: risk_engine can switch between topics
without a code deployment.

Phase 6 design (ADR-014 §5.7 + §5.9).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from shared.events.schemas import (
    CANONICAL_SIGNALS_ENRICHED_TOPIC,
    CANONICAL_SIGNALS_PENDING_TOPIC,
    ENRICHED_SCHEMA_VERSION,
    SCHEMA_VERSION,
    EventType,
    validate_event,
)
from shared.kafka.failure_publisher import KafkaFailurePublisher
from shared.logging.logger import get_logger
from shared.models.enriched_signal import EnrichedSignal, degraded_enrichment
from shared.models.signal import Direction, Signal, SignalStatus

logger = get_logger(__name__, service_name="risk_engine")

_CONSUMER_GROUP = "risk-v1"

# ── Import guards ─────────────────────────────────────────────────────────────
try:
    from confluent_kafka import Consumer, KafkaError
    _CONFLUENT_AVAILABLE = True
except ImportError:
    _CONFLUENT_AVAILABLE = False
    logger.warning("confluent-kafka not installed — KafkaEnrichedConsumer unavailable.")

from shared.kafka.config import get_kafka_auth_config


@dataclass
class EnrichedSignalEvent:
    """
    Parsed event — wraps EnrichedSignal with Kafka routing metadata.

    Used by RiskEngineService.  The enriched fields are advisory; the
    risk engine reads ``filtered`` and ``quality_score`` for soft-filter
    decisions, and ``regime`` for execution sizing hints.
    """

    enriched:    EnrichedSignal  # Full enriched signal (v4.0) or fallback with defaults
    trace_id:    str             # Propagated from originating TICK
    strategy_id: str             # Stable strategy identifier
    product_type: str            # Broker product type
    expires_at:  datetime        # Hard expiry
    raw_topic:   str             # Topic this arrived on (signals.enriched or signals.pending)
    raw_offset:  int
    raw_message: Any             # Original confluent-kafka Message for commit
    schema_version: str          # "4.0" or "3.0"


class KafkaEnrichedConsumer:
    """
    Risk-engine consumer for signals.enriched (primary) or signals.pending (fallback).

    Handles both v4.0 and v3.0 schemas transparently.  The active topic
    is set via ``subscribe_to(topic)`` — called by RiskEngineService when
    EnrichmentWatchdog changes state.

    Args:
        bootstrap_servers:    MSK bootstrap broker string (port 9098).
        aws_region:           AWS region for IAM token generation.
        initial_topic:        Starting topic (default: signals.enriched).
        poll_timeout_seconds: Blocking poll timeout. Keep ≤ 2s.
    """

    def __init__(
        self,
        bootstrap_servers:    str,
        aws_region:           str = "ap-south-1",
        initial_topic:        str = CANONICAL_SIGNALS_ENRICHED_TOPIC,
        poll_timeout_seconds: float = 1.0,
    ) -> None:
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError("confluent-kafka is required.")
        self._bootstrap_servers = bootstrap_servers
        self._aws_region        = aws_region
        self._poll_timeout      = poll_timeout_seconds
        self._current_topic     = initial_topic
        self._consumer:          Optional["Consumer"]        = None
        self._failure_publisher: Optional[KafkaFailurePublisher] = None
        self._running:           bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Create consumer, start failure publisher, and subscribe to the initial topic."""
        self._failure_publisher = KafkaFailurePublisher(
            bootstrap_servers=self._bootstrap_servers,
            aws_region=self._aws_region,
            source_service="risk_engine",
        )
        self._failure_publisher.start()
        self._consumer = self._build_consumer()
        self._consumer.subscribe([self._current_topic])
        self._running = True
        logger.info(
            "risk_engine.KafkaEnrichedConsumer.started",
            group=_CONSUMER_GROUP,
            topic=self._current_topic,
        )

    async def stop(self) -> None:
        """Close consumer and failure publisher, committing final offsets."""
        self._running = False
        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None
        if self._failure_publisher is not None:
            self._failure_publisher.close()
            self._failure_publisher = None
        logger.info("risk_engine.KafkaEnrichedConsumer.stopped")

    def subscribe_to(self, topic: str) -> None:
        """
        Switch the consumer to a different topic.

        Called by RiskEngineService when EnrichmentWatchdog toggles state.
        Safe to call from async context — consumer is single-threaded.
        """
        if self._consumer is None or topic == self._current_topic:
            return
        logger.info(
            "risk_engine.KafkaEnrichedConsumer.topic_switch",
            from_topic=self._current_topic,
            to_topic=topic,
        )
        self._current_topic = topic
        self._consumer.subscribe([topic])

    # ── Public interface ──────────────────────────────────────────────────────

    def poll_signal(self) -> Optional[EnrichedSignalEvent]:
        """
        Synchronous poll — MUST be called via asyncio.to_thread().

        Returns EnrichedSignalEvent on success, None on timeout/error.
        Parses both v4.0 (signals.enriched) and v3.0 (signals.pending fallback).
        """
        if not self._consumer or not self._running:
            return None

        msg = self._consumer.poll(self._poll_timeout)
        if msg is None:
            return None

        if msg.error():
            err = msg.error()
            if err.code() == KafkaError._PARTITION_EOF:
                return None
            logger.error("risk_engine.kafka_enriched_consumer_error", error=str(err))
            return None

        return self._parse_message(msg)

    def commit(self, event: EnrichedSignalEvent) -> None:
        """Commit offset for a successfully validated signal."""
        self.commit_message(event.raw_message)

    def commit_message(self, msg: Any) -> None:
        if self._consumer is not None:
            self._consumer.commit(message=msg, asynchronous=False)

    def publish_retry(
        self,
        event: EnrichedSignalEvent,
        *,
        reason: str,
        error_type: str = "processing_failed",
        details: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        Route a transiently-failed enriched signal to its ``.retry`` topic.

        On the first call the failure publisher routes to ``signals.enriched.retry``
        (or ``signals.pending.retry`` in fallback mode).  After
        ``max_retry_attempts`` (default 3) retries the publisher escalates to DLQ
        automatically.  Returns True if the publish succeeded.

        Called by RiskEngineService._enriched_processing_loop() on processing errors.
        """
        if self._failure_publisher is None:
            return False
        msg = event.raw_message
        return self._failure_publisher.publish_retry(
            source_topic=event.raw_topic,
            key=msg.key() if msg else None,
            value=msg.value() if msg else None,
            reason=reason,
            error_type=error_type,
            details=details,
            headers=msg.headers() if msg and hasattr(msg, "headers") else None,
        )

    def publish_dlq(
        self,
        event: EnrichedSignalEvent,
        *,
        reason: str,
        error_type: str,
        details: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        Route a stale or unrecoverable enriched signal to its DLQ topic.

        Used for signals that are past ``expires_at`` or have exceeded the
        maximum retry budget.  Returns True if the publish succeeded.

        Called by RiskEngineService._enriched_processing_loop() on expiry or
        unrecoverable validation failures.
        """
        if self._failure_publisher is None:
            return False
        msg = event.raw_message
        return self._failure_publisher.publish_dlq(
            source_topic=event.raw_topic,
            key=msg.key() if msg else None,
            value=msg.value() if msg else None,
            reason=reason,
            error_type=error_type,
            details=details,
        )

    @property
    def current_topic(self) -> str:
        return self._current_topic

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse_message(self, msg: Any) -> Optional[EnrichedSignalEvent]:
        """
        Parse a Kafka message handling both v4.0 and v3.0 schemas.

        Returns None and commits offset on unrecoverable errors.
        """
        try:
            body = json.loads(msg.value().decode("utf-8"))
            schema_version = body.get("schema_version", "")

            if schema_version == ENRICHED_SCHEMA_VERSION:
                return self._parse_v4(body, msg)
            elif schema_version == SCHEMA_VERSION:
                return self._parse_v3_fallback(body, msg)
            else:
                logger.warning(
                    "risk_engine.enriched_consumer.unknown_schema",
                    schema_version=schema_version,
                    offset=msg.offset(),
                )
                self.commit_message(msg)
                return None

        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.error(
                "risk_engine.enriched_consumer.parse_failed",
                offset=msg.offset() if msg.offset() is not None else "?",
                error=str(exc),
            )
            self.commit_message(msg)
            return None

    def _parse_v4(self, body: dict[str, Any], msg: Any) -> Optional[EnrichedSignalEvent]:
        """Parse v4.0 SIGNAL_ENRICHED message."""
        errors = validate_event(body, EventType.SIGNAL_ENRICHED)
        if errors:
            logger.warning(
                "risk_engine.enriched_consumer.v4_schema_error",
                errors=errors,
                offset=msg.offset(),
            )
            self.commit_message(msg)
            return None

        enriched = EnrichedSignal.from_dict(body)
        return EnrichedSignalEvent(
            enriched       = enriched,
            trace_id       = body.get("trace_id", ""),
            strategy_id    = body.get("strategy_id", enriched.strategy_name),
            product_type   = body.get("product_type", "MIS"),
            expires_at     = _parse_dt(body.get("expires_at")),
            raw_topic      = msg.topic() or CANONICAL_SIGNALS_ENRICHED_TOPIC,
            raw_offset     = msg.offset() or 0,
            raw_message    = msg,
            schema_version = ENRICHED_SCHEMA_VERSION,
        )

    def _parse_v3_fallback(self, body: dict[str, Any], msg: Any) -> Optional[EnrichedSignalEvent]:
        """
        Parse v3.0 SIGNAL_PENDING as a fallback enriched event.

        Enrichment fields default to: regime="unknown", quality_score=0.5, filtered=False.
        This path is used when EnrichmentWatchdog has activated fallback mode.
        """
        errors = validate_event(body, EventType.SIGNAL_PENDING)
        if errors:
            logger.warning(
                "risk_engine.enriched_consumer.v3_schema_error",
                errors=errors,
            )
            self.commit_message(msg)
            return None

        instrument_id = body["instrument_id"]
        market, symbol = instrument_id.split(":", 1)
        signal_time = _parse_dt(body["signal_time"])
        expires_at  = _parse_dt(body["expires_at"])

        metadata = dict(body.get("metadata") or {})
        metadata["strategy_id"]  = body["strategy_id"]
        metadata["product_type"] = body["product_type"]
        metadata["expires_at"]   = body["expires_at"]

        signal = Signal(
            signal_id       = body["signal_id"],
            strategy_name   = body["strategy_name"],
            symbol          = symbol,
            market          = market,
            direction       = Direction(body["direction"]),
            quantity        = int(body["quantity"]),
            price_at_signal = float(body.get("price_at_signal", 0.0)),
            confidence      = float(body.get("confidence", 1.0)),
            generated_at    = signal_time,
            status          = SignalStatus.PENDING,
            stop_loss       = body.get("stop_loss"),
            take_profit     = body.get("take_profit"),
            paper_trade     = bool(body.get("paper_trade", False)),
            metadata        = metadata,
        )

        enriched = degraded_enrichment(
            signal,
            trace_id     = body.get("trace_id", ""),
            strategy_id  = body["strategy_id"],
            product_type = body["product_type"],
            expires_at   = expires_at,
        )

        return EnrichedSignalEvent(
            enriched       = enriched,
            trace_id       = body.get("trace_id", ""),
            strategy_id    = body["strategy_id"],
            product_type   = body["product_type"],
            expires_at     = expires_at,
            raw_topic      = msg.topic() or CANONICAL_SIGNALS_PENDING_TOPIC,
            raw_offset     = msg.offset() or 0,
            raw_message    = msg,
            schema_version = SCHEMA_VERSION,
        )

    # ── Consumer construction ─────────────────────────────────────────────────

    def _build_consumer(self) -> "Consumer":
        region = self._aws_region
        return Consumer({
            "bootstrap.servers":                  self._bootstrap_servers,
            **get_kafka_auth_config(region),
            "group.id":                           _CONSUMER_GROUP,
            "auto.offset.reset":                  "latest",
            "enable.auto.commit":                 False,
            "enable.auto.offset.store":           False,
            "session.timeout.ms":                 30000,
            "heartbeat.interval.ms":              10000,
            "max.poll.interval.ms":               300000,
            "fetch.min.bytes":                    1,
            "fetch.wait.max.ms":                  500,
            "socket.connection.setup.timeout.ms": 15000,
            "log.connection.close":               False,
        })


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        return datetime.now(timezone.utc)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
