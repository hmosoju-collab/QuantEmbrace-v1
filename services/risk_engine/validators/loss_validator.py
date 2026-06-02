"""
Daily Loss Validator — tracks realized and unrealized P&L.

Rejects new trades when the portfolio's daily loss exceeds the configured
threshold, protecting capital from cascading losses during adverse market
conditions.

Bug fixed (Day-1 gap):
    The original implementation cached realized P&L on the first non-zero read
    and never refreshed it within the same trading day. A mid-day restart reset
    the cache to zero; new losses after a first cache hit were silently ignored.

    The fix has two parts:
        1. ``rehydrate()`` — called on service startup, forces a fresh DynamoDB
           read so a restart never starts the daily loss counter from zero.
        2. TTL-based cache — expires every _PNL_CACHE_TTL_SECONDS (30s).
           Intra-day losses are reflected within 30s without a DynamoDB read
           on every single signal.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date
from typing import Any, Optional

from botocore.exceptions import ClientError

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.models.signal import Signal
from shared.risk_state import nav_key
from shared.utils.helpers import utc_now

from risk_engine.limits.risk_limits import RiskLimits, RiskValidationResult
from risk_engine.validators.common import risk_data_unavailable_result

logger = get_logger(__name__, service_name="risk_engine")

# Realized P&L cache TTL.  30s balances DynamoDB cost vs. loss-detection latency.
_PNL_CACHE_TTL_SECONDS: float = 30.0


class DailyLossValidator:
    """
    Validates that the portfolio has not breached its daily loss limit.

    Tracks two components:
        - Realized P&L: closed trades for today (from DynamoDB, TTL-cached 30s).
        - Unrealized P&L: mark-to-market on open positions (live DynamoDB read).

    If combined daily loss exceeds ``max_daily_loss_pct`` of portfolio value,
    the signal is rejected and the kill switch is auto-activated.

    Startup contract:
        Call ``await rehydrate()`` before processing any signals so that a
        service restart does not reset the daily loss counter to zero.
    """

    VALIDATOR_NAME = "daily_loss_validator"

    def __init__(
        self,
        limits: RiskLimits,
        dynamo_client: Any = None,
        orders_table: Optional[str] = None,
        positions_table: Optional[str] = None,
        risk_state_table: Optional[str] = None,
        settings: Optional[AppSettings] = None,
    ) -> None:
        self._limits = limits
        self._settings = settings or get_settings()
        self._dynamo = dynamo_client
        self._orders_table = orders_table or self._settings.aws.dynamodb_table_orders
        self._positions_table = positions_table or self._settings.aws.dynamodb_table_positions
        self._risk_state_table = (
            risk_state_table or self._settings.aws.dynamodb_table_risk_state
        )

        # TTL-based realized P&L cache.
        # _pnl_cache_fetched_at uses time.monotonic() so it is restart-safe
        # (no wall-clock dependencies, no DST issues).
        # Initialized to 0.0 / epoch so the first call always fetches from DB.
        self._cached_realized_pnl: float = 0.0
        self._pnl_cache_date: Optional[date] = None
        self._pnl_cache_fetched_at: float = 0.0  # monotonic timestamp of last fetch

        # Per-symbol asyncio locks for _apply_fill_to_daily_symbol_pnl().
        # Prevents concurrent fills for the same symbol from racing on the
        # get → compute → put cost-basis update (each symbol has its own lock
        # so cross-symbol fills remain concurrent). The dict is bounded by
        # universe size (~50-200 symbols), not by order volume.
        self._symbol_locks: dict[str, asyncio.Lock] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    async def rehydrate(self) -> None:
        """
        Force-refresh the realized P&L cache from DynamoDB.

        MUST be called during ``RiskEngineService.start()`` before any signals
        are processed. Without this, a mid-day restart resets the daily loss
        counter to zero, allowing the portfolio to re-breach the loss limit.

        Resets the TTL clock so the next automatic refresh is 30s from now.
        """
        today = utc_now().date()
        logger.info(
            "Rehydrating daily P&L cache from DynamoDB for %s", today.isoformat()
        )
        fresh_pnl = await self._fetch_realized_pnl_from_db(today)
        self._cached_realized_pnl = fresh_pnl
        self._pnl_cache_date = today
        self._pnl_cache_fetched_at = time.monotonic()
        logger.info(
            "Daily P&L cache rehydrated: realized_pnl=%.2f for %s",
            fresh_pnl,
            today.isoformat(),
        )

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

            portfolio_value = self._limits.get_portfolio_value()
            max_loss_pct = self._limits.get_limit("max_daily_loss_pct", market=signal.market)
            max_loss_value = portfolio_value * (max_loss_pct / 100.0)

            daily_loss_pct = abs(min(total_daily_pnl, 0.0)) / portfolio_value * 100.0

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
                        "pnl_cache_age_seconds": time.monotonic() - self._pnl_cache_fetched_at,
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
            return risk_data_unavailable_result(
                signal=signal,
                validator_name=self.VALIDATOR_NAME,
                reason=f"Daily P&L/NAV state unavailable: {exc}",
            )

    async def get_daily_pnl(self) -> float:
        """Return the current total daily P&L (realized + unrealized).

        Used by the kill switch auto-trigger monitor.
        """
        today = utc_now().date()
        realized = await self._get_realized_pnl(today)
        unrealized = await self._get_unrealized_pnl()
        return realized + unrealized

    async def record_fill(
        self,
        *,
        order_id: str,
        symbol: str,
        market: str,
        direction: str,
        quantity: float,
        price: float,
        fill_id: Optional[str] = None,
    ) -> dict[str, float]:
        """
        Idempotently record a fill into daily realized P&L and NAV state.

        The risk engine consumes order fill deltas from ``orders.events``. Each
        event is first reserved by ``fill_id`` so Kafka replay cannot double
        count P&L. A per-symbol intraday cost-basis row tracks realized P&L for
        long and short closes; the aggregate daily row and NAV row are then
        updated with the same canonical fields read by ``validate()`` and the
        NAV refresh loop.
        """
        if self._dynamo is None:
            raise RuntimeError("DynamoDB client unavailable for fill accounting")
        if quantity <= 0 or price <= 0:
            raise ValueError("Fill quantity and price must be positive")

        today = utc_now().date()
        today_str = today.isoformat()
        event_key = fill_id or f"{order_id}:{direction}:{quantity}:{price}"
        now_iso = utc_now().isoformat()

        try:
            await asyncio.to_thread(
                self._dynamo.put_item,
                TableName=self._risk_state_table,
                Item={
                    "PK": {"S": f"FILL#{event_key}"},
                    "SK": {"S": "RISK_PNL"},
                    "order_id": {"S": order_id},
                    "symbol": {"S": symbol},
                    "market": {"S": market},
                    "direction": {"S": direction},
                    "quantity": {"N": str(quantity)},
                    "price": {"N": str(price)},
                    "trade_date": {"S": today_str},
                    "created_at": {"S": now_iso},
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                logger.info(
                    "daily_loss.record_fill_duplicate order_id=%s fill_id=%s",
                    order_id,
                    event_key,
                )
                total_pnl = await self.get_daily_pnl()
                return {"realized_delta": 0.0, "total_daily_pnl": total_pnl}
            raise

        symbol_lock = self._symbol_locks.setdefault(symbol, asyncio.Lock())
        async with symbol_lock:
            realized_delta = await self._apply_fill_to_daily_symbol_pnl(
                today_str=today_str,
                symbol=symbol,
                direction=direction.upper(),
                quantity=float(quantity),
                price=float(price),
                now_iso=now_iso,
            )
        realized_today = await self._update_daily_pnl_and_nav(
            today_str=today_str,
            realized_delta=realized_delta,
            now_iso=now_iso,
        )

        self._cached_realized_pnl = realized_today
        self._pnl_cache_date = today
        self._pnl_cache_fetched_at = time.monotonic()

        unrealized = await self._get_unrealized_pnl()
        total_pnl = realized_today + unrealized
        return {
            "realized_delta": realized_delta,
            "realized_today": realized_today,
            "unrealized_pnl": unrealized,
            "total_daily_pnl": total_pnl,
        }

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _get_realized_pnl(self, today: date) -> float:
        """Return today's realized P&L via a TTL-based DynamoDB cache.

        Cache is refreshed when either:
            (a) The date has rolled over — new trading day, reset to 0.
            (b) The cache is older than _PNL_CACHE_TTL_SECONDS (30s).
        """
        now_mono = time.monotonic()
        date_changed = self._pnl_cache_date != today
        cache_expired = (now_mono - self._pnl_cache_fetched_at) >= _PNL_CACHE_TTL_SECONDS

        if date_changed or cache_expired:
            if date_changed:
                logger.info(
                    "Trading date rolled over (%s → %s) — resetting P&L cache to 0",
                    self._pnl_cache_date,
                    today.isoformat(),
                )
            fresh = await self._fetch_realized_pnl_from_db(today)
            self._cached_realized_pnl = fresh
            self._pnl_cache_date = today
            self._pnl_cache_fetched_at = now_mono

        return self._cached_realized_pnl

    async def _fetch_realized_pnl_from_db(self, today: date) -> float:
        """Query DynamoDB for today's realized P&L.

        Fast path — aggregate row:
            Reads the ``PNL_DAY#<date>`` item written by ``_update_daily_pnl_and_nav``.
            This is an O(1) get_item; always preferred.

        Fallback path — orders table scan:
            Used only on the very first call of a fresh trading day (before any fill
            has written the aggregate row).  Paginates through all FILLED orders for
            today using ``LastEvaluatedKey`` to ensure no fills are missed.

        BLOCKER-002 fix — pagination:
            The previous fallback query was a single ``query()`` call.  DynamoDB
            returns at most 1MB per response.  On a high-fill day a single response
            may not include all orders, silently underestimating realized P&L and
            allowing risk limits to pass incorrectly.

            The fixed fallback follows ``LastEvaluatedKey`` until the result set is
            exhausted, guaranteeing every filled order is counted regardless of
            table size.
        """
        if self._dynamo is None:
            raise RuntimeError("DynamoDB client unavailable for realized P&L read")

        try:
            today_str = today.isoformat()

            # ── Fast path: atomic aggregate row (written by _update_daily_pnl_and_nav) ──
            aggregate = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._risk_state_table,
                Key={
                    "PK": {"S": f"PNL_DAY#{today_str}"},
                    "SK": {"S": "CURRENT"},
                },
                ConsistentRead=True,
            )
            aggregate_item = aggregate.get("Item")
            if aggregate_item:
                return float(
                    aggregate_item.get("realized_pnl", {}).get("N", "0") or "0"
                )

            # ── Fallback path: paginated query over orders table ──────────────────
            # Reached only on the first call of the day before any fill has been
            # recorded by _update_daily_pnl_and_nav.
            total = 0.0
            item_count = 0
            page_count = 0
            last_evaluated_key = None

            while True:
                query_kwargs: dict = {
                    "TableName": self._orders_table,
                    "IndexName": "DateIndex",
                    "KeyConditionExpression": "trade_date = :today",
                    "FilterExpression": "order_status = :filled",
                    "ExpressionAttributeValues": {
                        ":today": {"S": today_str},
                        ":filled": {"S": "FILLED"},
                    },
                }
                if last_evaluated_key is not None:
                    query_kwargs["ExclusiveStartKey"] = last_evaluated_key

                response = await asyncio.to_thread(self._dynamo.query, **query_kwargs)
                page_count += 1
                page_items = response.get("Items", [])
                item_count += len(page_items)

                for item in page_items:
                    pnl = float(item.get("realized_pnl", {}).get("N", "0"))
                    total += pnl

                last_evaluated_key = response.get("LastEvaluatedKey")
                if last_evaluated_key is None:
                    # All pages exhausted
                    break

            if page_count > 1:
                logger.warning(
                    "daily_loss.realized_pnl_fallback_paginated "
                    "pages=%d items=%d total_pnl=%.2f date=%s — "
                    "aggregate row was missing; consider investigating "
                    "_update_daily_pnl_and_nav failures",
                    page_count, item_count, total, today_str,
                )
            else:
                logger.debug(
                    "Fetched realized P&L from DynamoDB fallback: "
                    "%.2f (%d filled orders for %s)",
                    total, item_count, today_str,
                )
            return total

        except Exception:
            logger.exception("Failed to query realized P&L from DynamoDB")
            raise

    async def _get_unrealized_pnl(self) -> float:
        """Calculate unrealized P&L across all open positions from DynamoDB.

        BLOCKER-002 fix — paginated scan:
            The previous implementation used a single ``scan()`` call.  DynamoDB
            returns at most 1MB per response.  For a multi-symbol, multi-strategy
            system with many open positions this silently truncates the position
            list, understating unrealized exposure and allowing risk limits to
            pass when they should not.

            The fix follows ``LastEvaluatedKey`` through every page of the scan
            result until the entire positions table is consumed.

        Performance note:
            A full table scan on every risk validation is expensive.  If the
            positions table grows large, migrate to a GSI keyed on ``trade_date``
            or maintain a live unrealized-P&L aggregate in a dedicated DynamoDB
            item (similar to the ``PNL_DAY#`` aggregate used for realized P&L).
        """
        if self._dynamo is None:
            raise RuntimeError("DynamoDB client unavailable for unrealized P&L read")

        try:
            total = 0.0
            item_count = 0
            page_count = 0
            last_evaluated_key = None

            while True:
                scan_kwargs: dict = {
                    "TableName": self._positions_table,
                    "FilterExpression": "begins_with(PK, :prefix)",
                    "ExpressionAttributeValues": {
                        ":prefix": {"S": "POSITION#"},
                    },
                }
                if last_evaluated_key is not None:
                    scan_kwargs["ExclusiveStartKey"] = last_evaluated_key

                response = await asyncio.to_thread(self._dynamo.scan, **scan_kwargs)
                page_count += 1
                page_items = response.get("Items", [])
                item_count += len(page_items)

                for item in page_items:
                    qty = float(item.get("quantity", {}).get("N", "0"))
                    avg_price = float(item.get("avg_price", {}).get("N", "0"))
                    last_price = float(item.get("last_price", {}).get("N", "0"))
                    total += qty * (last_price - avg_price)

                last_evaluated_key = response.get("LastEvaluatedKey")
                if last_evaluated_key is None:
                    break

            if page_count > 1:
                logger.debug(
                    "daily_loss.unrealized_pnl_paginated pages=%d items=%d total=%.2f",
                    page_count, item_count, total,
                )
            return total

        except Exception:
            logger.exception("Failed to calculate unrealized P&L from DynamoDB")
            raise

    async def _apply_fill_to_daily_symbol_pnl(
        self,
        *,
        today_str: str,
        symbol: str,
        direction: str,
        quantity: float,
        price: float,
        now_iso: str,
    ) -> float:
        pk = f"PNL_SYMBOL#{today_str}#{symbol}"
        key = {"PK": {"S": pk}, "SK": {"S": "CURRENT"}}
        response = await asyncio.to_thread(
            self._dynamo.get_item,
            TableName=self._risk_state_table,
            Key=key,
            ConsistentRead=True,
        )
        item = response.get("Item") or {}

        old_qty = float(item.get("quantity", {}).get("N", "0") or "0")
        old_avg = float(item.get("avg_price", {}).get("N", "0") or "0")
        old_realized = float(item.get("realized_pnl", {}).get("N", "0") or "0")
        realized_delta = 0.0

        if direction == "BUY":
            if old_qty < 0:
                closing_qty = min(quantity, abs(old_qty))
                realized_delta = (old_avg - price) * closing_qty
                new_qty = old_qty + quantity
                new_avg = old_avg if new_qty < 0 else (price if new_qty > 0 else 0.0)
            else:
                new_qty = old_qty + quantity
                new_avg = ((old_qty * old_avg) + (quantity * price)) / new_qty
        elif direction == "SELL":
            if old_qty > 0:
                closing_qty = min(quantity, old_qty)
                realized_delta = (price - old_avg) * closing_qty
                new_qty = old_qty - quantity
                new_avg = old_avg if new_qty > 0 else (price if new_qty < 0 else 0.0)
            else:
                short_abs = abs(old_qty)
                new_qty = old_qty - quantity
                new_avg = ((short_abs * old_avg) + (quantity * price)) / abs(new_qty)
        else:
            raise ValueError(f"Unsupported fill direction {direction!r}")

        new_realized = old_realized + realized_delta
        await asyncio.to_thread(
            self._dynamo.put_item,
            TableName=self._risk_state_table,
            Item={
                "PK": {"S": pk},
                "SK": {"S": "CURRENT"},
                "symbol": {"S": symbol},
                "trade_date": {"S": today_str},
                "quantity": {"N": str(new_qty)},
                "avg_price": {"N": str(new_avg)},
                "realized_pnl": {"N": str(new_realized)},
                "updated_at": {"S": now_iso},
            },
        )
        return realized_delta

    async def _update_daily_pnl_and_nav(
        self,
        *,
        today_str: str,
        realized_delta: float,
        now_iso: str,
    ) -> float:
        """Atomically apply *realized_delta* to today's aggregate P&L row.

        Design — why ``update_item ADD`` instead of get → compute → put:
            The previous implementation performed a read-modify-write without
            any conditional expression.  Two concurrent fills arriving within
            the same millisecond both read the same ``realized_pnl`` value,
            each added their own delta, and one silently overwrote the other.
            The daily loss limit then underestimated realized losses by exactly
            one fill's P&L contribution — potentially allowing trading to
            continue past the loss cap.

            ``update_item`` with the ``ADD`` action is atomic at the DynamoDB
            item level (single-item transactions are serialized by DynamoDB
            internally).  Both fills are always counted.  ``ReturnValues=ALL_NEW``
            returns the true post-update aggregate so the in-process cache is
            set to the correct value immediately.

        Note on per-symbol cost basis (``_apply_fill_to_daily_symbol_pnl``):
            That method uses a get → put pattern.  Concurrent fills for the same
            symbol are serialized by ``self._symbol_locks[symbol]`` (acquired in
            ``record_fill`` before the call), so the race is closed.  Cross-symbol
            fills remain concurrent.  The daily aggregate row is the more critical
            path and is handled separately via atomic DynamoDB ``ADD``.
        """
        # Atomically ADD the delta to the existing realized_pnl.
        # If the item does not yet exist (first fill of the day) DynamoDB
        # initializes the Number attribute to 0 before applying ADD.
        response = await asyncio.to_thread(
            self._dynamo.update_item,
            TableName=self._risk_state_table,
            Key={
                "PK": {"S": f"PNL_DAY#{today_str}"},
                "SK": {"S": "CURRENT"},
            },
            UpdateExpression=(
                "SET trade_date = :td, updated_at = :ua "
                "ADD realized_pnl :delta"
            ),
            ExpressionAttributeValues={
                ":td": {"S": today_str},
                ":ua": {"S": now_iso},
                ":delta": {"N": str(realized_delta)},
            },
            ReturnValues="ALL_NEW",
        )
        # ALL_NEW returns the item after the update — this is the true total.
        realized_today = float(
            response["Attributes"]["realized_pnl"]["N"]
        )

        # NAV item is a derived read-optimisation view (used by KillSwitchMonitor
        # for fast portfolio_value access without querying PNL_DAY).  Written with
        # put_item; under concurrent fills the last writer wins — acceptable because
        # both writers derive from the same atomic realized_today above, so the final
        # NAV is always consistent with the final P&L aggregate.
        opening_nav = float(getattr(self._settings, "portfolio_value", 1_000_000.0))
        portfolio_value = opening_nav + realized_today
        await asyncio.to_thread(
            self._dynamo.put_item,
            TableName=self._risk_state_table,
            Item={
                **nav_key(),
                "portfolio_value": {"N": str(portfolio_value)},
                "opening_nav": {"N": str(opening_nav)},
                "realized_pnl_today": {"N": str(realized_today)},
                "last_fill_at": {"S": now_iso},
                "updated_at": {"S": now_iso},
            },
        )
        self._limits.update_portfolio_value(portfolio_value)
        return realized_today
