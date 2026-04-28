"""
Position Validator — checks single-instrument position limits.

Ensures that a new signal would not cause the portfolio to hold an
excessively large position in any single instrument, protecting against
concentration risk.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger

from risk_engine.limits.risk_limits import RiskLimits, RiskValidationResult
from strategy_engine.signals.signal import Signal

logger = get_logger(__name__, service_name="risk_engine")


class PositionValidator:
    """
    Validates that a signal does not exceed per-instrument position limits.

    Checks performed:
        1. Current position + signal quantity <= max shares per symbol.
        2. Position value (at signal price) <= max_position_size_pct of portfolio.
        3. Single order value <= max_single_order_value.

    Current positions are read from DynamoDB (source of truth for live state).
    """

    VALIDATOR_NAME = "position_validator"

    def __init__(
        self,
        limits: RiskLimits,
        dynamo_client: Any = None,
        positions_table: Optional[str] = None,
        settings: Optional[AppSettings] = None,
    ) -> None:
        self._limits = limits
        self._settings = settings or get_settings()
        self._dynamo = dynamo_client
        self._positions_table = positions_table or self._settings.aws.dynamodb_table_positions

    async def validate(self, signal: Signal) -> RiskValidationResult:
        """
        Validate a signal against position-size limits.

        Args:
            signal: The trading signal to validate.

        Returns:
            RiskValidationResult indicating approval or rejection with reason.
        """
        try:
            current_qty = await self._get_current_position(signal.symbol)
            signal_value = signal.quantity * signal.price_at_signal

            # Check 1: single order value cap
            max_order_value = self._limits.get_limit(
                "max_single_order_value", market=signal.market
            )
            if signal_value > max_order_value:
                return RiskValidationResult(
                    approved=False,
                    validator_name=self.VALIDATOR_NAME,
                    reason=(
                        f"Order value {signal_value:,.2f} exceeds max single order "
                        f"value {max_order_value:,.2f}"
                    ),
                    details={
                        "order_value": signal_value,
                        "max_single_order_value": max_order_value,
                    },
                )

            # Check 2: absolute share count per symbol
            proposed_qty = current_qty + signal.quantity
            max_per_symbol = int(
                self._limits.get_limit("max_position_per_symbol", market=signal.market)
            )
            if proposed_qty > max_per_symbol:
                return RiskValidationResult(
                    approved=False,
                    validator_name=self.VALIDATOR_NAME,
                    reason=(
                        f"Proposed position {proposed_qty} shares of {signal.symbol} "
                        f"exceeds max {max_per_symbol}"
                    ),
                    details={
                        "current_qty": float(current_qty),
                        "signal_qty": float(signal.quantity),
                        "max_per_symbol": float(max_per_symbol),
                    },
                )

            # Check 3: position value as % of portfolio
            proposed_value = proposed_qty * signal.price_at_signal
            max_pct = self._limits.get_limit("max_position_size_pct", market=signal.market)
            position_pct = (proposed_value / self._limits.portfolio_value) * 100.0
            if position_pct > max_pct:
                return RiskValidationResult(
                    approved=False,
                    validator_name=self.VALIDATOR_NAME,
                    reason=(
                        f"Position in {signal.symbol} would be {position_pct:.2f}% of "
                        f"portfolio, exceeding max {max_pct:.2f}%"
                    ),
                    details={
                        "position_pct": position_pct,
                        "max_position_size_pct": max_pct,
                        "proposed_value": proposed_value,
                    },
                )

            return RiskValidationResult(
                approved=True,
                validator_name=self.VALIDATOR_NAME,
                reason="Position size within limits",
                details={
                    "current_qty": float(current_qty),
                    "proposed_qty": float(proposed_qty),
                    "position_pct": position_pct,
                },
            )

        except Exception as exc:
            logger.exception("Position validation failed for %s", signal.symbol)
            return RiskValidationResult(
                approved=False,
                validator_name=self.VALIDATOR_NAME,
                reason=f"Validation error: {exc}",
            )

    async def _get_current_position(self, symbol: str) -> int:
        """
        Read the current position quantity for a symbol from DynamoDB.

        Args:
            symbol: The instrument symbol.

        Returns:
            Current quantity held (0 if no position exists).
        """
        if self._dynamo is None:
            logger.debug("No DynamoDB client — assuming zero position for %s", symbol)
            return 0

        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._positions_table,
                Key={
                    "PK": {"S": f"POSITION#{symbol}"},
                    "SK": {"S": "CURRENT"},
                },
            )
            item = response.get("Item")
            if item:
                return int(item.get("quantity", {}).get("N", "0"))
            return 0

        except Exception:
            logger.exception("Failed to read position for %s from DynamoDB", symbol)
            return 0
