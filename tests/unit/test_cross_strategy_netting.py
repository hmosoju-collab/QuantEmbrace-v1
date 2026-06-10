"""
Tests for cross-strategy position netting protection.

Covers:
    - Existing SHORT VWAP UNIONBANK + fresh BUY ORB UNIONBANK → rejected
    - Existing LONG ORB + fresh SELL VWAP same symbol → rejected
    - Same-direction entry (adding to existing LONG) → allowed
    - Explicit EXIT order (signal_id starts with EXIT-) is allowed
    - attach_exit_policy blocks overwrite from a different strategy's signal
    - Reconciliation detects ENTRY_UNWOUND_BY_COMPETING_ENTRY
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution_engine.orders.order import OrderSide
from execution_engine.orders.order_manager import OrderManager
from execution_engine.reconciliation.reconciliation import (
    MismatchType,
    PositionReconciliationService,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _settings() -> Any:
    return SimpleNamespace(
        aws=SimpleNamespace(
            dynamodb_table_orders="orders",
            dynamodb_table_positions="positions",
            dynamodb_table_risk_state="risk-state",
        ),
        portfolio_value=1_000_000.0,
    )


def _make_dynamo_position(
    symbol: str,
    qty: float,
    direction: str,
    exit_policy_id: str = "",
) -> dict:
    """Return a minimal DynamoDB positions item."""
    item: dict[str, Any] = {
        "PK": {"S": f"POSITION#{symbol}"},
        "SK": {"S": "LIVE"},
        "symbol": {"S": symbol},
        "quantity": {"N": str(qty)},
        "direction": {"S": direction},
    }
    if exit_policy_id:
        item["exit_policy_id"] = {"S": exit_policy_id}
    return item


class _FakeDynamo:
    """Minimal fake DynamoDB that stores one position item per symbol."""

    def __init__(self, position_item: dict | None = None) -> None:
        self._position = position_item  # raw DynamoDB item or None

    def get_item(self, *, TableName: str, Key: dict, **kwargs: Any) -> dict:
        if "POSITION#" in str(Key) and self._position is not None:
            return {"Item": self._position}
        return {}

    def update_item(self, **kwargs: Any) -> dict:
        return {}

    def scan(self, **kwargs: Any) -> dict:
        return {"Items": []}


def _manager(position_item: dict | None) -> OrderManager:
    dynamo = _FakeDynamo(position_item)
    return OrderManager(dynamo_client=dynamo, settings=_settings())


# ── check_direction_conflict ──────────────────────────────────────────────────

class TestCheckDirectionConflict:
    def test_short_existing_buy_new_conflicts(self) -> None:
        pos = _make_dynamo_position("UNIONBANK", qty=-296.0, direction="SHORT")
        mgr = _manager(pos)
        conflict, existing_dir, existing_qty = asyncio.get_event_loop().run_until_complete(
            mgr.check_direction_conflict("UNIONBANK", OrderSide.BUY)
        )
        assert conflict is True
        assert existing_dir == "SHORT"
        assert existing_qty == -296.0

    def test_long_existing_sell_new_conflicts(self) -> None:
        pos = _make_dynamo_position("BAJAJHLDNG", qty=10.0, direction="LONG")
        mgr = _manager(pos)
        conflict, existing_dir, _ = asyncio.get_event_loop().run_until_complete(
            mgr.check_direction_conflict("BAJAJHLDNG", OrderSide.SELL)
        )
        assert conflict is True
        assert existing_dir == "LONG"

    def test_short_existing_sell_new_no_conflict(self) -> None:
        """Adding to existing short (more sells) is not a conflict."""
        pos = _make_dynamo_position("RELIANCE", qty=-50.0, direction="SHORT")
        mgr = _manager(pos)
        conflict, _, _ = asyncio.get_event_loop().run_until_complete(
            mgr.check_direction_conflict("RELIANCE", OrderSide.SELL)
        )
        assert conflict is False

    def test_long_existing_buy_new_no_conflict(self) -> None:
        """Adding to existing long (more buys) is not a conflict."""
        pos = _make_dynamo_position("INFY", qty=20.0, direction="LONG")
        mgr = _manager(pos)
        conflict, _, _ = asyncio.get_event_loop().run_until_complete(
            mgr.check_direction_conflict("INFY", OrderSide.BUY)
        )
        assert conflict is False

    def test_flat_position_no_conflict(self) -> None:
        """A flat position never conflicts with a new entry."""
        pos = _make_dynamo_position("TCS", qty=0.0, direction="FLAT")
        mgr = _manager(pos)
        conflict, existing_dir, _ = asyncio.get_event_loop().run_until_complete(
            mgr.check_direction_conflict("TCS", OrderSide.BUY)
        )
        assert conflict is False
        assert existing_dir == "FLAT"

    def test_no_existing_position_no_conflict(self) -> None:
        """No position record → no conflict."""
        mgr = _manager(None)
        conflict, _, _ = asyncio.get_event_loop().run_until_complete(
            mgr.check_direction_conflict("HDFCBANK", OrderSide.BUY)
        )
        assert conflict is False

    def test_no_dynamo_client_no_conflict(self) -> None:
        """When dynamo_client is None and fail_open=True (paper), allow through."""
        mgr = OrderManager(dynamo_client=None, settings=_settings())
        conflict, _, _ = asyncio.get_event_loop().run_until_complete(
            mgr.check_direction_conflict("WIPRO", OrderSide.SELL, fail_open=True)
        )
        assert conflict is False

    def test_no_dynamo_client_live_fail_closed(self) -> None:
        """When dynamo_client is None and fail_open=False (live), reject the entry."""
        mgr = OrderManager(dynamo_client=None, settings=_settings())
        conflict, direction, qty = asyncio.get_event_loop().run_until_complete(
            mgr.check_direction_conflict("WIPRO", OrderSide.BUY, fail_open=False)
        )
        assert conflict is True
        assert direction == "POSITION_STORE_UNAVAILABLE"
        assert qty == 0.0

    def test_dynamo_exception_paper_fail_open(self) -> None:
        """DynamoDB raises in paper mode → fail-open (no conflict returned)."""
        class _RaisingDynamo:
            def get_item(self, **kwargs: Any) -> dict:
                raise RuntimeError("DynamoDB unavailable")
            def update_item(self, **kwargs: Any) -> dict:
                return {}

        mgr = OrderManager(dynamo_client=_RaisingDynamo(), settings=_settings())
        conflict, direction, qty = asyncio.get_event_loop().run_until_complete(
            mgr.check_direction_conflict("HDFCBANK", OrderSide.BUY, fail_open=True)
        )
        assert conflict is False
        assert direction == "FLAT"

    def test_dynamo_exception_live_fail_closed(self) -> None:
        """DynamoDB raises in live mode → fail-closed (conflict=True, reason=POSITION_STORE_UNAVAILABLE).
        No broker call, no order record, no position mutation should occur."""
        class _RaisingDynamo:
            def get_item(self, **kwargs: Any) -> dict:
                raise RuntimeError("DynamoDB unavailable")
            def update_item(self, **kwargs: Any) -> dict:
                return {}

        mgr = OrderManager(dynamo_client=_RaisingDynamo(), settings=_settings())
        conflict, direction, qty = asyncio.get_event_loop().run_until_complete(
            mgr.check_direction_conflict("HDFCBANK", OrderSide.BUY, fail_open=False)
        )
        assert conflict is True
        assert direction == "POSITION_STORE_UNAVAILABLE"
        assert qty == 0.0


# ── attach_exit_policy overwrite protection ───────────────────────────────────

class TestAttachExitPolicyOverwriteProtection:

    def _make_mgr_with_existing_policy(
        self, symbol: str, existing_signal_id: str, qty: float = -50.0
    ) -> OrderManager:
        pos = _make_dynamo_position(
            symbol,
            qty=qty,
            direction="SHORT" if qty < 0 else "LONG",
            exit_policy_id=f"POLICY-{existing_signal_id}",
        )
        return _manager(pos)

    def test_blocks_overwrite_from_different_signal(self) -> None:
        mgr = self._make_mgr_with_existing_policy(
            "UNIONBANK",
            existing_signal_id="original-signal-aaa",
        )
        result = asyncio.get_event_loop().run_until_complete(
            mgr.attach_exit_policy(
                "UNIONBANK",
                stop_price=169.0,
                take_profit=167.0,
                policy_id="POLICY-competing-signal-bbb",
                entry_signal_id="competing-signal-bbb",
            )
        )
        assert result is False

    def test_allows_same_signal_reattach(self) -> None:
        """Reattaching the same signal's policy is idempotent — must not block."""
        mgr = self._make_mgr_with_existing_policy(
            "UNIONBANK",
            existing_signal_id="original-signal-aaa",
        )
        result = asyncio.get_event_loop().run_until_complete(
            mgr.attach_exit_policy(
                "UNIONBANK",
                stop_price=169.0,
                take_profit=167.0,
                policy_id="POLICY-original-signal-aaa",
                entry_signal_id="original-signal-aaa",
            )
        )
        assert result is True

    def test_allows_attach_on_flat_position(self) -> None:
        """A flat position (qty=0) should never block an attach."""
        pos = _make_dynamo_position(
            "WIPRO",
            qty=0.0,
            direction="FLAT",
            exit_policy_id="POLICY-old-signal",
        )
        mgr = _manager(pos)
        result = asyncio.get_event_loop().run_until_complete(
            mgr.attach_exit_policy(
                "WIPRO",
                stop_price=500.0,
                entry_signal_id="new-entry-signal",
            )
        )
        assert result is True

    def test_allows_attach_without_entry_signal_id(self) -> None:
        """Callers that do not pass entry_signal_id bypass the guard (TEE path)."""
        mgr = self._make_mgr_with_existing_policy(
            "RELIANCE",
            existing_signal_id="some-signal",
        )
        result = asyncio.get_event_loop().run_until_complete(
            mgr.attach_exit_policy(
                "RELIANCE",
                stop_price=2900.0,
                # no entry_signal_id — TEE-style call, no guard
            )
        )
        assert result is True

    def test_allows_first_attach_no_existing_policy(self) -> None:
        """First attach on a fresh position (no existing policy_id) is always allowed."""
        pos = _make_dynamo_position("HDFCBANK", qty=-66.0, direction="SHORT")
        mgr = _manager(pos)
        result = asyncio.get_event_loop().run_until_complete(
            mgr.attach_exit_policy(
                "HDFCBANK",
                stop_price=760.0,
                entry_signal_id="first-signal-xyz",
            )
        )
        assert result is True


