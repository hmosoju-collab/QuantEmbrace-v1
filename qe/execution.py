"""Execution: reconcile current book → target book; SimBroker fills at close.

Fill/cash/cost arithmetic matches `run_delivery_paper_book._rebalance` exactly
(full-sum proceeds/spend association, per-delta cost on notional, cash rounded
to 2 at the end) — this is what makes rupee-exact parity with the registered
forward books possible.
"""

from dataclasses import dataclass, field

from qe.costs import EquityDeliveryCosts


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str  # BUY | SELL
    qty: int
    price: float
    notional: float


@dataclass(frozen=True)
class Holding:
    qty: int
    avg_price: float


@dataclass
class Book:
    cash: float
    holdings: dict[str, Holding] = field(default_factory=dict)

    def qty(self) -> dict[str, int]:
        return {s: h.qty for s, h in self.holdings.items()}

    def mark_to_market(self, prices: dict[str, float]) -> float:
        pos_val = sum(h.qty * prices.get(s, h.avg_price) for s, h in self.holdings.items())
        return self.cash + pos_val


def reconcile(
    current_qty: dict[str, int], target_qty: dict[str, int], prices: dict[str, float]
) -> list[Order]:
    """Delta orders taking the current book to the target (symbols without a
    price are skipped — same as v1; they neither trade nor block the rest)."""
    orders: list[Order] = []
    for s in sorted(set(current_qty) | set(target_qty)):
        d = target_qty.get(s, 0) - current_qty.get(s, 0)
        px = prices.get(s)
        if d == 0 or px is None:
            continue
        orders.append(
            Order(
                symbol=s,
                side="BUY" if d > 0 else "SELL",
                qty=abs(d),
                price=round(px, 2),
                notional=round(abs(d) * px, 0),
            )
        )
    return orders


@dataclass(frozen=True)
class Fill:
    orders: tuple[Order, ...]
    buy_cost: float
    sell_cost: float

    @property
    def total_cost(self) -> float:
        return round(self.buy_cost + self.sell_cost, 2)


class SimBroker:
    """Simulated positional fills at the as-of close, with statutory costs.

    ``rebalance_fill`` replaces the whole book with the target in one shot
    (positional monthly semantics): sells everything priced, buys the target,
    charges per-leg costs on the *delta* notional only.
    """

    def __init__(self, costs: EquityDeliveryCosts | None = None):
        self.costs = costs or EquityDeliveryCosts()

    def rebalance_fill(
        self, book: Book, target_qty: dict[str, int], prices: dict[str, float]
    ) -> Fill:
        cur_qty = book.qty()
        buy_frac = self.costs.leg_cost_frac("BUY")
        sell_frac = self.costs.leg_cost_frac("SELL")

        orders = reconcile(cur_qty, target_qty, prices)
        buy_cost = sum(abs(o.qty) * prices[o.symbol] * buy_frac for o in orders if o.side == "BUY")
        sell_cost = sum(
            abs(o.qty) * prices[o.symbol] * sell_frac for o in orders if o.side == "SELL"
        )

        # Cash flow with v1's exact association: full proceeds minus full spend.
        spend = sum(q * prices[s] for s, q in target_qty.items())
        proceeds = sum(q * prices[s] for s, q in cur_qty.items() if s in prices)
        cash = book.cash + proceeds - spend - buy_cost - sell_cost

        new_holdings: dict[str, Holding] = {}
        for s, t in target_qty.items():
            if t <= 0:
                continue
            new_holdings[s] = Holding(qty=t, avg_price=round(prices[s], 4))

        book.holdings = new_holdings
        book.cash = round(cash, 2)
        return Fill(orders=tuple(orders), buy_cost=buy_cost, sell_cost=sell_cost)


class PaperBroker(SimBroker):
    """Paper fills: identical simulation to SimBroker, fed live prices instead of
    historical closes. It shares SimBroker's fill code by construction (the
    research→paper parity guarantee) and, being a distinct type that only ever
    simulates, it *cannot* reach a real broker API — paper/live isolation is a
    property of the type, not a config flag (RA-1 §2.4, F-8)."""

    venue = "paper-sim"


class LiveBrokerLocked(RuntimeError):
    """Raised when live execution is attempted without a valid gate token."""


class LiveBroker:
    """The real broker port — DOUBLE-GATED so no code path can trade by accident.

    Construction requires BOTH (1) a ``LiveGateToken`` that ``is_valid`` for the
    running config, and (2) an explicitly-supplied broker client. A token alone
    cannot trade (there is no client), and a client alone cannot trade (there is
    no token). The token is minted only by the evidence ceremony in
    ``qe.live_gate`` — which currently refuses. No real broker adapter is wired
    here yet; connecting one is a deliberate, human-gated step (M6+).
    """

    def __init__(self, *, token, config_hash: str, client):
        if token is None or not token.is_valid(config_hash):
            raise LiveBrokerLocked(
                "LiveBroker requires a LiveGateToken valid for this config. "
                "Mint one via qe.live_gate.mint_live_gate_token (it refuses until the "
                "evidence gate passes). Live trading remains BLOCKED."
            )
        if client is None:
            raise LiveBrokerLocked(
                "LiveBroker requires an explicitly-supplied broker client — a valid token "
                "alone must never be able to place an order."
            )
        self._token = token
        self._client = client
