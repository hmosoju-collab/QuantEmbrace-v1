"""
RiskContext — pre-fetched snapshot of all data the validator pipeline needs.

``RiskContextBuilder.build(signal)`` assembles this in 3–4 DynamoDB reads
(replacing 8–12 scattered reads across individual validators).  Every
validator receives an already-populated ``RiskContext`` and performs zero
additional I/O.

Lifecycle:
    RiskContextBuilder.build(signal)   →  RiskContext  (immutable after creation)
    validate_signal(signal, context)   →  validators read context.*
    context discarded after validation (one context per signal, never reused)

Stale-data contract:
    ``live_spread_bps`` and ``adv_20d`` may be None in paper/shadow mode.
    For live orders, ``RiskContextBuilder`` records missing or failed reads in
    ``risk_data_errors`` and the risk service fails closed before approval.
    ``fetched_at`` lets validators decide whether available data is fresh enough
    to act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from shared.models.signal import Signal
from shared.utils.helpers import utc_now


@dataclass(frozen=True)
class PositionState:
    """
    Consolidated position state for a single symbol.

    Attributes:
        confirmed_quantity: Shares held in confirmed (fully settled) positions.
            Sourced from the ``POSITION#{symbol}`` DynamoDB item.
        pending_quantity: Shares locked in open/pending orders not yet filled.
            Sourced by summing ``PENDING`` and ``PLACED`` order quantities for
            the symbol from the orders table.
        avg_entry_price: Average entry price of confirmed positions (0.0 if flat).
    """

    confirmed_quantity: float = 0.0
    pending_quantity: float = 0.0
    avg_entry_price: float = 0.0

    @property
    def total_committed_quantity(self) -> float:
        """Total shares committed (confirmed + pending) for position limit checks."""
        return self.confirmed_quantity + self.pending_quantity


@dataclass(frozen=True)
class AnalyticsSnapshot:
    """
    Latest output from ``RiskAnalyticsEngine``.

    Written to DynamoDB by the background analytics loop every 30–300 seconds
    depending on market phase.  Read once per signal validation via
    ``RiskContextBuilder``.

    Attributes:
        sector_exposures: Mapping of sector name → absolute market value (₹/$ notional).
            e.g. {"FINANCIAL_SERVICES": 450000.0, "IT": 200000.0}
        portfolio_var_2pct: Simplified 5-day historical VaR at 2% confidence (₹/$).
            0.0 if fewer than 5 days of fill history are available.
        portfolio_pnl_today: Realized + unrealized P&L for today (₹/$).
        computed_at: When this snapshot was computed by the analytics engine.
    """

    sector_exposures: dict[str, float] = field(default_factory=dict)
    portfolio_var_2pct: float = 0.0
    portfolio_pnl_today: float = 0.0
    computed_at: Optional[datetime] = None

    def staleness_seconds(self, now: Optional[datetime] = None) -> float:
        """Return seconds since the snapshot was computed. inf if never computed."""
        if self.computed_at is None:
            return float("inf")
        ref = now or utc_now()
        delta = (ref - self.computed_at).total_seconds()
        return max(0.0, delta)

    @classmethod
    def empty(cls) -> AnalyticsSnapshot:
        """Return an empty snapshot (used when no analytics data is available yet)."""
        return cls()


@dataclass(frozen=True)
class RiskContext:
    """
    Pre-fetched snapshot of all data the validator pipeline needs.

    Built once per signal by ``RiskContextBuilder.build(signal)`` and passed
    to every validator.  Validators are pure functions: they read fields from
    this context and return a ``RiskValidationResult`` — no additional I/O.

    Attributes:
        signal: The trading signal being validated.  Same object passed to
            ``validate_signal()``.
        position: Consolidated position state for the signal's symbol.
        current_exposure: Total absolute portfolio exposure across all symbols
            (sum of |quantity * last_price| for all open positions).
        portfolio_nav: Current portfolio NAV.  Updated by the NAV refresh loop
            and read here for percentage-based limit calculations.
        analytics: Latest output from ``RiskAnalyticsEngine``.  May contain
            empty sector_exposures if analytics have never run.
        live_spread_bps: Live bid-ask spread for the signal's symbol in basis
            points, sourced from the ``LiveQuotePoller`` DynamoDB output table.
            None if no live quote data exists or the quote table is unavailable.
        adv_20d: 20-day average daily volume (in shares) for the signal's
            symbol, sourced from the candle-cache table (interval="day").
            During MARKET_OPEN the candle stream is paused; this field is
            populated from the morning ``candle_prefetch.py`` run instead.
            None if no historical data exists yet.
        fetched_at: UTC timestamp when this context was assembled.  Used by
            validators to assess data staleness (e.g. spread data >30s old).
    """

    signal: Signal
    position: PositionState
    current_exposure: float
    portfolio_nav: float
    analytics: AnalyticsSnapshot

    # Optional fields — paper/shadow validators may warn when None; live orders
    # are rejected by risk_data_errors before approval.
    live_spread_bps: Optional[float]  # from LiveQuotePoller DynamoDB output
    adv_20d: Optional[float]          # from candle-cache (interval="day")

    fetched_at: datetime
    risk_data_errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_risk_data_errors(self) -> bool:
        """True when one or more core risk inputs were unavailable or stale."""
        return bool(self.risk_data_errors)

    @property
    def signal_notional(self) -> float:
        """Notional value of the proposed trade (quantity × signal price)."""
        return abs(self.signal.quantity * self.signal.price_at_signal)

    @property
    def proposed_exposure(self) -> float:
        """Current exposure + notional of this signal (for exposure limit checks)."""
        return self.current_exposure + self.signal_notional

    @property
    def spread_data_age_seconds(self) -> float:
        """Seconds since the live quote data was fetched. inf if not available."""
        if self.live_spread_bps is None:
            return float("inf")
        return self.fetched_at_age_seconds

    @property
    def fetched_at_age_seconds(self) -> float:
        """Seconds elapsed since this context was assembled."""
        return max(0.0, (utc_now() - self.fetched_at).total_seconds())

    def sector_exposure(self, sector: str) -> float:
        """Return current exposure for a sector (0.0 if no positions in sector)."""
        return self.analytics.sector_exposures.get(sector, 0.0)

    def sector_exposure_pct(self, sector: str) -> float:
        """Return sector exposure as % of portfolio NAV (0.0 if nav is zero)."""
        if self.portfolio_nav <= 0:
            return 0.0
        return (self.sector_exposure(sector) / self.portfolio_nav) * 100.0

    def proposed_sector_exposure(self, sector: str) -> float:
        """Sector exposure after adding this signal's notional value."""
        return self.sector_exposure(sector) + self.signal_notional

    def proposed_sector_exposure_pct(self, sector: str) -> float:
        """Proposed sector exposure as % of portfolio NAV."""
        if self.portfolio_nav <= 0:
            return 0.0
        return (self.proposed_sector_exposure(sector) / self.portfolio_nav) * 100.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize for structured logging / audit trail."""
        return {
            "signal_id": self.signal.signal_id,
            "symbol": self.signal.symbol,
            "market": self.signal.market,
            "signal_notional": round(self.signal_notional, 2),
            "confirmed_qty": self.position.confirmed_quantity,
            "pending_qty": self.position.pending_quantity,
            "current_exposure": round(self.current_exposure, 2),
            "proposed_exposure": round(self.proposed_exposure, 2),
            "portfolio_nav": round(self.portfolio_nav, 2),
            "live_spread_bps": self.live_spread_bps,
            "adv_20d": self.adv_20d,
            "risk_data_errors": list(self.risk_data_errors),
            "analytics_sector_count": len(self.analytics.sector_exposures),
            "analytics_var_2pct": round(self.analytics.portfolio_var_2pct, 2),
            "fetched_at": self.fetched_at.isoformat(),
        }
