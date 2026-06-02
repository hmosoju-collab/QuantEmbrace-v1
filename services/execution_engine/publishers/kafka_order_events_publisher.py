"""
Kafka Order Events Publisher — publishes ORDER_FILLED / ORDER_REJECTED events
to the orders.events MSK Serverless topic.

The risk engine consumes orders.events (risk-v1) to update real-time P&L,
position state, and NAV after each fill.

Topic:  orders.events  (2 partitions, key = order_id)
Schema: v3.0 ORDER_FILLED / ORDER_REJECTED event

Event schema (schema_version 3.0 — frozen):
    {
        "event_id":          str (uuid4)
        "trace_id":          str (propagated from originating TICK event)
        "event_type":        "ORDER_FILLED" | "ORDER_REJECTED" | "ORDER_PARTIAL"
        "schema_version":    "3.0"
        "source":            "execution_engine"
        "published_time":    ISO8601 UTC
        "order_id":          str
        "signal_id":         str (sha256 deterministic)
        "risk_decision_id":  str (uuid4)
        "instrument_id":     "{MARKET}:{SYMBOL}"
        "market":            "NSE" | "US"
        "direction":         "BUY" | "SELL"
        "quantity_ordered":  int
        "quantity_filled":   int
        "avg_fill_price":    float | null
        "broker_order_id":   str (broker-assigned order ID)
        "fill_time":         ISO8601 UTC | null
        "reject_reason":     str | null
    }
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from shared.events.schemas import (
    CANONICAL_ORDER_EVENTS_TOPIC,
    EventType,
    SCHEMA_VERSION,
    validate_event,
)
from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="execution_engine")

# ── Import guard ──────────────────────────────────────────────────────────────
try:
    from confluent_kafka import Producer, KafkaException
    _CONFLUENT_AVAILABLE = True
except ImportError:
    _CONFLUENT_AVAILABLE = False
    logger.warning(
        "confluent-kafka not installed — KafkaOrderEventsPublisher will not function. "
        "Install: pip install confluent-kafka~=2.3"
    )

from shared.kafka.config import get_kafka_auth_config

_TOPIC = CANONICAL_ORDER_EVENTS_TOPIC
_SCHEMA_VERSION = SCHEMA_VERSION


class KafkaOrderEventsPublisher:
    """
    Async-compatible Kafka publisher for ORDER_FILLED / ORDER_REJECTED events.

    Publishes to orders.events (key = order_id) so the risk engine can
    consume fills per-instrument with partition ordering guarantees.

    Delivery semantics:
        acks=all + enable.idempotence=true ensures each order event is
        committed to all ISR replicas. A fill that fails delivery after all
        retries is logged as a critical error — the risk engine would not
        update P&L and position state.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        aws_region: str = "ap-south-1",
    ) -> None:
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError(
                "confluent-kafka is required for KafkaOrderEventsPublisher. "
                "Install: pip install confluent-kafka~=2.3"
            )
        self._bootstrap_servers = bootstrap_servers
        self._aws_region = aws_region
        self._producer: Optional["Producer"] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._pending_count: int = 0
        self._lock = threading.Lock()

    async def start(self) -> None:
        """Initialize the Kafka producer and start the delivery-receipt poller."""
        if self._running:
            return
        self._producer = self._build_producer()
        self._running = True
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="kafka_order_events_delivery_poll"
        )
        logger.info("KafkaOrderEventsPublisher started (topic=%s)", _TOPIC)

    async def stop(self) -> None:
        """Drain all in-flight deliveries and close the producer."""
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._producer is not None:
            remaining = self._producer.flush(timeout=10)
            if remaining > 0:
                logger.critical(
                    "KafkaOrderEventsPublisher.stop: %d order event(s) undelivered "
                    "after 10s flush — risk engine P&L state may be stale",
                    remaining,
                )
            self._producer = None
        logger.info("KafkaOrderEventsPublisher stopped")

    @property
    def pending_count(self) -> int:
        """Number of messages in the producer buffer awaiting delivery."""
        return self._pending_count

    async def publish_fill(
        self,
        order_id: str,
        signal_id: str,
        risk_decision_id: str,
        trace_id: str,
        symbol: str,
        market: str,
        direction: str,
        quantity_ordered: int,
        quantity_filled: int,
        avg_fill_price: float,
        broker_order_id: str,
        fill_time: Optional[datetime] = None,
        strategy_id: str = "",
        product_type: str = "DAY",
        expires_at: Optional[str] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        event_type: Optional[str] = None,
    ) -> None:
        """
        Publish an ORDER_FILLED event to orders.events.

        Args:
            order_id: Internal order ID (DynamoDB key).
            signal_id: Originating signal ID (deterministic sha256).
            risk_decision_id: Risk approval record ID.
            trace_id: Propagated trace ID from the originating tick.
            symbol: Trading symbol.
            market: "NSE" or "US".
            direction: "BUY" or "SELL".
            quantity_ordered: Original order quantity.
            quantity_filled: Actual filled quantity.
            avg_fill_price: Volume-weighted average fill price.
            broker_order_id: Broker-assigned order ID.
            fill_time: Fill timestamp (defaults to now if None).
        """
        resolved_event_type = event_type or (
            "ORDER_PARTIAL" if quantity_filled < quantity_ordered else "ORDER_FILLED"
        )
        event = {
            "event_id":         str(uuid.uuid4()),
            "trace_id":         trace_id or str(uuid.uuid4()),
            "event_type":       resolved_event_type,
            "schema_version":   _SCHEMA_VERSION,
            "source":           "execution_engine",
            "published_time":   datetime.now(timezone.utc).isoformat(),
            "order_id":         order_id,
            "signal_id":        signal_id,
            "risk_decision_id": risk_decision_id,
            "strategy_id":      strategy_id,
            "instrument_id":    f"{market}:{symbol}",
            "market":           market,
            "direction":        direction,
            "quantity_ordered": quantity_ordered,
            "quantity_filled":  quantity_filled,
            "avg_fill_price":   avg_fill_price,
            "broker_order_id":  broker_order_id,
            "product_type":     product_type,
            "expires_at":       expires_at,
            "stop_loss":        stop_loss,
            "take_profit":      take_profit,
            "fill_time":        (fill_time or datetime.now(timezone.utc)).isoformat(),
            "reject_reason":    None,
        }
        self._validate_event(event)
        await self._produce(order_id, event)

    async def publish_rejection(
        self,
        order_id: str,
        signal_id: str,
        risk_decision_id: str,
        trace_id: str,
        symbol: str,
        market: str,
        direction: str,
        quantity_ordered: int,
        broker_order_id: str,
        reject_reason: str,
        strategy_id: str = "",
        product_type: str = "DAY",
        expires_at: Optional[str] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> None:
        """Publish an ORDER_REJECTED event to orders.events."""
        event = {
            "event_id":         str(uuid.uuid4()),
            "trace_id":         trace_id or str(uuid.uuid4()),
            "event_type":       "ORDER_REJECTED",
            "schema_version":   _SCHEMA_VERSION,
            "source":           "execution_engine",
            "published_time":   datetime.now(timezone.utc).isoformat(),
            "order_id":         order_id,
            "signal_id":        signal_id,
            "risk_decision_id": risk_decision_id,
            "strategy_id":      strategy_id,
            "instrument_id":    f"{market}:{symbol}",
            "market":           market,
            "direction":        direction,
            "quantity_ordered": quantity_ordered,
            "quantity_filled":  0,
            "avg_fill_price":   None,
            "broker_order_id":  broker_order_id,
            "product_type":     product_type,
            "expires_at":       expires_at,
            "stop_loss":        stop_loss,
            "take_profit":      take_profit,
            "fill_time":        None,
            "reject_reason":    reject_reason,
        }
        self._validate_event(event)
        await self._produce(order_id, event)

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _produce(self, order_id: str, event: dict) -> None:
        """Enqueue an order event for delivery."""
        if self._producer is None:
            logger.error(
                "KafkaOrderEventsPublisher.publish called before start() — event dropped"
            )
            return

        key = order_id.encode("utf-8")
        value = json.dumps(event, default=str).encode("utf-8")

        try:
            with self._lock:
                self._pending_count += 1
            self._producer.produce(
                topic=_TOPIC,
                key=key,
                value=value,
                on_delivery=self._on_delivery,
            )
        except (KafkaException, BufferError) as exc:
            with self._lock:
                self._pending_count -= 1
            logger.exception(
                "Failed to enqueue order event (order_id=%s) to Kafka: %s",
                order_id,
                exc,
            )

    @staticmethod
    def _validate_event(event: dict) -> None:
        """Validate an ORDER_* event before enqueueing it."""
        errors = validate_event(event, event.get("event_type", EventType.ORDER_FILLED.value))
        if errors:
            raise ValueError(f"Invalid order event: {'; '.join(errors)}")

    def _on_delivery(self, err, msg) -> None:
        with self._lock:
            self._pending_count = max(0, self._pending_count - 1)

        if err is not None:
            logger.critical(
                "ORDER EVENT delivery FAILED (topic=%s, key=%s): %s — "
                "risk engine P&L/position state will not reflect this fill",
                msg.topic(),
                msg.key().decode("utf-8") if msg.key() else "?",
                err,
            )
        else:
            logger.debug(
                "ORDER EVENT delivered (topic=%s, partition=%d, offset=%d)",
                msg.topic(),
                msg.partition(),
                msg.offset(),
            )

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(0.1)
                if self._producer is not None:
                    self._producer.poll(0)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("KafkaOrderEventsPublisher poll loop error")

    def _build_producer(self) -> "Producer":
        region = self._aws_region
        return Producer({
            "bootstrap.servers":                     self._bootstrap_servers,
            **get_kafka_auth_config(region),
            "acks":                                  "all",
            "enable.idempotence":                    True,
            "max.in.flight.requests.per.connection": 1,
            "retries":                               5,
            "retry.backoff.ms":                      200,
            "compression.type":                      "lz4",
            "batch.size":                            4096,
            "linger.ms":                             5,
            "socket.connection.setup.timeout.ms":    15000,
        })
