from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json

from strategy_engine.service import StrategyEngineService
from strategy_engine.strategies.base_strategy import StrategyState


class _FakeTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}

    def put_item(self, *, Item: dict) -> None:
        self.items[(Item["strategy_name"], Item["symbol"])] = Item

    def get_item(self, *, Key: dict) -> dict:
        item = self.items.get((Key["strategy_name"], Key["symbol"]))
        return {"Item": item} if item is not None else {}


class _FakeDynamo:
    def __init__(self, table: _FakeTable) -> None:
        self.table = table
        self.table_names: list[str] = []

    def Table(self, table_name: str) -> _FakeTable:
        self.table_names.append(table_name)
        return self.table


class _AwsSettings:
    dynamodb_table_prefix = "qe-test"


class _Settings:
    aws = _AwsSettings()


def _service() -> StrategyEngineService:
    svc = object.__new__(StrategyEngineService)
    svc._settings = _Settings()
    return svc


def test_strategy_state_put_uses_global_symbol_range_key(monkeypatch) -> None:
    table = _FakeTable()
    fake_dynamo = _FakeDynamo(table)
    monkeypatch.setattr("strategy_engine.service.get_dynamodb_resource", lambda: fake_dynamo)
    svc = _service()
    state = StrategyState(
        strategy_name="nse_orb_15m",
        positions={"RELIANCE": 1},
        indicators={"atr": 12.5},
        last_signal_time=datetime(2026, 5, 9, 9, 30, tzinfo=UTC),
        custom_state={"phase": "normal"},
    )

    asyncio.run(svc._save_strategy_state("nse_orb_15m", state))

    item = table.items[("nse_orb_15m", "__GLOBAL__")]
    assert fake_dynamo.table_names == ["qe-test-strategy-state"]
    assert item["strategy_name"] == "nse_orb_15m"
    assert item["symbol"] == "__GLOBAL__"
    assert item["schema_version"] == "1"
    payload = json.loads(item["state"])
    assert payload["positions"] == {"RELIANCE": 1}
    assert payload["indicators"] == {"atr": 12.5}
    assert payload["last_signal_time"] == "2026-05-09T09:30:00+00:00"


def test_strategy_state_load_round_trips_strategy_state(monkeypatch) -> None:
    table = _FakeTable()
    fake_dynamo = _FakeDynamo(table)
    monkeypatch.setattr("strategy_engine.service.get_dynamodb_resource", lambda: fake_dynamo)
    svc = _service()
    original = StrategyState(
        strategy_name="nse_scalp_1m",
        positions={"INFY": 2},
        indicators={"ema": 1450.25},
        last_signal_time=datetime(2026, 5, 9, 10, 0, tzinfo=UTC),
        custom_state={"ready": True},
    )

    asyncio.run(svc._save_strategy_state("nse_scalp_1m", original))
    restored = asyncio.run(svc._load_strategy_state("nse_scalp_1m"))

    assert isinstance(restored, StrategyState)
    assert restored.strategy_name == original.strategy_name
    assert restored.positions == original.positions
    assert restored.indicators == original.indicators
    assert restored.last_signal_time == original.last_signal_time
    assert restored.custom_state == original.custom_state
