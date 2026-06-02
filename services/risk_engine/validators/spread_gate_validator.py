"""
SpreadGateValidator — reject signals when the live bid-ask spread is too wide.

Wide spreads mean the cost of entry eats into the expected edge before a
single tick of movement.  A 1% spread on a 1000-share RELIANCE position at
₹2500 costs ₹25,000 just to enter.  No strategy with a typical 0.3–0.5%
target has positive expectancy when the spread is 100+ bps.

Data source:
    ``context.live_spread_bps`` — sourced from ``LiveQuotePoller`` DynamoDB
    output table (PK=QUOTE#{market}#{symbol}/LATEST).  Populated by the
    execution_engine's polling loop every 2–5 seconds (phase-dependent).

Stale-data rule (Zerodha rate capacity alignment — ADR-014 Misalignment 1):
    If spread data is missing or stale (captured_at > 30s ago), the validator
    APPROVES with a ``STALE_SPREAD_DATA`` warning.  Data gaps must NEVER block
    trading.  This rule is non-negotiable.

Threshold config:
    Default ``max_spread_bps=50`` (0.5%).  Configurable per instrument class
    via InstrumentRegistry or RiskLimits override.
"""

from __future__ import annotations

from typing import Optional

from shared.logging.logger import get_logger
from shared.models.risk_context import RiskContext

from risk_engine.limits.risk_limits import RiskValidationResult
from risk_engine.validators.common import risk_data_unavailable_result

logger = get_logger(__name__, service_name="risk_engine")

# Default maximum spread in basis points (0.5%).  Instruments with wider
# typical spreads (mid-caps, illiquid F&O) can have this overridden per-symbol.
_DEFAULT_MAX_SPREAD_BPS: float = 50.0

# Spread age above which we treat data as stale and approve with a warning.
_STALE_SPREAD_AGE_SECONDS: float = 30.0


class SpreadGateValidator:
    """
    Reject signals on instruments with a bid-ask spread wider than threshold.

    Reads ``context.live_spread_bps`` (pre-fetched by RiskContextBuilder) and
    compares against ``max_spread_bps``.  Zero I/O at validation time.

    Stale-data behavior:
        ``context.live_spread_bps is None`` → APPROVE with STALE_SPREAD_DATA.
        This covers: LiveQuotePoller not yet running, MARKET_OPEN phase where
        quote polling is paused, or DynamoDB read failure in RiskContextBuilder.
    """

    VALIDATOR_NAME = "spread_gate_validator"

    def __init__(
        self,
        max_spread_bps: float = _DEFAULT_MAX_SPREAD_BPS,
        per_symbol_overrides: Optional[dict[str, float]] = None,
    ) -> None:
        """
        Args:
            max_spread_bps: Global maximum spread threshold in basis points.
                Signals are rejected when live spread exceeds this value.
            per_symbol_overrides: Optional mapping of symbol → max_spread_bps
                for instruments whose typical spread differs from the global
                default (e.g. {"ZOMATO": 80.0, "NIFTY50": 20.0}).
        """
        self._max_spread_bps = max_spread_bps
        self._overrides = per_symbol_overrides or {}

    def validate(self, context: RiskContext) -> RiskValidationResult:
        """
        Validate a signal against the live bid-ask spread.

        Synchronous — reads only from the pre-fetched RiskContext.  No I/O.

        Args:
            context: Pre-fetched risk context for this signal.

        Returns:
            RiskValidationResult: approved if spread is within threshold or
            data is stale; rejected if spread exceeds threshold.
        """
        symbol = context.signal.symbol
        spread_bps = context.live_spread_bps

        if spread_bps is None:
            logger.debug(
                "spread_gate.stale_data symbol=%s",
                symbol,
            )
            return risk_data_unavailable_result(
                signal=context.signal,
                validator_name=self.VALIDATOR_NAME,
                reason="STALE_SPREAD_DATA — no live quote available; spread gate cannot run",
                details={"spread_bps": -1.0, "max_spread_bps": self._threshold(symbol)},
            )

        threshold = self._threshold(symbol)

        if spread_bps > threshold:
            logger.warning(
                "spread_gate.rejected symbol=%s spread_bps=%.1f max=%.1f",
                symbol,
                spread_bps,
                threshold,
            )
            return RiskValidationResult(
                approved=False,
                validator_name=self.VALIDATOR_NAME,
                reason=(
                    f"Spread {spread_bps:.1f} bps exceeds max {threshold:.1f} bps "
                    f"for {symbol} — entry cost too high"
                ),
                details={
                    "spread_bps": spread_bps,
                    "max_spread_bps": threshold,
                    "excess_bps": round(spread_bps - threshold, 2),
                },
            )

        return RiskValidationResult(
            approved=True,
            validator_name=self.VALIDATOR_NAME,
            reason=f"Spread {spread_bps:.1f} bps within {threshold:.1f} bps threshold",
            details={"spread_bps": spread_bps, "max_spread_bps": threshold},
        )

    def _threshold(self, symbol: str) -> float:
        """Return effective max_spread_bps for symbol (per-symbol override or global)."""
        return self._overrides.get(symbol, self._max_spread_bps)