# ── Reconciliation: ENTRY_UNWOUND_BY_COMPETING_ENTRY ─────────────────────────

class TestReconEntryUnwoundByCompetingEntry:

    def _make_recon(self, orders_items: list[dict]) -> PositionReconciliationService:
        class _FakeReconDynamo:
            def scan(self, **kwargs: Any) -> dict:
                if kwargs.get("TableName") == "positions":
                    return {"Items": []}
                # orders scan
                return {"Items": orders_items}

            def get_item(self, **kwargs: Any) -> dict:
                return {}

        return PositionReconciliationService(
            dynamo_client=_FakeReconDynamo(),
            positions_table="positions",
            mode="paper",
        )

    def _make_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        signal_id: str,
        status: str = "FILLED",
    ) -> dict:
        return {
            "PK": {"S": f"ORDER#{signal_id}"},
            "SK": {"S": "META"},
            "symbol": {"S": symbol},
            "side": {"S": side},
            "filled_quantity": {"N": str(qty)},
            "signal_id": {"S": signal_id},
            "order_status": {"S": status},
        }

    def test_detects_competing_entry_unwind(self) -> None:
        """UNIONBANK SHORT from vwap + BUY from orb → flagged."""
        orders = [
            self._make_order("UNIONBANK", "SELL", 296, "vwap-signal-aaa"),
            self._make_order("UNIONBANK", "BUY", 295, "orb-signal-bbb"),
        ]
        recon = self._make_recon(orders)
        mismatches = asyncio.get_event_loop().run_until_complete(
            recon.check_competing_entry_unwind("orders")
        )
        assert len(mismatches) == 1
        assert mismatches[0].mismatch_type == MismatchType.ENTRY_UNWOUND_BY_COMPETING_ENTRY
        assert mismatches[0].symbol == "UNIONBANK"

    def test_explicit_exit_order_is_not_flagged(self) -> None:
        """SELL entry + explicit EXIT BUY (signal_id starts with EXIT-) is clean."""
        orders = [
            self._make_order("HDFCBANK", "SELL", 66, "vwap-signal-ccc"),
            self._make_order("HDFCBANK", "BUY", 66, "EXIT-vwap-signal-ccc"),
        ]
        recon = self._make_recon(orders)
        mismatches = asyncio.get_event_loop().run_until_complete(
            recon.check_competing_entry_unwind("orders")
        )
        assert len(mismatches) == 0

    def test_same_direction_entries_not_flagged(self) -> None:
        """Two SELL entries from different strategies (pyramiding) are not a conflict."""
        orders = [
            self._make_order("IRFC", "SELL", 516, "vwap-signal-ddd"),
            self._make_order("IRFC", "SELL", 200, "momentum-signal-eee"),
        ]
        recon = self._make_recon(orders)
        mismatches = asyncio.get_event_loop().run_until_complete(
            recon.check_competing_entry_unwind("orders")
        )
        assert len(mismatches) == 0

    def test_no_orders_no_mismatches(self) -> None:
        recon = self._make_recon([])
        mismatches = asyncio.get_event_loop().run_until_complete(
            recon.check_competing_entry_unwind("orders")
        )
        assert len(mismatches) == 0

    def test_multiple_symbols_only_conflicting_flagged(self) -> None:
        """Clean symbol (HDFCBANK) is not flagged alongside conflicting (UNIONBANK)."""
        orders = [
            self._make_order("UNIONBANK", "SELL", 296, "vwap-signal-aaa"),
            self._make_order("UNIONBANK", "BUY", 295, "orb-signal-bbb"),
            self._make_order("HDFCBANK", "SELL", 66, "vwap-signal-ccc"),
            self._make_order("HDFCBANK", "BUY", 66, "EXIT-vwap-signal-ccc"),
        ]
        recon = self._make_recon(orders)
        mismatches = asyncio.get_event_loop().run_until_complete(
            recon.check_competing_entry_unwind("orders")
        )
        flagged = [m.symbol for m in mismatches]
        assert "UNIONBANK" in flagged
        assert "HDFCBANK" not in flagged
