"""
Integration-style tests for the execution engine idempotency path.

These tests keep AWS and broker calls fully in-memory while exercising the
real ``ExecutionService`` + ``OrderManager`` flow end to end:
    - first submission
    - concurrent duplicate submission
    - broker failure leaving PENDING
    - restart reconciliation of stranded PENDING
    - legacy lock-row presence in signal-index
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from botocore.exceptions import ClientError


def _install_import_aliases() -> None:
    """Make both `services.*` and top-level imports resolve to the same modules."""
    project_root = Path(__file__).resolve().parents[2]
    services_dir = project_root / "services"

    for path in (project_root, services_dir):
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)

    sys.modules.setdefault("structlog", types.ModuleType("structlog"))

    import services.shared as services_shared
    import services.shared.config as services_shared_config
    import services.shared.config.settings as services_shared_settings
    import services.shared.logging as services_shared_logging
    import services.shared.logging.logger as services_shared_logger
    import services.shared.utils as services_shared_utils
    import services.shared.utils.helpers as services_shared_helpers
    import services.execution_engine as services_execution_engine
    import services.execution_engine.orders as services_execution_orders
    import services.execution_engine.orders.order as services_execution_order
    import services.execution_engine.brokers as services_execution_brokers
    import services.execution_engine.retry as services_execution_retry

    sys.modules["shared"] = services_shared
    sys.modules["shared.config"] = services_shared_config
    sys.modules["shared.config.settings"] = services_shared_settings
    sys.modules["shared.logging"] = services_shared_logging
    sys.modules["shared.logging.logger"] = services_shared_logger
    sys.modules["shared.utils"] = services_shared_utils
    sys.modules["shared.utils.helpers"] = services_shared_helpers
    sys.modules["execution_engine"] = services_execution_engine
    sys.modules["execution_engine.orders"] = services_execution_orders
    sys.modules["execution_engine.orders.order"] = services_execution_order
    sys.modules["execution_engine.brokers"] = services_execution_brokers
    sys.modules["execution_engine.retry"] = services_execution_retry


_install_import_aliases()

from services.execution_engine.orders.order import (  # noqa: E402
    Market,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderStatus,
    OrderStatusUpdate,
    OrderType,
)
from services.execution_engine.orders.order_manager import OrderManager  # noqa: E402
from services.execution_engine.service import ExecutionService  # noqa: E402


def _settings() -> Any:
    return SimpleNamespace(
        execution=SimpleNamespace(
            max_retries=1,
            retry_base_delay=0.0,
            max_delay=0.0,
        )
    )


def _order_manager_settings() -> Any:
    return SimpleNamespace(
        aws=SimpleNamespace(
            dynamodb_table_orders="test-orders",
        )
    )


class FakeDynamoClient:
    """Tiny in-memory DynamoDB low-level client for execution tests."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(item: dict[str, Any]) -> tuple[str, str]:
        return item["PK"]["S"], item["SK"]["S"]

    def transact_write_items(self, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            reasons: list[dict[str, str]] = []
            pending: list[tuple[tuple[str, str], dict[str, Any]]] = []

            for tx in TransactItems:
                put = tx["Put"]
                item = put["Item"]
                key = self._key(item)
                exists = key in self._items
                if exists and put.get("ConditionExpression") == "attribute_not_exists(PK)":
                    reasons.append({"Code": "ConditionalCheckFailed"})
                else:
                    reasons.append({"Code": "None"})
                    pending.append((key, item.copy()))

            if any(r["Code"] != "None" for r in reasons):
                raise ClientError(
                    {
                        "Error": {
                            "Code": "TransactionCanceledException",
                            "Message": "transaction cancelled",
                        },
                        "CancellationReasons": reasons,
                    },
                    "TransactWriteItems",
                )

            for key, item in pending:
                self._items[key] = item

        return {}

    def update_item(
        self,
        TableName: str,
        Key: dict[str, Any],
        UpdateExpression: str,
        ExpressionAttributeValues: dict[str, Any],
        ConditionExpression: Optional[str] = None,
    ) -> dict[str, Any]:
        del TableName, UpdateExpression
        with self._lock:
            key = (Key["PK"]["S"], Key["SK"]["S"])
            item = self._items[key]

            if ConditionExpression == "order_status = :prev_status":
                current = item.get("order_status", {}).get("S", "")
                if current != ExpressionAttributeValues[":prev_status"]["S"]:
                    raise RuntimeError("conditional update failed")

            mapping = {
                ":status": "order_status",
                ":filled_qty": "filled_quantity",
                ":filled": "filled_quantity",
                ":avg_price": "avg_fill_price",
                ":slippage": "slippage",
                ":broker_id": "broker_order_id",
                ":broker_msg": "broker_message",
                ":msg": "broker_message",
                ":updated_at": "updated_at",
                ":ts": "updated_at",
            }
            for expr_key, item_key in mapping.items():
                if expr_key in ExpressionAttributeValues:
                    item[item_key] = ExpressionAttributeValues[expr_key]

        return {}

    def get_item(self, TableName: str, Key: dict[str, Any]) -> dict[str, Any]:
        del TableName
        with self._lock:
            key = (Key["PK"]["S"], Key["SK"]["S"])
            return {"Item": self._items.get(key)}

    def query(
        self,
        TableName: str,
        IndexName: str,
        KeyConditionExpression: str,
        ExpressionAttributeValues: dict[str, Any],
        FilterExpression: Optional[str] = None,
        ExclusiveStartKey: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        del TableName, KeyConditionExpression, ExclusiveStartKey
        with self._lock:
            items = list(self._items.values())

            if IndexName == "signal-index":
                signal_id = ExpressionAttributeValues[":sid"]["S"]
                matches = [
                    item for item in items
                    if item.get("signal_id", {}).get("S") == signal_id
                ]
                if FilterExpression == "SK = :meta":
                    meta = ExpressionAttributeValues[":meta"]["S"]
                    matches = [item for item in matches if item["SK"]["S"] == meta]
                matches.sort(key=lambda item: (item["PK"]["S"], item["SK"]["S"]))
                return {"Items": matches}

            if IndexName == "status-index":
                status = ExpressionAttributeValues[":s"]["S"]
                matches = [
                    item for item in items
                    if item.get("order_status", {}).get("S") == status
                ]
                matches.sort(
                    key=lambda item: (
                        item.get("created_at", {}).get("S", ""),
                        item["PK"]["S"],
                    )
                )
                return {"Items": matches}

        raise NotImplementedError(f"Unsupported index {IndexName}")

    def put_legacy_signal_lock(self, signal_id: str, order_id: str) -> None:
        with self._lock:
            self._items[(f"SIGNAL#{signal_id}", "LOCK")] = {
                "PK": {"S": f"SIGNAL#{signal_id}"},
                "SK": {"S": "LOCK"},
                "signal_id": {"S": signal_id},
                "order_id": {"S": order_id},
                "created_at": {"S": "2026-01-01T00:00:00+00:00"},
            }


class FakeBroker:
    """In-memory broker double with programmable placement outcomes."""

    def __init__(
        self,
        market: Market,
        effects: Optional[list[Any]] = None,
        gate: Optional[asyncio.Event] = None,
    ) -> None:
        self.market = market
        self.effects = list(effects or [])
        self.gate = gate
        self.placed_requests: list[Any] = []
        self.status_updates: dict[str, OrderStatusUpdate] = {}

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def place_order(self, order: Any) -> OrderResponse:
        if self.gate is not None:
            await self.gate.wait()
        self.placed_requests.append(order)

        if self.effects:
            effect = self.effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            if callable(effect):
                return effect(order)

        broker_order_id = f"{self.market.value.lower()}-{len(self.placed_requests)}"
        response = OrderResponse(
            order_id=order.order_id,
            broker_order_id=broker_order_id,
            status=OrderStatus.PLACED,
            symbol=order.symbol,
            market=order.market,
        )
        self.status_updates[broker_order_id] = OrderStatusUpdate(
            order_id=order.order_id,
            broker_order_id=broker_order_id,
            previous_status=OrderStatus.PLACED,
            new_status=OrderStatus.PLACED,
            filled_quantity=0.0,
            avg_fill_price=0.0,
        )
        return response

    async def get_order_status(self, broker_order_id: str) -> OrderStatusUpdate:
        return self.status_updates[broker_order_id]


def _make_service(
    dynamo: FakeDynamoClient,
    nse_broker: Optional[FakeBroker] = None,
    us_broker: Optional[FakeBroker] = None,
) -> ExecutionService:
    service = ExecutionService(settings=_settings())
    service._order_manager = OrderManager(
        dynamo_client=dynamo,
        orders_table="test-orders",
        settings=_order_manager_settings(),
    )
    service._zerodha = nse_broker or FakeBroker(Market.NSE)
    service._alpaca = us_broker or FakeBroker(Market.US)
    return service


@pytest.mark.asyncio
async def test_first_submission_persists_and_places_order() -> None:
    dynamo = FakeDynamoClient()
    broker = FakeBroker(Market.NSE)
    service = _make_service(dynamo=dynamo, nse_broker=broker)

    response = await service.execute_approved_signal(
        signal_id="sig-first",
        risk_decision_id="risk-first",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        quantity=10.0,
        order_type=OrderType.MARKET,
        market=Market.NSE,
    )

    stored = await service._order_manager.get_order_by_signal("sig-first")
    assert response.status == OrderStatus.PLACED
    assert len(broker.placed_requests) == 1
    assert stored is not None
    assert stored.order_id == response.order_id
    assert stored.broker_order_id == response.broker_order_id
    assert stored.status == OrderStatus.PLACED


@pytest.mark.asyncio
async def test_concurrent_duplicate_submission_places_only_one_order() -> None:
    dynamo = FakeDynamoClient()
    gate = asyncio.Event()
    broker = FakeBroker(Market.NSE, gate=gate)
    service = _make_service(dynamo=dynamo, nse_broker=broker)

    first = asyncio.create_task(
        service.execute_approved_signal(
            signal_id="sig-concurrent",
            risk_decision_id="risk-concurrent",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=5.0,
            order_type=OrderType.MARKET,
            market=Market.NSE,
        )
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(
        service.execute_approved_signal(
            signal_id="sig-concurrent",
            risk_decision_id="risk-concurrent",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=5.0,
            order_type=OrderType.MARKET,
            market=Market.NSE,
        )
    )

    gate.set()
    first_response, second_response = await asyncio.gather(first, second)

    assert len(broker.placed_requests) == 1
    assert first_response.order_id == second_response.order_id
    assert first_response.broker_order_id == second_response.broker_order_id


@pytest.mark.asyncio
async def test_broker_failure_leaves_pending_order() -> None:
    dynamo = FakeDynamoClient()
    broker = FakeBroker(Market.NSE, effects=[RuntimeError("broker down")])
    service = _make_service(dynamo=dynamo, nse_broker=broker)

    with pytest.raises(RuntimeError, match="broker down"):
        await service.execute_approved_signal(
            signal_id="sig-pending",
            risk_decision_id="risk-pending",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=12.0,
            order_type=OrderType.MARKET,
            market=Market.NSE,
        )

    stored = await service._order_manager.get_order_by_signal("sig-pending")
    assert stored is not None
    assert stored.status == OrderStatus.PENDING
    assert stored.broker_order_id == ""


@pytest.mark.asyncio
async def test_restart_reconciliation_retries_stranded_pending() -> None:
    dynamo = FakeDynamoClient()
    failing_broker = FakeBroker(Market.NSE, effects=[RuntimeError("first call fails")])
    first_service = _make_service(dynamo=dynamo, nse_broker=failing_broker)

    with pytest.raises(RuntimeError, match="first call fails"):
        await first_service.execute_approved_signal(
            signal_id="sig-restart",
            risk_decision_id="risk-restart",
            symbol="RELIANCE",
            side=OrderSide.SELL,
            quantity=7.0,
            order_type=OrderType.LIMIT,
            market=Market.NSE,
            limit_price=2500.0,
            stop_price=2450.0,
        )

    recovering_broker = FakeBroker(Market.NSE)
    restarted_service = _make_service(dynamo=dynamo, nse_broker=recovering_broker)
    await restarted_service._reconcile_state()

    stored = await restarted_service._order_manager.get_order_by_signal("sig-restart")
    assert stored is not None
    assert stored.status == OrderStatus.PLACED
    assert stored.side == OrderSide.SELL
    assert stored.order_type == OrderType.LIMIT
    assert stored.quantity == 7.0
    assert stored.limit_price == 2500.0
    assert stored.stop_price == 2450.0
    assert len(recovering_broker.placed_requests) == 1
    retried_request = recovering_broker.placed_requests[0]
    assert retried_request.order_id == stored.order_id
    assert retried_request.side == OrderSide.SELL
    assert retried_request.quantity == 7.0
    assert retried_request.limit_price == 2500.0
    assert retried_request.stop_price == 2450.0


@pytest.mark.asyncio
async def test_signal_index_ignores_legacy_lock_rows() -> None:
    dynamo = FakeDynamoClient()
    service = _make_service(dynamo=dynamo)

    await service._order_manager.submit_order(
        OrderRequest(
            signal_id="sig-legacy",
            risk_decision_id="risk-legacy",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=3.0,
            order_type=OrderType.MARKET,
            market=Market.US,
        )
    )
    dynamo.put_legacy_signal_lock(signal_id="sig-legacy", order_id="legacy-lock-order")

    stored = await service._order_manager.get_order_by_signal("sig-legacy")
    assert stored is not None
    assert stored.order_id != "legacy-lock-order"
    assert stored.symbol == "AAPL"
    assert stored.status == OrderStatus.PENDING
