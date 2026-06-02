"""
Bulk Order Poller — O(1) fill detection via ``kite.orders()``.

Supersedes the legacy per-order poller which called
``kite.order_history(order_id=X)`` per open order, making O(N) API calls
per 300ms cycle.  With 5 open orders that was 16.7 req/sec — over the
Zerodha 10 req/sec limit.  With 10 orders: 33.3 req/sec.

Fix: ``kite.orders()`` returns ALL orders in a single API call.  This
poller makes exactly ONE Zerodha API call per cycle regardless of how
many orders are open.  Rate cost: 2–3.3 req/sec (adaptive interval).

Adaptive interval (responds to market phase and open order count):

    Market Phase    Open Orders    Poll Interval    req/sec cost
    PRE_CLOSE       any            300ms            3.3  (MIS deadline)
    any             0              2000ms           0.5  (idle)
    any             1–2            1000ms           1.0  (light)
    any             3–5            500ms            2.0  (normal)
    any             6+             300ms            3.3  (heavy)

Idempotency (unchanged from fill_poller.py):

    fill_id = sha256("{broker_order_id}|{filled_qty}|{avg_price}")[:16]
    DynamoDB conditional write (attribute_not_exists(PK)) on FILL#{fill_id}
    → exactly-once position update even if polling + future postback both
      detect the same fill.

Design:

    Run BOTH pollers in parallel for 5 consecutive trading days:
        - BulkOrderPoller handles all state mutations
    After 5-day validation:
        - Delete fill_poller.py (or keep as archived code)

The DynamoDB idempotency gate ensures the parallel period is safe:
even if both pollers detect the same fill, only the first one through
the gate updates positions.

Kafka wiring (Phase 2 — complete):
    After the DynamoDB idempotency gate passes, ``_handle_fill`` calls
    ``KafkaOrderEventsPublisher.publish_fill()`` so the risk engine's
    ``_orders_event_loop`` updates real-time P&L.  The publisher is optional
    (defaults to None) so the poller degrades gracefully if the publisher is
    not yet initialised.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Awaitable, Callable, Optional, Set

from botocore.exceptions import ClientError

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.utils.helpers import utc_iso
from shared.zerodha.market_phase import MarketPhase, MarketPhaseGovernor
from shared.zerodha.rate_limiter import EndpointClass, Priority, ZerodhaRateLimiter

from execution_engine.brokers.zerodha_broker import ZerodhaBrokerClient
from execution_engine.orders.order import Market, OrderSide, OrderStatus, StoredOrder
from execution_engine.orders.order_manager import OrderManager
from execution_engine.publishers.kafka_order_events_publisher import KafkaOrderEventsPublisher

logger = get_logger(__name__, service_name="execution_engine")

# ── Constants ─────────────────────────────────────────────────────────────────

_FILL_TTL_SECONDS: int = 86_400  # 24h TTL on DynamoDB fill records

# Consecutive poll cycle errors before logging CRITICAL
_MAX_CONSECUTIVE_ERRORS: int = 10  # 10 × min_interval ≈ 3–20s of blindness

# Kite Connect "COMPLETE" status string → our FILLED
_KITE_COMPLETE = "COMPLETE"
_KITE_CANCELLED = "CANCELLED"
_KITE_REJECTED  = "REJECTED"


def compute_fill_id(
    broker_order_id: str,
    filled_quantity: float,
    avg_fill_price: float,
) -> str:
    """
    Deterministic fill ID — sha256(order_id|fill_qty|fill_price).

    Using the same formula means the DynamoDB idempotency gate correctly
    blocks duplicate processing even when both pollers run in parallel
    to catch fills missed by BulkOrderPoller.

    Returns:
        16-character lowercase hex string.
    """
    raw = f"{broker_order_id}|{filled_quantity}|{avg_fill_price}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── Bulk Order Poller ─────────────────────────────────────────────────────────

class BulkOrderPoller:
    """
    O(1) Zerodha fill poller using ``kite.orders()`` bulk endpoint.

    One API call per cycle covers ALL orders.  Adaptive polling interval
    based on open order count and market phase (via ``MarketPhaseGovernor``).

    Single O(1) source of fill detection for the execution engine.

    Architecture:
        - Uses ``ZerodhaRateLimiter`` (Priority.HIGH) — shares token budget
          with order placement and cancel operations.
        - DynamoDB idempotency gate: same ``FILL#{fill_id}`` schema as the
          old poller — migration is transparent to the fills table.
        - ``on_phase_change()`` is registered as a ``MarketPhaseGovernor``
          listener — polling interval adjusts automatically at phase boundaries.
    """

    def __init__(
        self,
        zerodha: ZerodhaBrokerClient,
        order_manager: OrderManager,
        dynamo_client: Any,
        fills_table: str,
        rate_limiter: ZerodhaRateLimiter,
        phase_governor: Optional[MarketPhaseGovernor] = None,
        kafka_publisher: Optional[KafkaOrderEventsPublisher] = None,
        protective_stop_callback: Optional[
            Callable[..., Awaitable[Any]]
        ] = None,
        settings: Optional[AppSettings] = None,
    ) -> None:
        """
        Initialise the bulk order poller.

        Args:
            zerodha:          Connected Zerodha broker client.
            order_manager:    Order manager with DynamoDB access.
            dynamo_client:    boto3 DynamoDB client for the fills table.
            fills_table:      DynamoDB fills-idempotency table name.
            rate_limiter:     Shared ``ZerodhaRateLimiter`` instance.
            phase_governor:   Optional ``MarketPhaseGovernor`` — registers
                              ``on_phase_change`` listener if provided.
            kafka_publisher:  Optional ``KafkaOrderEventsPublisher`` — when
                              provided, every fill detected by polling is also
                              published to ``orders.events`` so the risk engine
                              can update real-time P&L.  The DynamoDB idempotency
                              gate still runs first, so a Kafka re-delivery of the
                              same fill is silently blocked at the DynamoDB level.
            settings:         App settings (loaded from env if None).
        """
        self._zerodha          = zerodha
        self._order_manager    = order_manager
        self._dynamo           = dynamo_client
        self._fills_table      = fills_table
        self._rate_limiter     = rate_limiter
        self._kafka_publisher  = kafka_publisher
        self._protective_stop_callback = protective_stop_callback
        self._settings         = settings or get_settings()

        self._phase              = MarketPhase.POST_CLOSE
        self._running            = False
        self._consecutive_errors = 0

        # Set of broker_order_ids we are actively tracking.
        # Updated from DynamoDB at the start of each cycle.
        self._tracked_ids: Set[str] = set()

        if phase_governor is not None:
            phase_governor.add_listener(self.on_phase_change)

    # ── Phase awareness ────────────────────────────────────────────────────────

    def on_phase_change(self, phase_name: str) -> None:
        """
        Called by ``MarketPhaseGovernor`` on every phase transition.

        Updates polling interval immediately — no restart required.
        """
        try:
            self._phase = MarketPhase(phase_name)
            logger.info(
                "bulk_order_poller.phase_changed",
                phase=phase_name,
                new_interval_ms=int(self._get_poll_interval(len(self._tracked_ids)) * 1000),
            )
        except ValueError:
            logger.warning("bulk_order_poller.unknown_phase", phase_name=phase_name)

    def _get_poll_interval(self, open_order_count: int) -> float:
        """
        Adaptive polling interval based on phase and open order count.

        PRE_CLOSE always uses the fastest interval — MIS auto-square-off
        deadline at 15:15 IST requires tight monitoring.

        Returns:
            Polling interval in seconds.
        """
        if self._phase == MarketPhase.PRE_CLOSE:
            return 0.3   # 3.3 req/s — aggressive during MIS window
        if open_order_count == 0:
            return 2.0   # 0.5 req/s — idle, nothing to check
        if open_order_count <= 2:
            return 1.0   # 1.0 req/s — light activity
        if open_order_count <= 5:
            return 0.5   # 2.0 req/s — normal trading
        return 0.3       # 3.3 req/s — heavy order load

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start the bulk polling loop.

        Designed to run as an asyncio task alongside other execution engine
        coroutines.  Stops when ``stop()`` is called.
        """
        self._running = True
        logger.info(
            "bulk_order_poller.started",
            fills_table=self._fills_table,
        )
        await self._poll_loop()

    async def stop(self) -> None:
        """Signal the polling loop to stop after the current cycle completes."""
        self._running = False
        logger.info("bulk_order_poller.stopped")

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """
        Main adaptive polling loop.

        Each cycle:
          1. Record start time.
          2. Get current tracked order count (from DynamoDB GSI).
          3. Compute adaptive poll interval.
          4. Run ``_poll_cycle()`` — ONE bulk API call, process all orders.
          5. Sleep for remainder of interval window.

        On error: log and increment counter.  After 10 consecutive failures,
        log CRITICAL to fire CloudWatch alarm.  Never stop the loop on errors.
        """
        while self._running:
            open_count   = len(self._tracked_ids)
            interval     = self._get_poll_interval(open_count)
            cycle_start  = asyncio.get_event_loop().time()

            try:
                await self._poll_cycle()
                self._consecutive_errors = 0
            except Exception:
                self._consecutive_errors += 1
                logger.exception(
                    "bulk_order_poller.cycle_error",
                    consecutive_errors=self._consecutive_errors,
                    phase=self._phase.value,
                )
                if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    logger.critical(
                        "bulk_order_poller.blind_window",
                        consecutive_errors=self._consecutive_errors,
                        message=(
                            "Bulk fill detection offline.  Position state may be stale."
                        ),
                    )

            elapsed    = asyncio.get_event_loop().time() - cycle_start
            sleep_time = max(0.0, interval - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    # ── Single poll cycle — O(1) API call ─────────────────────────────────────

    async def _poll_cycle(self) -> None:
        """
        One poll cycle: fetch ALL orders in a single API call, process changes.

        Steps:
          1. Fetch open NSE orders from DynamoDB (our tracked set).
          2. One ``kite.orders()`` call — returns all today's orders.
          3. For each broker order that is in our tracked set:
               a. Check if status has changed vs what we know.
               b. If FILLED → idempotency gate → position update.
               c. If CANCELLED/REJECTED → status update only.
          4. Update ``_tracked_ids`` for next cycle's interval calculation.
        """
        # Step 1: get our open orders from DynamoDB
        open_orders = await self._order_manager.get_open_orders()
        nse_open = [
            o for o in open_orders
            if o.market == Market.NSE
            and o.broker_order_id
            and o.status in (OrderStatus.PLACED, OrderStatus.PARTIALLY_FILLED)
        ]

        self._tracked_ids = {o.broker_order_id for o in nse_open}

        if not self._tracked_ids:
            return  # Nothing to track — skip API call entirely

        # Build lookup by broker_order_id for O(1) access in step 3
        our_orders: dict[str, StoredOrder] = {
            o.broker_order_id: o for o in nse_open
        }

        # Step 2: ONE bulk API call — returns ALL broker orders today
        await self._rate_limiter.acquire(Priority.HIGH, EndpointClass.OTHER)
        all_broker_orders = await self._zerodha.get_all_orders()

        if not all_broker_orders:
            return

        # Step 3: process only orders we are tracking
        tasks = []
        for broker_order in all_broker_orders:
            bid = str(broker_order.get("order_id", ""))
            if bid not in our_orders:
                continue   # Not our order (or already terminal)

            our_order  = our_orders[bid]
            new_status = str(broker_order.get("status", "")).upper()
            new_qty    = float(broker_order.get("filled_quantity", 0))
            new_price  = float(broker_order.get("average_price", 0))
            broker_msg = str(broker_order.get("status_message", ""))

            if new_status == _KITE_COMPLETE:
                tasks.append(
                    self._handle_fill(our_order, new_qty, new_price, broker_msg)
                )
            elif (
                new_status == "OPEN PENDING" or "PARTIALLY" in new_status
            ) and new_qty > (our_order.filled_quantity or 0.0):
                tasks.append(
                    self._handle_partial(our_order, new_qty, new_price, broker_msg)
                )
            elif new_status in (_KITE_CANCELLED, _KITE_REJECTED):
                tasks.append(
                    self._handle_terminal(our_order, new_status, broker_msg, new_qty, new_price)
                )

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(
                        "bulk_order_poller.task_error",
                        error=str(result),
                    )

    # ── Fill handling ──────────────────────────────────────────────────────────

    async def _handle_fill(
        self,
        order: StoredOrder,
        filled_quantity: float,
        avg_fill_price: float,
        broker_message: str,
    ) -> None:
        """
        Process a completed fill through the DynamoDB idempotency gate.

        Idempotency gate — attribute_not_exists(PK) on the fills table so
        the parallel-run migration period is safe — the first poller through
        the gate wins, the second is silently blocked.
        """
        fill_id   = compute_fill_id(order.broker_order_id, filled_quantity, avg_fill_price)
        now_iso   = utc_iso()
        ttl_epoch = int(time.time()) + _FILL_TTL_SECONDS
        prior_qty = order.filled_quantity or 0.0
        delta_qty = max(0.0, filled_quantity - prior_qty)
        delta_price = self._calculate_delta_fill_price(
            prior_qty=prior_qty,
            prior_avg=order.avg_fill_price or 0.0,
            new_qty=filled_quantity,
            new_avg=avg_fill_price,
        )

        is_new = await self._write_fill_record(
            fill_id=fill_id,
            order=order,
            filled_quantity=filled_quantity,
            avg_fill_price=avg_fill_price,
            now_iso=now_iso,
            ttl_epoch=ttl_epoch,
        )

        if not is_new:
            logger.debug(
                "bulk_order_poller.duplicate_suppressed",
                fill_id=fill_id,
                order_id=order.order_id,
                broker_order_id=order.broker_order_id,
            )
            return

        await self._order_manager.update_order_status(
            order_id=order.order_id,
            new_status=OrderStatus.FILLED,
            filled_quantity=filled_quantity,
            average_price=avg_fill_price,
            broker_message=broker_message,
        )

        if delta_qty > 0:
            await self._place_protective_stop_for_delta(
                order=order,
                cumulative_filled=filled_quantity,
                delta_qty=delta_qty,
                fill_id=fill_id,
            )
            await self._order_manager.apply_fill_to_position(
                symbol=order.symbol,
                side=order.side,
                filled_quantity=delta_qty,
                avg_fill_price=delta_price,
                last_price=delta_price,
                order_id=order.order_id,
                signal_id=order.signal_id,
                risk_decision_id=order.risk_decision_id,
                order_type=order.order_type.value,
                market_str=order.market.value,
            )

        slippage_bps = self._compute_slippage_bps(order, avg_fill_price)

        logger.info(
            "bulk_order_poller.order_filled",
            fill_id=fill_id,
            order_id=order.order_id,
            broker_order_id=order.broker_order_id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            market=order.market.value,
            side=order.side.value,
            filled_quantity=delta_qty,
            avg_fill_price=delta_price,
            slippage_bps=slippage_bps,
            fill_source="zerodha_bulk_polling",
        )

        # Publish ORDER_FILLED to orders.events so the risk engine's
        # _orders_event_loop updates real-time P&L and position state.
        # trace_id is not stored with the order (poll-detected fills arrive
        # outside the Kafka signal chain), so we use a deterministic surrogate
        # that makes the fill traceable back to its order_id in CloudWatch.
        if self._kafka_publisher is not None:
            try:
                await self._kafka_publisher.publish_fill(
                    order_id=order.order_id,
                    signal_id=order.signal_id,
                    risk_decision_id=order.risk_decision_id,
                    trace_id=f"poller:{order.order_id}",
                    symbol=order.symbol,
                    market=order.market.value,
                    direction=order.side.value,
                    quantity_ordered=int(order.quantity),
                    quantity_filled=int(delta_qty),
                    avg_fill_price=delta_price,
                    broker_order_id=order.broker_order_id,
                    event_type="ORDER_FILLED",
                )
            except Exception:
                logger.exception(
                    "bulk_order_poller.kafka_publish_failed (order_id=%s) — "
                    "risk engine P&L state may lag until next NAV refresh",
                    order.order_id,
                )
        else:
            logger.debug(
                "bulk_order_poller.no_kafka_publisher — "
                "ORDER_FILLED not published to orders.events (order_id=%s)",
                order.order_id,
            )

    async def _handle_partial(
        self,
        order: StoredOrder,
        filled_quantity: float,
        avg_fill_price: float,
        broker_message: str,
    ) -> None:
        """Apply a partial fill increment immediately and update order status."""
        prior_qty = order.filled_quantity or 0.0
        if filled_quantity <= prior_qty:
            return
        delta_qty = filled_quantity - prior_qty
        delta_price = self._calculate_delta_fill_price(
            prior_qty=prior_qty,
            prior_avg=order.avg_fill_price or 0.0,
            new_qty=filled_quantity,
            new_avg=avg_fill_price,
        )
        fill_id = compute_fill_id(order.broker_order_id, filled_quantity, avg_fill_price)
        is_new = await self._write_fill_record(
            fill_id=fill_id,
            order=order,
            filled_quantity=filled_quantity,
            avg_fill_price=avg_fill_price,
            now_iso=utc_iso(),
            ttl_epoch=int(time.time()) + _FILL_TTL_SECONDS,
        )
        if not is_new:
            return

        await self._order_manager.update_order_status(
            order_id=order.order_id,
            new_status=OrderStatus.PARTIALLY_FILLED,
            filled_quantity=filled_quantity,
            average_price=avg_fill_price,
            broker_message=broker_message,
        )
        await self._place_protective_stop_for_delta(
            order=order,
            cumulative_filled=filled_quantity,
            delta_qty=delta_qty,
            fill_id=fill_id,
        )
        await self._order_manager.apply_fill_to_position(
            symbol=order.symbol,
            side=order.side,
            filled_quantity=delta_qty,
            avg_fill_price=delta_price,
            last_price=delta_price,
            order_id=order.order_id,
            signal_id=order.signal_id,
            risk_decision_id=order.risk_decision_id,
            order_type=order.order_type.value,
            market_str=order.market.value,
        )
        logger.info(
            "bulk_order_poller.partial_fill",
            order_id=order.order_id,
            symbol=order.symbol,
            filled_quantity=delta_qty,
            avg_fill_price=delta_price,
        )
        if self._kafka_publisher is not None:
            await self._kafka_publisher.publish_fill(
                order_id=order.order_id,
                signal_id=order.signal_id,
                risk_decision_id=order.risk_decision_id,
                trace_id=f"poller:{order.order_id}",
                symbol=order.symbol,
                market=order.market.value,
                direction=order.side.value,
                quantity_ordered=int(order.quantity),
                quantity_filled=int(delta_qty),
                avg_fill_price=delta_price,
                broker_order_id=order.broker_order_id,
                event_type="ORDER_PARTIAL",
            )

    async def _handle_terminal(
        self,
        order: StoredOrder,
        kite_status: str,
        broker_message: str,
        filled_quantity: float = 0.0,
        avg_fill_price: float = 0.0,
    ) -> None:
        """
        Update status for a cancelled or rejected order.

        For ORDER_REJECTED, also publishes to orders.events so the risk engine
        can release the reserved position capacity.  CANCELLED orders are not
        published — a cancel is operator-initiated and the position reservation
        was already released at cancel-request time.
        """
        new_status = (
            OrderStatus.CANCELLED if kite_status == _KITE_CANCELLED
            else OrderStatus.REJECTED
        )
        prior_qty = order.filled_quantity or 0.0
        if filled_quantity > prior_qty:
            delta_qty = filled_quantity - prior_qty
            delta_price = self._calculate_delta_fill_price(
                prior_qty=prior_qty,
                prior_avg=order.avg_fill_price or 0.0,
                new_qty=filled_quantity,
                new_avg=avg_fill_price,
            )
            fill_id = compute_fill_id(order.broker_order_id, filled_quantity, avg_fill_price)
            is_new = await self._write_fill_record(
                fill_id=fill_id,
                order=order,
                filled_quantity=filled_quantity,
                avg_fill_price=avg_fill_price,
                now_iso=utc_iso(),
                ttl_epoch=int(time.time()) + _FILL_TTL_SECONDS,
            )
            if is_new:
                await self._place_protective_stop_for_delta(
                    order=order,
                    cumulative_filled=filled_quantity,
                    delta_qty=delta_qty,
                    fill_id=fill_id,
                )
                await self._order_manager.apply_fill_to_position(
                    symbol=order.symbol,
                    side=order.side,
                    filled_quantity=delta_qty,
                    avg_fill_price=delta_price,
                    last_price=delta_price,
                    order_id=order.order_id,
                    signal_id=order.signal_id,
                    risk_decision_id=order.risk_decision_id,
                    order_type=order.order_type.value,
                    market_str=order.market.value,
                )
        await self._order_manager.update_order_status(
            order_id=order.order_id,
            new_status=new_status,
            filled_quantity=max(filled_quantity, order.filled_quantity or 0.0),
            average_price=avg_fill_price or order.avg_fill_price,
            broker_message=broker_message,
        )
        logger.info(
            "bulk_order_poller.order_terminal",
            order_id=order.order_id,
            symbol=order.symbol,
            terminal_status=new_status.value,
        )

        # Publish ORDER_REJECTED (not CANCELLED) to orders.events — the risk
        # engine uses rejections to release reserved position slots.
        if new_status == OrderStatus.REJECTED and self._kafka_publisher is not None:
            try:
                await self._kafka_publisher.publish_rejection(
                    order_id=order.order_id,
                    signal_id=order.signal_id,
                    risk_decision_id=order.risk_decision_id,
                    trace_id=f"poller:{order.order_id}",
                    symbol=order.symbol,
                    market=order.market.value,
                    direction=order.side.value,
                    quantity_ordered=int(order.quantity),
                    broker_order_id=order.broker_order_id,
                    reject_reason=broker_message or "broker_rejected",
                )
            except Exception:
                logger.exception(
                    "bulk_order_poller.kafka_rejection_publish_failed (order_id=%s)",
                    order.order_id,
                )

    # ── DynamoDB idempotency gate ─────────────────────────────────────────────

    async def _write_fill_record(
        self,
        fill_id: str,
        order: StoredOrder,
        filled_quantity: float,
        avg_fill_price: float,
        now_iso: str,
        ttl_epoch: int,
    ) -> bool:
        """
        Conditional DynamoDB write — returns True if new fill, False if duplicate.

        Uses ``attribute_not_exists(PK)`` so exactly one writer wins even when
        if Zerodha delivers duplicate fill events both detect the same fill
        during the parallel migration period.
        """
        if self._dynamo is None:
            logger.warning(
                "bulk_order_poller.no_dynamo_client",
                fill_id=fill_id,
                message="Fill idempotency gate bypassed — no DynamoDB client",
            )
            return True

        try:
            await asyncio.to_thread(
                self._dynamo.put_item,
                TableName=self._fills_table,
                Item={
                    "PK":              {"S": f"FILL#{fill_id}"},
                    "SK":              {"S": "META"},
                    "fill_id":         {"S": fill_id},
                    "order_id":        {"S": order.order_id},
                    "broker_order_id": {"S": order.broker_order_id},
                    "signal_id":       {"S": order.signal_id},
                    "symbol":          {"S": order.symbol},
                    "market":          {"S": order.market.value},
                    "side":            {"S": order.side.value},
                    "filled_quantity": {"N": str(filled_quantity)},
                    "avg_fill_price":  {"N": str(avg_fill_price)},
                    "fill_source":     {"S": "zerodha_bulk_polling"},
                    "created_at":      {"S": now_iso},
                    "TTL":             {"N": str(ttl_epoch)},
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
            return True

        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_slippage_bps(
        order: StoredOrder,
        avg_fill_price: float,
    ) -> Optional[float]:
        """Slippage in basis points from limit price (None if no reference)."""
        ref = order.limit_price
        if not ref or ref == 0:
            return None
        raw = avg_fill_price - ref
        if order.side == OrderSide.SELL:
            raw = -raw
        return round(raw / ref * 10_000, 3)

    @staticmethod
    def _calculate_delta_fill_price(
        *,
        prior_qty: float,
        prior_avg: float,
        new_qty: float,
        new_avg: float,
    ) -> float:
        delta_qty = new_qty - prior_qty
        if delta_qty <= 0:
            return new_avg
        return max(0.0, ((new_qty * new_avg) - (prior_qty * prior_avg)) / delta_qty)

    async def _place_protective_stop_for_delta(
        self,
        *,
        order: StoredOrder,
        cumulative_filled: float,
        delta_qty: float,
        fill_id: str,
    ) -> None:
        if self._protective_stop_callback is None or delta_qty <= 0:
            return
        if getattr(order, "is_protective", False):
            logger.debug(
                "bulk_order_poller.protective_fill_no_child_stop",
                order_id=order.order_id,
                broker_order_id=order.broker_order_id,
            )
            return
        try:
            await self._protective_stop_callback(
                parent_order=order,
                entry_response=order,
                filled_quantity=delta_qty,
                fill_id=fill_id or compute_fill_id(
                    order.broker_order_id,
                    cumulative_filled,
                    order.avg_fill_price,
                ),
            )
        except Exception:
            logger.exception(
                "bulk_order_poller.protective_stop_failed",
                order_id=order.order_id,
                broker_order_id=order.broker_order_id,
                filled_quantity=delta_qty,
            )
