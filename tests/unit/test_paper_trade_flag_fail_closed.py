"""F-1 (docs/architecture/current-state.md §10): paper_trade must be a real JSON
boolean at every signal boundary.

Every consumer reads the flag with ``bool(...)``, so a present-but-null value
would route LIVE (``bool(None) is False``) and a string would mis-route. The
schema validator refuses anything but a boolean, so such a message is sent to
the DLQ and never approved or executed. A missing flag was already refused
(``paper_trade`` is in SIGNAL_REQUIRED_FIELDS).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from execution_engine.consumers.kafka_approved_consumer import KafkaApprovedConsumer
from risk_engine.consumers.kafka_signal_consumer import KafkaSignalConsumer
from risk_engine.publishers.kafka_approved_publisher import KafkaApprovedPublisher
from shared.events.schemas import (
    CANONICAL_SIGNALS_APPROVED_TOPIC,
    CANONICAL_SIGNALS_PENDING_TOPIC,
    EventType,
    validate_event,
)
from shared.models.signal import Direction, Signal
from strategy_engine.publishers.kafka_signal_publisher import KafkaSignalPublisher

BAD_VALUES = [None, "false", "true", 0, 1]


class _Msg:
    def __init__(self, topic: str, value: dict) -> None:
        self._topic, self._value = topic, json.dumps(value).encode("utf-8")

    def topic(self):
        return self._topic

    def key(self):
        return b"NSE:RELIANCE"

    def value(self):
        return self._value

    def offset(self):
        return 7

    def partition(self):
        return 0


class _Dlq:
    def __init__(self) -> None:
        self.dlq: list[dict] = []

    def publish_dlq(self, **kwargs) -> bool:
        self.dlq.append(kwargs)
        return True


def _pending(paper_trade: bool = True) -> dict:
    signal = Signal(
        signal_id="sig-1",
        symbol="RELIANCE",
        market="NSE",
        direction=Direction.BUY,
        quantity=10,
        confidence=0.9,
        strategy_name="orb_opening_range",
        generated_at=datetime.now(timezone.utc),
        price_at_signal=2500.0,
        stop_loss=2475.0,
        take_profit=2550.0,
        paper_trade=paper_trade,
        metadata={"strategy_id": "orb-v1", "product_type": "MIS"},
    )
    return object.__new__(KafkaSignalPublisher)._build_signal_event(signal, trace_id="t-1")


def _approved(paper_trade: bool = True) -> dict:
    consumer = object.__new__(KafkaSignalConsumer)
    consumer._failure_publisher = None
    consumer._last_failure_routed = False
    event = consumer._parse_message(_Msg(CANONICAL_SIGNALS_PENDING_TOPIC, _pending(paper_trade)))
    return object.__new__(KafkaApprovedPublisher)._build_event(
        event.signal, risk_decision_id="risk-1", trace_id="t-1"
    )


def _enriched() -> dict:
    return _pending() | {
        "event_type": "SIGNAL_ENRICHED",
        "schema_version": "4.0",
        "regime": "unknown",
        "regime_confidence": 0.0,
        "quality_score": 0.5,
        "filtered": False,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
        "enrichment_latency_ms": 1.0,
        "model_versions": {},
    }


@pytest.mark.parametrize("flag", [True, False])
def test_boolean_flag_is_accepted_everywhere(flag):
    assert validate_event(_pending(flag), EventType.SIGNAL_PENDING) == []
    assert validate_event(_approved(flag), EventType.SIGNAL_APPROVED) == []


@pytest.mark.parametrize("bad", BAD_VALUES)
@pytest.mark.parametrize(
    "build,kind",
    [
        (_pending, EventType.SIGNAL_PENDING),
        (_approved, EventType.SIGNAL_APPROVED),
        (_enriched, EventType.SIGNAL_ENRICHED),
    ],
)
def test_non_boolean_flag_is_refused(build, kind, bad):
    event = build() | {"paper_trade": bad}
    errors = validate_event(event, kind)
    assert any("paper_trade must be a JSON boolean" in e for e in errors)


def test_missing_flag_is_still_refused():
    event = _approved()
    event.pop("paper_trade")
    assert any("paper_trade" in e for e in validate_event(event, EventType.SIGNAL_APPROVED))


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_execution_consumer_never_builds_a_live_event_from_a_bad_flag(bad):
    consumer = object.__new__(KafkaApprovedConsumer)
    consumer._failure_publisher = _Dlq()
    consumer._last_failure_routed = False
    msg = _Msg(CANONICAL_SIGNALS_APPROVED_TOPIC, _approved() | {"paper_trade": bad})
    assert consumer._parse_message(msg) is None  # refused, not executed
    assert consumer._failure_publisher.dlq and consumer._last_failure_routed


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_risk_consumer_refuses_a_bad_flag(bad):
    consumer = object.__new__(KafkaSignalConsumer)
    consumer._failure_publisher = _Dlq()
    consumer._last_failure_routed = False
    msg = _Msg(CANONICAL_SIGNALS_PENDING_TOPIC, _pending() | {"paper_trade": bad})
    assert consumer._parse_message(msg) is None
    assert consumer._failure_publisher.dlq
