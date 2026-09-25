"""NSE statutory cost models — core engine code, not lab code (RA-1 §2.4).

Ported from `strategy_engine.backtesting.backtester.IndianCostModel` (the v1
single source of truth) with per-leg fractions matching `run_factor_study._leg_cost_frac`.
Parity is enforced by test against the v1 class — the numbers must never drift.

Rate sources (verified 2026-06): exchange txn = NSE cash 0.00297% (revised
1 Oct 2024); STT delivery 0.1% both legs; stamp delivery 0.015% buy; SEBI
Rs 10/crore; GST 18% on (brokerage + exchange + SEBI); delivery brokerage 0
at discount brokers. DP charge (~Rs 16/scrip sell) not modelled — documented
minor omission, same as v1.
"""

from dataclasses import dataclass
from typing import Literal

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class EquityDeliveryCosts:
    """NSE equity delivery (CNC) statutory costs + per-leg slippage."""

    stt_buy_pct: float = 0.1
    stt_sell_pct: float = 0.1
    exchange_txn_pct: float = 0.00297
    sebi_turnover_pct: float = 0.0001
    stamp_buy_pct: float = 0.015
    gst_pct: float = 18.0
    slippage_frac: float = 0.0005  # 5 bps per leg, matching the factor study
    version: str = "in-eq-delivery-2024.10+slip5bps"

    def statutory_leg_frac(self, side: Side) -> float:
        """Statutory cost as a fraction of notional for one leg (no slippage)."""
        exch = self.exchange_txn_pct / 100.0
        sebi = self.sebi_turnover_pct / 100.0
        stt = (self.stt_sell_pct if side == "SELL" else self.stt_buy_pct) / 100.0
        stamp = (self.stamp_buy_pct / 100.0) if side == "BUY" else 0.0
        gst = (exch + sebi) * (self.gst_pct / 100.0)  # brokerage = 0 for delivery
        return exch + sebi + stt + stamp + gst

    def leg_cost_frac(self, side: Side) -> float:
        """Total per-leg fraction of notional: statutory + slippage."""
        return self.statutory_leg_frac(side) + self.slippage_frac

    @property
    def round_trip_frac(self) -> float:
        return self.leg_cost_frac("BUY") + self.leg_cost_frac("SELL")


@dataclass(frozen=True)
class USEquityCosts:
    """US equity/ETF cash-account costs (ADR-041 P3). Same interface shape as
    ``EquityDeliveryCosts`` (``leg_cost_frac``/``round_trip_frac``/``version``)
    so ``SimBroker`` and ``execute_rebalance`` use either without change.

    Rate sources (verified 2026-07): SEC Section 31 fee, SELL-side only, FY2025
    rate $27.80 per $1,000,000 of proceeds (re-verify before any live use — the
    SEC revises this periodically); FINRA Trading Activity Fee, SELL-side only,
    $0.000166/share (2024 rate), capped $8.30/trade. TAF is intrinsically
    per-share, but this engine's cost interface is notional-based (matching the
    NSE model) — approximated here as a flat pct-of-notional at a $200
    reference share price (mid-range for SPY/TLT/GLD). This is a documented
    minor approximation, immaterial at ETF price levels (analogous to the NSE
    model's undocumented DP-charge omission). Zero commission, matching the
    $0-budget research assumption (ADR-041).
    """

    sec_fee_pct: float = 0.00278  # % of SELL notional
    taf_pct: float = 0.000083  # % of SELL notional (~$0.000166/share @ $200/share)
    slippage_frac: float = 0.0005  # 5 bps per leg, matching the Phase 2 screen's primary run
    version: str = "us-eq-cash-2025.sec31+taf-slip5bps"

    def statutory_leg_frac(self, side: Side) -> float:
        """Statutory cost as a fraction of notional for one leg (no slippage).
        SEC fee + TAF are SELL-side only; commission is zero both sides."""
        if side == "SELL":
            return (self.sec_fee_pct + self.taf_pct) / 100.0
        return 0.0

    def leg_cost_frac(self, side: Side) -> float:
        """Total per-leg fraction of notional: statutory + slippage."""
        return self.statutory_leg_frac(side) + self.slippage_frac

    @property
    def round_trip_frac(self) -> float:
        return self.leg_cost_frac("BUY") + self.leg_cost_frac("SELL")


MARKET_COSTS: dict[str, type] = {"NSE": EquityDeliveryCosts, "US": USEquityCosts}


def cost_model_for_market(market: str) -> "EquityDeliveryCosts | USEquityCosts":
    """Dispatch the right cost model for a market string (ADR-041 P3). Fail
    closed on an unregistered market rather than silently using NSE costs."""
    try:
        return MARKET_COSTS[market]()
    except KeyError:
        raise ValueError(
            f"no cost model registered for market={market!r} (known: {sorted(MARKET_COSTS)})"
        ) from None
