from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from data_ingestion.connectors.base import Market, NormalizedTick
from data_ingestion.publishers.kafka_tick_publisher import KafkaTickPublisher
from strategy_engine.consumers.kafka_tick_consumer import KafkaTickConsumer, TickEvent
from strategy_engine.service import StrategyEngineService


class _FakeMsg:
    def __init__(self, value: dict, topic: str = "ticks.nse") -> None:
        self._value = json.dumps(value).encode("utf-8")
        self._topic = topic

    def value(self) -> bytes:
        return self._value

    def topic(self) -> str:
        return self._topic

    def key(self) -> bytes:
        return b"NSE:RELIANCE"

    def offset(self) -> int:
        return 7

    def partition(self) -> int:
        return 0


def _publisher() -> KafkaTickPublisher:
    publisher = object.__new__(KafkaTickPublisher)
    publisher._sequence_lock = threading.Lock()
    publisher._sequence_id = 0
    return publisher


def _consumer() -> KafkaTickConsumer:
    consumer = object.__new__(KafkaTickConsumer)
    consumer._failure_publisher = None
    consumer._last_failure_routed = False
    return consumer


def _gap_tick() -> NormalizedTick:
    return NormalizedTick(
        symbol="RELIANCE",
        market=Market.NSE,
        last_price=2500.0,
        bid=2499.5,
        ask=2500.5,
        volume=100,
        timestamp=datetime(2026, 5, 10, 9, 20, tzinfo=UTC),
        broker="zerodha",
        gap_detected=True,
    )


def test_tick_kafka_event_preserves_reconnect_gap_flag() -> None:
    event = _publisher()._build_event(_gap_tick(), "NSE:RELIANCE", "ticks.nse")

    assert event["gap_detected"] is True

    parsed = _consumer()._parse_message(_FakeMsg(event))

    assert parsed is not None
    assert parsed.gap_detected is True
    assert parsed.symbol == "RELIANCE"


def test_tick_consumer_defaults_missing_gap_flag_to_false() -> None:
    event = _publisher()._build_event(_gap_tick(), "NSE:RELIANCE", "ticks.nse")
    event.pop("gap_detected")

    parsed = _consumer()._parse_message(_FakeMsg(event))

    assert parsed is not None
    assert parsed.gap_detected is False


def test_strategy_service_suppresses_signals_for_reconnect_gap_tick(monkeypatch) -> None:
    svc = object.__new__(StrategyEngineService)
    svc.process_tick = AsyncMock(return_value=[])
    metric_counts: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(
        "strategy_engine.service._metrics",
        SimpleNamespace(
            record_count=lambda name, dimensions=None: metric_counts.append(
                (name, dimensions)
            ),
            record_latency=lambda *args, **kwargs: None,
        ),
    )
    tick = TickEvent(
        symbol="RELIANCE",
        market="NSE",
        price=2500.0,
        volume=100,
        timestamp=datetime(2026, 5, 10, 9, 20, tzinfo=UTC),
        trace_id="trace-gap",
        sequence_id=42,
        gap_detected=True,
        raw_topic="ticks.nse",
        raw_offset=7,
        raw_message=object(),
    )

    asyncio.run(svc._handle_kafka_tick(tick))

    svc.process_tick.assert_awaited_once()
    assert svc.process_tick.call_args.kwargs["suppress_signals"] is True
    assert metric_counts == [
        ("ReconnectGapTicksSuppressed", {"Market": "NSE"}),
    ]
