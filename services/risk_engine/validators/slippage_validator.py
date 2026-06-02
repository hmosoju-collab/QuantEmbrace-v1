"""
Slippage Validator — rejects signals where expected market impact exceeds tolerance.

Problem:
    Without a slippage check, the system places MARKET orders with no awareness of:
      1. The bid/ask spread at signal time (direct cost per trade).
      2. The order size relative to available liquidity (market impact).

    A momentum strategy capturing 0.3% expected alpha that pays 0.4% in spread +
    impact costs every trade is a systematic money-loser, regardless of signal quality.

What this validator checks:
    1. Spread check:
       bid/ask spread at signal time as % of mid-price.
       If spread > max_spread_pct → reject (cost is too high relative to expected edge).

    2. ADV check (Average Daily Volume):
       order size as % of average daily volume.
       If order_qty / ADV > max_adv_pct → reject (order would move the book).
       ADV is read from DynamoDB ``latest-prices`` table where the data ingestion
       service writes it alongside each tick.

    3. Impact estimate:
       Combines spread + estimated market impact into a total cost estimate.
       If total_cost_pct > max_total_cost_pct → reject.

Configuration (all via AppSettings.risk or environment variables):
    RISK_MAX_SPREAD_PCT            default 0.20  (20 bps)
    RISK_MAX_ADV_PCT               default 0.50  (0.5% of ADV per order)
    RISK_MAX_TOTAL_COST_PCT        default 0.30  (30 bps combined)
    RISK_SLIPPAGE_WARN_ONLY        default False (True = log warning but approve)

Failure behaviour:
    If bid/ask are missing (zero) or ADV data is absent from DynamoDB, the
    validator returns ``risk_data_unavailable_result()``: paper signals are
    approved with a warning; live signals are rejected (fail-closed).  This
    prevents live orders when cost cannot be assessed.  Set
    ``RISK_SLIPPAGE_WARN_ONLY=true`` to soften rejections for paper signals
    only — live signals are always hard-rejected regardless of that flag.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger

from risk_engine.limits.risk_limits import RiskLimits, RiskValidationResult
from risk_engine.validators.common import is_paper_signal, risk_data_unavailable_result
from shared.models.signal import Signal

logger = get_logger(__name__, service_name="risk_engine")

# ── Defaults ──────────────────────────────────────────────────────────────────
_DEFAULT_MAX_SPREAD_PCT:      float = float(os.environ.get("RISK_MAX_SPREAD_PCT",      "0.20"))
_DEFAULT_MAX_ADV_PCT:         float = float(os.environ.get("RISK_MAX_ADV_PCT",         "0.50"))
_DEFAULT_MAX_TOTAL_COST_PCT:  float = float(os.environ.get("RISK_MAX_TOTAL_COST_PCT",  "0.30"))
_DEFAULT_WARN_ONLY:           bool  = os.environ.get("RISK_SLIPPAGE_WARN_ONLY", "").lower() == "true"

# Approximate linear market impact model: k * sqrt(order_qty / ADV) * volatility
# This is a simplified Almgren-Chriss-style estimate. k is a liquidity constant.
_IMPACT_CONSTANT: float = 0.1


@dataclass
class _SlippageEstimate:
    """Components of the estimated cost for a single order."""
    spread_cost_pct:  float   # Half-spread = (ask - bid) / 2 / mid * 100
    impact_cost_pct:  float   # Estimated linear market impact
    total_cost_pct:   float   # spread_cost + impact_cost
    adv_available:    bool    # Whether ADV data was available for the check
    adv:              float   # ADV used (0 if unavailable)
    order_adv_pct:    float   # order_qty / ADV * 100 (0 if ADV unavailable)


class SlippageValidator:
    """
    Risk validator: estimates and limits expected trading costs per signal.

    Checks bid/ask spread width and order-size-to-ADV ratio before approving
    a signal for execution. Signals with costs exceeding thresholds are
    rejected to prevent trading against a structural cost disadvantage.
    """

    VALIDATOR_NAME = "slippage_validator"

    def __init__(
        self,
        limits: RiskLimits,
        dynamo_client: Any = None,
        prices_table: Optional[str] = None,
        max_spread_pct: float = _DEFAULT_MAX_SPREAD_PCT,
        max_adv_pct: float = _DEFAULT_MAX_ADV_PCT,
        max_total_cost_pct: float = _DEFAULT_MAX_TOTAL_COST_PCT,
        warn_only: bool = _DEFAULT_WARN_ONLY,
        settings: Optional[AppSettings] = None,
    ) -> None:
        """
        Args:
            limits: Risk limits (unused directly, but passed for consistency).
            dynamo_client: Low-level boto3 DynamoDB client.
            prices_table: DynamoDB latest-prices table (contains ADV data).
            max_spread_pct: Max acceptable half-spread as % of mid (default 0.20%).
            max_adv_pct: Max order size as % of ADV (default 0.50%).
            max_total_cost_pct: Max combined spread + impact cost (default 0.30%).
            warn_only: If True, log rejections as warnings but approve anyway.
                       Use during calibration to understand cost distribution
                       without blocking trades.
            settings: Application settings.
        """
        self._limits = limits
        self._settings = settings or get_settings()
        self._dynamo = dynamo_client
        self._prices_table = prices_table or self._settings.aws.dynamodb_table_prices
        self._max_spread_pct = max_spread_pct
        self._max_adv_pct = max_adv_pct
        self._max_total_cost_pct = max_total_cost_pct
        self._warn_only = warn_only

    async def validate(self, signal: Signal) -> RiskValidationResult:
        """
        Validate that the expected trading cost for this signal is within limits.

        Args:
            signal: The trading signal to validate.

        Returns:
            RiskValidationResult with approval/rejection and cost breakdown.
        """
        try:
            bid = getattr(signal, "bid", 0.0) or 0.0
            ask = getattr(signal, "ask", 0.0) or 0.0
            price = signal.price_at_signal

            if bid <= 0 or ask <= 0:
                return risk_data_unavailable_result(
                    signal=signal,
                    validator_name=self.VALIDATOR_NAME,
                    reason="Bid/ask missing; slippage spread check cannot run",
                    details={"bid": bid, "ask": ask},
                )

            estimate = await self._estimate_cost(
                symbol=signal.symbol,
                quantity=signal.quantity,
                bid=bid,
                ask=ask,
                price=price,
            )

            # Build detail dict for logging regardless of outcome
            details = {
                "symbol":           signal.symbol,
                "bid":              bid,
                "ask":              ask,
                "spread_cost_pct":  round(estimate.spread_cost_pct, 4),
                "impact_cost_pct":  round(estimate.impact_cost_pct, 4),
                "total_cost_pct":   round(estimate.total_cost_pct, 4),
                "max_spread_pct":   self._max_spread_pct,
                "max_adv_pct":      self._max_adv_pct,
                "max_total_cost_pct": self._max_total_cost_pct,
                "adv_available":    estimate.adv_available,
                "adv":              round(estimate.adv, 0),
                "order_adv_pct":    round(estimate.order_adv_pct, 4),
                "warn_only":        self._warn_only,
            }

            if not estimate.adv_available:
                return risk_data_unavailable_result(
                    signal=signal,
                    validator_name=self.VALIDATOR_NAME,
                    reason="ADV missing; slippage impact check cannot run",
                    details=details,
                )

            # ── Check 1: spread ───────────────────────────────────────────────
            if estimate.spread_cost_pct > self._max_spread_pct:
                reason = (
                    f"Spread too wide for {signal.symbol}: "
                    f"half-spread={estimate.spread_cost_pct:.3f}% > "
                    f"max={self._max_spread_pct:.3f}%"
                )
                return self._result(
                    signal=signal, approved=False, reason=reason, details=details
                )

            # ── Check 2: ADV (market impact) ──────────────────────────────────
            if estimate.adv_available and estimate.order_adv_pct > self._max_adv_pct:
                reason = (
                    f"Order too large relative to ADV for {signal.symbol}: "
                    f"{estimate.order_adv_pct:.3f}% of ADV > max={self._max_adv_pct:.3f}%"
                )
                return self._result(
                    signal=signal, approved=False, reason=reason, details=details
                )

            # ── Check 3: combined cost ────────────────────────────────────────
            if estimate.total_cost_pct > self._max_total_cost_pct:
                reason = (
                    f"Total estimated cost too high for {signal.symbol}: "
                    f"{estimate.total_cost_pct:.3f}% "
                    f"(spread={estimate.spread_cost_pct:.3f}% + "
                    f"impact={estimate.impact_cost_pct:.3f}%) > "
                    f"max={self._max_total_cost_pct:.3f}%"
                )
                return self._result(
                    signal=signal, approved=False, reason=reason, details=details
                )

            return RiskValidationResult(
                approved=True,
                validator_name=self.VALIDATOR_NAME,
                reason=(
                    f"Estimated cost {estimate.total_cost_pct:.3f}% "
                    f"within limit {self._max_total_cost_pct:.3f}%"
                ),
                details=details,
            )

        except Exception as exc:
            logger.exception("Slippage validation error for %s", signal.symbol)
            return risk_data_unavailable_result(
                signal=signal,
                validator_name=self.VALIDATOR_NAME,
                reason=f"Slippage validation error: {exc}",
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _result(
        self,
        signal: Signal,
        approved: bool,
        reason: str,
        details: dict,
    ) -> RiskValidationResult:
        """
        Construct a RiskValidationResult, honouring warn_only mode.

        In warn_only=True, all checks produce approved=True but the reason
        and details carry the rejection text for operators to review.
        """
        if not approved and self._warn_only and is_paper_signal(signal):
            logger.warning(
                "slippage_validator.warn_only_rejection: %s "
                "(signal approved because warn_only=True)",
                reason,
            )
            return RiskValidationResult(
                approved=True,
                validator_name=self.VALIDATOR_NAME,
                reason=f"[WARN_ONLY] {reason}",
                details=details,
            )
        return RiskValidationResult(
            approved=approved,
            validator_name=self.VALIDATOR_NAME,
            reason=reason,
            details=details,
        )

    async def _estimate_cost(
        self,
        symbol: str,
        quantity: float,
        bid: float,
        ask: float,
        price: float,
    ) -> _SlippageEstimate:
        """
        Estimate trading costs for this order.

        Args:
            symbol:   Trading symbol.
            quantity: Number of shares in the order.
            bid:      Best bid price at signal time.
            ask:      Best ask price at signal time.
            price:    Signal price (mid-point reference).

        Returns:
            SlippageEstimate with spread, impact, and total cost breakdown.
        """
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else price
        if mid <= 0:
            mid = price

        # Half-spread as % of mid price = the cost of crossing the spread
        spread_cost_pct = ((ask - bid) / 2.0 / mid) * 100.0 if mid > 0 else 0.0

        # Try to read ADV from DynamoDB latest-prices table
        adv = await self._get_adv(symbol)
        adv_available = adv > 0
        order_adv_pct = (quantity / adv * 100.0) if adv_available else 0.0

        # Linear market impact estimate: k * (order_qty / ADV)
        # Simplified model — sufficient for Phase 1. Phase 2+ can use
        # Almgren-Chriss with volatility input from the ML layer.
        impact_cost_pct = 0.0
        if adv_available and adv > 0:
            impact_cost_pct = _IMPACT_CONSTANT * (quantity / adv) * 100.0

        total_cost_pct = spread_cost_pct + impact_cost_pct

        return _SlippageEstimate(
            spread_cost_pct=spread_cost_pct,
            impact_cost_pct=impact_cost_pct,
            total_cost_pct=total_cost_pct,
            adv_available=adv_available,
            adv=adv,
            order_adv_pct=order_adv_pct,
        )

    async def _get_adv(self, symbol: str) -> float:
        """
        Read the Average Daily Volume for a symbol from DynamoDB.

        The data ingestion service writes ADV alongside each tick update
        (30-day rolling ADV, updated daily from the broker REST API).

        Returns 0.0 if ADV data is not available.
        """
        if self._dynamo is None:
            raise RuntimeError("DynamoDB client unavailable for ADV read")

        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._prices_table,
                Key={
                    "PK": {"S": f"PRICE#{symbol}"},
                    "SK": {"S": "LATEST"},
                },
                ProjectionExpression="adv_30d",
                ConsistentRead=False,
            )
            item = response.get("Item")
            if item and "adv_30d" in item:
                return float(item["adv_30d"]["N"])
        except Exception:
            logger.warning(
                "Failed to read ADV for %s",
                symbol,
                exc_info=True,
            )
            raise
        return 0.0
