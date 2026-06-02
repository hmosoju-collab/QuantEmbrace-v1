from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from execution_engine.consumers.kafka_approved_consumer import KafkaApprovedConsumer
from risk_engine.consumers.kafka_signal_consumer import KafkaSignalConsumer
from risk_engine.publishers.kafka_approved_publisher import KafkaApprovedPublisher
from shared.events.schemas import (
    CANONICAL_KILL_SWITCH_TOPIC,
    CANONICAL_ORDER_EVENTS_TOPIC,
    CANONICAL_SIGNALS_APPROVED_TOPIC,
    CANONICAL_SIGNALS_PENDING_TOPIC,
    EventType,
    validate_event,
)
from shared.models.signal import Direction, Signal
from strategy_engine.publishers.kafka_signal_publisher import KafkaSignalPublisher


class FakeMsg:
    def __init__(
        self,
        *,
        topic: str,
        value: dict | bytes,
        key: bytes = b"NSE:RELIANCE",
        offset: int = 42,
    ) -> None:
        self._topic = topic
        self._key = key
        self._value = json.dumps(value).encode("utf-8") if isinstance(value, dict) else value
        self._offset = offset

    def topic(self) -> str:
        return self._topic

    def key(self) -> bytes:
        return self._key

    def value(self) -> bytes:
        return self._value

    def offset(self) -> int:
        return self._offset

    def partition(self) -> int:
        return 0

    def error(self):
        return None


class FakeConsumer:
    def __init__(self, msg: FakeMsg | None = None) -> None:
        self.msg = msg
        self.committed: list[FakeMsg] = []

    def poll(self, _timeout: float):
        msg = self.msg
        self.msg = None
        return msg

    def commit(self, *, message, asynchronous: bool) -> None:
        assert asynchronous is False
        self.committed.append(message)


class FakeFailurePublisher:
    def __init__(self) -> None:
        self.dlq: list[dict] = []
        self.retry: list[dict] = []

    def publish_dlq(self, **kwargs) -> bool:
        self.dlq.append(kwargs)
        return True

    def publish_retry(self, **kwargs) -> bool:
        self.retry.append(kwargs)
        return True


def _sample_signal() -> Signal:
    return Signal(
        signal_id="input-uuid",
        symbol="RELIANCE",
        market="NSE",
        direction=Direction.BUY,
        quantity=10,
        confidence=0.91,
        strategy_name="orb_opening_range",
        generated_at=datetime.now(timezone.utc),
        price_at_signal=2500.0,
        stop_loss=2475.0,
        take_profit=2550.0,
        metadata={"strategy_id": "orb-v1", "product_type": "MIS"},
    )


def test_strategy_risk_execution_signal_round_trip_preserves_protective_levels():
    strategy_pub = object.__new__(KafkaSignalPublisher)
    pending = strategy_pub._build_signal_event(_sample_signal(), trace_id="trace-1")

    assert validate_event(pending, EventType.SIGNAL_PENDING) == []
    assert pending["event_type"] == "SIGNAL_PENDING"
    assert pending["schema_version"] == "3.0"
    assert pending["expires_at"]
    assert pending["strategy_id"] == "orb-v1"
    assert pending["product_type"] == "MIS"
    assert pending["stop_loss"] == 2475.0
    assert pending["take_profit"] == 2550.0

    risk_consumer = object.__new__(KafkaSignalConsumer)
    risk_consumer._failure_publisher = None
    risk_consumer._last_failure_routed = False
    signal_event = risk_consumer._parse_message(
        FakeMsg(topic=CANONICAL_SIGNALS_PENDING_TOPIC, value=pending)
    )

    assert signal_event is not None
    assert signal_event.signal.stop_loss == 2475.0
    assert signal_event.signal.take_profit == 2550.0
    assert signal_event.signal.metadata["strategy_id"] == "orb-v1"
    assert signal_event.product_type == "MIS"

    approved_pub = object.__new__(KafkaApprovedPublisher)
    approved = approved_pub._build_event(
        signal_event.signal,
        risk_decision_id="risk-123",
        trace_id=signal_event.trace_id,
    )

    assert validate_event(approved, EventType.SIGNAL_APPROVED) == []
    assert approved["stop_loss"] == 2475.0
    assert approved["take_profit"] == 2550.0
    assert approved["product_type"] == "MIS"
    assert approved["strategy_id"] == "orb-v1"
    assert approved["expires_at"] == pending["expires_at"]

    execution_consumer = object.__new__(KafkaApprovedConsumer)
    execution_consumer._failure_publisher = None
    execution_consumer._last_failure_routed = False
    execution_event = execution_consumer._parse_message(
        FakeMsg(topic=CANONICAL_SIGNALS_APPROVED_TOPIC, value=approved)
    )

    assert execution_event is not None
    assert execution_event.stop_loss == 2475.0
    assert execution_event.take_profit == 2550.0
    assert execution_event.product_type == "MIS"
    assert execution_event.strategy_id == "orb-v1"


