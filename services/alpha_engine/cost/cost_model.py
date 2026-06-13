"""CostModel — net-edge economics for alpha forecasts (bps units).

Wraps the single cost truth from the strategy_engine viability gate
(``_viability.ROUND_TRIP_COST_PCT`` = 0.20% = 20 bps round trip) so the Alpha
Engine and the live trading path can never disagree on the NSE cost stack. A
drift-guard unit test asserts ``CostEstimate.total_round_trip_bps`` equals
``ROUND_TRIP_COST_PCT * 100`` — if someone retunes the viability constant, the
test fails loudly rather than letting two cost models silently diverge
(tech-debt register item #4: three cost models exist; unification is cutover-era).

``net_edge_bps = forecast_return_bps - total_round_trip_bps`` (phase9 §3.2).

Live per-symbol spread/slippage from ``latest-prices`` is [PLANNED] — v1 uses the
flat baseline (5 bps half-spread + 5 bps slippage per side ≈ 20 bps round trip).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from shared.models.alpha import AlphaForecast

# Cross-boundary import of the cost constant only (sanctioned by the monorepo
# Dockerfile PYTHONPATH; see ADR-031). Importing the constant — not behavior —
# keeps a single source of truth for the round-trip cost.
from strategy_engine.strategies._viability import ROUND_TRIP_COST_PCT

# Total round-trip cost in basis points (0.20% -> 20 bps).
TOTAL_ROUND_TRIP_BPS: float = ROUND_TRIP_COST_PCT * 100.0

# Edge bands for research stratification (ADR-031 rev 2). Boundaries 20/30/40/50.
EDGE_BANDS: tuple[str, ...] = ("<20", "20-30", "30-40", "40-50", ">=50")


def classify_edge_band(net_edge_bps: float) -> str:
    """Bucket a net edge into a research band. Boundaries are left-closed."""
    if net_edge_bps < 20.0:
        return "<20"
    if net_edge_bps < 30.0:
        return "20-30"
    if net_edge_bps < 40.0:
        return "30-40"
    if net_edge_bps < 50.0:
        return "40-50"
    return ">=50"


@dataclass(frozen=True)
class CostEstimate:
    """Per-side-folded round-trip cost breakdown for one forecast, in bps.

    The three components sum to ``total_round_trip_bps``. In v1 statutory
    fees/taxes are folded into the flat baseline (``fees_taxes_bps = 0``), exactly
    as the viability gate documents; the field exists so a future per-symbol
    statutory model is additive.
    """

    expected_spread_bps: float
    expected_slippage_bps: float
    fees_taxes_bps: float

    @property
    def total_round_trip_bps(self) -> float:
        return self.expected_spread_bps + self.expected_slippage_bps + self.fees_taxes_bps


class CostModel:
    """Computes net edge for forecasts. Stateless and deterministic in v1."""

    def __init__(
        self,
        *,
        total_round_trip_bps: float = TOTAL_ROUND_TRIP_BPS,
        fees_taxes_bps: float = 0.0,
    ) -> None:
        self._total_round_trip_bps = total_round_trip_bps
        self._fees_taxes_bps = fees_taxes_bps
        # Split the non-fee remainder evenly between spread and slippage so the
        # breakdown always sums to the configured round-trip cost.
        self._half = (total_round_trip_bps - fees_taxes_bps) / 2.0

    def estimate(
        self,
        symbol: str,
        market: str,
        price: float,
        notional: float = 0.0,
    ) -> CostEstimate:
        """Return the round-trip cost estimate for a prospective forecast.

        v1 returns the flat baseline for every symbol; the signature already
        accepts the inputs (symbol/market/price/notional) a future live
        spread/impact model will need (per-symbol live spread is [PLANNED]).
        """
        return CostEstimate(
            expected_spread_bps=self._half,
            expected_slippage_bps=self._half,
            fees_taxes_bps=self._fees_taxes_bps,
        )

    def apply(self, forecast: AlphaForecast) -> AlphaForecast:
        """Return a copy of ``forecast`` with cost fields and net edge populated.

        ``forecast_id`` is preserved (it is derived only from identity fields),
        so costing never changes a forecast's identity.
        """
        est = self.estimate(
            symbol=forecast.symbol,
            market=forecast.market,
            price=forecast.decision_price,
        )
        net_edge_bps = forecast.forecast_return_bps - est.total_round_trip_bps
        return replace(
            forecast,
            expected_spread_bps=est.expected_spread_bps,
            expected_slippage_bps=est.expected_slippage_bps,
            fees_taxes_bps=est.fees_taxes_bps,
            net_edge_bps=net_edge_bps,
            edge_band=classify_edge_band(net_edge_bps),
        )
