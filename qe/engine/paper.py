"""Paper engine — a backtest running in real time (RA-1 §2.3).

A paper session advances the persisted book by the completed month-end
rebalances it still owes (normally one, executed at that month-end's own
row/prices), using the *same* `execute_rebalance` step the backtest uses.
Mid-month sessions are MTM-only. Successive sessions build an out-of-sample
track record on sim's complete-month schedule. The
only differences from sim are the clock (wall vs event time), the data source
(live lake vs pinned snapshot), the broker type (PaperBroker vs SimBroker —
same fills), state persistence, and the always-on kill switch.

Isolation is structural: PaperBroker cannot reach a real broker, and the live
broker is unconstructible without an M6 gate token. Live trading stays BLOCKED.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
import uuid

import pandas as pd

from qe.clock import Clock, SimClock, WallClock, market_close_time, market_tz
from qe.config import RunConfig
from qe.costs import cost_model_for_market
from qe.data.feed import LiveLakeFeed, reference_symbol_for_market
from qe.data.panel import Panel
from qe.engine.book_store import BookState, BookStoreError, load_book_state, save_book_state
from qe.engine.core import execute_rebalance, prices_asof
from qe.engine.sim import month_end_positions
from qe.execution import PaperBroker
from qe.journal import JournalWriter
from qe.killswitch import KillSwitch, drawdown_trigger, staleness_trigger
from qe.strategy import Context, FactorBookStrategy, RiskParityLiteStrategy
from qe.version import code_version


@dataclass(frozen=True)
class PaperSessionResult:
    session_id: str
    journal_path: Path
    config_hash: str
    code_sha: str
    data_snapshot_id: str
    as_of: date
    due: bool
    rebalanced: bool
    kill_active: bool
    nav: float
    state_path: Path
    book_state: BookState


def _panel_dates(panel: Panel) -> list[date]:
    # The index is already normalized to the market's own trading-date tz by
    # load_panel(tz=...) — no forced re-conversion (ADR-041 P3/P5; the same
    # fix as Panel.date_at and qe.engine.sim.month_end_positions).
    return [pd.Timestamp(t).date() for t in panel.index]


def _row_for_date(panel: Panel, d: date) -> int | None:
    dates = _panel_dates(panel)
    for pos in range(len(dates) - 1, -1, -1):  # last row matching the date
        if dates[pos] == d:
            return pos
    return None


def _pending_rebalances(
    panel: Panel, *, inception: date, last_rebalance: date | None, as_of: date, today: date
) -> list[tuple[date, int]]:
    """Completed month-end trading rows the book still owes a rebalance for.

    Paper follows sim's complete-month rule (`qe.engine.sim.rebalance_schedule`):
    a month-end is due only when its month is provably over — a later calendar
    month exists in the panel, or the wall clock is already past it. The panel's
    frontier row is never assumed to be a month-end (the next lake refresh may
    extend the same month), so a mid-month session with fresh data is not-due
    rather than an off-schedule rebalance.
    """
    month_ends = month_end_positions(panel.index, inception)
    pending: list[tuple[date, int]] = []
    for i, (d, pos) in enumerate(month_ends):
        if d > as_of:
            break
        month_over = i + 1 < len(month_ends) or (today.year, today.month) > (d.year, d.month)
        if month_over and (last_rebalance is None or d > last_rebalance):
            pending.append((d, pos))
    return pending


def _build_strategy(config: RunConfig) -> FactorBookStrategy | RiskParityLiteStrategy:
    s = config.strategy
    assert s is not None
    if s.kind == "risk_parity_lite":
        return RiskParityLiteStrategy(
            assets=s.assets, vol_lookback=s.vol_lookback, cash_buffer=s.cash_buffer
        )
    return FactorBookStrategy(
        factor=s.factor, top_n=s.top_n, k=s.k, max_weight=s.max_weight, cash_buffer=s.cash_buffer
    )


def run_paper(
    config: RunConfig,
    *,
    base_dir: str | Path = ".",
    as_of: date | None = None,
    panel: Panel | None = None,
    panel_snapshot_id: str | None = None,
    state_path: str | Path | None = None,
    kill_path: str | Path | None = None,
    clock: Clock | None = None,
) -> PaperSessionResult:
    """Run one paper session. Injecting ``panel``/``clock`` is for tests and the
    paper==sim parity proof; a real session uses a LiveLakeFeed + WallClock."""
    base_dir = Path(base_dir)
    if config.strategy is None:
        raise ValueError("paper mode requires a strategy section")
    strategy = _build_strategy(config)

    # ── data ──────────────────────────────────────────────────────────────
    if panel is None:
        feed = LiveLakeFeed(
            base_dir / config.data.lake_root,
            market=config.universe.market,
            segment=config.universe.segment,
            interval=config.data.interval,
            reference_symbol=reference_symbol_for_market(config.universe.market),
        )
        as_of = as_of or feed.latest_date()
        warmup_start = date(as_of.year - 2, as_of.month, 1)
        panel, snapshot_id = feed.load(warmup_start, as_of)
    else:
        snapshot_id = panel_snapshot_id or "ds-injected"
        as_of = as_of or panel.date_at(len(panel.index) - 1)

    pos = _row_for_date(panel, as_of)
    if pos is None:
        raise ValueError(f"as_of {as_of} not a trading day in the panel")

    clock = clock or WallClock(tz=market_tz(config.universe.market))

    # ── state (resume or seed), fail-closed on config drift ───────────────
    state_path = (
        Path(state_path)
        if state_path
        else (base_dir / "backtest-data" / "paper_book" / f"qe_{config.name}_state.json")
    )
    config_hash = config.config_hash()
    state = load_book_state(state_path)
    if state is None:
        state = BookState.seed(inception=as_of, seed_nav=config.seed_nav, config_hash=config_hash)
    elif state.config_hash != config_hash:
        raise BookStoreError(
            f"config hash drift on live book {state_path}: state was built under "
            f"{state.config_hash[:12]}, this run is {config_hash[:12]}. Refusing to continue a "
            "different experiment on the same book (fail-closed)."
        )
    book = state.to_book()

    kill = KillSwitch(
        kill_path or base_dir / "backtest-data" / "paper_book" / "qe_kill_switch.json"
    )

    code_sha = code_version(base_dir)
    # Microsecond + short random suffix so back-to-back sessions (retries, tests,
    # a resumed month-end run) never collide on the journal filename.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    session_id = f"{config.name}-{stamp}-{uuid.uuid4().hex[:6]}-{config_hash[:12]}"
    journal_path = base_dir / config.journal_dir / f"paper-{session_id}.jsonl"

    prices_now = prices_asof(panel.close.iloc[: pos + 1], list(book.qty()))
    nav_now = round(book.mark_to_market(prices_now), 2)
    peak_nav = max([h["nav"] for h in state.nav_history] + [nav_now], default=nav_now)
    data_age_days = (clock.today() - as_of).days

    pending = _pending_rebalances(
        panel,
        inception=date.fromisoformat(state.inception),
        last_rebalance=(date.fromisoformat(state.last_rebalance) if state.last_rebalance else None),
        as_of=as_of,
        today=clock.today(),
    )
    due = bool(pending)
    rebalanced = False

    with JournalWriter(journal_path) as journal:
        journal.session_start(
            session_id=session_id,
            mode="paper",
            config_hash=config_hash,
            config=config.model_dump(mode="json"),
            code_sha=code_sha,
            data_snapshot_id=snapshot_id,
        )
        journal.write(
            "PAPER_SESSION",
            {
                "as_of": as_of.isoformat(),
                "clock": clock.kind,
                "now": clock.now().isoformat(),
                "data_age_days": data_age_days,
                "nav_open": nav_now,
                "due": due,
                "due_dates": [d.isoformat() for d, _ in pending],
                "broker": PaperBroker.venue,
            },
        )

        # ── auto-triggers → idempotent kill (retires the self-refire class) ──
        for reason in (
            staleness_trigger(data_age_days, config.risk.max_data_age_days),
            drawdown_trigger(nav_now, peak_nav, config.risk.max_drawdown_frac),
        ):
            if reason:
                st = kill.activate(reason, by="auto-trigger")
                journal.write("KILL_TRIGGERED", {"reason": st.reason, "at": st.activated_at})

        kill_active = kill.is_active()

        if not due:
            journal.write("REBALANCE_SKIPPED", {"date": as_of.isoformat(), "reason": "not-due"})
        elif kill_active:
            # A rebalance was due but the kill switch is active — suppress and
            # journal it (the intent must be auditable), leave the book untouched.
            st = kill.state()
            journal.write(
                "KILL_BLOCKED",
                {"date": as_of.isoformat(), "reason": st.reason, "activated_by": st.activated_by},
            )
        else:
            broker = PaperBroker(cost_model_for_market(config.universe.market))
            # Each owed month-end executes at its own row/prices — the same
            # (date, row) sim's schedule would use, preserving paper==sim.
            # Normally one; more only if sessions were missed (catch-up, like
            # sim replaying the same months). Mirrors sim: a rejected rebalance
            # is journaled and the loop continues.
            for due_date, due_pos in pending:
                outcome = execute_rebalance(
                    book=book,
                    ctx=Context.at(panel, due_pos),
                    on_date=due_date,
                    strategy=strategy,
                    broker=broker,
                    risk_cfg=config.risk,
                    journal=journal,
                    kill=kill,
                )
                if outcome.executed:
                    rebalanced = True
                    state.nav_history.append(
                        {"date": due_date.isoformat(), "nav": outcome.nav_after}
                    )
                    state.last_rebalance = due_date.isoformat()

        prices_final = prices_asof(panel.close.iloc[: pos + 1], list(book.qty()))
        nav_final = round(book.mark_to_market(prices_final), 2)
        save_book_state(state_path, state, book)
        journal.write("MTM", {"date": as_of.isoformat(), "nav": nav_final})
        journal.session_end(
            "OK",
            {
                "as_of": as_of.isoformat(),
                "due": due,
                "rebalanced": rebalanced,
                "kill_active": kill_active,
                "nav": nav_final,
                "n_rebalances_total": len(state.nav_history),
            },
        )

    return PaperSessionResult(
        session_id=session_id,
        journal_path=journal_path,
        config_hash=config_hash,
        code_sha=code_sha,
        data_snapshot_id=snapshot_id,
        as_of=as_of,
        due=due,
        rebalanced=rebalanced,
        kill_active=kill_active,
        nav=nav_final,
        state_path=state_path,
        book_state=state,
    )


def sim_clock_at(d: date, *, market: str = "NSE") -> SimClock:
    """A SimClock pinned to a market's own close time/tz on a trading date —
    used by tests/parity to run paper deterministically at a historical
    instant. Defaults to the NSE close (15:30 IST) — existing NSE callers are
    unaffected; pass ``market="US"`` for the NYSE close (16:00 ET, ADR-041 P5)."""
    return SimClock(datetime.combine(d, market_close_time(market), tzinfo=market_tz(market)))
