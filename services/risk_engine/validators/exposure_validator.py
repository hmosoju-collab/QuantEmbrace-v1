"""
Exposure Validator — checks total portfolio exposure.

Ensures that the aggregate value of all open positions does not exceed
the configured maximum exposure percentage, preventing the portfolio
from becoming over-leveraged.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger

from risk_engine.limits.risk_limits import RiskLimits, RiskValidationResult
from strategy_engine.signals.signal import Signal

logger = get_logger(__name__, service_name="risk_engine")


class ExposureValidator:
    """
    Validates that a new trade would not breach total portfolio exposure limits.

    Total exposure is calculated as the sum of absolute market values of all
    open positions. Both long and short positions contribute to exposure.

    If adding the new signal's notional value would push total exposure above
    ``max_total_exposure_pct`` of portfolio value, the signal is rejected.
    """

    VALIDATOR_NAME = "exposure_validator"

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
        Validate a signal against total portfolio exposure limits.

        Args:
            signal: The trading signal to validate.

        Returns:
            RiskValidationResult indicating approval or rejection with reason.
        """
        try:
            current_exposure = await self._get_total_exposure()
            signal_value = abs(signal.quantity * signal.price_at_signal)
            proposed_exposure = current_exposure + signal_value

            max_exposure_pct = self._limits.get_limit(
                "max_total_exposure_pct", market=signal.market
            )
            portfolio_value = self._limits.portfolio_value
            max_exposure_value = portfolio_value * (max_exposure_pct / 100.0)

            exposure_pct = (proposed_exposure / portfolio_value) * 100.0

            if proposed_exposure > max_exposure_value:
                return RiskValidationResult(
                    approved=False,
                    validator_name=self.VALIDATOR_NAME,
                    reason=(
                        f"Proposed total exposure {exposure_pct:.2f}% "
                        f"({proposed_exposure:,.2f}) exceeds max "
                        f"{max_exposure_pct:.2f}% ({max_exposure_value:,.2f})"
                    ),
                    details={
                        "current_exposure": current_exposure,
                        "signal_value": signal_value,
                        "proposed_exposure": proposed_exposure,
                        "max_exposure_pct": max_exposure_pct,
                        "max_exposure_value": max_exposure_value,
                    },
                )

            return RiskValidationResult(
                approved=True,
                validator_name=self.VALIDATOR_NAME,
                reason="Total exposure within limits",
                details={
                    "current_exposure": current_exposure,
                    "proposed_exposure": proposed_exposure,
                    "exposure_pct": exposure_pct,
                    "max_exposure_pct": max_exposure_pct,
                },
            )

        except Exception as exc:
            logger.exception("Exposure validation failed")
            return RiskValidationResult(
                approved=False,
                validator_name=self.VALIDATOR_NAME,
                reason=f"Validation error: {exc}",
            )

    async def _get_total_exposure(self) -> float:
        """
        Calculate the total absolute exposure across all open positions.

        Scans the positions table and sums abs(quantity * last_price) for
        every open position.

        Returns:
            Total exposure value in base currency.
        """
        if self._dynamo is None:
            logger.debug("No DynamoDB client — assuming zero total exposure")
            return 0.0

        try:
            total = 0.0
            last_evaluated_key: Optional[dict[str, Any]] = None

            while True:
                scan_kwargs: dict[str, Any] = {
                    "TableName": self._positions_table,
                    "FilterExpression": "begins_with(PK, :prefix)",
                    "ExpressionAttributeValues": {
                        ":prefix": {"S": "POSITION#"},
                    },
                }
                if last_evaluated_key:
                    scan_kwargs["ExclusiveStartKey"] = last_evaluated_key

                response = await asyncio.to_thread(
                    self._dynamo.scan, **scan_kwargs
                )

                for item in response.get("Items", []):
                    qty = abs(float(item.get("quantity", {}).get("N", "0")))
                    price = float(item.get("last_price", {}).get("N", "0"))
                    total += qty * price

                last_evaluated_key = response.get("LastEvaluatedKey")
                if not last_evaluated_key:
                    break

            return total

        except Exception:
            logger.exception("Failed to calculate total exposure from DynamoDB")
            return 0.0
