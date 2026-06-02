"""
KafkaEnrichedPublisher — publishes EnrichedSignal to signals.enriched.

Topic:          ``signals.enriched``
Key:            symbol (same partitioning as signals.pending — preserves
                ordering per symbol within the pipeline)
Schema version: 4.0 (SIGNAL_ENRICHED event type)

Phase 6 design (ADR-014 §5.6):
  Partitioned by symbol so signals for the same instrument are processed
  in order by risk_engine even after passing through ai_engine enrichment.

  4 partitions match signals.pending.  This allows partition-aligned
  consumption by risk_engine's consumer group.

Delivery reporting:
  Background poll loop logs delivery errors.  Delivery failures are logged
  and the signal is NOT retried — the risk_engine watchdog detects the lag
  and falls back to signals.pending automatically.

MSK IAM auth:
  Same SASL/OAUTHBEARER pattern as all other producers in the system.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from shared.events.schemas import (
    CANONICAL_SIGNALS_ENRICHED_TOPIC,
    ENRICHED_SCHEMA_VERSION,
    EventType,
)
from shared.logging.logger import get_logger
from shared.models.enriched_signal import EnrichedSignal

logger = get_logger(__name__, service_name="ai_engine")

_TOPIC = CANONICAL_SIGNALS_ENRICHED_TOPIC

# ── Import guards ─────────────────────────────────────────────────────────────
try:
    from confluent_kafka import Producer, KafkaException
    _CONFLUENT_AVAILABLE = True
except ImportError:
    _CONFLUENT_AVAILABLE = False
    logger.warning("confluent-kafka not installed — KafkaEnrichedPublisher unavailable.")

from shared.kafka.config import get_kafka_auth_config


class KafkaEnrichedPublisher:
    """
    Producer for the signals.enriched Kafka topic.

    Args:
        bootstrap_servers: MSK bootstrap broker string (port 9098).
        aws_region:        AWS region for IAM token generation.
        source_service:    Service name for event envelope (default "ai_engine").
    """

    def __init__(
        self,
        bootstrap_servers: str,
        aws_region:        str = "ap-south-1",
        source_service:    str = "ai_engine",
    ) -> None:
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError(
                "confluent-kafka is required. pip install confluent-kafka~=2.3"
            )
        self._bootstrap_servers = bootstrap_servers
        self._aws_region        = aws_region
        self._source_service    = source_service
        self._producer: Optional["Producer"] = None
        self._poll_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._running: bool = False
        self._published_count: int = 0
        self._error_count: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Create the confluent-kafka producer."""
        self._producer = self._build_producer()
        self._running  = True
        logger.info(
            "ai_engine.KafkaEnrichedPublisher.started",
            topic=_TOPIC,
        )

    def stop(self) -> None:
        """Flush pending messages and close the producer."""
        self._running = False
        if self._producer is not None:
            self._producer.flush(timeout=10)
            self._producer = None
        logger.info(
            "ai_engine.KafkaEnrichedPublisher.stopped",
            published=self._published_count,
            errors=self._error_count,
        )

    # ── Public interface ──────────────────────────────────────────────────────

    def publish(self, enriched: EnrichedSignal) -> bool:
        """
        Publish an EnrichedSignal to signals.enriched.

        Synchronous (confluent-kafka Producer.produce is non-blocking but
        synchronous for message enqueue).  Call via asyncio.to_thread if needed
        in very tight loops.

        Returns:
            True if the message was enqueued; False on error.
        """
        if not self._producer or not self._running:
            logger.error("ai_engine.publisher_not_started")
            return False

        payload = self._build_event(enriched)
        key     = enriched.symbol.encode("utf-8")
        value   = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        try:
            self._producer.produce(
                topic     = _TOPIC,
                key       = key,
                value     = value,
                on_delivery = self._on_delivery,
            )
            self._producer.poll(0)  # trigger callbacks without blocking
            self._published_count += 1
            return True

        except Exception as exc:
            self._error_count += 1
            logger.error(
                "ai_engine.publisher_produce_error",
                signal_id=enriched.signal_id,
                error=str(exc),
            )
            return False

    def flush(self, timeout_seconds: float = 5.0) -> None:
        """Flush all pending messages (call before stop in tests)."""
        if self._producer:
            self._producer.flush(timeout=timeout_seconds)

    @property
    def published_count(self) -> int:
        return self._published_count

    @property
    def error_count(self) -> int:
        return self._error_count

    # ── Event envelope ────────────────────────────────────────────────────────

    def _build_event(self, enriched: EnrichedSignal) -> dict[str, Any]:
        """Build the v4.0 SIGNAL_ENRICHED Kafka event envelope."""
        now = datetime.now(timezone.utc)
        payload = enriched.to_dict()
        payload.update({
            "event_id":       str(uuid.uuid4()),
            "event_type":     EventType.SIGNAL_ENRICHED.value,
            "schema_version": ENRICHED_SCHEMA_VERSION,
            "source":         self._source_service,
            "published_time": now.isoformat(),
            # v3.0 compat fields for risk_engine fallback parser
            "instrument_id":  enriched.instrument_id,
            "signal_time":    enriched.generated_at.isoformat(),
        })
        return payload

    # ── Delivery callback ─────────────────────────────────────────────────────

    def _on_delivery(self, err: Any, msg: Any) -> None:
        """Confluent-kafka delivery report callback."""
        if err is not None:
            self._error_count += 1
            logger.error(
                "ai_engine.publisher_delivery_error",
                topic=_TOPIC,
                error=str(err),
            )
        else:
            logger.debug(
                "ai_engine.publisher_delivered",
                topic=msg.topic(),
                partition=msg.partition(),
                offset=msg.offset(),
            )

    # ── Producer construction ─────────────────────────────────────────────────

    def _build_producer(self) -> "Producer":
        """Build confluent-kafka Producer for MSK Serverless with IAM auth."""
        region = self._aws_region
        return Producer({
            "bootstrap.servers":                  self._bootstrap_servers,
            **get_kafka_auth_config(region),
            "acks":                               "all",
            "retries":                            3,
            "retry.backoff.ms":                   200,
            "linger.ms":                          5,
            "batch.size":                         16384,
            "compression.type":                   "lz4",
            "socket.connection.setup.timeout.ms": 15000,
        })
