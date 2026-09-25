"""Fail-closed pre-live drills — prove the safety invariants actually fire.

The pre-live runbook requires demonstrating that the engine halts on the failure
modes that matter before any capital is at risk. These drills run the real paper
engine against crafted conditions and assert the SAFE outcome:

  1. staleness     — data older than the limit ⇒ auto-halt, no rebalance
  2. kill_halt     — an active kill switch ⇒ order emission blocked
  3. config_drift  — a changed config on a live book ⇒ refuse to continue

Unlike the live gate (which refuses until evidence exists), these pass now — they
are proof the fail-closed machinery works. Self-contained: builds its own tiny
synthetic panel, so `qe drill` is always runnable and deterministic.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

from qe.clock import IST, SimClock
from qe.config import DataConfig, RiskConfig, RunConfig, StrategyConfig, UniverseConfig
from qe.data.panel import Panel
from qe.engine import run_paper, run_sim
from qe.engine.book_store import BookStoreError


@dataclass(frozen=True)
class DrillResult:
    name: str
    passed: bool
    detail: str


def _panel() -> Panel:
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


def _cfg(tmp: Path, **risk) -> RunConfig:
    return RunConfig(
        name="drill",
        mode="paper",
        start_date=date(2024, 6, 30),
        end_date=date(2025, 5, 28),
        universe=UniverseConfig(symbols=None),
        data=DataConfig(lake_root="unused"),
        journal_dir=str(tmp / "j"),
        seed_nav=1_000_000.0,
        strategy=StrategyConfig(factor="delivery"),
        risk=RiskConfig(**risk),
    )


def _first_rebalance_date(tmp: Path, panel: Panel) -> date:
    sim = run_sim(_cfg(tmp).model_copy(update={"mode": "sim"}), base_dir=tmp, panel=panel)
    return sim.nav_history[0][0]


def run_fail_closed_drills(base_dir: str | Path | None = None) -> list[DrillResult]:
    tmp = Path(base_dir) if base_dir else Path(tempfile.mkdtemp(prefix="qe-drill-"))
    panel = _panel()
    d0 = _first_rebalance_date(tmp / "sched", panel)
    results: list[DrillResult] = []

    # 1. staleness — clock is 30 days after the data date, limit 7 ⇒ must halt.
    cfg = _cfg(tmp / "stale", max_data_age_days=7)
    stale_clock = SimClock(datetime.combine(d0 + timedelta(days=30), time(15, 30), tzinfo=IST))
    r = run_paper(
        cfg,
        base_dir=tmp,
        as_of=d0,
        panel=panel,
        state_path=tmp / "st1.json",
        kill_path=tmp / "k1.json",
        clock=stale_clock,
    )
    ok = r.kill_active and not r.rebalanced
    results.append(
        DrillResult("staleness", ok, f"kill_active={r.kill_active} rebalanced={r.rebalanced}")
    )

    # 2. kill_halt — pre-activate the kill switch ⇒ due rebalance must not emit.
    from qe.killswitch import KillSwitch

    kpath = tmp / "k2.json"
    KillSwitch(kpath).activate("drill halt", by="drill")
    cfg = _cfg(tmp / "kill")
    r = run_paper(
        cfg,
        base_dir=tmp,
        as_of=d0,
        panel=panel,
        state_path=tmp / "st2.json",
        kill_path=kpath,
        clock=SimClock(datetime.combine(d0, time(15, 30), tzinfo=IST)),
    )
    ok = r.due and r.kill_active and not r.rebalanced
    results.append(
        DrillResult(
            "kill_halt", ok, f"due={r.due} kill_active={r.kill_active} rebalanced={r.rebalanced}"
        )
    )

    # 3. config_drift — a changed config on an existing book ⇒ refuse to continue.
    cfg = _cfg(tmp / "drift")
    spath = tmp / "st3.json"
    run_paper(
        cfg,
        base_dir=tmp,
        as_of=d0,
        panel=panel,
        state_path=spath,
        kill_path=tmp / "k3.json",
        clock=SimClock(datetime.combine(d0, time(15, 30), tzinfo=IST)),
    )
    drifted = cfg.model_copy(update={"strategy": StrategyConfig(factor="delivery", k=15)})
    d1 = run_sim(
        cfg.model_copy(update={"mode": "sim"}), base_dir=tmp / "s3", panel=panel
    ).nav_history[1][0]
    refused = False
    try:
        run_paper(
            drifted,
            base_dir=tmp,
            as_of=d1,
            panel=panel,
            state_path=spath,
            kill_path=tmp / "k3.json",
            clock=SimClock(datetime.combine(d1, time(15, 30), tzinfo=IST)),
        )
    except BookStoreError:
        refused = True
    results.append(DrillResult("config_drift", refused, f"refused_drifted_config={refused}"))

    return results
