"""
NSE MIS (Margin Intraday Square-off) Pre-Close Manager.

Zerodha auto-squares all MIS (intraday margin) positions at approximately
15:15 IST regardless of slippage or market conditions. This is done at
whatever price is available, with no control over execution quality.

This module fires proactively at 15:05 IST:
    1. Reads all open MIS positions from DynamoDB.
    2. Sends SELL (or BUY for short) orders at market price.
    3. Monitors fills until 15:10 IST.
    4. If any position is not confirmed closed by 15:10, activates the
       kill switch and sends a CRITICAL alert — the broker will close it
       at 15:15 but we need operators aware of the exposure.

Why 15:05 and not 15:00?
    NSE liquidity typically drops sharply after 15:00 as large participants
    start squaring. We close at 15:05 to still have reasonable market depth.
    Closing at 15:00 is too early (misses last 15 min of profit potential);
    15:10 is too close to the 15:15 auto-square-off window for safety.

Signed quantity invariant:
    quantity > 0  → LONG  (net long exposure)
    quantity < 0  → SHORT (net short exposure)
    quantity == 0 → FLAT  (no exposure — excluded from square-off)

    The DynamoDB ``direction`` field is cross-validated against the signed
    quantity on every scan. Mismatches emit a WARNING and fall back to the
    quantity-derived direction, which is the canonical source of truth.

Configurable via environment variables:
    MIS_CLOSE_TIME_IST    — HH:MM to fire close (default "15:05")
    MIS_DEADLINE_TIME_IST — HH:MM deadline to confirm fills (default "15:10")
    MIS_CONFIRM_INTERVAL  — seconds between fill checks (default 10)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timezone
from typing import TYPE_CHECKING, Any, Optional
import os

from services.shared.logging.logger import get_logger
from services.shared.monitoring import LiveCounters

if TYPE_CHECKING:
    from services.execution_engine.brokers.zerodha_broker import ZerodhaBrokerClient
    from services.execution_engine.orders.order_manager import OrderManager

logger = get_logger(__name__, service_name="execution_engine")

# ── Configuration ─────────────────────────────────────────────────────────────
_CLOSE_TIME_STR:    str = os.environ.get("MIS_CLOSE_TIME_IST",    "15:05")
_DEADLINE_TIME_STR: str = os.environ.get("MIS_DEADLINE_TIME_IST", "15:10")
_CONFIRM_INTERVAL:  int = int(os.environ.get("MIS_CONFIRM_INTERVAL", "10"))

# ── IST timezone offset (UTC+05:30 = 19800 seconds) ──────────────────────────
_IST_OFFSET_SECONDS = 19800


def _parse_ist_time(hhmm: str) -> time:
    """Parse "HH:MM" string to a time object."""
    h, m = hhmm.split(":")
    return time(int(h), int(m), 0)


_CLOSE_TIME    = _parse_ist_time(_CLOSE_TIME_STR)
_DEADLINE_TIME = _parse_ist_time(_DEADLINE_TIME_STR)


def _now_ist() -> datetime:
    """Return the current IST time as a timezone-aware datetime."""
    from datetime import timedelta
    ist_offset = timezone(timedelta(seconds=_IST_OFFSET_SECONDS))
    return datetime.now(ist_offset)


def _seconds_until_ist(target: time) -> float:
    """
    Return seconds from now until the next occurrence of target IST time.
    Returns 0 if the target has already passed today.
    """
    now_ist = _now_ist()
    target_dt = now_ist.replace(
        hour=target.hour,
        minute=target.minute,
        second=target.second,
        microsecond=0,
    )
    delta = (target_dt - now_ist).total_seconds()
    return max(0.0, delta)


class MISSquareOffManager:
    """
    Manages proactive closure of NSE MIS intraday positions before 15:15 IST.

    Usage (called from ExecutionService.start() as a concurrent task):

        asyncio.gather(
            self._consume_approved_signals(),
            self._margin_refresh_loop(),
            MISSquareOffManager(zerodha, order_manager, dynamo, table).run(),
        )
    """

    def __init__(
        self,
        zerodha_broker: "ZerodhaBrokerClient",
        order_manager: "OrderManager",
        dynamo_client: Any,
        positions_table: str,
        kill_switch_table: str,
        sns_client: Optional[Any] = None,
        alert_topic_arn: str = "",
        live_counters: Optional[LiveCounters] = None,
        paper_trading: bool = True,
    ) -> None:
        """
        Args:
            zerodha_broker: Authenticated Zerodha broker client.
            order_manager: Order manager for placing close orders.
            dynamo_client: Low-level boto3 DynamoDB client.
            positions_table: DynamoDB table name for positions.
            kill_switch_table: DynamoDB risk-state table (kill switch key).
            sns_client: Optional SNS client for critical alerts.
            alert_topic_arn: SNS topic ARN for CRITICAL alerts.
            live_counters: Optional shared LiveCounters for monitoring status.
            paper_trading: When True, simulate fills via apply_fill_to_position
                instead of placing real Zerodha orders.
        """
        self._zerodha = zerodha_broker
        self._order_manager = order_manager
        self._dynamo = dynamo_client
        self._positions_table = positions_table
        self._kill_switch_table = kill_switch_table
        self._sns = sns_client
        self._alert_topic_arn = alert_topic_arn
        self._live_counters = live_counters
        self._paper_trading = paper_trading
        self._running = True

    async def run(self) -> None:
        """
        Main loop — waits until MIS_CLOSE_TIME_IST, then closes all MIS
        positions. Runs once per trading day.

        This coroutine runs for the full session lifetime. It will fire once
        at _CLOSE_TIME each day and then sleep until the next day's close time.
        """
        while self._running:
            wait_secs = _seconds_until_ist(_CLOSE_TIME)

            # If the service starts after the deadline has already passed today
            # (e.g. container restart after market close), skip this day's execution
            # and wait until tomorrow's close time.  Without this guard every restart
            # after 15:10 IST fires a 0-sleep → immediate close attempt → deadline
            # already exceeded → kill switch activated — a false positive.
            if wait_secs == 0.0 and _seconds_until_ist(_DEADLINE_TIME) == 0.0:
                logger.warning(
                    "mis_square_off.skipped_past_deadline",
                    close_time_ist=_CLOSE_TIME_STR,
                    deadline_time_ist=_DEADLINE_TIME_STR,
                    detail="Service started after MIS deadline — skipping today, sleeping until tomorrow",
                )
                # Sleep 24h - deadline_to_close gap to land at tomorrow's close time
                await asyncio.sleep(86400 - (_IST_OFFSET_SECONDS % 86400))
                continue

            logger.info(
                "mis_square_off.scheduled",
                close_time_ist=_CLOSE_TIME_STR,
                deadline_time_ist=_DEADLINE_TIME_STR,
                seconds_until_close=round(wait_secs),
            )
            await asyncio.sleep(wait_secs)

            if not self._running:
                break

            try:
                await self._execute_mis_square_off()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do NOT re-raise — service restart during the 15:05-15:15 IST window
                # would leave positions unmanaged longer than Zerodha's 15:15 auto-square
                # backstop.  Log CRITICAL and alert; Zerodha closes the residual exposure.
                logger.critical(
                    "mis_square_off.execute_crashed",
                    detail=(
                        "Unhandled exception in MIS square-off execution. "
                        "Zerodha will auto-close at 15:15 IST. Investigate immediately."
                    ),
                    exc_info=True,
                )
                await self._send_critical_alert(
                    "CRITICAL: MIS square-off execution crashed with an unhandled exception. "
                    "Zerodha will auto-close MIS positions at 15:15 IST. "
                    "Check execution_engine logs for the traceback."
                )

            # Sleep past midnight IST (ensure we don't double-fire)
            await asyncio.sleep(3600)

    async def _execute_mis_square_off(self) -> None:
        """
        Close all open MIS positions proactively before Zerodha's auto-square.

        Steps:
            1. Read open MIS positions from DynamoDB (product=MIS).
            2. Place market SELL orders for all long MIS positions.
            3. Place market BUY orders for all short MIS positions.
            4. Poll for fills every _CONFIRM_INTERVAL seconds.
            5. If any position is still open at _DEADLINE_TIME, activate
               kill switch and send CRITICAL alert.
        """
        logger.warning(
            "mis_square_off.starting",
            close_time_ist=_CLOSE_TIME_STR,
            detail="Beginning proactive MIS position closure before Zerodha 15:15 auto-square",
        )

        try:
            mis_positions = await self._get_open_mis_positions()
        except Exception:
            logger.exception("mis_square_off.fetch_positions_failed")
            await self._send_critical_alert(
                "FAILED to read MIS positions before square-off. "
                "Zerodha will auto-square at 15:15 IST with no fill records."
            )
            return

        if not mis_positions:
            logger.info("mis_square_off.no_positions", detail="No open MIS positions — nothing to close")
            if self._live_counters is not None:
                self._live_counters.mis_positions_discovered = 0
                self._live_counters.mis_long_discovered = 0
                self._live_counters.mis_short_discovered = 0
                self._live_counters.mis_orders_placed = 0
                self._live_counters.mis_orders_rejected = 0
                self._live_counters.mis_positions_flat = 0
                self._live_counters.mis_at_deadline = False
            return

        long_count  = sum(1 for p in mis_positions if p["effective_dir"] == "LONG")
        short_count = sum(1 for p in mis_positions if p["effective_dir"] == "SHORT")

        if self._live_counters is not None:
            self._live_counters.mis_positions_discovered = len(mis_positions)
            self._live_counters.mis_long_discovered      = long_count
            self._live_counters.mis_short_discovered     = short_count

        logger.warning(
            "mis_square_off.positions_found",
            position_count=len(mis_positions),
            symbols=[p["symbol"] for p in mis_positions],
            directions={p["symbol"]: p["effective_dir"] for p in mis_positions},
            quantities={p["symbol"]: p["quantity"] for p in mis_positions},
        )

        # Place close orders for each open MIS position
        close_order_ids: list[str] = []
        rejected_count = 0
        for position in mis_positions:
            try:
                order_id = await self._place_mis_close_order(position)
                if order_id:
                    close_order_ids.append(order_id)
                else:
                    rejected_count += 1
            except Exception:
                rejected_count += 1
                logger.exception(
                    "mis_square_off.close_order_failed",
                    symbol=position.get("symbol"),
                )

        if self._live_counters is not None:
            self._live_counters.mis_orders_placed   = len(close_order_ids)
            self._live_counters.mis_orders_rejected = rejected_count

        logger.info(
            "mis_square_off.close_orders_placed",
            total_positions=len(mis_positions),
            orders_placed=len(close_order_ids),
        )

        # Poll for fills until deadline
        await self._await_fills_or_escalate(close_order_ids, mis_positions)

    async def _get_open_mis_positions(self) -> list[dict]:
        """
        Read open MIS positions from DynamoDB positions table.

        Returns a list of resolved position dicts with the following keys:
            symbol          (str)   — instrument symbol
            quantity        (float) — signed quantity (+ = long, - = short)
            effective_dir   (str)   — "LONG" or "SHORT", derived from quantity
            close_side      (str)   — "SELL" (long) or "BUY" (short)
            close_qty       (float) — abs(quantity), always positive
            avg_entry_price (float) — volume-weighted average entry price

        Filter: product = MIS AND quantity <> 0

        The ``direction`` DynamoDB field is cross-validated against the signed
        quantity on every row. Mismatches are logged as WARNING and the
        quantity-derived direction is used as the authoritative value.
        """
        response = await asyncio.to_thread(
            self._dynamo.scan,
            TableName=self._positions_table,
            FilterExpression="product = :mis AND quantity <> :zero",
            ExpressionAttributeValues={
                ":mis":  {"S": "MIS"},
                ":zero": {"N": "0"},
            },
            ProjectionExpression="symbol, direction, quantity, avg_entry_price, last_price, product, exit_order_id",
        )
        items = response.get("Items", [])

        # Validate which exit_order_ids are genuinely in-flight before resolving.
        # Stale IDs (terminal or missing order) are excluded so positions aren't
        # silently skipped due to a leftover exit_order_id from a prior position entry.
        valid_exit_ids = await self._build_valid_exit_ids(items)

        resolved: list[dict] = []
        for item in items:
            pos = self._resolve_position(item, valid_exit_ids)
            if pos is not None:
                resolved.append(pos)
        return resolved

    async def _build_valid_exit_ids(self, items: list[dict]) -> frozenset[str]:
        """
        Return the set of exit_order_ids that are genuinely in-flight (PENDING/PLACED).

        Queries the order manager for each exit_order_id found in the position scan.
        IDs whose orders are in a terminal state (FILLED, CANCELLED, REJECTED) or not
        found in the orders table are considered stale and excluded from the result.

        Args:
            items: Raw DynamoDB items from the positions table scan.

        Returns:
            frozenset of order IDs that are genuinely in-flight.
        """
        if self._order_manager is None:
            logger.warning(
                "mis_square_off.no_order_manager — cannot validate exit_order_ids, "
                "treating all as stale so no position is silently skipped"
            )
            return frozenset()

        from execution_engine.orders.order import OrderStatus  # noqa: PLC0415
        _TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}

        valid: set[str] = set()
        for item in items:
            eid = item.get("exit_order_id", {}).get("S")
            if not eid:
                continue
            try:
                stored = await self._order_manager.get_stored_order(eid)
            except Exception:
                logger.warning(
                    "mis_square_off.exit_id_check_failed",
                    exit_order_id=eid,
                    detail="Order lookup failed — treating exit_order_id as stale",
                )
                continue
            if stored is None:
                logger.warning(
                    "mis_square_off.stale_exit_order_id",
                    exit_order_id=eid,
                    detail="Order not found in DynamoDB — exit_order_id is stale",
                )
                continue
            if stored.status in _TERMINAL:
                logger.warning(
                    "mis_square_off.stale_exit_order_id",
                    exit_order_id=eid,
                    order_status=stored.status.value,
                    detail="Order in terminal state — exit_order_id is stale",
                )
                continue
            valid.add(eid)

        return frozenset(valid)

    def _resolve_position(self, item: dict, valid_exit_ids: frozenset[str]) -> Optional[dict]:
        """
        Parse and validate a single DynamoDB position item.

        Derives effective direction and close side from signed quantity.
        Cross-validates against the stored ``direction`` field and emits a
        WARNING on any mismatch. Returns None if quantity is effectively zero.

        Args:
            item: Raw DynamoDB item dict (attribute-typed).
            valid_exit_ids: Set of exit_order_ids confirmed in-flight by
                ``_build_valid_exit_ids``. Stale IDs are absent from this set.

        Returns:
            Resolved position dict, or None if quantity is flat.
        """
        symbol = item.get("symbol", {}).get("S", "UNKNOWN")
        raw_qty_str = item.get("quantity", {}).get("N")
        stored_dir = item.get("direction", {}).get("S")  # may be absent
        avg_price  = float(item.get("avg_entry_price", {}).get("N", "0") or "0")
        last_price = float(item.get("last_price",      {}).get("N", "0") or "0") or avg_price
        exit_order_id = item.get("exit_order_id", {}).get("S")

        # Only skip the position if the exit order is confirmed in-flight.
        # A stale exit_order_id (terminal order or missing) is NOT in valid_exit_ids
        # and must not block the square-off — that was the root cause of the BEML bug.
        if exit_order_id and exit_order_id in valid_exit_ids:
            logger.warning(
                "mis_square_off.skip_exit_in_flight",
                symbol=symbol,
                exit_order_id=exit_order_id,
                detail="exit_order_id confirmed in-flight — TEE is handling this exit",
            )
            return None
        if exit_order_id and exit_order_id not in valid_exit_ids:
            logger.warning(
                "mis_square_off.overriding_stale_exit_order_id",
                symbol=symbol,
                exit_order_id=exit_order_id,
                detail="exit_order_id is stale (terminal or not found) — proceeding with MIS square-off",
            )

        if raw_qty_str is None:
            logger.warning(
                "mis_square_off.position_missing_quantity",
                symbol=symbol,
                detail="No quantity attribute on DynamoDB item — skipping",
            )
            return None

        quantity = float(raw_qty_str)

        # Guard: quantity must be meaningfully non-zero
        if abs(quantity) < 1e-9:
            logger.warning(
                "mis_square_off.position_effectively_zero",
                symbol=symbol,
                quantity=quantity,
                detail="quantity is effectively zero but passed <> filter — skipping",
            )
            return None

        # Canonical direction derived from signed quantity
        effective_dir = "LONG" if quantity > 0 else "SHORT"

        # Cross-validate stored direction field
        if stored_dir is not None and stored_dir != effective_dir:
            logger.warning(
                "mis_square_off.direction_quantity_mismatch",
                symbol=symbol,
                stored_direction=stored_dir,
                effective_direction=effective_dir,
                quantity=quantity,
                detail=(
                    f"DynamoDB direction={stored_dir!r} disagrees with "
                    f"signed quantity={quantity} → using quantity-derived "
                    f"direction={effective_dir!r} for square-off"
                ),
            )
        elif stored_dir is None:
            logger.warning(
                "mis_square_off.direction_field_absent",
                symbol=symbol,
                quantity=quantity,
                effective_direction=effective_dir,
                detail="direction field absent — deriving from signed quantity",
            )

        # Close side is always derived from signed quantity, never from direction
        close_side = "SELL" if quantity > 0 else "BUY"
        close_qty = abs(quantity)

        return {
            "symbol":          symbol,
            "quantity":        quantity,        # signed
            "effective_dir":   effective_dir,   # quantity-derived
            "close_side":      close_side,      # quantity-derived
            "close_qty":       close_qty,       # always positive
            "avg_entry_price": avg_price,
            "last_price":      last_price,      # for paper fill price
        }

    async def _place_mis_close_order(self, position: dict) -> Optional[str]:
        """
        Place a market close order for a single MIS position.

        Close side and quantity are taken from the resolved position dict
        produced by ``_resolve_position``:
            - close_side is derived from signed quantity (SELL=long, BUY=short)
            - close_qty  is abs(quantity) and is always positive

        Args:
            position: Resolved dict from _resolve_position.

        Returns:
            Internal order_id if placed successfully, None on failure.
        """
        from services.execution_engine.orders.order import (  # noqa: PLC0415
            Market,
            OrderRequest,
            OrderSide,
            OrderType,
            ProductType,
        )
        import uuid  # noqa: PLC0415

        symbol    = position["symbol"]
        close_qty = position["close_qty"]    # abs(quantity), always positive
        quantity  = position["quantity"]     # signed, for logging

        if close_qty <= 0:
            logger.warning(
                "mis_square_off.close_qty_zero",
                symbol=symbol,
                quantity=quantity,
                detail="close_qty is zero after resolution — skipping order placement",
            )
            return None

        close_side = (
            OrderSide.SELL if position["close_side"] == "SELL" else OrderSide.BUY
        )

        order_id = f"mis-close-{uuid.uuid4().hex[:12]}"

        # Paper mode: simulate fill directly via order_manager instead of hitting Zerodha.
        if self._paper_trading:
            fill_price = position.get("last_price") or position.get("avg_entry_price", 0.0)
            try:
                await self._order_manager.apply_fill_to_position(
                    symbol=symbol,
                    side=close_side,
                    filled_quantity=close_qty,
                    avg_fill_price=fill_price,
                    last_price=fill_price,
                    market_str="NSE",
                    order_id=order_id,
                    signal_id=f"mis-square-off-{symbol}",
                    risk_decision_id="mis-auto-close",
                )
                logger.info(
                    "mis_square_off.paper_close_simulated",
                    symbol=symbol,
                    side=close_side.value,
                    close_qty=close_qty,
                    fill_price=fill_price,
                    order_id=order_id,
                )
                return order_id
            except Exception:
                logger.exception(
                    "mis_square_off.paper_close_failed",
                    symbol=symbol,
                    side=close_side.value,
                )
                return None

        order_request = OrderRequest(
            order_id=order_id,
            signal_id=f"mis-square-off-{symbol}",
            risk_decision_id="mis-auto-close",
            symbol=symbol,
            side=close_side,
            quantity=close_qty,
            order_type=OrderType.MARKET,
            market=Market.NSE,
            product_type=ProductType.MIS,
        )

        response = await self._zerodha.place_order(order_request)
        if response and response.success:
            logger.info(
                "mis_square_off.close_order_placed",
                symbol=symbol,
                side=close_side.value,
                signed_quantity=quantity,
                close_qty=close_qty,
                effective_direction=position["effective_dir"],
                product_type="MIS",
                order_id=order_id,
                broker_order_id=response.broker_order_id,
            )
            return order_id
        else:
            logger.error(
                "mis_square_off.close_order_rejected",
                symbol=symbol,
                side=close_side.value,
                close_qty=close_qty,
                error=getattr(response, "error", "unknown"),
            )
            return None

    async def _await_fills_or_escalate(
        self,
        order_ids: list[str],
        original_positions: list[dict],
    ) -> None:
        """
        Poll for order fills until _DEADLINE_TIME IST.
        If any order is unfilled at deadline, activate kill switch and alert.

        Args:
            order_ids: Internal order IDs of placed close orders.
            original_positions: Original position list (for escalation context).
        """
        symbols_pending = {p["symbol"] for p in original_positions}

        while symbols_pending:
            # Check if we've hit the deadline
            if _seconds_until_ist(_DEADLINE_TIME) == 0:
                break

            await asyncio.sleep(_CONFIRM_INTERVAL)

            # Check fill status for pending symbols
            newly_filled: set[str] = set()
            for symbol in list(symbols_pending):
                try:
                    broker_positions = await self._zerodha.get_positions()
                    # Position is closed if it no longer appears in broker positions
                    # or if quantity = 0
                    broker_qty = next(
                        (abs(p.get("quantity", 0)) for p in broker_positions
                         if p.get("tradingsymbol") == symbol and
                         p.get("product") == "MIS"),
                        0,
                    )
                    if broker_qty == 0:
                        newly_filled.add(symbol)
                        logger.info(
                            "mis_square_off.position_closed",
                            symbol=symbol,
                        )
                except Exception:
                    logger.warning(
                        "mis_square_off.fill_check_failed",
                        symbol=symbol,
                        exc_info=True,
                    )

            symbols_pending -= newly_filled

        total = len(original_positions)
        closed_count = total - len(symbols_pending)

        if self._live_counters is not None:
            self._live_counters.mis_positions_flat = closed_count

        if symbols_pending:
            # Positions still open at deadline — escalate
            if self._live_counters is not None:
                self._live_counters.mis_at_deadline = True

            logger.critical(
                "mis_square_off.positions_not_closed_at_deadline",
                unclosed_symbols=list(symbols_pending),
                deadline_ist=_DEADLINE_TIME_STR,
                detail="Zerodha will auto-close at 15:15 IST at whatever price is available. "
                       "Kill switch activated to halt new orders.",
            )

            # Activate kill switch — trading should halt until positions are resolved
            await self._activate_kill_switch(
                reason=f"MIS positions not confirmed closed by {_DEADLINE_TIME_STR} IST: "
                       f"{sorted(symbols_pending)}"
            )
            await self._send_critical_alert(
                f"CRITICAL: MIS positions NOT closed by {_DEADLINE_TIME_STR} IST. "
                f"Symbols: {sorted(symbols_pending)}. "
                f"Zerodha will auto-square at 15:15 IST. Kill switch ACTIVATED."
            )
        else:
            if self._live_counters is not None:
                self._live_counters.mis_at_deadline = False
            logger.info(
                "mis_square_off.all_positions_closed",
                detail="All MIS positions confirmed closed before deadline",
            )

    async def _activate_kill_switch(self, reason: str) -> None:
        """Write kill_switch=ACTIVE to DynamoDB risk-state table."""
        try:
            from datetime import datetime, timezone
            from shared.risk_state import kill_switch_item

            now = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(
                self._dynamo.put_item,
                TableName=self._kill_switch_table,
                Item=kill_switch_item(
                    active=True,
                    reason=reason,
                    activated_by="mis-square-off-manager",
                    activated_at=now,
                    updated_at=now,
                ),
            )
            if self._live_counters is not None:
                self._live_counters.mis_kill_switch_activated = True
            logger.critical(
                "kill_switch.activated",
                activated_by="mis-square-off-manager",
                reason=reason,
            )
        except Exception:
            logger.exception("mis_square_off.kill_switch_activation_failed")

    async def _send_critical_alert(self, message: str) -> None:
        """Publish a CRITICAL alert to SNS."""
        if self._sns is None or not self._alert_topic_arn:
            logger.warning(
                "mis_square_off.no_sns_configured",
                detail=message,
            )
            return
        try:
            await asyncio.to_thread(
                self._sns.publish,
                TopicArn=self._alert_topic_arn,
                Subject="[QUANTEMBRACE CRITICAL] MIS Square-Off Alert",
                Message=message,
            )
        except Exception:
            logger.exception("mis_square_off.sns_publish_failed")

    def stop(self) -> None:
        """Signal the manager to stop after the current run."""
        self._running = False
