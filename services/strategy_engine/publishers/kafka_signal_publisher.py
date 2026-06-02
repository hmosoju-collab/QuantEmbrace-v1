"""
Kafka Signal Publisher — publishes signals from the strategy engine to risk.

Publishes ``SIGNAL_PENDING`` events to the ``signals.pending`` MSK Serverless
topic. The risk engine consumes this topic (risk-v1 consumer group) to validate
signals before forwarding approved signals to the execution engine.

Topic:  signals.pending  (2 partitions, key = instrument_id)
Schema: v3.0 SIGNAL_PENDING event (see architecture/data_flow.md)

Deterministic signal_id (non-negotiable — risk engine deduplication):
    sha256(strategy_name|symbol|direction|price_4dp|signal_time_iso)[:32]

    Guarantee: the same strategy output at the same tick always produces
    the same signal_id. If the strategy engine restarts and replays a tick,
    the risk engine silently discards the duplicate via its DynamoDB
    conditional write on signal_id.

trace_id propagation:
    Each SIGNAL_PENDING event carries the trace_id from its originating
    TICK event (set by KafkaTickPublisher in data_ingestion). This trace_id
    flows: TICK → SIGNAL_PENDING → SIGNAL_APPROVED → ORDER_FILLED.
    One CloudWatch Logs Insights query on trace_id reconstructs the full
    trade lifecycle.

expires_at:
    signal_time + 30 seconds. The risk engine rejects signals that arrive
    after expires_at — this prevents stale signals from executing after a
    queue backup or processing lag.

Event schema (v3.0 SIGNAL_PENDING — frozen):
    {
        "event_id":        str (uuid4)
        "trace_id":        str (propagated from TICK — never regenerated)
        "event_type":      "SIGNAL_PENDING"
        "schema_version":  "3.0"
        "source":          "strategy_engine"
        "published_time":  ISO8601 UTC
        "signal_id":       sha256(5 fields)[:32]  (deterministic)
        "strategy_name":   str
        "instrument_id":   "{MARKET}:{SYMBOL}"
        "market":          "NSE" | "US"
        "direction":       "BUY" | "SELL"
        "quantity":        int
        "target_price":    float | None  (take_profit)
        "stop_loss":       float | None
        "confidence":      float  (0.0–1.0)
        "price_at_signal": float
        "signal_time":     ISO8601 UTC  (strategy generation time)
        "expires_at":      ISO8601 UTC  (signal_time + 30s)
        "metadata":        dict
    }
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from shared.events.schemas import (
    CANONICAL_KILL_SWITCH_TOPIC,
    CANONICAL_SIGNALS_PENDING_TOPIC,
    EventType,
    SCHEMA_VERSION,
    validate_event,
)
from shared.kafka.local_outbox import LocalOutbox
from shared.logging.logger import get_logger
from shared.models.signal import Signal

logger = get_logger(__name__, service_name="strategy_engine")

# ── Import guard ──────────────────────────────────────────────────────────────
try:
    from confluent_kafka import Producer, KafkaException
    _CONFLUENT_AVAILABLE = True
except ImportError:
    _CONFLUENT_AVAILABLE = False
    logger.warning(
        "confluent-kafka not installed — KafkaSignalPublisher will not function. "
        "Install: pip install confluent-kafka~=2.3"
    )

from shared.kafka.config import get_kafka_auth_config

_TOPIC_SIGNALS_PENDING = CANONICAL_SIGNALS_PENDING_TOPIC
_SCHEMA_VERSION = SCHEMA_VERSION

# Signal TTL — risk engine rejects signals received after expires_at
_SIGNAL_TTL_SECONDS = 30

# Consecutive delivery failures before halt is triggered (Phase 8 / ADR-015)
_CONSECUTIVE_FAILURE_THRESHOLD = 3


class KafkaSignalPublisher:
    """
    Async-compatible Kafka publisher for SIGNAL_PENDING events.

    Delivery semantics:
        acks=all + enable.idempotence=true ensures each signal is committed
        to all ISR replicas before acknowledgement. A signal that fails
        delivery after all retries is logged as an error — the signal is
        lost (acceptable: risk engine will reject it via expires_at anyway).

    Phase 8 halt integration (ADR-015):
        After ``consecutive_failure_threshold`` consecutive delivery failures,
        ``on_halt`` is invoked (expected to set a service-level
        ``_kafka_healthy = False`` flag that stops the consumer loop), and
        a KILL_SWITCH_ACTIVE event is enqueued to the local outbox so it
        is delivered to Kafka once the broker recovers.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        aws_region: str = "ap-south-1",
        outbox: Optional[LocalOutbox] = None,
        on_halt: Optional[Callable[[], None]] = None,
        consecutive_failure_threshold: int = _CONSECUTIVE_FAILURE_THRESHOLD,
    ) -> None:
        """
        Initialize the Kafka signal publisher.

        Args:
            bootstrap_servers: MSK bootstrap broker string (port 9098).
            aws_region: AWS region for IAM token generation.
            outbox: LocalOutbox for buffering kill-switch events when Kafka
                    is unavailable. If None, kill-switch events are not buffered.
            on_halt: Callable invoked (no args) when consecutive failures
                     exceed ``consecutive_failure_threshold``. Expected to
                     set a service-level halt flag.
            consecutive_failure_threshold: Failures before halt fires.
        """
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError(
                "confluent-kafka is required for KafkaSignalPublisher. "
                "Install: pip install confluent-kafka~=2.3"
            )

        self._bootstrap_servers = bootstrap_servers
        self._aws_region = aws_region
        self._outbox = outbox
        self._on_halt = on_halt
        self._threshold = consecutive_failure_threshold
        self._producer: Optional["Producer"] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._running: bool = False

        # Consecutive failure tracking (Phase 8 / ADR-015)
        self._consecutive_failures: int = 0
        self._halt_fired: bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize the Kafka producer and start the delivery-receipt poll loop."""
        if self._running:
            return

        self._producer = self._build_producer()
        self._running = True
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="kafka_signal_delivery_poll"
        )

        if self._outbox is not None:
            self._outbox.open()
            producer_ref = self._producer

            def _outbox_producer_send(topic: str, key: str, value: bytes) -> None:
                producer_ref.produce(topic=topic, key=key.encode("utf-8"), value=value)
                producer_ref.poll(0)

            await self._outbox.start_drain(_outbox_producer_send)

        logger.info(
            "KafkaSignalPublisher started (topic=%s)", _TOPIC_SIGNALS_PENDING
        )

    async def stop(self) -> None:
        """Flush all in-flight signal events and stop the producer."""
        self._running = False

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._outbox is not None:
            await self._outbox.stop_drain()
            self._outbox.close()

        if self._producer is not None:
            # flush() blocks until all buffered messages are delivered or timeout
            remaining = await asyncio.to_thread(self._producer.flush, 10)
            if remaining > 0:
                logger.warning(
                    "KafkaSignalPublisher: %d signal(s) not delivered before shutdown",
                    remaining,
                )
            logger.info("KafkaSignalPublisher stopped")

    # ── Public interface ──────────────────────────────────────────────────────

    async def publish(self, signal: Signal, trace_id: str = "") -> bool:
        """
        Produce a SIGNAL_PENDING event to the signals.pending topic.

        The message key is ``instrument_id`` (e.g. "NSE:RELIANCE"), ensuring
        all signals for the same instrument land on the same partition and are
        processed by the risk engine in order.

        The signal_id in the Kafka event is a deterministic hash (not the UUID
        from Signal.signal_id) so the risk engine can detect duplicates after
        a strategy engine restart replays the same tick.

        Args:
            signal: The trading signal generated by a strategy.
            trace_id: Trace ID from the originating tick event. Pass "" if
                      the signal was not generated from a Kafka tick (e.g.
                      during testing). A fresh uuid4 will be used as fallback.
        """
        if not self._producer:
            logger.error("KafkaSignalPublisher.publish called before start()")
            return False

        event = self._build_signal_event(signal, trace_id)
        instrument_id = event["instrument_id"]

        try:
            value_bytes = json.dumps(event, default=str).encode("utf-8")
            key_bytes   = instrument_id.encode("utf-8")

            self._producer.produce(
                topic=_TOPIC_SIGNALS_PENDING,
                key=key_bytes,
                value=value_bytes,
                on_delivery=self._delivery_callback,
            )
            # Trigger poll to fire any ready delivery callbacks (non-blocking)
            self._producer.poll(0)
            return True

        except KafkaException as e:
            logger.error(
                "Kafka produce error for signal %s (%s): %s",
                event.get("signal_id", "?"),
                instrument_id,
                e,
            )
            return False
        except Exception:
            logger.exception(
                "Unexpected error producing signal %s to Kafka",
                event.get("signal_id", "?"),
            )
            return False

    # ── Event construction ────────────────────────────────────────────────────

    def _build_signal_event(self, signal: Signal, trace_id: str) -> dict:
        """
        Build the v3.0 SIGNAL_PENDING event dict from a Signal object.

        Computes deterministic signal_id and expires_at. Propagates trace_id
        from the originating tick unchanged — never generates a new trace_id
        unless the signal has no upstream trace (in which case a fallback
        uuid4 is used so downstream tracing doesn't break).
        """
        now_utc = datetime.now(timezone.utc)

        # Ensure signal_time is UTC-aware
        signal_time = signal.generated_at
        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=timezone.utc)

        expires_at = signal_time + timedelta(seconds=_SIGNAL_TTL_SECONDS)
        det_signal_id = _make_deterministic_signal_id(signal)
        instrument_id = f"{signal.market}:{signal.symbol}"
        strategy_id = str(signal.metadata.get("strategy_id") or signal.strategy_name)
        product_type = str(
            signal.metadata.get("product_type")
            or ("MIS" if signal.market.upper() == "NSE" else "DAY")
        )

        event = {
            "event_id":        str(uuid.uuid4()),
            "trace_id":        trace_id if trace_id else str(uuid.uuid4()),
            "event_type":      EventType.SIGNAL_PENDING.value,
            "schema_version":  _SCHEMA_VERSION,
            "source":          "strategy_engine",
            "published_time":  now_utc.isoformat(),
            # Deterministic signal_id — NOT Signal.signal_id (which is UUID-based)
            "signal_id":       det_signal_id,
            "strategy_id":     strategy_id,
            "strategy_name":   signal.strategy_name,
            "instrument_id":   instrument_id,
            "market":          signal.market,
            "direction":       signal.direction.value,
            "quantity":        signal.quantity,
            "target_price":    signal.take_profit,
            "stop_loss":       signal.stop_loss,
            "take_profit":     signal.take_profit,
            "product_type":    product_type,
            "confidence":      signal.confidence,
            "price_at_signal": signal.price_at_signal,
            "signal_time":     signal_time.isoformat(),
            "expires_at":      expires_at.isoformat(),
            "paper_trade":     signal.paper_trade,  # Phase 3 — propagated to risk + execution
            "metadata":        signal.metadata,
        }
        errors = validate_event(event, EventType.SIGNAL_PENDING)
        if errors:
            raise ValueError(f"Invalid SIGNAL_PENDING event: {'; '.join(errors)}")
        return event

    # ── Delivery tracking + halt (Phase 8 / ADR-015) ─────────────────────────

    def _delivery_callback(self, err, msg) -> None:
        """
        Confluent-kafka delivery callback — called from the producer's internal thread.

        On success: resets consecutive failure counter.
        On failure: increments counter; at threshold, invokes halt callback and
        enqueues a KILL_SWITCH_ACTIVE event to the local outbox.
        """
        if err is None:
            self._consecutive_failures = 0
            logger.debug(
                "Signal delivered: topic=%s partition=%d offset=%d key=%s",
                msg.topic(),
                msg.partition(),
                msg.offset(),
                msg.key().decode("utf-8") if msg.key() else "?",
            )
        else:
            logger.error(
                "Signal delivery failed: topic=%s partition=%s error=%s",
                msg.topic() if msg else "?",
                msg.partition() if msg else "?",
                err,
            )
            self._record_failure()

    def _record_failure(self) -> None:
        """Increment consecutive failure counter; fire halt at threshold."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold and not self._halt_fired:
            self._halt_fired = True
            logger.critical(
                "kafka_signal_publisher.halt_triggered consec=%d threshold=%d "
                "— invoking halt callback and buffering kill-switch event",
                self._consecutive_failures,
                self._threshold,
            )
            if self._on_halt is not None:
                try:
                    self._on_halt()
                except Exception:
                    logger.exception("kafka_signal_publisher.halt_callback_error")
            self._enqueue_kill_switch_to_outbox()

    def _enqueue_kill_switch_to_outbox(self) -> None:
        """Buffer a KILL_SWITCH_ACTIVE event so it reaches Kafka on recovery."""
        if self._outbox is None:
            return
        import json as _json
        payload = _json.dumps({
            "event_id":     str(uuid.uuid4()),
            "trace_id":     str(uuid.uuid4()),
            "event_type":   EventType.KILL_SWITCH_ACTIVE.value,
            "schema_version": SCHEMA_VERSION,
            "source":       "strategy_engine",
            "published_time": datetime.now(timezone.utc).isoformat(),
            "reason":       "kafka_consecutive_delivery_failures",
            "activated_by": "kafka_signal_publisher",
        }, default=str).encode("utf-8")
        self._outbox.enqueue(
            topic=CANONICAL_KILL_SWITCH_TOPIC,
            key="GLOBAL",
            value=payload,
        )

    # ── Background poll loop ──────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """
        Background asyncio task that polls the producer for delivery receipts
        every 100ms, preventing the internal delivery queue from stalling.
        """
        while self._running:
            try:
                await asyncio.sleep(0.1)
                if self._producer:
                    self._producer.poll(0)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("KafkaSignalPublisher: error in poll loop")

    # ── Producer construction ─────────────────────────────────────────────────

    def _build_producer(self) -> "Producer":
        """
        Build a confluent-kafka Producer configured for MSK Serverless IAM auth.

        Same durability config as KafkaTickPublisher (acks=all, idempotent)
        since signals must be committed to all ISR replicas before the risk
        engine is allowed to act on them.
        """
        region = self._aws_region
        conf = {
            "bootstrap.servers":                     self._bootstrap_servers,
            **get_kafka_auth_config(region),
            # Durability + idempotence
            "acks":                                  "all",
            "enable.idempotence":                    True,
            "max.in.flight.requests.per.connection": 1,
            "retries":                               5,
            "retry.backoff.ms":                      200,
            # Throughput tuning (signal rate << tick rate)
            "compression.type":                      "lz4",
            "batch.size":                            4096,
            "linger.ms":                             5,
            "delivery.timeout.ms":                   30000,
            # Connection
            "socket.connection.setup.timeout.ms":    15000,
            "log.connection.close":                  False,
        }
        return Producer(conf)


# ── Module-level helper ───────────────────────────────────────────────────────

def _make_deterministic_signal_id(signal: Signal) -> str:
    """
    Compute a deterministic 32-character signal ID from 5 stable signal fields.

    Formula:
        sha256(strategy_name|symbol|direction|price_4dp|signal_time_iso)[:32]

    Why deterministic:
        The same strategy processing the same tick always produces the same
        output (direction, quantity, price). Encoding these fields into the
        signal_id means a restarted strategy engine that replays a tick
        produces a signal_id that the risk engine has already seen, and
        its DynamoDB conditional write (attribute_not_exists(signal_id))
        silently discards the duplicate without executing a second order.

    Collision probability:
        32 hex characters = 128 bits of entropy. SHA-256 collisions at this
        prefix length are negligible in any realistic trading volume.
    """
    signal_time = signal.generated_at
    if signal_time.tzinfo is None:
        signal_time = signal_time.replace(tzinfo=timezone.utc)

    raw = "|".join([
        signal.strategy_name,
        signal.symbol,
        signal.direction.value,
        f"{signal.price_at_signal:.4f}",
        signal_time.isoformat(),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
