"""
Order Manager — idempotent order submission and lifecycle tracking.

Ensures that duplicate signals do not produce duplicate orders by using
DynamoDB conditional writes with the order_id as the deduplication key.
Tracks partial fills, slippage, and order state transitions.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from botocore.exceptions import ClientError

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.utils.helpers import utc_iso

from execution_engine.orders.order import (
    Market,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderStatus,
    OrderStatusUpdate,
    OrderType,
    StoredOrder,
)

# Re-export for callers that import OrderResponse from this module
__all__ = ["OrderManager"]

logger = get_logger(__name__, service_name="execution_engine")

# Valid state transitions to prevent illegal status changes
_VALID_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {OrderStatus.PLACED, OrderStatus.REJECTED},
    OrderStatus.PLACED: {
        OrderStatus.FILLED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
    },
    # Terminal states — no further transitions
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
}


def _unmarshal_item(item: dict) -> StoredOrder:
    """
    Convert a raw DynamoDB item dict (DynamoDB JSON format) into a typed
    ``StoredOrder``.

    DynamoDB returns attribute values as typed wrappers, e.g.
    ``{"S": "PLACED"}``, ``{"N": "100"}``.  This helper extracts the
    scalar values and constructs a pydantic model that the service layer
    can work with using normal attribute access.

    Returns a ``StoredOrder`` (which extends ``OrderResponse``) so that
    callers — particularly the startup reconciliation path — have access to
    the original order parameters (``signal_id``, ``risk_decision_id``,
    ``side``, ``order_type``, ``quantity``, ``limit_price``, ``stop_price``)
    that are required to retry a stranded PENDING order after a process
    crash.

    Args:
        item: Raw DynamoDB item as returned by ``get_item`` / ``query``.

    Returns:
        ``StoredOrder`` populated from all stored DynamoDB fields.
    """

    def _s(key: str, default: str = "") -> str:
        return item.get(key, {}).get("S", default)

    def _f(key: str, default: float = 0.0) -> float:
        raw = item.get(key, {}).get("N")
        return float(raw) if raw is not None else default

    def _f_opt(key: str) -> Optional[float]:
        raw = item.get(key, {}).get("N")
        return float(raw) if raw is not None else None

    return StoredOrder(
        # ── Broker-response fields ────────────────────────────────────────────
        order_id=_s("order_id"),
        broker_order_id=_s("broker_order_id"),
        status=OrderStatus(_s("order_status", OrderStatus.PENDING.value)),
        symbol=_s("symbol"),
        market=Market(_s("market", Market.NSE.value)),
        filled_quantity=_f("filled_quantity"),
        avg_fill_price=_f("avg_fill_price"),
        broker_message=_s("broker_message"),
        # ── Original order parameters (needed for PENDING retry) ─────────────
        signal_id=_s("signal_id"),
        risk_decision_id=_s("risk_decision_id"),
        side=OrderSide(_s("side", OrderSide.BUY.value)),
        order_type=OrderType(_s("order_type", OrderType.MARKET.value)),
        quantity=_f("quantity"),
        limit_price=_f_opt("limit_price"),
        stop_price=_f_opt("stop_price"),
    )


class OrderManager:
    """
    Manages the full order lifecycle with idempotency guarantees.

    Key responsibilities:
        - Idempotent order submission (DynamoDB conditional writes).
        - State transition validation (no illegal status jumps).
        - Partial fill tracking with running totals.
        - Slippage calculation (fill price vs. signal price).

    All state is persisted in DynamoDB so the service can restart at any
    time without losing order state or producing duplicates.
    """

    def __init__(
        self,
        dynamo_client: Any = None,
        orders_table: Optional[str] = None,
        settings: Optional[AppSettings] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._dynamo = dynamo_client
        self._orders_table = orders_table or self._settings.aws.dynamodb_table_orders

    async def submit_order(self, order: OrderRequest) -> bool:
        """
        Atomically record a new order and reserve its signal_id in DynamoDB.

        Uses ``transact_write_items`` to write two items in one atomic operation:

        1. **Order record** — ``PK=ORDER#{order_id}``, ``SK=META``.
           Condition: ``attribute_not_exists(PK)``.

        2. **Signal reservation** — ``PK=SIGNAL#{signal_id}``, ``SK=LOCK``.
           Condition: ``attribute_not_exists(PK)``.

        Uniqueness is enforced on **both** the order_id *and* the signal_id.
        Because signal_id appears in the reservation condition, two concurrent
        consumers that each generate a fresh ``order_id`` for the same signal
        will race on the ``SIGNAL#{signal_id}`` item — exactly one succeeds
        and the other gets ``TransactionCanceledException``.  This closes the
        window where two concurrent workers could both pass the pre-check GSI
        query and each place a broker order.

        Args:
            order: The order to record.

        Returns:
            True  — both items written (this consumer owns the signal).
            False — signal_id or order_id already exists (concurrent duplicate).
        """
        if self._dynamo is None:
            logger.warning("No DynamoDB client — order %s not persisted", order.order_id)
            return True

        item: dict[str, Any] = {
            "PK": {"S": f"ORDER#{order.order_id}"},
            "SK": {"S": "META"},
            "order_id": {"S": order.order_id},
            "signal_id": {"S": order.signal_id},
            "risk_decision_id": {"S": order.risk_decision_id},
            "symbol": {"S": order.symbol},
            "market": {"S": order.market.value},
            "side": {"S": order.side.value},
            "order_type": {"S": order.order_type.value},
            "quantity": {"N": str(order.quantity)},
            "order_status": {"S": OrderStatus.PENDING.value},
            "filled_quantity": {"N": "0"},
            "avg_fill_price": {"N": "0"},
            "slippage": {"N": "0"},
            "created_at": {"S": order.created_at.isoformat()},
            "updated_at": {"S": utc_iso()},
            "trade_date": {"S": order.created_at.strftime("%Y-%m-%d")},
            "metadata": {"S": json.dumps(order.metadata)},
        }
        if order.limit_price is not None:
            item["limit_price"] = {"N": str(order.limit_price)}
        if order.stop_price is not None:
            item["stop_price"] = {"N": str(order.stop_price)}

        # Signal reservation — keyed on signal_id so concurrent consumers
        # race on this item, not on the order item.
        #
        # IMPORTANT: do NOT include a "signal_id" attribute here.
        # The signal-index GSI uses signal_id as its hash key.  Any item that
        # carries a signal_id attribute is projected into that index.  If we
        # stored signal_id on the lock row it would appear in the index
        # alongside the real order row, making get_order_by_signal()
        # nondeterministic (whichever row DynamoDB returns first wins).
        # The lock's purpose is purely to hold a reservation slot via the
        # attribute_not_exists(PK) condition — it does not need to be
        # queryable by signal_id.
        signal_lock: dict[str, Any] = {
            "PK": {"S": f"SIGNAL#{order.signal_id}"},
            "SK": {"S": "LOCK"},
            "order_id": {"S": order.order_id},
            "created_at": {"S": utc_iso()},
        }

        try:
            await asyncio.to_thread(
                self._dynamo.transact_write_items,
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._orders_table,
                            "Item": item,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._orders_table,
                            "Item": signal_lock,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ],
            )
            logger.info(
                "Order %s recorded in DynamoDB (signal_id=%s)",
                order.order_id,
                order.signal_id,
            )
            return True

        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "TransactionCanceledException":
                reasons = exc.response.get("CancellationReasons", [])
                failed = [r.get("Code", "None") for r in reasons]
                logger.warning(
                    "submit_order transaction cancelled for order_id=%s signal_id=%s "
                    "— cancellation reasons: %s (concurrent duplicate suppressed)",
                    order.order_id,
                    order.signal_id,
                    failed,
                )
                return False
            logger.exception("Failed to record order %s in DynamoDB", order.order_id)
            raise

        except Exception:
            logger.exception("Failed to record order %s in DynamoDB", order.order_id)
            raise

    async def update_status(self, update: OrderStatusUpdate) -> bool:
        """
        Update an order's status in DynamoDB with transition validation.

        Uses a conditional write to ensure the status transition is valid
        (e.g., PENDING -> PLACED is allowed, FILLED -> PENDING is not).

        Args:
            update: The status update to apply.

        Returns:
            True if the update was applied, False if the transition was invalid.
        """
        # Validate the transition
        valid_next = _VALID_TRANSITIONS.get(update.previous_status, set())
        if update.new_status not in valid_next:
            logger.error(
                "Invalid status transition for order %s: %s -> %s",
                update.order_id,
                update.previous_status.value,
                update.new_status.value,
            )
            return False

        if self._dynamo is None:
            logger.warning("No DynamoDB client — status update not persisted")
            return True

        try:
            update_expr = (
                "SET order_status = :new_status, "
                "filled_quantity = :filled_qty, "
                "avg_fill_price = :avg_price, "
                "slippage = :slippage, "
                "broker_order_id = :broker_id, "
                "broker_message = :broker_msg, "
                "updated_at = :updated_at"
            )

            await asyncio.to_thread(
                self._dynamo.update_item,
                TableName=self._orders_table,
                Key={
                    "PK": {"S": f"ORDER#{update.order_id}"},
                    "SK": {"S": "META"},
                },
                UpdateExpression=update_expr,
                ConditionExpression="order_status = :prev_status",
                ExpressionAttributeValues={
                    ":new_status": {"S": update.new_status.value},
                    ":prev_status": {"S": update.previous_status.value},
                    ":filled_qty": {"N": str(update.filled_quantity)},
                    ":avg_price": {"N": str(update.avg_fill_price)},
                    ":slippage": {"N": str(update.slippage)},
                    ":broker_id": {"S": update.broker_order_id},
                    ":broker_msg": {"S": update.broker_message},
                    ":updated_at": {"S": utc_iso()},
                },
            )

            logger.info(
                "Order %s status updated: %s -> %s (filled=%s avg_price=%s)",
                update.order_id,
                update.previous_status.value,
                update.new_status.value,
                update.filled_quantity,
                update.avg_fill_price,
            )
            return True

        except Exception:
            logger.exception(
                "Failed to update status for order %s", update.order_id
            )
            return False

    async def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        """
        Retrieve an order record from DynamoDB.

        Args:
            order_id: The internal order ID.

        Returns:
            Order item dictionary, or None if not found.
        """
        if self._dynamo is None:
            return None

        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._orders_table,
                Key={
                    "PK": {"S": f"ORDER#{order_id}"},
                    "SK": {"S": "META"},
                },
            )
            return response.get("Item")

        except Exception:
            logger.exception("Failed to retrieve order %s", order_id)
            return None

    async def record_order(self, response: OrderResponse) -> None:
        """
        Persist a broker ``OrderResponse`` to DynamoDB after placement.

        Called immediately after the broker confirms the order so that the
        broker-assigned order ID is stored and the status reflects PLACED.

        Args:
            response: OrderResponse returned by the broker's place_order().
        """
        if self._dynamo is None:
            logger.warning(
                "No DynamoDB client — order response %s not persisted",
                response.order_id,
            )
            return

        try:
            await asyncio.to_thread(
                self._dynamo.update_item,
                TableName=self._orders_table,
                Key={
                    "PK": {"S": f"ORDER#{response.order_id}"},
                    "SK": {"S": "META"},
                },
                UpdateExpression=(
                    "SET order_status = :status, "
                    "broker_order_id = :broker_id, "
                    "broker_message = :msg, "
                    "updated_at = :ts"
                ),
                ExpressionAttributeValues={
                    ":status": {"S": response.status.value},
                    ":broker_id": {"S": response.broker_order_id},
                    ":msg": {"S": response.broker_message},
                    ":ts": {"S": utc_iso()},
                },
            )
            logger.debug(
                "Order %s recorded with broker_id=%s",
                response.order_id,
                response.broker_order_id,
            )
        except Exception:
            logger.exception(
                "Failed to record order response for %s", response.order_id
            )
            raise

    async def get_order_by_signal(
        self, signal_id: str
    ) -> Optional[StoredOrder]:
        """
        Look up an order by the originating signal ID.

        Uses the ``signal-index`` GSI (hash key: ``signal_id``) so the lookup
        is a targeted index read rather than a full-table scan.  Expects at
        most one matching ORDER row because signal IDs are unique per the
        deduplication contract enforced by ``submit_order``.

        Why no ``Limit=1``:
            DynamoDB applies ``Limit`` to the number of items *evaluated*
            before the ``FilterExpression`` is checked — not to the number
            returned.  If the first item evaluated is a legacy signal-lock
            row (``SK=LOCK``), DynamoDB discards it via the filter, returns
            zero items, and stops — even though the real order row
            (``SK=META``) is also in the index for this signal_id.  Omitting
            ``Limit`` lets DynamoDB evaluate all projected rows for the hash
            key and return only the ``SK=META`` order row.

        Why FilterExpression on SK:
            Lock rows written before the ``signal_id`` attribute was removed
            from ``signal_lock`` may still be projected in ``signal-index``.
            Filtering on ``SK = 'META'`` guarantees we only unmarshal the
            real order record, never a lock row.

        Args:
            signal_id: The ``signal_id`` field on the order.

        Returns:
            Typed ``StoredOrder`` if an order row exists, None otherwise.
        """
        if self._dynamo is None:
            return None

        try:
            response = await asyncio.to_thread(
                self._dynamo.query,
                TableName=self._orders_table,
                IndexName="signal-index",
                KeyConditionExpression="signal_id = :sid",
                # Accept only the real order row — reject any legacy lock rows
                # (SK=LOCK) that may still be projected into the index.
                FilterExpression="SK = :meta",
                ExpressionAttributeValues={
                    ":sid": {"S": signal_id},
                    ":meta": {"S": "META"},
                },
            )
            items = response.get("Items", [])
            if not items:
                return None
            if len(items) > 1:
                # Should never happen — signal_id is unique per the transact
                # write contract.  Log and use the first item so the caller
                # still gets a deterministic result.
                logger.error(
                    "get_order_by_signal found %d rows for signal_id=%s — "
                    "expected exactly one; using first",
                    len(items),
                    signal_id,
                )
            return _unmarshal_item(items[0])
        except Exception:
            logger.exception(
                "Failed to look up order by signal_id=%s", signal_id
            )
            return None

    async def update_order_status(
        self,
        order_id: str,
        new_status: OrderStatus,
        filled_quantity: float = 0.0,
        average_price: float = 0.0,
        broker_message: str = "",
    ) -> bool:
        """
        Update order status fields in DynamoDB.

        Convenience wrapper used by the reconciliation path and the order
        update stream handler. Does not enforce transition validation —
        callers are responsible for passing valid state transitions.

        Args:
            order_id: Internal order ID.
            new_status: New ``OrderStatus`` to set.
            filled_quantity: Total quantity filled so far.
            average_price: Average fill price.
            broker_message: Optional message from the broker.

        Returns:
            True on success, False on failure.
        """
        if self._dynamo is None:
            return True

        try:
            await asyncio.to_thread(
                self._dynamo.update_item,
                TableName=self._orders_table,
                Key={
                    "PK": {"S": f"ORDER#{order_id}"},
                    "SK": {"S": "META"},
                },
                UpdateExpression=(
                    "SET order_status = :status, "
                    "filled_quantity = :filled, "
                    "avg_fill_price = :avg_price, "
                    "broker_message = :msg, "
                    "updated_at = :ts"
                ),
                ExpressionAttributeValues={
                    ":status": {"S": new_status.value},
                    ":filled": {"N": str(filled_quantity)},
                    ":avg_price": {"N": str(average_price)},
                    ":msg": {"S": broker_message},
                    ":ts": {"S": utc_iso()},
                },
            )
            logger.info(
                "Order %s status updated → %s (filled=%.2f avg_price=%.4f)",
                order_id,
                new_status.value,
                filled_quantity,
                average_price,
            )
            return True
        except Exception:
            logger.exception("Failed to update status for order %s", order_id)
            return False

    async def get_open_orders(self) -> list[StoredOrder]:
        """
        Retrieve all orders that are not yet in a terminal state.

        Terminal states: FILLED, CANCELLED, REJECTED.
        Open states:     PENDING, PLACED, PARTIALLY_FILLED.

        Uses the ``status-index`` GSI (hash key: ``order_status``) so each
        status is a targeted index query rather than a full-table scan.
        Runs three parallel queries — one per open status — and merges the
        results.  This is significantly cheaper at scale than a scan with an
        OR filter expression.

        Returns:
            List of typed ``OrderResponse`` objects for all open orders.
        """
        if self._dynamo is None:
            return []

        _OPEN_STATUSES = [
            OrderStatus.PENDING.value,
            OrderStatus.PLACED.value,
            OrderStatus.PARTIALLY_FILLED.value,
        ]

        async def _query_status(status_val: str) -> list[dict[str, Any]]:
            """Query the status-index GSI for a single status value."""
            collected: list[dict[str, Any]] = []
            exclusive_start_key: Optional[dict] = None

            # Paginate in case there are many open orders in one status bucket
            while True:
                kwargs: dict[str, Any] = {
                    "TableName": self._orders_table,
                    "IndexName": "status-index",
                    "KeyConditionExpression": "order_status = :s",
                    "ExpressionAttributeValues": {":s": {"S": status_val}},
                }
                if exclusive_start_key:
                    kwargs["ExclusiveStartKey"] = exclusive_start_key

                response = await asyncio.to_thread(self._dynamo.query, **kwargs)
                collected.extend(response.get("Items", []))

                exclusive_start_key = response.get("LastEvaluatedKey")
                if not exclusive_start_key:
                    break

            return collected

        try:
            # Run all three queries concurrently
            results = await asyncio.gather(
                *[_query_status(s) for s in _OPEN_STATUSES],
                return_exceptions=True,
            )

            raw_items: list[dict[str, Any]] = []
            for r in results:
                if isinstance(r, Exception):
                    logger.error("Error querying open orders by status: %s", r)
                else:
                    raw_items.extend(r)

            orders = [_unmarshal_item(item) for item in raw_items]
            logger.info("Found %d open orders during reconciliation", len(orders))
            return orders

        except Exception:
            logger.exception("Failed to fetch open orders from DynamoDB")
            return []

    async def wait_for_inflight_orders(
        self, timeout_seconds: float = 30.0
    ) -> None:
        """
        Block until all non-terminal orders reach a terminal state or timeout.

        Called during graceful shutdown so the service doesn't exit while
        orders are still settling. Polls DynamoDB every 2 seconds.

        Args:
            timeout_seconds: Maximum time to wait before returning regardless.
        """
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            open_orders = await self.get_open_orders()
            if not open_orders:
                logger.info("All in-flight orders have settled")
                return
            logger.info(
                "Waiting for %d in-flight order(s) to settle "
                "(%.0fs remaining)…",
                len(open_orders),
                deadline - asyncio.get_event_loop().time(),
            )
            await asyncio.sleep(2.0)

        open_orders = await self.get_open_orders()
        if open_orders:
            logger.warning(
                "Shutdown timeout reached with %d order(s) still open — "
                "proceeding with shutdown",
                len(open_orders),
            )

    def calculate_slippage(
        self, signal_price: float, fill_price: float, side: str
    ) -> float:
        """
        Calculate slippage between the signal price and the actual fill price.

        Positive slippage means the fill was worse than expected.
        Negative slippage (price improvement) is possible in favorable markets.

        Args:
            signal_price: Price at the time the signal was generated.
            fill_price: Actual average fill price from the broker.
            side: Order side ('BUY' or 'SELL').

        Returns:
            Slippage value (positive = unfavorable for the trader).
        """
        if signal_price == 0:
            return 0.0
        return fill_price - signal_price if side == "BUY" else signal_price - fill_price
