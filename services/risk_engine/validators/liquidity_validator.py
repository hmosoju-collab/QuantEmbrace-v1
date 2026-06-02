"""
LiquidityValidator — reject signals on instruments with insufficient volume.

An order representing > 5% of a symbol's average daily volume (ADV) will
move the price against you on entry.  For a personal trader running 5–30
signals per day the relevant question is not "can I exit?" but "will I
create the move I'm trying to profit from?"

Example:
    RELIANCE ADV: 4,000,000 shares.  1% of ADV = 40,000 shares.
    Any signal for < 40,000 shares is comfortably liquid.

    SMALLCAP_XYZ ADV: 20,000 shares.  Buying 500 shares = 2.5% of ADV.
    Market-impact cost exceeds edge — reject.

ADV data source:
    ``context.adv_20d`` — pre-fetched by RiskContextBuilder from:
    1. Prices table (PRICE#{symbol}/LATEST, field adv_30d / adv_20d)
       Written by data_ingestion service or candle_prefetch.py.
    2. During MARKET_OPEN (IntradayCandleStream paused), RiskContextBuilder
       falls back to the morning candle_prefetch.py run (same table key).

Graceful degradation (non-negotiable):
    If ``context.adv_20d is None``, approve with ``LOW_ADV_DATA`` warning.
    A data absence must never block trading — it means we haven't fetched
    historical candles for this instrument yet, not that it's illiquid.
"""

from __future__ import annotations

from typing import Optional

from shared.logging.logger import get_logger
from shared.models.risk_context import RiskContext

from risk_engine.limits.risk_limits import RiskValidationResult
from risk_engine.validators.common import risk_data_unavailable_result

logger = get_logger(__name__, service_name="risk_engine")

# Reject signals where the order quantity exceeds this % of ADV.
# 1.0% is the conservative default for a personal intraday trader.
_DEFAULT_MAX_ORDER_ADV_PCT: float = 1.0

# Below this absolute ADV level always approve (instrument is liquid by convention
# and we're unlikely to be trading meaningful size relative to it).
# 1,000,000 shares/day = large-cap, no liquidity concern.
_LIQUID_ADV_FLOOR: float = 1_000_000.0


class LiquidityValidator:
    """
    Reject signals on instruments where order size exceeds ADV threshold.

    Reads ``context.adv_20d`` (pre-fetched by RiskContextBuilder) and the
    signal quantity to compute order-as-percentage-of-ADV.  Zero I/O.
    """

    VALIDATOR_NAME = "liquidity_validator"

    def __init__(
        self,
        max_order_adv_pct: float = _DEFAULT_MAX_ORDER_ADV_PCT,
        per_symbol_overrides: Optional[dict[str, float]] = None,
    ) -> None:
        """
        Args:
            max_order_adv_pct: Reject if (signal_qty / adv) * 100 exceeds this.
                Default 1.0% — conservative for intraday strategies.
            per_symbol_overrides: Optional symbol → max_order_adv_pct for
                instruments where a different threshold is appropriate.
        """
        self._max_adv_pct = max_order_adv_pct
        self._overrides = per_symbol_overrides or {}

    def validate(self, context: RiskContext) -> RiskValidationResult:
        """
        Validate signal against ADV-based liquidity threshold.

        Synchronous — reads only from pre-fetched RiskContext.  No I/O.

        Args:
            context: Pre-fetched risk context for this signal.

        Returns:
            RiskValidationResult: approved if order is liquid; rejected if
            order-to-ADV ratio exceeds threshold.  Approves with warning if
            no ADV data is available.
        """
        symbol = context.signal.symbol
        adv = context.adv_20d

        if adv is None or adv <= 0:
            logger.debug(
                "liquidity_validator.no_adv_data symbol=%s",
                symbol,
            )
            return risk_data_unavailable_result(
                signal=context.signal,
                validator_name=self.VALIDATOR_NAME,
                reason="LOW_ADV_DATA — ADV not available; liquidity check cannot run",
                details={"adv_20d": -1.0, "order_qty": context.signal.quantity},
            )

        # Very liquid instruments — skip ratio check to avoid false positives
        if adv >= _LIQUID_ADV_FLOOR:
            return RiskValidationResult(
                approved=True,
                validator_name=self.VALIDATOR_NAME,
                reason=f"Liquid instrument (ADV={adv:,.0f} shares) — liquidity check skipped",
                details={"adv_20d": adv, "order_qty": context.signal.quantity},
            )

        order_qty = float(context.signal.quantity)
        order_adv_pct = (order_qty / adv) * 100.0
        threshold = self._threshold(symbol)

        if order_adv_pct > threshold:
            logger.warning(
                "liquidity_validator.rejected symbol=%s "
                "order_qty=%.0f adv=%.0f order_adv_pct=%.2f%% max=%.2f%%",
                symbol,
                order_qty,
                adv,
                order_adv_pct,
                threshold,
            )
            return RiskValidationResult(
                approved=False,
                validator_name=self.VALIDATOR_NAME,
                reason=(
                    f"Order size {order_qty:.0f} shares is {order_adv_pct:.2f}% of ADV "
                    f"({adv:,.0f} shares/day) — exceeds {threshold:.2f}% max. "
                    f"Reduce size or choose a more liquid instrument."
                ),
                details={
                    "adv_20d": round(adv, 0),
                    "order_qty": order_qty,
                    "order_adv_pct": round(order_adv_pct, 2),
                    "max_order_adv_pct": threshold,
                },
            )

        return RiskValidationResult(
            approved=True,
            validator_name=self.VALIDATOR_NAME,
            reason=(
                f"Order size {order_qty:.0f} ({order_adv_pct:.2f}% of ADV) "
                f"within {threshold:.2f}% threshold"
            ),
            details={
                "adv_20d": round(adv, 0),
                "order_qty": order_qty,
                "order_adv_pct": round(order_adv_pct, 2),
                "max_order_adv_pct": threshold,
            },
        )

    def _threshold(self, symbol: str) -> float:
        """Return effective max_order_adv_pct (per-symbol override or global)."""
        return self._overrides.get(symbol, self._max_adv_pct)
