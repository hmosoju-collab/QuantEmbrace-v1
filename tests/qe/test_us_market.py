"""ADR-041 Phase 3 — qe US market support: costs, tz dispatch, benchmark, NAV
currency. NSE behavior must be provably unchanged (see the "NSE unchanged"
assertions in each test) — Phase 3 must not perturb the ₹0.00 parity path.
"""

from datetime import date

import pandas as pd
import pytest

from qe.clock import IST, NY, market_tz
from qe.costs import EquityDeliveryCosts, USEquityCosts, cost_model_for_market
from qe.data.panel import Panel
from qe.engine.sim import month_end_positions, rebalance_schedule
from qe.reporting.session_report import render_session_report
from qe.research.benchmark import buy_hold_benchmark_return

# ── market_tz / cost_model_for_market dispatch ───────────────────────────────


def test_market_tz_dispatch():
    assert market_tz("NSE") is IST
    assert market_tz("US") is NY
    with pytest.raises(ValueError, match="no timezone registered"):
        market_tz("LSE")


def test_cost_model_for_market_dispatch():
    assert isinstance(cost_model_for_market("NSE"), EquityDeliveryCosts)
    assert isinstance(cost_model_for_market("US"), USEquityCosts)
    with pytest.raises(ValueError, match="no cost model registered"):
        cost_model_for_market("LSE")


# ── USEquityCosts ─────────────────────────────────────────────────────────────


def test_us_costs_buy_side_is_slippage_only():
    costs = USEquityCosts()
    assert costs.statutory_leg_frac("BUY") == 0.0
    assert costs.leg_cost_frac("BUY") == pytest.approx(costs.slippage_frac)


def test_us_costs_sell_side_includes_sec_and_taf():
    costs = USEquityCosts()
    expected_statutory = (costs.sec_fee_pct + costs.taf_pct) / 100.0
    assert costs.statutory_leg_frac("SELL") == pytest.approx(expected_statutory)
    assert costs.leg_cost_frac("SELL") == pytest.approx(expected_statutory + costs.slippage_frac)


def test_us_costs_much_cheaper_than_nse():
    """Sanity: US statutory costs (SEC fee + TAF) are an order of magnitude
    below NSE's statutory stack (STT/stamp/GST) — catches an accidental unit
    error (e.g. leaving a rate in "pct" units where "frac" was intended). Both
    models share the same 5bps/leg slippage assumption, so compare the
    statutory components alone rather than the slippage-dominated total."""
    us = USEquityCosts()
    nse = EquityDeliveryCosts()
    us_statutory = us.statutory_leg_frac("BUY") + us.statutory_leg_frac("SELL")
    nse_statutory = nse.statutory_leg_frac("BUY") + nse.statutory_leg_frac("SELL")
    assert us_statutory < nse_statutory / 10
    assert us.round_trip_frac < nse.round_trip_frac


def test_nse_costs_unchanged():
    """EquityDeliveryCosts itself must be untouched by the US additions."""
    costs = EquityDeliveryCosts()
    assert costs.stt_buy_pct == 0.1
    assert costs.exchange_txn_pct == 0.00297
    assert costs.leg_cost_frac("BUY") > 0.0015  # well above the US round-trip


# ── tz-safe month-end / date_at (ADR-041 P3: no IST re-conversion) ──────────


def _panel(index: pd.DatetimeIndex) -> Panel:
    syms = ["A", "B"]
    close = pd.DataFrame(100.0, index=index, columns=syms)
    turn = pd.DataFrame(1e6, index=index, columns=syms)
    deliv = pd.DataFrame(50.0, index=index, columns=syms)
    return Panel(close=close, turnover=turn, delivery=deliv)


def test_date_at_us_panel_no_off_by_one():
    """A US panel's index is NY-normalized by load_panel(tz=...); date_at must
    return that same NY calendar date, not shift it (the historical bug this
    guards: forcing tz_convert(IST) here mislabels non-IST panels)."""
    idx = pd.date_range("2024-01-01", periods=5, freq="B", tz="America/New_York")
    panel = _panel(idx)
    for i, ts in enumerate(idx):
        assert panel.date_at(i) == ts.date()


def test_date_at_nse_panel_unchanged():
    idx = pd.date_range("2024-01-01", periods=5, freq="B", tz="Asia/Kolkata")
    panel = _panel(idx)
    for i, ts in enumerate(idx):
        assert panel.date_at(i) == ts.date()


def test_month_end_positions_us_calendar_no_shift():
    """A month-end that falls late in the NY trading day must not roll over
    to the next calendar day/month via a spurious IST conversion."""
    idx = pd.date_range("2024-01-29", "2024-03-01", freq="B", tz="America/New_York")
    out = month_end_positions(idx, date(2024, 1, 1))
    months = {(d.year, d.month) for d, _ in out}
    assert (2024, 1) in months and (2024, 2) in months
    jan_end = next(d for d, _ in out if (d.year, d.month) == (2024, 1))
    assert jan_end == date(2024, 1, 31)  # last US business day of Jan 2024


def test_rebalance_schedule_us_excludes_partial_final_month():
    idx = pd.date_range("2024-01-02", "2024-03-15", freq="B", tz="America/New_York")
    panel = _panel(idx)
    sched = rebalance_schedule(panel, date(2024, 1, 1))
    assert all((d.year, d.month) != (2024, 3) for d, _ in sched)
    assert sched[-1][0] == date(2024, 2, 29)  # last complete month (Feb 2024, leap year)


def test_month_end_positions_nse_unchanged():
    """Same NSE-calendar case the engine has always handled — must be bit-for-bit
    identical after removing the (redundant) forced tz_convert(IST)."""
    idx = pd.date_range("2024-01-01", "2024-02-29", freq="B", tz="Asia/Kolkata")
    out = month_end_positions(idx, date(2024, 1, 1))
    jan_end = next(d for d, _ in out if (d.year, d.month) == (2024, 1))
    assert jan_end == date(2024, 1, 31)


# ── SPY buy-and-hold benchmark ────────────────────────────────────────────────


def test_buy_hold_benchmark_return():
    idx = pd.date_range("2024-01-01", periods=4, freq="B", tz="America/New_York")
    close = pd.DataFrame({"SPY": [400.0, 404.0, 396.0, 420.0]}, index=idx)
    panel = Panel(close=close, turnover=close * 0 + 1, delivery=close * 0 + 50)
    assert buy_hold_benchmark_return(panel, "SPY", 0, 3) == pytest.approx(420.0 / 400.0 - 1.0)
    assert buy_hold_benchmark_return(panel, "SPY", 0, 0) == pytest.approx(0.0)


# ── NAV currency in the rendered session report ──────────────────────────────


def _write_journal(path, market: str, nav: float):
    from qe.journal import JournalWriter

    with JournalWriter(path) as j:
        j.session_start(
            session_id="s1",
            mode="null",
            config_hash="deadbeef",
            config={"universe": {"market": market}},
            code_sha="abc123",
            data_snapshot_id="ds-1",
        )
        j.session_end("OK", {"final_nav": nav})


def test_session_report_uses_dollar_for_us(tmp_path):
    path = tmp_path / "us.jsonl"
    _write_journal(path, "US", 1_050_000.0)
    text = render_session_report(path)
    assert "$1,050,000.00" in text
    assert "₹" not in text


def test_session_report_uses_rupee_for_nse(tmp_path):
    path = tmp_path / "nse.jsonl"
    _write_journal(path, "NSE", 1_050_000.0)
    text = render_session_report(path)
    assert "₹1,050,000.00" in text
