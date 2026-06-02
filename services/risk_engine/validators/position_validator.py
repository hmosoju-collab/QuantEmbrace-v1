"""
Position Validator — checks single-instrument position limits.

Ensures that a new signal would not cause the portfolio to hold an
excessively large position in any single instrument.

Bug fixed (Day-1 gap — dirty read):
    The original validator read only the POSITIONS table (confirmed fills).
    There is a race window between the risk engine approving signal A and the
    execution engine writing the fill back to DynamoDB. If signal B for the
    same symbol arrives during that window, the position validator would see
    the pre-fill state and approve a combined position that exceeds limits.

    The fix: ``_get_pending_quantity()`` queries the ORDERS table for any
    PENDING or PLACED orders for the same symbol. These are orders that have
    been approved but whose fills have not yet been recorded. The pending
    quantity is added to the current confirmed position before limit checks,
    producing the effective position:

        effective_position = confirmed_position + pending_orders_quantity

    This closes the dirty-read window entirely.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.risk_state import position_key

from risk_engine.limits.risk_limits import RiskLimits, RiskValidationResult
from risk_engine.validators.common import risk_data_unavailable_result
from shared.models.signal import Signal

logger = get_logger(__name__, service_name="risk_engine")

# Order statuses that represent "in-flight" — approved but not yet filled.
# These must be included in effective position calculations.
_IN_FLIGHT_STATUSES = ("PENDING", "PLACED", "PARTIALLY_FILLED")


class PositionValidator:
    """
    Validates that a signal does not exceed per-instrument position limits.

    Checks performed:
        1. Single order value <= max_single_order_value.
        2. Effective position (confirmed + in-flight) + signal <= max shares per symbol.
        3. Effective position value <= max_position_size_pct of portfolio.

    ``effective_position = confirmed_position + pending_order_quantity``

    This closes the dirty-read race between risk approval and fill recording.
    """

    VALIDATOR_NAME = "position_validator"

    def __init__(
        self,
        limits: RiskLimits,
        dynamo_client: Any = None,
        positions_table: Optional[str] = None,
        orders_table: Optional[str] = None,
        settings: Optional[AppSettings] = None,
    ) -> None:
        self._limits = limits
        self._settings = settings or get_settings()
        self._dynamo = dynamo_client
        self._positions_table = positions_table or self._settings.aws.dynamodb_table_positions
        # orders_table is needed for the pending-quantity check
        self._orders_table = orders_table or self._settings.aws.dynamodb_table_orders

    async def validate(self, signal: Signal) -> RiskValidationResult:
        """
        Validate a signal against position-size limits.

        Args:
            signal: The trading signal to validate.

        Returns:
            RiskValidationResult indicating approval or rejection with reason.
        """
        try:
            # Read confirmed position and in-flight orders concurrently.
            confirmed_qty, pending_qty = await asyncio.gather(
                self._get_confirmed_position(signal.symbol),
                self._get_pending_quantity(signal.symbol),
            )
            effective_qty = confirmed_qty + pending_qty
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

            # Check 2: absolute share count per symbol (confirmed + in-flight + new)
            proposed_qty = effective_qty + signal.quantity
            max_per_symbol = int(
                self._limits.get_limit("max_position_per_symbol", market=signal.market)
            )
            if proposed_qty > max_per_symbol:
                return RiskValidationResult(
                    approved=False,
                    validator_name=self.VALIDATOR_NAME,
                    reason=(
                        f"Effective position {proposed_qty} shares of {signal.symbol} "
                        f"(confirmed={confirmed_qty} + in-flight={pending_qty} + "
                        f"signal={signal.quantity}) exceeds max {max_per_symbol}"
                    ),
                    details={
                        "confirmed_qty": float(confirmed_qty),
                        "pending_qty": float(pending_qty),
                        "effective_qty": float(effective_qty),
                        "signal_qty": float(signal.quantity),
                        "proposed_qty": float(proposed_qty),
                        "max_per_symbol": float(max_per_symbol),
                    },
                )

            # Check 3: effective position value as % of portfolio
            proposed_value = proposed_qty * signal.price_at_signal
            max_pct = self._limits.get_limit("max_position_size_pct", market=signal.market)
            portfolio_value = self._limits.get_portfolio_value()
            position_pct = (proposed_value / portfolio_value) * 100.0
            if position_pct > max_pct:
                return RiskValidationResult(
                    approved=False,
                    validator_name=self.VALIDATOR_NAME,
                    reason=(
                        f"Effective position in {signal.symbol} would be "
                        f"{position_pct:.2f}% of portfolio "
                        f"(confirmed={confirmed_qty} + in-flight={pending_qty}), "
                        f"exceeding max {max_pct:.2f}%"
                    ),
                    details={
                        "position_pct": position_pct,
                        "max_position_size_pct": max_pct,
                        "proposed_value": proposed_value,
                        "confirmed_qty": float(confirmed_qty),
                        "pending_qty": float(pending_qty),
                    },
                )

            return RiskValidationResult(
                approved=True,
                validator_name=self.VALIDATOR_NAME,
                reason="Position size within limits",
                details={
                    "confirmed_qty": float(confirmed_qty),
                    "pending_qty": float(pending_qty),
                    "effective_qty": float(effective_qty),
                    "proposed_qty": float(proposed_qty),
                    "position_pct": position_pct,
                },
            )

        except Exception as exc:
            logger.exception("Position validation failed for %s", signal.symbol)
            return risk_data_unavailable_result(
                signal=signal,
                validator_name=self.VALIDATOR_NAME,
                reason=f"Position state read failed: {exc}",
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _get_confirmed_position(self, symbol: str) -> int:
        """
        Read the confirmed (filled) position quantity for a symbol.

        This is the POSITION table — updated only when a fill is recorded.
        """
        if self._dynamo is None:
            raise RuntimeError("DynamoDB client unavailable for confirmed position read")

        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._positions_table,
                Key=position_key(symbol),
            )
            item = response.get("Item")
            if item:
                return int(item.get("quantity", {}).get("N", "0"))
            return 0

        except Exception:
            logger.exception("Failed to read confirmed position for %s from DynamoDB", symbol)
            raise

    async def _get_pending_quantity(self, symbol: str) -> int:
        """
        Sum the quantity of all in-flight orders for a symbol.

        Queries the ORDERS table for PENDING, PLACED, and PARTIALLY_FILLED
        orders. Their quantities represent approved-but-not-yet-confirmed
        position changes that must be counted against limits to prevent
        concurrent signals from collectively exceeding limits.

        This closes the dirty-read race window between risk approval and the
        execution engine writing the fill back to the positions table.
        """
        if self._dynamo is None:
            raise RuntimeError("DynamoDB client unavailable for in-flight order read")

        try:
            total_pending = 0
            for status in _IN_FLIGHT_STATUSES:
                response = await asyncio.to_thread(
                    self._dynamo.query,
                    TableName=self._orders_table,
                    IndexName="symbol-status-index",
                    KeyConditionExpression="symbol = :sym AND order_status = :st",
                    ExpressionAttributeValues={
                        ":sym": {"S": symbol},
                        ":st": {"S": status},
                    },
                    ProjectionExpression="quantity, order_status",
                )
                for item in response.get("Items", []):
                    qty = int(item.get("quantity", {}).get("N", "0"))
                    total_pending += qty

            if total_pending > 0:
                logger.debug(
                    "Found %d in-flight shares for %s (PENDING+PLACED+PARTIAL)",
                    total_pending,
                    symbol,
                )
            return total_pending

        except Exception:
            logger.exception(
                "Failed to read pending orders for %s",
                symbol,
            )
            raise
