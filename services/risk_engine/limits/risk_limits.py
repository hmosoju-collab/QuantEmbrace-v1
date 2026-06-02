"""
Risk Limits — configurable thresholds for risk validation.

Defines the RiskLimits dataclass and per-market limit configurations.
Limits can be loaded from settings or overridden dynamically.

NAV note:
    ``portfolio_value`` must reflect current NAV, not opening capital.
    Use ``NAVTracker.get_nav()`` to retrieve the live value before
    constructing RiskLimits, or call ``RiskLimits.update_portfolio_value()``
    after receiving fill notifications.

    Stale NAV computes percentage limits against the wrong base:
        After a 10% drawdown, max_position_size_pct=5% should cap at 5% of
        CURRENT capital ($900k), not opening capital ($1M). Using opening NAV
        silently allows 5.5% of real capital — limits become looser as you lose.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# How often to refresh portfolio NAV from DynamoDB (seconds).
_NAV_REFRESH_INTERVAL: float = 30.0


@dataclass
class RiskLimits:
    """
    Configurable risk thresholds.

    These limits are checked by individual validators before any signal
    is approved for execution.

    ``portfolio_value`` is kept live: the ``NAVTracker`` background task
    updates it from DynamoDB every 30 seconds. Validators always call
    ``get_portfolio_value()`` — never read ``portfolio_value`` directly
    so they always pick up the latest refreshed value.

    Attributes:
        max_position_size_pct: Max single position as % of portfolio value.
        max_total_exposure_pct: Max total exposure (all positions) as % of portfolio.
        max_daily_loss_pct: Max daily loss as % of portfolio — triggers kill switch.
        max_single_order_value: Max value for any single order.
        max_open_orders: Max number of concurrent open orders.
        max_position_per_symbol: Max shares held for a single symbol.
        max_sector_exposure_pct: Max exposure to a single sector as % of portfolio.
        cooldown_after_loss_seconds: Seconds to pause after hitting loss limit.
        portfolio_value: Opening / config portfolio value. Updated by NAVTracker.
    """

    max_position_size_pct: float = 5.0
    max_total_exposure_pct: float = 80.0
    max_daily_loss_pct: float = 2.0
    max_single_order_value: float = 500_000.0
    max_open_orders: int = 50
    max_position_per_symbol: int = 10_000
    max_concurrent_positions: int = 1
    max_sector_exposure_pct: float = 25.0
    cooldown_after_loss_seconds: int = 300
    portfolio_value: float = 1_000_000.0
    allow_leverage: bool = False

    # Per-market overrides (if set, override global limits for that market)
    nse_overrides: Optional[dict[str, float]] = field(default=None)
    us_overrides: Optional[dict[str, float]] = field(default=None)

    # Internal: monotonic clock of last NAV update (0 = never refreshed)
    _nav_last_updated: float = field(default=0.0, init=False, repr=False, compare=False)

    @classmethod
    def for_profile(cls, profile: str, *, portfolio_value: float) -> "RiskLimits":
        """Return conservative defaults for a rollout risk profile."""
        normalized = (profile or "tiny-live").strip().lower()
        profiles: dict[str, dict[str, Any]] = {
            "paper": {
                "max_position_size_pct": 5.0,
                "max_total_exposure_pct": 80.0,
                "max_daily_loss_pct": 2.0,
                "max_single_order_value": 500_000.0,
                "max_open_orders": 50,
                "max_position_per_symbol": 10_000,
                "max_concurrent_positions": 10,
                "allow_leverage": False,
            },
            "shadow": {
                "max_position_size_pct": 5.0,
                "max_total_exposure_pct": 50.0,
                "max_daily_loss_pct": 1.0,
                "max_single_order_value": 100_000.0,
                "max_open_orders": 5,
                "max_position_per_symbol": 2_000,
                "max_concurrent_positions": 3,
                "allow_leverage": False,
            },
            "tiny-live": {
                "max_position_size_pct": 5.0,
                "max_total_exposure_pct": 20.0,
                "max_daily_loss_pct": 0.5,
                "max_single_order_value": 5_000.0,
                "max_open_orders": 1,
                "max_position_per_symbol": 100,
                "max_concurrent_positions": 1,
                "allow_leverage": False,
            },
            "medium-live": {
                "max_position_size_pct": 3.0,
                "max_total_exposure_pct": 35.0,
                "max_daily_loss_pct": 0.75,
                "max_single_order_value": 50_000.0,
                "max_open_orders": 3,
                "max_position_per_symbol": 1_000,
                "max_concurrent_positions": 3,
                "allow_leverage": False,
            },
        }
        values = profiles.get(normalized)
        if values is None:
            raise ValueError(
                "Unknown risk profile "
                f"{profile!r}; expected paper, shadow, tiny-live, or medium-live"
            )
        return cls(portfolio_value=portfolio_value, **values)

    def get_portfolio_value(self) -> float:
        """
        Return the current portfolio value.

        Always use this method — not ``self.portfolio_value`` directly.
        This allows the NAVTracker to update portfolio_value in place and
        all validators pick up the new value transparently.
        """
        return self.portfolio_value

    def update_portfolio_value(self, new_value: float) -> None:
        """
        Update portfolio value to reflect current NAV.

        Called by NAVTracker after each DynamoDB refresh.

        Args:
            new_value: Current portfolio NAV.
        """
        if new_value > 0:
            self.portfolio_value = new_value
            self._nav_last_updated = time.monotonic()

    def nav_staleness_seconds(self) -> float:
        """Return seconds since portfolio_value was last refreshed (0 = never)."""
        if self._nav_last_updated == 0.0:
            return float("inf")
        return time.monotonic() - self._nav_last_updated

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
