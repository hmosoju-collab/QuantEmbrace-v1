"""ADR-041 Phase 5 — US paper==sim parity (mirrors test_paper_engine.py's M4
acceptance for the NSE factor books, same discipline: successive paper
sessions at each of sim's rebalance dates, resuming persisted state each time,
must reproduce sim's NAV history to the cent).
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from qe.config import DataConfig, RiskConfig, RunConfig, StrategyConfig, UniverseConfig
from qe.data.panel import Panel
from qe.engine import run_paper, run_sim, sim_clock_at
from qe.engine.book_store import load_book_state

ASSETS = ("SPY", "TLT", "GLD")
SEED = 1_000_000.0
INCEPTION = date(2024, 1, 1)


@pytest.fixture()
def panel() -> Panel:
    rng = np.random.default_rng(11)
    dates = pd.date_range("2023-01-02", periods=650, freq="B", tz="America/New_York")
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


def _config(tmp_path, panel: Panel, mode: str, **risk) -> RunConfig:
    return RunConfig(
        name=f"us-pp-{mode}",
        mode=mode,
        start_date=INCEPTION,
        end_date=panel.index[-1].date(),
        universe=UniverseConfig(market="US", symbols=ASSETS),
        data=DataConfig(lake_root="unused"),
        journal_dir=str(tmp_path / "journals"),
        seed_nav=SEED,
        strategy=StrategyConfig(
            kind="risk_parity_lite", assets=ASSETS, vol_lookback=63, cash_buffer=0.005
        ),
        risk=RiskConfig(**risk),
    )


def test_us_paper_reproduces_sim_exactly(panel, tmp_path):
    sim = run_sim(_config(tmp_path, panel, "sim"), base_dir=tmp_path, panel=panel)
    assert len(sim.nav_history) >= 3

    paper_cfg = _config(tmp_path, panel, "paper")
    state_path = tmp_path / "book_state.json"
    kill_path = tmp_path / "kill.json"

    for d, _ in sim.nav_history:
        res = run_paper(
            paper_cfg,
            base_dir=tmp_path,
            as_of=d,
            panel=panel,
            state_path=state_path,
            kill_path=kill_path,
            clock=sim_clock_at(d, market="US"),
        )
        assert res.due and res.rebalanced and not res.kill_active

    final_state = load_book_state(state_path)
    assert len(final_state.nav_history) == len(sim.nav_history)
    for got, (d, nav) in zip(final_state.nav_history, sim.nav_history, strict=True):
        assert got["date"] == d.isoformat()
        assert got["nav"] == pytest.approx(nav, abs=0.01), d


def test_sim_clock_at_us_reports_correct_ny_date():
    """Regression guard for the SimClock.today()/IST-reconversion bug found
    while building this: a US clock pinned at NYSE close (16:00 ET) must
    report the SAME calendar date, not the next one (16:00 EDT = 20:00 UTC =
    01:30 IST the next day — the old hardcoded-IST `.today()` would have
    mislabeled every US paper session's trading date)."""
    d = date(2026, 7, 31)
    clock = sim_clock_at(d, market="US")
    assert clock.today() == d
