"""
LtpResolver — shared last-traded-price lookup with freshness metadata.

Priority chain:
    1. prices table  (DynamoDB ``QUOTE#{market}#{symbol}/LATEST``, field ``ltp``)
       → source="prices_table"        when captured_at age <= freshness_seconds
       → source="prices_table_stale"  when item exists but captured_at is too old
    2. position fill price (the ``last_price`` field on the position record, set at
       fill time and never updated after that)
       → source="position_fill"  always is_stale=True

Shared by:
    - MonitoringStatusService  (§5 Open Positions Detail — LTP, P&L, source, age)
    - TradeExitEngine          (stop-loss / take-profit evaluation)

The prices table is written by LiveQuotePoller every 2 seconds.  If the poller is
not running (e.g. paper session outside market hours), the table will either be
absent or contain stale entries.  LtpResolver falls back gracefully and marks the
result so callers can warn the user.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

_FRESHNESS_DEFAULT_SECONDS: float = 5.0


@dataclass
class LtpResult:
    """LTP resolution result with provenance metadata."""

    price: float
    source: str                       # "prices_table" | "prices_table_stale" | "position_fill"
    captured_at: Optional[datetime]   # UTC; None when source == "position_fill"
    age_seconds: Optional[float]      # seconds since captured_at; None when unknown
    is_stale: bool                    # True when age > freshness_seconds or no timestamp


class LtpResolver:
    """
    Shared LTP lookup used by MonitoringStatusService and TradeExitEngine.

    Reads ``QUOTE#{market}#{symbol}/LATEST`` from the prices table (written by
    LiveQuotePoller every 2 s) and checks ``captured_at`` against the configured
    freshness window before trusting the value.  Falls back to the position's
    fill-time ``last_price`` when the table is unavailable or the item is missing.

    Args:
        dynamo_client:      boto3 DynamoDB client.  If None, always returns the
                            position_fill_price fallback.
        prices_table:       Table name.  If None, skips the DynamoDB lookup.
        freshness_seconds:  Maximum age (seconds) before a prices-table entry is
                            considered stale.  Default 5.0 (poller runs every 2 s).
        market:             Exchange prefix used in the DynamoDB PK.  Default "NSE".
    """

    def __init__(
        self,
        dynamo_client: Optional[Any],
        prices_table: Optional[str],
        freshness_seconds: float = _FRESHNESS_DEFAULT_SECONDS,
        market: str = "NSE",
    ) -> None:
        self._dynamo = dynamo_client
        self._prices_table = prices_table
        self._freshness = freshness_seconds
        self._market = market.upper()

    async def resolve(
        self,
        symbol: str,
        position_fill_price: Optional[float] = None,
    ) -> Optional[LtpResult]:
        """
        Resolve the current LTP for *symbol*.

        Returns:
            ``LtpResult`` if any price source is available, else ``None``.
        """
        if self._dynamo is not None and self._prices_table:
            result = await self._read_from_prices_table(symbol)
            if result is not None:
                return result

        if position_fill_price is not None:
            return LtpResult(
                price=position_fill_price,
                source="position_fill",
                captured_at=None,
                age_seconds=None,
                is_stale=True,
            )
        return None

    async def _read_from_prices_table(self, symbol: str) -> Optional[LtpResult]:
        """Read ``QUOTE#{market}#{symbol}/LATEST`` from DynamoDB."""
        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._prices_table,
                Key={
                    "PK": {"S": f"QUOTE#{self._market}#{symbol.upper()}"},
                    "SK": {"S": "LATEST"},
                },
            )
        except Exception:
            return None

        item = (response or {}).get("Item")
        if item is None:
            return None

        # Accept either "ltp" (written by LiveQuotePoller) or "last_price" (legacy)
        ltp_str = (
            item.get("ltp", {}).get("N")
            or item.get("last_price", {}).get("N")
        )
        if not ltp_str:
            return None
        try:
            price = float(ltp_str)
        except ValueError:
            return None

        # Freshness check via the captured_at field written by LiveQuotePoller
        captured_at_str = item.get("captured_at", {}).get("S")
        captured_at: Optional[datetime] = None
        age_seconds: Optional[float] = None

        if captured_at_str:
            try:
                captured_at = datetime.fromisoformat(captured_at_str)
                if captured_at.tzinfo is None:
                    captured_at = captured_at.replace(tzinfo=timezone.utc)
                age_seconds = (datetime.now(timezone.utc) - captured_at).total_seconds()
            except (ValueError, TypeError):
                pass

        is_stale = age_seconds is None or age_seconds > self._freshness
        source = "prices_table_stale" if is_stale else "prices_table"

        return LtpResult(
            price=price,
            source=source,
            captured_at=captured_at,
            age_seconds=age_seconds,
            is_stale=is_stale,
        )
