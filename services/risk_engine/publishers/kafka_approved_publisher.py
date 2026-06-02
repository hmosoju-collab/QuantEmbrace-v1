"""
Kafka Approved Signal Publisher — publishes SIGNAL_APPROVED events to the
execution engine via the signals.approved MSK Serverless topic.

Topic:  signals.approved  (2 partitions, key = instrument_id)
Schema: v3.0 SIGNAL_APPROVED event

Event schema (schema_version 3.0 — frozen):
    {
        "event_id":          str (uuid4)
        "trace_id":          str (propagated from originating TICK event)
        "event_type":        "SIGNAL_APPROVED"
        "schema_version":    "3.0"
        "source":            "risk_engine"
        "published_time":    ISO8601 UTC
        "signal_id":         str (sha256 deterministic — same as SIGNAL_PENDING)
        "risk_decision_id":  str (uuid4 — links approval to audit log entry)
        "instrument_id":     "{MARKET}:{SYMBOL}"
        "market":            "NSE" | "US"
        "strategy_name":     str
        "direction":         "BUY" | "SELL"
        "quantity":          int
        "price_at_signal":   float
        "confidence":        float
        "signal_time":       ISO8601 UTC (original signal generation time)
        "approved_at":       ISO8601 UTC
    }

Producer config: acks=all, enable.idempotence=true, max.in.flight=1
    retries=5, retry.backoff.ms=200, compression.type=lz4
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import threading
from typing import Callable, Optional
import uuid

from shared.events.schemas import (
    CANONICAL_KILL_SWITCH_TOPIC,
    CANONICAL_SIGNALS_APPROVED_TOPIC,
    SCHEMA_VERSION,
    EventType,
    validate_event,
)
from shared.kafka.local_outbox import LocalOutbox
from shared.logging.logger import get_logger
from shared.models.signal import Signal

logger = get_logger(__name__, service_name="risk_engine")

# ── Import guard ──────────────────────────────────────────────────────────────
try:
    from confluent_kafka import KafkaException, Producer
    _CONFLUENT_AVAILABLE = True
except ImportError:
    _CONFLUENT_AVAILABLE = False
    logger.warning(
        "confluent-kafka not installed — KafkaApprovedPublisher will not function. "
        "Install: pip install confluent-kafka~=2.3"
    )

from shared.kafka.config import get_kafka_auth_config

_TOPIC = CANONICAL_SIGNALS_APPROVED_TOPIC
_SCHEMA_VERSION = SCHEMA_VERSION

# Consecutive delivery failures before halt is triggered (Phase 8 / ADR-015)
_CONSECUTIVE_FAILURE_THRESHOLD = 3


class KafkaApprovedPublisher:
    """
    Async-compatible Kafka publisher for SIGNAL_APPROVED events.

    Delivery semantics:
        acks=all + enable.idempotence=true ensures each approved signal is
        committed to all ISR replicas before acknowledgement. A signal that
        fails delivery after all retries is logged as a critical error.

    Threading:
        publish() calls confluent-kafka Producer.produce() which is
        non-blocking (enqueues to internal buffer). Delivery receipts are
        polled by a background asyncio task every 100ms.

    Phase 8 halt integration (ADR-015):
        After ``consecutive_failure_threshold`` consecutive delivery failures
        or delivery timeouts, ``on_halt`` is invoked (expected to set a
        service-level halt flag), and a KILL_SWITCH_ACTIVE event is enqueued
        to the local outbox for delivery when Kafka recovers.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        aws_region: str = "ap-south-1",
        delivery_timeout_seconds: float = 10.0,
        outbox: Optional[LocalOutbox] = None,
        on_halt: Optional[Callable[[], None]] = None,
        consecutive_failure_threshold: int = _CONSECUTIVE_FAILURE_THRESHOLD,
    ) -> None:
        """
        Initialize the approved signal publisher.

        Args:
            bootstrap_servers: MSK bootstrap broker string (port 9098).
            aws_region: AWS region for IAM token generation.
            delivery_timeout_seconds: Seconds to wait for delivery ack.
            outbox: LocalOutbox for buffering kill-switch events on Kafka outage.
            on_halt: Callable invoked when consecutive failures hit threshold.
            consecutive_failure_threshold: Failures before halt fires.
        """
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError(
                "confluent-kafka is required for KafkaApprovedPublisher. "
                "Install: pip install confluent-kafka~=2.3"
            )
        self._bootstrap_servers = bootstrap_servers
        self._aws_region = aws_region
        self._producer: Producer | None = None
        self._poll_task: asyncio.Task | None = None
        self._running: bool = False
        self._pending_count: int = 0
        self._lock = threading.Lock()
        self._delivery_timeout_seconds = delivery_timeout_seconds
        self._outbox = outbox
        self._on_halt = on_halt
        self._threshold = consecutive_failure_threshold

        # Consecutive failure tracking (Phase 8 / ADR-015)
        self._consecutive_failures: int = 0
        self._halt_fired: bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize the Kafka producer and start the delivery-receipt poller."""
        if self._running:
            return
        self._producer = self._build_producer()
        self._running = True
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="kafka_approved_delivery_poll"
        )

        if self._outbox is not None:
            self._outbox.open()
            producer_ref = self._producer

            def _outbox_producer_send(topic: str, key: str, value: bytes) -> None:
                producer_ref.produce(topic=topic, key=key.encode("utf-8"), value=value)
                producer_ref.poll(0)

            await self._outbox.start_drain(_outbox_producer_send)

        logger.info("KafkaApprovedPublisher started (topic=%s)", _TOPIC)

    async def stop(self) -> None:
        """
        Drain all in-flight deliveries and close the producer.

        Waits up to 10 seconds for pending messages to be acknowledged before
        returning. Any unacknowledged messages are logged as critical errors.
        """
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._outbox is not None:
            await self._outbox.stop_drain()
            self._outbox.close()

        if self._producer is not None:
            # Synchronous flush — blocks until all queued messages are delivered
            remaining = self._producer.flush(timeout=10)
            if remaining > 0:
                logger.critical(
                    "KafkaApprovedPublisher.stop: %d approved signal(s) undelivered "
                    "after 10s flush — these signals will not reach execution",
                    remaining,
                )
            self._producer = None
        logger.info("KafkaApprovedPublisher stopped")

    @property
    def pending_count(self) -> int:
        """Number of messages in the producer buffer awaiting delivery."""
        return self._pending_count

    # ── Public interface ──────────────────────────────────────────────────────

    async def publish(
        self,
        signal: Signal,
        risk_decision_id: str,
        trace_id: str = "",
    ) -> dict:
        """
        Publish a SIGNAL_APPROVED event and wait for Kafka durability.

        This method waits for the confluent-kafka delivery callback instead of
        returning after local enqueue. With ``acks=all`` and idempotence enabled,
        a successful return means Kafka acknowledged the record; failures raise
        so the source ``signals.pending`` offset is not committed.

        Args:
            signal: The approved Signal model.
            risk_decision_id: UUID of the risk decision record (links to S3 audit).
            trace_id: Trace ID propagated from the originating tick event.
        """
        if self._producer is None:
            raise RuntimeError(
                "KafkaApprovedPublisher.publish called before start() — "
                f"signal {signal.signal_id} was not published"
            )

        event = self._build_event(signal, risk_decision_id, trace_id)
        instrument_id = f"{signal.market}:{signal.symbol}"
        key = instrument_id.encode("utf-8")
        value = json.dumps(event, default=str).encode("utf-8")
        loop = asyncio.get_running_loop()
        delivery_ack: asyncio.Future[object] = loop.create_future()

        def finish_ok(msg) -> None:
            if not delivery_ack.done():
                delivery_ack.set_result(msg)

        def finish_error(exc: BaseException) -> None:
            if not delivery_ack.done():
                delivery_ack.set_exception(exc)

        def on_delivery(err, msg) -> None:
            with self._lock:
                self._pending_count = max(0, self._pending_count - 1)

            if err is not None:
                error = RuntimeError(
                    "SIGNAL_APPROVED delivery failed "
                    f"topic={msg.topic() if msg else '?'} "
                    f"key={msg.key().decode('utf-8') if msg and msg.key() else '?'} "
                    f"error={err}"
                )
                logger.critical(
                    "%s — approved signal was not durably published",
                    error,
                )
                self._record_failure()
                loop.call_soon_threadsafe(finish_error, error)
                return

            self._consecutive_failures = 0
            logger.debug(
                "SIGNAL_APPROVED delivered (topic=%s, partition=%d, offset=%d, key=%s)",
                msg.topic(),
                msg.partition(),
                msg.offset(),
                msg.key().decode("utf-8") if msg.key() else "?",
            )
            loop.call_soon_threadsafe(finish_ok, msg)

        try:
            with self._lock:
                self._pending_count += 1
            self._producer.produce(
                topic=_TOPIC,
                key=key,
                value=value,
                on_delivery=on_delivery,
            )
            self._producer.poll(0)
        except (KafkaException, BufferError) as exc:
            with self._lock:
                self._pending_count -= 1
            raise RuntimeError(
                f"Failed to enqueue approved signal {signal.signal_id} to Kafka"
            ) from exc

        try:
            await asyncio.wait_for(delivery_ack, timeout=self._delivery_timeout_seconds)
        except TimeoutError as exc:
            logger.critical(
                "Timed out waiting %.1fs for SIGNAL_APPROVED delivery ack "
                "(signal_id=%s risk_decision_id=%s). Source offset must not be committed.",
                self._delivery_timeout_seconds,
                signal.signal_id,
                risk_decision_id,
            )
            self._record_failure()
            raise RuntimeError(
                f"Timed out waiting for approved signal {signal.signal_id} delivery ack"
            ) from exc

        return event

    # ── Halt tracking (Phase 8 / ADR-015) ────────────────────────────────────

    def _record_failure(self) -> None:
        """Increment consecutive failure counter; fire halt at threshold."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold and not self._halt_fired:
            self._halt_fired = True
            logger.critical(
                "kafka_approved_publisher.halt_triggered consec=%d threshold=%d "
                "— invoking halt callback and buffering kill-switch event",
                self._consecutive_failures,
                self._threshold,
            )
            if self._on_halt is not None:
                try:
                    self._on_halt()
                except Exception:
                    logger.exception("kafka_approved_publisher.halt_callback_error")
            self._enqueue_kill_switch_to_outbox()

    def _enqueue_kill_switch_to_outbox(self) -> None:
        """Buffer a KILL_SWITCH_ACTIVE event so it reaches Kafka on recovery."""
        if self._outbox is None:
            return
        payload = json.dumps({
            "event_id":      str(uuid.uuid4()),
            "trace_id":      str(uuid.uuid4()),
            "event_type":    EventType.KILL_SWITCH_ACTIVE.value,
            "schema_version": SCHEMA_VERSION,
            "source":        "risk_engine",
            "published_time": datetime.now(UTC).isoformat(),
            "reason":        "kafka_consecutive_delivery_failures",
            "activated_by":  "kafka_approved_publisher",
        }, default=str).encode("utf-8")
        self._outbox.enqueue(
            topic=CANONICAL_KILL_SWITCH_TOPIC,
            key="GLOBAL",
            value=payload,
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Background task: call producer.poll() every 100ms to trigger callbacks."""
        while self._running:
            try:
                await asyncio.sleep(0.1)
                if self._producer is not None:
                    self._producer.poll(0)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("KafkaApprovedPublisher poll loop error")

    def _build_event(
        self,
        signal: Signal,
        risk_decision_id: str,
        trace_id: str,
    ) -> dict:
        """Build a v3.0 SIGNAL_APPROVED event dict from a Signal model."""
        now_utc = datetime.now(UTC)
        instrument_id = f"{signal.market}:{signal.symbol}"
        strategy_id = str(signal.metadata.get("strategy_id") or signal.strategy_name)
        product_type = str(
            signal.metadata.get("product_type")
            or ("MIS" if signal.market.upper() == "NSE" else "DAY")
        )
        expires_at = signal.metadata.get("expires_at")
        if not expires_at:
            signal_time = signal.generated_at
            if signal_time.tzinfo is None:
                signal_time = signal_time.replace(tzinfo=UTC)
            expires_at = (signal_time + timedelta(seconds=30)).isoformat()

        event = {
            "event_id":         str(uuid.uuid4()),
            "trace_id":         trace_id or str(uuid.uuid4()),
            "event_type":       EventType.SIGNAL_APPROVED.value,
            "schema_version":   _SCHEMA_VERSION,
            "source":           "risk_engine",
            "published_time":   now_utc.isoformat(),
            "signal_id":        signal.signal_id,
            "risk_decision_id": risk_decision_id,
            "strategy_id":      strategy_id,
            "instrument_id":    instrument_id,
            "market":           signal.market,
            "strategy_name":    signal.strategy_name,
            "direction":        signal.direction.value,
            "quantity":         signal.quantity,
            "price_at_signal":  signal.price_at_signal,
            "stop_loss":        signal.stop_loss,
            "take_profit":      signal.take_profit,
            "product_type":     product_type,
            "confidence":       signal.confidence,
            "signal_time":      signal.generated_at.isoformat(),
            "expires_at":       str(expires_at),
            "approved_at":      now_utc.isoformat(),
            "paper_trade":      signal.paper_trade,  # Phase 3 — pass through unchanged
            "metadata":         signal.metadata,
        }
        errors = validate_event(event, EventType.SIGNAL_APPROVED)
        if errors:
            raise ValueError(f"Invalid SIGNAL_APPROVED event: {'; '.join(errors)}")
        return event

    def _build_producer(self) -> Producer:
        """Build a confluent-kafka Producer for MSK Serverless with IAM auth."""
        region = self._aws_region
        return Producer({
            "bootstrap.servers":                     self._bootstrap_servers,
            **get_kafka_auth_config(region),
            # Delivery guarantees
            "acks":                                  "all",
            "enable.idempotence":                    True,
            "max.in.flight.requests.per.connection": 1,
            "retries":                               5,
            "retry.backoff.ms":                      200,
            # Throughput
            "compression.type":                      "lz4",
            "batch.size":                            4096,
            "linger.ms":                             5,
            "socket.connection.setup.timeout.ms":    15000,
        })
