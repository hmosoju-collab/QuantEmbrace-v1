"""Bounded Kafka retry-topic replayer.

Retry topics are not quarantine topics: records written to ``<topic>.retry``
must either be replayed to the original topic or escalated to DLQ. This module
owns that loop for service-local retry topics.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import time
from typing import Any

from shared.events.schemas import SCHEMA_VERSION, dlq_topic, retry_topic
from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="shared")

try:
    from confluent_kafka import Consumer, KafkaError, Producer
    _CONFLUENT_AVAILABLE = True
except ImportError:
    _CONFLUENT_AVAILABLE = False
    Consumer = None  # type: ignore[assignment]
    KafkaError = None  # type: ignore[assignment]
    Producer = None  # type: ignore[assignment]

from shared.kafka.config import get_kafka_auth_config


_RETRY_ATTEMPT_HEADER = "qe-retry-attempt"
_RETRY_SOURCE_HEADER = "qe-retry-source-topic"
_RETRY_EVENT_HEADER = "qe-retry-event-id"


class KafkaRetryReplayer:
    """Consume ``<topic>.retry`` envelopes and republish original records."""

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        aws_region: str = "ap-south-1",
        source_topics: list[str],
        source_service: str,
        consumer_group: str | None = None,
        poll_timeout_seconds: float = 1.0,
        max_tick_retry_age_seconds: float = 5.0,
    ) -> None:
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError("confluent-kafka is required for KafkaRetryReplayer")
        self._bootstrap_servers = bootstrap_servers
        self._aws_region = aws_region
        self._source_topics = source_topics
        self._retry_topics = [retry_topic(topic) for topic in source_topics]
        self._source_service = source_service
        self._consumer_group = consumer_group or f"{source_service}-retry-v1"
        self._poll_timeout = poll_timeout_seconds
        self._max_tick_retry_age_seconds = max_tick_retry_age_seconds
        self._consumer: Consumer | None = None
        self._producer: Producer | None = None
        self._running = False

    async def start(self) -> None:
        """Start the retry-topic consumer and replay producer."""
        if self._running:
            return
        self._consumer = self._build_consumer()
        self._producer = self._build_producer()
        self._consumer.subscribe(self._retry_topics)
        self._running = True
        logger.info(
            "KafkaRetryReplayer started service=%s group=%s topics=%s",
            self._source_service,
            self._consumer_group,
            self._retry_topics,
        )

    async def stop(self) -> None:
        """Stop consuming retry topics and flush replayed records."""
        self._running = False
        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None
        if self._producer is not None:
            remaining = self._producer.flush(10)
            if remaining:
                logger.error(
                    "KafkaRetryReplayer stopped with %d replay record(s) undelivered",
                    remaining,
                )
            self._producer = None

    async def run(self) -> None:
        """Continuously drain retry topics until stopped."""
        if not self._running:
            await self.start()
        while self._running:
            try:
                await asyncio.to_thread(self.poll_once)
            except Exception:
                logger.exception("KafkaRetryReplayer loop error; backing off 1s")
                await asyncio.sleep(1)

    def poll_once(self) -> bool:
        """Poll and process at most one retry message. Returns True if handled."""
        if self._consumer is None or self._producer is None or not self._running:
            return False

        msg = self._consumer.poll(self._poll_timeout)
        if msg is None:
            return False
        if msg.error():
            err = msg.error()
            if KafkaError is not None and err.code() == KafkaError._PARTITION_EOF:
                return False
            logger.error("Kafka retry consumer error on %s: %s", msg.topic() or "?", err)
            return False

        raw_value = msg.value() or b""
        retry_topic_name = msg.topic() or ""

        try:
            envelope = json.loads(raw_value.decode("utf-8"))
            self._handle_envelope(envelope)
            self._consumer.commit(message=msg, asynchronous=False)
            return True
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError) as exc:
            try:
                self._publish_malformed_retry_dlq(
                    retry_topic_name=retry_topic_name,
                    key=msg.key() if hasattr(msg, "key") else None,
                    raw_value=raw_value,
                    reason=str(exc),
                )
                self._consumer.commit(message=msg, asynchronous=False)
                return True
            except Exception:
                logger.exception(
                    "Failed to DLQ malformed retry envelope topic=%s offset=%s; "
                    "leaving uncommitted",
                    retry_topic_name or "?",
                    msg.offset() if hasattr(msg, "offset") else "?",
                )
                return False
        except Exception:
            logger.exception(
                "Failed to replay retry envelope topic=%s offset=%s; leaving uncommitted",
                retry_topic_name or "?",
                msg.offset() if hasattr(msg, "offset") else "?",
            )
            return False

    def _handle_envelope(self, envelope: dict[str, Any]) -> None:
        source_topic = str(envelope.get("source_topic") or "")
        if source_topic not in self._source_topics:
            raise ValueError(f"Retry envelope source_topic {source_topic!r} is not owned")
        original_value = envelope.get("original_value")
        if not isinstance(original_value, str) or not original_value:
            raise ValueError("Retry envelope has no original_value")

        if self._is_stale(source_topic, original_value):
            self._publish_retry_dlq(envelope, reason="retry_payload_stale")
            return

        retry_attempt = int(envelope.get("retry_attempt") or 1)
        key_text = envelope.get("original_key")
        key = key_text.encode("utf-8") if isinstance(key_text, str) else None
        headers = [
            (_RETRY_ATTEMPT_HEADER, str(retry_attempt).encode("utf-8")),
            (_RETRY_SOURCE_HEADER, str(envelope.get("failure_topic") or "").encode("utf-8")),
            (_RETRY_EVENT_HEADER, str(envelope.get("event_id") or "").encode("utf-8")),
        ]
        self._producer.produce(
            topic=source_topic,
            key=key,
            value=original_value.encode("utf-8"),
            headers=headers,
        )
        remaining = self._producer.flush(10)
        if remaining:
            raise RuntimeError(f"Retry replay to {source_topic} was not durably delivered")

        logger.info(
            "KafkaRetryReplayer replayed retry event_id=%s source_topic=%s attempt=%s",
            envelope.get("event_id"),
            source_topic,
            retry_attempt,
        )

    def _publish_retry_dlq(self, envelope: dict[str, Any], *, reason: str) -> None:
        source_topic = str(envelope["source_topic"])
        dlq_envelope = {
            **envelope,
            "failure_topic": dlq_topic(source_topic),
            "retry_replay_status": "DLQ",
            "retry_replay_reason": reason,
            "retry_replayed_at": datetime.now(UTC).isoformat(),
        }
        key_text = envelope.get("original_key")
        self._producer.produce(
            topic=dlq_topic(source_topic),
            key=key_text.encode("utf-8") if isinstance(key_text, str) else None,
            value=json.dumps(dlq_envelope, default=str).encode("utf-8"),
        )
        remaining = self._producer.flush(10)
        if remaining:
            raise RuntimeError(f"Retry DLQ publish to {dlq_topic(source_topic)} failed")

    def _publish_malformed_retry_dlq(
        self,
        *,
        retry_topic_name: str,
        key: bytes | None,
        raw_value: bytes,
        reason: str,
    ) -> None:
        source_topic = self._source_topic_from_retry_topic(retry_topic_name)
        if source_topic is None:
            raise ValueError(f"Retry topic {retry_topic_name!r} is not owned")

        dlq_envelope = {
            "event_id": None,
            "event_type": "KAFKA_RETRY_ENVELOPE_INVALID",
            "schema_version": SCHEMA_VERSION,
            "source": self._source_service,
            "published_time": datetime.now(UTC).isoformat(),
            "source_topic": source_topic,
            "failure_topic": dlq_topic(source_topic),
            "retry_topic": retry_topic_name,
            "reason": reason,
            "error_type": "retry_envelope_malformed",
            "retry_replay_status": "DLQ",
            "retry_replayed_at": datetime.now(UTC).isoformat(),
            "original_key": key.decode("utf-8", errors="replace") if key else None,
            "original_value": raw_value.decode("utf-8", errors="replace"),
        }
        self._producer.produce(
            topic=dlq_topic(source_topic),
            key=key,
            value=json.dumps(dlq_envelope, default=str).encode("utf-8"),
        )
        remaining = self._producer.flush(10)
        if remaining:
            raise RuntimeError(f"Malformed retry DLQ publish to {dlq_topic(source_topic)} failed")

    def _source_topic_from_retry_topic(self, retry_topic_name: str) -> str | None:
        if not retry_topic_name.endswith(".retry"):
            return None
        source_topic = retry_topic_name[: -len(".retry")]
        if source_topic not in self._source_topics:
            return None
        return source_topic

    def _is_stale(self, source_topic: str, original_value: str) -> bool:
        try:
            payload = json.loads(original_value)
        except json.JSONDecodeError:
            return False

        expires_at = payload.get("expires_at")
        if expires_at:
            return _parse_iso(expires_at) <= datetime.now(UTC)

        if source_topic.startswith("ticks."):
            exchange_time = payload.get("exchange_time")
            if not exchange_time:
                return False
            age_seconds = time.time() - _parse_iso(exchange_time).timestamp()
            return age_seconds > self._max_tick_retry_age_seconds

        return False

    def _build_consumer(self) -> Consumer:
        return Consumer(
            {
                **self._common_kafka_config(),
                "group.id": self._consumer_group,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
            }
        )

    def _build_producer(self) -> Producer:
        return Producer(
            {
                **self._common_kafka_config(),
                "acks": "all",
                "enable.idempotence": True,
                "max.in.flight.requests.per.connection": 1,
                "retries": 5,
                "retry.backoff.ms": 200,
                "delivery.timeout.ms": 30000,
                "log.connection.close": False,
            }
        )

    def _common_kafka_config(self) -> dict[str, Any]:
        region = self._aws_region
        return {
            "bootstrap.servers": self._bootstrap_servers,
            **get_kafka_auth_config(region),
            "socket.connection.setup.timeout.ms": 15000,
        }


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
