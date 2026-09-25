"""M4 acceptance: paper (WallClock) and sim (SimClock) are the same engine.

Driving successive paper sessions at each of sim's rebalance dates — resuming
persisted state each time — must reproduce sim's NAV history to the paisa. That
proves the three-clocks invariant (RA-1 §2.3) numerically AND exercises the
resume/persistence path.
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from qe.config import DataConfig, RiskConfig, RunConfig, StrategyConfig, UniverseConfig
from qe.data.panel import Panel
from qe.engine import run_paper, run_sim, sim_clock_at
from qe.engine.book_store import BookStoreError, load_book_state
from qe.reporting import session_report

IST = "Asia/Kolkata"
SEED = 1_000_000.0
INCEPTION = date(2024, 6, 30)


@pytest.fixture()
def panel() -> Panel:
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


def _config(tmp_path, panel, mode: str, **risk) -> RunConfig:
    return RunConfig(
        name=f"pp-{mode}",
        mode=mode,
        start_date=INCEPTION,
        end_date=pd.Timestamp(panel.index[-1]).tz_convert(IST).date(),
        universe=UniverseConfig(symbols=None),
        data=DataConfig(lake_root="unused"),
        journal_dir=str(tmp_path / "journals"),
        seed_nav=SEED,
        strategy=StrategyConfig(factor="delivery"),
        risk=RiskConfig(**risk),
    )


def _rebalance_dates(tmp_path, panel) -> list[date]:
    """Actual month-end trading dates the book rebalances on (not calendar
    month-ends, which may be weekends/holidays)."""
    sim = run_sim(
        _config(tmp_path / "sched", panel, "sim"), base_dir=tmp_path / "sched", panel=panel
    )
    return [d for d, _ in sim.nav_history]


def test_paper_reproduces_sim_exactly(panel, tmp_path):
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
            clock=sim_clock_at(d),
        )
        assert res.due and res.rebalanced and not res.kill_active

    final_state = load_book_state(state_path)
    assert len(final_state.nav_history) == len(sim.nav_history)
    for got, (d, nav) in zip(final_state.nav_history, sim.nav_history, strict=True):
        assert got["date"] == d.isoformat()
        assert got["nav"] == pytest.approx(nav, abs=0.01), d


def test_paper_is_idempotent_within_a_month(panel, tmp_path):
    """Re-running the same month-end must not double-rebalance (idempotent)."""
    cfg = _config(tmp_path, panel, "paper")
    d0 = _rebalance_dates(tmp_path, panel)[0]
    common = {
        "base_dir": tmp_path,
        "panel": panel,
        "state_path": tmp_path / "s.json",
        "kill_path": tmp_path / "k.json",
    }
    first = run_paper(cfg, as_of=d0, clock=sim_clock_at(d0), **common)
    assert first.due and first.rebalanced

    second = run_paper(cfg, as_of=d0, clock=sim_clock_at(d0), **common)
    assert not second.due and not second.rebalanced  # already rebalanced this month
    assert len(load_book_state(tmp_path / "s.json").nav_history) == 1


def test_paper_fails_closed_on_config_drift(panel, tmp_path):
    state_path = tmp_path / "s.json"
    dates = _rebalance_dates(tmp_path, panel)
    run_paper(
        _config(tmp_path, panel, "paper"),
        base_dir=tmp_path,
        as_of=dates[0],
        panel=panel,
        state_path=state_path,
        kill_path=tmp_path / "k.json",
        clock=sim_clock_at(dates[0]),
    )
    # A different strategy config on the same book must be refused.
    drifted = _config(tmp_path, panel, "paper").model_copy(
        update={"strategy": StrategyConfig(factor="delivery", k=15)}
    )
    with pytest.raises(BookStoreError, match="config hash drift"):
        run_paper(
            drifted,
            base_dir=tmp_path,
            as_of=dates[1],
            panel=panel,
            state_path=state_path,
            kill_path=tmp_path / "k.json",
            clock=sim_clock_at(dates[1]),
        )


def _truncate(panel: Panel, upto_pos: int) -> Panel:
    k = upto_pos + 1
    return Panel(
        close=panel.close.iloc[:k],
        turnover=panel.turnover.iloc[:k],
        delivery=panel.delivery.iloc[:k],
    )


def _panel_dates(panel: Panel) -> list[date]:
    return [pd.Timestamp(t).tz_convert(IST).date() for t in panel.index]


def test_mid_month_frontier_is_not_due(panel, tmp_path):
    """A session at the lake frontier mid-month must be MTM-only, never a rebalance.

    Regression (2026-07-08, first real paper session): the latest lake date is
    always the last row of its month *in the panel*, so the old rule rebalanced
    the book on any fresh-data day — sim would never trade mid-month."""
    dates = _panel_dates(panel)
    frontier = next(
        i
        for i, d in enumerate(dates)
        if d > INCEPTION and 5 <= d.day <= 15 and i + 1 < len(dates) and dates[i + 1].month == d.month
    )
    live = _truncate(panel, frontier)
    d_f = dates[frontier]

    res = run_paper(
        _config(tmp_path, panel, "paper"),
        base_dir=tmp_path,
        as_of=d_f,
        panel=live,
        state_path=tmp_path / "s.json",
        kill_path=tmp_path / "k.json",
        clock=sim_clock_at(d_f),
    )
    assert not res.due and not res.rebalanced and not res.kill_active
    assert res.nav == pytest.approx(SEED)  # all cash, untouched
    assert load_book_state(tmp_path / "s.json").nav_history == []


def test_deferred_month_end_executes_at_month_end_prices(panel, tmp_path):
    """The owed month-end executes on the *next* session at the month-end's own
    row — the same (date, prices) sim uses — once the month is provably over."""
    dates = _panel_dates(panel)
    frontier = next(
        i
        for i, d in enumerate(dates)
        if d > INCEPTION and 5 <= d.day <= 15 and i + 1 < len(dates) and dates[i + 1].month == d.month
    )
    d_f = dates[frontier]
    next_month_first = next(
        i for i, d in enumerate(dates) if i > frontier and d.month != d_f.month
    )
    month_end_date = dates[next_month_first - 1]

    cfg = _config(tmp_path, panel, "paper")
    common = {
        "base_dir": tmp_path,
        "state_path": tmp_path / "s.json",
        "kill_path": tmp_path / "k.json",
    }
    # Session 1: mid-month, seeds the book, MTM-only.
    run_paper(cfg, as_of=d_f, panel=_truncate(panel, frontier), clock=sim_clock_at(d_f), **common)
    # Session 2: frontier has crossed into the next month — month-end now owed.
    d2 = dates[next_month_first]
    res = run_paper(
        cfg, as_of=d2, panel=_truncate(panel, next_month_first), clock=sim_clock_at(d2), **common
    )
    assert res.due and res.rebalanced
    state = load_book_state(tmp_path / "s.json")
    assert state.last_rebalance == month_end_date.isoformat()
    assert [h["date"] for h in state.nav_history] == [month_end_date.isoformat()]

    # Sim over the same window rebalances on the same date with the same NAV.
    sim_cfg = _config(tmp_path / "sim2", panel, "sim").model_copy(
        update={"start_date": d_f, "end_date": d2}
    )
    sim = run_sim(sim_cfg, base_dir=tmp_path / "sim2", panel=_truncate(panel, next_month_first))
    assert sim.nav_history[0][0] == month_end_date
    assert state.nav_history[0]["nav"] == pytest.approx(sim.nav_history[0][1], abs=0.01)


def test_pinned_month_end_due_once_clock_passes_month(panel, tmp_path):
    """Runbook case `--as-of <month-end>` run after the month turned: the panel
    ends at the month-end, so completeness is proven by the wall clock."""
    dates = _panel_dates(panel)
    frontier = next(
        i
        for i, d in enumerate(dates)
        if d > INCEPTION and i + 1 < len(dates) and dates[i + 1].month != d.month
    )
    month_end_date = dates[frontier]
    live = _truncate(panel, frontier)
    cfg = _config(tmp_path, panel, "paper")
    common = {
        "base_dir": tmp_path,
        "state_path": tmp_path / "s.json",
        "kill_path": tmp_path / "k.json",
    }
    # Run on the month-end evening itself: not provably over → not due.
    same_day = run_paper(cfg, as_of=month_end_date, panel=live, clock=sim_clock_at(month_end_date), **common)
    assert not same_day.due and not same_day.rebalanced
    # Re-run days later (clock in the next month): now due, executes at the pin.
    later = run_paper(
        cfg,
        as_of=month_end_date,
        panel=live,
        clock=sim_clock_at(dates[frontier + 2]),
        **common,
    )
    assert later.due and later.rebalanced
    assert load_book_state(tmp_path / "s.json").last_rebalance == month_end_date.isoformat()


def test_active_kill_switch_blocks_emission(panel, tmp_path):
    cfg = _config(tmp_path, panel, "paper")
    from qe.killswitch import KillSwitch

    kill_path = tmp_path / "k.json"
    KillSwitch(kill_path).activate("operator halt", by="test")

    d0 = _rebalance_dates(tmp_path, panel)[0]
    res = run_paper(
        cfg,
        base_dir=tmp_path,
        as_of=d0,
        panel=panel,
        state_path=tmp_path / "s.json",
        kill_path=kill_path,
        clock=sim_clock_at(d0),
    )
    assert res.due and res.kill_active and not res.rebalanced
    # Book untouched: still all cash at seed.
    state = load_book_state(tmp_path / "s.json")
    assert state.nav_history == []
    rep = session_report(res.journal_path)
    assert rep["kill_blocked"] == 1


def test_drawdown_trigger_halts_a_due_rebalance(panel, tmp_path):
    """A book below its peak past the DD limit auto-halts the due rebalance.

    Seeds a book whose recorded peak is far above the current all-cash NAV, so
    the drawdown trigger fires deterministically (the trigger predicate itself is
    unit-tested separately; this proves the paper engine wires it to the kill)."""
    from qe.engine.book_store import BookState, save_book_state
    from qe.execution import Book

    cfg = _config(tmp_path, panel, "paper", max_drawdown_frac=0.25)
    d0 = _rebalance_dates(tmp_path, panel)[0]
    state_path, kill_path = tmp_path / "s.json", tmp_path / "k.json"

    seeded = BookState.seed(
        inception=date(2024, 1, 1), seed_nav=SEED, config_hash=cfg.config_hash()
    )
    seeded.nav_history = [{"date": "2024-05-31", "nav": 2_000_000.0}]  # peak 2x current cash
    seeded.last_rebalance = "2024-05-31"
    save_book_state(state_path, seeded, Book(cash=SEED))

    res = run_paper(
        cfg,
        base_dir=tmp_path,
        as_of=d0,
        panel=panel,
        state_path=state_path,
        kill_path=kill_path,
        clock=sim_clock_at(d0),
    )
    assert res.due and res.kill_active and not res.rebalanced
    rep = session_report(res.journal_path)
    assert rep["kill_triggered"] == 1 and rep["kill_blocked"] == 1
