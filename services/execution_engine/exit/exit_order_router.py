"""
ExitOrderRouter — routes exit orders to the correct handler by trading mode.

Key invariants enforced here:
    - Exits NEVER pass through the signal pipeline (signals.pending → signals.approved).
    - Exits NEVER increment signals_today.
    - Exits are NEVER blocked by daily strategy caps.
    - Idempotency is enforced via conditional DynamoDB write on exit_order_id.
    - Kill switch exits use an unconditional write (priority 1, overrides any in-flight exit).

Mode behaviour:
    PAPER    — synthetic fill via order_manager.apply_fill_to_position; no broker call.
    LIVE     — broker.place_order via ZerodhaBrokerClient (Phase 4 gate; disabled by default).
    BACKTEST — simulated fill, returns True with no DynamoDB side effects.
"""
from __future__ import annotations

import asyncio
import os
from enum import Enum
from typing import Any, Optional

from services.shared.logging.logger import get_logger
from services.execution_engine.exit.exit_models import ExitOrderRequest
from services.shared.monitoring import LiveCounters

logger = get_logger(__name__, service_name="execution_engine")


# ── Mode enum ─────────────────────────────────────────────────────────────────


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"
    BACKTEST = "backtest"


# ── Router ────────────────────────────────────────────────────────────────────


