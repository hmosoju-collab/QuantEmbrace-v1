from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from execution_engine.orders.order import OrderStatus
from execution_engine.orders.order_manager import OrderManager


def _settings() -> Any:
    return SimpleNamespace(
        aws=SimpleNamespace(
            dynamodb_table_orders="orders",
            dynamodb_table_positions="positions",
            dynamodb_table_risk_state="risk-state",
        )
    )


class _Dynamo:
    def __init__(self, status: OrderStatus) -> None:
        self.item: dict[str, Any] = {
            "PK": {"S": "ORDER#order-1"},
            "SK": {"S": "META"},
            "order_id": {"S": "order-1"},
            "broker_order_id": {"S": "broker-1"},
            "order_status": {"S": status.value},
            "symbol": {"S": "RELIANCE"},
            "market": {"S": "NSE"},
            "filled_quantity": {"N": "0"},
            "avg_fill_price": {"N": "0"},
        }

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        return {"Item": self.item}

    def update_item(
        self,
        *,
        UpdateExpression: str,
        ExpressionAttributeValues: dict[str, Any],
        ConditionExpression: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if ConditionExpression == "order_status = :prev_status":
            expected = ExpressionAttributeValues[":prev_status"]["S"]
            if self.item["order_status"]["S"] != expected:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "ConditionalCheckFailedException",
                            "Message": "status changed",
                        }
                    },
                    "UpdateItem",
                )
        self.item["order_status"] = ExpressionAttributeValues[":status"]
        self.item["filled_quantity"] = ExpressionAttributeValues[":filled_qty"]
        self.item["avg_fill_price"] = ExpressionAttributeValues[":avg_price"]
        return {}


@pytest.mark.asyncio
async def test_terminal_order_cannot_move_back_to_cancelled() -> None:
    dynamo = _Dynamo(OrderStatus.FILLED)
    manager = OrderManager(dynamo_client=dynamo, orders_table="orders", settings=_settings())

    updated = await manager.update_order_status(
        order_id="order-1",
        new_status=OrderStatus.CANCELLED,
    )

    assert updated is False
    assert dynamo.item["order_status"]["S"] == OrderStatus.FILLED.value


@pytest.mark.asyncio
async def test_pending_order_can_be_cancelled_locally() -> None:
    dynamo = _Dynamo(OrderStatus.PENDING)
    manager = OrderManager(dynamo_client=dynamo, orders_table="orders", settings=_settings())

    updated = await manager.update_order_status(
        order_id="order-1",
        new_status=OrderStatus.CANCELLED,
        broker_message="local kill switch cancel before broker placement",
    )

    assert updated is True
    assert dynamo.item["order_status"]["S"] == OrderStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_pending_order_can_move_to_ack_unknown() -> None:
    dynamo = _Dynamo(OrderStatus.PENDING)
    manager = OrderManager(dynamo_client=dynamo, orders_table="orders", settings=_settings())

    updated = await manager.mark_order_ack_unknown(
        order_id="order-1",
        broker_message="network timeout after placement attempt",
    )

    assert updated is True
    assert dynamo.item["order_status"]["S"] == OrderStatus.ACK_UNKNOWN.value


@pytest.mark.asyncio
async def test_ack_unknown_can_be_recovered_to_placed() -> None:
    dynamo = _Dynamo(OrderStatus.ACK_UNKNOWN)
    manager = OrderManager(dynamo_client=dynamo, orders_table="orders", settings=_settings())

    updated = await manager.update_order_status(
        order_id="order-1",
        new_status=OrderStatus.PLACED,
        broker_message="broker tag scan recovered order",
    )

    assert updated is True
    assert dynamo.item["order_status"]["S"] == OrderStatus.PLACED.value
