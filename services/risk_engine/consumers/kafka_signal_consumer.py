"""
Kafka Signal Consumer — consumes SIGNAL_PENDING events for the risk engine.

Consumes from the ``signals.pending`` MSK Serverless topic using IAM
authentication (SASL/OAUTHBEARER on port 9098).

Consumer group: ``risk-v1``

Offsets are committed manually only after the risk decision and any retry/DLQ routing are durable. On restart, Kafka redelivers any uncommitted signal.

Signal fields consumed from v3.0 SIGNAL_PENDING schema:
    signal_id      → deterministic sha256 hash (deduplication key)
    trace_id       → propagated unchanged through risk → execution → fill
    instrument_id  → "{MARKET}:{SYMBOL}" e.g. "NSE:RELIANCE"
    strategy_name  → originating strategy identifier
    direction      → "BUY" | "SELL"
    quantity       → order quantity
    price_at_signal → price when signal was generated
    signal_time    → ISO-8601 UTC (used by SignalAgeValidator)
    expires_at     → ISO-8601 UTC (signal_time + 30s)
    confidence     → float 0.0–1.0

Usage in service.py:
    consumer = KafkaSignalConsumer(
        bootstrap_servers=kafka_bootstrap,
        aws_region=settings.aws.region,
    )
    await consumer.start()

    # Inside async processing loop (asyncio.to_thread wrapper):
    signal_event = await asyncio.to_thread(consumer.poll_signal)
    if signal_event is not None:
        decision = await risk_service.validate_signal(signal_event.signal)

    await consumer.stop()
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from shared.events.schemas import (
    CANONICAL_SIGNALS_PENDING_TOPIC,
    EventType,
    SCHEMA_VERSION,
    validate_event,
)
from shared.kafka.failure_publisher import KafkaFailurePublisher
from shared.logging.logger import get_logger
from shared.models.signal import Direction, Signal, SignalStatus

logger = get_logger(__name__, service_name="risk_engine")

# ── Import guard ──────────────────────────────────────────────────────────────
try:
    from confluent_kafka import Consumer, KafkaError
    _CONFLUENT_AVAILABLE = True
except ImportError:
    _CONFLUENT_AVAILABLE = False
    logger.warning(
        "confluent-kafka not installed — KafkaSignalConsumer will not function. "
        "Install: pip install confluent-kafka~=2.3"
    )

from shared.kafka.config import get_kafka_auth_config

# Topic this consumer subscribes to
_TOPIC = CANONICAL_SIGNALS_PENDING_TOPIC

# Consumer group — independent offset tracking from execution-v1
_CONSUMER_GROUP = "risk-v1"


@dataclass
class SignalEvent:
    """
    Parsed SIGNAL_PENDING event from Kafka.

    Wraps the Signal model with Kafka-envelope fields (trace_id) needed
    for propagation into the SIGNAL_APPROVED event.
    """

    signal: Signal        # Fully populated Signal model
    trace_id: str         # Propagated from originating TICK event
    strategy_id: str      # Stable strategy identifier used for limits/audit
    product_type: str     # Broker product type requested by strategy/risk
    expires_at: datetime  # Hard expiry from strategy_engine
    raw_topic: str        # Kafka topic this arrived on (for logging)
    raw_offset: int       # Kafka offset (for logging)
    raw_message: Any      # Original confluent-kafka Message for manual commit


class KafkaSignalConsumer:
    """
    Consumer for the signals.pending Kafka topic.

    Consumer group ``risk-v1`` gives the risk engine its own independent
    offset cursor. The strategy engine produces to ``signals.pending``; the
    risk engine consumes, validates, and approves/rejects each signal.

    Threading model:
        poll_signal() is synchronous (confluent-kafka Consumer.poll() is
        blocking). It must be called via asyncio.to_thread() in the risk
        engine's async processing loop.

    Offset management:
        enable.auto.commit=False. Offsets are committed synchronously only after
        validation, audit logging, approved-event enqueue, or retry/DLQ routing
        succeeds.

    v3.0 schema validation:
        Messages with schema_version != "3.0" are logged and skipped.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        aws_region: str = "ap-south-1",
        consumer_group: str = _CONSUMER_GROUP,
        poll_timeout_seconds: float = 1.0,
    ) -> None:
        """
        Initialize the Kafka signal consumer.

        Args:
            bootstrap_servers: MSK bootstrap broker string (port 9098).
            aws_region: AWS region for IAM token generation.
            consumer_group: Kafka consumer group ID. Defaults to "risk-v1".
            poll_timeout_seconds: Blocking poll timeout. Keep ≤ 2s.
        """
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError(
                "confluent-kafka is required for KafkaSignalConsumer. "
                "Install: pip install confluent-kafka~=2.3"
            )
        self._bootstrap_servers = bootstrap_servers
        self._aws_region = aws_region
        self._consumer_group = consumer_group
        self._poll_timeout = poll_timeout_seconds
        self._consumer: Optional["Consumer"] = None
        self._failure_publisher: Optional[KafkaFailurePublisher] = None
        self._running: bool = False
        self._last_failure_routed: bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Create the Kafka consumer and subscribe to signals.pending."""
        self._consumer = self._build_consumer()
        self._failure_publisher = KafkaFailurePublisher(
            bootstrap_servers=self._bootstrap_servers,
            aws_region=self._aws_region,
            source_service="risk_engine",
        )
        self._failure_publisher.start()
        self._consumer.subscribe([_TOPIC])
        self._running = True
        logger.info(
            "KafkaSignalConsumer started (group=%s, topic=%s)",
            self._consumer_group,
            _TOPIC,
        )

    async def stop(self) -> None:
        """Close the consumer, committing final offsets to the broker."""
        self._running = False
        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None
        if self._failure_publisher is not None:
            self._failure_publisher.close()
            self._failure_publisher = None
            logger.info("KafkaSignalConsumer stopped (group=%s)", self._consumer_group)

    # ── Public interface ──────────────────────────────────────────────────────

    def poll_signal(self) -> Optional[SignalEvent]:
        """
        Synchronous: poll the broker for one SIGNAL_PENDING message.

        Blocks for up to ``poll_timeout_seconds``. Returns a SignalEvent on
        success, None on timeout, error, or shutdown.

        IMPORTANT: This method is synchronous and MUST be called via
        asyncio.to_thread() in async code.

        Returns:
            SignalEvent if a valid v3.0 SIGNAL_PENDING message was received.
            None on timeout, end-of-partition, or parse errors (logged).
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
            logger.error(
                "Kafka consumer error on topic %s: %s",
                msg.topic() or "?",
                err,
            )
            return None

        self._last_failure_routed = False
        event = self._parse_message(msg)
        if event is None and self._last_failure_routed:
            self.commit_message(msg)
        return event

    def commit(self, event: SignalEvent) -> None:
        """Synchronously commit a successfully processed signal offset."""
        self.commit_message(event.raw_message)

    def commit_message(self, msg: Any) -> None:
        """Synchronously commit a raw Kafka message offset."""
        if self._consumer is None:
            return
        self._consumer.commit(message=msg, asynchronous=False)

    def publish_retry(
        self,
        event: SignalEvent,
        *,
        reason: str,
        error_type: str = "processing_failed",
        details: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Route a processing-failed signal to signals.pending.retry."""
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
        event: SignalEvent,
        *,
        reason: str,
        error_type: str,
        details: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Route a stale or unrecoverable signal to signals.pending.dlq."""
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

    # ── Message parsing ───────────────────────────────────────────────────────

    def _parse_message(self, msg) -> Optional[SignalEvent]:
        """
        Parse a raw confluent-kafka Message into a SignalEvent.

        Validates schema_version is "3.0" and event_type is "SIGNAL_PENDING".
        Returns None and logs a warning on any parse error rather than raising.
        """
        try:
            body = json.loads(msg.value().decode("utf-8"))

            errors = validate_event(body, EventType.SIGNAL_PENDING)
            if errors:
                self._last_failure_routed = self._publish_message_to_dlq(
                    msg,
                    reason="; ".join(errors),
                    error_type="schema_validation_failed",
                )
                return None

            # instrument_id = "NSE:RELIANCE" or "US:AAPL"
            instrument_id: str = body["instrument_id"]
            market, symbol = instrument_id.split(":", 1)

            signal_time = datetime.fromisoformat(body["signal_time"])
            if signal_time.tzinfo is None:
                signal_time = signal_time.replace(tzinfo=timezone.utc)

            expires_at = datetime.fromisoformat(body["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)

            metadata = dict(body.get("metadata") or {})
            metadata["strategy_id"] = body["strategy_id"]
            metadata["product_type"] = body["product_type"]
            metadata["expires_at"] = body["expires_at"]

            signal = Signal(
                signal_id=body["signal_id"],
                strategy_name=body["strategy_name"],
                symbol=symbol,
                market=market,
                direction=Direction(body["direction"]),
                quantity=int(body["quantity"]),
                price_at_signal=float(body["price_at_signal"]),
                confidence=float(body.get("confidence", 1.0)),
                generated_at=signal_time,
                status=SignalStatus.PENDING,
                stop_loss=body.get("stop_loss"),
                take_profit=body.get("take_profit"),
                paper_trade=bool(body.get("paper_trade", False)),  # Phase 3 passthrough
                metadata=metadata,
            )

            return SignalEvent(
                signal=signal,
                trace_id=body.get("trace_id", ""),
                strategy_id=body["strategy_id"],
                product_type=body["product_type"],
                expires_at=expires_at,
                raw_topic=msg.topic() or _TOPIC,
                raw_offset=msg.offset() or 0,
                raw_message=msg,
            )

        except (KeyError, ValueError, json.JSONDecodeError, AttributeError) as exc:
            logger.exception(
                "Malformed SIGNAL_PENDING message on topic %s (offset=%s) — skipping",
                msg.topic() or "?",
                msg.offset() if msg.offset() is not None else "?",
            )
            self._last_failure_routed = self._publish_message_to_dlq(
                msg,
                reason=str(exc),
                error_type="malformed_signal_pending",
            )
            return None

    def _publish_message_to_dlq(self, msg: Any, *, reason: str, error_type: str) -> bool:
        if self._failure_publisher is None:
            return False
        return self._failure_publisher.publish_dlq(
            source_topic=msg.topic() or _TOPIC,
            key=msg.key() if msg.key() else None,
            value=msg.value() if msg.value() else None,
            reason=reason,
            error_type=error_type,
            details={
                "partition": msg.partition() if hasattr(msg, "partition") else None,
                "offset": msg.offset() if hasattr(msg, "offset") else None,
            },
        )

    # ── Consumer construction ─────────────────────────────────────────────────

    def _build_consumer(self) -> "Consumer":
        """Build a confluent-kafka Consumer for MSK Serverless with IAM auth."""
        region = self._aws_region
        return Consumer({
            "bootstrap.servers":                     self._bootstrap_servers,
            **get_kafka_auth_config(region),
            "group.id":                              self._consumer_group,
            "auto.offset.reset":                     "latest",
            "enable.auto.commit":                    False,
            "enable.auto.offset.store":              False,
            "session.timeout.ms":                    30000,
            "heartbeat.interval.ms":                 10000,
            "max.poll.interval.ms":                  300000,
            "fetch.min.bytes":                       1,
            "fetch.wait.max.ms":                     500,
            "socket.connection.setup.timeout.ms":    15000,
            "log.connection.close":                  False,
        })
