"""SimClock engine: deterministic monthly-rebalance replay over the panel.

The M2 engine (RA-1 §2.3, backtest clock). One loop drives strategy → portfolio
→ risk → execution → SimBroker, journaling every decision. Rebalance semantics
match the registered forward books: last trading day of each COMPLETE month
from inception (the in-progress final month is excluded), fills at that day's
close, final mark-to-market at the latest panel date.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import time

import pandas as pd

from qe.clock import market_tz
from qe.config import RunConfig
from qe.costs import cost_model_for_market
from qe.data.panel import Panel, load_panel, resolve_panel_files
from qe.data.snapshot import SnapshotError, create_snapshot, load_manifest, verify_snapshot
from qe.engine.core import execute_rebalance, prices_asof
from qe.execution import Book, SimBroker
from qe.journal import JournalWriter
from qe.strategy import Context, FactorBookStrategy, RiskParityLiteStrategy
from qe.strategy.base import Strategy
from qe.version import code_version


@dataclass(frozen=True)
class SimRunResult:
    session_id: str
    journal_path: Path
    config_hash: str
    code_sha: str
    data_snapshot_id: str
    nav_history: list[tuple[date, float]]  # NAV after each rebalance
    rebal_positions: list[int]  # panel row of each rebalance
    final_mtm: tuple[date, float]
    final_pos: int
    panel: Panel
    book: Book


def month_end_positions(index: pd.DatetimeIndex, from_date: date) -> list[tuple[date, int]]:
    """(last-trading-day, row-position) per calendar month at/after from_date.

    ``index`` is already normalized to the market's own trading-date tz by
    ``load_panel(tz=...)`` — grouping on it directly (no re-conversion) keeps
    this correct for both the NSE (IST) and US (America/New_York) lakes
    (ADR-041 P3)."""
    norm = pd.DatetimeIndex(index).normalize()
    df = pd.DataFrame({"pos": range(len(norm)), "ym": norm.to_period("M")}, index=norm)
    out: list[tuple[date, int]] = []
    for _, g in df.groupby("ym", sort=True):
        d = g.index[-1].date()
        if d >= from_date:
            out.append((d, int(g["pos"].iloc[-1])))
    return out


def rebalance_schedule(panel: Panel, inception: date) -> list[tuple[date, int]]:
    """Complete-month month-ends only — the final (possibly partial) month in
    the panel is never a rebalance, exactly as the v1 forward replay."""
    me = month_end_positions(panel.index, inception)
    if not me:
        raise ValueError(f"no month-ends at/after {inception} in the panel")
    latest_ym = pd.Period(pd.Timestamp(panel.index[-1]), freq="M")
    rebal = [(d, p) for (d, p) in me if pd.Period(pd.Timestamp(d), freq="M") != latest_ym]
    return rebal if rebal else me[:1]


def _build_strategy(config: RunConfig) -> Strategy:
    s = config.strategy
    assert s is not None  # guaranteed by RunConfig validation for mode=sim
    if s.kind == "risk_parity_lite":
        return RiskParityLiteStrategy(
            assets=s.assets, vol_lookback=s.vol_lookback, cash_buffer=s.cash_buffer
        )
    return FactorBookStrategy(
        factor=s.factor, top_n=s.top_n, k=s.k, max_weight=s.max_weight, cash_buffer=s.cash_buffer
    )


def run_sim(
    config: RunConfig,
    *,
    base_dir: str | Path = ".",
    panel: Panel | None = None,
    panel_snapshot_id: str | None = None,
    broker: SimBroker | None = None,
) -> SimRunResult:
    """Run a sim session. ``panel`` injection is for tests/research only —
    a lake-backed run always pins (or verifies) a data snapshot. A caller that
    snapshotted the lake itself (e.g. the walk-forward study) passes the real
    id via ``panel_snapshot_id`` alongside the injected panel. ``broker``
    injection is for tests only (e.g. isolating engine mechanics from the
    cost model in a cross-engine parity check) — a real run always uses
    ``cost_model_for_market``."""
    base_dir = Path(base_dir)
    warmup_start = date(config.start_date.year - 2, config.start_date.month, 1)

    if panel is None:
        lake_root = base_dir / config.data.lake_root
        files = resolve_panel_files(
            lake_root,
            warmup_start,
            config.end_date,
            market=config.universe.market,
            segment=config.universe.segment,
            interval=config.data.interval,
        )
        if config.data.snapshot_id:
            manifest = load_manifest(lake_root, config.data.snapshot_id)
            problems = verify_snapshot(lake_root, manifest)
            if problems:
                raise SnapshotError(
                    f"snapshot {config.data.snapshot_id} verification failed: {problems[:5]}"
                )
        else:
            manifest = create_snapshot(
                lake_root,
                files,
                {
                    "market": config.universe.market,
                    "segment": config.universe.segment,
                    "interval": config.data.interval,
                    "panel_start": str(warmup_start),
                    "end_date": str(config.end_date),
                },
            )
        snapshot_id = manifest["snapshot_id"]
        tz = market_tz(config.universe.market)
        panel = load_panel(files, warmup_start, config.end_date, tz=str(tz))
    else:
        snapshot_id = panel_snapshot_id or "ds-injected"  # injected panel

    strategy = _build_strategy(config)
    broker = broker or SimBroker(cost_model_for_market(config.universe.market))
    book = Book(cash=config.seed_nav)
    schedule = rebalance_schedule(panel, config.start_date)

    config_hash = config.config_hash()
    code_sha = code_version(base_dir)
    session_id = (
        f"{config.name}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{config_hash[:12]}"
    )
    journal_path = base_dir / config.journal_dir / f"{session_id}.jsonl"

    nav_history: list[tuple[date, float]] = []
    with JournalWriter(journal_path) as journal:
        journal.session_start(
            session_id=session_id,
            mode=config.mode,
            config_hash=config_hash,
            config=config.model_dump(mode="json"),
            code_sha=code_sha,
            data_snapshot_id=snapshot_id,
        )
        journal.write(
            "SCHEDULE",
            {
                "rebalances": [d.isoformat() for d, _ in schedule],
                "cost_model": broker.costs.version,
            },
        )

        for d, pos in schedule:
            # kill=None: backtest replays history and never blocks on a live halt.
            outcome = execute_rebalance(
                book=book,
                ctx=Context.at(panel, pos),
                on_date=d,
                strategy=strategy,
                broker=broker,
                risk_cfg=config.risk,
                journal=journal,
                kill=None,
            )
            if outcome.executed:
                nav_history.append((d, outcome.nav_after))

        final_pos = len(panel.index) - 1
        mtm_prices = prices_asof(panel.close.iloc[: final_pos + 1], list(book.qty()))
        final_nav = round(book.mark_to_market(mtm_prices), 2)
        final_date = panel.date_at(final_pos)
        journal.write("MTM", {"date": final_date.isoformat(), "nav": final_nav})
        journal.session_end(
            "OK",
            {
                "n_rebalances": len(nav_history),
                "final_nav": final_nav,
                "final_date": final_date.isoformat(),
                "seed_nav": config.seed_nav,
            },
        )

    return SimRunResult(
        session_id=session_id,
        journal_path=journal_path,
        config_hash=config_hash,
        code_sha=code_sha,
        data_snapshot_id=snapshot_id,
        nav_history=nav_history,
        rebal_positions=[p for _, p in schedule],
        final_mtm=(final_date, final_nav),
        final_pos=final_pos,
        panel=panel,
        book=book,
    )