class ExitOrderRouter:
    """
    Mode-aware exit order dispatcher with idempotency enforcement.

    This is the single dispatch point for ALL exit orders in the system:
        - TradeExitEngine stop-loss / take-profit exits
        - MISSquareOffManager time-based exits
        - Kill switch flatten exits

    None of these paths touch the Kafka signal topics or ``signals_today``.

    Args:
        mode:                  PAPER | LIVE | BACKTEST.
        dynamo_client:         boto3 DynamoDB client.
        positions_table:       Positions DynamoDB table name.
        order_manager:         OrderManager instance (paper fill path).
        zerodha_broker:        ZerodhaBrokerClient (live path; optional).
        kafka_publisher:       KafkaOrderEventsPublisher (orders.events; Phase 3).
        live_trading_enabled:  Must be True to unlock live mode. Default False.
    """

    def __init__(
        self,
        mode: TradingMode,
        dynamo_client: Any,
        positions_table: str,
        *,
        order_manager: Optional[Any] = None,
        zerodha_broker: Optional[Any] = None,
        kafka_publisher: Optional[Any] = None,
        live_trading_enabled: bool = False,
        live_counters: Optional[LiveCounters] = None,
    ) -> None:
        self._mode = mode
        self._dynamo = dynamo_client
        self._positions_table = positions_table
        self._order_manager = order_manager
        self._zerodha = zerodha_broker
        self._kafka_publisher = kafka_publisher
        self._live_enabled = live_trading_enabled
        self._live_counters = live_counters

    @property
    def mode(self) -> TradingMode:
        return self._mode

    async def route(self, request: ExitOrderRequest) -> bool:
        """
        Route an exit order through the idempotency lock and mode handler.

        Returns:
            True  — exit placed (or simulated fill recorded).
            False — idempotent skip (exit_order_id already set on position)
                    or routing failure.

        This method NEVER:
            - Calls signals.pending or signals.approved Kafka topics.
            - Touches signals_today counter.
            - Consults the daily cap.
        """
        # Pre-gate: reject live exits before acquiring the DynamoDB lock.
        # Without this, _route_live() returning False would leave exit_order_id
        # permanently set on the position (lock poisoning), preventing all
        # subsequent exit attempts (TEE retry, MIS square-off, kill switch).
        if self._mode == TradingMode.LIVE and not self._live_enabled:
            logger.error(
                "exit_router.live_gate_blocked",
                symbol=request.symbol,
                trigger=request.trigger_type.value,
                detail=(
                    "Live exit blocked pre-lock: live_trading_enabled=False. "
                    "Set QE_EXECUTION_LIVE_TRADING_ENABLED=true to enable Phase 4."
                ),
            )
            if self._live_counters is not None:
                self._live_counters.router_live_attempts += 1
                self._live_counters.router_live_blocked += 1
            return False

        acquired = await self._acquire_exit_lock(request)
        if not acquired:
            logger.info(
                "exit_router.idempotent_skip",
                exit_id=request.exit_id,
                symbol=request.symbol,
                trigger=request.trigger_type.value,
                mode=self._mode.value,
            )
            if self._live_counters is not None:
                self._live_counters.router_idempotency_skips += 1
            return False

        if self._mode == TradingMode.PAPER:
            return await self._route_paper(request)
        if self._mode == TradingMode.LIVE:
            return await self._route_live(request)
        if self._mode == TradingMode.BACKTEST:
            return await self._route_backtest(request)

        logger.error("exit_router.unknown_mode", mode=str(self._mode))
        return False

    # ── Idempotency ───────────────────────────────────────────────────────────

    async def _acquire_exit_lock(self, request: ExitOrderRequest) -> bool:
        """
        Conditional DynamoDB write to claim the exit_order_id slot.

        Standard exit (stop-loss, take-profit, MIS):
            Condition: attribute_not_exists(exit_order_id) AND direction <> :flat
            → First caller wins. Subsequent callers see ConditionalCheckFailed → skip.

        Kill switch:
            Condition: direction <> :flat   (no exit_order_id check)
            → Unconditional override. Kill switch always wins.

        Returns True if lock acquired, False if already held or position is flat.
        """
        from shared.risk_state import position_key  # noqa: PLC0415

        key = position_key(request.symbol)
        expr_values: dict[str, Any] = {
            ":exit_id": {"S": request.exit_id},
            ":trigger": {"S": request.trigger_type.value},
            ":flat":    {"S": "FLAT"},
        }

        if request.is_kill_switch:
            condition = "direction <> :flat"
        else:
            condition = "attribute_not_exists(exit_order_id) AND direction <> :flat"

        try:
            await asyncio.to_thread(
                self._dynamo.update_item,
                TableName=self._positions_table,
                Key=key,
                UpdateExpression="SET exit_order_id = :exit_id, exit_trigger = :trigger",
                ConditionExpression=condition,
                ExpressionAttributeValues=expr_values,
            )
            if self._live_counters is not None:
                self._live_counters.router_idempotency_successes += 1
            return True
        except Exception as exc:
            if _dynamo_error_code(exc) == "ConditionalCheckFailedException":
                return False
            logger.exception(
                "exit_router.lock_error",
                symbol=request.symbol,
                exit_id=request.exit_id,
            )
            return False

    # ── Mode handlers ─────────────────────────────────────────────────────────

    async def _route_paper(self, request: ExitOrderRequest) -> bool:
        """
        Synthetic paper exit fill.

        No broker call. No ZerodhaRateLimiter tokens consumed.
        Calls order_manager.apply_fill_to_position() to update the position
        to FLAT in DynamoDB, exactly as a live fill would.
        """
        import uuid as _uuid  # noqa: PLC0415

        exit_order_id = f"PAPER-EXIT-{_uuid.uuid4().hex[:16].upper()}"
        fill_price = request.exit_price if request.exit_price is not None else request.avg_entry_price

        logger.info(
            "exit_router.paper_exit_placed",
            exit_id=request.exit_id,
            exit_order_id=exit_order_id,
            symbol=request.symbol,
            close_side=request.close_side,
            close_qty=request.close_qty,
            product_type=request.product_type,
            trigger=request.trigger_type.value,
            fill_price=fill_price,
        )

        if self._order_manager is not None:
            from services.execution_engine.orders.order import OrderSide  # noqa: PLC0415

            side = OrderSide.SELL if request.close_side == "SELL" else OrderSide.BUY
            try:
                await self._order_manager.apply_fill_to_position(
                    symbol=request.symbol,
                    side=side,
                    filled_quantity=request.close_qty,
                    avg_fill_price=fill_price,
                    last_price=fill_price,
                    order_id=exit_order_id,
                    signal_id=request.exit_id,
                    risk_decision_id="exit-auto",
                    order_type="MARKET",
                    market_str="NSE" if request.market == "NSE" else "US",
                )
            except Exception:
                logger.exception(
                    "exit_router.paper_position_update_failed",
                    symbol=request.symbol,
                    exit_order_id=exit_order_id,
                )
                if self._live_counters is not None:
                    self._live_counters.router_failed_routes += 1
                return False

        if self._live_counters is not None:
            self._live_counters.router_paper_exits += 1
            entry = request.avg_entry_price
            qty   = request.close_qty
            if request.close_side == "SELL":
                pnl = (fill_price - entry) * qty
            else:
                pnl = (entry - fill_price) * qty
            self._live_counters.realized_pnl = (self._live_counters.realized_pnl or 0.0) + pnl

        return True

    async def _route_live(self, request: ExitOrderRequest) -> bool:
        """
        Live broker exit via ZerodhaBrokerClient.

        Gate: live_trading_enabled=True checked pre-lock in route(); only reaches
        here when the flag is set AND the DynamoDB lock has been acquired.

        Timeout controlled by env var LIVE_EXIT_BROKER_TIMEOUT_S (default 15 s).
        On timeout we do NOT release the lock — we cannot know whether the broker
        received the order and a duplicate would be worse than a stuck position.
        On any other exception the lock is released so the position can be retried.
        """
        if self._live_counters is not None:
            self._live_counters.router_live_attempts += 1

        if self._zerodha is None:
            logger.error(
                "exit_router.live_no_broker",
                symbol=request.symbol,
                detail="live_trading_enabled=True but no zerodha_broker configured.",
            )
            if self._live_counters is not None:
                self._live_counters.router_live_blocked += 1
            await self._release_exit_lock(request)
            return False

        timeout_s = float(os.environ.get("LIVE_EXIT_BROKER_TIMEOUT_S", "15"))
        try:
            order_resp = await asyncio.wait_for(
                self._zerodha.place_order(
                    symbol=request.symbol,
                    side=request.close_side,
                    quantity=request.close_qty,
                    product_type=request.product_type,
                    order_type="MARKET",
                    tag=request.exit_id[:20],
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.critical(
                "exit_router.live_broker_timeout",
                symbol=request.symbol,
                exit_id=request.exit_id,
                timeout_s=timeout_s,
                detail=(
                    "Broker call timed out. Lock NOT released — order disposition unknown. "
                    "Operator must verify position state manually."
                ),
            )
            if self._live_counters is not None:
                self._live_counters.router_failed_routes += 1
            return False
        except Exception:
            logger.exception(
                "exit_router.live_broker_error",
                symbol=request.symbol,
                exit_id=request.exit_id,
                detail="Broker call failed. Releasing lock so position can be retried.",
            )
            if self._live_counters is not None:
                self._live_counters.router_failed_routes += 1
            await self._release_exit_lock(request)
            return False

        broker_order_id = getattr(order_resp, "order_id", None) or str(order_resp)
        logger.info(
            "exit_router.live_exit_placed",
            exit_id=request.exit_id,
            broker_order_id=broker_order_id,
            symbol=request.symbol,
            close_side=request.close_side,
            close_qty=request.close_qty,
            product_type=request.product_type,
            trigger=request.trigger_type.value,
        )
        if self._live_counters is not None:
            self._live_counters.router_live_exits += 1
        return True

    async def _release_exit_lock(self, request: ExitOrderRequest) -> None:
        """
        Remove exit_order_id from the position so it can be retried.

        Called only when a live broker call fails with a non-timeout exception —
        in that case we are certain the order was NOT sent, so releasing is safe.
        Never called on timeout: we cannot know if the broker received the order.
        """
        from shared.risk_state import position_key  # noqa: PLC0415

        key = position_key(request.symbol)
        try:
            await asyncio.to_thread(
                self._dynamo.update_item,
                TableName=self._positions_table,
                Key=key,
                UpdateExpression="REMOVE exit_order_id, exit_trigger",
                ConditionExpression="exit_order_id = :exit_id",
                ExpressionAttributeValues={":exit_id": {"S": request.exit_id}},
            )
            logger.info(
                "exit_router.exit_lock_released",
                symbol=request.symbol,
                exit_id=request.exit_id,
            )
        except Exception:
            logger.exception(
                "exit_router.exit_lock_release_failed",
                symbol=request.symbol,
                exit_id=request.exit_id,
            )

    async def _route_backtest(self, request: ExitOrderRequest) -> bool:
        """
        Backtest exit — simulated fill at exit_price, no DynamoDB side effects.
        Returns True so the backtest engine can record the fill.
        """
        fill_price = request.exit_price if request.exit_price is not None else request.avg_entry_price

        logger.info(
            "exit_router.backtest_exit_simulated",
            exit_id=request.exit_id,
            symbol=request.symbol,
            close_side=request.close_side,
            close_qty=request.close_qty,
            trigger=request.trigger_type.value,
            fill_price=fill_price,
        )
        if self._live_counters is not None:
            self._live_counters.router_backtest_exits += 1
        return True


# ── Helpers ───────────────────────────────────────────────────────────────────


def _dynamo_error_code(exc: Exception) -> str:
    """Extract DynamoDB error code from a botocore ClientError (or any exception)."""
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")
