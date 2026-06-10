"""Execution simulator + NSE cost/slippage model for the backtesting lab.

A deterministic, **paper/backtest-only** fill simulator. It applies the full NSE
intraday cost stack (brokerage, STT, exchange transaction charges, SEBI fees, GST,
stamp duty) plus spread and tiered slippage, tracks **signed** positions with
correct long/short P&L, supports optional partial fills, tracks turnover, and can
reject trades whose net edge is below a configurable floor.

Reuses the statutory rates from the existing engine's ``IndianCostModel`` (single
source of truth) and the canonical ``Direction`` enum, so simulated economics match
the production `Backtester` and signals stay production-compatible.

**No broker APIs.** Nothing here connects to any live broker or places orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shared.models.signal import Direction

from strategy_engine.backtesting.backtester import IndianCostModel

# Liquidity tiers for slippage selection.
LIQUID = "liquid"
MID = "mid"
ILLIQUID = "illiquid"


@dataclass(frozen=True)
class ExecutionConfig:
    """Configurable cost/slippage assumptions (all bps unless noted)."""

    brokerage_pct: float = 0.03                 # per-leg brokerage, percent
    enable_statutory_costs: bool = True         # STT/exchange/SEBI/GST/stamp
    indian_costs: IndianCostModel = field(default_factory=IndianCostModel)

    # Slippage tiers (bps) + spread (bps). Half the spread is paid per fill.
    slippage_bps_liquid: float = 1.0
    slippage_bps_mid: float = 3.0
    slippage_bps_illiquid: float = 8.0
    spread_bps: float = 5.0

    # Optional flat round-trip cost estimate (bps) used by the edge gate when > 0.
    intraday_round_trip_cost_bps: float = 0.0

    # Net-edge gate: reject trades whose expected net edge (%) is below this.
    min_net_edge_pct: float = 0.0

    allow_partial_fills: bool = False

    def slippage_bps_for(self, tier: str) -> float:
        return {
            LIQUID: self.slippage_bps_liquid,
            MID: self.slippage_bps_mid,
            ILLIQUID: self.slippage_bps_illiquid,
        }.get(tier, self.slippage_bps_liquid)


@dataclass
class CostBreakdown:
    """Itemised costs for a single fill (currency units)."""

    brokerage: float = 0.0
    stt: float = 0.0
    exchange_txn: float = 0.0
    sebi: float = 0.0
    gst: float = 0.0
    stamp: float = 0.0
    slippage: float = 0.0  # implicit cost embedded in the worse fill price
    spread: float = 0.0    # half-spread component of slippage (informational)

    @property
    def statutory_total(self) -> float:
        """Explicit fees charged on top of the trade value (excludes slippage)."""
        return self.brokerage + self.stt + self.exchange_txn + self.sebi + self.gst + self.stamp

    @property
    def total(self) -> float:
        """All-in cost view: explicit fees + price-embedded slippage."""
        return self.statutory_total + self.slippage


@dataclass
class Fill:
    symbol: str
    side: Direction
    requested_qty: int
    filled_qty: int
    reference_price: float
    fill_price: float
    value: float
    costs: CostBreakdown
    rejected: bool = False
    reason: str | None = None


@dataclass
class Position:
    symbol: str
    quantity: int = 0          # SIGNED: > 0 long, < 0 short, 0 flat
    avg_price: float = 0.0
    realized_pnl: float = 0.0  # net of statutory costs (slippage is in fill prices)

    def unrealized_pnl(self, mark: float) -> float:
        if self.quantity == 0:
            return 0.0
        if self.quantity > 0:
            return (mark - self.avg_price) * self.quantity
        return (self.avg_price - mark) * abs(self.quantity)


class ExecutionSimulator:
    """Stateful, deterministic fill simulator (paper/backtest only)."""

    def __init__(self, config: ExecutionConfig | None = None) -> None:
        self._cfg = config or ExecutionConfig()
        self._positions: dict[str, Position] = {}
        self._turnover: float = 0.0
        self._total_statutory: float = 0.0
        self._total_slippage: float = 0.0
        self._fills: list[Fill] = []

    # ── public accessors ──────────────────────────────────────────────────────
    @property
    def turnover(self) -> float:
        return self._turnover

    @property
    def total_costs(self) -> float:
        """Explicit statutory/brokerage costs across all fills."""
        return self._total_statutory

    @property
    def total_slippage(self) -> float:
        return self._total_slippage

    @property
    def realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self._positions.values())

    @property
    def fills(self) -> list[Fill]:
        return list(self._fills)

    def position(self, symbol: str) -> Position:
        return self._positions.setdefault(symbol, Position(symbol=symbol))

    def net_signed_quantity(self, symbol: str) -> int:
        return self.position(symbol).quantity

    # ── pricing / costs ───────────────────────────────────────────────────────
    def apply_slippage(self, reference_price: float, side: Direction, tier: str) -> tuple[float, float, float]:
        """Return (fill_price, slippage_per_unit, half_spread_per_unit)."""
        slip_bps = self._cfg.slippage_bps_for(tier)
        half_spread_bps = self._cfg.spread_bps / 2.0
        adverse_bps = slip_bps + half_spread_bps
        direction = 1.0 if side == Direction.BUY else -1.0
        fill_price = reference_price * (1.0 + direction * adverse_bps / 10_000.0)
        slippage_per_unit = abs(fill_price - reference_price)
        half_spread_per_unit = reference_price * half_spread_bps / 10_000.0
        return fill_price, slippage_per_unit, half_spread_per_unit

    def compute_costs(self, trade_value: float, side: Direction, *, market: str = "NSE") -> CostBreakdown:
        """Itemised statutory + brokerage costs for one leg (mirrors IndianCostModel)."""
        cb = CostBreakdown()
        cb.brokerage = trade_value * (self._cfg.brokerage_pct / 100.0)
        m = self._cfg.indian_costs
        if self._cfg.enable_statutory_costs and market.upper() in {"NSE", "BSE"}:
            cb.exchange_txn = trade_value * (m.exchange_txn_pct / 100.0)
            cb.sebi = trade_value * (m.sebi_turnover_pct / 100.0)
            cb.stt = (
                trade_value * (m.stt_sell_pct / 100.0)
                if side == Direction.SELL
                else trade_value * (m.stt_buy_pct / 100.0)
            )
            cb.stamp = trade_value * (m.stamp_buy_pct / 100.0) if side == Direction.BUY else 0.0
            cb.gst = (cb.brokerage + cb.exchange_txn + cb.sebi) * (m.gst_pct / 100.0)
        return cb

    # ── edge gate ─────────────────────────────────────────────────────────────
    def round_trip_cost_pct(self, price: float, tier: str = LIQUID) -> float:
        """Estimated round-trip cost as a percent of notional at ``price``."""
        if self._cfg.intraday_round_trip_cost_bps > 0:
            return self._cfg.intraday_round_trip_cost_bps / 100.0  # bps → percent
        if price <= 0:
            return 0.0
        cost_in = self.compute_costs(price, Direction.BUY).statutory_total
        cost_out = self.compute_costs(price, Direction.SELL).statutory_total
        statutory_pct = (cost_in + cost_out) / price * 100.0
        slip_pct = (2 * self._cfg.slippage_bps_for(tier) + self._cfg.spread_bps) / 100.0
        return statutory_pct + slip_pct

    def net_edge_pct(self, entry: float, target: float, *, tier: str = LIQUID) -> float:
        if entry <= 0:
            return 0.0
        gross = abs(target - entry) / entry * 100.0
        return gross - self.round_trip_cost_pct(entry, tier)

    def accept_trade(self, entry: float, target: float, *, tier: str = LIQUID) -> bool:
        return self.net_edge_pct(entry, target, tier=tier) >= self._cfg.min_net_edge_pct

    # ── sizing ────────────────────────────────────────────────────────────────
    @staticmethod
    def position_size(nav: float, risk_pct: float, entry: float, stop: float) -> int:
        """Risk-based size: floor(nav * risk_pct / risk_per_unit)."""
        risk_per_unit = abs(entry - stop)
        if risk_per_unit <= 0:
            return 0
        return int((nav * risk_pct) / risk_per_unit)

    # ── order entry ───────────────────────────────────────────────────────────
    def submit(
        self,
        symbol: str,
        side: Direction,
        quantity: int,
        reference_price: float,
        *,
        tier: str = LIQUID,
        market: str = "NSE",
        target: float | None = None,
        fill_ratio: float = 1.0,
        enforce_edge: bool = True,
    ) -> Fill:
        """Simulate a fill. Returns a ``Fill`` (``rejected=True`` if gated)."""
        if quantity <= 0:
            raise ValueError("quantity must be positive (side carries direction)")

        # Net-edge gate.
        if (
            enforce_edge
            and target is not None
            and self._cfg.min_net_edge_pct > 0
            and not self.accept_trade(reference_price, target, tier=tier)
        ):
            return Fill(
                symbol=symbol, side=side, requested_qty=quantity, filled_qty=0,
                reference_price=reference_price, fill_price=reference_price, value=0.0,
                costs=CostBreakdown(), rejected=True,
                reason=(
                    f"net edge {self.net_edge_pct(reference_price, target, tier=tier):.3f}% "
                    f"< min {self._cfg.min_net_edge_pct:.3f}%"
                ),
            )

        # Partial fills.
        if self._cfg.allow_partial_fills:
            filled_qty = max(0, int(quantity * max(0.0, min(1.0, fill_ratio))))
        else:
            filled_qty = quantity
        if filled_qty == 0:
            return Fill(
                symbol=symbol, side=side, requested_qty=quantity, filled_qty=0,
                reference_price=reference_price, fill_price=reference_price, value=0.0,
                costs=CostBreakdown(), rejected=True, reason="zero fill",
            )

        fill_price, slip_per_unit, half_spread_unit = self.apply_slippage(reference_price, side, tier)
        value = fill_price * filled_qty
        costs = self.compute_costs(value, side, market=market)
        costs.slippage = slip_per_unit * filled_qty
        costs.spread = half_spread_unit * filled_qty

        self._apply_to_position(symbol, side, filled_qty, fill_price, costs.statutory_total)
        self._turnover += value
        self._total_statutory += costs.statutory_total
        self._total_slippage += costs.slippage

        fill = Fill(
            symbol=symbol, side=side, requested_qty=quantity, filled_qty=filled_qty,
            reference_price=reference_price, fill_price=fill_price, value=value, costs=costs,
        )
        self._fills.append(fill)
        return fill

    # ── signed position accounting ────────────────────────────────────────────
    def _apply_to_position(
        self, symbol: str, side: Direction, qty: int, fill_price: float, statutory_cost: float
    ) -> None:
        pos = self.position(symbol)
        signed = qty if side == Direction.BUY else -qty

        if pos.quantity == 0 or (pos.quantity > 0) == (signed > 0):
            # Opening or increasing in the same direction → weighted average price.
            new_qty = pos.quantity + signed
            total_abs = abs(pos.quantity) + abs(signed)
            pos.avg_price = (
                (pos.avg_price * abs(pos.quantity) + fill_price * abs(signed)) / total_abs
                if total_abs
                else 0.0
            )
            pos.quantity = new_qty
        else:
            # Reducing / closing / flipping → realise P&L on the closed portion.
            closing = min(abs(signed), abs(pos.quantity))
            if pos.quantity > 0:  # closing a long via a sell
                pos.realized_pnl += (fill_price - pos.avg_price) * closing
            else:  # closing a short via a buy
                pos.realized_pnl += (pos.avg_price - fill_price) * closing
            new_qty = pos.quantity + signed
            pos.quantity = new_qty
            if new_qty == 0:
                pos.avg_price = 0.0
            elif (new_qty > 0) != (pos.quantity - signed > 0):
                # Flipped past zero — remaining qty opens a fresh position at fill price.
                pos.avg_price = fill_price

        # Explicit fees reduce realised P&L (slippage is already in fill_price).
        pos.realized_pnl -= statutory_cost
