"""
Risk Limits — configurable thresholds for risk validation.

Defines the RiskLimits dataclass and per-market limit configurations.
Limits can be loaded from settings or overridden dynamically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RiskLimits:
    """
    Configurable risk thresholds.

    These limits are checked by individual validators before any signal
    is approved for execution.

    Attributes:
        max_position_size_pct: Max single position as % of portfolio value.
        max_total_exposure_pct: Max total exposure (all positions) as % of portfolio.
        max_daily_loss_pct: Max daily loss as % of portfolio — triggers kill switch.
        max_single_order_value: Max value for any single order.
        max_open_orders: Max number of concurrent open orders.
        max_position_per_symbol: Max shares held for a single symbol.
        max_sector_exposure_pct: Max exposure to a single sector as % of portfolio.
        cooldown_after_loss_seconds: Seconds to pause after hitting loss limit.
        portfolio_value: Total portfolio value for percentage calculations.
    """

    max_position_size_pct: float = 5.0
    max_total_exposure_pct: float = 80.0
    max_daily_loss_pct: float = 2.0
    max_single_order_value: float = 500_000.0
    max_open_orders: int = 50
    max_position_per_symbol: int = 10_000
    max_sector_exposure_pct: float = 25.0
    cooldown_after_loss_seconds: int = 300
    portfolio_value: float = 1_000_000.0

    # Per-market overrides (if set, override global limits for that market)
    nse_overrides: Optional[dict[str, float]] = field(default=None)
    us_overrides: Optional[dict[str, float]] = field(default=None)

    def get_limit(self, key: str, market: Optional[str] = None) -> float:
        """
        Get a limit value, applying market-specific overrides if available.

        Args:
            key: Limit attribute name (e.g., "max_position_size_pct").
            market: Optional market for override lookup ("NSE" or "US").

        Returns:
            The effective limit value.
        """
        # Check market overrides first
        if market == "NSE" and self.nse_overrides and key in self.nse_overrides:
            return self.nse_overrides[key]
        if market == "US" and self.us_overrides and key in self.us_overrides:
            return self.us_overrides[key]

        return getattr(self, key, 0.0)


@dataclass
class RiskValidationResult:
    """
    Result of a risk validation check.

    Attributes:
        approved: Whether the signal passed validation.
        validator_name: Name of the validator that produced this result.
        reason: Human-readable reason (especially if rejected).
        details: Additional details for logging/debugging.
    """

    approved: bool
    validator_name: str
    reason: str = ""
    details: dict[str, float] = field(default_factory=dict)
