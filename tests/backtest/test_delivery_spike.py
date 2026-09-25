"""Unit tests for H4 — the delivery-spike event-drift study.

Covers:
  * NO-LOOKAHEAD: an event detected on signal day t enters at close[t+1], never on/before t
    (NSE delivery % is published post-close);
  * the event study recovers a planted positive drift, and the calendar portfolio charges the
    delivery cost (a flat-price held position loses exactly the round-trip);
  * per-trade economics frame shape;
  * governance guards: the study touches NO broker / Kite / live-state APIs.

Lake-free: small synthetic panels generated in-process. No network, no broker.

Run:  python -m pytest tests/backtest/test_delivery_spike.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))
sys.path.insert(0, str(_REPO / "scripts" / "backtest"))

import run_delivery_spike as rds  # noqa: E402
from run_factor_study import _BUY_FRAC, _SELL_FRAC, _ROUND_TRIP  # noqa: E402

_IST = "Asia/Kolkata"


def test_self_test_passes():
    assert rds._self_test() == 0


def test_entry_is_t_plus_one_no_lookahead():
    # one clean planted spike: delivery jump + volume jump for S000 on day t0, nothing else spikes.
    n_days, n_syms, t0 = 120, 10, 90
    dates = pd.date_range("2020-01-01", periods=n_days, freq="B", tz=_IST)
    syms = [f"S{i:03d}" for i in range(n_syms)]
    close = pd.DataFrame(100.0, index=dates, columns=syms)
    turn = pd.DataFrame(1.0e6, index=dates, columns=syms)
    deliv = pd.DataFrame(30.0, index=dates, columns=syms)     # constant baseline → no false spikes
    deliv.iloc[t0, 0] = 85.0
    turn.iloc[t0, 0] = 3.0e6

    events = rds.detect_events(close, turn, deliv, top_n=10, z=2.0, vol_mult=1.5, dlv_floor=50)
    assert (t0 + 1, "S000") in events, "event must enter at the day AFTER the signal (post-close data)"
    # and nothing enters on or before the signal day for that symbol
    assert all(not (sym == "S000" and e <= t0) for e, sym in events)


def test_calendar_portfolio_charges_round_trip_on_flat_price():
    # a single held position whose price never moves must lose exactly the round-trip cost.
    n_days = 60
    dates = pd.date_range("2020-01-01", periods=n_days, freq="B", tz=_IST)
    close = pd.DataFrame({"X": np.full(n_days, 100.0)}, index=dates)
    events = [(20, "X")]          # enters at close[20], held 20+1..20+hold
    port = rds.calendar_portfolio(close, events, hold=5)
    total = (1.0 + port).prod() - 1.0
    assert total < 0, "a flat-price held position must show a cost drag"
    # buy cost hits the entry day, sell cost the exit day → they compound multiplicatively
    expected = (1.0 - _BUY_FRAC) * (1.0 - _SELL_FRAC) - 1.0
    assert abs(total - expected) < 1e-9, "drag should equal the (compounded) round-trip cost"
    # additive round-trip is the same to first order
    assert abs(expected - (-_ROUND_TRIP)) < 1e-5


def test_event_study_recovers_planted_drift():
    n_days, n_syms = 200, 12
    rng = np.random.default_rng(9)
    dates = pd.date_range("2020-01-01", periods=n_days, freq="B", tz=_IST)
    syms = [f"S{i:03d}" for i in range(n_syms)]
    steps = rng.normal(0.0, 0.004, (n_days, n_syms))
    deliv = pd.DataFrame(30.0, index=dates, columns=syms)
    for t in range(80, n_days - 25, 25):
        ci = (t // 25) % n_syms
        deliv.iloc[t, ci] = 85.0
        steps[t + 1:t + 11, ci] += 0.006      # planted post-event drift
    close = pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=dates, columns=syms)
    turn = pd.DataFrame(1.0e6, index=dates, columns=syms)
    turn.iloc[80::25] = 3.0e6

    events = rds.detect_events(close, turn, deliv, top_n=12, z=2.0, vol_mult=1.5, dlv_floor=50)
    assert len(events) >= 3
    _mkt_r, mkt_l = rds._market(close, turn)
    es = rds.event_study(close, mkt_l, events)
    i10 = es.horizons.index(10)
    assert es.mean_ret[i10] > 0, "planted positive drift not recovered at T+10"


def test_per_trade_net_columns():
    n_days, n_syms = 160, 10
    rng = np.random.default_rng(3)
    dates = pd.date_range("2020-01-01", periods=n_days, freq="B", tz=_IST)
    syms = [f"S{i:03d}" for i in range(n_syms)]
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.01, (n_days, n_syms)), axis=0)),
                         index=dates, columns=syms)
    _mkt_r, mkt_l = rds._market(close, close * 1e4)
    df = rds.per_trade_net(close, mkt_l, [(50, "S000"), (60, "S001")])
    assert set(df.columns) == {"hold", "n", "mean_net", "median_net", "hit", "mean_abn_net", "tstat_abn"}
    assert list(df["hold"]) == list(rds._NET_HOLDS)


def test_study_touches_no_broker_or_live_state():
    src = Path(rds.__file__).read_text().lower()
    for forbidden in ("kiteconnect", "place_order", "boto3", "broker_client", "import broker"):
        assert forbidden not in src, f"H4 study must not reference {forbidden!r}"
    assert "backtesting can recommend" in src
    assert "live trading remains blocked" in src
    assert "no-lookahead" in src or "no lookahead" in src
