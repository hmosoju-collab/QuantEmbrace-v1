"""
Kafka Signal Consumer — ai_engine consumes signals.pending (aiengine-v1).

Mirrors the risk_engine KafkaSignalConsumer but uses a different consumer
group (``aiengine-v1``) so ai_engine has its own independent offset cursor.

Consumer group: ``aiengine-v1``
Topic:          ``signals.pending``
Schema:         v3.0 SIGNAL_PENDING (same as risk-v1 parses)

The ai_engine enriches each signal and publishes to signals.enriched.
The risk_engine then consumes signals.enriched (primary) with automatic
fallback to signals.pending when ai_engine lag exceeds threshold.

Threading model:
    poll_signal() is synchronous (confluent-kafka Consumer.poll is blocking).
    Must be called via asyncio.to_thread() from the async processing loop.

Offset management:
    enable.auto.commit=False.  Offsets are committed only after the enriched
    signal has been successfully published to signals.enriched (or explicitly
    on parse errors to avoid infinite redelivery of malformed messages).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from shared.events.schemas import CANONICAL_SIGNALS_PENDING_TOPIC, EventType, SCHEMA_VERSION, validate_event
from shared.logging.logger import get_logger
from shared.models.signal import Direction, Signal, SignalStatus

logger = get_logger(__name__, service_name="ai_engine")

# Consumer group — independent from risk-v1 and execution-v1
_CONSUMER_GROUP = "aiengine-v1"
_TOPIC = CANONICAL_SIGNALS_PENDING_TOPIC

# ── Import guards ─────────────────────────────────────────────────────────────
try:
    from confluent_kafka import Consumer, KafkaError
    _CONFLUENT_AVAILABLE = True
except ImportError:
    _CONFLUENT_AVAILABLE = False
    logger.warning("confluent-kafka not installed — KafkaSignalConsumer unavailable.")

from shared.kafka.config import get_kafka_auth_config


@dataclass
class AISignalEvent:
    """
    Parsed SIGNAL_PENDING event from Kafka for ai_engine processing.

    Identical to risk_engine's SignalEvent but with ``aiengine-v1`` branding.
    """

    signal:       Signal       # Fully populated Signal model
    trace_id:     str          # Propagated from originating TICK event
    strategy_id:  str          # Stable strategy identifier
    product_type: str          # Broker product type (CNC, MIS, NRML)
    expires_at:   datetime     # Hard expiry from strategy_engine
    raw_topic:    str          # Kafka topic (for logging)
    raw_offset:   int          # Kafka offset (for logging)
    raw_message:  Any          # Original confluent-kafka Message for commit


class KafkaSignalConsumer:
    """
    Kafka consumer for signals.pending (ai_engine — group: aiengine-v1).

    Args:
        bootstrap_servers:   MSK bootstrap broker string (port 9098).
        aws_region:          AWS region for IAM token generation.
        poll_timeout_seconds: Blocking poll timeout. Keep ≤ 2s.
    """

    def __init__(
        self,
        bootstrap_servers:    str,
        aws_region:           str = "ap-south-1",
        poll_timeout_seconds: float = 1.0,
    ) -> None:
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError(
                "confluent-kafka is required. pip install confluent-kafka~=2.3"
            )
        self._bootstrap_servers = bootstrap_servers
        self._aws_region        = aws_region
        self._poll_timeout      = poll_timeout_seconds
        self._consumer: Optional["Consumer"] = None
        self._running: bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Create consumer and subscribe to signals.pending."""
        self._consumer = self._build_consumer()
        self._consumer.subscribe([_TOPIC])
        self._running = True
        logger.info(
            "ai_engine.KafkaSignalConsumer.started",
            group=_CONSUMER_GROUP,
            topic=_TOPIC,
        )

    async def stop(self) -> None:
        """Close consumer, committing final offsets."""
        self._running = False
        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None
        logger.info("ai_engine.KafkaSignalConsumer.stopped")

    # ── Public interface ──────────────────────────────────────────────────────

    def poll_signal(self) -> Optional[AISignalEvent]:
        """
        Synchronous poll — MUST be called via asyncio.to_thread().

        Returns:
            AISignalEvent on success, None on timeout/error.
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
            logger.error("ai_engine.kafka_consumer_error", error=str(err))
            return None

        return self._parse_message(msg)

    def commit(self, event: AISignalEvent) -> None:
        """Commit offset for a successfully enriched + published signal."""
        self.commit_message(event.raw_message)

    def commit_message(self, msg: Any) -> None:
        """Commit a raw Kafka message offset synchronously."""
        if self._consumer is not None:
            self._consumer.commit(message=msg, asynchronous=False)

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse_message(self, msg: Any) -> Optional[AISignalEvent]:
        """
        Parse a raw Kafka message into AISignalEvent.

        Returns None and commits offset on unrecoverable parse errors
        (malformed JSON, wrong schema version) to prevent infinite redelivery.
        """
        try:
            body = json.loads(msg.value().decode("utf-8"))

            errors = validate_event(body, EventType.SIGNAL_PENDING)
            if errors:
                logger.warning(
                    "ai_engine.signal_parse_schema_error",
                    errors=errors,
                    offset=msg.offset(),
                )
                self.commit_message(msg)  # skip malformed
                return None

            instrument_id: str = body["instrument_id"]
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

            return AISignalEvent(
                signal       = signal,
                trace_id     = body.get("trace_id", ""),
                strategy_id  = body["strategy_id"],
                product_type = body["product_type"],
                expires_at   = expires_at,
                raw_topic    = msg.topic() or _TOPIC,
                raw_offset   = msg.offset() or 0,
                raw_message  = msg,
            )

        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            logger.error(
                "ai_engine.signal_parse_failed",
                offset=msg.offset() if msg.offset() is not None else "?",
                error=str(exc),
            )
            self.commit_message(msg)  # skip malformed
            return None

    # ── Consumer construction ─────────────────────────────────────────────────

    def _build_consumer(self) -> "Consumer":
        """Build confluent-kafka Consumer for MSK Serverless with IAM auth."""
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_dt(value: Any) -> datetime:
    """Parse ISO-8601 string to timezone-aware datetime."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
