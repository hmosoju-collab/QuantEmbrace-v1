"""Retry and DLQ publisher for Kafka consumers.

Consumers use this after they have made a durable decision about a poison or
failed message. The original Kafka payload is wrapped, not discarded, so
operators can inspect the exact bytes that failed.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any
import uuid

from shared.events.schemas import SCHEMA_VERSION, dlq_topic, retry_topic
from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="shared")

try:
    from confluent_kafka import KafkaException, Producer
    _CONFLUENT_AVAILABLE = True
except ImportError:
    _CONFLUENT_AVAILABLE = False
    Producer = None  # type: ignore[assignment]
    KafkaException = Exception  # type: ignore[assignment]

from shared.kafka.config import get_kafka_auth_config


class KafkaFailurePublisher:
    """Publishes malformed or failed records to ``<topic>.retry``/``<topic>.dlq``."""

    def __init__(
        self,
        bootstrap_servers: str,
        aws_region: str = "ap-south-1",
        source_service: str = "unknown",
        max_retry_attempts: int = 3,
    ) -> None:
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError("confluent-kafka is required for KafkaFailurePublisher")
        self._bootstrap_servers = bootstrap_servers
        self._aws_region = aws_region
        self._source_service = source_service
        self._max_retry_attempts = max_retry_attempts
        self._producer: Producer | None = None

    def start(self) -> None:
        """Create the underlying Kafka producer."""
        if self._producer is not None:
            return
        self._producer = self._build_producer()

    def close(self, timeout: float = 5.0) -> None:
        """Flush pending failure records."""
        if self._producer is not None:
            remaining = self._producer.flush(timeout)
            if remaining:
                logger.error(
                    "KafkaFailurePublisher close left %d failure event(s) undelivered",
                    remaining,
                )
            self._producer = None

    def publish_dlq(
        self,
        *,
        source_topic: str,
        key: bytes | None,
        value: bytes | None,
        reason: str,
        error_type: str,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """Publish an inspectable record to ``<source_topic>.dlq``."""
        return self._publish(
            target_topic=dlq_topic(source_topic),
            source_topic=source_topic,
            key=key,
            value=value,
            reason=reason,
            error_type=error_type,
            details=details,
        )

    def publish_retry(
        self,
        *,
        source_topic: str,
        key: bytes | None,
        value: bytes | None,
        reason: str,
        error_type: str,
        details: dict[str, Any] | None = None,
        headers: list[tuple[str, bytes | str | None]] | None = None,
    ) -> bool:
        """Publish an inspectable record to ``<source_topic>.retry``.

        Retry attempts are tracked through Kafka headers on replayed primary
        messages. Once max attempts are exhausted, the record is sent to DLQ
        instead of being put back onto the retry topic forever.
        """
        retry_attempt = _next_retry_attempt(headers)
        if retry_attempt > self._max_retry_attempts:
            return self._publish(
                target_topic=dlq_topic(source_topic),
                source_topic=source_topic,
                key=key,
                value=value,
                reason=reason,
                error_type=f"{error_type}_retry_exhausted",
                details={
                    **(details or {}),
                    "retry_attempt": retry_attempt,
                    "max_retry_attempts": self._max_retry_attempts,
                },
            )

        return self._publish(
            target_topic=retry_topic(source_topic),
            source_topic=source_topic,
            key=key,
            value=value,
            reason=reason,
            error_type=error_type,
            details={
                **(details or {}),
                "retry_attempt": retry_attempt,
                "max_retry_attempts": self._max_retry_attempts,
            },
        )

    def _publish(
        self,
        *,
        target_topic: str,
        source_topic: str,
        key: bytes | None,
        value: bytes | None,
        reason: str,
        error_type: str,
        details: dict[str, Any] | None,
    ) -> bool:
        if self._producer is None:
            logger.error("KafkaFailurePublisher used before start(); failure record dropped")
            return False

        envelope = {
            "event_id": str(uuid.uuid4()),
            "event_type": "KAFKA_FAILURE",
            "schema_version": SCHEMA_VERSION,
            "source": self._source_service,
            "published_time": datetime.now(UTC).isoformat(),
            "source_topic": source_topic,
            "failure_topic": target_topic,
            "reason": reason,
            "error_type": error_type,
            "details": details or {},
            "retry_attempt": (details or {}).get("retry_attempt", 0),
            "max_retry_attempts": (details or {}).get(
                "max_retry_attempts",
                self._max_retry_attempts,
            ),
            "original_key": key.decode("utf-8", errors="replace") if key else None,
            "original_value": value.decode("utf-8", errors="replace") if value else None,
        }
        try:
            self._producer.produce(
                topic=target_topic,
                key=key,
                value=json.dumps(envelope, default=str).encode("utf-8"),
            )
            self._producer.poll(0)
            return True
        except (KafkaException, BufferError):
            logger.exception(
                "Failed to publish Kafka failure record to %s for source topic %s",
                target_topic,
                source_topic,
            )
            return False

    def _build_producer(self) -> Producer:
        region = self._aws_region
        return Producer(
            {
                "bootstrap.servers": self._bootstrap_servers,
                **get_kafka_auth_config(region),
                "acks": "all",
                "enable.idempotence": True,
                "max.in.flight.requests.per.connection": 1,
                "retries": 5,
                "retry.backoff.ms": 200,
                "compression.type": "lz4",
                "linger.ms": 5,
                "delivery.timeout.ms": 30000,
                "socket.connection.setup.timeout.ms": 15000,
                "log.connection.close": False,
            }
        )


def _next_retry_attempt(headers: list[tuple[str, bytes | str | None]] | None) -> int:
    """Return the next retry attempt number from replay headers."""
    current_attempt = 0
    for name, value in headers or []:
        if name != "qe-retry-attempt" or value is None:
            continue
        try:
            raw_value = value.decode("utf-8") if isinstance(value, bytes) else str(value)
            current_attempt = int(raw_value)
        except (TypeError, ValueError):
            current_attempt = 0
        break
    return current_attempt + 1
