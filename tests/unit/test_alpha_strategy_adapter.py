"""Unit tests for StrategyAlphaAdapter (ADR-031, P3)."""

from __future__ import annotations

from datetime import UTC, datetime
import os
import sys

import pytest

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from alpha_engine.models.strategy_alpha_adapter import StrategyAlphaAdapter  # noqa: E402
from shared.models.signal import Direction, Signal  # noqa: E402
from strategy_engine.strategies.base_strategy import Bar  # noqa: E402

pytestmark = pytest.mark.asyncio

_BAR_TS = datetime(2026, 6, 15, 4, 5, 0, tzinfo=UTC)


class _StubStrategy:
    """Minimal BaseStrategy-shaped stub: emits a preset signal once."""

    market = "NSE"

    def __init__(self, signal: Signal | None) -> None:
        self._signal = signal
        self.bars_seen = 0

    async def on_bar(self, bar: Bar) -> None:
        self.bars_seen += 1

    async def generate_signal(self) -> Signal | None:
        return self._signal


def _bar() -> Bar:
    return Bar(
        symbol="RELIANCE", market="NSE", open=2500, high=2520, low=2495,
        close=2515, volume=10000, timestamp=_BAR_TS, interval="minute",
    )


def _signal() -> Signal:
    return Signal(
        symbol="RELIANCE", market="NSE", direction=Direction.BUY, quantity=10,
        confidence=0.82, strategy_name="alpha_orb_v2", price_at_signal=2500.0,
        stop_loss=2480.0, take_profit=2530.0,  # +1.2% target -> 120 bps raw
        metadata={
            "vol_ratio": 1.8, "or_range": 12.5, "strategy_version": "orb_v2",
            "viability_net_edge_pct": 1.0, "paper_trade": True,
        },
    )


def _adapter(strategy, *, horizons=(15, 30, 60), universe="NIFTY50"):
    return StrategyAlphaAdapter(
        strategy,
        model_id="alpha_orb_v2",
        model_version="2026-06-13",
        alpha_family="momentum",
        timeframe="1m",
        horizons_minutes=list(horizons),
        feature_keys=["vol_ratio", "or_range"],
        universe_resolver=lambda _s: universe,
    )


async def test_none_signal_yields_no_forecasts():
    out = await _adapter(_StubStrategy(None)).on_bar(_bar())
    assert out == []


async def test_one_signal_fans_out_one_forecast_per_horizon():
    out = await _adapter(_StubStrategy(_signal())).on_bar(_bar())
    assert [f.horizon_minutes for f in out] == [15, 30, 60]
    # distinct ids per horizon, same decision identity otherwise
    assert len({f.forecast_id for f in out}) == 3
    assert {f.symbol for f in out} == {"RELIANCE"}
    assert {f.direction for f in out} == {Direction.BUY}


async def test_forecast_return_bps_is_take_profit_distance():
    out = await _adapter(_StubStrategy(_signal())).on_bar(_bar())
    # |2530 - 2500| / 2500 * 10000 = 120 bps
    assert out[0].forecast_return_bps == pytest.approx(120.0)


async def test_decision_ts_is_bar_close_not_wall_clock():
    """forecast_id must be idempotent across replays -> decision_ts = bar.timestamp."""
    out = await _adapter(_StubStrategy(_signal())).on_bar(_bar())
    assert out[0].decision_ts == _BAR_TS


async def test_explainability_features_extracted():
    out = await _adapter(_StubStrategy(_signal())).on_bar(_bar())
    feats = {fc.feature: fc.value for fc in out[0].top_features}
    assert feats == {"vol_ratio": 1.8, "or_range": 12.5}
    assert out[0].explainability_version == "heuristic-v1"


async def test_universe_attribution_applied():
    out = await _adapter(_StubStrategy(_signal()), universe="NIFTY50").on_bar(_bar())
    assert out[0].universe == "NIFTY50"


async def test_quantity_and_sizing_are_discarded():
    out = await _adapter(_StubStrategy(_signal())).on_bar(_bar())
    f = out[0]
    # AlphaForecast carries no quantity field; viability metadata is preserved.
    assert not hasattr(f, "quantity")
    assert f.metadata["take_profit"] == 2530.0
    assert f.metadata["stop_loss"] == 2480.0
