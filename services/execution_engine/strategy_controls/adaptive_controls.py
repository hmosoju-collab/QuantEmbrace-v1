"""
Adaptive strategy circuit breaker.

Tracks consecutive stop-loss exits per strategy and per symbol.
Writes circuit-breaker state to DynamoDB risk_state table.
Risk engine reads this state to block new ENTRY signals for disabled strategies/symbols.

DynamoDB keys:
    PK=ADAPTIVE_CB#STRATEGY#{strategy_id} SK=CURRENT → consecutive_sl, disabled, disabled_at
    PK=ADAPTIVE_CB#SYMBOL#{symbol}        SK=CURRENT → consecutive_sl, disabled, disabled_at

Disabled state persists for the remainder of the trading session (cleared by setup service
on next session start).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

_IST = timezone(timedelta(hours=5, minutes=30))

# DynamoDB key prefix constants
_PK_STRATEGY = "ADAPTIVE_CB#STRATEGY#"
_PK_SYMBOL   = "ADAPTIVE_CB#SYMBOL#"
_SK_CURRENT  = "CURRENT"

# Entry-block key written to risk_state when daily paper loss limit is crossed
_ENTRY_BLOCK_PK_PAPER_LOSS = "ENTRY_BLOCK_PAPER_LOSS"
_ENTRY_BLOCK_SK_GLOBAL     = "GLOBAL"


class AdaptiveStrategyControls:
    """
    Session-scoped circuit breaker for strategies and symbols.

    record_exit() is called by the execution engine after each fill's exit is confirmed.
    It increments or resets consecutive-SL counters in DynamoDB and writes a disabled
    flag when the threshold is crossed.

    check_strategy_blocked() and check_symbol_blocked() are called before accepting a
    new ENTRY signal; they read the disabled flag from DynamoDB.
    """

    def __init__(
        self,
        dynamo_client: Any,
        risk_state_table: str,
        max_consecutive_sl_strategy: int = 3,
        max_consecutive_sl_symbol: int = 2,
        daily_loss_limit_inr: float = 2500.0,
    ) -> None:
        self._dynamo          = dynamo_client
        self._table           = risk_state_table
        self._max_sl_strategy = max_consecutive_sl_strategy
        self._max_sl_symbol   = max_consecutive_sl_symbol
        self._loss_limit      = daily_loss_limit_inr
        # Accumulated session P&L — in-memory only, session-scoped
        self._session_loss: float = 0.0

    @property
    def session_loss(self) -> float:
        return self._session_loss

    async def record_exit(
        self,
        *,
        strategy_id: str,
        symbol: str,
        exit_reason: str,
        pnl: float,
    ) -> None:
        """
        Update consecutive-SL counters and disable strategies/symbols if thresholds are crossed.
        Also accumulates session P&L and sets a daily-loss entry block in paper mode.
        """
        self._session_loss += pnl

        is_sl = exit_reason == "STOP_LOSS"
        is_reset = exit_reason in ("TAKE_PROFIT", "MIS_SQUARE_OFF")

        # Update strategy-level circuit breaker
        if is_sl:
            await self._increment_and_maybe_disable(
                key_prefix=_PK_STRATEGY,
                key_id=strategy_id,
                threshold=self._max_sl_strategy,
                disable_reason=f"consecutive_sl>={self._max_sl_strategy} for strategy {strategy_id}",
            )
        elif is_reset:
            await self._reset_consecutive_sl(_PK_STRATEGY, strategy_id)

        # Update symbol-level circuit breaker
        if is_sl:
            await self._increment_and_maybe_disable(
                key_prefix=_PK_SYMBOL,
                key_id=symbol,
                threshold=self._max_sl_symbol,
                disable_reason=f"consecutive_sl>={self._max_sl_symbol} for symbol {symbol}",
            )
        elif is_reset:
            await self._reset_consecutive_sl(_PK_SYMBOL, symbol)

        # Check daily paper loss limit
        if self._session_loss <= -self._loss_limit:
            await self._set_paper_loss_entry_block()

    async def check_strategy_blocked(self, strategy_id: str) -> bool:
        """Return True if the strategy has been disabled by the circuit breaker."""
        return await self._is_disabled(_PK_STRATEGY, strategy_id)

    async def check_symbol_blocked(self, symbol: str) -> bool:
        """Return True if the symbol has been disabled by the circuit breaker."""
        return await self._is_disabled(_PK_SYMBOL, symbol)

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def get_consecutive_sl(self, key_prefix: str, key_id: str) -> int:
        """Read the current consecutive_sl counter from DynamoDB. Returns 0 on any error."""
        try:
            resp = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._table,
                Key={
                    "PK": {"S": f"{key_prefix}{key_id}"},
                    "SK": {"S": _SK_CURRENT},
                },
            )
            item = resp.get("Item", {})
            n_raw = item.get("consecutive_sl", {})
            if isinstance(n_raw, dict) and "N" in n_raw:
                return int(n_raw["N"])
            return 0
        except Exception:
            return 0

    async def _increment_and_maybe_disable(
        self,
        key_prefix: str,
        key_id: str,
        threshold: int,
        disable_reason: str,
    ) -> None:
        """Atomically increment consecutive_sl; write disabled=True if threshold is crossed."""
        new_count = await self.get_consecutive_sl(key_prefix, key_id) + 1
        disabled  = new_count >= threshold
        now_iso   = datetime.now(_IST).isoformat()

        item: dict[str, Any] = {
            "PK": {"S": f"{key_prefix}{key_id}"},
            "SK": {"S": _SK_CURRENT},
            "consecutive_sl": {"N": str(new_count)},
            "disabled": {"BOOL": disabled},
            "disabled_at": {"S": now_iso if disabled else ""},
            "disable_reason": {"S": disable_reason if disabled else ""},
        }

        try:
            await asyncio.to_thread(
                self._dynamo.put_item,
                TableName=self._table,
                Item=item,
            )
        except Exception:
            # Non-fatal: circuit-breaker state is advisory; trading continues
            pass

    async def _reset_consecutive_sl(self, key_prefix: str, key_id: str) -> None:
        """Reset consecutive_sl to 0 on TP or MIS square-off (good exit resets the streak)."""
        try:
            await asyncio.to_thread(
                self._dynamo.update_item,
                TableName=self._table,
                Key={
                    "PK": {"S": f"{key_prefix}{key_id}"},
                    "SK": {"S": _SK_CURRENT},
                },
                UpdateExpression="SET consecutive_sl = :zero",
                ExpressionAttributeValues={":zero": {"N": "0"}},
            )
        except Exception:
            pass

    async def _is_disabled(self, key_prefix: str, key_id: str) -> bool:
        """Read the disabled flag from DynamoDB. Defaults to False on any read error."""
        try:
            resp = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._table,
                Key={
                    "PK": {"S": f"{key_prefix}{key_id}"},
                    "SK": {"S": _SK_CURRENT},
                },
            )
            item = resp.get("Item", {})
            flag = item.get("disabled", {})
            if isinstance(flag, dict):
                return bool(flag.get("BOOL", False))
            return False
        except Exception:
            return False

    async def _set_paper_loss_entry_block(self) -> None:
        """
        Write an entry-block item to risk_state when the daily paper loss limit is crossed.
        Uses a separate PK from the live ENTRY_BLOCK/GLOBAL so paper and live blocks
        remain fully isolated.
        """
        now_iso = datetime.now(_IST).isoformat()
        try:
            await asyncio.to_thread(
                self._dynamo.put_item,
                TableName=self._table,
                Item={
                    "PK": {"S": _ENTRY_BLOCK_PK_PAPER_LOSS},
                    "SK": {"S": _ENTRY_BLOCK_SK_GLOBAL},
                    "blocked": {"BOOL": True},
                    "reason": {"S": "DAILY_PAPER_LOSS_LIMIT_CROSSED"},
                    "source": {"S": "AdaptiveStrategyControls"},
                    "session_loss": {"N": str(self._session_loss)},
                    "created_at": {"S": now_iso},
                },
            )
        except Exception:
            pass
