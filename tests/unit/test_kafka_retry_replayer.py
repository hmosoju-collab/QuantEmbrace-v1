from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from typing import Any

from shared.kafka.failure_publisher import KafkaFailurePublisher
from shared.kafka.retry_replayer import KafkaRetryReplayer


class _FakeProducer:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def produce(self, **kwargs: Any) -> None:
        self.records.append(kwargs)

    def poll(self, _timeout: float) -> None:
        return None

    def flush(self, _timeout: float) -> int:
        return 0


def _replayer() -> KafkaRetryReplayer:
    replayer = object.__new__(KafkaRetryReplayer)
    replayer._source_topics = ["signals.pending", "ticks.nse"]
    replayer._source_service = "risk_engine"
    replayer._producer = _FakeProducer()
    replayer._max_tick_retry_age_seconds = 5.0
    return replayer


def test_retry_replayer_republishes_original_payload_to_source_topic() -> None:
    replayer = _replayer()
    payload = {
        "event_type": "SIGNAL_PENDING",
        "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
    }
    envelope = {
        "event_id": "retry-1",
        "source_topic": "signals.pending",
        "failure_topic": "signals.pending.retry",
        "retry_attempt": 2,
        "original_key": "NSE:RELIANCE",
        "original_value": json.dumps(payload),
    }

    replayer._handle_envelope(envelope)

    record = replayer._producer.records[0]
    assert record["topic"] == "signals.pending"
    assert record["key"] == b"NSE:RELIANCE"
    assert json.loads(record["value"].decode("utf-8")) == payload
    assert ("qe-retry-attempt", b"2") in record["headers"]


def test_retry_replayer_dlqs_stale_expiring_payload() -> None:
    replayer = _replayer()
    envelope = {
        "event_id": "retry-stale",
        "source_topic": "signals.pending",
        "failure_topic": "signals.pending.retry",
        "retry_attempt": 1,
        "original_key": "NSE:RELIANCE",
        "original_value": json.dumps(
            {
                "event_type": "SIGNAL_PENDING",
                "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            }
        ),
    }

    replayer._handle_envelope(envelope)

    record = replayer._producer.records[0]
    assert record["topic"] == "signals.pending.dlq"
    dlq_payload = json.loads(record["value"].decode("utf-8"))
    assert dlq_payload["retry_replay_status"] == "DLQ"
    assert dlq_payload["retry_replay_reason"] == "retry_payload_stale"


def test_retry_replayer_dlqs_malformed_retry_envelope() -> None:
    replayer = _replayer()

    replayer._publish_malformed_retry_dlq(
        retry_topic_name="signals.pending.retry",
        key=b"NSE:RELIANCE",
        raw_value=b"{not-json",
        reason="invalid json",
    )

    record = replayer._producer.records[0]
    assert record["topic"] == "signals.pending.dlq"
    assert record["key"] == b"NSE:RELIANCE"
    dlq_payload = json.loads(record["value"].decode("utf-8"))
    assert dlq_payload["event_type"] == "KAFKA_RETRY_ENVELOPE_INVALID"
    assert dlq_payload["retry_topic"] == "signals.pending.retry"
    assert dlq_payload["retry_replay_status"] == "DLQ"


def test_failure_publisher_escalates_exhausted_retry_to_dlq() -> None:
    publisher = object.__new__(KafkaFailurePublisher)
    publisher._producer = _FakeProducer()
    publisher._source_service = "risk_engine"
    publisher._max_retry_attempts = 2

    ok = publisher.publish_retry(
        source_topic="signals.pending",
        key=b"NSE:RELIANCE",
        value=b'{"event_type":"SIGNAL_PENDING"}',
        reason="still failing",
        error_type="risk_processing_failed",
        headers=[("qe-retry-attempt", b"2")],
    )

    assert ok is True
    record = publisher._producer.records[0]
    assert record["topic"] == "signals.pending.dlq"
    envelope = json.loads(record["value"].decode("utf-8"))
    assert envelope["retry_attempt"] == 3
    assert envelope["error_type"] == "risk_processing_failed_retry_exhausted"
