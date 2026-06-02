from __future__ import annotations

# ruff: noqa: E402
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from botocore.exceptions import ClientError
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICES_DIR = PROJECT_ROOT / "services"
if str(SERVICES_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICES_DIR))

from risk_engine.limits.risk_limits import RiskLimits
from risk_engine.validators.loss_validator import DailyLossValidator
from risk_engine.validators.position_validator import PositionValidator
from shared.models.signal import Direction, Signal
from shared.risk_state import KILL_SWITCH_PK, KILL_SWITCH_SK, POSITION_SK
from shared.utils.helpers import utc_now


class FakeDynamo:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.get_calls: list[dict[str, Any]] = []

    @staticmethod
    def _key_from_item(item: dict[str, Any]) -> tuple[str, str]:
        return item["PK"]["S"], item["SK"]["S"]

    @staticmethod
    def _key_from_key(key: dict[str, Any]) -> tuple[str, str]:
        return key["PK"]["S"], key["SK"]["S"]

    def put_item(
        self,
        TableName: str,
        Item: dict[str, Any],
        ConditionExpression: str | None = None,
    ) -> dict[str, Any]:
        del TableName
        key = self._key_from_item(Item)
        if ConditionExpression == "attribute_not_exists(PK)" and key in self.items:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ConditionalCheckFailedException",
                        "Message": "conditional put failed",
                    }
                },
                "PutItem",
            )
        self.items[key] = Item.copy()
        return {}

    def get_item(self, TableName: str, Key: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        del TableName, kwargs
        self.get_calls.append(Key)
        return {"Item": self.items.get(self._key_from_key(Key))}

    def update_item(
        self,
        TableName: str,
        Key: dict[str, Any],
        UpdateExpression: str,
        ConditionExpression: str,
        ExpressionAttributeValues: dict[str, Any],
    ) -> dict[str, Any]:
        del TableName, UpdateExpression
        key = self._key_from_key(Key)
        item = self.items.get(key)
        expected_risk_id = ExpressionAttributeValues[":risk_id"]["S"]
        if (
            ConditionExpression == "attribute_exists(PK) AND risk_decision_id = :risk_id"
            and (
                item is None
                or item.get("risk_decision_id", {}).get("S") != expected_risk_id
            )
        ):
            raise ClientError(
                {
                    "Error": {
                        "Code": "ConditionalCheckFailedException",
                        "Message": "conditional update failed",
                    }
                },
                "UpdateItem",
            )
        item["publish_status"] = ExpressionAttributeValues[":published"]
        item["published_at"] = ExpressionAttributeValues[":now"]
        item["updated_at"] = ExpressionAttributeValues[":now"]
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"Items": []}

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"Items": []}


def _settings() -> Any:
    return SimpleNamespace(
        portfolio_value=1_000_000.0,
        aws=SimpleNamespace(
            dynamodb_table_orders="orders",
            dynamodb_table_positions="positions",
            dynamodb_table_risk_state="risk-state",
            dynamodb_table_prices="prices",
            s3_bucket="audit",
            region="ap-south-1",
            sns_kill_switch_topic_arn="",
        ),
        risk=SimpleNamespace(
            profile="tiny-live",
            max_position_size_pct=5.0,
            max_total_exposure_pct=20.0,
            max_daily_loss_pct=0.5,
            max_single_order_value=5_000.0,
            max_open_orders=1,
            max_position_per_symbol=100,
            max_concurrent_positions=1,
            max_sector_exposure_pct=20.0,
            allow_leverage=False,
            max_spread_bps=50.0,
            max_order_adv_pct=1.0,
            model_fields_set=set(),
        ),
        health_check_port=8080,
    )


def _signal(*, paper_trade: bool) -> Signal:
    return Signal(
        symbol="RELIANCE",
        market="NSE",
        direction=Direction.BUY,
        quantity=1,
        confidence=0.9,
        strategy_name="test",
        price_at_signal=100.0,
        paper_trade=paper_trade,
    )


@pytest.mark.asyncio
async def test_position_validator_fails_closed_without_dynamo_for_live() -> None:
    validator = PositionValidator(limits=RiskLimits(), dynamo_client=None, settings=_settings())
    result = await validator.validate(_signal(paper_trade=False))
    assert result.approved is False
    assert "LIVE_RISK_DATA_UNAVAILABLE" in result.reason


