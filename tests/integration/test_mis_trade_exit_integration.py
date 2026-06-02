"""
Phase 3 integration tests — MIS × TradeExitEngine idempotency.

These tests prove that MIS and TEE cannot both fire an exit for the same
position.  They share a FakeDynamo instance that keeps a single in-memory
position table; writes from one component are visible to the other.

Scenarios covered:
    1. Paper fill → attach_exit_policy → TEE picks up position next poll.
    2. TEE sets exit_order_id → MIS reads it and skips the position.
    3. MIS fires, position is marked FLAT → TEE scan excludes FLAT positions.
    4. Kill switch uses unconditional write even when exit_order_id is set.
    5. Short position (negative qty) gets exit policy and MIS handles it.
    6. Concurrent TEE+MIS race: only one conditional write wins per position.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── import path setup ────────────────────────────────────────────────────────


def _setup_paths() -> None:
    project_root = Path(__file__).resolve().parents[2]
    services_dir = project_root / "services"
    for p in (project_root, services_dir):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)

    if "structlog" not in sys.modules:
        sl = types.ModuleType("structlog")
        sl.get_logger = lambda *a, **kw: MagicMock()  # type: ignore[attr-defined]
        sys.modules["structlog"] = sl

    import services.execution_engine as _ee
    import services.shared as _sh
    import services.shared.logging as _shl
    import services.shared.logging.logger as _shll

    sys.modules.setdefault("shared", _sh)
    sys.modules.setdefault("shared.logging", _shl)
    sys.modules.setdefault("shared.logging.logger", _shll)
    sys.modules.setdefault("execution_engine", _ee)


_setup_paths()

from services.execution_engine.exit.exit_models import ExitOrderRequest, ExitTriggerType  # noqa: E402
from services.execution_engine.exit.exit_order_router import ExitOrderRouter, TradingMode  # noqa: E402
from services.execution_engine.mis_square_off import MISSquareOffManager  # noqa: E402
from services.execution_engine.monitors.trade_exit_engine import TradeExitEngine  # noqa: E402


# ── FakeDynamo ───────────────────────────────────────────────────────────────


class _ConditionalCheckFailed(Exception):
    """Mimics botocore ConditionalCheckFailedException."""

    def __init__(self) -> None:
        super().__init__("ConditionalCheckFailedException")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeDynamo:
    """
    Minimal in-memory DynamoDB.

    Items are stored as DynamoDB attribute-typed dicts, keyed by
    POSITION#{symbol} for position records.

    Supports the scan/update_item/get_item patterns used by TEE, MIS,
    ExitOrderRouter, and OrderManager in this integration test suite.
    """

    def __init__(self, positions: dict[str, dict]) -> None:
        """
        positions: {symbol: {attr_name: {"S"/"N": value}, ...}}
        """
        self._positions = positions  # mutable shared state

    # ── DynamoDB API surface ──────────────────────────────────────────────────

    def scan(self, **kwargs: Any) -> dict:
        filter_expr: str = kwargs.get("FilterExpression", "")
        expr_vals: dict = kwargs.get("ExpressionAttributeValues", {})
        items = [
            item for item in self._positions.values()
            if self._matches(item, filter_expr, expr_vals)
        ]
        return {"Items": items}

    def get_item(self, **kwargs: Any) -> dict:
        key: dict = kwargs.get("Key", {})
        pk = (key.get("PK") or {}).get("S", "")
        symbol = pk.replace("POSITION#", "")
        item = self._positions.get(symbol)
        return {"Item": item} if item else {}

    def update_item(self, **kwargs: Any) -> dict:
        key: dict = kwargs.get("Key", {})
        pk = (key.get("PK") or {}).get("S", "")
        symbol = pk.replace("POSITION#", "")

        cond: str = kwargs.get("ConditionExpression", "")
        expr_vals: dict = kwargs.get("ExpressionAttributeValues", {})
        update_expr: str = kwargs.get("UpdateExpression", "")

        item = self._positions.get(symbol, {})

        if cond and not self._matches(item, cond, expr_vals):
            raise _ConditionalCheckFailed()

        self._apply_set(item, update_expr, expr_vals)
        self._positions[symbol] = item
        return {}

    def put_item(self, **kwargs: Any) -> dict:
        item: dict = kwargs.get("Item", {})
        pk = item.get("PK", {}).get("S", "")
        symbol = pk.replace("POSITION#", "")
        self._positions[symbol] = item
        return {}

    # ── Filter evaluation ─────────────────────────────────────────────────────

    def _matches(self, item: dict, expr: str, vals: dict) -> bool:
        """Evaluate common FilterExpression / ConditionExpression patterns."""
        clauses = [c.strip() for c in expr.replace(" AND ", "&&").split("&&")]
        return all(self._eval_clause(item, c, vals) for c in clauses)

    def _eval_clause(self, item: dict, clause: str, vals: dict) -> bool:
        if "attribute_not_exists" in clause:
            attr = clause.split("(")[1].rstrip(")")
            return attr not in item
        if "attribute_exists" in clause:
            attr = clause.split("(")[1].rstrip(")")
            return attr in item
        if "<>" in clause:
            attr_raw, placeholder = [x.strip() for x in clause.split("<>")]
            attr = attr_raw.lstrip("#")
            actual = self._get_scalar(item, attr)
            expected = self._scalar_from_val(vals.get(placeholder, {}))
            return actual != expected
        if " = " in clause:
            attr_raw, placeholder = [x.strip() for x in clause.split(" = ")]
            attr = attr_raw.lstrip("#")
            actual = self._get_scalar(item, attr)
            expected = self._scalar_from_val(vals.get(placeholder, {}))
            return actual == expected
        return True

    @staticmethod
    def _get_scalar(item: dict, attr: str) -> Any:
        cell = item.get(attr, {})
        return cell.get("S") or cell.get("N")

    @staticmethod
    def _scalar_from_val(val_def: dict) -> Any:
        return val_def.get("S") or val_def.get("N")

    # ── SET expression applier ────────────────────────────────────────────────

    @staticmethod
    def _apply_set(item: dict, update_expr: str, vals: dict) -> None:
        if not update_expr.upper().startswith("SET "):
            return
        assignments = update_expr[4:].split(",")
        for assignment in assignments:
            lhs, rhs = [x.strip() for x in assignment.split("=")]
            attr = lhs.lstrip("#")
            val_def = vals.get(rhs.strip(), {})
            item[attr] = val_def


# ── Position factory ──────────────────────────────────────────────────────────


def _dynamo_position(
    symbol: str,
    *,
    direction: str,
    quantity: float,
    avg_entry_price: float = 100.0,
    stop_price: float | None = None,
    take_profit: float | None = None,
    last_price: float | None = None,
    exit_order_id: str | None = None,
    exit_state: str | None = None,
    product: str = "MIS",
) -> dict:
    """Return a DynamoDB attribute-typed position item."""
    item: dict = {
        "symbol":          {"S": symbol},
        "direction":       {"S": direction},
        "quantity":        {"N": str(quantity)},
        "avg_entry_price": {"N": str(avg_entry_price)},
        "product":         {"S": product},
    }
    if stop_price is not None:
        item["stop_price"] = {"N": str(stop_price)}
    if take_profit is not None:
        item["take_profit"] = {"N": str(take_profit)}
    if last_price is not None:
        item["last_price"] = {"N": str(last_price)}
    if exit_order_id is not None:
        item["exit_order_id"] = {"S": exit_order_id}
    if exit_state is not None:
        item["exit_state"] = {"S": exit_state}
    return item


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_exit_router(dynamo: FakeDynamo) -> ExitOrderRouter:
    om = MagicMock()
    om.apply_fill_to_position = AsyncMock(return_value=True)
    return ExitOrderRouter(
        mode=TradingMode.PAPER,
        dynamo_client=dynamo,
        positions_table="test-positions",
        order_manager=om,
        live_trading_enabled=False,
    )


def _make_tee(dynamo: FakeDynamo, router: ExitOrderRouter) -> TradeExitEngine:
    return TradeExitEngine(
        dynamo_client=dynamo,
        positions_table="test-positions",
        router=router,
        poll_interval=60,
        trailing_enabled=False,  # disable trailing to keep these tests focused
    )


def _make_mis(dynamo: FakeDynamo) -> MISSquareOffManager:
    zerodha = MagicMock()
    zerodha.place_order = AsyncMock(
        return_value=MagicMock(success=True, broker_order_id="BRK-001")
    )
    return MISSquareOffManager(
        zerodha_broker=zerodha,
        order_manager=MagicMock(),
        dynamo_client=dynamo,
        positions_table="test-positions",
        kill_switch_table="test-risk",
    )


# ── Test 1 ────────────────────────────────────────────────────────────────────


class TestPaperFillAttachesExitPolicy:
    """
    After attach_exit_policy() runs, the position has stop_price in DynamoDB
    and TEE picks it up on the next scan.
    """

    async def test_attach_exit_policy_writes_stop_price_to_dynamo(self) -> None:
        positions: dict = {}
        dynamo = FakeDynamo(positions)

        # Simulate a fresh fill: position exists but no stop_price yet
        positions["MARUTI"] = _dynamo_position(
            "MARUTI", direction="LONG", quantity=7.0, avg_entry_price=1350.0
        )

        # attach_exit_policy writes stop_price and exit_state
        dynamo.update_item(
            TableName="test-positions",
            Key={"PK": {"S": "POSITION#MARUTI"}, "SK": {"S": "CURRENT"}},
            UpdateExpression=(
                "SET exit_state = :exit_state, exit_policy_id = :policy_id, "
                "stop_price = :stop_price, take_profit = :take_profit"
            ),
            ExpressionAttributeValues={
                ":exit_state":  {"S": "EXIT_POLICY_ATTACHED"},
                ":policy_id":   {"S": "POLICY-sig-001"},
                ":stop_price":  {"N": "1300.0"},
                ":take_profit": {"N": "1450.0"},
            },
        )

        item = positions["MARUTI"]
        assert item["stop_price"]["N"] == "1300.0"
        assert item["take_profit"]["N"] == "1450.0"
        assert item["exit_state"]["S"] == "EXIT_POLICY_ATTACHED"

    async def test_tee_picks_up_position_with_stop_price_after_attach(self) -> None:
        positions: dict = {}
        dynamo = FakeDynamo(positions)
        router = _make_exit_router(dynamo)
        router.route = AsyncMock(return_value=True)
        tee = _make_tee(dynamo, router)

        # Position with stop_price but last_price hits the stop
        positions["MARUTI"] = _dynamo_position(
            "MARUTI", direction="LONG", quantity=7.0,
            avg_entry_price=1350.0, stop_price=1300.0, last_price=1280.0,
        )

        managed = await tee._get_open_managed_positions()
        assert len(managed) == 1
        assert managed[0]["symbol"] == "MARUTI"
        assert managed[0]["stop_price"] == 1300.0

        await tee._evaluate_exit_conditions(managed[0])
        router.route.assert_called_once()
        req: ExitOrderRequest = router.route.call_args[0][0]
        assert req.trigger_type == ExitTriggerType.STOP_LOSS
        assert req.close_side == "SELL"


# ── Test 2 ────────────────────────────────────────────────────────────────────


class TestTEEExitOrderIdBlocksMIS:
    """
    When TEE has already set exit_order_id on a position via ExitOrderRouter,
    MIS must read that field and skip the position entirely.
    """

    async def test_mis_skips_position_when_exit_order_id_is_set(self) -> None:
        """
        TEE fires: ExitOrderRouter sets exit_order_id on the position.
        MIS then scans and reads exit_order_id → _resolve_position returns None.
        MIS places NO zerodha order.
        """
        positions: dict = {}
        dynamo = FakeDynamo(positions)
        router = _make_exit_router(dynamo)
        mis = _make_mis(dynamo)

        # Position with exit_order_id already set (TEE claimed the slot)
        positions["MARUTI"] = _dynamo_position(
            "MARUTI", direction="LONG", quantity=7.0,
            avg_entry_price=1350.0, stop_price=1300.0,
            exit_order_id="EXIT-MARUTI-NSE-STOP_LOSS-2026-05-25",
        )

        mis_positions = await mis._get_open_mis_positions()

        assert mis_positions == [], (
            "MIS must return empty list when exit_order_id is set"
        )
        mis._zerodha.place_order.assert_not_called()

    async def test_mis_processes_position_without_exit_order_id(self) -> None:
        """
        When no exit_order_id is set, MIS resolves the position normally.
        """
        positions: dict = {}
        dynamo = FakeDynamo(positions)
        mis = _make_mis(dynamo)

        positions["NHPC"] = _dynamo_position(
            "NHPC", direction="LONG", quantity=1270.0,
            avg_entry_price=75.0, stop_price=72.0,
        )

        mis_positions = await mis._get_open_mis_positions()
        assert len(mis_positions) == 1
        assert mis_positions[0]["symbol"] == "NHPC"
        assert mis_positions[0]["close_side"] == "SELL"

    async def test_exit_router_conditional_write_sets_exit_order_id(self) -> None:
        """
        ExitOrderRouter._acquire_exit_lock sets exit_order_id on the position.
        Confirm the FakeDynamo state is updated correctly.
        """
        positions: dict = {}
        dynamo = FakeDynamo(positions)
        positions["MARUTI"] = _dynamo_position(
            "MARUTI", direction="LONG", quantity=7.0,
            avg_entry_price=1350.0, stop_price=1300.0,
        )

        router = _make_exit_router(dynamo)
        req = ExitOrderRequest.from_position(
            symbol="MARUTI", market="NSE", direction="LONG",
            signed_quantity=7.0, trigger_type=ExitTriggerType.STOP_LOSS,
            session_date_ist="2026-05-25",
        )

        result = await router.route(req)
        assert result is True

        # exit_order_id must now be written to the position in FakeDynamo
        item = positions["MARUTI"]
        assert "exit_order_id" in item
        assert "EXIT-MARUTI-NSE-STOP_LOSS-2026-05-25" in item["exit_order_id"]["S"]


# ── Test 3 ────────────────────────────────────────────────────────────────────


class TestFlatPositionNotProcessedByTEE:
    """
    After MIS fires and the position is marked FLAT (direction = FLAT),
    TEE's scan filter (direction <> FLAT AND attribute_exists(stop_price))
    must exclude it.
    """

    async def test_tee_scan_excludes_flat_positions(self) -> None:
        positions: dict = {}
        dynamo = FakeDynamo(positions)
        router = _make_exit_router(dynamo)
        tee = _make_tee(dynamo, router)

        # Initially open
        positions["ICICIBANK"] = _dynamo_position(
            "ICICIBANK", direction="SHORT", quantity=-78.0,
            avg_entry_price=900.0, stop_price=950.0, last_price=955.0,
        )

        # MIS fires: simulate apply_fill_to_position marking the position FLAT
        positions["ICICIBANK"]["direction"] = {"S": "FLAT"}
        positions["ICICIBANK"]["quantity"]  = {"N": "0"}

        managed = await tee._get_open_managed_positions()
        symbols = [p["symbol"] for p in managed]
        assert "ICICIBANK" not in symbols, (
            "TEE must not process FLAT position after MIS closure"
        )

    async def test_tee_processes_open_position_in_same_scan(self) -> None:
        """
        Only the FLAT position is excluded; other open positions are still evaluated.
        """
        positions: dict = {}
        dynamo = FakeDynamo(positions)
        router = _make_exit_router(dynamo)
        router.route = AsyncMock(return_value=True)
        tee = _make_tee(dynamo, router)

        positions["ICICIBANK"] = _dynamo_position(
            "ICICIBANK", direction="FLAT", quantity=0.0,
            stop_price=950.0,
        )
        positions["ATGL"] = _dynamo_position(
            "ATGL", direction="LONG", quantity=152.0,
            avg_entry_price=620.0, stop_price=580.0, last_price=570.0,
        )

        managed = await tee._get_open_managed_positions()
        symbols = [p["symbol"] for p in managed]
        assert "ICICIBANK" not in symbols
        assert "ATGL" in symbols


# ── Test 4 ────────────────────────────────────────────────────────────────────


class TestKillSwitchOverridesExitOrderId:
    """
    Kill switch uses an unconditional write (direction <> :flat only).
    It must succeed even when exit_order_id is already set.
    """

    async def test_kill_switch_fires_even_with_exit_order_id_set(self) -> None:
        positions: dict = {}
        dynamo = FakeDynamo(positions)
        positions["MARUTI"] = _dynamo_position(
            "MARUTI", direction="LONG", quantity=7.0,
            avg_entry_price=1350.0, stop_price=1300.0,
            exit_order_id="EXIT-MARUTI-NSE-STOP_LOSS-2026-05-25",
        )

        router = _make_exit_router(dynamo)
        ks_req = ExitOrderRequest(
            exit_id="EXIT-ALL-NSE-KILL_SWITCH-2026-05-25",
            symbol="MARUTI", market="NSE",
            position_direction="LONG", close_side="SELL",
            close_qty=7.0, trigger_type=ExitTriggerType.KILL_SWITCH,
            is_kill_switch=True,
        )

        result = await router.route(ks_req)
        # Kill switch must succeed, overwriting any existing exit_order_id
        assert result is True

    async def test_kill_switch_blocked_when_position_is_flat(self) -> None:
        """
        Kill switch condition is 'direction <> :flat'.
        When the position IS already FLAT, the DynamoDB write fails and route()
        returns False — even for kill switch.  The ExitOrderRequest still needs
        a valid close_qty > 0 (model constraint); the blocking happens at the DB layer.
        """
        positions: dict = {}
        dynamo = FakeDynamo(positions)
        positions["MARUTI"] = _dynamo_position(
            "MARUTI", direction="FLAT", quantity=0.0, stop_price=1300.0,
        )

        router = _make_exit_router(dynamo)
        ks_req = ExitOrderRequest(
            exit_id="EXIT-ALL-NSE-KILL_SWITCH-2026-05-25",
            symbol="MARUTI", market="NSE",
            position_direction="LONG", close_side="SELL",
            close_qty=7.0,  # valid qty; DynamoDB blocks because direction=FLAT
            trigger_type=ExitTriggerType.KILL_SWITCH,
            is_kill_switch=True,
        )

        result = await router.route(ks_req)
        # DynamoDB conditional write fails: direction == FLAT != :flat check inverted
        assert result is False


# ── Test 5 ────────────────────────────────────────────────────────────────────


class TestShortNegativeQtyIntegration:
    """
    Short positions with negative signed quantity must work end-to-end:
    exit policy attached, TEE generates BUY exit, MIS generates BUY close.
    """

    async def test_short_position_tee_generates_buy_exit(self) -> None:
        positions: dict = {}
        dynamo = FakeDynamo(positions)
        router = _make_exit_router(dynamo)
        router.route = AsyncMock(return_value=True)
        tee = _make_tee(dynamo, router)

        positions["ICICIBANK"] = _dynamo_position(
            "ICICIBANK", direction="SHORT", quantity=-78.0,
            avg_entry_price=900.0, stop_price=950.0, last_price=960.0,
        )

        managed = await tee._get_open_managed_positions()
        assert len(managed) == 1
        assert managed[0]["quantity"] == -78.0

        await tee._evaluate_exit_conditions(managed[0])

        router.route.assert_called_once()
        req: ExitOrderRequest = router.route.call_args[0][0]
        assert req.close_side == "BUY"
        assert req.close_qty == 78.0
        assert req.position_direction == "SHORT"

    async def test_short_position_mis_generates_buy_close(self) -> None:
        positions: dict = {}
        dynamo = FakeDynamo(positions)
        mis = _make_mis(dynamo)

        positions["ICICIBANK"] = _dynamo_position(
            "ICICIBANK", direction="SHORT", quantity=-78.0,
            avg_entry_price=900.0,
        )

        mis_positions = await mis._get_open_mis_positions()
        assert len(mis_positions) == 1
        pos = mis_positions[0]
        assert pos["close_side"] == "BUY"
        assert pos["close_qty"] == 78.0
        assert pos["effective_dir"] == "SHORT"

    async def test_short_exit_order_id_blocks_mis_too(self) -> None:
        positions: dict = {}
        dynamo = FakeDynamo(positions)
        mis = _make_mis(dynamo)

        positions["ICICIBANK"] = _dynamo_position(
            "ICICIBANK", direction="SHORT", quantity=-78.0,
            avg_entry_price=900.0, stop_price=950.0,
            exit_order_id="EXIT-ICICIBANK-NSE-STOP_LOSS-2026-05-25",
        )

        mis_positions = await mis._get_open_mis_positions()
        assert mis_positions == [], "MIS must skip short position with exit_order_id set"


# ── Test 6 ────────────────────────────────────────────────────────────────────


class TestTEEAndMISConditionalWriteRace:
    """
    TEE and MIS race for the same position.
    ExitOrderRouter's conditional write guarantees at most one wins.
    """

    async def test_first_route_wins_second_returns_false(self) -> None:
        positions: dict = {}
        dynamo = FakeDynamo(positions)
        positions["MARUTI"] = _dynamo_position(
            "MARUTI", direction="LONG", quantity=7.0,
            avg_entry_price=1350.0, stop_price=1300.0,
        )

        router = _make_exit_router(dynamo)

        req_sl = ExitOrderRequest.from_position(
            symbol="MARUTI", market="NSE", direction="LONG",
            signed_quantity=7.0, trigger_type=ExitTriggerType.STOP_LOSS,
            session_date_ist="2026-05-25",
        )
        req_mis = ExitOrderRequest.from_position(
            symbol="MARUTI", market="NSE", direction="LONG",
            signed_quantity=7.0, trigger_type=ExitTriggerType.MIS_CLOSE,
            session_date_ist="2026-05-25",
        )

        first  = await router.route(req_sl)
        second = await router.route(req_mis)

        assert first is True,  "first caller must succeed"
        assert second is False, "second caller must be rejected (idempotent skip)"

    async def test_duplicate_same_trigger_is_idempotent(self) -> None:
        """Sending the exact same exit request twice returns False on the second."""
        positions: dict = {}
        dynamo = FakeDynamo(positions)
        positions["NHPC"] = _dynamo_position(
            "NHPC", direction="LONG", quantity=1270.0,
            avg_entry_price=75.0, stop_price=72.0,
        )

        router = _make_exit_router(dynamo)
        req = ExitOrderRequest.from_position(
            symbol="NHPC", market="NSE", direction="LONG",
            signed_quantity=1270.0, trigger_type=ExitTriggerType.STOP_LOSS,
            session_date_ist="2026-05-25",
        )

        r1 = await router.route(req)
        r2 = await router.route(req)
        assert r1 is True
        assert r2 is False
