"""Unit tests for the Alpha Engine CostModel (ADR-031, P1)."""

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

from alpha_engine.cost.cost_model import (  # noqa: E402
    TOTAL_ROUND_TRIP_BPS,
    CostModel,
    classify_edge_band,
)
from shared.models.alpha import AlphaForecast  # noqa: E402
from shared.models.signal import Direction  # noqa: E402
from strategy_engine.strategies._viability import ROUND_TRIP_COST_PCT  # noqa: E402

_TS = datetime(2026, 6, 15, 4, 0, 0, tzinfo=UTC)


def _raw_forecast(forecast_return_bps: float) -> AlphaForecast:
    return AlphaForecast(
        model_id="alpha_orb_v2",
        model_version="2026-06-13",
        alpha_family="momentum",
        symbol="RELIANCE",
        market="NSE",
        universe="NIFTY50",
        direction=Direction.BUY,
        timeframe="1m",
        horizon_minutes=15,
        forecast_return_bps=forecast_return_bps,
        confidence=0.8,
        decision_price=2500.0,
        decision_ts=_TS,
        trace_id="trace-abc",
    )


def test_cost_drift_guard_matches_viability_constant():
    """The single source of truth for round-trip cost must not silently diverge."""
    est = CostModel().estimate(symbol="RELIANCE", market="NSE", price=2500.0)
    assert est.total_round_trip_bps == pytest.approx(ROUND_TRIP_COST_PCT * 100.0)
    assert TOTAL_ROUND_TRIP_BPS == pytest.approx(20.0)


def test_breakdown_sums_to_total():
    est = CostModel().estimate(symbol="X", market="NSE", price=100.0)
    assert (
        est.expected_spread_bps + est.expected_slippage_bps + est.fees_taxes_bps
        == pytest.approx(est.total_round_trip_bps)
    )


def test_apply_computes_net_edge():
    out = CostModel().apply(_raw_forecast(110.0))
    assert out.net_edge_bps == pytest.approx(90.0)  # 110 - 20
    assert out.edge_band == ">=50"


def test_apply_preserves_forecast_id():
    raw = _raw_forecast(110.0)
    out = CostModel().apply(raw)
    assert out.forecast_id == raw.forecast_id  # costing must not change identity


def test_apply_negative_edge():
    out = CostModel().apply(_raw_forecast(10.0))
    assert out.net_edge_bps == pytest.approx(-10.0)
    assert out.edge_band == "<20"


@pytest.mark.parametrize(
    "net_edge,expected",
    [
        (-5.0, "<20"),
        (0.0, "<20"),
        (19.999, "<20"),
        (20.0, "20-30"),
        (29.999, "20-30"),
        (30.0, "30-40"),
        (39.999, "30-40"),
        (40.0, "40-50"),
        (49.999, "40-50"),
        (50.0, ">=50"),
        (200.0, ">=50"),
    ],
)
def test_edge_band_boundaries(net_edge, expected):
    assert classify_edge_band(net_edge) == expected
