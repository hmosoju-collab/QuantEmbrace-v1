"""Shadow-flow integration test for the Alpha Engine (ADR-031, P2).

Drives the real ``AlphaEngineService._run_one_cycle`` end-to-end with in-memory
fakes (no LocalStack/Redpanda required — the repo's integration suite is fake-
backed). Proves the core guarantees:
  * every forecast (published or suppressed) is persisted to the forecast store,
  * publishing only ever reaches ``alpha.opportunities`` (real guarded path),
  * the kill switch pauses ALL output.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
import sys

import pytest

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
_UNIT_DIR = os.path.join(_PROJECT_ROOT, "tests", "unit")
for _p in (_SERVICES_DIR, _UNIT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _alpha_fakes import FakeTable  # noqa: E402

from alpha_engine.cost.cost_model import CostModel  # noqa: E402
from alpha_engine.gates.kill_switch_gate import KillSwitchGate  # noqa: E402
from alpha_engine.publishers.alpha_shadow_publisher import AlphaShadowPublisher  # noqa: E402
from alpha_engine.ranking.ranker import AlphaRanker  # noqa: E402
from alpha_engine.store.forecast_store import ForecastStore  # noqa: E402
from shared.models.alpha import AlphaForecast  # noqa: E402
from shared.models.signal import Direction  # noqa: E402
from shared.risk_state import kill_switch_resource_key  # noqa: E402

pytestmark = pytest.mark.asyncio

_TS = datetime(2026, 6, 15, 5, 0, 0, tzinfo=UTC)


class _FakeProducer:
    """Records every produce() call so we can assert the egress topic set."""

    def __init__(self) -> None:
        self.produced: list[tuple[str, bytes]] = []

    def produce(self, *, topic: str, key: bytes, value: bytes, **_) -> None:
        self.produced.append((topic, key))

    def poll(self, _timeout) -> int:
        return 0

    def flush(self, _timeout=10) -> int:
        return 0

    @property
    def topics(self) -> set[str]:
        return {t for t, _ in self.produced}


def _raw(symbol: str, horizon: int, forecast_return_bps: float, direction=Direction.BUY) -> AlphaForecast:
    return AlphaForecast(
        model_id="alpha_orb_v2",
        model_version="2026-06-13",
        alpha_family="momentum",
        symbol=symbol,
        market="NSE",
        universe="NIFTY50",
        direction=direction,
        timeframe="1m",
        horizon_minutes=horizon,
        forecast_return_bps=forecast_return_bps,
        confidence=0.85,
        decision_price=2500.0,
        decision_ts=_TS,
        trace_id="trace-flow",
    )


def _service(*, kill_switch_active: bool, publisher: AlphaShadowPublisher, forecast_table: FakeTable):
    """Build an AlphaEngineService with injected fakes (bypassing start())."""
    from alpha_engine.service import AlphaEngineService

    svc = AlphaEngineService.__new__(AlphaEngineService)
    svc._running = True
    svc._cost = CostModel()
    svc._ranker = AlphaRanker(top_n=10, min_net_edge_bps=50.0, forecast_ttl_seconds=180)
    svc._forecast_store = ForecastStore(table=forecast_table)

    ks_table = FakeTable()
    if kill_switch_active:
        key = kill_switch_resource_key()
        ks_table.put_item(Item={"PK": key["PK"], "SK": key["SK"], "active": True, "status": "ACTIVE"})
    svc._gate = KillSwitchGate(table=ks_table, poll_interval_seconds=0.0)
    svc._publisher = publisher

    class _Cfg:
        publish_enabled = True
        rank_interval_seconds = 60.0

    svc._cfg = _Cfg()
    return svc


async def _ready_publisher() -> tuple[AlphaShadowPublisher, _FakeProducer]:
    pub = AlphaShadowPublisher("localhost:9092")
    producer = _FakeProducer()
    pub._producer = producer  # inject — bypass real Kafka
    pub._running = True
    return pub, producer


async def test_one_cycle_persists_all_and_publishes_only_alpha_topic():
    pub, producer = await _ready_publisher()
    forecast_table = FakeTable()
    svc = _service(kill_switch_active=False, publisher=pub, forecast_table=forecast_table)

    # 3 horizons of a strong signal (net 90bps -> published) + a weak one
    # (net 40bps -> stored, suppressed by the 50bps floor).
    forecasts = [
        _raw("RELIANCE", 15, 110.0),
        _raw("RELIANCE", 30, 110.0),
        _raw("RELIANCE", 60, 110.0),
        _raw("TCS", 15, 60.0),
    ]

    async def _collect():
        return forecasts

    svc._collect_forecasts = _collect  # type: ignore[assignment]
    await svc._run_one_cycle(_TS)

    # All four forecasts persisted (published or suppressed).
    assert len(forecast_table) == 4
    # Exactly the 3 strong forecasts were published, all to alpha.opportunities.
    assert len(producer.produced) == 3
    assert producer.topics == {"alpha.opportunities"}
    # And nothing reached the trading path.
    assert "signals.pending" not in producer.topics
    assert "signals.approved" not in producer.topics
    assert "orders.events" not in producer.topics


async def test_kill_switch_pauses_all_output():
    pub, producer = await _ready_publisher()
    forecast_table = FakeTable()
    svc = _service(kill_switch_active=True, publisher=pub, forecast_table=forecast_table)

    async def _collect():
        return [_raw("RELIANCE", 15, 110.0)]

    svc._collect_forecasts = _collect  # type: ignore[assignment]
    await svc._run_one_cycle(_TS)

    assert len(forecast_table) == 0     # no store writes
    assert len(producer.produced) == 0  # no publishes
