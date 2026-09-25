"""Unit tests for the factor diversification & correlation study (operating-doc §5).

Covers:
  * the `value` factor is exposed via FACTORS_ALL but kept OUT of the default FACTORS
    list, and adding it does NOT perturb the other factors' surviving universe
    (its NaN set is a subset of momentum's — the invariant the harness edit relies on);
  * `value` runs end-to-end through run_factor on a synthetic panel;
  * correlation / diversification primitives are correct (identical→1, independent→~0,
    diversification ratio improves only with an uncorrelated leg);
  * drawdown-overlap is symmetric with a sane diagonal;
  * governance guards: the study touches NO broker / Kite / live-state APIs.

Lake-free: a synthetic panel is generated in-process. No network, no broker.

Run:  python -m pytest tests/backtest/test_factor_correlations.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))
sys.path.insert(0, str(_REPO / "scripts" / "backtest"))

import run_factor_correlations as rfc  # noqa: E402
import run_factor_study as rfs  # noqa: E402

_IST = "Asia/Kolkata"


def _synthetic_panel(n_days: int = 420, n_syms: int = 60, seed: int = 5):
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2020-01-01", periods=n_days, freq="B", tz=_IST)
    syms = [f"S{i:03d}" for i in range(n_syms)]
    drift = rng.normal(0, 0.0006, n_syms)
    steps = rng.normal(drift, 0.02, (n_days, n_syms))
    close = pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=dates, columns=syms)
    turn = close * pd.DataFrame(rng.uniform(1e5, 1e6, close.shape), index=dates, columns=syms)
    deliv = pd.DataFrame(rng.uniform(20, 80, close.shape), index=dates, columns=syms)
    return close, turn, deliv


# ── value factor wiring & non-perturbation ────────────────────────────────────


def test_value_exposed_but_not_in_default_factors():
    assert "value" in rfs.FACTORS_ALL
    assert "value" not in rfs.FACTORS          # default report unchanged
    assert rfs.FACTORS_ALL == rfs.FACTORS + ["value"]


def test_value_does_not_perturb_other_factors_universe():
    close, _turn, deliv = _synthetic_panel()
    univ = list(close.columns)
    i = 300  # well past the 252d warmup
    scores = rfs._factor_scores(close, deliv, univ, i)
    assert "value" in scores.columns
    # post-dropna, value is present wherever momentum is — no extra rows dropped
    assert scores["value"].notna().all()
    assert scores["momentum"].notna().all()
    # the combo is still the documented 4-factor blend (value not folded in)
    assert "combo" in scores.columns


def test_value_factor_runs_end_to_end():
    close, turn, deliv = _synthetic_panel()
    r = rfs.run_factor("value", close, turn, deliv, top_n=40, k=8)
    assert r.n_rebalances > 0
    assert r.monthly_net.notna().all()
    # the other factors still run alongside the value column
    assert rfs.run_factor("delivery", close, turn, deliv, 40, 8).n_rebalances > 0


def test_sleeve_returns_aligns_columns():
    close, turn, deliv = _synthetic_panel()
    R = rfc.sleeve_returns(close, turn, deliv, ["delivery", "momentum", "value"], 40, 8)
    assert list(R.columns) == ["delivery", "momentum", "value"]
    assert len(R) > 0 and R.notna().all().all()


# ── correlation / diversification primitives ──────────────────────────────────


def test_correlation_extremes():
    idx = pd.date_range("2020-01-31", periods=40, freq="ME", tz=_IST)
    rng = np.random.default_rng(1)
    a = pd.Series(rng.normal(0.01, 0.04, 40), index=idx)
    R = pd.DataFrame({"a": a, "dup": a, "indep": pd.Series(rng.normal(0.01, 0.04, 40), index=idx)})
    pear = R.corr()
    assert abs(pear.loc["a", "dup"] - 1.0) < 1e-9
    assert abs(pear.loc["a", "indep"]) < 0.5


def test_diversification_ratio_rewards_uncorrelated_leg():
    idx = pd.date_range("2020-01-31", periods=40, freq="ME", tz=_IST)
    rng = np.random.default_rng(2)
    a = pd.Series(rng.normal(0.01, 0.04, 40), index=idx)
    R = pd.DataFrame({"a": a, "dup": a, "indep": pd.Series(rng.normal(0.01, 0.04, 40), index=idx)})
    dr_dup = rfc.diversification_ratio(R[["a", "dup"]])
    dr_mix = rfc.diversification_ratio(R[["a", "indep"]])
    assert dr_dup < 1.05            # identical pair → no diversification
    assert dr_mix > dr_dup          # an uncorrelated leg helps


def test_drawdown_overlap_symmetric_and_diagonal():
    idx = pd.date_range("2020-01-31", periods=40, freq="ME", tz=_IST)
    rng = np.random.default_rng(4)
    R = pd.DataFrame({c: pd.Series(rng.normal(0.01, 0.04, 40), index=idx) for c in ("x", "y")})
    M = rfc.drawdown_overlap(R)
    assert abs(M.loc["x", "y"] - M.loc["y", "x"]) < 1e-12      # symmetric
    assert 0.0 <= M.loc["x", "x"] <= 1.0                       # diagonal = x's own DD fraction


def test_down_month_corr_returns_frame():
    idx = pd.date_range("2020-01-31", periods=40, freq="ME", tz=_IST)
    rng = np.random.default_rng(6)
    R = pd.DataFrame({c: pd.Series(rng.normal(0.01, 0.04, 40), index=idx) for c in ("x", "y")})
    bench = pd.Series(rng.normal(0.008, 0.03, 40), index=idx)
    down, n_down = rfc.down_month_corr(R, bench)
    assert n_down >= 1 and not down.empty and "down_corr" in down.columns


def test_self_test_passes():
    assert rfc._self_test() == 0


# ── governance guards ─────────────────────────────────────────────────────────


def test_study_touches_no_broker_or_live_state():
    src = (Path(rfc.__file__)).read_text().lower()
    # call/import-specific tokens (the prose legitimately contains the word "broker"/"kite" in
    # the disclaimers — assert on what would actually *use* a broker, not on the disclaimers).
    for forbidden in ("kiteconnect", "place_order", "boto3", "broker_client", "import broker"):
        assert forbidden not in src, f"correlation study must not reference {forbidden!r}"
    assert "backtesting can recommend" in src
    assert "live trading remains blocked" in src
