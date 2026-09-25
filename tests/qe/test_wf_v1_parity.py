"""The qe.research.wf_v1 port must reproduce the v1 walk-forward script
function-for-function (same panel in, identical series out)."""

from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

from qe.data.panel import Panel
from qe.research import wf_v1

_REPO = Path(__file__).resolve().parents[2]
for _p in (_REPO / "scripts" / "backtest", _REPO / "scripts" / "paper"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

v1 = pytest.importorskip("run_delivery_walkforward")

IST = "Asia/Kolkata"


@pytest.fixture(scope="module")
def panel() -> Panel:
    """Same construction as the v1 script's self-test (rng seed 11)."""
    rng = np.random.default_rng(11)
    dates = pd.date_range("2020-01-01", periods=600, freq="B", tz=IST)
    syms = [f"S{i:03d}" for i in range(80)]
    steps = rng.normal(0.0004, 0.02, (len(dates), len(syms)))
    close = pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=dates, columns=syms)
    turn = close * pd.DataFrame(rng.uniform(1e5, 1e6, close.shape), index=dates, columns=syms)
    deliv = pd.DataFrame(rng.uniform(20, 80, close.shape), index=dates, columns=syms)
    return Panel(close=close, turnover=turn, delivery=deliv)


def test_round_trip_cost_matches(panel):
    assert wf_v1.ROUND_TRIP == pytest.approx(v1._ROUND_TRIP, abs=1e-15)


def test_regime_series_matches(panel):
    ours = wf_v1.regime_series(panel.close, panel.turnover, sma=200)
    theirs = v1._regime_series(panel.close, panel.turnover, sma=200)
    pd.testing.assert_series_equal(ours, theirs)


@pytest.mark.parametrize("overlay", [False, True])
def test_delivery_leg_matches(panel, overlay):
    regime = wf_v1.regime_series(panel.close, panel.turnover, 200) if overlay else None
    ours = wf_v1.run_delivery(panel.close, panel.turnover, panel.delivery, regime, 40, 8)
    theirs = v1._run_delivery(panel.close, panel.turnover, panel.delivery, regime, 40, 8)
    pd.testing.assert_series_equal(ours.monthly_net, theirs.monthly_net)
    assert ours.cash_months == theirs.cash_months


@pytest.mark.parametrize("overlay", [False, True])
def test_benchmark_leg_matches(panel, overlay):
    regime = wf_v1.regime_series(panel.close, panel.turnover, 200) if overlay else None
    ours = wf_v1.run_benchmark(panel.close, panel.turnover, regime, 40)
    theirs = v1._run_benchmark(panel.close, panel.turnover, regime, 40)
    pd.testing.assert_series_equal(ours.monthly_net, theirs.monthly_net)
    assert ours.cash_months == theirs.cash_months


def test_metrics_and_per_year_match(panel):
    from qe.research.metrics import monthly_metrics, per_year

    leg = wf_v1.run_delivery(panel.close, panel.turnover, panel.delivery, None, 40, 8)
    ours_m = monthly_metrics(leg.monthly_net)
    theirs_m = v1._metrics(leg.monthly_net)
    for key in ("cagr", "vol", "sharpe", "maxdd", "hit", "months"):
        assert ours_m[key] == pytest.approx(theirs_m[key], abs=1e-12), key
    pd.testing.assert_frame_equal(per_year(leg.monthly_net), v1._per_year(leg.monthly_net))
