"""Unit tests for the AlphaRanker (ADR-031, P1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
import sys

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from alpha_engine.cost.cost_model import CostModel  # noqa: E402
from alpha_engine.ranking.ranker import (  # noqa: E402
    SUPPRESS_EDGE_FLOOR,
    SUPPRESS_TOP_N_CAP,
    AlphaRanker,
)
from shared.models.alpha import AlphaForecast  # noqa: E402
from shared.models.signal import Direction  # noqa: E402

_CYCLE_TS = datetime(2026, 6, 15, 5, 0, 0, tzinfo=UTC)
_COST = CostModel()


def _f(
    *,
    symbol: str = "RELIANCE",
    direction: Direction = Direction.BUY,
    net_edge_bps: float = 60.0,
    confidence: float = 0.8,
    model_id: str = "alpha_orb_v2",
    horizon: int = 15,
    decision_ts: datetime | None = None,
) -> AlphaForecast:
    """Build a cost-applied forecast with a target net edge (raw = net + 20bps)."""
    raw = AlphaForecast(
        model_id=model_id,
        model_version="2026-06-13",
        alpha_family="momentum",
        symbol=symbol,
        market="NSE",
        universe="NIFTY50",
        direction=direction,
        timeframe="1m",
        horizon_minutes=horizon,
        forecast_return_bps=net_edge_bps + 20.0,
        confidence=confidence,
        decision_price=2500.0,
        decision_ts=decision_ts or _CYCLE_TS,
        trace_id="t",
    )
    return _COST.apply(raw)


def test_edge_floor_just_below_is_suppressed():
    ranker = AlphaRanker(top_n=10, min_net_edge_bps=50.0, forecast_ttl_seconds=180)
    res = ranker.rank([_f(net_edge_bps=49.9)], _CYCLE_TS)
    assert len(res.opportunities) == 1
    opp = res.opportunities[0]
    assert opp.published is False
    assert opp.suppressed_by == SUPPRESS_EDGE_FLOOR
    assert res.published_opportunities == []


def test_edge_floor_at_threshold_is_published():
    ranker = AlphaRanker(top_n=10, min_net_edge_bps=50.0, forecast_ttl_seconds=180)
    res = ranker.rank([_f(net_edge_bps=50.0)], _CYCLE_TS)
    opp = res.opportunities[0]
    assert opp.published is True
    assert opp.suppressed_by == ""
    assert len(res.published_opportunities) == 1


def test_ttl_expiry_drops_old_forecasts():
    ranker = AlphaRanker(top_n=10, min_net_edge_bps=50.0, forecast_ttl_seconds=180)
    stale = _f(decision_ts=_CYCLE_TS - timedelta(seconds=300))
    fresh = _f(decision_ts=_CYCLE_TS - timedelta(seconds=60))
    res = ranker.rank([stale, fresh], _CYCLE_TS)
    assert res.expired_count == 1
    assert len(res.opportunities) == 1
    assert res.opportunities[0].forecast.forecast_id == fresh.forecast_id


def test_top_n_cap_suppresses_lower_scored_eligible():
    ranker = AlphaRanker(top_n=1, min_net_edge_bps=50.0, forecast_ttl_seconds=180)
    high = _f(symbol="RELIANCE", confidence=0.9, net_edge_bps=80.0)
    low = _f(symbol="TCS", confidence=0.6, net_edge_bps=60.0)
    res = ranker.rank([low, high], _CYCLE_TS)
    published = res.published_opportunities
    assert len(published) == 1
    assert published[0].forecast.symbol == "RELIANCE"
    # the eligible-but-capped one is retained, flagged top_n_cap
    capped = next(o for o in res.opportunities if o.forecast.symbol == "TCS")
    assert capped.published is False
    assert capped.suppressed_by == SUPPRESS_TOP_N_CAP


def test_ranks_are_sorted_by_score_descending():
    ranker = AlphaRanker(top_n=10, min_net_edge_bps=0.0, forecast_ttl_seconds=180)
    a = _f(symbol="A", confidence=0.9, net_edge_bps=100.0)  # score 90
    b = _f(symbol="B", confidence=0.5, net_edge_bps=100.0)  # score 50
    res = ranker.rank([b, a], _CYCLE_TS)
    assert [o.forecast.symbol for o in res.opportunities] == ["A", "B"]
    assert [o.rank for o in res.opportunities] == [1, 2]


def test_conflict_pairs_both_kept_with_shared_group_id():
    """Opposite-direction forecasts on one symbol are both kept and co-tagged (#7)."""
    ranker = AlphaRanker(top_n=10, min_net_edge_bps=0.0, forecast_ttl_seconds=180)
    buy = _f(symbol="RELIANCE", direction=Direction.BUY, model_id="alpha_orb_v2")
    sell = _f(symbol="RELIANCE", direction=Direction.SELL, model_id="alpha_vwap_rev_v2")
    solo = _f(symbol="TCS", direction=Direction.BUY, model_id="alpha_orb_v2")
    res = ranker.rank([buy, sell, solo], _CYCLE_TS)

    by_id = {o.forecast.forecast_id: o for o in res.opportunities}
    cg_buy = by_id[buy.forecast_id].conflict_group_id
    cg_sell = by_id[sell.forecast_id].conflict_group_id
    cg_solo = by_id[solo.forecast_id].conflict_group_id

    assert len(res.opportunities) == 3  # nothing dropped
    assert cg_buy is not None and cg_buy == cg_sell
    assert cg_solo is None


def test_same_symbol_same_direction_multi_horizon_is_not_conflict():
    ranker = AlphaRanker(top_n=10, min_net_edge_bps=0.0, forecast_ttl_seconds=180)
    h15 = _f(symbol="RELIANCE", direction=Direction.BUY, horizon=15)
    h30 = _f(symbol="RELIANCE", direction=Direction.BUY, horizon=30)
    res = ranker.rank([h15, h30], _CYCLE_TS)
    assert all(o.conflict_group_id is None for o in res.opportunities)


def test_cycle_id_is_deterministic():
    ranker = AlphaRanker(top_n=10, min_net_edge_bps=50.0, forecast_ttl_seconds=180)
    forecasts = [_f(symbol="A"), _f(symbol="B")]
    r1 = ranker.rank(list(forecasts), _CYCLE_TS)
    r2 = ranker.rank(list(reversed(forecasts)), _CYCLE_TS)
    assert r1.cycle_id == r2.cycle_id  # order-independent over forecast ids
