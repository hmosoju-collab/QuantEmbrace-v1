"""Trade Exit Engine (TEE) simulator for the backtesting lab.

Replays a managed position bar-by-bar through an **R-based** exit policy and
records every exit decision, so we can compare the **old global** policy against
the **new strategy-aware** policy on identical signals.

R math (mirrors production `trade_exit_engine.py`):
    initial_risk = abs(entry - stop)
    LONG  R = (price - entry) / initial_risk
    SHORT R = (entry - price) / initial_risk

Exit priority per bar (production order):
    1. stop-loss / trailing stop      (highest — uses the stop as of this bar)
    2. breakeven shift                (move stop to entry at breakeven_at_r)
    3. partial profit booking         (once, at partial_profit_at_r)
    4. trailing activation / advance  (only ever tightens — never loosens)
    5. final target / fixed TP        (suppressed when trailing active, if configured)
    6. max-hold time exit
    7. hard time exit (IST)

MIS square-off (15:05 IST) is applied by `mis_simulator` as final cleanup only.
Fills are routed through the AWS-BT-6 `ExecutionSimulator`. Backtest-only — no
broker APIs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time, timedelta
from enum import Enum
from typing import Any

from shared.models.signal import Direction

from backtesting.execution_simulator import ExecutionConfig, ExecutionSimulator
from backtesting.mis_simulator import MISConfig, MISSimulator

IST = "Asia/Kolkata"


class ExitReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TRAILING = "TRAILING"
    BREAKEVEN = "BREAKEVEN"          # state change (stop → entry), not an exit
    PARTIAL_PROFIT = "PARTIAL_PROFIT"
    FINAL_TARGET = "FINAL_TARGET"
    TIME_EXIT = "TIME_EXIT"
    MIS_CLOSE = "MIS_CLOSE"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class TEEPolicy:
    name: str
    breakeven_at_r: float | None
    partial_profit_at_r: float | None
    partial_qty_pct: float            # 0–100
    trailing_activate_at_r: float | None
    trailing_distance_r: float | None
    final_target_r: float | None
    max_hold_minutes: int | None
    hard_exit_time: str | None        # "HH:MM" IST
    suppress_fixed_tp_after_trailing: bool


# Old global policy: pre-R-based behaviour — SL + fixed TP + global trailing at
# 1.25R. No breakeven, no partial booking, no time exits (rides to MIS).
OLD_GLOBAL_POLICY = TEEPolicy(
    name="old_global",
    breakeven_at_r=None,
    partial_profit_at_r=None,
    partial_qty_pct=0.0,
    trailing_activate_at_r=1.25,
    trailing_distance_r=0.6,
    final_target_r=2.5,
    max_hold_minutes=None,
    hard_exit_time=None,
    suppress_fixed_tp_after_trailing=False,
)

# New strategy-aware default (R-based): breakeven + partial + trailing + hard exit.
NEW_DEFAULT_POLICY = TEEPolicy(
    name="new_default",
    breakeven_at_r=1.0,
    partial_profit_at_r=1.0,
    partial_qty_pct=50.0,
    trailing_activate_at_r=1.25,
    trailing_distance_r=0.75,
    final_target_r=2.5,
    max_hold_minutes=None,
    hard_exit_time="15:00",
    suppress_fixed_tp_after_trailing=True,
)

# Per-strategy overrides for the NEW policy.
NEW_POLICIES: dict[str, TEEPolicy] = {
    "vwap_reversion": TEEPolicy(
        name="new_vwap_reversion", breakeven_at_r=0.75, partial_profit_at_r=1.0,
        partial_qty_pct=50.0, trailing_activate_at_r=1.25, trailing_distance_r=0.75,
        final_target_r=2.0, max_hold_minutes=30, hard_exit_time="15:00",
        suppress_fixed_tp_after_trailing=True,
    ),
    "preclose": TEEPolicy(
        name="new_preclose", breakeven_at_r=0.75, partial_profit_at_r=1.0,
        partial_qty_pct=50.0, trailing_activate_at_r=1.5, trailing_distance_r=0.75,
        final_target_r=2.0, max_hold_minutes=None, hard_exit_time="15:00",
        suppress_fixed_tp_after_trailing=True,
    ),
}


def get_policy(strategy: str | None = None, *, mode: str = "new") -> TEEPolicy:
    """Return the TEE policy. ``mode='old'`` → global; ``mode='new'`` → per-strategy."""
    if mode == "old":
        return OLD_GLOBAL_POLICY
    if strategy and strategy in NEW_POLICIES:
        return NEW_POLICIES[strategy]
    return NEW_DEFAULT_POLICY


def _hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def _ist(ts) -> Any:
    return ts.tz_convert(IST) if getattr(ts, "tz", None) is not None else ts


@dataclass
class ExitEvent:
    reason: ExitReason
    quantity: int
    price: float
    timestamp: Any
    r_multiple: float


@dataclass
class ManagedPosition:
    symbol: str
    direction: Direction          # BUY = long, SELL = short
    entry_price: float
    initial_stop: float
    quantity: int                 # absolute size
    entry_time: Any
    current_stop: float = 0.0
    trailing_stop: float | None = None
    breakeven_locked: bool = False
    partial_booked: bool = False
    trailing_active: bool = False
    remaining_qty: int = 0
    best_price: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    closed: bool = False
    exits: list[ExitEvent] = field(default_factory=list)
    stop_history: list[float] = field(default_factory=list)

    @property
    def is_long(self) -> bool:
        return self.direction == Direction.BUY

    @property
    def initial_risk(self) -> float:
        return abs(self.entry_price - self.initial_stop)

    def r_at(self, price: float) -> float:
        risk = self.initial_risk
        if risk <= 0:
            return 0.0
        return (price - self.entry_price) / risk if self.is_long else (self.entry_price - price) / risk


class TEESimulator:
    """Bar-by-bar R-based exit engine. Decisions only — fills via ExecutionSimulator."""

    def open_position(self, *, symbol, direction, entry_price, stop, quantity, entry_time) -> ManagedPosition:
        pos = ManagedPosition(
            symbol=symbol, direction=direction, entry_price=entry_price, initial_stop=stop,
            quantity=quantity, entry_time=entry_time, current_stop=stop,
            remaining_qty=quantity, best_price=entry_price,
        )
        pos.stop_history.append(stop)
        return pos

    def process_bar(self, pos: ManagedPosition, bar, policy: TEEPolicy) -> list[ExitEvent]:
        """Apply the exit priority for one bar. Returns events (partial and/or full)."""
        if pos.closed:
            return []
        events: list[ExitEvent] = []
        fav = bar.high if pos.is_long else bar.low      # favourable extreme
        adv = bar.low if pos.is_long else bar.high      # adverse extreme
        now_ist = _ist(bar.timestamp)

        # 1) Stop / trailing (uses the stop as of this bar — before any advance).
        eff_stop = pos.trailing_stop if (pos.trailing_active and pos.trailing_stop is not None) else pos.current_stop
        if self._stop_hit(pos, bar, eff_stop):
            price = self._stop_fill(pos, bar, eff_stop)
            reason = ExitReason.TRAILING if pos.trailing_active else ExitReason.STOP_LOSS
            events.append(self._close(pos, pos.remaining_qty, price, reason, bar.timestamp))
            return events

        # 2) MFE / MAE (favourable / adverse excursion in R).
        pos.mfe_r = max(pos.mfe_r, pos.r_at(fav))
        pos.mae_r = min(pos.mae_r, pos.r_at(adv))
        if pos.is_long:
            pos.best_price = max(pos.best_price, fav)
        else:
            pos.best_price = min(pos.best_price, fav)

        fav_r = pos.r_at(fav)

        # 3) Breakeven shift (stop → entry).
        if policy.breakeven_at_r is not None and not pos.breakeven_locked and fav_r >= policy.breakeven_at_r:
            pos.current_stop = pos.entry_price
            pos.breakeven_locked = True
            pos.stop_history.append(pos.current_stop)

        # 4) Partial profit booking (once, idempotent).
        if (
            policy.partial_profit_at_r is not None
            and not pos.partial_booked
            and policy.partial_qty_pct > 0
            and fav_r >= policy.partial_profit_at_r
            and pos.remaining_qty > 0
        ):
            book_qty = int(pos.quantity * policy.partial_qty_pct / 100.0)
            book_qty = min(book_qty, pos.remaining_qty)
            if book_qty > 0:
                price = self._target_price(pos, policy.partial_profit_at_r)
                pos.partial_booked = True
                events.append(self._book_partial(pos, book_qty, price, bar.timestamp))

        # 5) Trailing activation / advance (only tightens).
        if policy.trailing_activate_at_r is not None and policy.trailing_distance_r is not None:
            if fav_r >= policy.trailing_activate_at_r:
                pos.trailing_active = True
            if pos.trailing_active:
                dist = policy.trailing_distance_r * pos.initial_risk
                candidate = pos.best_price - dist if pos.is_long else pos.best_price + dist
                if pos.trailing_stop is None:
                    pos.trailing_stop = candidate
                    pos.stop_history.append(pos.trailing_stop)
                else:
                    tightened = max(pos.trailing_stop, candidate) if pos.is_long else min(pos.trailing_stop, candidate)
                    if tightened != pos.trailing_stop:
                        pos.trailing_stop = tightened
                        pos.stop_history.append(pos.trailing_stop)

        # 6) Final target / fixed TP (suppressed when trailing active, if configured).
        if (
            policy.final_target_r is not None
            and not (policy.suppress_fixed_tp_after_trailing and pos.trailing_active)
            and fav_r >= policy.final_target_r
            and pos.remaining_qty > 0
        ):
            price = self._target_price(pos, policy.final_target_r)
            events.append(self._close(pos, pos.remaining_qty, price, ExitReason.FINAL_TARGET, bar.timestamp))
            return events

        # 7) Max-hold time exit.
        if policy.max_hold_minutes is not None:
            held = bar.timestamp - pos.entry_time
            if held >= timedelta(minutes=policy.max_hold_minutes) and pos.remaining_qty > 0:
                events.append(self._close(pos, pos.remaining_qty, float(bar.close), ExitReason.TIME_EXIT, bar.timestamp))
                return events

        # 8) Hard time exit (IST).
        if policy.hard_exit_time is not None and now_ist.time() >= _hhmm(policy.hard_exit_time):
            if pos.remaining_qty > 0:
                events.append(self._close(pos, pos.remaining_qty, float(bar.close), ExitReason.TIME_EXIT, bar.timestamp))
                return events

        return events

    # ── helpers ──────────────────────────────────────────────────────────────
    def _stop_hit(self, pos: ManagedPosition, bar, eff_stop: float) -> bool:
        return bar.low <= eff_stop if pos.is_long else bar.high >= eff_stop

    def _stop_fill(self, pos: ManagedPosition, bar, eff_stop: float) -> float:
        # Gap-through fills at the worse open.
        if pos.is_long:
            return float(bar.open) if bar.open < eff_stop else float(eff_stop)
        return float(bar.open) if bar.open > eff_stop else float(eff_stop)

    def _target_price(self, pos: ManagedPosition, r: float) -> float:
        return pos.entry_price + r * pos.initial_risk if pos.is_long else pos.entry_price - r * pos.initial_risk

    def _book_partial(self, pos: ManagedPosition, qty: int, price: float, ts) -> ExitEvent:
        pos.remaining_qty -= qty
        ev = ExitEvent(ExitReason.PARTIAL_PROFIT, qty, price, ts, pos.r_at(price))
        pos.exits.append(ev)
        return ev

    def _close(self, pos: ManagedPosition, qty: int, price: float, reason: ExitReason, ts) -> ExitEvent:
        qty = min(qty, pos.remaining_qty)
        pos.remaining_qty -= qty
        ev = ExitEvent(reason, qty, price, ts, pos.r_at(price))
        pos.exits.append(ev)
        if pos.remaining_qty <= 0:
            pos.closed = True
        return ev


# ── trade lifecycle + metrics ───────────────────────────────────────────────────


@dataclass
class TradeOutcome:
    symbol: str
    policy: str
    direction: str
    entry_price: float
    initial_risk: float
    quantity: int
    exits: list[ExitEvent]
    realized_pnl: float
    realized_r: float
    mfe_r: float
    mae_r: float
    profit_capture_ratio: float
    giveback_ratio: float
    final_reason: str
    mis_dependent: bool
    unresolved: bool
    total_costs: float
    total_slippage: float


def _frictionless() -> ExecutionConfig:
    return ExecutionConfig(
        enable_statutory_costs=False, brokerage_pct=0.0, slippage_bps_liquid=0.0,
        slippage_bps_mid=0.0, slippage_bps_illiquid=0.0, spread_bps=0.0, min_net_edge_pct=0.0,
    )


def run_trade(
    *,
    symbol: str,
    direction: Direction,
    entry_price: float,
    stop: float,
    quantity: int,
    entry_time,
    bars,
    policy: TEEPolicy,
    mis: MISSimulator | None = None,
    exec_sim: ExecutionSimulator | None = None,
    daily_cap_hit: bool = False,
) -> TradeOutcome:
    """Drive one managed position through bars: entry → TEE exits → MIS cleanup.

    ``daily_cap_hit`` is accepted to document the invariant that a daily cap blocks
    NEW ENTRIES only — it **never** suppresses exits here.
    """
    tee = TEESimulator()
    mis = mis or MISSimulator()
    exec_sim = exec_sim or ExecutionSimulator(_frictionless())
    opp = Direction.SELL if direction == Direction.BUY else Direction.BUY

    pos = tee.open_position(
        symbol=symbol, direction=direction, entry_price=entry_price, stop=stop,
        quantity=quantity, entry_time=entry_time,
    )
    exec_sim.submit(symbol, direction, quantity, entry_price, enforce_edge=False)

    for bar in bars:
        # TEE exits/partials always run regardless of daily_cap_hit.
        for ev in tee.process_bar(pos, bar, policy):
            exec_sim.submit(symbol, opp, ev.quantity, ev.price, enforce_edge=False)
        if pos.closed:
            break
        # MIS final cleanup for any still-open position at/after 15:05 IST.
        if mis.should_square_off(bar.timestamp) and not pos.closed:
            price = mis.square_off_price(bar)
            ev = tee._close(pos, pos.remaining_qty, price, ExitReason.MIS_CLOSE, bar.timestamp)
            exec_sim.submit(symbol, opp, ev.quantity, ev.price, enforce_edge=False)
            break

    unresolved = not pos.closed
    if unresolved and pos.remaining_qty > 0 and bars:
        # EOD with no square-off seen → mark unresolved (still flatten for accounting).
        last = bars[-1]
        ev = tee._close(pos, pos.remaining_qty, float(last.close), ExitReason.UNRESOLVED, last.timestamp)
        exec_sim.submit(symbol, opp, ev.quantity, ev.price, enforce_edge=False)

    return _build_outcome(pos, policy, exec_sim)


def _build_outcome(pos: ManagedPosition, policy: TEEPolicy, exec_sim: ExecutionSimulator) -> TradeOutcome:
    realized_pnl = exec_sim.realized_pnl
    denom = pos.initial_risk * pos.quantity
    realized_r = realized_pnl / denom if denom > 0 else 0.0
    mfe_r = pos.mfe_r
    capture = (realized_r / mfe_r) if mfe_r > 0 else 0.0
    giveback = max(0.0, (mfe_r - realized_r) / mfe_r) if mfe_r > 0 else 0.0
    final_reason = pos.exits[-1].reason.value if pos.exits else ExitReason.UNRESOLVED.value
    return TradeOutcome(
        symbol=pos.symbol,
        policy=policy.name,
        direction="LONG" if pos.is_long else "SHORT",
        entry_price=pos.entry_price,
        initial_risk=pos.initial_risk,
        quantity=pos.quantity,
        exits=list(pos.exits),
        realized_pnl=realized_pnl,
        realized_r=realized_r,
        mfe_r=mfe_r,
        mae_r=pos.mae_r,
        profit_capture_ratio=capture,
        giveback_ratio=giveback,
        final_reason=final_reason,
        mis_dependent=(final_reason == ExitReason.MIS_CLOSE.value),
        unresolved=(final_reason == ExitReason.UNRESOLVED.value),
        total_costs=exec_sim.total_costs,
        total_slippage=exec_sim.total_slippage,
    )


def compare_policies(trade_spec: dict, bars, *, strategy: str | None = None, **kw) -> dict[str, TradeOutcome]:
    """Run the SAME trade under old and new TEE policies; return both outcomes."""
    old = run_trade(bars=bars, policy=get_policy(strategy, mode="old"), **trade_spec, **kw)
    new = run_trade(bars=bars, policy=get_policy(strategy, mode="new"), **trade_spec, **kw)
    return {"old": old, "new": new}
