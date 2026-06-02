"""
RiskContextBuilder — pre-fetch all validator inputs in one parallel batch.

Consolidates the 8–12 individual DynamoDB reads scattered across validators
(each previously fetching their own data per signal) into a single parallel
batch of 5–6 concurrent reads that all complete in ~5–8ms wall clock time.

Every read uses ``asyncio.gather`` so reads happen concurrently.  Missing or
failed live-risk reads are recorded in ``RiskContext.risk_data_errors``.  Paper
mode may continue with warning defaults, but live approvals fail closed in the
risk service before an order can be published.

DynamoDB read plan (one signal):
    ① positions table      — get_item POSITION#{symbol}/CURRENT → PositionState
    ② orders table         — query   status-index PENDING/PLACED by symbol → pending_qty
    ③ prices table         — get_item PRICE#{symbol}/LATEST     → adv_30d
    ④ prices table         — get_item QUOTE#{market}#{symbol}/LATEST → spread_bps
    ⑤ risk-state table     — get_item NAV#CURRENT/STATE          → portfolio_nav
    ⑥ risk-state table     — get_item ANALYTICS#SNAPSHOT/CURRENT → AnalyticsSnapshot

All 6 reads are launched concurrently via asyncio.gather.
Wall-clock latency ≈ max(individual read latencies) ≈ 5–8ms.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.models.risk_context import AnalyticsSnapshot, PositionState, RiskContext
from shared.models.signal import Signal
from shared.risk_state import nav_key, position_key
from shared.utils.helpers import utc_now

logger = get_logger(__name__, service_name="risk_engine")

# Stale-data thresholds for read ④ (live quote)
_STALE_QUOTE_THRESHOLD_SECONDS = 30.0


class RiskContextBuilder:
    """
    Pre-fetch all risk validator inputs for a single signal.

    All DynamoDB reads run concurrently.  Read failures are converted into
    explicit ``risk_data_errors`` so live orders halt instead of silently using
    flat/zero/fallback state.

    Attributes:
        _dynamo:           Low-level boto3 DynamoDB client (``client()``).
        _positions_table:  Table storing open positions (PK=POSITION#{symbol}).
        _orders_table:     Table storing order state (for pending quantity scan).
        _prices_table:     Table storing latest price + ADV data (PK=PRICE#{symbol}).
        _risk_state_table: Table storing NAV + analytics snapshots.
        _limits:           RiskLimits for NAV fallback value.
    """

    def __init__(
        self,
        dynamo_client: Any,
        limits: Any,                          # RiskLimits — avoid circular import
        positions_table: Optional[str] = None,
        orders_table: Optional[str] = None,
        prices_table: Optional[str] = None,
        risk_state_table: Optional[str] = None,
        settings: Optional[AppSettings] = None,
    ) -> None:
        self._dynamo = dynamo_client
        self._limits = limits
        self._settings = settings or get_settings()
        self._positions_table = (
            positions_table or self._settings.aws.dynamodb_table_positions
        )
        self._orders_table = orders_table or self._settings.aws.dynamodb_table_orders
        self._prices_table = prices_table or self._settings.aws.dynamodb_table_prices
        self._risk_state_table = (
            risk_state_table or self._settings.aws.dynamodb_table_risk_state
        )

    # ── Public API ──────────────────────────────────��─────────────────────────

    async def build(self, signal: Signal) -> RiskContext:
        """
        Build a pre-fetched RiskContext for the given signal.

        All DynamoDB reads run concurrently.  Any individual read failure
        returns a safe default (0.0 / None) so the pipeline continues with
        partial data and the relevant validator degrades gracefully.

        Args:
            signal: The signal about to be validated.

        Returns:
            A fully populated (or gracefully degraded) RiskContext.
        """
        fetched_at = utc_now()
        risk_data_errors: list[str] = []

        (
            position_state,
            pending_quantity,
            adv_20d,
            live_spread_bps,
            portfolio_nav,
            analytics,
            current_exposure,
        ) = await asyncio.gather(
            self._fetch_position_state(signal.symbol, risk_data_errors),
            self._fetch_pending_quantity(signal.symbol, risk_data_errors),
            self._fetch_adv(signal.symbol, risk_data_errors),
            self._fetch_live_spread_bps(signal.market, signal.symbol, risk_data_errors),
            self._fetch_portfolio_nav(risk_data_errors),
            self._fetch_analytics_snapshot(risk_data_errors),
            self._fetch_total_exposure(risk_data_errors),
            return_exceptions=False,
        )

        # Merge the separately-fetched pending_quantity into position_state.
        # PositionState is frozen, so we construct a new one.
        position = PositionState(
            confirmed_quantity=position_state.confirmed_quantity,
            avg_entry_price=position_state.avg_entry_price,
            pending_quantity=pending_quantity,
        )

        context = RiskContext(
            signal=signal,
            position=position,
            current_exposure=current_exposure,
            portfolio_nav=portfolio_nav,
            analytics=analytics,
            live_spread_bps=live_spread_bps,
            adv_20d=adv_20d,
            fetched_at=fetched_at,
            risk_data_errors=tuple(dict.fromkeys(risk_data_errors)),
        )

        logger.debug(
            "risk_context.built signal_id=%s symbol=%s "
            "spread_bps=%s adv_20d=%s nav=%.0f exposure=%.0f errors=%s",
            signal.signal_id,
            signal.symbol,
            live_spread_bps,
            adv_20d,
            portfolio_nav,
            current_exposure,
            context.risk_data_errors,
        )

        return context

    # ── Individual read helpers ─────────────────────────────────��─────────────

    @staticmethod
    def _record_error(errors: Optional[list[str]], code: str) -> None:
        if errors is not None:
            errors.append(code)

    async def _fetch_position_state(
        self,
        symbol: str,
        errors: Optional[list[str]] = None,
    ) -> PositionState:
        """
        Read confirmed position state for a symbol.

        DynamoDB key: PK=POSITION#{symbol}, SK=CURRENT
        Fields read: confirmed_quantity, avg_entry_price
        """
        if self._dynamo is None:
            self._record_error(errors, "position_state:dynamo_unavailable")
            return PositionState()

        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._positions_table,
                Key=position_key(symbol),
                ProjectionExpression="confirmed_quantity, avg_entry_price",
                ConsistentRead=False,
            )
            item = response.get("Item")
            if not item:
                return PositionState()

            return PositionState(
                confirmed_quantity=float(
                    item.get("confirmed_quantity", {}).get("N", "0")
                ),
                avg_entry_price=float(
                    item.get("avg_entry_price", {}).get("N", "0")
                ),
            )
        except Exception:
            self._record_error(errors, "position_state:read_failed")
            logger.warning(
                "risk_context.position_state_read_failed symbol=%s",
                symbol,
            )
            return PositionState()

    async def _fetch_pending_quantity(
        self,
        symbol: str,
        errors: Optional[list[str]] = None,
    ) -> float:
        """
        Sum quantities of PENDING and PLACED orders for a symbol.

        Queries the orders table status-index for PENDING and PLACED rows
        and sums the quantity field.  This prevents a new signal from
        exceeding position limits when an open order for the same symbol
        is already in-flight.

        Returns:
            Total pending/in-flight quantity for the symbol (0.0 if none).
        """
        if self._dynamo is None:
            self._record_error(errors, "pending_quantity:dynamo_unavailable")
            return 0.0

        total_pending = 0.0
        for status in ("PENDING", "PLACED"):
            try:
                response = await asyncio.to_thread(
                    self._dynamo.query,
                    TableName=self._orders_table,
                    IndexName="status-index",
                    KeyConditionExpression="order_status = :s",
                    FilterExpression="symbol = :sym",
                    ExpressionAttributeValues={
                        ":s": {"S": status},
                        ":sym": {"S": symbol},
                    },
                    ProjectionExpression="quantity",
                )
                for item in response.get("Items", []):
                    total_pending += float(item.get("quantity", {}).get("N", "0"))
            except Exception:
                self._record_error(errors, f"pending_quantity:{status.lower()}_read_failed")
                logger.warning(
                    "risk_context.pending_qty_read_failed symbol=%s status=%s",
                    symbol,
                    status,
                )

        return total_pending

    async def _fetch_adv(
        self,
        symbol: str,
        errors: Optional[list[str]] = None,
    ) -> Optional[float]:
        """
        Read 20-day (or 30-day) average daily volume from the prices table.

        DynamoDB key: PK=PRICE#{symbol}, SK=LATEST, field: adv_30d (or adv_20d)
        Written by data_ingestion service from Zerodha historical data.
        Also falls back to candle_prefetch.py data if available (same table).

        Returns:
            ADV in shares, or None if not available.
        """
        if self._dynamo is None:
            self._record_error(errors, "adv:dynamo_unavailable")
            return None

        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._prices_table,
                Key={
                    "PK": {"S": f"PRICE#{symbol}"},
                    "SK": {"S": "LATEST"},
                },
                ProjectionExpression="adv_30d, adv_20d",
                ConsistentRead=False,
            )
            item = response.get("Item")
            if not item:
                self._record_error(errors, "adv:missing")
                return None

            # Prefer adv_20d if available, fall back to adv_30d
            if "adv_20d" in item:
                return float(item["adv_20d"]["N"])
            if "adv_30d" in item:
                return float(item["adv_30d"]["N"])
            self._record_error(errors, "adv:missing")
            return None

        except Exception:
            self._record_error(errors, "adv:read_failed")
            logger.warning(
                "risk_context.adv_read_failed symbol=%s", symbol
            )
            return None

    async def _fetch_live_spread_bps(
        self,
        market: str,
        symbol: str,
        errors: Optional[list[str]] = None,
    ) -> Optional[float]:
        """
        Read latest bid-ask spread in basis points from the quote cache table.

        DynamoDB key: PK=QUOTE#{market}#{symbol}, SK=LATEST
        Written by LiveQuotePoller (execution_engine) after each poll cycle.

        Returns:
            Spread in basis points, or None if no live quote data is available
            or the data is stale (> STALE_QUOTE_THRESHOLD_SECONDS old).
        """
        if self._dynamo is None:
            self._record_error(errors, "live_spread:dynamo_unavailable")
            return None

        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._prices_table,
                Key={
                    "PK": {"S": f"QUOTE#{market}#{symbol}"},
                    "SK": {"S": "LATEST"},
                },
                ProjectionExpression="spread_bps, captured_at",
                ConsistentRead=False,
            )
            item = response.get("Item")
            if not item:
                self._record_error(errors, "live_spread:missing")
                return None

            spread_bps_raw = item.get("spread_bps", {}).get("N")
            if spread_bps_raw is None:
                self._record_error(errors, "live_spread:missing")
                return None

            captured_at_raw = item.get("captured_at", {}).get("S")
            if not captured_at_raw:
                self._record_error(errors, "live_spread:timestamp_missing")
                return None
            try:
                captured_at = datetime.fromisoformat(captured_at_raw)
                if captured_at.tzinfo is None:
                    captured_at = captured_at.replace(tzinfo=timezone.utc)
                age = (utc_now() - captured_at).total_seconds()
                if age > _STALE_QUOTE_THRESHOLD_SECONDS:
                    self._record_error(errors, "live_spread:stale")
                    logger.debug(
                        "risk_context.spread_stale symbol=%s age=%.1fs",
                        symbol,
                        age,
                    )
                    return None
            except ValueError:
                self._record_error(errors, "live_spread:timestamp_invalid")
                return None

            return float(spread_bps_raw)

        except Exception:
            self._record_error(errors, "live_spread:read_failed")
            logger.warning(
                "risk_context.spread_read_failed symbol=%s",
                symbol,
            )
            return None

    async def _fetch_portfolio_nav(
        self,
        errors: Optional[list[str]] = None,
    ) -> float:
        """
        Read current portfolio NAV from the risk-state table.

        Fallback: use self._limits.portfolio_value if DynamoDB is unavailable.

        Returns:
            Current portfolio NAV (₹/$). Never 0.0 — falls back to config value.
        """
        fallback_nav = self._limits.portfolio_value if self._limits else 1_000_000.0

        if self._dynamo is None:
            self._record_error(errors, "portfolio_nav:dynamo_unavailable")
            return fallback_nav

        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._risk_state_table,
                Key=nav_key(),
                ProjectionExpression="portfolio_value",
                ConsistentRead=False,
            )
            item = response.get("Item")
            if item:
                nav = float(item.get("portfolio_value", {}).get("N", "0"))
                if nav > 0:
                    return nav
            self._record_error(errors, "portfolio_nav:missing_or_non_positive")
            return fallback_nav

        except Exception:
            self._record_error(errors, "portfolio_nav:read_failed")
            logger.warning(
                "risk_context.nav_read_failed fallback_nav=%.0f",
                fallback_nav,
            )
            return fallback_nav

    async def _fetch_analytics_snapshot(
        self,
        errors: Optional[list[str]] = None,
    ) -> AnalyticsSnapshot:
        """
        Read the latest analytics snapshot from the risk-state table.

        DynamoDB key: PK=ANALYTICS#SNAPSHOT, SK=CURRENT
        Written by RiskAnalyticsEngine every 30–300s (phase-dependent).

        Returns:
            Latest AnalyticsSnapshot, or an empty snapshot if not yet computed.
        """
        if self._dynamo is None:
            self._record_error(errors, "analytics_snapshot:dynamo_unavailable")
            return AnalyticsSnapshot.empty()

        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._risk_state_table,
                Key={
                    "PK": {"S": "ANALYTICS#SNAPSHOT"},
                    "SK": {"S": "CURRENT"},
                },
                ConsistentRead=False,
            )
            item = response.get("Item")
            if not item:
                self._record_error(errors, "analytics_snapshot:missing")
                return AnalyticsSnapshot.empty()

            # sector_exposures stored as map: sector_key → {"N": "value"}
            sector_raw = item.get("sector_exposures", {})
            # DynamoDB map attribute: {"M": {"SECTOR_NAME": {"N": "12345.0"}, ...}}
            sector_map_raw: dict[str, Any] = {}
            if isinstance(sector_raw, dict):
                inner = sector_raw.get("M", sector_raw)
                for k, v in inner.items():
                    if isinstance(v, dict) and "N" in v:
                        sector_map_raw[k] = float(v["N"])
                    elif isinstance(v, (int, float)):
                        sector_map_raw[k] = float(v)

            var_2pct = float(item.get("portfolio_var_2pct", {}).get("N", "0") or "0")
            pnl_today = float(item.get("portfolio_pnl_today", {}).get("N", "0") or "0")

            computed_at: Optional[datetime] = None
            raw_ts = item.get("computed_at", {}).get("S")
            if raw_ts:
                try:
                    computed_at = datetime.fromisoformat(raw_ts)
                    if computed_at.tzinfo is None:
                        computed_at = computed_at.replace(tzinfo=timezone.utc)
                except ValueError:
                    self._record_error(errors, "analytics_snapshot:timestamp_invalid")
            else:
                self._record_error(errors, "analytics_snapshot:timestamp_missing")

            return AnalyticsSnapshot(
                sector_exposures=sector_map_raw,
                portfolio_var_2pct=var_2pct,
                portfolio_pnl_today=pnl_today,
                computed_at=computed_at,
            )

        except Exception:
            self._record_error(errors, "analytics_snapshot:read_failed")
            logger.warning("risk_context.analytics_read_failed")
            return AnalyticsSnapshot.empty()

    async def _fetch_total_exposure(
        self,
        errors: Optional[list[str]] = None,
    ) -> float:
        """
        Scan positions table for total absolute portfolio exposure.

        Fallback: uses the analytics snapshot's total sector exposure sum
        (will be updated as RiskAnalyticsEngine produces fresh snapshots).

        Returns:
            Total exposure in base currency (₹/$). 0.0 on error.
        """
        if self._dynamo is None:
            self._record_error(errors, "total_exposure:dynamo_unavailable")
            return 0.0

        try:
            total = 0.0
            last_key: Optional[dict[str, Any]] = None

            while True:
                scan_kwargs: dict[str, Any] = {
                    "TableName": self._positions_table,
                    "FilterExpression": "begins_with(PK, :prefix)",
                    "ExpressionAttributeValues": {
                        ":prefix": {"S": "POSITION#"},
                    },
                    "ProjectionExpression": "quantity, last_price",
                }
                if last_key:
                    scan_kwargs["ExclusiveStartKey"] = last_key

                response = await asyncio.to_thread(
                    self._dynamo.scan, **scan_kwargs
                )
                for item in response.get("Items", []):
                    qty = abs(float(item.get("quantity", {}).get("N", "0")))
                    price = float(item.get("last_price", {}).get("N", "0"))
                    total += qty * price

                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break

            return total

        except Exception:
            self._record_error(errors, "total_exposure:scan_failed")
            logger.warning("risk_context.exposure_scan_failed")
            return 0.0
