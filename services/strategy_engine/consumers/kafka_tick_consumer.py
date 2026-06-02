"""
Kafka Tick Consumer — tick source for the strategy engine.

Consumes from ``ticks.nse`` and ``ticks.us`` MSK Serverless topics using
IAM authentication (SASL/OAUTHBEARER on port 9098).

Consumer group: ``strategy-v1``

Offsets are committed manually only after tick processing and signal publishing
succeed. On restart, Kafka redelivers any uncommitted tick.

Tick fields consumed from v3.0 schema:
    instrument_id → split to (market, symbol)
    exchange_time → tick timestamp (broker time, not system clock)
    ltp           → last traded price
    volume        → tick volume
    trace_id      → propagated unchanged to SIGNAL_PENDING event
    sequence_id   → monotonic counter (set by KafkaTickPublisher)
    gap_detected  → True for post-reconnect warm-up ticks; suppress signals

Usage in service.py:
    consumer = KafkaTickConsumer(
        bootstrap_servers=kafka_bootstrap,
        aws_region=settings.aws.region,
    )
    await consumer.start()

    # Inside async processing loop (asyncio.to_thread wrapper):
    tick = await asyncio.to_thread(consumer.poll_tick)
    if tick is not None:
        await service.process_tick(
            symbol=tick.symbol,
            price=tick.price,
            volume=tick.volume,
            timestamp=tick.timestamp,
            trace_id=tick.trace_id,
        )

    await consumer.stop()
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from shared.kafka.failure_publisher import KafkaFailurePublisher
from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="strategy_engine")

# ── Import guard ──────────────────────────────────────────────────────────────
try:
    from confluent_kafka import Consumer, KafkaError
    _CONFLUENT_AVAILABLE = True
except ImportError:
    _CONFLUENT_AVAILABLE = False
    logger.warning(
        "confluent-kafka not installed — KafkaTickConsumer will not function. "
        "Install: pip install confluent-kafka~=2.3"
    )

from shared.kafka.config import get_kafka_auth_config

# Topics this consumer subscribes to
_TOPICS = ["ticks.nse", "ticks.us"]

# Default consumer group — independent offset tracking per service version
_CONSUMER_GROUP = "strategy-v1"


def _coerce_bool(value: Any) -> bool:
    """Parse JSON bool-like values without turning the string 'false' truthy."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


@dataclass
class TickEvent:
    """
    Parsed tick event from Kafka — minimal fields required by strategy dispatch.

    Derived from the v3.0 TICK event schema (phase2_final_approved.md §4.1).
    Only fields needed for strategy indicator computation and signal generation
    are extracted; the raw event is not retained.
    """

    symbol: str           # e.g. "RELIANCE", "AAPL"
    market: str           # "NSE" | "US"
    price: float          # last traded price (ltp)
    volume: int           # tick volume
    timestamp: datetime   # exchange_time (broker time, UTC-aware)
    trace_id: str         # propagated from TICK → SIGNAL_PENDING → ... → FILL
    sequence_id: int      # monotonic counter (resets on publisher restart)
    gap_detected: bool    # post-reconnect warm-up tick; strategy must suppress signals
    raw_topic: str
    raw_offset: int
    raw_message: Any


