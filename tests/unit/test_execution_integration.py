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
from datetime import datetime, timedelta, timezone
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


# ---------------------------------------------------------------------------
# Module-level placeholders — NO side-effects at import/collection time
# ---------------------------------------------------------------------------
_MODULES_SNAPSHOT: frozenset = frozenset()
Market = None
OrderRequest = None
OrderResponse = None
OrderSide = None
OrderStatus = None
OrderStatusUpdate = None
OrderType = None
OrderManager = None
ExecutionService = None
ApprovedSignalEvent = None


def setUpModule() -> None:  # noqa: N802
    """Called by pytest/unittest AFTER collection, BEFORE running tests."""
    global _MODULES_SNAPSHOT
    global Market, OrderRequest, OrderResponse, OrderSide, OrderStatus
    global OrderStatusUpdate, OrderType, OrderManager, ExecutionService, ApprovedSignalEvent

    _MODULES_SNAPSHOT = frozenset(sys.modules.keys())

    _install_import_aliases()

    from services.execution_engine.orders.order import (  # noqa: E402
        Market as _M,
        OrderRequest as _OR,
        OrderResponse as _ORsp,
        OrderSide as _OS,
        OrderStatus as _OSt,
        OrderStatusUpdate as _OSU,
        OrderType as _OT,
    )
    Market = _M
    OrderRequest = _OR
    OrderResponse = _ORsp
    OrderSide = _OS
    OrderStatus = _OSt
    OrderStatusUpdate = _OSU
    OrderType = _OT

    from services.execution_engine.orders.order_manager import OrderManager as _OM  # noqa: E402
    from services.execution_engine.service import ExecutionService as _ES  # noqa: E402
    from services.execution_engine.consumers.kafka_approved_consumer import (  # noqa: E402
        ApprovedSignalEvent as _ASE,
    )
    OrderManager = _OM
    ExecutionService = _ES
    ApprovedSignalEvent = _ASE


def tearDownModule() -> None:  # noqa: N802
    """Remove every sys.modules key added during setUpModule."""
    added = frozenset(sys.modules.keys()) - _MODULES_SNAPSHOT
    for key in added:
        sys.modules.pop(key, None)


def _settings() -> Any:
    return SimpleNamespace(
        execution=SimpleNamespace(
            max_retries=1,
            retry_base_delay=0.0,
            retry_max_delay=0.0,   # matches ExecutionService.__init__ → RetryHandler(max_delay=...)
            ack_unknown_recheck_delay_seconds=0.0,
        ),
        risk=SimpleNamespace(
            kill_switch_poll_interval_seconds=0.0,
        ),
        aws=SimpleNamespace(
            dynamodb_table_risk_state="test-risk-state",
        )
    )