def test_order_event_schema_requires_protective_and_strategy_fields():
    now = datetime.now(timezone.utc).isoformat()
    order_event = {
        "event_id": "evt-1",
        "trace_id": "trace-1",
        "event_type": "ORDER_FILLED",
        "schema_version": "3.0",
        "source": "execution_engine",
        "published_time": now,
        "order_id": "ord-1",
        "signal_id": "sig-1",
        "risk_decision_id": "risk-1",
        "strategy_id": "orb-v1",
        "instrument_id": "NSE:RELIANCE",
        "market": "NSE",
        "direction": "BUY",
        "quantity_ordered": 10,
        "quantity_filled": 10,
        "avg_fill_price": 2501.0,
        "broker_order_id": "kite-1",
        "fill_time": now,
        "reject_reason": None,
        "product_type": "MIS",
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        "stop_loss": 2475.0,
        "take_profit": 2550.0,
    }

    assert validate_event(order_event, EventType.ORDER_FILLED) == []

    broken = dict(order_event)
    broken.pop("stop_loss")
    errors = validate_event(broken, EventType.ORDER_FILLED)
    assert errors
    assert "stop_loss" in errors[0]


def test_manual_commit_is_disabled_for_all_phase2_consumers(monkeypatch):
    captured: list[dict] = []

    class CaptureConsumer:
        def __init__(self, conf: dict) -> None:
            captured.append(conf)

    modules = [
        "strategy_engine.consumers.kafka_tick_consumer",
        "risk_engine.consumers.kafka_signal_consumer",
        "risk_engine.consumers.kafka_order_events_consumer",
        "execution_engine.consumers.kafka_approved_consumer",
        "risk_engine.killswitch.kafka_kill_switch_listener",
    ]

    for module_name in modules:
        module = __import__(module_name, fromlist=["Consumer"])
        monkeypatch.setattr(module, "Consumer", CaptureConsumer, raising=False)

    from execution_engine.consumers.kafka_approved_consumer import KafkaApprovedConsumer
    from risk_engine.consumers.kafka_order_events_consumer import KafkaOrderEventsConsumer
    from risk_engine.consumers.kafka_signal_consumer import KafkaSignalConsumer
    from risk_engine.killswitch.kafka_kill_switch_listener import KafkaKillSwitchListener
    from strategy_engine.consumers.kafka_tick_consumer import KafkaTickConsumer

    for cls in [
        KafkaTickConsumer,
        KafkaSignalConsumer,
        KafkaOrderEventsConsumer,
        KafkaApprovedConsumer,
        KafkaKillSwitchListener,
    ]:
        consumer = object.__new__(cls)
        consumer._bootstrap_servers = "broker:9098"
        consumer._aws_region = "ap-south-1"
        consumer._consumer_group = "test-group"
        consumer._poll_timeout = 0.1
        if cls is KafkaKillSwitchListener:
            consumer._kill_switch = SimpleNamespace()
        consumer._build_consumer()

    assert captured
    for conf in captured:
        assert conf["enable.auto.commit"] is False
        assert conf["enable.auto.offset.store"] is False


def test_malformed_signal_is_routed_to_dlq_then_committed():
    malformed = {
        "event_id": "evt-bad",
        "event_type": "SIGNAL_PENDING",
        "schema_version": "3.0",
    }
    msg = FakeMsg(topic=CANONICAL_SIGNALS_PENDING_TOPIC, value=malformed)
    fake_consumer = FakeConsumer(msg)
    failure_publisher = FakeFailurePublisher()

    consumer = object.__new__(KafkaSignalConsumer)
    consumer._consumer = fake_consumer
    consumer._running = True
    consumer._poll_timeout = 0.1
    consumer._failure_publisher = failure_publisher
    consumer._last_failure_routed = False

    assert consumer.poll_signal() is None
    assert len(failure_publisher.dlq) == 1
    assert failure_publisher.dlq[0]["source_topic"] == CANONICAL_SIGNALS_PENDING_TOPIC
    assert fake_consumer.committed == [msg]


def test_canonical_topic_names_are_single_source_of_truth():
    assert CANONICAL_SIGNALS_PENDING_TOPIC == "signals.pending"
    assert CANONICAL_SIGNALS_APPROVED_TOPIC == "signals.approved"
    assert CANONICAL_ORDER_EVENTS_TOPIC == "orders.events"
    assert CANONICAL_KILL_SWITCH_TOPIC == "risk.kill-switch"
