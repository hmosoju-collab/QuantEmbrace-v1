"""
Trade Exit Engine (TEE) — monitors open positions and triggers exits.

Runs as a long-lived async task alongside the execution service.

Responsibilities (Phase 3):
    - Poll DynamoDB positions every TEE_POLL_INTERVAL_SECONDS (default 30).
    - For each managed position (direction <> FLAT, stop_price present):
        * Resolve last known price (prices table → position.last_price fallback).
        * Evaluate stop-loss (side-aware). Trigger: STOP_LOSS or TRAILING.
        * Evaluate take-profit (side-aware, skip when trailing is active).
        * Manage trailing stop: activate, advance, and write updated stop_price to DynamoDB.
        * Stop-loss takes priority when both conditions are met in the same cycle.
    - Alert CRITICAL on positions that are OPEN but have no stop_price (unmanaged).
    - Route exit via ExitOrderRouter — NEVER touches the signal pipeline.

Side-aware exit rules:
    LONG:  stop fires when last_price <= stop_price  (price fell to/below stop)
           tp   fires when last_price >= take_profit  (price rose to/above target)
    SHORT: stop fires when last_price >= stop_price  (price rose to/above stop)
           tp   fires when last_price <= take_profit  (price fell to/below target)

Trailing stop rules (Phase 3):
    LONG:  activates when last_price >= entry * (1 + activation_pct/100)
           trail  = last_price * (1 - trail_pct/100)
           stop_price only moves upward (max of current and new trail)
    SHORT: activates when last_price <= entry * (1 - activation_pct/100)
           trail  = last_price * (1 + trail_pct/100)
           stop_price only moves downward (min of current and new trail)

    When trailing is active (exit_state == TRAILING_ACTIVE), TP is suppressed —
    the trailing stop provides a better dynamic exit.

Not in Phase 3 (Phase 4):
    - Breakeven lock trigger
    - Partial profit booking (Phase 3.1)
    - Startup reconciliation (separate service)

Exit priority hierarchy (full system):
    1. Kill switch     — unconditional flatten, not managed here
    2. TEE             — stop-loss / take-profit / trailing (this module)
    3. MIS at 15:05    — time-based close (MISSquareOffManager)
    4. Zerodha 15:15   — broker auto-square (no code path)
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Optional

from services.shared.logging.logger import get_logger
from services.execution_engine.exit.exit_models import (
    ExitOrderRequest,
    ExitTriggerType,
)
from services.execution_engine.exit.exit_order_router import ExitOrderRouter
from services.shared.monitoring import LiveCounters
from services.shared.monitoring.ltp_resolver import LtpResolver

logger = get_logger(__name__, service_name="execution_engine")

_DEFAULT_POLL_INTERVAL: int = int(os.environ.get("TEE_POLL_INTERVAL_SECONDS", "30"))


def _session_date_ist() -> str:
    """Return today's session date string in IST (UTC+05:30)."""
    from datetime import timedelta  # noqa: PLC0415

    ist_offset = timezone(timedelta(seconds=19800))
    return datetime.now(ist_offset).strftime("%Y-%m-%d")


