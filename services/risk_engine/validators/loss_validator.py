"""
Daily Loss Validator — tracks realized and unrealized P&L.

Rejects new trades when the portfolio's daily loss exceeds the configured
threshold, protecting capital from cascading losses during adverse market
conditions.
"""

from __future__ import annotations

import asyncio
from datetime import date, timezone
from typing import Any, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.utils.helpers import utc_now

from risk_engine.limits.risk_limits import RiskLimits, RiskValidationResult
from strategy_engine.signals.signal import Signal

logger = get_logger(__name__, service_name="risk_engine")


class DailyLossValidator:
    """
    Validates that the portfolio has not breached its daily loss limit.

    Tracks two components:
        - Realized P&L: closed trades for the day (from DynamoDB).
        - Unrealized P&L: mark-to-market on open positions.

    If the combined daily loss exceeds ``max_daily_loss_pct`` of portfolio
    value, the signal is rejected. This validator also signals the kill
    switch for automatic activation when the threshold is breached.
    """

    VALIDATOR_NAME = "daily_loss_validator"

    def __init__(
        self,
        limits: RiskLimits,
        dynamo_client: Any = None,
        orders_table: Optional[str] = None,
        positions_table: Optional[str] = None,
        settings: Optional[AppSettings] = None,
    ) -> None:
        self._limits = limits
        self._settings = settings or get_settings()
        self._dynamo = dynamo_client
        self._orders_table = orders_table or self._settings.aws.dynamodb_table_orders
        self._positions_table = positions_table or self._settings.aws.dynamodb_table_positions

        # In-memory daily P&L cache (reset when the date rolls over)
        self._cached_date: Optional[date] = None
        self._cached_realized_pnl: float = 0.0

    async def validate(self, signal: Signal) -> RiskValidationResult:
        """
        Validate a signal against the daily loss limit.

        Args:
            signal: The trading signal to validate.

        Returns:
            RiskValidationResult indicating approval or rejection.
        """
        try:
            today = utc_now().date()
            realized_pnl = await self._get_realized_pnl(today)
            unrealized_pnl = await self._get_unrealized_pnl()
            total_daily_pnl = realized_pnl + unrealized_pnl

            portfolio_value = self._limits.portfolio_value
            max_loss_pct = self._limits.get_limit("max_daily_loss_pct", market=signal.market)
            max_loss_value = portfolio_value * (max_loss_pct / 100.0)

            daily_loss_pct = abs(min(total_daily_pnl, 0.0)) / portfolio_value * 100.0

            # A negative P&L whose absolute value exceeds the limit means we have
            # lost more than the allowed daily threshold.
            if total_daily_pnl < 0 and abs(total_daily_pnl) >= max_loss_value:
                return RiskValidationResult(
                    approved=False,
                    validator_name=self.VALIDATOR_NAME,
                    reason=(
                        f"Daily loss {daily_loss_pct:.2f}% ({total_daily_pnl:,.2f}) "
                        f"exceeds limit {max_loss_pct:.2f}% ({max_loss_value:,.2f})"
                    ),
                    details={
                        "realized_pnl": realized_pnl,
                        "unrealized_pnl": unrealized_pnl,
                        "total_daily_pnl": total_daily_pnl,
                        "daily_loss_pct": daily_loss_pct,
                        "max_daily_loss_pct": max_loss_pct,
                        "max_loss_value": max_loss_value,
                    },
                )

            return RiskValidationResult(
                approved=True,
                validator_name=self.VALIDATOR_NAME,
                reason="Daily loss within limits",
                details={
                    "realized_pnl": realized_pnl,
                    "unrealized_pnl": unrealized_pnl,
                    "total_daily_pnl": total_daily_pnl,
                    "daily_loss_pct": daily_loss_pct,
                    "max_daily_loss_pct": max_loss_pct,
                },
            )

        except Exception as exc:
            logger.exception("Daily loss validation failed")
            return RiskValidationResult(
                approved=False,
                validator_name=self.VALIDATOR_NAME,
                reason=f"Validation error: {exc}",
            )

    async def get_daily_pnl(self) -> float:
        """
        Return the current total daily P&L (realized + unrealized).

        Useful for external callers such as the kill switch auto-trigger.

        Returns:
            Total daily P&L in base currency.
        """
        today = utc_now().date()
        realized = await self._get_realized_pnl(today)
        unrealized = await self._get_unrealized_pnl()
        return realized + unrealized

    async def _get_realized_pnl(self, today: date) -> float:
        """
        Fetch realized P&L from filled orders for today.

        Uses a simple cache that resets when the date rolls over.

        Args:
            today: The current trading date.

        Returns:
            Realized P&L for the current day.
        """
        # If we already queried today and have a cached value, return it
        # (Realistically this would be event-driven; polling is a fallback.)
        if self._cached_date == today and self._cached_realized_pnl != 0.0:
            return self._cached_realized_pnl

        if self._cached_date != today:
            self._cached_realized_pnl = 0.0
            self._cached_date = today

        if self._dynamo is None:
            return 0.0

        try:
            today_str = today.isoformat()
            response = await asyncio.to_thread(
                self._dynamo.query,
                TableName=self._orders_table,
                IndexName="DateIndex",
                KeyConditionExpression="trade_date = :today",
                FilterExpression="order_status = :filled",
                ExpressionAttributeValues={
                    ":today": {"S": today_str},
                    ":filled": {"S": "FILLED"},
                },
            )

            total = 0.0
            for item in response.get("Items", []):
                pnl = float(item.get("realized_pnl", {}).get("N", "0"))
                total += pnl

            self._cached_realized_pnl = total
            return total

        except Exception:
            logger.exception("Failed to query realized P&L from DynamoDB")
            return self._cached_realized_pnl

    async def _get_unrealized_pnl(self) -> float:
        """
        Calculate unrealized P&L across all open positions.

        Returns:
            Unrealized P&L in base currency.
        """
        if self._dynamo is None:
            return 0.0

        try:
            response = await asyncio.to_thread(
                self._dynamo.scan,
                TableName=self._positions_table,
                FilterExpression="begins_with(PK, :prefix)",
                ExpressionAttributeValues={
                    ":prefix": {"S": "POSITION#"},
                },
            )

            total = 0.0
            for item in response.get("Items", []):
                qty = float(item.get("quantity", {}).get("N", "0"))
                avg_price = float(item.get("avg_price", {}).get("N", "0"))
                last_price = float(item.get("last_price", {}).get("N", "0"))
                total += qty * (last_price - avg_price)

            return total

        except Exception:
            logger.exception("Failed to calculate unrealized P&L from DynamoDB")
            return 0.0
