"""
Kafka Order Events Consumer — consumes ORDER_FILLED / ORDER_REJECTED / ORDER_PARTIAL
events for the risk engine's real-time P&L and position state.

Consumes from the ``orders.events`` MSK Serverless topic using IAM
authentication (SASL/OAUTHBEARER on port 9098).

Consumer group: ``risk-v1``

The execution engine publishes to orders.events after every broker fill or
rejection.  The risk engine consumes these events to:
    - Update real-time realized P&L in DailyLossValidator.
    - Free reserved position slots in PositionValidator (partial/rejected orders).
    - Trigger NAV recalculation (portfolio_value update in RiskLimits).

Partition offset handling:
    auto.offset.reset=latest — the risk engine does NOT need to replay historical
    fills on startup.  Position state is rehydrated from DynamoDB directly.  We
    only want fills that arrive *after* this instance started so we don't double-
    count P&L on restart.

    enable.auto.commit=False. Offsets are committed synchronously only after
    P&L/position handling succeeds or the event is routed to retry/DLQ.

Event types consumed:
    ORDER_FILLED   — full fill; update P&L + close position slot.
    ORDER_PARTIAL  — partial fill; update P&L; keep position slot open.
    ORDER_REJECTED — broker rejected; release reserved position slot; no P&L.

v3.0 schema validation:
    Messages with schema_version != "3.0" are logged and skipped.

Usage in service.py:
    consumer = KafkaOrderEventsConsumer(
        bootstrap_servers=kafka_bootstrap,
        aws_region=settings.aws.region,
    )
    await consumer.start()

    # Inside async processing loop (asyncio.to_thread wrapper):
    fill_event = await asyncio.to_thread(consumer.poll_fill)
    if fill_event is not None:
        await risk_service.handle_fill(fill_event)

    await consumer.stop()
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from shared.events.schemas import (
    CANONICAL_ORDER_EVENTS_TOPIC,
    EventType,
    validate_event,
)
from shared.kafka.failure_publisher import KafkaFailurePublisher
from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="risk_engine")

# ── Import guard ──────────────────────────────────────────────────────────────
try:
    from confluent_kafka import Consumer, KafkaError
    _CONFLUENT_AVAILABLE = True
except ImportError:
    _CONFLUENT_AVAILABLE = False
    logger.warning(
        "confluent-kafka not installed — KafkaOrderEventsConsumer will not function. "
        "Install: pip install confluent-kafka~=2.3"
    )

from shared.kafka.config import get_kafka_auth_config

# Topic and consumer group
_TOPIC = CANONICAL_ORDER_EVENTS_TOPIC
_CONSUMER_GROUP = "risk-v1"


@dataclass(frozen=True)
class OrderFillEvent:
    """
    Parsed ORDER_FILLED / ORDER_PARTIAL / ORDER_REJECTED event from Kafka.

    All three event types are returned via poll_fill() so the risk engine can
    act on fills *and* releases (rejected orders free position capacity).

    Attributes:
        event_type:        "ORDER_FILLED" | "ORDER_PARTIAL" | "ORDER_REJECTED"
        order_id:          Internal order ID (DynamoDB key).
        signal_id:         Originating signal ID (deterministic sha256).
        risk_decision_id:  Risk approval record that authorised the order.
        trace_id:          Propagated from originating TICK event.
        instrument_id:     "{MARKET}:{SYMBOL}" e.g. "NSE:RELIANCE"
        market:            "NSE" | "US"
        symbol:            Trading symbol without market prefix.
        direction:         "BUY" | "SELL"
        quantity_ordered:  Original order quantity.
        quantity_filled:   Actual filled quantity (0 for REJECTED).
        avg_fill_price:    VWAP fill price (None for REJECTED).
        broker_order_id:   Broker-assigned order reference.
        fill_time:         Fill timestamp in UTC (None for REJECTED).
        reject_reason:     Broker rejection message (None for fills).
        raw_topic:         Kafka topic (for logging).
        raw_partition:     Kafka partition number (for fill deduplication key).
        raw_offset:        Kafka partition offset (for logging).
    """

    event_type: str
    order_id: str
    signal_id: str
    risk_decision_id: str
    trace_id: str
    strategy_id: str
    instrument_id: str
    market: str
    symbol: str
    direction: str
    quantity_ordered: int
    quantity_filled: int
    avg_fill_price: Optional[float]
    broker_order_id: str
    product_type: str
    expires_at: Optional[datetime]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    fill_time: Optional[datetime]
    reject_reason: Optional[str]
    raw_topic: str
    raw_partition: int
    raw_offset: int
    raw_message: Any

    @property
    def is_fill(self) -> bool:
        """True for ORDER_FILLED and ORDER_PARTIAL events."""
        return self.event_type in ("ORDER_FILLED", "ORDER_PARTIAL")

    @property
    def is_full_fill(self) -> bool:
        """True only for ORDER_FILLED (complete quantity executed)."""
        return self.event_type == "ORDER_FILLED"

    @property
    def is_rejected(self) -> bool:
        """True for ORDER_REJECTED events."""
        return self.event_type == "ORDER_REJECTED"

    @property
    def notional_value(self) -> float:
        """Approximate notional value of the filled portion (0.0 if rejected)."""
        if self.avg_fill_price is None or self.quantity_filled == 0:
            return 0.0
        return self.avg_fill_price * self.quantity_filled


class KafkaOrderEventsConsumer:
    """
    Consumer for the orders.events Kafka topic (risk engine view).

    Consumes ORDER_FILLED / ORDER_PARTIAL / ORDER_REJECTED events published
    by the execution engine.  The risk engine uses these events to maintain
    real-time P&L, update the daily loss counter, and free reserved position
    slots for partial/rejected orders.

    Consumer group: ``risk-v1`` — shares offset tracking with the signal
    consumer so a single consumer-group rebalance covers both topics.

    Threading model:
        poll_fill() is synchronous. Call via asyncio.to_thread() in the
        risk engine's async processing loop.

    Offset reset policy: ``latest``
        On first-ever start (or after a long outage), the risk engine
        rehydrates position state from DynamoDB rather than replaying fills.
        Starting from ``latest`` avoids double-counting historic P&L.

    Idempotency:
        DailyLossValidator uses DynamoDB conditional updates keyed on
        order_id so a replayed fill after a crash is safely ignored.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        aws_region: str = "ap-south-1",
        consumer_group: str = _CONSUMER_GROUP,
        poll_timeout_seconds: float = 1.0,
    ) -> None:
        """
        Initialize the order events consumer.

        Args:
            bootstrap_servers: MSK bootstrap broker string (port 9098).
            aws_region: AWS region for IAM token generation.
            consumer_group: Kafka consumer group ID. Defaults to "risk-v1".
            poll_timeout_seconds: Blocking poll timeout. Keep ≤ 2s.

        Raises:
            RuntimeError: If confluent-kafka or aws-msk-iam-sasl-signer-python
                          are not installed.
        """
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError(
                "confluent-kafka is required for KafkaOrderEventsConsumer. "
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
        """Create the Kafka consumer and subscribe to orders.events."""
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
            "KafkaOrderEventsConsumer started (group=%s, topic=%s)",
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
            logger.info(
                "KafkaOrderEventsConsumer stopped (group=%s)", self._consumer_group
            )

    # ── Public interface ──────────────────────────────────────────────────────

    def poll_fill(self) -> Optional[OrderFillEvent]:
        """
        Synchronous: poll the broker for one ORDER_FILLED / ORDER_PARTIAL /
        ORDER_REJECTED message.

        Blocks for up to ``poll_timeout_seconds``. Returns an OrderFillEvent on
        success, None on timeout, error, or shutdown.

        IMPORTANT: This method is synchronous and MUST be called via
        asyncio.to_thread() in async code.

        Returns:
            OrderFillEvent on a valid v3.0 order event message.
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

    def commit(self, event: OrderFillEvent) -> None:
        """Synchronously commit a successfully processed order event offset."""
        self.commit_message(event.raw_message)

    def commit_message(self, msg: Any) -> None:
        """Synchronously commit a raw Kafka message offset."""
        if self._consumer is None:
            return
        self._consumer.commit(message=msg, asynchronous=False)

    def publish_retry(
        self,
        event: OrderFillEvent,
        *,
        reason: str,
        error_type: str = "processing_failed",
        details: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Route a processing-failed order event to orders.events.retry."""
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

    # ── Message parsing ───────────────────────────────────────────────────────

    def _parse_message(self, msg) -> Optional[OrderFillEvent]:
        """
        Parse a raw confluent-kafka Message into an OrderFillEvent.

        Validates schema_version is "3.0" and event_type is one of the
        three recognised order event types.  Returns None and logs a
        warning on any parse error rather than raising.
        """
        try:
            body = json.loads(msg.value().decode("utf-8"))

            event_type = body.get("event_type")
            if event_type not in ("ORDER_FILLED", "ORDER_PARTIAL", "ORDER_REJECTED"):
                self._last_failure_routed = self._publish_message_to_dlq(
                    msg,
                    reason=f"Unexpected event_type {event_type!r}",
                    error_type="schema_validation_failed",
                )
                return None

            errors = validate_event(body, event_type)
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

            # fill_time may be null for rejected orders
            fill_time: Optional[datetime] = None
            raw_fill_time = body.get("fill_time")
            if raw_fill_time:
                fill_time = datetime.fromisoformat(raw_fill_time)
                if fill_time.tzinfo is None:
                    fill_time = fill_time.replace(tzinfo=timezone.utc)

            # avg_fill_price may be null for rejected orders
            raw_price = body.get("avg_fill_price")
            avg_fill_price: Optional[float] = float(raw_price) if raw_price is not None else None
            expires_at: Optional[datetime] = None
            raw_expires_at = body.get("expires_at")
            if raw_expires_at:
                expires_at = datetime.fromisoformat(raw_expires_at)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)

            return OrderFillEvent(
                event_type=event_type,
                order_id=body["order_id"],
                signal_id=body["signal_id"],
                risk_decision_id=body["risk_decision_id"],
                trace_id=body.get("trace_id", ""),
                strategy_id=body["strategy_id"],
                instrument_id=instrument_id,
                market=market,
                symbol=symbol,
                direction=body["direction"],
                quantity_ordered=int(body["quantity_ordered"]),
                quantity_filled=int(body.get("quantity_filled", 0)),
                avg_fill_price=avg_fill_price,
                broker_order_id=body.get("broker_order_id", ""),
                product_type=body["product_type"],
                expires_at=expires_at,
                stop_loss=body.get("stop_loss"),
                take_profit=body.get("take_profit"),
                fill_time=fill_time,
                reject_reason=body.get("reject_reason"),
                raw_topic=msg.topic() or _TOPIC,
                raw_partition=msg.partition() if hasattr(msg, "partition") and msg.partition() is not None else 0,
                raw_offset=msg.offset() or 0,
                raw_message=msg,
            )

        except (KeyError, ValueError, json.JSONDecodeError, AttributeError) as exc:
            logger.exception(
                "Malformed order event message on topic %s (offset=%s) — skipping",
                msg.topic() or "?",
                msg.offset() if msg.offset() is not None else "?",
            )
            self._last_failure_routed = self._publish_message_to_dlq(
                msg,
                reason=str(exc),
                error_type="malformed_order_event",
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
            # Start from latest — position state is rehydrated from DynamoDB,
            # not from replaying the orders.events log.
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