class TradeExitEngine:
    """
    Monitors all managed open positions and fires exit orders on trigger.

    Never touches the Kafka signal pipeline. Never increments signals_today.
    The daily strategy cap cannot block exits.

    Args:
        dynamo_client:           boto3 DynamoDB client.
        positions_table:         DynamoDB positions table name.
        router:                  ExitOrderRouter (paper / live / backtest).
        prices_table:            Optional DynamoDB prices table (QUOTE#NSE#{symbol}/LATEST).
        poll_interval:           Seconds between position scans. Default: 60.
        trailing_enabled:        Whether to manage trailing stops. Default True.
        trailing_activation_pct: Price move % from entry before trailing activates. Default 1.25.
        trailing_stop_pct:       Trailing stop distance % from high watermark. Default 0.6.
    """

    def __init__(
        self,
        dynamo_client: Any,
        positions_table: str,
        router: ExitOrderRouter,
        *,
        prices_table: Optional[str] = None,
        poll_interval: int = _DEFAULT_POLL_INTERVAL,
        trailing_enabled: bool = True,
        trailing_activation_pct: float = 1.25,
        trailing_stop_pct: float = 0.6,
        live_counters: Optional[LiveCounters] = None,
    ) -> None:
        self._dynamo = dynamo_client
        self._positions_table = positions_table
        self._router = router
        self._prices_table = prices_table
        self._poll_interval = poll_interval
        self._running = True
        self._trailing_enabled = trailing_enabled
        self._trailing_activation_pct = trailing_activation_pct
        self._trailing_stop_pct = trailing_stop_pct
        self._live_counters = live_counters
        self._ltp_resolver = LtpResolver(
            dynamo_client=dynamo_client,
            prices_table=prices_table,
            freshness_seconds=float(os.environ.get("TEE_LTP_FRESHNESS_SECONDS", "5.0")),
        )
        # In LIVE mode, block exit evaluation when LTP age exceeds this threshold.
        # Paper mode only warns. Controlled by TEE_MAX_STALE_LTP_LIVE_SECONDS (default 3 s).
        self._max_stale_ltp_live = float(os.environ.get("TEE_MAX_STALE_LTP_LIVE_SECONDS", "3.0"))

    async def run(self) -> None:
        """Main loop — poll and evaluate exit conditions continuously."""
        logger.info(
            "tee.started",
            poll_interval_seconds=self._poll_interval,
            mode=self._router.mode.value,
        )
        if self._live_counters is not None:
            self._live_counters.tee_running = True
            self._live_counters.tee_poll_interval = self._poll_interval
        while self._running:
            try:
                await self._check_all_positions()
            except Exception:
                logger.exception("tee.cycle_error")
            await asyncio.sleep(self._poll_interval)
        logger.info("tee.stopped")

    def stop(self) -> None:
        """Signal the engine to stop after the current cycle completes."""
        self._running = False

    # ── Position scan ─────────────────────────────────────────────────────────

    async def _check_all_positions(self) -> None:
        """Single poll cycle: scan positions, alert unmanaged, evaluate exits."""
        managed = await self._get_open_managed_positions()
        await self._alert_unmanaged_positions()

        if not managed:
            logger.debug("tee.no_managed_positions")
            return

        logger.info(
            "tee.cycle_start",
            managed_positions=len(managed),
            symbols=[p["symbol"] for p in managed],
        )

        for position in managed:
            try:
                await self._evaluate_exit_conditions(position)
            except Exception:
                logger.exception(
                    "tee.position_evaluation_error",
                    symbol=position.get("symbol"),
                )

    async def _get_open_managed_positions(self) -> list[dict]:
        """
        Scan DynamoDB for positions that are open AND have an exit policy.

        Filter: direction <> FLAT  AND  attribute_exists(stop_price)

        Returns a list of parsed position dicts ready for exit evaluation.
        """
        response = await asyncio.to_thread(
            self._dynamo.scan,
            TableName=self._positions_table,
            FilterExpression=(
                "#dir <> :flat AND attribute_exists(stop_price)"
            ),
            ExpressionAttributeValues={
                ":flat": {"S": "FLAT"},
            },
            ProjectionExpression=(
                "symbol, #dir, quantity, avg_price, avg_entry_price, "
                "stop_price, take_profit, last_price, product, exit_order_id, exit_state"
            ),
            ExpressionAttributeNames={"#dir": "direction"},
        )
        items = response.get("Items", [])
        resolved = []
        for item in items:
            pos = self._parse_position(item)
            if pos is not None:
                resolved.append(pos)
        return resolved

    async def _alert_unmanaged_positions(self) -> None:
        """
        Scan for open positions that have NO stop_price.

        These positions are unprotected. Each one gets a CRITICAL log on every
        poll cycle until the condition is resolved.
        """
        response = await asyncio.to_thread(
            self._dynamo.scan,
            TableName=self._positions_table,
            FilterExpression=(
                "#dir <> :flat AND attribute_not_exists(stop_price)"
            ),
            ExpressionAttributeValues={
                ":flat": {"S": "FLAT"},
            },
            ProjectionExpression="symbol, #dir, quantity",
            ExpressionAttributeNames={"#dir": "direction"},
        )
        for item in response.get("Items", []):
            symbol = item.get("symbol", {}).get("S", "UNKNOWN")
            logger.critical(
                "tee.unmanaged_open_position",
                symbol=symbol,
                detail=(
                    "Position is OPEN but has no exit policy (stop_price absent). "
                    "Risk is unprotected. Investigate immediately."
                ),
            )
            if self._live_counters is not None:
                self._live_counters.tee_unmanaged_detections += 1

    def _parse_position(self, item: dict) -> Optional[dict]:
        """Parse a DynamoDB attribute-typed position item into a plain dict."""
        symbol    = item.get("symbol",    {}).get("S", "UNKNOWN")
        direction = item.get("direction", {}).get("S", "")
        qty_str   = item.get("quantity",  {}).get("N")
        stop_str  = item.get("stop_price", {}).get("N")
        tp_str    = item.get("take_profit", {}).get("N")
        # avg_price may appear under either name depending on fill path
        avg_str   = (
            item.get("avg_price", {}).get("N")
            or item.get("avg_entry_price", {}).get("N")
        )
        last_str  = item.get("last_price", {}).get("N")
        exit_oid  = item.get("exit_order_id", {}).get("S")
        exit_state = item.get("exit_state", {}).get("S")

        if qty_str is None or stop_str is None:
            return None

        quantity = float(qty_str)
        if abs(quantity) < 1e-9:
            return None

        return {
            "symbol":        symbol,
            "direction":     direction,
            "quantity":      quantity,
            "avg_price":     float(avg_str) if avg_str else 0.0,
            "stop_price":    float(stop_str),
            "take_profit":   float(tp_str) if tp_str else None,
            "last_price":    float(last_str) if last_str else None,
            "exit_order_id": exit_oid,
            "exit_state":    exit_state,
        }

    # ── Exit condition evaluation ─────────────────────────────────────────────

    async def _evaluate_exit_conditions(self, position: dict) -> None:
        """Check stop-loss, take-profit, and trailing stop for one managed position."""
        if position["exit_order_id"] is not None:
            return  # exit already in-flight

        symbol     = position["symbol"]
        last_price = await self._get_last_price(symbol, position)

        if last_price is None:
            logger.warning("tee.no_price_available", symbol=symbol)
            return

        direction   = position["direction"]
        stop_price  = position["stop_price"]
        take_profit = position["take_profit"]
        exit_state  = position.get("exit_state")

        # 1. Stop-loss / trailing stop: highest priority, always checked first.
        #    When trailing is active the stop_price field holds the trailing level.
        if self.stop_loss_triggered(direction, last_price, stop_price):
            trigger = (
                ExitTriggerType.TRAILING
                if exit_state == "TRAILING_ACTIVE"
                else ExitTriggerType.STOP_LOSS
            )
            logger.warning(
                "tee.stop_loss_triggered",
                symbol=symbol,
                direction=direction,
                last_price=last_price,
                stop_price=stop_price,
                trigger=trigger.value,
            )
            await self._fire_exit(position, trigger, last_price)
            return

        # 2. Take-profit: suppressed while trailing is active (trailing provides a
        #    dynamic exit that is often better than the original fixed TP).
        if (
            take_profit is not None
            and exit_state != "TRAILING_ACTIVE"
            and self.take_profit_triggered(direction, last_price, take_profit)
        ):
            logger.info(
                "tee.take_profit_triggered",
                symbol=symbol,
                direction=direction,
                last_price=last_price,
                take_profit=take_profit,
            )
            await self._fire_exit(position, ExitTriggerType.TAKE_PROFIT, take_profit)
            return

        # 3. Trailing stop management (Phase 3).
        if self._trailing_enabled:
            await self._manage_trailing_stop(position, last_price)

    # ── Trigger logic (public for testability) ────────────────────────────────

    @staticmethod
    def stop_loss_triggered(direction: str, last_price: float, stop_price: float) -> bool:
        """
        Return True when the stop-loss condition is met.

        LONG:  price fell to or below stop_price  (last_price <= stop_price)
        SHORT: price rose to or above stop_price  (last_price >= stop_price)
        """
        if direction == "LONG":
            return last_price <= stop_price
        if direction == "SHORT":
            return last_price >= stop_price
        return False

    @staticmethod
    def take_profit_triggered(direction: str, last_price: float, take_profit: float) -> bool:
        """
        Return True when the take-profit condition is met.

        LONG:  price rose to or above take_profit  (last_price >= take_profit)
        SHORT: price fell to or below take_profit  (last_price <= take_profit)
        """
        if direction == "LONG":
            return last_price >= take_profit
        if direction == "SHORT":
            return last_price <= take_profit
        return False

    @staticmethod
    def trailing_activation_triggered(
        direction: str,
        last_price: float,
        avg_entry_price: float,
        activation_pct: float,
    ) -> bool:
        """
        Return True when trailing stop should activate.

        LONG:  price rose >= entry * (1 + activation_pct/100)
        SHORT: price fell <= entry * (1 - activation_pct/100)
        """
        if direction == "LONG":
            return last_price >= avg_entry_price * (1.0 + activation_pct / 100.0)
        if direction == "SHORT":
            return last_price <= avg_entry_price * (1.0 - activation_pct / 100.0)
        return False

    @staticmethod
    def compute_trailing_stop(
        direction: str,
        last_price: float,
        current_stop: float,
        trail_pct: float,
    ) -> float:
        """
        Compute the new trailing stop price.

        LONG:  new_stop = max(current_stop, last_price * (1 - trail_pct/100))
               Stop only moves upward — never loosens risk.
        SHORT: new_stop = min(current_stop, last_price * (1 + trail_pct/100))
               Stop only moves downward — never loosens risk.
        """
        if direction == "LONG":
            candidate = last_price * (1.0 - trail_pct / 100.0)
            return max(current_stop, candidate)
        if direction == "SHORT":
            candidate = last_price * (1.0 + trail_pct / 100.0)
            return min(current_stop, candidate)
        return current_stop

    # ── Trailing stop management ──────────────────────────────────────────────

    async def _manage_trailing_stop(self, position: dict, last_price: float) -> None:
        """Activate or advance the trailing stop for one position."""
        exit_state = position.get("exit_state")
        direction  = position["direction"]

        if exit_state == "TRAILING_ACTIVE":
            await self._advance_trailing_stop(position, last_price)
        else:
            if self.trailing_activation_triggered(
                direction, last_price, position["avg_price"],
                self._trailing_activation_pct,
            ):
                await self._activate_trailing_stop(position, last_price)

    async def _activate_trailing_stop(self, position: dict, last_price: float) -> None:
        """
        Transition exit_state → TRAILING_ACTIVE and write initial trailing stop.
        The trailing stop is always at least as tight as the existing stop_price.
        """
        symbol    = position["symbol"]
        direction = position["direction"]
        new_stop  = self.compute_trailing_stop(
            direction, last_price, position["stop_price"], self._trailing_stop_pct
        )

        logger.info(
            "tee.trailing_stop_activated",
            symbol=symbol,
            direction=direction,
            last_price=last_price,
            initial_trailing_stop=new_stop,
            prev_stop=position["stop_price"],
        )

        if self._live_counters is not None:
            self._live_counters.tee_trailing_activated += 1

        await self._write_trailing_stop(symbol, new_stop, state="TRAILING_ACTIVE")

    async def _advance_trailing_stop(self, position: dict, last_price: float) -> None:
        """
        Advance the trailing stop if price moved favorably. No-op if stop would loosen.
        """
        symbol    = position["symbol"]
        direction = position["direction"]
        new_stop  = self.compute_trailing_stop(
            direction, last_price, position["stop_price"], self._trailing_stop_pct
        )

        if new_stop == position["stop_price"]:
            return  # no improvement — skip DynamoDB write

        logger.info(
            "tee.trailing_stop_advanced",
            symbol=symbol,
            direction=direction,
            last_price=last_price,
            new_stop=new_stop,
            prev_stop=position["stop_price"],
        )

        await self._write_trailing_stop(symbol, new_stop, state="TRAILING_ACTIVE")

    async def _write_trailing_stop(self, symbol: str, new_stop: float, state: str) -> None:
        """Persist the new trailing stop price and exit_state to DynamoDB."""
        from shared.risk_state import position_key  # noqa: PLC0415

        try:
            await asyncio.to_thread(
                self._dynamo.update_item,
                TableName=self._positions_table,
                Key=position_key(symbol),
                UpdateExpression="SET stop_price = :new_stop, exit_state = :state",
                ConditionExpression="direction <> :flat",
                ExpressionAttributeValues={
                    ":new_stop": {"N": str(new_stop)},
                    ":state":    {"S": state},
                    ":flat":     {"S": "FLAT"},
                },
            )
        except Exception:
            logger.exception("tee.trailing_stop_write_failed", symbol=symbol)

    # ── Exit dispatch ─────────────────────────────────────────────────────────

    async def _fire_exit(
        self,
        position: dict,
        trigger: ExitTriggerType,
        exit_price: Optional[float],
    ) -> None:
        """Build and route an ExitOrderRequest via ExitOrderRouter."""
        direction = position["direction"]
        request = ExitOrderRequest.from_position(
            symbol=position["symbol"],
            market="NSE",
            direction=direction,
            signed_quantity=position["quantity"],
            trigger_type=trigger,
            session_date_ist=_session_date_ist(),
            avg_entry_price=position["avg_price"],
            exit_price=exit_price,
        )
        logger.info(
            "tee.firing_exit",
            symbol=position["symbol"],
            direction=direction,
            trigger=trigger.value,
            close_side=request.close_side,
            close_qty=request.close_qty,
            product_type=request.product_type,
            exit_id=request.exit_id,
            exit_state=position.get("exit_state"),
        )

        if self._live_counters is not None:
            c = self._live_counters
            if trigger == ExitTriggerType.STOP_LOSS:
                c.tee_stop_loss_hits += 1
            elif trigger == ExitTriggerType.TAKE_PROFIT:
                c.tee_take_profit_hits += 1
            elif trigger == ExitTriggerType.TRAILING:
                c.tee_trailing_hits += 1

            from datetime import timedelta as _td  # noqa: PLC0415
            now_ist = datetime.now(timezone(_td(seconds=19800))).strftime("%Y-%m-%d %H:%M:%S IST")
            signed_qty = int(request.close_qty) if request.close_side == "BUY" else -int(request.close_qty)
            event_str = (
                f"{now_ist} | {position['symbol']:<10} | {trigger.value:<18} | "
                f"exit={exit_price or position['avg_price']:.2f}  | "
                f"qty={signed_qty:+d}"
            )
            c.tee_latest_events = (c.tee_latest_events or [])[-9:] + [event_str]

        await self._router.route(request)

    # ── Price source ──────────────────────────────────────────────────────────

    async def _get_last_price(self, symbol: str, position: dict) -> Optional[float]:
        """
        Resolve the current LTP for *symbol* via LtpResolver.

        Priority (handled by LtpResolver):
            1. prices table (QUOTE#NSE/{symbol}/LATEST) — checked for freshness via
               captured_at field written by LiveQuotePoller.
            2. position's last_price (entry fill price) — always is_stale=True.

        Logs a warning when the price is stale so the operator can see that exit
        decisions are being made against non-current prices.
        """
        result = await self._ltp_resolver.resolve(
            symbol,
            position_fill_price=position.get("last_price"),
        )
        if result is None:
            return None

        if result.is_stale:
            age = result.age_seconds or float("inf")
            # In LIVE mode: block exit evaluation when LTP is too stale.
            # Executing a stop-loss against a 30-second-old price is dangerous.
            # In PAPER mode: warn only — stale exits are non-monetary.
            if (
                self._router.mode.value == "live"
                and age > self._max_stale_ltp_live
            ):
                logger.critical(
                    "tee.stale_ltp_blocked_live",
                    symbol=symbol,
                    source=result.source,
                    age_seconds=age,
                    max_allowed_seconds=self._max_stale_ltp_live,
                    detail=(
                        "Exit evaluation BLOCKED: LTP is stale beyond live threshold. "
                        "LiveQuotePoller may be offline. Check execution_engine logs."
                    ),
                )
                if self._live_counters is not None:
                    self._live_counters.tee_stale_ltp_blocks += 1
                return None
            logger.warning(
                "tee.stale_ltp",
                symbol=symbol,
                source=result.source,
                age_seconds=age,
                price=result.price,
            )
        else:
            logger.debug(
                "tee.ltp_resolved",
                symbol=symbol,
                source=result.source,
                age_seconds=result.age_seconds,
                price=result.price,
            )
        return result.price
