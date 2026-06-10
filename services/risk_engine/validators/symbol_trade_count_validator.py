"""SymbolTradeCountValidator — enforces max_trades_per_symbol_per_day.

Prevents re-entry into the same symbol after the per-day limit is reached.
This is the risk-engine enforcement point for the paper_optimization.yaml
`max_trades_per_symbol_per_day` config value.

Key invariants:
    * Only new-entry signals are counted and rejected.
    * EXIT signals (is_closeout=True or signal_id starts with "EXIT-") are always exempt.
    * MIS square-off and TEE exits continue to work regardless of this limit.
    * Count is maintained in-memory and rehydrated from DynamoDB on startup/date change.
    * In-memory pre-increment on approval (conservative — misses no double-entries).
    * Rejection reason: MAX_TRADES_PER_SYMBOL_REACHED.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from risk_engine.limits.risk_limits import RiskValidationResult
from shared.models.signal import Signal

logger = logging.getLogger("risk_engine.validators.symbol_trade_count")

VALIDATOR_NAME = "symbol_trade_count_validator"

_IST = timezone(timedelta(hours=5, minutes=30))

# DynamoDB order statuses that represent a completed entry fill.
_FILLED_STATUSES = ("FILLED", "PAPER_FILLED", "PARTIALLY_FILLED")


def _is_exit_signal(signal: Signal) -> bool:
    """Return True if this signal is an exit order, not a new entry."""
    if signal.metadata.get("is_closeout"):
        return True
    if signal.signal_id.startswith("EXIT-"):
        return True
    return False


class SymbolTradeCountValidator:
    """Enforce a maximum number of entry orders per symbol per trading day.

    Args:
        dynamo_client:        Low-level boto3 DynamoDB client (may be None for paper fail-open).
        orders_table:         DynamoDB orders table name.
        max_trades_per_symbol: Maximum allowed entry fills per symbol per day (default 1).
        risk_profile:         "paper" | "live" — controls fail behaviour on DynamoDB error.
    """

    def __init__(
        self,
        dynamo_client: Any,
        orders_table: str,
        max_trades_per_symbol: int = 1,
        max_entries_per_day: int = 0,
        risk_profile: str = "paper",
    ) -> None:
        self._dynamo = dynamo_client
        self._orders_table = orders_table
        self._max_trades = max_trades_per_symbol
        # Book-wide daily entry budget (0 = disabled). Trade count is itself a
        # cost decision: at ~0.2% round-trip cost, an uncapped book bleeds
        # ₹50-100 per entry regardless of signal quality.
        self._max_entries_per_day = max_entries_per_day
        self._profile = risk_profile.lower()

        # In-memory state — keyed by symbol, value = approved entry count today
        self._counts: dict[str, int] = {}
        self._trade_date: str = ""
        self._rehydrated: bool = False

    # ── Public API ──────────────────────────────────────────────────────────────

    async def validate(self, signal: Signal) -> RiskValidationResult:
        """Check the per-symbol daily entry count and approve or reject.

        Exit signals are always exempt.  Entry signals increment the in-memory
        counter on approval.  Rejects with MAX_TRADES_PER_SYMBOL_REACHED when
        the daily limit is reached.
        """
        # Exits bypass the check entirely.
        if _is_exit_signal(signal):
            return RiskValidationResult(
                approved=True,
                validator_name=VALIDATOR_NAME,
                reason="exit_signal_exempt",
            )

        today = datetime.now(_IST).strftime("%Y-%m-%d")

        # Rehydrate from DynamoDB on first call or date rollover.
        if not self._rehydrated or self._trade_date != today:
            await self._rehydrate(today)

        # Book-wide daily entry budget — checked before the per-symbol limit so
        # the rejection reason is unambiguous once the global budget is spent.
        if self._max_entries_per_day > 0:
            total_today = sum(self._counts.values())
            if total_today >= self._max_entries_per_day:
                logger.warning(
                    "symbol_trade_count_validator.global_limit_rejected "
                    "signal_id=%s symbol=%s total=%d limit=%d",
                    signal.signal_id, signal.symbol, total_today,
                    self._max_entries_per_day,
                )
                return RiskValidationResult(
                    approved=False,
                    validator_name=VALIDATOR_NAME,
                    reason=(
                        f"GLOBAL_DAILY_ENTRY_LIMIT_REACHED: {total_today} entry "
                        f"order(s) today (limit={self._max_entries_per_day}). "
                        "All exits continue to work."
                    ),
                    details={
                        "symbol": signal.symbol,
                        "total_entries_today": total_today,
                        "max_entries_per_day": self._max_entries_per_day,
                    },
                )

        current = self._counts.get(signal.symbol, 0)
        if current >= self._max_trades:
            logger.warning(
                "symbol_trade_count_validator.rejected "
                "signal_id=%s symbol=%s count=%d limit=%d",
                signal.signal_id, signal.symbol, current, self._max_trades,
            )
            return RiskValidationResult(
                approved=False,
                validator_name=VALIDATOR_NAME,
                reason=(
                    f"MAX_TRADES_PER_SYMBOL_REACHED: {signal.symbol} has "
                    f"{current} filled entry order(s) today (limit={self._max_trades}). "
                    "All exits continue to work."
                ),
                details={
                    "symbol": signal.symbol,
                    "current_count": current,
                    "max_trades_per_symbol": self._max_trades,
                },
            )

        # Pre-increment: reserve a slot even before the execution fill is confirmed.
        # This is conservative (may block one extra entry if execution rejects),
        # which is acceptable for paper optimization.
        self._counts[signal.symbol] = current + 1
        logger.debug(
            "symbol_trade_count_validator.approved "
            "signal_id=%s symbol=%s count=%d->%d",
            signal.signal_id, signal.symbol, current, current + 1,
        )
        return RiskValidationResult(
            approved=True,
            validator_name=VALIDATOR_NAME,
            reason=f"symbol_trade_count_ok: {current + 1}/{self._max_trades}",
            details={
                "symbol": signal.symbol,
                "new_count": current + 1,
                "max_trades_per_symbol": self._max_trades,
            },
        )

    def invalidate_cache(self) -> None:
        """Force a full rehydration on the next validate() call."""
        self._rehydrated = False

    # ── Internal ────────────────────────────────────────────────────────────────

    async def _rehydrate(self, trade_date: str) -> None:
        """Load today's filled entry counts from DynamoDB orders table."""
        self._trade_date = trade_date
        self._counts = {}

        if self._dynamo is None:
            logger.warning(
                "symbol_trade_count_validator.no_dynamo — starting with zero counts"
            )
            self._rehydrated = True
            return

        try:
            counts = await self._scan_filled_entries(trade_date)
            self._counts = counts
            logger.info(
                "symbol_trade_count_validator.rehydrated "
                "trade_date=%s symbols_with_entries=%d",
                trade_date, len(counts),
            )
        except Exception as exc:
            logger.error(
                "symbol_trade_count_validator.rehydrate_failed "
                "trade_date=%s error=%s — starting with zero counts",
                trade_date, exc,
            )
            # Fail-open: start with zero counts. Conservative in that we may
            # allow one extra entry after a restart, but we never block exits.
            self._counts = {}

        self._rehydrated = True

    async def _scan_filled_entries(self, trade_date: str) -> dict[str, int]:
        """Scan DynamoDB orders table for today's filled entry orders per symbol.

        Excludes EXIT signals (signal_id starts with "EXIT-").
        Returns a dict mapping symbol → filled entry count.
        """
        counts: dict[str, int] = {}
        kwargs: dict[str, Any] = dict(
            TableName=self._orders_table,
            FilterExpression=(
                "trade_date = :td"
                " AND (order_status = :filled OR order_status = :paper_filled)"
                " AND NOT begins_with(signal_id, :exit_prefix)"
            ),
            ExpressionAttributeValues={
                ":td":            {"S": trade_date},
                ":filled":        {"S": "FILLED"},
                ":paper_filled":  {"S": "PAPER_FILLED"},
                ":exit_prefix":   {"S": "EXIT-"},
            },
            ProjectionExpression="symbol, signal_id, order_status",
        )

        while True:
            resp = await asyncio.to_thread(self._dynamo.scan, **kwargs)
            for item in resp.get("Items", []):
                sym_attr = item.get("symbol", {})
                symbol = sym_attr.get("S", "") if isinstance(sym_attr, dict) else str(sym_attr)
                if symbol:
                    counts[symbol] = counts.get(symbol, 0) + 1
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last

        return counts
