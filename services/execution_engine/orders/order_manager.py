"""
Order Manager — idempotent order submission and lifecycle tracking.

Ensures that duplicate signals do not produce duplicate orders by using
DynamoDB conditional writes with the order_id as the deduplication key.
Tracks partial fills, slippage, and order state transitions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any, Optional

from botocore.exceptions import ClientError

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.risk_state import nav_key, position_key
from shared.utils.helpers import utc_iso

from execution_engine.orders.order import (
    Market,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderStatus,
    OrderStatusUpdate,
    OrderType,
    ProductType,
    StoredOrder,
)

# Re-export for callers that import OrderResponse from this module
__all__ = ["OrderManager"]

logger = get_logger(__name__, service_name="execution_engine")

# Valid state transitions to prevent illegal status changes
_VALID_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {
        OrderStatus.ACK_UNKNOWN,
        OrderStatus.PLACED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    },
    OrderStatus.ACK_UNKNOWN: {
        OrderStatus.PLACED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    },
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


def _is_valid_status_transition(
    previous_status: OrderStatus,
    new_status: OrderStatus,
) -> bool:
    """Return True when an order lifecycle transition is legal or idempotent."""
    if previous_status == new_status:
        return True
    return new_status in _VALID_TRANSITIONS.get(previous_status, set())


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

    def _b(key: str, default: bool = False) -> bool:
        return bool(item.get(key, {}).get("BOOL", default))

    def _metadata() -> dict[str, Any]:
        raw = item.get("metadata", {}).get("S", "{}")
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

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
        product_type=ProductType(_s("product_type", ProductType.DAY.value)),
        quantity=_f("quantity"),
        limit_price=_f_opt("limit_price"),
        stop_price=_f_opt("stop_price"),
        parent_order_id=_s("parent_order_id"),
        protective_type=_s("protective_type"),
        is_protective=_b("is_protective"),
        protective_stop_order_id=_s("protective_stop_order_id"),
        broker_idempotency_key=_s("broker_idempotency_key"),
        metadata=_metadata(),
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
        positions_table: Optional[str] = None,
        risk_state_table: Optional[str] = None,
        settings: Optional[AppSettings] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._dynamo = dynamo_client
        self._orders_table = orders_table or self._settings.aws.dynamodb_table_orders
        self._positions_table = positions_table or self._settings.aws.dynamodb_table_positions
        # risk_state_table: used to write NAV updates after each fill so the
        # risk engine can keep its portfolio_value current (no static NAV).
        self._risk_state_table = (
            risk_state_table or self._settings.aws.dynamodb_table_risk_state
        )

    @staticmethod
    def broker_idempotency_key(order_id: str) -> str:
        """
        Return a broker-safe idempotency key for the internal order ID.

        Zerodha exposes only a 20-character ``tag`` field, while Alpaca accepts
        a longer client_order_id. Using a deterministic hash keeps both brokers
        on the same replay-safe key without leaking timestamp-heavy order IDs
        into a truncated tag.
        """
        return "QE" + hashlib.sha256(order_id.encode("utf-8")).hexdigest()[:18]

    @classmethod
    def attach_broker_idempotency(cls, order: OrderRequest) -> OrderRequest:
        """Ensure an OrderRequest carries the broker idempotency key in metadata."""
        if not order.metadata.get("broker_idempotency_key"):
            order.metadata["broker_idempotency_key"] = cls.broker_idempotency_key(
                order.order_id
            )
        return order

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

        self.attach_broker_idempotency(order)

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
            "product_type": {"S": order.product_type.value},
            "quantity": {"N": str(order.quantity)},
            "order_status": {"S": OrderStatus.PENDING.value},
            "filled_quantity": {"N": "0"},
            "avg_fill_price": {"N": "0"},
            "slippage": {"N": "0"},
            "created_at": {"S": order.created_at.isoformat()},
            "updated_at": {"S": utc_iso()},
            "trade_date": {"S": order.created_at.strftime("%Y-%m-%d")},
            "metadata": {"S": json.dumps(order.metadata)},
            "broker_idempotency_key": {"S": order.metadata["broker_idempotency_key"]},
            "broker_order_state": {"S": "RESERVED"},
            "outbox_status": {"S": "BROKER_RESERVED"},
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

    async def submit_child_order(
        self,
        order: OrderRequest,
        *,
        parent_order_id: str,
        protective_type: str,
    ) -> bool:
        """
        Persist a protective child order without reserving the parent signal_id.

        Protective stops are independent broker orders created after an entry
        fill. They must not consume the parent signal reservation or pollute the
        parent signal lookup, so callers pass a synthetic child signal_id while
        the parent linkage is stored explicitly.
        """
        if self._dynamo is None:
            logger.warning("No DynamoDB client - child order %s not persisted", order.order_id)
            return True

        self.attach_broker_idempotency(order)

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
            "product_type": {"S": order.product_type.value},
            "quantity": {"N": str(order.quantity)},
            "order_status": {"S": OrderStatus.PENDING.value},
            "filled_quantity": {"N": "0"},
            "avg_fill_price": {"N": "0"},
            "slippage": {"N": "0"},
            "created_at": {"S": order.created_at.isoformat()},
            "updated_at": {"S": utc_iso()},
            "trade_date": {"S": order.created_at.strftime("%Y-%m-%d")},
            "metadata": {"S": json.dumps(order.metadata)},
            "parent_order_id": {"S": parent_order_id},
            "protective_type": {"S": protective_type},
            "is_protective": {"BOOL": True},
            "broker_idempotency_key": {"S": order.metadata["broker_idempotency_key"]},
            "broker_order_state": {"S": "RESERVED"},
            "outbox_status": {"S": "BROKER_RESERVED"},
        }
        if order.limit_price is not None:
            item["limit_price"] = {"N": str(order.limit_price)}
        if order.stop_price is not None:
            item["stop_price"] = {"N": str(order.stop_price)}

        try:
            await asyncio.to_thread(
                self._dynamo.put_item,
                TableName=self._orders_table,
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.info(
                    "protective child order already exists parent=%s child=%s",
                    parent_order_id,
                    order.order_id,
                )
                return False
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
        return await self._transition_order_status(
            order_id=update.order_id,
            new_status=update.new_status,
            expected_previous_status=update.previous_status,
            filled_quantity=update.filled_quantity,
            average_price=update.avg_fill_price,
            broker_message=update.broker_message,
            broker_order_id=update.broker_order_id,
            slippage=update.slippage,
        )

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

        updated = await self._transition_order_status(
            order_id=response.order_id,
            new_status=response.status,
            filled_quantity=response.filled_quantity,
            average_price=response.avg_fill_price,
            broker_message=response.broker_message,
            broker_order_id=response.broker_order_id,
            broker_order_state="SUBMITTED",
            outbox_status="BROKER_ACKED",
            broker_submitted=True,
        )
        if not updated:
            raise RuntimeError(
                f"Illegal broker placement transition for order {response.order_id} "
                f"to {response.status.value}"
            )
        logger.debug(
            "Order %s recorded with broker_id=%s",
            response.order_id,
            response.broker_order_id,
        )

    async def mark_order_ack_unknown(
        self,
        *,
        order_id: str,
        broker_message: str,
    ) -> bool:
        """
        Persist that broker placement outcome is ambiguous.

        ``ACK_UNKNOWN`` is intentionally non-terminal and non-retryable by the
        hot path.  A later broker tag scan may advance it to PLACED/FILLED, but
        the execution service must not blindly place a second order while this
        status is present.
        """
        return await self._transition_order_status(
            order_id=order_id,
            new_status=OrderStatus.ACK_UNKNOWN,
            broker_message=broker_message,
            broker_order_state="ACK_UNKNOWN",
            outbox_status="BROKER_ACK_UNKNOWN",
        )

    async def mark_protection_failed(
        self,
        *,
        parent_order_id: str,
        reason: str,
        flatten_order_id: str = "",
        flatten_broker_order_id: str = "",
    ) -> None:
        """Mark an entry order whose protective child could not be confirmed."""
        if self._dynamo is None:
            logger.warning(
                "No DynamoDB client - protection failure for %s not persisted",
                parent_order_id,
            )
            return

        now = utc_iso()
        update_parts = [
            "protection_status = :protection_status",
            "protection_failed_at = :protection_failed_at",
            "protection_failure_reason = :protection_failure_reason",
            "updated_at = :updated_at",
        ]
        expr_values: dict[str, Any] = {
            ":protection_status": {"S": "PROTECTION_FAILED"},
            ":protection_failed_at": {"S": now},
            ":protection_failure_reason": {"S": reason[:1000]},
            ":updated_at": {"S": now},
        }
        if flatten_order_id:
            update_parts.append("emergency_flatten_order_id = :flatten_order_id")
            expr_values[":flatten_order_id"] = {"S": flatten_order_id}
        if flatten_broker_order_id:
            update_parts.append("emergency_flatten_broker_order_id = :flatten_broker_order_id")
            expr_values[":flatten_broker_order_id"] = {"S": flatten_broker_order_id}

        await asyncio.to_thread(
            self._dynamo.update_item,
            TableName=self._orders_table,
            Key={
                "PK": {"S": f"ORDER#{parent_order_id}"},
                "SK": {"S": "META"},
            },
            UpdateExpression="SET " + ", ".join(update_parts),
            ExpressionAttributeValues=expr_values,
        )

    async def get_stored_order(self, order_id: str) -> Optional[StoredOrder]:
        """Retrieve an order record and unmarshal it into a StoredOrder."""
        item = await self.get_order(order_id)
        if not item:
            return None
        return _unmarshal_item(item)

    async def link_protective_child(
        self,
        *,
        parent_order_id: str,
        child_order_id: str,
        child_broker_order_id: str,
        protective_type: str,
        protected_quantity: float,
    ) -> None:
        """Record the parent -> protective child relationship on the parent row."""
        if self._dynamo is None:
            return
        await asyncio.to_thread(
            self._dynamo.update_item,
            TableName=self._orders_table,
            Key={
                "PK": {"S": f"ORDER#{parent_order_id}"},
                "SK": {"S": "META"},
            },
            UpdateExpression=(
                "SET protective_stop_order_id = :child, "
                "protective_stop_broker_order_id = :broker_child, "
                "protective_type = :ptype, "
                "protected_quantity = if_not_exists(protected_quantity, :zero) + :qty, "
                "updated_at = :ts"
            ),
            ExpressionAttributeValues={
                ":child": {"S": child_order_id},
                ":broker_child": {"S": child_broker_order_id},
                ":ptype": {"S": protective_type},
                ":qty": {"N": str(protected_quantity)},
                ":zero": {"N": "0"},
                ":ts": {"S": utc_iso()},
            },
        )

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

        Convenience wrapper used by reconciliation, fill polling, and local
        kill-switch cancellation paths.  The current DynamoDB state is read and
        the transition is centrally validated before the conditional write.

        Args:
            order_id: Internal order ID.
            new_status: New ``OrderStatus`` to set.
            filled_quantity: Total quantity filled so far.
            average_price: Average fill price.
            broker_message: Optional message from the broker.

        Returns:
            True on success, False on failure.
        """
        return await self._transition_order_status(
            order_id=order_id,
            new_status=new_status,
            filled_quantity=filled_quantity,
            average_price=average_price,
            broker_message=broker_message,
        )

    async def _transition_order_status(
        self,
        *,
        order_id: str,
        new_status: OrderStatus,
        filled_quantity: float = 0.0,
        average_price: float = 0.0,
        broker_message: str = "",
        expected_previous_status: Optional[OrderStatus] = None,
        broker_order_id: Optional[str] = None,
        slippage: float = 0.0,
        broker_order_state: Optional[str] = None,
        outbox_status: Optional[str] = None,
        broker_submitted: bool = False,
    ) -> bool:
        """
        Centrally enforce order lifecycle transitions and persist them atomically.

        The update is conditional on the previous status observed by this method
        so a stale poller, replay, or concurrent cancel cannot move a terminal
        order backwards.  Repeating the same status is allowed as an idempotent
        metadata refresh.
        """
        previous_status = expected_previous_status
        if self._dynamo is not None and previous_status is None:
            item = await self.get_order(order_id)
            if not item:
                logger.error(
                    "order_status_transition.order_missing",
                    order_id=order_id,
                    new_status=new_status.value,
                )
                return False
            try:
                previous_status = OrderStatus(
                    item.get("order_status", {}).get("S", OrderStatus.PENDING.value)
                )
            except ValueError:
                logger.error(
                    "order_status_transition.unknown_previous_status",
                    order_id=order_id,
                    raw_status=item.get("order_status", {}).get("S"),
                    new_status=new_status.value,
                )
                return False

        previous_status = previous_status or OrderStatus.PENDING
        if not _is_valid_status_transition(previous_status, new_status):
            logger.error(
                "order_status_transition.invalid",
                order_id=order_id,
                previous_status=previous_status.value,
                new_status=new_status.value,
            )
            return False

        if self._dynamo is None:
            logger.warning("No DynamoDB client - status transition not persisted")
            return True

        now = utc_iso()
        update_parts = [
            "order_status = :status",
            "filled_quantity = :filled_qty",
            "avg_fill_price = :avg_price",
            "slippage = :slippage",
            "broker_message = :broker_msg",
            "updated_at = :updated_at",
        ]
        expr_values: dict[str, Any] = {
            ":status": {"S": new_status.value},
            ":prev_status": {"S": previous_status.value},
            ":filled_qty": {"N": str(filled_quantity)},
            ":avg_price": {"N": str(average_price)},
            ":slippage": {"N": str(slippage)},
            ":broker_msg": {"S": broker_message},
            ":updated_at": {"S": now},
        }
        if broker_order_id is not None:
            update_parts.append("broker_order_id = :broker_id")
            expr_values[":broker_id"] = {"S": broker_order_id}
        if broker_order_state is not None:
            update_parts.append("broker_order_state = :broker_state")
            expr_values[":broker_state"] = {"S": broker_order_state}
        if outbox_status is not None:
            update_parts.append("outbox_status = :outbox_status")
            expr_values[":outbox_status"] = {"S": outbox_status}
        if broker_submitted:
            update_parts.append("broker_submitted_at = :broker_submitted_at")
            expr_values[":broker_submitted_at"] = {"S": now}

        try:
            await asyncio.to_thread(
                self._dynamo.update_item,
                TableName=self._orders_table,
                Key={
                    "PK": {"S": f"ORDER#{order_id}"},
                    "SK": {"S": "META"},
                },
                UpdateExpression="SET " + ", ".join(update_parts),
                ConditionExpression="order_status = :prev_status",
                ExpressionAttributeValues=expr_values,
            )
            logger.info(
                "order_status_transition.applied",
                order_id=order_id,
                previous_status=previous_status.value,
                new_status=new_status.value,
                filled_quantity=filled_quantity,
                avg_fill_price=average_price,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                current = await self.get_stored_order(order_id)
                if current is not None and current.status == new_status:
                    logger.info(
                        "order_status_transition.idempotent_replay",
                        order_id=order_id,
                        status=new_status.value,
                    )
                    return True
                logger.error(
                    "order_status_transition.concurrent_update_blocked",
                    order_id=order_id,
                    expected_previous_status=previous_status.value,
                    attempted_status=new_status.value,
                    current_status=current.status.value if current else "UNKNOWN",
                )
                return False
            logger.exception("Failed to update status for order %s", order_id)
            return False
        except Exception:
            logger.exception("Failed to update status for order %s", order_id)
            return False

    async def get_open_orders(self) -> list[StoredOrder]:
        """
        Retrieve all orders that are not yet in a terminal state.

        Terminal states: FILLED, CANCELLED, REJECTED.
        Open states:     PENDING, ACK_UNKNOWN, PLACED, PARTIALLY_FILLED.

        Uses the ``status-index`` GSI (hash key: ``order_status``) so each
        status is a targeted index query rather than a full-table scan.
        Runs parallel queries — one per open status — and merges the
        results.  This is significantly cheaper at scale than a scan with an
        OR filter expression.

        Returns:
            List of typed ``OrderResponse`` objects for all open orders.
        """
        if self._dynamo is None:
            return []

        _OPEN_STATUSES = [
            OrderStatus.PENDING.value,
            OrderStatus.ACK_UNKNOWN.value,
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

    async def get_all_open_positions(self) -> list[StoredOrder]:
        """Alias for get_open_orders — used by position_monitor."""
        return await self.get_open_orders()

    async def get_orders_by_status(self, status_value: str) -> list[StoredOrder]:
        """
        Retrieve all orders matching a single status value via the status-index GSI.

        Paginates automatically.  Used by ``OrphanDetector`` to scan FILLED
        entry orders without a full table scan.

        Args:
            status_value: Raw status string, e.g. ``OrderStatus.FILLED.value``.

        Returns:
            List of ``StoredOrder`` objects with the requested status.
        """
        if self._dynamo is None:
            return []

        collected: list[dict[str, Any]] = []
        exclusive_start_key: Optional[dict] = None

        try:
            while True:
                kwargs: dict[str, Any] = {
                    "TableName": self._orders_table,
                    "IndexName": "status-index",
                    "KeyConditionExpression": "order_status = :s",
                    "ExpressionAttributeValues": {":s": {"S": status_value}},
                }
                if exclusive_start_key:
                    kwargs["ExclusiveStartKey"] = exclusive_start_key

                response = await asyncio.to_thread(self._dynamo.query, **kwargs)
                collected.extend(response.get("Items", []))

                exclusive_start_key = response.get("LastEvaluatedKey")
                if not exclusive_start_key:
                    break

            return [_unmarshal_item(item) for item in collected]

        except Exception:
            logger.exception(
                "Failed to fetch orders by status=%s from DynamoDB", status_value
            )
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

    async def apply_fill_to_position(
        self,
        symbol: str,
        side: OrderSide,
        filled_quantity: float,
        avg_fill_price: float,
        last_price: float,
        # Optional lifecycle metadata — pass when available for structured logging
        order_id: Optional[str] = None,
        signal_id: Optional[str] = None,
        risk_decision_id: Optional[str] = None,
        order_type: Optional[str] = None,
        market_str: Optional[str] = None,
        signal_price: Optional[float] = None,
        order_submitted_at: Optional[str] = None,
        tick_time: Optional[str] = None,
        signal_time: Optional[str] = None,
        risk_approval_time: Optional[str] = None,
    ) -> bool:
        """
        Atomically update the positions table to reflect a confirmed fill.

        Called by the reconciliation path (startup) and the live fill handler
        to keep the positions table consistent with executed trades.

        Position update logic:
            BUY:  quantity += filled_qty
                  avg_price = weighted average of existing + new fill
            SELL: quantity -= filled_qty
                  avg_price unchanged (realized P&L is recorded on orders table)

        Uses DynamoDB ``update_item`` with conditional arithmetic expressions
        so concurrent fills for the same symbol accumulate correctly without
        read-modify-write races.

        Args:
            symbol:          Trading symbol.
            side:            BUY or SELL.
            filled_quantity: Number of shares/units filled.
            avg_fill_price:  Average price of the fill.
            last_price:      Current market price (for mark-to-market).

        Returns:
            True on success, False on failure.
        """
        if self._dynamo is None:
            logger.warning(
                "No DynamoDB client — position not updated for fill: "
                "%s %s %.0f @ %.4f",
                side.value, symbol, filled_quantity, avg_fill_price,
            )
            return True

        key = position_key(symbol)

        try:
            for _attempt in range(3):
                existing_resp = await asyncio.to_thread(
                    self._dynamo.get_item,
                    TableName=self._positions_table,
                    Key=key,
                )
                item = existing_resp.get("Item") or {}

                def _num(name: str, default: float = 0.0) -> float:
                    raw = item.get(name, {}).get("N")
                    return float(raw) if raw is not None else default

                old_qty = _num("quantity", _num("confirmed_quantity", 0.0))
                old_avg = _num("avg_price", _num("avg_entry_price", 0.0))
                old_cost = _num("cost_basis", old_qty * old_avg)
                old_realized = _num("realized_pnl", 0.0)

                if side == OrderSide.BUY:
                    new_qty = old_qty + filled_quantity
                    new_cost = old_cost + (filled_quantity * avg_fill_price)
                    new_avg = new_cost / new_qty if abs(new_qty) > 1e-9 else 0.0
                    if old_qty < 0:
                        closing_qty = min(filled_quantity, abs(old_qty))
                        realized_pnl = old_realized + (old_avg - avg_fill_price) * closing_qty
                    else:
                        realized_pnl = old_realized
                else:
                    closing_qty = min(filled_quantity, max(old_qty, 0.0))
                    realized_pnl = old_realized + (
                        (avg_fill_price - old_avg) * closing_qty
                    )
                    new_qty = old_qty - filled_quantity
                    if new_qty > 1e-9:
                        new_avg = old_avg
                        new_cost = old_avg * new_qty
                    elif new_qty < -1e-9:
                        new_avg = avg_fill_price
                        new_cost = new_qty * avg_fill_price
                    else:
                        new_qty = 0.0
                        new_avg = 0.0
                        new_cost = 0.0

                if new_qty > 0:
                    direction = "LONG"
                    unrealized_pnl = new_qty * (last_price - new_avg)
                elif new_qty < 0:
                    direction = "SHORT"
                    unrealized_pnl = abs(new_qty) * (new_avg - last_price)
                else:
                    direction = "FLAT"
                    unrealized_pnl = 0.0

                condition = "attribute_not_exists(updated_at)"
                expr_values: dict[str, Any] = {
                    ":qty": {"N": str(new_qty)},
                    ":avg": {"N": str(new_avg)},
                    ":cost": {"N": str(new_cost)},
                    ":last": {"N": str(last_price)},
                    ":realized": {"N": str(realized_pnl)},
                    ":unrealized": {"N": str(unrealized_pnl)},
                    ":sym": {"S": symbol},
                    ":direction": {"S": direction},
                    ":product": {"S": "MIS" if market_str == "NSE" else "DAY"},
                    ":ts": {"S": utc_iso()},
                }
                if item.get("updated_at", {}).get("S"):
                    condition = "updated_at = :prev_updated_at"
                    expr_values[":prev_updated_at"] = item["updated_at"]

                # When opening or building a long/short position, clear any
                # stale exit_order_id left from a previous exit on the same
                # symbol. If the field persists, TEE skips the position at the
                # "exit already in-flight" guard even though no exit is running.
                _set_clause = (
                    "SET quantity = :qty, "
                    "confirmed_quantity = :qty, "
                    "avg_price = :avg, "
                    "avg_entry_price = :avg, "
                    "cost_basis = :cost, "
                    "last_price = :last, "
                    "realized_pnl = :realized, "
                    "unrealized_pnl = :unrealized, "
                    "symbol = :sym, "
                    "direction = :direction, "
                    "product = :product, "
                    "updated_at = :ts"
                )
                if direction != "FLAT":
                    _set_clause += " REMOVE exit_order_id, exit_trigger, exit_state"

                try:
                    await asyncio.to_thread(
                        self._dynamo.update_item,
                        TableName=self._positions_table,
                        Key=key,
                        UpdateExpression=_set_clause,
                        ConditionExpression=condition,
                        ExpressionAttributeValues=expr_values,
                    )
                except ClientError as exc:
                    if (
                        exc.response["Error"]["Code"]
                        == "ConditionalCheckFailedException"
                        and _attempt < 2
                    ):
                        continue
                    raise
                break

            logger.info(
                "Position updated: %s %s %.0f @ %.4f → positions table",
                side.value, symbol, filled_quantity, avg_fill_price,
            )

            # Emit unified trade lifecycle structured event — captures the
            # complete tick→signal→risk→order→fill timing in one log line.
            self.log_trade_lifecycle(
                order_id=order_id,
                signal_id=signal_id,
                risk_decision_id=risk_decision_id,
                symbol=symbol,
                market=market_str,
                side=side.value,
                quantity=filled_quantity,
                order_type=order_type,
                avg_fill_price=avg_fill_price,
                filled_quantity=filled_quantity,
                signal_price=signal_price,
                order_submitted_at=order_submitted_at,
                fill_confirmed_at=utc_iso(),
                tick_time=tick_time,
                signal_time=signal_time,
                risk_approval_time=risk_approval_time,
            )

            # Best-effort NAV update in risk-state table.
            # This allows the risk engine's NAV refresh loop to pick up the
            # latest portfolio value without a full position scan.
            # We write portfolio_value = opening_capital + realized_pnl_running.
            # Unrealized P&L is excluded here (refreshed on next positions scan).
            await self._write_nav_snapshot(
                side=side,
                filled_quantity=filled_quantity,
                avg_fill_price=avg_fill_price,
            )

            return True

        except Exception:
            logger.exception(
                "Failed to update position for fill: %s %s %.0f @ %.4f",
                side.value, symbol, filled_quantity, avg_fill_price,
            )
            return False

    async def attach_exit_policy(
        self,
        symbol: str,
        *,
        stop_price: Optional[float],
        take_profit: Optional[float] = None,
        policy_id: Optional[str] = None,
    ) -> bool:
        """
        Attach an exit policy to an open position immediately after entry fill.

        Writes stop_price, take_profit (optional), policy_id, and
        exit_state=EXIT_POLICY_ATTACHED to the positions table.

        This is the Phase 2 exit policy attachment step. Called from the
        execution service after every confirmed entry fill (paper and live).

        If stop_price is None or zero, a CRITICAL warning is emitted — the
        position will be open but unmanaged (no stop protection). The TEE will
        alert on this condition on every poll cycle.

        Args:
            symbol:      Trading symbol.
            stop_price:  Stop-loss level. Must be > 0 for a managed position.
            take_profit: Take-profit level (optional).
            policy_id:   Unique policy identifier (defaults to POLICY-{symbol}-{ts}).

        Returns:
            True on success, False on failure.
        """
        if self._dynamo is None:
            logger.warning(
                "attach_exit_policy.no_dynamo",
                symbol=symbol,
                detail="No DynamoDB client — exit policy not persisted",
            )
            return True  # non-fatal in tests without dynamo

        if not stop_price or stop_price <= 0:
            logger.critical(
                "attach_exit_policy.missing_stop_price",
                symbol=symbol,
                stop_price=stop_price,
                detail=(
                    "Entry fill confirmed but stop_price is absent or zero. "
                    "Position is OPEN but UNMANAGED. Risk is unprotected."
                ),
            )
            # Still attach what we have — TEE will alert on every cycle.

        pid = policy_id or f"POLICY-{symbol}-{utc_iso()}"
        key = position_key(symbol)

        update_parts = [
            "exit_state = :exit_state",
            "exit_policy_id = :policy_id",
        ]
        expr_values: dict[str, Any] = {
            ":exit_state": {"S": "EXIT_POLICY_ATTACHED"},
            ":policy_id":  {"S": pid},
        }

        if stop_price and stop_price > 0:
            update_parts.append("stop_price = :stop_price")
            expr_values[":stop_price"] = {"N": str(stop_price)}

        if take_profit is not None and take_profit > 0:
            update_parts.append("take_profit = :take_profit")
            expr_values[":take_profit"] = {"N": str(take_profit)}

        try:
            await asyncio.to_thread(
                self._dynamo.update_item,
                TableName=self._positions_table,
                Key=key,
                UpdateExpression="SET " + ", ".join(update_parts),
                ExpressionAttributeValues=expr_values,
            )
            logger.info(
                "attach_exit_policy.attached",
                symbol=symbol,
                stop_price=stop_price,
                take_profit=take_profit,
                policy_id=pid,
            )
            return True
        except Exception:
            logger.exception(
                "attach_exit_policy.write_failed",
                symbol=symbol,
                stop_price=stop_price,
            )
            return False

    async def _write_nav_snapshot(
        self,
        side: "OrderSide",
        filled_quantity: float,
        avg_fill_price: float,
    ) -> None:
        """
        Atomically update the running portfolio NAV in DynamoDB after a fill.

        Writes to the risk-state table so the risk engine's NAV refresh loop
        can read current portfolio value without a full position scan.

        The NAV item stores:
            portfolio_value  — opening_capital + cumulative realized_pnl
            last_fill_at     — UTC ISO timestamp of last fill
            updated_at       — UTC ISO timestamp of this write

        This is a best-effort write. If it fails, the risk engine continues
        using its last known NAV value (at most 30s stale) and logs a warning.

        Args:
            side:            BUY or SELL.
            filled_quantity: Shares filled.
            avg_fill_price:  Average fill price.
        """
        if self._dynamo is None or not self._risk_state_table:
            return

        try:
            now = utc_iso()
            fill_value = filled_quantity * avg_fill_price

            # For a SELL, cash increases. For a BUY, cash decreases. This is a
            # cash-flow NAV approximation; risk_engine.record_fill writes the
            # realized-PnL NAV fields used for daily-loss gating.
            cash_delta = fill_value if side == OrderSide.SELL else -fill_value
            existing = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._risk_state_table,
                Key=nav_key(),
            )
            old_cash = float(
                (existing.get("Item") or {})
                .get("realized_cash_flow", {})
                .get("N", "0")
            )
            new_cash = old_cash + cash_delta
            opening_nav = float(getattr(self._settings, "portfolio_value", 1_000_000.0))
            portfolio_value = opening_nav + new_cash

            await asyncio.to_thread(
                self._dynamo.update_item,
                TableName=self._risk_state_table,
                Key=nav_key(),
                UpdateExpression=(
                    "SET last_fill_at = :ts, updated_at = :ts, "
                    "realized_cash_flow = :cash, portfolio_value = :nav"
                ),
                ExpressionAttributeValues={
                    ":ts": {"S": now},
                    ":cash": {"N": str(new_cash)},
                    ":nav": {"N": str(portfolio_value)},
                },
            )
        except Exception:
            logger.warning(
                "NAV snapshot write failed after fill — "
                "risk engine will use previous NAV value (at most 30s stale).",
                exc_info=True,
            )

    def log_trade_lifecycle(
        self,
        *,
        avg_fill_price: float,
        filled_quantity: float,
        order_id: Optional[str] = None,
        signal_id: Optional[str] = None,
        risk_decision_id: Optional[str] = None,
        symbol: Optional[str] = None,
        market: Optional[str] = None,
        side: Optional[str] = None,
        quantity: Optional[float] = None,
        order_type: Optional[str] = None,
        signal_price: Optional[float] = None,
        order_submitted_at: Optional[str] = None,
        fill_confirmed_at: Optional[str] = None,
        tick_time: Optional[str] = None,
        signal_time: Optional[str] = None,
        risk_approval_time: Optional[str] = None,
    ) -> None:
        """
        Emit a single structured log event covering the full trade lifecycle.

        This is the primary audit trail for execution quality analysis.  A
        single log line captures every meaningful timestamp in the pipeline
        (tick → signal → risk approval → order submission → fill) plus derived
        duration and slippage metrics so that post-trade analysis does not
        require joining across multiple log sources.

        Fields emitted:

            Identifiers:
                event               Always "trade_lifecycle"
                order_id            Internal execution engine order ID
                signal_id           Originating strategy signal ID
                risk_decision_id    Risk engine approval ID

            Order parameters:
                symbol, market, side, quantity, order_type

            Timestamps (ISO-8601 UTC, None if not available):
                tick_time           When the triggering tick arrived at data_ingestion
                signal_time         When the strategy engine generated the signal
                risk_approval_time  When the risk engine approved the signal
                order_submitted_at  When execution engine sent the order to the broker
                fill_confirmed_at   When the fill was confirmed by the broker

            Durations (milliseconds, None if timestamps unavailable):
                order_to_fill_ms    order_submitted_at → fill_confirmed_at
                signal_to_fill_ms   signal_time → fill_confirmed_at
                tick_to_fill_ms     tick_time → fill_confirmed_at (end-to-end)

            Execution quality:
                avg_fill_price      Average price paid/received
                signal_price        Price when signal was generated (if available)
                slippage_bps        (fill − signal) / signal × 10 000, signed:
                                    positive = unfavorable (paid more / sold less)
                filled_quantity     Actual shares filled
                fill_value          filled_quantity × avg_fill_price

        All fields are always present in the log record; None values serialise
        as ``null`` in JSON structured logging backends so dashboards can
        distinguish "not available" from zero.

        This method never raises — lifecycle logging must not interrupt the
        critical path.

        Args:
            avg_fill_price:      Average fill price (required).
            filled_quantity:     Shares/units filled (required).
            order_id:            Internal order ID.
            signal_id:           Originating signal ID.
            risk_decision_id:    Risk approval ID.
            symbol:              Trading symbol.
            market:              Market identifier (e.g. "NSE", "US").
            side:                Order side string ("BUY" or "SELL").
            quantity:            Requested order quantity.
            order_type:          Order type string (e.g. "MARKET").
            signal_price:        Price at signal generation time.
            order_submitted_at:  ISO UTC timestamp of order submission.
            fill_confirmed_at:   ISO UTC timestamp of fill confirmation.
            tick_time:           ISO UTC timestamp of the originating tick.
            signal_time:         ISO UTC timestamp of signal generation.
            risk_approval_time:  ISO UTC timestamp of risk approval.
        """
        try:
            now_iso = fill_confirmed_at or utc_iso()

            # ── Compute durations ─────────────────────────────────────────────

            def _ms_between(t_start: Optional[str], t_end: Optional[str]) -> Optional[float]:
                """Return milliseconds between two ISO UTC strings, or None."""
                if not t_start or not t_end:
                    return None
                try:
                    # Handle both +00:00 and Z suffixes
                    def _parse(s: str) -> datetime:
                        return datetime.fromisoformat(s.replace("Z", "+00:00"))
                    delta = _parse(t_end) - _parse(t_start)
                    return round(delta.total_seconds() * 1000.0, 2)
                except Exception:
                    return None

            order_to_fill_ms  = _ms_between(order_submitted_at, now_iso)
            signal_to_fill_ms = _ms_between(signal_time, now_iso)
            tick_to_fill_ms   = _ms_between(tick_time, now_iso)

            # ── Compute slippage in basis points ─────────────────────────────
            slippage_bps: Optional[float] = None
            if signal_price and signal_price != 0:
                raw_slippage = avg_fill_price - signal_price
                # For a BUY: positive slippage = paid more than signal price (bad)
                # For a SELL: positive slippage = received less (bad) → negate
                if side and side.upper() == "SELL":
                    raw_slippage = -raw_slippage
                slippage_bps = round(raw_slippage / signal_price * 10_000, 3)

            fill_value = round(filled_quantity * avg_fill_price, 4)

            # ── Emit the single structured log event ──────────────────────────
            logger.info(
                "trade_lifecycle",
                # Identifiers
                event="trade_lifecycle",
                order_id=order_id,
                signal_id=signal_id,
                risk_decision_id=risk_decision_id,
                # Order parameters
                symbol=symbol,
                market=market,
                side=side,
                quantity=quantity,
                order_type=order_type,
                # Timestamps
                tick_time=tick_time,
                signal_time=signal_time,
                risk_approval_time=risk_approval_time,
                order_submitted_at=order_submitted_at,
                fill_confirmed_at=now_iso,
                # Durations
                order_to_fill_ms=order_to_fill_ms,
                signal_to_fill_ms=signal_to_fill_ms,
                tick_to_fill_ms=tick_to_fill_ms,
                # Execution quality
                avg_fill_price=avg_fill_price,
                signal_price=signal_price,
                slippage_bps=slippage_bps,
                filled_quantity=filled_quantity,
                fill_value=fill_value,
            )

        except Exception:
            # Never let lifecycle logging interrupt the fill processing path
            logger.warning(
                "log_trade_lifecycle failed — logging error should not affect order state",
                exc_info=True,
            )
