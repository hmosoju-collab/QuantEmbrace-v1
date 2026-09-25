"""ADR-041 Phase 4 — US risk-parity study + walk-forward split (qe.research.us_study).

Uses the same synthetic SPY/TLT/GLD panel construction as test_us_rplite_parity.py.
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from qe.config import DataConfig, RunConfig, StrategyConfig, UniverseConfig
from qe.data.panel import Panel
from qe.research.us_study import run_risk_parity_study, run_risk_parity_walkforward

ASSETS = ("SPY", "TLT", "GLD")
SEED = 1_000_000.0
INCEPTION = date(2024, 1, 1)


@pytest.fixture(scope="module")
def panel() -> Panel:
    rng = np.random.default_rng(7)
    dates = pd.date_range("2023-01-02", periods=900, freq="B", tz="America/New_York")
    paths = {"SPY": (0.0004, 0.011), "TLT": (0.0001, 0.009), "GLD": (0.0002, 0.010)}
    close = pd.DataFrame(
        {
            sym: 100 * np.exp(np.cumsum(rng.normal(mu, sigma, len(dates))))
            for sym, (mu, sigma) in paths.items()
        },
        index=dates,
    )
    return Panel(
        close=close,
        turnover=close * 1e6,
        delivery=pd.DataFrame(50.0, index=dates, columns=close.columns),
    )


def _config(tmp_path, panel: Panel, **overrides) -> RunConfig:
    base = {
        "name": "rplite-book",
        "mode": "sim",
        "start_date": INCEPTION,
        "end_date": panel.index[-1].date(),
        "universe": UniverseConfig(market="US", symbols=ASSETS),
        "data": DataConfig(lake_root="unused"),
        "journal_dir": str(tmp_path / "journals"),
        "seed_nav": SEED,
        "strategy": StrategyConfig(
            kind="risk_parity_lite", assets=ASSETS, vol_lookback=63, cash_buffer=0.005
        ),
    }
    base.update(overrides)
    return RunConfig(**base)


def test_risk_parity_study_summary_shape_and_currency(tmp_path, panel):
    config = _config(tmp_path, panel)
    result = run_risk_parity_study(
        config, base_dir=tmp_path, panel=panel, report_dir=tmp_path / "reports"
    )
    assert result.months
    assert result.report_path.exists() and result.summary_path.exists()

    report_text = result.report_path.read_text()
    assert "$" in report_text
    assert "₹" not in report_text

    import json

    summary = json.loads(result.summary_path.read_text())
    for key in (
        "session_id",
        "config_hash",
        "code_sha",
        "data_snapshot_id",
        "inception",
        "seed_nav",
        "nav_history",
        "final_mtm",
        "months",
        "book_cum",
        "bench_cum",
        "n_full",
    ):
        assert key in summary, f"missing key required by check_us_forward_gate.py: {key}"
    for m in summary["months"]:
        assert {"to", "book", "bench", "alpha"} <= set(m)


def test_risk_parity_walkforward_splits_and_reports(tmp_path, panel):
    config = _config(tmp_path, panel)
    split = date(2025, 1, 1)
    is_half, oos_half = run_risk_parity_walkforward(
        config, split_date=split, base_dir=tmp_path, panel=panel
    )
    assert is_half.label == "in-sample" and oos_half.label == "out-of-sample"
    assert is_half.end == split and oos_half.start == split
    # Each half ran independent rebalances within its own window.
    assert all(d < split for d, _ in is_half.sim.nav_history)
    assert all(d >= split for d, _ in oos_half.sim.nav_history)
    for h in (is_half, oos_half):
        assert h.metrics["months"] > 0

    from qe.research.us_study import render_walkforward_report

    text = render_walkforward_report((is_half, oos_half))
    assert "in-sample" in text.lower() and "out-of-sample" in text.lower()
