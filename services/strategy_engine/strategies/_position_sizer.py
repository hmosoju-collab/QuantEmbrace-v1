"""
NAV-based position sizer — shared by all QuantEmbrace candle and tick strategies.

Three-constraint formula
-------------------------
    qty_by_target    = floor(nav × target_pct   / price)          # notional target
    qty_by_hard_cap  = floor(nav × HARD_CAP_PCT / price)          # absolute ceiling
    qty_by_risk_stop = floor(nav × MAX_LOSS_PCT / stop_distance)   # loss-per-trade cap
    final_qty = max(1, min(qty_by_target, qty_by_hard_cap, qty_by_risk_stop))

Conviction scaling
------------------
    confidence >= HIGH_CONVICTION_THRESHOLD → target_pct = 5% (full size)
    confidence <  HIGH_CONVICTION_THRESHOLD → target_pct = 2.5% (default)

The hard cap (5% NAV) is identical to risk_engine position_validator.max_position_pct_nav.
Signals produced here are always within the risk engine's limit, preventing rejections
caused by over-sized positions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# ── Sizing constants ──────────────────────────────────────────────────────────

_TARGET_PCT_DEFAULT   : float = 0.025   # 2.5% of NAV — normal sizing
_TARGET_PCT_HIGH_CONV : float = 0.050   # 5.0% of NAV — high-conviction only
HARD_CAP_PCT          : float = 0.050   # 5.0% NAV — absolute ceiling (matches risk engine)
MAX_LOSS_PCT          : float = 0.0025  # 0.25% NAV — max loss if stop hit

HIGH_CONVICTION_THRESHOLD: float = 0.80  # confidence >= this unlocks full size


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SizingResult:
    """
    Immutable sizing output.

    ``qty`` is the final recommended quantity.  Callers must treat ``qty == 0``
    as a reject (invalid inputs — do not emit a signal).

    ``rejected_if_exceeds_cap`` is a safety assertion.  It should always be
    False after the min() formula is applied.  A True value indicates a logic
    bug and the caller should not emit the signal.
    """

    qty:                    int
    nav_used:               float
    target_notional:        float    # nav × target_pct
    max_allowed_notional:   float    # nav × HARD_CAP_PCT
    sizing_reason:          str      # "risk_stop" | "target_notional" | "hard_cap"
    rejected_if_exceeds_cap: bool    # True if qty × price > max_allowed_notional

    def to_metadata(self) -> dict:
        """Return sizing fields for inclusion in Signal.metadata."""
        return {
            "nav_used":               self.nav_used,
            "target_notional":        round(self.target_notional, 2),
            "max_allowed_notional":   round(self.max_allowed_notional, 2),
            "sizing_reason":          self.sizing_reason,
            "rejected_if_exceeds_cap": self.rejected_if_exceeds_cap,
        }


# ── Public API ────────────────────────────────────────────────────────────────

def size_position(
    price:          float,
    stop_distance:  float,
    nav:            float,
    confidence:     float = 0.0,
    target_pct:     float | None = None,
    hard_cap_pct:   float = HARD_CAP_PCT,
    max_loss_pct:   float = MAX_LOSS_PCT,
) -> SizingResult:
    """
    Compute position quantity from three NAV-based constraints.

    Args:
        price:          Current market price of the instrument.
        stop_distance:  Distance from entry to stop-loss in price units.
                        Must be positive.
        nav:            Current portfolio NAV in the same currency as price.
        confidence:     Strategy confidence score [0, 1].  Scores >= 0.80
                        unlock the full 5% NAV target; lower scores use 2.5%.
        target_pct:     Override the conviction-based target (optional).
        hard_cap_pct:   Hard position size ceiling as fraction of NAV.
        max_loss_pct:   Max allowable loss on one trade as fraction of NAV.

    Returns:
        SizingResult.  When qty == 0, inputs were invalid — do not emit.
    """
    max_allowed = nav * hard_cap_pct

    if price <= 0 or stop_distance <= 0 or nav <= 0:
        return SizingResult(
            qty=0,
            nav_used=nav,
            target_notional=0.0,
            max_allowed_notional=max_allowed,
            sizing_reason="invalid_inputs",
            rejected_if_exceeds_cap=False,
        )

    # Resolve target based on conviction (or explicit override)
    if target_pct is None:
        resolved_target_pct = (
            _TARGET_PCT_HIGH_CONV
            if confidence >= HIGH_CONVICTION_THRESHOLD
            else _TARGET_PCT_DEFAULT
        )
    else:
        resolved_target_pct = target_pct

    target_notional  = nav * resolved_target_pct
    max_loss_amount  = nav * max_loss_pct

    qty_by_target    = math.floor(target_notional   / price)
    qty_by_hard_cap  = math.floor(max_allowed        / price)
    qty_by_risk_stop = math.floor(max_loss_amount    / stop_distance)

    # Binding constraint
    raw = min(qty_by_target, qty_by_hard_cap, qty_by_risk_stop)
    qty = max(1, raw)

    # Determine which constraint drove the final size
    if raw <= 0:
        reason = "risk_stop_floor_one"
    elif raw == qty_by_risk_stop and qty_by_risk_stop < qty_by_target:
        reason = "risk_stop"
    elif raw == qty_by_target and qty_by_target <= qty_by_hard_cap:
        reason = "target_notional"
    else:
        reason = "hard_cap"

    # Safety assertion: actual notional must not exceed cap
    actual_notional = qty * price
    exceeds = actual_notional > max_allowed

    return SizingResult(
        qty=qty,
        nav_used=nav,
        target_notional=round(target_notional, 2),
        max_allowed_notional=round(max_allowed, 2),
        sizing_reason=reason,
        rejected_if_exceeds_cap=exceeds,
    )