@pytest.mark.asyncio
async def test_position_validator_warns_only_without_dynamo_for_paper() -> None:
    validator = PositionValidator(limits=RiskLimits(), dynamo_client=None, settings=_settings())
    result = await validator.validate(_signal(paper_trade=True))
    assert result.approved is True
    assert "PAPER_WARN_RISK_DATA_UNAVAILABLE" in result.reason


@pytest.mark.asyncio
async def test_position_validator_reads_current_position_key() -> None:
    dynamo = FakeDynamo()
    dynamo.items[("POSITION#RELIANCE", POSITION_SK)] = {
        "PK": {"S": "POSITION#RELIANCE"},
        "SK": {"S": POSITION_SK},
        "quantity": {"N": "1"},
    }
    validator = PositionValidator(
        limits=RiskLimits(max_single_order_value=10_000.0),
        dynamo_client=dynamo,
        settings=_settings(),
    )
    await validator.validate(_signal(paper_trade=False))
    assert {"PK": {"S": "POSITION#RELIANCE"}, "SK": {"S": POSITION_SK}} in dynamo.get_calls


@pytest.mark.asyncio
async def test_daily_loss_record_fill_updates_realized_pnl_nav_and_is_idempotent() -> None:
    dynamo = FakeDynamo()
    limits = RiskLimits(portfolio_value=1_000_000.0)
    validator = DailyLossValidator(
        limits=limits,
        dynamo_client=dynamo,
        risk_state_table="risk-state",
        settings=_settings(),
    )

    await validator.record_fill(
        order_id="order-1",
        symbol="RELIANCE",
        market="NSE",
        direction="BUY",
        quantity=10,
        price=100,
        fill_id="fill-buy",
    )
    sell_update = await validator.record_fill(
        order_id="order-2",
        symbol="RELIANCE",
        market="NSE",
        direction="SELL",
        quantity=5,
        price=90,
        fill_id="fill-sell",
    )
    duplicate = await validator.record_fill(
        order_id="order-2",
        symbol="RELIANCE",
        market="NSE",
        direction="SELL",
        quantity=5,
        price=90,
        fill_id="fill-sell",
    )

    assert sell_update["realized_delta"] == pytest.approx(-50.0)
    assert duplicate["realized_delta"] == pytest.approx(0.0)
    today = utc_now().date().isoformat()
    assert dynamo.items[(f"PNL_DAY#{today}", "CURRENT")]["realized_pnl"]["N"] == "-50.0"
    assert dynamo.items[("NAV#CURRENT", "STATE")]["portfolio_value"]["N"] == "999950.0"
    assert limits.get_portfolio_value() == pytest.approx(999950.0)


@pytest.mark.asyncio
async def test_risk_decision_reservation_suppresses_replay() -> None:
    from risk_engine.service import RiskDecision, RiskDecisionStatus, RiskEngineService

    dynamo = FakeDynamo()
    service = RiskEngineService.__new__(RiskEngineService)
    service._dynamo = dynamo
    service._settings = _settings()
    decision = RiskDecision(
        risk_decision_id="risk-1",
        signal_id="sig-1",
        status=RiskDecisionStatus.APPROVED,
        reason="ok",
    )

    assert await service._reserve_risk_decision(decision) is True
    assert await service._reserve_risk_decision(decision) is False
    item = dynamo.items[("RISK_DECISION#sig-1", "DECISION")]
    assert item["publish_status"]["S"] == "PENDING"


@pytest.mark.asyncio
async def test_pending_risk_decision_replay_republishes_before_commit() -> None:
    from risk_engine.service import RiskDecision, RiskDecisionStatus, RiskEngineService

    dynamo = FakeDynamo()
    service = RiskEngineService.__new__(RiskEngineService)
    service._dynamo = dynamo
    service._settings = _settings()
    service._kafka_publisher = SimpleNamespace(publish=AsyncMock(return_value={}))
    service._kill_switch_monitor = SimpleNamespace(record_order=MagicMock())
    signal = _signal(paper_trade=False)
    signal.signal_id = "sig-pending"
    signal_event = SimpleNamespace(signal=signal, trace_id="trace-pending")
    decision = RiskDecision(
        risk_decision_id="risk-pending",
        signal_id=signal.signal_id,
        status=RiskDecisionStatus.APPROVED,
        reason="ok",
    )

    assert await service._reserve_risk_decision(decision) is True
    reservation = await service._get_risk_decision_reservation(signal.signal_id)
    await service._handle_existing_approval_reservation(signal_event, reservation)

    service._kafka_publisher.publish.assert_awaited_once_with(
        signal=signal,
        risk_decision_id="risk-pending",
        trace_id="trace-pending",
    )
    service._kill_switch_monitor.record_order.assert_called_once()
    item = dynamo.items[("RISK_DECISION#sig-pending", "DECISION")]
    assert item["publish_status"]["S"] == "PUBLISHED"


