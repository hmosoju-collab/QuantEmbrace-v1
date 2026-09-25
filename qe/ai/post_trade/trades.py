"""Completed trades, reconstructed read-only from an engine journal.

A trade is a position EPISODE: from the rebalance where a symbol's quantity
goes 0 → >0 to the rebalance where it returns to 0. Monthly re-weighting
buys/sells inside an episode are folded in (quantity-weighted prices), so a
review sees what the book actually did with the name. Each rebalance's total
cost is allocated to its orders pro rata by notional. Open episodes (still
held at the journal's end) are not completed trades and are not returned.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from qe.journal import read_header, read_journal


@dataclass(frozen=True)
class Trade:
    session_id: str
    symbol: str
    open_date: date
    close_date: date
    qty_bought: int
    qty_sold: int
    buy_notional: float
    sell_notional: float
    costs: float

    @property
    def entry_price(self) -> float:
        return self.buy_notional / self.qty_bought

    @property
    def exit_price(self) -> float:
        return self.sell_notional / self.qty_sold

    @property
    def net_pnl(self) -> float:
        return self.sell_notional - self.buy_notional - self.costs

    @property
    def net_return(self) -> float:
        return self.net_pnl / self.buy_notional


@dataclass
class _Episode:
    open_date: date
    qty: int = 0
    qty_bought: int = 0
    qty_sold: int = 0
    buy_notional: float = 0.0
    sell_notional: float = 0.0
    costs: float = 0.0
    fills: list[tuple[date, str, float]] = field(default_factory=list)


def completed_trades(engine_journal: str | Path) -> list[Trade]:
    header = read_header(engine_journal)
    session = header["session_id"]
    open_eps: dict[str, _Episode] = {}
    done: list[Trade] = []
    for rec in read_journal(engine_journal):
        if rec["type"] != "REBALANCE":
            continue
        d = date.fromisoformat(rec["data"]["date"])
        orders = rec["data"]["orders"]
        total = sum(abs(o["qty"]) * o["price"] for o in orders) or 1.0
        cost = float(rec["data"].get("cost", 0.0))
        for o in sorted(orders, key=lambda o: (o["side"] != "SELL", o["symbol"])):
            notional = abs(o["qty"]) * o["price"]
            ep = open_eps.get(o["symbol"])
            if ep is None:
                if o["side"] != "BUY":
                    continue  # a sell without a known entry (journal starts mid-position)
                ep = open_eps[o["symbol"]] = _Episode(open_date=d)
            ep.costs += cost * notional / total
            if o["side"] == "BUY":
                ep.qty += o["qty"]
                ep.qty_bought += o["qty"]
                ep.buy_notional += notional
            else:
                ep.qty -= o["qty"]
                ep.qty_sold += o["qty"]
                ep.sell_notional += notional
            if ep.qty <= 0:
                done.append(
                    Trade(
                        session,
                        o["symbol"],
                        ep.open_date,
                        d,
                        ep.qty_bought,
                        ep.qty_sold,
                        ep.buy_notional,
                        ep.sell_notional,
                        ep.costs,
                    )
                )
                del open_eps[o["symbol"]]
    return done


def open_positions(engine_journal: str | Path) -> dict[str, int]:
    """Quantity still held at the journal's end (for reporting only)."""
    qty: dict[str, int] = defaultdict(int)
    for rec in read_journal(engine_journal):
        if rec["type"] == "REBALANCE":
            for o in rec["data"]["orders"]:
                qty[o["symbol"]] += o["qty"] if o["side"] == "BUY" else -o["qty"]
    return {s: q for s, q in qty.items() if q > 0}
