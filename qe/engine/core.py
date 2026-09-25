"""The shared rebalance step — the atom of the one engine (RA-1 §2.3, F-3).

`execute_rebalance` is the single code path that turns a point-in-time market
view into orders and fills. Backtest (SimClock, `qe.engine.sim`) and paper
(WallClock, `qe.engine.paper`) both call it, so what is researched is
byte-for-byte what is paper-traded. The arithmetic here is lifted verbatim from
the M2 sim loop that holds ₹0.00 parity with the registered forward books —
do not alter it without re-running the parity suite.
"""

from dataclasses import dataclass
from datetime import date

from qe import risk
from qe.config import RiskConfig
from qe.execution import Book, Fill, SimBroker, reconcile
from qe.journal import JournalWriter
from qe.killswitch import KillSwitch
from qe.portfolio import net_targets, size_targets
from qe.strategy import Context, Strategy


@dataclass(frozen=True)
class RebalanceOutcome:
    date: date
    approved: bool
    executed: bool
    nav_after: float | None
    fill: Fill | None
    reject_reason: str  # "" when approved/executed; risk detail or "kill-switch active"


def execute_rebalance(
    *,
    book: Book,
    ctx: Context,
    on_date: date,
    strategy: Strategy,
    broker: SimBroker,
    risk_cfg: RiskConfig,
    journal: JournalWriter,
    kill: KillSwitch | None = None,
) -> RebalanceOutcome:
    """Run one rebalance against ``book`` and journal every decision.

    ``kill=None`` (backtest) skips the kill check entirely, so the sim numeric
    path is unchanged. A paper/live caller passes a real KillSwitch: an active
    switch blocks order emission *after* strategy+risk are journaled (so the
    intent is still auditable) and leaves the book untouched.
    """
    weights = net_targets([strategy.rebalance(ctx)])
    prices = prices_asof(ctx.close, list(weights) + list(book.qty()))
    nav_before = book.mark_to_market(prices)
    target_qty = size_targets(weights, nav_before, prices)

    # Projected cash for the risk check, same association as the fill.
    spend = sum(q * prices[s] for s, q in target_qty.items())
    proceeds = sum(q * prices[s] for s, q in book.qty().items() if s in prices)
    est_cost = spend * broker.costs.leg_cost_frac("BUY") + proceeds * broker.costs.leg_cost_frac(
        "SELL"
    )
    traded_notional = sum(
        abs(o.qty) * prices[o.symbol] for o in reconcile(book.qty(), target_qty, prices)
    )
    proposal = risk.Proposal(
        weights=weights,
        target_qty=target_qty,
        prices=prices,
        nav=nav_before,
        projected_cash=book.cash + proceeds - spend - est_cost,
        max_weight=strategy.max_weight if hasattr(strategy, "max_weight") else 1.0,
        turnover_frac=traded_notional / nav_before if nav_before > 0 else 0.0,
        max_positions=risk_cfg.max_positions,
        max_turnover_frac=risk_cfg.max_turnover_frac,
    )
    verdict = risk.evaluate(proposal)
    journal.write(
        "RISK",
        {
            "date": on_date.isoformat(),
            "approved": verdict.approved,
            "checks": [{"check": r.check, "ok": r.ok, "detail": r.detail} for r in verdict.results],
        },
    )
    if not verdict.approved:
        journal.write("REBALANCE_SKIPPED", {"date": on_date.isoformat(), "reason": "risk"})
        detail = "; ".join(f"{r.check}:{r.detail}" for r in verdict.rejections)
        return RebalanceOutcome(on_date, False, False, None, None, detail)

    # Final hard gate before any order leaves the engine (RA-1 §2.4).
    if kill is not None and kill.is_active():
        st = kill.state()
        journal.write(
            "KILL_BLOCKED",
            {"date": on_date.isoformat(), "reason": st.reason, "activated_by": st.activated_by},
        )
        return RebalanceOutcome(on_date, True, False, None, None, "kill-switch active")

    fill = broker.rebalance_fill(book, target_qty, prices)
    nav_after = round(book.mark_to_market(prices), 2)
    journal.write(
        "REBALANCE",
        {
            "date": on_date.isoformat(),
            "n_targets": len(target_qty),
            "orders": [o.__dict__ for o in fill.orders],
            "cost": fill.total_cost,
            "cash": book.cash,
            "nav": nav_after,
        },
    )
    return RebalanceOutcome(on_date, True, True, nav_after, fill, "")


def prices_asof(close, symbols: list[str]) -> dict[str, float]:
    """Last valid (forward-filled) price per symbol — shared by both engines."""
    import pandas as pd

    last = close.ffill().iloc[-1]
    return {s: float(last[s]) for s in symbols if s in last.index and not pd.isna(last[s])}
