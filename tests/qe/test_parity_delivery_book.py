"""M2 parity acceptance (synthetic leg): the qe engine must reproduce the v1
forward-book replay — same NAV at every rebalance, same final MTM, same
benchmark legs — on an identical panel, for both factors.

The v1 `replay()` is imported directly from scripts/paper and run side-by-side.
"""

from datetime import date
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

from qe.config import DataConfig, RunConfig, StrategyConfig, UniverseConfig
from qe.data.panel import Panel
from qe.research import run_factor_book_study

_REPO = Path(__file__).resolve().parents[2]
for _p in (_REPO / "scripts" / "paper", _REPO / "scripts" / "backtest"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

v1_replay_mod = pytest.importorskip("replay_delivery_book_forward")

IST = "Asia/Kolkata"
SEED = 1_000_000.0
INCEPTION = date(2025, 3, 31)


@pytest.fixture(scope="module")
def synthetic_panel() -> Panel:
    """Same construction as the v1 replay self-test (rng seed 5)."""
    rng = np.random.default_rng(5)
    dates = pd.date_range("2023-06-01", periods=520, freq="B", tz=IST)
    syms = [f"S{i:03d}" for i in range(60)]
    close = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0.0003, 0.018, (len(dates), len(syms))), 0)),
        index=dates,
        columns=syms,
    )
    turn = close * pd.DataFrame(rng.uniform(1e5, 1e6, close.shape), index=dates, columns=syms)
    deliv = pd.DataFrame(rng.uniform(20, 80, close.shape), index=dates, columns=syms)
    return Panel(close=close, turnover=turn, delivery=deliv)


def _qe_config(tmp_path: Path, panel: Panel, factor: str) -> RunConfig:
    return RunConfig(
        name=f"parity-{factor}",
        mode="sim",
        start_date=INCEPTION,
        end_date=pd.Timestamp(panel.index[-1]).tz_convert(IST).date(),
        universe=UniverseConfig(symbols=None),
        data=DataConfig(lake_root="unused"),
        journal_dir=str(tmp_path / "journals"),
        seed_nav=SEED,
        strategy=StrategyConfig(factor=factor),
    )


@pytest.mark.parametrize("factor", ["delivery", "momentum"])
def test_qe_engine_matches_v1_replay(synthetic_panel, tmp_path, factor):
    v1_state = v1_replay_mod.replay(
        synthetic_panel.close,
        synthetic_panel.turnover,
        synthetic_panel.delivery,
        inception=INCEPTION,
        seed=SEED,
        factor=factor,
    )

    config = _qe_config(tmp_path, synthetic_panel, factor)
    study = run_factor_book_study(
        config, base_dir=tmp_path, panel=synthetic_panel, report_dir=tmp_path / "reports"
    )
    sim = study.sim

    # Rebalance dates and NAV after each rebalance — rupee-exact.
    v1_navs = v1_state["nav_history"]
    assert [d.isoformat() for d, _ in sim.nav_history] == [h["date"] for h in v1_navs]
    for (_, qe_nav), v1_h in zip(sim.nav_history, v1_navs, strict=True):
        assert qe_nav == pytest.approx(v1_h["nav"], abs=0.01), v1_h["date"]

    # Final mark-to-market.
    assert sim.final_mtm[0].isoformat() == v1_state["current_mtm"]["date"]
    assert sim.final_mtm[1] == pytest.approx(v1_state["current_mtm"]["nav"], abs=0.01)

    # Benchmark legs and cumulative.
    qe_bench = [m["bench"] for m in study.months]
    assert qe_bench == pytest.approx(v1_state["_bench_rets"], abs=1e-12)
    v1_book_cum = v1_state["_book_navs"][-1] / SEED - 1.0
    assert study.book_cum == pytest.approx(v1_book_cum, abs=1e-6)
