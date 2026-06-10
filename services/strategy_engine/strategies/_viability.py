"""Universal pre-trade viability gate — shared by all QuantEmbrace strategies.

Extracted from Scalp1mStrategy's hardened viability filter (Session 8
post-mortem), generalized per the Retail Quant Investment Committee Report
(docs/strategy/retail-quant-investment-committee-report-2026-06-10.md,
Standing Rules R1/R2):

    R1 — a signal whose profit target cannot clear the full Indian round-trip
         cost stack must never be emitted.
    R2 — derived floors at the 0.20% round-trip baseline:
         profit target ≥ 2.5 × cost (0.50%), stop distance ≥ 0.40% of entry.

Cost baseline (matches the paper simulator and §0 of the committee report):
    5 bps slippage + 5 bps half-spread per side ≈ 0.20% round trip, which
    also covers Zerodha brokerage + STT + exchange/SEBI/stamp/GST at the
    book's ₹25–50k clip sizes.

Strategies call ``check_signal_viability()`` immediately before constructing
a Signal and must return None (emit nothing) when ``viable`` is False.
This module never checks positions, broker state, or risk limits.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Cost model constants (Standing Rules R1/R2) ───────────────────────────────

ROUND_TRIP_COST_PCT: float = 0.20
"""Total round-trip cost as % of notional: slippage + spread + statutory."""

MIN_TARGET_COST_MULTIPLE: float = 2.5
"""Profit target must be at least this multiple of the round-trip cost."""

MIN_STOP_PCT: float = 0.40
"""Stop distance floor as % of entry price — below this, bid/ask noise
plus exit slippage dominates the stop (cost can exceed 1R)."""


@dataclass(frozen=True)
class ViabilityResult:
    """Outcome of the pre-trade viability check.

    ``viable`` False means the signal must not be emitted. ``reason`` is a
    machine-parseable string (prefix before ':') for log aggregation.
    """

    viable: bool
    reason: str           # "" when viable
    stop_pct: float       # stop distance as % of entry price
    target_pct: float     # take-profit distance as % of entry price
    net_edge_pct: float   # target_pct - round-trip cost

    def to_metadata(self) -> dict:
        """Fields for inclusion in Signal.metadata on emitted signals."""
        return {
            "viability_stop_pct": round(self.stop_pct, 4),
            "viability_target_pct": round(self.target_pct, 4),
            "viability_net_edge_pct": round(self.net_edge_pct, 4),
        }


def check_signal_viability(
    price: float,
    stop_distance: float,
    tp_distance: float,
    min_stop_pct: float = MIN_STOP_PCT,
    cost_pct: float = ROUND_TRIP_COST_PCT,
    min_cost_multiple: float = MIN_TARGET_COST_MULTIPLE,
) -> ViabilityResult:
    """Check whether a prospective entry clears the retail cost floor.

    Args:
        price:          Entry price (must be > 0).
        stop_distance:  |entry - stop| in price units (must be > 0).
        tp_distance:    |take_profit - entry| in price units (must be > 0).
        min_stop_pct:   Stop floor as % of price (default R2: 0.40).
        cost_pct:       Round-trip cost as % of notional (default 0.20).
        min_cost_multiple: Required target/cost multiple (default R2: 2.5).

    Returns:
        ViabilityResult — callers must not emit a Signal when viable=False.
    """
    if price <= 0 or stop_distance <= 0 or tp_distance <= 0:
        return ViabilityResult(
            viable=False,
            reason="invalid_inputs: price/stop/tp must be positive",
            stop_pct=0.0,
            target_pct=0.0,
            net_edge_pct=0.0,
        )

    stop_pct = (stop_distance / price) * 100.0
    target_pct = (tp_distance / price) * 100.0
    net_edge_pct = target_pct - cost_pct
    min_target_pct = min_cost_multiple * cost_pct

    if stop_pct < min_stop_pct:
        return ViabilityResult(
            viable=False,
            reason=(
                f"stop_below_floor: {stop_pct:.3f}% < {min_stop_pct:.2f}% "
                "(noise/cost dominates stop)"
            ),
            stop_pct=stop_pct,
            target_pct=target_pct,
            net_edge_pct=net_edge_pct,
        )

    if target_pct < min_target_pct:
        return ViabilityResult(
            viable=False,
            reason=(
                f"target_below_cost_floor: {target_pct:.3f}% < "
                f"{min_target_pct:.2f}% ({min_cost_multiple}x {cost_pct:.2f}% cost)"
            ),
            stop_pct=stop_pct,
            target_pct=target_pct,
            net_edge_pct=net_edge_pct,
        )

    return ViabilityResult(
        viable=True,
        reason="",
        stop_pct=stop_pct,
        target_pct=target_pct,
        net_edge_pct=net_edge_pct,
    )