def _order_manager_settings() -> Any:
    return SimpleNamespace(
        aws=SimpleNamespace(
            dynamodb_table_orders="test-orders",
            dynamodb_table_positions="test-positions",    # required by OrderManager.__init__:140
            dynamodb_table_risk_state="test-risk-state", # required by OrderManager.__init__:144
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
        del TableName
        with self._lock:
            key = (Key["PK"]["S"], Key["SK"]["S"])
            item = self._items.setdefault(
                key,
                {"PK": {"S": key[0]}, "SK": {"S": key[1]}},
            )

            if ConditionExpression == "order_status = :prev_status":
                current = item.get("order_status", {}).get("S", "")
                if current != ExpressionAttributeValues[":prev_status"]["S"]:
                    raise RuntimeError("conditional update failed")
            elif ConditionExpression == "attribute_not_exists(updated_at)":
                if "updated_at" in item:
                    raise ClientError(
                        {
                            "Error": {
                                "Code": "ConditionalCheckFailedException",
                                "Message": "conditional update failed",
                            }
                        },
                        "UpdateItem",
                    )
            elif ConditionExpression == "updated_at = :prev_updated_at":
                current = item.get("updated_at", {}).get("S", "")
                expected = ExpressionAttributeValues[":prev_updated_at"]["S"]
                if current != expected:
                    raise ClientError(
                        {
                            "Error": {
                                "Code": "ConditionalCheckFailedException",
                                "Message": "conditional update failed",
                            }
                        },
                        "UpdateItem",
                    )

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
                ":broker_state": "broker_order_state",
                ":outbox_status": "outbox_status",
                ":child": "protective_stop_order_id",
                ":broker_child": "protective_stop_broker_order_id",
                ":ptype": "protective_type",
                ":protection_status": "protection_status",
                ":protection_failed_at": "protection_failed_at",
                ":protection_failure_reason": "protection_failure_reason",
                ":flatten_order_id": "emergency_flatten_order_id",
                ":flatten_broker_order_id": "emergency_flatten_broker_order_id",
                ":last": "last_price",
                ":realized": "realized_pnl",
                ":unrealized": "unrealized_pnl",
                ":sym": "symbol",
                ":direction": "direction",
                ":product": "product",
                ":cash": "realized_cash_flow",
                ":nav": "portfolio_value",
            }
            for expr_key, item_key in mapping.items():
                if expr_key in ExpressionAttributeValues:
                    item[item_key] = ExpressionAttributeValues[expr_key]
            if ":qty" in ExpressionAttributeValues:
                if "protected_quantity" in UpdateExpression:
                    current = float(item.get("protected_quantity", {}).get("N", "0"))
                    delta = float(ExpressionAttributeValues[":qty"]["N"])
                    item["protected_quantity"] = {"N": str(current + delta)}
                else:
                    item["quantity"] = ExpressionAttributeValues[":qty"]
                    item["confirmed_quantity"] = ExpressionAttributeValues[":qty"]
            if ":avg" in ExpressionAttributeValues:
                item["avg_price"] = ExpressionAttributeValues[":avg"]
                item["avg_entry_price"] = ExpressionAttributeValues[":avg"]
            if ":cost" in ExpressionAttributeValues:
                item["cost_basis"] = ExpressionAttributeValues[":cost"]
            if ":delta" in ExpressionAttributeValues:
                current = float(item.get("realized_cash_flow", {}).get("N", "0"))
                delta = float(ExpressionAttributeValues[":delta"]["N"])
                item["realized_cash_flow"] = {"N": str(current + delta)}

        return {}

    def put_item(
        self,
        TableName: str,
        Item: dict[str, Any],
        ConditionExpression: Optional[str] = None,
    ) -> dict[str, Any]:
        del TableName
        with self._lock:
            key = self._key(Item)
            if ConditionExpression == "attribute_not_exists(PK)" and key in self._items:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "ConditionalCheckFailedException",
                            "Message": "conditional put failed",
                        }
                    },
                    "PutItem",
                )
            self._items[key] = Item.copy()
        return {}

    def get_item(self, TableName: str, Key: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        del kwargs
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
                    and item.get("PK", {}).get("S", "").startswith("ORDER#")
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
        self.cancelled_order_ids: list[str] = []
        self.status_updates: dict[str, OrderStatusUpdate] = {}
        self._by_client_id: dict[str, OrderResponse] = {}

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
                response = effect(order)
                if isinstance(response, OrderResponse):
                    self._remember(order, response)
                return response

        broker_order_id = f"{self.market.value.lower()}-{len(self.placed_requests)}"
        response = OrderResponse(
            order_id=order.order_id,
            broker_order_id=broker_order_id,
            status=OrderStatus.PLACED,
            symbol=order.symbol,
            market=order.market,
        )
        self._remember(order, response)
        return response

    def _remember(self, order: Any, response: Any) -> None:
        key = order.metadata.get("broker_idempotency_key", order.order_id)
        self._by_client_id[str(key)] = response
        self.status_updates[response.broker_order_id] = OrderStatusUpdate(
            order_id=order.order_id,
            broker_order_id=response.broker_order_id,
            previous_status=OrderStatus.PLACED,
            new_status=OrderStatus.PLACED,
            filled_quantity=0.0,
            avg_fill_price=0.0,
        )

    async def get_order_status(self, broker_order_id: str) -> OrderStatusUpdate:
        return self.status_updates[broker_order_id]

    async def find_order_by_client_order_id(
        self,
        client_order_id: str,
        order: Any = None,
    ) -> Optional[OrderResponse]:
        del order
        return self._by_client_id.get(client_order_id)

    async def cancel_order(self, broker_order_id: str) -> OrderStatusUpdate:
        self.cancelled_order_ids.append(broker_order_id)
        return OrderStatusUpdate(
            order_id="",
            broker_order_id=broker_order_id,
            previous_status=OrderStatus.PLACED,
            new_status=OrderStatus.CANCELLED,
            broker_message="cancelled",
        )


class FakeOrderPublisher:
    """Capture order events published by ExecutionService."""

    def __init__(self) -> None:
        self.fills: list[dict[str, Any]] = []
        self.rejections: list[dict[str, Any]] = []

    async def publish_fill(self, **kwargs: Any) -> None:
        self.fills.append(kwargs)

    async def publish_rejection(self, **kwargs: Any) -> None:
        self.rejections.append(kwargs)


def _make_service(
    dynamo: FakeDynamoClient,
    nse_broker: Optional[FakeBroker] = None,
    us_broker: Optional[FakeBroker] = None,
) -> ExecutionService:
    service = ExecutionService(settings=_settings())
    service._dynamo = dynamo
    service._order_manager = OrderManager(
        dynamo_client=dynamo,
        orders_table="test-orders",
        settings=_order_manager_settings(),
    )
    service._zerodha = nse_broker or FakeBroker(Market.NSE)
    service._alpaca = us_broker or FakeBroker(Market.US)
    return service


def _approved_event(
    *,
    signal_id: str = "sig-approved",
    risk_decision_id: str = "risk-approved",
    symbol: str = "RELIANCE",
    market: str = "NSE",
    direction: str = "BUY",
    quantity: float = 10.0,
    expires_at: Optional[datetime] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    paper_trade: bool = False,
) -> Any:
    now = datetime.now(timezone.utc)
    return ApprovedSignalEvent(
        signal_id=signal_id,
        risk_decision_id=risk_decision_id,
        trace_id=f"trace-{signal_id}",
        strategy_id="strategy-1",
        symbol=symbol,
        market=market,
        strategy_name="unit",
        direction=direction,
        quantity=quantity,
        price_at_signal=2500.0,
        stop_loss=stop_loss,
        take_profit=take_profit,
        product_type="MIS" if market == "NSE" else "DAY",
        expires_at=expires_at or now + timedelta(minutes=1),
        confidence=0.9,
        signal_time=now,
        approved_at=now,
        paper_trade=paper_trade,
        raw_topic="signals.approved",
        raw_offset=1,
        raw_message=None,
    )


@pytest.mark.asyncio
async def test_first_submission_persists_and_places_order() -> None:
    dynamo = FakeDynamoClient()
    broker = FakeBroker(Market.NSE)
    service = _make_service(dynamo=dynamo, nse_broker=broker)

    response = await service.execute_approved_signal(
        signal_id="sig-first",
        risk_decision_id="risk-first",
        trace_id="test-trace-first",
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
async def test_paper_order_applies_spread_slippage_not_raw_signal_price() -> None:
    dynamo = FakeDynamoClient()
    service = _make_service(dynamo=dynamo)
    publisher = FakeOrderPublisher()
    service._kafka_order_publisher = publisher
    service._settings.execution.paper_latency_ms = 0
    service._settings.execution.paper_slippage_bps = 10.0
    service._settings.execution.paper_spread_bps = 20.0
    service._settings.execution.paper_market_open_gap_bps = 0.0

    approved = _approved_event(signal_id="sig-paper-realistic", paper_trade=True)
    await service._handle_paper_order(approved, OrderSide.BUY, Market.NSE)

    assert len(publisher.fills) == 1
    fill = publisher.fills[0]
    assert fill["avg_fill_price"] > approved.price_at_signal
    assert fill["avg_fill_price"] != approved.price_at_signal
    assert fill["quantity_filled"] == int(approved.quantity)


@pytest.mark.asyncio
async def test_paper_partial_fill_publishes_partial_quantity() -> None:
    dynamo = FakeDynamoClient()
    service = _make_service(dynamo=dynamo)
    publisher = FakeOrderPublisher()
    service._kafka_order_publisher = publisher
    service._settings.execution.paper_latency_ms = 0
    service._settings.execution.paper_partial_fill_probability = 1.0
    service._settings.execution.paper_partial_fill_min_pct = 0.25

    approved = _approved_event(
        signal_id="sig-paper-partial",
        paper_trade=True,
        quantity=10.0,
    )
    await service._handle_paper_order(approved, OrderSide.BUY, Market.NSE)

    assert len(publisher.fills) == 1
    fill = publisher.fills[0]
    assert 0 < fill["quantity_filled"] < fill["quantity_ordered"]


@pytest.mark.asyncio
async def test_paper_circuit_lock_publishes_rejection() -> None:
    dynamo = FakeDynamoClient()
    service = _make_service(dynamo=dynamo)
    publisher = FakeOrderPublisher()
    service._kafka_order_publisher = publisher
    service._settings.execution.paper_latency_ms = 0
    service._settings.execution.paper_circuit_lock_probability = 1.0

    approved = _approved_event(signal_id="sig-paper-circuit", paper_trade=True)
    await service._handle_paper_order(approved, OrderSide.BUY, Market.NSE)

    assert publisher.fills == []
    assert len(publisher.rejections) == 1
    assert publisher.rejections[0]["reject_reason"] == "PAPER_CIRCUIT_LOCK"


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
            trace_id="test-trace-concurrent-1",
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
            trace_id="test-trace-concurrent-2",
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
async def test_broker_failure_marks_ack_unknown_order() -> None:
    dynamo = FakeDynamoClient()
    broker = FakeBroker(Market.NSE, effects=[RuntimeError("broker down")])
    service = _make_service(dynamo=dynamo, nse_broker=broker)

    with pytest.raises(RuntimeError, match="broker down"):
        await service.execute_approved_signal(
            signal_id="sig-pending",
            risk_decision_id="risk-pending",
            trace_id="test-trace-pending",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=12.0,
            order_type=OrderType.MARKET,
            market=Market.NSE,
        )

    stored = await service._order_manager.get_order_by_signal("sig-pending")
    assert stored is not None
    assert stored.status == OrderStatus.ACK_UNKNOWN
    assert stored.broker_order_id == ""


@pytest.mark.asyncio
async def test_restart_reconciliation_retries_stranded_pending() -> None:
    dynamo = FakeDynamoClient()
    first_service = _make_service(dynamo=dynamo)
    stranded = OrderRequest(
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
    await first_service._order_manager.submit_order(stranded)

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


@pytest.mark.asyncio
async def test_stale_approved_event_never_places_broker_order() -> None:
    dynamo = FakeDynamoClient()
    broker = FakeBroker(Market.NSE)
    service = _make_service(dynamo=dynamo, nse_broker=broker)

    ok = await service._handle_approved_signal_event(
        _approved_event(
            signal_id="sig-stale",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
    )

    assert ok is True
    assert broker.placed_requests == []


@pytest.mark.asyncio
async def test_entry_fill_places_protective_stop_child_order() -> None:
    dynamo = FakeDynamoClient()

    def filled(order: Any) -> Any:
        return OrderResponse(
            order_id=order.order_id,
            broker_order_id="nse-entry-filled",
            status=OrderStatus.FILLED,
            symbol=order.symbol,
            market=order.market,
            filled_quantity=10.0,
            avg_fill_price=2500.0,
        )

    broker = FakeBroker(Market.NSE, effects=[filled])
    service = _make_service(dynamo=dynamo, nse_broker=broker)

    response = await service.execute_approved_signal(
        signal_id="sig-protective",
        risk_decision_id="risk-protective",
        trace_id="trace-protective",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        quantity=10.0,
        order_type=OrderType.MARKET,
        market=Market.NSE,
        stop_price=2450.0,
    )

    assert response.status == OrderStatus.FILLED
    assert len(broker.placed_requests) == 2
    child = broker.placed_requests[1]
    assert child.side == OrderSide.SELL
    assert child.order_type == OrderType.STOP_LOSS_MARKET
    assert child.stop_price == 2450.0
    assert child.metadata["parent_order_id"] == response.order_id

    parent_item = await service._order_manager.get_order(response.order_id)
    assert parent_item["protective_stop_order_id"]["S"] == child.order_id
    assert parent_item["protective_stop_broker_order_id"]["S"] == "nse-2"


@pytest.mark.asyncio
async def test_kill_switch_does_not_block_or_cancel_protective_stop_after_fill() -> None:
    dynamo = FakeDynamoClient()
    service_ref: dict[str, Any] = {}

    def filled_then_emergency(order: Any) -> Any:
        service = service_ref["service"]
        service._kill_switch_active = True
        service._kill_switch_reason = "unit test emergency after entry fill"
        return OrderResponse(
            order_id=order.order_id,
            broker_order_id="nse-entry-before-kill",
            status=OrderStatus.FILLED,
            symbol=order.symbol,
            market=order.market,
            filled_quantity=10.0,
            avg_fill_price=2500.0,
        )

    broker = FakeBroker(Market.NSE, effects=[filled_then_emergency])
    service = _make_service(dynamo=dynamo, nse_broker=broker)
    service_ref["service"] = service

    response = await service.execute_approved_signal(
        signal_id="sig-protective-kill-switch",
        risk_decision_id="risk-protective-kill-switch",
        trace_id="trace-protective-kill-switch",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        quantity=10.0,
        order_type=OrderType.MARKET,
        market=Market.NSE,
        stop_price=2450.0,
    )

    assert response.status == OrderStatus.FILLED
    assert len(broker.placed_requests) == 2
    child = broker.placed_requests[1]
    assert child.side == OrderSide.SELL
    assert child.order_type == OrderType.STOP_LOSS_MARKET
    assert child.stop_price == 2450.0

    await service._handle_kill_switch_activation(
        reason="unit test emergency sweep",
        activated_by="test",
    )

    assert broker.cancelled_order_ids == []


@pytest.mark.asyncio
async def test_protective_stop_failure_halts_and_places_emergency_flatten() -> None:
    dynamo = FakeDynamoClient()

    def filled(order: Any) -> Any:
        return OrderResponse(
            order_id=order.order_id,
            broker_order_id="nse-entry-filled-unprotected",
            status=OrderStatus.FILLED,
            symbol=order.symbol,
            market=order.market,
            filled_quantity=10.0,
            avg_fill_price=2500.0,
        )

    broker = FakeBroker(
        Market.NSE,
        effects=[
            filled,
            RuntimeError("child stop rejected by broker"),
        ],
    )
    service = _make_service(dynamo=dynamo, nse_broker=broker)

    response = await service.execute_approved_signal(
        signal_id="sig-stop-failure",
        risk_decision_id="risk-stop-failure",
        trace_id="trace-stop-failure",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        quantity=10.0,
        order_type=OrderType.MARKET,
        market=Market.NSE,
        stop_price=2450.0,
    )

    assert response.status == OrderStatus.FILLED
    assert service._kill_switch_active is True
    assert len(broker.placed_requests) == 3
    failed_stop = broker.placed_requests[1]
    flatten = broker.placed_requests[2]
    assert failed_stop.order_type == OrderType.STOP_LOSS_MARKET
    assert flatten.order_type == OrderType.MARKET
    assert flatten.side == OrderSide.SELL
    assert flatten.quantity == pytest.approx(10.0)
    assert flatten.metadata["protective_type"] == "EMERGENCY_FLATTEN"

    parent_item = await service._order_manager.get_order(response.order_id)
    assert parent_item["protection_status"]["S"] == "PROTECTION_FAILED"
    assert parent_item["emergency_flatten_order_id"]["S"] == flatten.order_id
    assert parent_item["emergency_flatten_broker_order_id"]["S"] == "nse-3"


@pytest.mark.asyncio
async def test_reconciliation_places_pending_protective_stop_during_kill_switch() -> None:
    dynamo = FakeDynamoClient()
    broker = FakeBroker(Market.NSE)
    service = _make_service(dynamo=dynamo, nse_broker=broker)

    parent = OrderRequest(
        signal_id="sig-parent-protected",
        risk_decision_id="risk-parent-protected",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        quantity=10.0,
        order_type=OrderType.MARKET,
        market=Market.NSE,
        stop_price=2450.0,
    )
    await service._order_manager.submit_order(parent)
    await service._order_manager.record_order(
        OrderResponse(
            order_id=parent.order_id,
            broker_order_id="nse-parent-filled",
            status=OrderStatus.FILLED,
            symbol=parent.symbol,
            market=parent.market,
            filled_quantity=10.0,
            avg_fill_price=2500.0,
        )
    )

    child = OrderRequest(
        order_id=f"SL-{parent.order_id}-fill-1",
        signal_id=f"{parent.signal_id}#SL#fill-1",
        risk_decision_id=parent.risk_decision_id,
        symbol=parent.symbol,
        side=OrderSide.SELL,
        quantity=10.0,
        order_type=OrderType.STOP_LOSS_MARKET,
        market=parent.market,
        stop_price=2450.0,
        product_type=parent.product_type,
        metadata={
            "parent_order_id": parent.order_id,
            "protective_type": "STOP_LOSS",
            "entry_fill_id": "fill-1",
        },
    )
    await service._order_manager.submit_child_order(
        child,
        parent_order_id=parent.order_id,
        protective_type="STOP_LOSS",
    )

    service._kill_switch_active = True
    service._kill_switch_reason = "unit test restart emergency"

    await service._reconcile_state()

    assert len(broker.placed_requests) == 1
    placed_child = broker.placed_requests[0]
    assert placed_child.order_id == child.order_id
    assert placed_child.order_type == OrderType.STOP_LOSS_MARKET

    parent_item = await service._order_manager.get_order(parent.order_id)
    assert parent_item["protective_stop_order_id"]["S"] == child.order_id
    assert parent_item["protective_stop_broker_order_id"]["S"] == "nse-1"


@pytest.mark.asyncio
async def test_lost_broker_ack_recovered_by_broker_idempotency_key() -> None:
    from services.execution_engine.retry.retry_handler import RetryHandler

    dynamo = FakeDynamoClient()
    broker = FakeBroker(Market.NSE)

    def accepted_then_timeout(order: Any) -> Any:
        response = OrderResponse(
            order_id=order.order_id,
            broker_order_id="nse-accepted-before-timeout",
            status=OrderStatus.PLACED,
            symbol=order.symbol,
            market=order.market,
        )
        broker._remember(order, response)
        raise RuntimeError("ack lost")

    broker.effects.append(accepted_then_timeout)
    service = _make_service(dynamo=dynamo, nse_broker=broker)
    service._retry_handler = RetryHandler(max_retries=2, base_delay=0.0, max_delay=0.0)

    response = await service.execute_approved_signal(
        signal_id="sig-ack-lost",
        risk_decision_id="risk-ack-lost",
        trace_id="trace-ack-lost",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        quantity=3.0,
        order_type=OrderType.MARKET,
        market=Market.NSE,
    )

    assert response.broker_order_id == "nse-accepted-before-timeout"
    assert len(broker.placed_requests) == 1


@pytest.mark.asyncio
async def test_broker_429_marks_ack_unknown_without_blind_second_order() -> None:
    dynamo = FakeDynamoClient()
    broker = FakeBroker(Market.NSE, effects=[RuntimeError("429 Too Many Requests")])
    service = _make_service(dynamo=dynamo, nse_broker=broker)

    with pytest.raises(RuntimeError, match="ACK_UNKNOWN unresolved"):
        await service.execute_approved_signal(
            signal_id="sig-429",
            risk_decision_id="risk-429",
            trace_id="trace-429",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=3.0,
            order_type=OrderType.MARKET,
            market=Market.NSE,
        )

    assert len(broker.placed_requests) == 1
    stored = await service._order_manager.get_order_by_signal("sig-429")
    assert stored is not None
    assert stored.status == OrderStatus.ACK_UNKNOWN

    with pytest.raises(RuntimeError, match="ACK_UNKNOWN unresolved"):
        await service.execute_approved_signal(
            signal_id="sig-429",
            risk_decision_id="risk-429",
            trace_id="trace-429",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=3.0,
            order_type=OrderType.MARKET,
            market=Market.NSE,
        )

    assert len(broker.placed_requests) == 1


@pytest.mark.asyncio
async def test_duplicate_approved_events_create_exactly_one_broker_order() -> None:
    dynamo = FakeDynamoClient()
    broker = FakeBroker(Market.NSE)
    service = _make_service(dynamo=dynamo, nse_broker=broker)
    approved = _approved_event(signal_id="sig-duplicate")

    assert await service._handle_approved_signal_event(approved) is True
    assert await service._handle_approved_signal_event(approved) is True

    assert len(broker.placed_requests) == 1


@pytest.mark.asyncio
async def test_partial_fill_then_cancel_leaves_correct_exposure() -> None:
    from services.execution_engine.polling.bulk_order_poller import BulkOrderPoller

    dynamo = FakeDynamoClient()
    service = _make_service(dynamo=dynamo)
    await service._order_manager.submit_order(
        OrderRequest(
            signal_id="sig-partial",
            risk_decision_id="risk-partial",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=100.0,
            order_type=OrderType.MARKET,
            market=Market.NSE,
            stop_price=95.0,
        )
    )
    await service._order_manager.record_order(
        OrderResponse(
            order_id=(await service._order_manager.get_order_by_signal("sig-partial")).order_id,
            broker_order_id="nse-partial",
            status=OrderStatus.PLACED,
            symbol="RELIANCE",
            market=Market.NSE,
        )
    )
    order = await service._order_manager.get_order_by_signal("sig-partial")

    poller = BulkOrderPoller(
        zerodha=service._zerodha,
        order_manager=service._order_manager,
        dynamo_client=dynamo,
        fills_table="test-fills",
        rate_limiter=service._nse_rate_limiter,
        protective_stop_callback=service._maybe_place_protective_stop,
    )

    await poller._handle_partial(order, 40.0, 100.0, "partial")
    order_after_partial = await service._order_manager.get_order_by_signal("sig-partial")
    await poller._handle_terminal(order_after_partial, "CANCELLED", "cancelled", 40.0, 100.0)

    pos = dynamo.get_item(
        TableName="test-positions",
        Key={"PK": {"S": "POSITION#RELIANCE"}, "SK": {"S": "CURRENT"}},
    )["Item"]
    final_order = await service._order_manager.get_order_by_signal("sig-partial")

    assert float(pos["quantity"]["N"]) == pytest.approx(40.0)
    assert float(pos["avg_price"]["N"]) == pytest.approx(100.0)
    assert final_order.status == OrderStatus.CANCELLED
    assert final_order.filled_quantity == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_weighted_average_position_and_realized_unrealized_pnl() -> None:
    dynamo = FakeDynamoClient()
    manager = OrderManager(
        dynamo_client=dynamo,
        orders_table="test-orders",
        positions_table="test-positions",
        risk_state_table="test-risk-state",
        settings=_order_manager_settings(),
    )

    await manager.apply_fill_to_position(
        symbol="RELIANCE",
        side=OrderSide.BUY,
        filled_quantity=10.0,
        avg_fill_price=100.0,
        last_price=100.0,
    )
    await manager.apply_fill_to_position(
        symbol="RELIANCE",
        side=OrderSide.BUY,
        filled_quantity=10.0,
        avg_fill_price=120.0,
        last_price=120.0,
    )
    await manager.apply_fill_to_position(
        symbol="RELIANCE",
        side=OrderSide.SELL,
        filled_quantity=5.0,
        avg_fill_price=130.0,
        last_price=130.0,
    )

    pos = dynamo.get_item(
        TableName="test-positions",
        Key={"PK": {"S": "POSITION#RELIANCE"}, "SK": {"S": "CURRENT"}},
    )["Item"]
    assert float(pos["quantity"]["N"]) == pytest.approx(15.0)
    assert float(pos["avg_price"]["N"]) == pytest.approx(110.0)
    assert float(pos["realized_pnl"]["N"]) == pytest.approx(100.0)
    assert float(pos["unrealized_pnl"]["N"]) == pytest.approx(300.0)


@pytest.mark.asyncio
async def test_execution_kill_switch_blocks_new_orders_and_cancels_open_orders() -> None:
    dynamo = FakeDynamoClient()
    broker = FakeBroker(Market.NSE)
    service = _make_service(dynamo=dynamo, nse_broker=broker)

    first = await service.execute_approved_signal(
        signal_id="sig-kill-open",
        risk_decision_id="risk-kill-open",
        trace_id="trace-kill-open",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        quantity=2.0,
        order_type=OrderType.MARKET,
        market=Market.NSE,
    )

    await service._handle_kill_switch_activation(
        reason="unit test halt",
        activated_by="test",
    )

    assert broker.cancelled_order_ids == [first.broker_order_id]

    with pytest.raises(RuntimeError, match="kill switch active"):
        await service.execute_approved_signal(
            signal_id="sig-kill-blocked",
            risk_decision_id="risk-kill-blocked",
            trace_id="trace-kill-blocked",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=1.0,
            order_type=OrderType.MARKET,
            market=Market.NSE,
        )


@pytest.mark.asyncio
async def test_execution_loads_durable_dynamodb_kill_switch_and_cancels_open_orders() -> None:
    dynamo = FakeDynamoClient()
    broker = FakeBroker(Market.NSE)
    service = _make_service(dynamo=dynamo, nse_broker=broker)

    first = await service.execute_approved_signal(
        signal_id="sig-durable-kill-open",
        risk_decision_id="risk-durable-kill-open",
        trace_id="trace-durable-kill-open",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        quantity=2.0,
        order_type=OrderType.MARKET,
        market=Market.NSE,
    )
    dynamo.put_item(
        TableName="test-risk-state",
        Item={
            "PK": {"S": "KILLSWITCH"},
            "SK": {"S": "GLOBAL"},
            "active": {"BOOL": True},
            "status": {"S": "ACTIVE"},
            "reason": {"S": "unit durable halt"},
            "activated_by": {"S": "unit-test"},
        },
    )

    assert await service._refresh_durable_kill_switch_state(
        force=True,
        cancel_open_orders=True,
    ) is True

    assert service._kill_switch_active is True
    assert broker.cancelled_order_ids == [first.broker_order_id]

    with pytest.raises(RuntimeError, match="kill switch active"):
        await service.execute_approved_signal(
            signal_id="sig-durable-kill-blocked",
            risk_decision_id="risk-durable-kill-blocked",
            trace_id="trace-durable-kill-blocked",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=1.0,
            order_type=OrderType.MARKET,
            market=Market.NSE,
        )


@pytest.mark.asyncio
async def test_position_drift_callback_halts_execution_locally() -> None:
    dynamo = FakeDynamoClient()
    broker = FakeBroker(Market.NSE)
    service = _make_service(dynamo=dynamo, nse_broker=broker)

    first = await service.execute_approved_signal(
        signal_id="sig-drift-open",
        risk_decision_id="risk-drift-open",
        trace_id="trace-drift-open",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        quantity=2.0,
        order_type=OrderType.MARKET,
        market=Market.NSE,
    )

    await service._handle_position_drift_local_halt(
        symbol="RELIANCE",
        broker_qty=3.0,
        dynamo_qty=2.0,
    )

    assert service._kill_switch_active is True
    assert broker.cancelled_order_ids == [first.broker_order_id]
