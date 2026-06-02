"""
Kafka Approved Signal Consumer — consumes SIGNAL_APPROVED events for the
execution engine.

Consumes from the ``signals.approved`` MSK Serverless topic using IAM
authentication (SASL/OAUTHBEARER on port 9098).

Consumer group: ``execution-v1``

Signal fields consumed from v3.0 SIGNAL_APPROVED schema:
    signal_id        → DynamoDB idempotency key (prevents duplicate orders)
    risk_decision_id → links execution to the risk approval audit record
    trace_id         → propagated from TICK event through to ORDER_FILLED
    instrument_id    → "{MARKET}:{SYMBOL}" e.g. "NSE:RELIANCE"
    strategy_name    → originating strategy identifier
    direction        → "BUY" | "SELL"
    quantity         → order quantity
    price_at_signal  → indicative price (for LIMIT order reference)
    confidence       → float 0.0–1.0
    signal_time      → ISO-8601 UTC (used for age validation on execution side)
    approved_at      → ISO-8601 UTC (risk approval timestamp)

Usage in service.py:
    consumer = KafkaApprovedConsumer(
        bootstrap_servers=kafka_bootstrap,
        aws_region=settings.aws.region,
    )
    await consumer.start()

    # Inside async processing loop (asyncio.to_thread wrapper):
    approved = await asyncio.to_thread(consumer.poll_approved)
    if approved is not None:
        await service.execute_approved_signal(
            signal_id=approved.signal_id,
            risk_decision_id=approved.risk_decision_id,
            ...
        )

    await consumer.stop()
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from shared.events.schemas import (
    CANONICAL_SIGNALS_APPROVED_TOPIC,
    EventType,
    validate_event,
)
from shared.kafka.failure_publisher import KafkaFailurePublisher
from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="execution_engine")

# ── Import guard ──────────────────────────────────────────────────────────────
try:
    from confluent_kafka import Consumer, KafkaError
    _CONFLUENT_AVAILABLE = True
except ImportError:
    _CONFLUENT_AVAILABLE = False
    logger.warning(
        "confluent-kafka not installed — KafkaApprovedConsumer will not function. "
        "Install: pip install confluent-kafka~=2.3"
    )

from shared.kafka.config import get_kafka_auth_config

_TOPIC = CANONICAL_SIGNALS_APPROVED_TOPIC
_CONSUMER_GROUP = "execution-v1"


@dataclass
class ApprovedSignalEvent:
    """
    Parsed SIGNAL_APPROVED event from Kafka.

    Contains all fields needed by execute_approved_signal() plus the Kafka
    envelope fields for trace propagation into ORDER_FILLED events.
    """

    signal_id: str
    risk_decision_id: str
    trace_id: str
    strategy_id: str
    symbol: str
    market: str           # "NSE" | "US"
    strategy_name: str
    direction: str        # "BUY" | "SELL"
    quantity: float
    price_at_signal: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    product_type: str
    expires_at: datetime
    confidence: float
    signal_time: datetime
    approved_at: datetime
    paper_trade: bool     # Phase 3 — if True, route to _handle_paper_order() not live broker
    raw_topic: str
    raw_offset: int
    raw_message: Any


class KafkaApprovedConsumer:
    """
    Consumer for the signals.approved Kafka topic.

    Consumer group ``execution-v1`` gives the execution engine its own
    independent offset cursor. The risk engine produces approved signals to
    ``signals.approved``; the execution engine consumes and places broker orders.

    Threading model:
        poll_approved() is synchronous (confluent-kafka Consumer.poll() is
        blocking). It must be called via asyncio.to_thread() in the execution
        engine's async processing loop.

    Offset management:
        enable.auto.commit=False. Offsets are committed synchronously only after
        order reservation/execution succeeds or retry/DLQ routing is durable.
        Idempotency is enforced by DynamoDB conditional writes in OrderManager.

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
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError(
                "confluent-kafka is required for KafkaApprovedConsumer. "
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

    async def start(self) -> None:
        """Create the Kafka consumer and subscribe to signals.approved."""
        self._consumer = self._build_consumer()
        self._failure_publisher = KafkaFailurePublisher(
            bootstrap_servers=self._bootstrap_servers,
            aws_region=self._aws_region,
            source_service="execution_engine",
        )
        self._failure_publisher.start()
        self._consumer.subscribe([_TOPIC])
        self._running = True
        logger.info(
            "KafkaApprovedConsumer started (group=%s, topic=%s)",
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
            logger.info("KafkaApprovedConsumer stopped (group=%s)", self._consumer_group)

    def poll_approved(self) -> Optional[ApprovedSignalEvent]:
        """
        Synchronous: poll the broker for one SIGNAL_APPROVED message.

        IMPORTANT: Must be called via asyncio.to_thread() in async code.

        Returns:
            ApprovedSignalEvent if a valid v3.0 SIGNAL_APPROVED message was received.
            None on timeout, end-of-partition, or parse errors.
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

    def commit(self, event: ApprovedSignalEvent) -> None:
        """Synchronously commit a successfully reserved/processed approved signal."""
        self.commit_message(event.raw_message)

    def commit_message(self, msg: Any) -> None:
        """Synchronously commit a raw Kafka message offset."""
        if self._consumer is None:
            return
        self._consumer.commit(message=msg, asynchronous=False)

    def publish_retry(
        self,
        event: ApprovedSignalEvent,
        *,
        reason: str,
        error_type: str = "processing_failed",
        details: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Route a processing-failed approval to signals.approved.retry."""
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
        event: ApprovedSignalEvent,
        *,
        reason: str,
        error_type: str,
        details: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Route a stale or unrecoverable approval to signals.approved.dlq."""
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

    def _parse_message(self, msg) -> Optional[ApprovedSignalEvent]:
        """Parse a raw confluent-kafka Message into an ApprovedSignalEvent."""
        try:
            body = json.loads(msg.value().decode("utf-8"))

            errors = validate_event(body, EventType.SIGNAL_APPROVED)
            if errors:
                self._last_failure_routed = self._publish_message_to_dlq(
                    msg,
                    reason="; ".join(errors),
                    error_type="schema_validation_failed",
                )
                return None

            instrument_id: str = body["instrument_id"]
            market, symbol = instrument_id.split(":", 1)

            def parse_dt(val: str) -> datetime:
                dt = datetime.fromisoformat(val)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

            return ApprovedSignalEvent(
                signal_id=body["signal_id"],
                risk_decision_id=body["risk_decision_id"],
                trace_id=body.get("trace_id", ""),
                strategy_id=body["strategy_id"],
                symbol=symbol,
                market=market,
                strategy_name=body["strategy_name"],
                direction=body["direction"].upper(),
                quantity=float(body["quantity"]),
                price_at_signal=float(body["price_at_signal"]),
                stop_loss=body.get("stop_loss"),
                take_profit=body.get("take_profit"),
                product_type=body["product_type"],
                expires_at=parse_dt(body["expires_at"]),
                confidence=float(body.get("confidence", 1.0)),
                signal_time=parse_dt(body["signal_time"]),
                approved_at=parse_dt(body["approved_at"]),
                paper_trade=bool(body.get("paper_trade", False)),  # Phase 3 — safe default
                raw_topic=msg.topic() or _TOPIC,
                raw_offset=msg.offset() or 0,
                raw_message=msg,
            )

        except (KeyError, ValueError, json.JSONDecodeError, AttributeError) as exc:
            logger.exception(
                "Malformed SIGNAL_APPROVED message on topic %s (offset=%s) — skipping",
                msg.topic() or "?",
                msg.offset() if msg.offset() is not None else "?",
            )
            self._last_failure_routed = self._publish_message_to_dlq(
                msg,
                reason=str(exc),
                error_type="malformed_signal_approved",
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