class KafkaTickConsumer:
    """
    Consumer for the ticks.nse and ticks.us Kafka topics.

    The consumer group ``strategy-v1`` gives the strategy engine its own
    independent offset cursor, separate from risk-v1 and execution-v1 —
    all three consumer groups receive a full copy of every tick without
    interfering with each other.

    Threading model:
        poll_tick() is synchronous (confluent-kafka Consumer.poll() is
        blocking). It must be called via asyncio.to_thread() in the
        strategy engine's async processing loop.

    Offset management:
        enable.auto.commit=False. A tick offset is committed only after downstream
        signal publishing succeeds. If the strategy crashes first, Kafka
        redelivers the tick and deterministic signal_id deduplication suppresses
        duplicates downstream.

    v3.0 schema validation:
        Messages with schema_version != "3.0" are logged and skipped. This
        prevents future schema migrations from causing silent data corruption
        in running strategy instances.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        aws_region: str = "ap-south-1",
        consumer_group: str = _CONSUMER_GROUP,
        poll_timeout_seconds: float = 1.0,
    ) -> None:
        """
        Initialize the Kafka tick consumer.

        Args:
            bootstrap_servers: MSK bootstrap broker string (port 9098).
                               Format: b-X.{cluster}.{uuid}.kafka.{region}.amazonaws.com:9098
            aws_region: AWS region for IAM token generation.
            consumer_group: Kafka consumer group ID. Defaults to "strategy-v1".
            poll_timeout_seconds: Blocking poll timeout. Controls the maximum
                time the event loop thread is blocked. Keep ≤ 2s.
        """
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError(
                "confluent-kafka is required for KafkaTickConsumer. "
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
        """
        Create the Kafka consumer and subscribe to tick topics.

        The consumer is created here rather than in __init__ so that no
        network connections are attempted during object construction.
        """
        self._consumer = self._build_consumer()
        self._failure_publisher = KafkaFailurePublisher(
            bootstrap_servers=self._bootstrap_servers,
            aws_region=self._aws_region,
            source_service="strategy_engine",
        )
        self._failure_publisher.start()
        self._consumer.subscribe(_TOPICS)
        self._running = True
        logger.info(
            "KafkaTickConsumer started (group=%s, topics=%s)",
            self._consumer_group,
            _TOPICS,
        )

    async def stop(self) -> None:
        """
        Close the consumer, committing final offsets to the broker.

        After stop() returns, poll_tick() will return None immediately.
        """
        self._running = False
        if self._consumer is not None:
            # close() triggers a synchronous rebalance + final offset commit
            self._consumer.close()
            self._consumer = None
        if self._failure_publisher is not None:
            self._failure_publisher.close()
            self._failure_publisher = None
        logger.info("KafkaTickConsumer stopped (group=%s)", self._consumer_group)

    # ── Public interface ──────────────────────────────────────────────────────

    def poll_tick(self) -> Optional[TickEvent]:
        """
        Synchronous: poll the broker for one tick message.

        Blocks for up to ``poll_timeout_seconds`` waiting for a message.
        Returns a TickEvent on success, None on timeout, error, or shutdown.

        IMPORTANT: This method is synchronous and MUST be called via
        asyncio.to_thread() in async code. Calling it directly in a coroutine
        will block the event loop for up to poll_timeout_seconds.

        Returns:
            TickEvent if a valid v3.0 tick message was received.
            None on timeout (no message available), end-of-partition, or
            parse errors (logged internally).
        """
        if not self._consumer or not self._running:
            return None

        msg = self._consumer.poll(self._poll_timeout)

        if msg is None:
            # Timeout — no message available within poll_timeout_seconds
            return None

        if msg.error():
            err = msg.error()
            if err.code() == KafkaError._PARTITION_EOF:
                # End-of-partition is expected at low traffic — not an error
                return None
            logger.error(
                "Kafka consumer error on topic %s: %s",
                msg.topic() if msg.topic() else "?",
                err,
            )
            return None

        self._last_failure_routed = False
        event = self._parse_message(msg)
        if event is None and self._last_failure_routed:
            self.commit_message(msg)
        return event

    def commit(self, event: TickEvent) -> None:
        """Synchronously commit a tick after signal publishing succeeds."""
        self.commit_message(event.raw_message)

    def commit_message(self, msg: Any) -> None:
        if self._consumer is None:
            return
        self._consumer.commit(message=msg, asynchronous=False)

    def publish_retry(
        self,
        event: TickEvent,
        *,
        reason: str,
        error_type: str = "processing_failed",
    ) -> bool:
        """Route a processing-failed tick to <topic>.retry."""
        if self._failure_publisher is None:
            return False
        msg = event.raw_message
        return self._failure_publisher.publish_retry(
            source_topic=event.raw_topic,
            key=msg.key() if msg else None,
            value=msg.value() if msg else None,
            reason=reason,
            error_type=error_type,
            details={
                "symbol": event.symbol,
                "market": event.market,
                "raw_offset": event.raw_offset,
            },
            headers=msg.headers() if msg and hasattr(msg, "headers") else None,
        )

    # ── Message parsing ───────────────────────────────────────────────────────

    def _parse_message(self, msg) -> Optional[TickEvent]:
        """
        Parse a raw confluent-kafka Message into a TickEvent.

        Validates schema_version is "3.0". Extracts (market, symbol) from
        instrument_id by splitting on ":". Parses exchange_time with
        timezone coercion to UTC-aware if naive.

        Returns None and logs a warning on any parse error rather than
        raising, so the consumer loop stays alive on bad messages.
        """
        try:
            body = json.loads(msg.value().decode("utf-8"))

            # Schema guard — reject unknown versions to prevent silent corruption
            schema_version = body.get("schema_version")
            if schema_version != "3.0":
                logger.warning(
                    "Unsupported schema_version %r on topic %s (key=%s) — skipping",
                    schema_version,
                    msg.topic(),
                    msg.key().decode("utf-8") if msg.key() else "?",
                )
                self._last_failure_routed = self._publish_message_to_dlq(
                    msg,
                    reason=f"Unsupported schema_version {schema_version!r}",
                    error_type="schema_validation_failed",
                )
                return None

            # instrument_id = "NSE:RELIANCE" or "US:AAPL"
            instrument_id: str = body["instrument_id"]
            market, symbol = instrument_id.split(":", 1)

            # exchange_time is broker time (ISO 8601, UTC)
            timestamp = datetime.fromisoformat(body["exchange_time"])
            if timestamp.tzinfo is None:
                # Defensive: broker should always send UTC-aware, but coerce if not
                timestamp = timestamp.replace(tzinfo=timezone.utc)

            return TickEvent(
                symbol=symbol,
                market=market,
                price=float(body["ltp"]),
                volume=int(body.get("volume") or 0),
                timestamp=timestamp,
                trace_id=body.get("trace_id", ""),
                sequence_id=int(body.get("sequence_id", 0)),
                gap_detected=_coerce_bool(body.get("gap_detected", False)),
                raw_topic=msg.topic() or "",
                raw_offset=msg.offset() or 0,
                raw_message=msg,
            )

        except (KeyError, ValueError, json.JSONDecodeError, AttributeError) as exc:
            logger.exception(
                "Malformed tick message on topic %s (offset=%s) — skipping",
                msg.topic() if msg.topic() else "?",
                msg.offset() if msg.offset() is not None else "?",
            )
            self._last_failure_routed = self._publish_message_to_dlq(
                msg,
                reason=str(exc),
                error_type="malformed_tick",
            )
            return None

    def _publish_message_to_dlq(self, msg: Any, *, reason: str, error_type: str) -> bool:
        if self._failure_publisher is None:
            return False
        return self._failure_publisher.publish_dlq(
            source_topic=msg.topic() or "ticks.unknown",
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
        """
        Build a confluent-kafka Consumer configured for MSK Serverless IAM auth.

        Key config choices:
            auto.offset.reset=latest    → on first start, skip old ticks.
                                          We don't want to replay historical
                                          ticks through live strategy instances.
            enable.auto.commit=False    → offset is committed only after tick
                                          processing and signal publish complete.
            session.timeout.ms=30000    → broker detects dead consumer after 30s
                                          (triggers rebalance to another instance).
            max.poll.interval.ms=300000 → 5 minutes max between polls.
                                          Prevents rebalance if strategy
                                          initialization takes a long time.
            fetch.wait.max.ms=500       → max wait for fetch response; combined
                                          with poll_timeout_seconds controls
                                          per-message latency.
        """
        region = self._aws_region
        conf = {
            "bootstrap.servers":                      self._bootstrap_servers,
            **get_kafka_auth_config(region),
            # Consumer group
            "group.id":                               self._consumer_group,
            # Offset management
            "auto.offset.reset":                      "latest",
            "enable.auto.commit":                     False,
            "enable.auto.offset.store":               False,
            # Session / rebalance
            "session.timeout.ms":                     30000,
            "heartbeat.interval.ms":                  10000,
            "max.poll.interval.ms":                   300000,
            # Fetch tuning for personal-scale tick rate
            "fetch.min.bytes":                        1,
            "fetch.wait.max.ms":                      500,
            # Connection
            "socket.connection.setup.timeout.ms":     15000,
            # Reduce log noise
            "log.connection.close":                   False,
        }
        return Consumer(conf)