@pytest.mark.asyncio
async def test_published_risk_decision_replay_is_suppressed() -> None:
    from risk_engine.service import RiskDecision, RiskDecisionStatus, RiskEngineService

    dynamo = FakeDynamo()
    service = RiskEngineService.__new__(RiskEngineService)
    service._dynamo = dynamo
    service._settings = _settings()
    service._kafka_publisher = SimpleNamespace(publish=AsyncMock(return_value={}))
    service._kill_switch_monitor = SimpleNamespace(record_order=MagicMock())
    signal = _signal(paper_trade=False)
    signal.signal_id = "sig-published"
    signal_event = SimpleNamespace(signal=signal, trace_id="trace-published")
    decision = RiskDecision(
        risk_decision_id="risk-published",
        signal_id=signal.signal_id,
        status=RiskDecisionStatus.APPROVED,
        reason="ok",
    )

    assert await service._reserve_risk_decision(decision) is True
    await service._mark_risk_decision_published(signal.signal_id, "risk-published")
    reservation = await service._get_risk_decision_reservation(signal.signal_id)
    await service._handle_existing_approval_reservation(signal_event, reservation)

    service._kafka_publisher.publish.assert_not_awaited()
    service._kill_switch_monitor.record_order.assert_not_called()


class _DeliveryMsg:
    def topic(self) -> str:
        return "signals.approved"

    def key(self) -> bytes:
        return b"NSE:RELIANCE"

    def partition(self) -> int:
        return 0

    def offset(self) -> int:
        return 12


class _ImmediateProducer:
    def __init__(self, err: Exception | None = None) -> None:
        self.err = err
        self.produced: list[dict[str, Any]] = []

    def produce(self, **kwargs: Any) -> None:
        self.produced.append(kwargs)
        kwargs["on_delivery"](self.err, _DeliveryMsg())

    def poll(self, _timeout: float) -> None:
        return None


@pytest.mark.asyncio
async def test_approved_publisher_waits_for_delivery_ack() -> None:
    from risk_engine.publishers.kafka_approved_publisher import KafkaApprovedPublisher

    publisher = KafkaApprovedPublisher.__new__(KafkaApprovedPublisher)
    publisher._producer = _ImmediateProducer()
    publisher._lock = MagicMock()
    publisher._lock.__enter__.return_value = None
    publisher._lock.__exit__.return_value = None
    publisher._pending_count = 0
    publisher._delivery_timeout_seconds = 0.5
    signal = _signal(paper_trade=False)
    signal.signal_id = "sig-delivered"

    event = await publisher.publish(signal, risk_decision_id="risk-delivered")

    assert event["signal_id"] == "sig-delivered"
    assert publisher._pending_count == 0


@pytest.mark.asyncio
async def test_approved_publisher_raises_on_delivery_failure() -> None:
    from risk_engine.publishers.kafka_approved_publisher import KafkaApprovedPublisher

    publisher = KafkaApprovedPublisher.__new__(KafkaApprovedPublisher)
    publisher._producer = _ImmediateProducer(err=RuntimeError("broker rejected"))
    publisher._lock = MagicMock()
    publisher._lock.__enter__.return_value = None
    publisher._lock.__exit__.return_value = None
    publisher._pending_count = 0
    publisher._delivery_timeout_seconds = 0.5
    signal = _signal(paper_trade=False)
    signal.signal_id = "sig-failed"

    with pytest.raises(RuntimeError, match="SIGNAL_APPROVED delivery failed"):
        await publisher.publish(signal, risk_decision_id="risk-failed")


def test_canonical_kill_switch_key_constants() -> None:
    assert KILL_SWITCH_PK == "KILLSWITCH"
    assert KILL_SWITCH_SK == "GLOBAL"
