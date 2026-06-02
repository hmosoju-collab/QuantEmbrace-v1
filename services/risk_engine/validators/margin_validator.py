"""
Margin Validator — ensures sufficient broker margin before approving a signal.

Design:
    The validator reads margin state from DynamoDB (written by the execution
    engine's background margin-refresh loop every 5 seconds). It does NOT call
    broker APIs directly.

    Why indirect reads instead of direct broker calls:
        Zerodha rate limit = 10 req/s. At NSE market open, 20+ concurrent signals
        each triggering a live margin API call would exceed this limit, causing
        spurious "insufficient margin" rejections for trades that are actually
        valid. The execution engine serialises broker calls; the risk engine only
        reads a cached snapshot.

    In-memory cache (5s TTL):
        On top of DynamoDB reads, the validator maintains a short in-memory cache
        so a burst of concurrent signals within the same second doesn't produce
        multiple DynamoDB reads.

    Failure behaviour:
        If DynamoDB is unreachable, the validator uses the last known cached
        value. If no cache exists (first call after startup before the execution
        engine has written a margin snapshot), the validator returns
        ``risk_data_unavailable_result()``: paper signals are approved with a
        warning; live signals are rejected (fail-closed).  This prevents placing
        live orders when margin state is completely unknown.

    Buffer:
        We require ``available_cash >= order_value * (1 + MARGIN_BUFFER_PCT)``.
        The buffer (default 20%) ensures we always leave headroom for stop-loss
        orders and slippage without exhausting margin entirely.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger

from risk_engine.limits.risk_limits import RiskLimits, RiskValidationResult
from risk_engine.validators.common import risk_data_unavailable_result
from shared.models.signal import Signal

logger = get_logger(__name__, service_name="risk_engine")

# How long to keep a DynamoDB margin snapshot in the in-process cache (seconds).
_CACHE_TTL_SECONDS: float = 5.0

# Fraction of order value that must remain available as buffer after the trade.
# e.g. 0.20 = keep 20% of margin free for stop-loss slippage.
_DEFAULT_MARGIN_BUFFER_PCT: float = 0.20


@dataclass
class _MarginSnapshot:
    """Cached margin state for one market."""
    available_cash: float
    used_margin: float
    collateral_value: float
    refreshed_at: str          # ISO-8601 string from DynamoDB
    fetched_at_mono: float     # monotonic clock at fetch time (for TTL)


class MarginValidator:
    """
    Risk validator: ensures the signal's order value does not exceed
    available broker margin (with a safety buffer).

    Reads margin from DynamoDB ``risk-state`` table (maintained by the
    execution engine) with a 5-second in-memory TTL. Never calls broker
    APIs directly.
    """

    VALIDATOR_NAME = "margin_validator"

    def __init__(
        self,
        limits: RiskLimits,
        dynamo_client: Any = None,
        risk_state_table: Optional[str] = None,
        margin_buffer_pct: float = _DEFAULT_MARGIN_BUFFER_PCT,
        settings: Optional[AppSettings] = None,
    ) -> None:
        """
        Args:
            limits: Risk limits (used for portfolio value context).
            dynamo_client: Low-level boto3 DynamoDB client.
            risk_state_table: DynamoDB table name for risk state.
            margin_buffer_pct: Fraction of order value to keep as buffer.
            settings: Application settings (fallback config source).
        """
        self._limits = limits
        self._settings = settings or get_settings()
        self._dynamo = dynamo_client
        self._risk_state_table = (
            risk_state_table or self._settings.aws.dynamodb_table_risk_state
        )
        self._margin_buffer_pct = margin_buffer_pct

        # In-process margin snapshot cache keyed by market ("NSE" | "US")
        self._cache: dict[str, _MarginSnapshot] = {}

    async def validate(self, signal: Signal) -> RiskValidationResult:
        """
        Validate that there is sufficient margin for the signal's order value.

        Args:
            signal: The trading signal to validate.

        Returns:
            RiskValidationResult indicating approval or rejection with reason.
        """
        try:
            market = signal.market.upper() if signal.market else "UNKNOWN"
            order_value = signal.quantity * signal.price_at_signal

            snapshot = await self._get_margin_snapshot(market)

            if snapshot is None:
                logger.warning(
                    "No margin snapshot available for %s — rejecting live signal %s",
                    market,
                    signal.signal_id,
                )
                return risk_data_unavailable_result(
                    signal=signal,
                    validator_name=self.VALIDATOR_NAME,
                    reason="Margin data unavailable; margin check cannot run",
                    details={"market": market, "margin_data_available": False},
                )

            # Total available capital = cash + collateral
            total_available = snapshot.available_cash + snapshot.collateral_value
            # Required = order value + buffer reserve
            required = order_value * (1 + self._margin_buffer_pct)

            if total_available < required:
                return RiskValidationResult(
                    approved=False,
                    validator_name=self.VALIDATOR_NAME,
                    reason=(
                        f"Insufficient margin for {signal.symbol}: "
                        f"available={total_available:,.2f} "
                        f"required={required:,.2f} "
                        f"(order={order_value:,.2f} + "
                        f"{self._margin_buffer_pct*100:.0f}% buffer), "
                        f"market={market}"
                    ),
                    details={
                        "market": market,
                        "available_cash": snapshot.available_cash,
                        "collateral_value": snapshot.collateral_value,
                        "total_available": total_available,
                        "used_margin": snapshot.used_margin,
                        "order_value": order_value,
                        "required_with_buffer": required,
                        "margin_buffer_pct": self._margin_buffer_pct,
                        "snapshot_age_seconds": round(
                            time.monotonic() - snapshot.fetched_at_mono, 2
                        ),
                    },
                )

            return RiskValidationResult(
                approved=True,
                validator_name=self.VALIDATOR_NAME,
                reason="Sufficient margin available",
                details={
                    "market": market,
                    "available_cash": snapshot.available_cash,
                    "collateral_value": snapshot.collateral_value,
                    "total_available": total_available,
                    "order_value": order_value,
                    "remaining_after_order": total_available - order_value,
                    "snapshot_age_seconds": round(
                        time.monotonic() - snapshot.fetched_at_mono, 2
                    ),
                },
            )

        except Exception as exc:
            logger.exception("Margin validation failed for %s", signal.symbol)
            return risk_data_unavailable_result(
                signal=signal,
                validator_name=self.VALIDATOR_NAME,
                reason=f"Margin validation error: {exc}",
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _get_margin_snapshot(self, market: str) -> Optional[_MarginSnapshot]:
        """
        Return a margin snapshot for the given market.

        Serves from the in-process cache if fresh (< TTL). Otherwise reads
        from DynamoDB and refreshes the cache.

        Args:
            market: "NSE" or "US".

        Returns:
            MarginSnapshot if data is available, None otherwise.
        """
        now = time.monotonic()
        cached = self._cache.get(market)

        if cached is not None and (now - cached.fetched_at_mono) < _CACHE_TTL_SECONDS:
            return cached

        # Cache miss or TTL expired — read from DynamoDB
        snapshot = await self._fetch_from_dynamodb(market)
        if snapshot is not None:
            self._cache[market] = snapshot
            return snapshot

        return None

    async def _fetch_from_dynamodb(self, market: str) -> Optional[_MarginSnapshot]:
        """
        Read margin snapshot from DynamoDB ``risk-state`` table.

        Item written by execution_engine._margin_refresh_loop() every 5s:
            PK = "MARGIN#{market}"
            SK = "CURRENT"
            available_cash, used_margin, collateral_value, refreshed_at, TTL

        Args:
            market: "NSE" or "US".

        Returns:
            MarginSnapshot if the item exists, None on miss or error.
        """
        if self._dynamo is None:
            raise RuntimeError("DynamoDB client unavailable for margin read")

        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._risk_state_table,
                Key={
                    "PK": {"S": f"MARGIN#{market}"},
                    "SK": {"S": "CURRENT"},
                },
                ConsistentRead=False,  # eventually consistent is fine — 5s refresh
            )
            item = response.get("Item")
            if not item:
                logger.warning(
                    "No margin snapshot in DynamoDB for market=%s. "
                    "Execution engine margin-refresh loop may not be running.",
                    market,
                )
                return None

            return _MarginSnapshot(
                available_cash=float(item.get("available_cash", {}).get("N", "0")),
                used_margin=float(item.get("used_margin", {}).get("N", "0")),
                collateral_value=float(item.get("collateral_value", {}).get("N", "0")),
                refreshed_at=item.get("refreshed_at", {}).get("S", ""),
                fetched_at_mono=time.monotonic(),
            )

        except Exception:
            logger.exception(
                "Failed to read margin snapshot from DynamoDB for market=%s", market
            )
            raise
