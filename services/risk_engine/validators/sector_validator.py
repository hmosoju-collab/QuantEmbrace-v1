"""
SectorConcentrationValidator — enforce sector exposure cap.

Prevents the portfolio from becoming excessively concentrated in a single
sector (e.g. loading up on FINANCIAL_SERVICES stocks during a banking rally).

The ``max_sector_exposure_pct`` limit has existed in ``RiskLimits`` since Phase 1
but was never enforced because validators had no access to sector data.
Phase 4 wires sector information via ``RiskContext.analytics.sector_exposures``
(populated by ``RiskAnalyticsEngine``) so this validator now has what it needs.

Instrument sector assignment:
    Read from ``InstrumentRegistry`` (instruments.yaml).  If the registry
    does not have an entry for the signal's symbol, live orders are rejected
    because sector exposure cannot be bounded.

Live-data rule:
    If the symbol is not in the InstrumentRegistry (sector = ``UNKNOWN``), the
    validator returns ``risk_data_unavailable_result()``: paper signals approve
    with a warning; live signals are rejected (fail-closed).  Note: a known
    symbol with empty analytics still passes — the check is computed against
    zero existing sector exposure, which is correct for the first signal of a
    session.  Only missing sector assignment blocks live signals.
"""

from __future__ import annotations

from typing import Optional

from shared.logging.logger import get_logger
from shared.models.risk_context import RiskContext

from risk_engine.limits.risk_limits import RiskLimits, RiskValidationResult
from risk_engine.validators.common import risk_data_unavailable_result

logger = get_logger(__name__, service_name="risk_engine")


class SectorConcentrationValidator:
    """
    Reject signals that would push a sector above ``max_sector_exposure_pct``.

    Reads sector exposures from ``context.analytics.sector_exposures`` and the
    signal symbol's sector from the InstrumentRegistry.  Zero I/O.
    """

    VALIDATOR_NAME = "sector_concentration_validator"

    def __init__(
        self,
        limits: RiskLimits,
        instrument_registry: Optional[Any] = None,
    ) -> None:
        """
        Args:
            limits: RiskLimits for ``max_sector_exposure_pct`` and
                ``get_portfolio_value()``.
            instrument_registry: Optional ``InstrumentRegistry`` for sector lookup.
                If None, all symbols default to "UNKNOWN" sector (graceful degrade).
        """
        self._limits = limits
        self._registry = instrument_registry

    def validate(self, context: RiskContext) -> RiskValidationResult:
        """
        Validate a signal against sector concentration limits.

        Synchronous — reads only from RiskContext.  No I/O.

        Args:
            context: Pre-fetched risk context for this signal.

        Returns:
            RiskValidationResult: approved if sector exposure stays within limit;
            rejected if adding this signal's notional exceeds the sector cap.
        """
        symbol = context.signal.symbol

        # Look up the sector for this instrument
        sector = self._get_sector(symbol)

        if sector == "UNKNOWN":
            logger.debug(
                "sector_validator.unknown_sector symbol=%s",
                symbol,
            )
            return risk_data_unavailable_result(
                signal=context.signal,
                validator_name=self.VALIDATOR_NAME,
                reason=f"UNKNOWN_SECTOR — {symbol} not in instrument registry; sector check skipped",
                details={"sector": "UNKNOWN", "symbol": symbol},
            )

        max_pct = self._limits.get_limit("max_sector_exposure_pct")
        portfolio_nav = context.portfolio_nav

        if portfolio_nav <= 0:
            return risk_data_unavailable_result(
                signal=context.signal,
                validator_name=self.VALIDATOR_NAME,
                reason="Portfolio NAV is zero — sector check skipped",
                details={"sector": sector, "portfolio_nav": 0.0},
            )

        current_sector_exposure = context.sector_exposure(sector)
        proposed_sector_exposure = context.proposed_sector_exposure(sector)
        proposed_pct = (proposed_sector_exposure / portfolio_nav) * 100.0

        if proposed_pct > max_pct:
            logger.warning(
                "sector_validator.rejected symbol=%s sector=%s "
                "proposed_pct=%.1f max_pct=%.1f",
                symbol,
                sector,
                proposed_pct,
                max_pct,
            )
            return RiskValidationResult(
                approved=False,
                validator_name=self.VALIDATOR_NAME,
                reason=(
                    f"Sector {sector} concentration would reach {proposed_pct:.1f}% "
                    f"(max {max_pct:.1f}%) after adding {symbol}"
                ),
                details={
                    "sector": sector,
                    "current_sector_exposure": round(current_sector_exposure, 2),
                    "proposed_sector_exposure": round(proposed_sector_exposure, 2),
                    "proposed_sector_pct": round(proposed_pct, 2),
                    "max_sector_pct": max_pct,
                    "signal_notional": round(context.signal_notional, 2),
                },
            )

        return RiskValidationResult(
            approved=True,
            validator_name=self.VALIDATOR_NAME,
            reason=(
                f"Sector {sector} concentration {proposed_pct:.1f}% "
                f"within {max_pct:.1f}% limit"
            ),
            details={
                "sector": sector,
                "proposed_sector_pct": round(proposed_pct, 2),
                "max_sector_pct": max_pct,
            },
        )

    def _get_sector(self, symbol: str) -> str:
        """Look up sector from the instrument registry. Returns 'UNKNOWN' if missing."""
        if self._registry is None:
            return "UNKNOWN"
        try:
            config = self._registry.get(symbol)
            if config is not None:
                return config.sector
        except Exception:
            pass
        return "UNKNOWN"


# Avoid forward-reference issues for the Optional[Any] annotation
from typing import Any  # noqa: E402
