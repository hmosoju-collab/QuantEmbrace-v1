"""
Regression tests for Bug 6 — MIS NAV corruption (Session 17, 2026-06-12).

Root cause:
    loss_validator.record_fill does a put_item (full replace) on NAV#CURRENT.
    The old _write_nav_snapshot used a cash-flow accumulator that read
    realized_cash_flow from the same item; after the put_item that field is
    gone, so old_cash = 0.  A BUY-to-close-short then wrote:
        portfolio_value = seed + (0 − close_notional) = seed − close_notional
    producing the observed value 932,682 = 1,000,000 − 67,317.62.

Fix:
    _write_nav_snapshot now uses a realized-pnl-delta ADD:
        SET portfolio_value = if_not_exists(portfolio_value, :opening_nav) + :delta
    where :delta = realized_pnl_after − realized_pnl_before.
    Entry fills have delta ≈ 0 and are skipped entirely (no DynamoDB write).
    Exit fills add exactly the trade P&L, regardless of what put_item events
    have occurred between the entry fill and the exit.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

# ── path bootstrap ────────────────────────────────────────────────────────────

def _setup() -> None:
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


_setup()

from execution_engine.orders.order import OrderSide  # noqa: E402
from execution_engine.orders.order_manager import OrderManager  # noqa: E402


# ── minimal DynamoDB stub ────────────────────────────────────────────────────

class _TwoTableDynamo:
    """
    Minimal stub for two DynamoDB tables: positions and risk-state.

    The put_item on the risk-state table models the loss_validator overwrite
    that deletes realized_cash_flow — the exact condition that triggered Bug 6.
    """

    def __init__(self, seed_nav: float = 1_000_000.0) -> None:
        self._positions: dict[str, dict[str, Any]] = {}
        # Start with an item that has portfolio_value set by loss_validator
        # but NO realized_cash_flow (put_item wipes it).
        self._risk_state: dict[str, dict[str, Any]] = {
            "NAV#CURRENT:STATE": {
                "PK": {"S": "NAV#CURRENT"},
                "SK": {"S": "STATE"},
                "portfolio_value": {"N": str(seed_nav)},
                "opening_nav": {"N": str(seed_nav)},
                "realized_pnl_today": {"N": "0"},
                # NB: realized_cash_flow intentionally absent — loss_validator
                # put_item does not write this field.
            }
        }

    # -- write helpers ---------------------------------------------------------

    def put_item(self, *, TableName: str, Item: dict[str, Any], **_: Any) -> dict:
        key = f"{Item['PK']['S']}:{Item['SK']['S']}"
        if "positions" in TableName:
            self._positions[key] = dict(Item)
        else:
            self._risk_state[key] = dict(Item)
        return {}

    def update_item(
        self,
        *,
        TableName: str,
        Key: dict[str, Any],
        UpdateExpression: str,
        ExpressionAttributeValues: dict[str, Any],
        ConditionExpression: str = "",
        ReturnValues: str = "",
        **_: Any,
    ) -> dict:
        pk = Key["PK"]["S"]
        sk = Key.get("SK", {}).get("S", "META")
        tbl_key = f"{pk}:{sk}"

        if "positions" in TableName:
            store = self._positions
        else:
            store = self._risk_state

        item = store.setdefault(tbl_key, {"PK": {"S": pk}, "SK": {"S": sk}})

        # NAV write: SET ... portfolio_value = if_not_exists(portfolio_value, :opening_nav) + :delta
        if ":delta" in ExpressionAttributeValues and "positions" not in TableName:
            opening_nav = float(
                ExpressionAttributeValues.get(":opening_nav", {"N": "1000000"})["N"]
            )
            current = float(item.get("portfolio_value", {"N": str(opening_nav)})["N"])
            delta = float(ExpressionAttributeValues[":delta"]["N"])
            item["portfolio_value"] = {"N": str(round(current + delta, 6))}
            if ":ts" in ExpressionAttributeValues:
                item["last_fill_at"] = ExpressionAttributeValues[":ts"]
                item["updated_at"] = ExpressionAttributeValues[":ts"]
            return {}

        # Position write: generic field mapping
        for expr_key, field in {
            ":qty": None,   # handled specially
            ":avg": None,
            ":cost": "cost_basis",
            ":last": "last_price",
            ":realized": "realized_pnl",
            ":unrealized": "unrealized_pnl",
            ":sym": "symbol",
            ":direction": "direction",
            ":product": "product",
            ":ts": "updated_at",
        }.items():
            if expr_key not in ExpressionAttributeValues:
                continue
            if expr_key == ":qty":
                item["quantity"] = ExpressionAttributeValues[":qty"]
                item["confirmed_quantity"] = ExpressionAttributeValues[":qty"]
            elif expr_key == ":avg":
                item["avg_price"] = ExpressionAttributeValues[":avg"]
                item["avg_entry_price"] = ExpressionAttributeValues[":avg"]
            elif field:
                item[field] = ExpressionAttributeValues[expr_key]

        if ConditionExpression and "updated_at" in ConditionExpression:
            pass  # simplified — skip optimistic concurrency for test

        return {"Attributes": item} if ReturnValues else {}

    # -- read helper -----------------------------------------------------------

    def get_item(self, *, TableName: str, Key: dict[str, Any], **_: Any) -> dict:
        pk = Key["PK"]["S"]
        sk = Key.get("SK", {}).get("S", "META")
        tbl_key = f"{pk}:{sk}"
        store = self._positions if "positions" in TableName else self._risk_state
        item = store.get(tbl_key)
        return {"Item": item} if item else {}

    # -- inspection helpers ----------------------------------------------------

    def nav_portfolio_value(self) -> float:
        item = self._risk_state.get("NAV#CURRENT:STATE", {})
        return float(item.get("portfolio_value", {"N": "0"})["N"])

    def position_realized_pnl(self, symbol: str) -> float:
        item = self._positions.get(f"POSITION#{symbol}:CURRENT", {})
        return float(item.get("realized_pnl", {"N": "0"})["N"])


def _settings(seed_nav: float = 1_000_000.0) -> Any:
    return SimpleNamespace(
        portfolio_value=seed_nav,
        aws=SimpleNamespace(
            dynamodb_table_orders="orders",
            dynamodb_table_positions="test-positions",
            dynamodb_table_risk_state="test-risk-state",
        ),
    )


def _make_manager(dynamo: _TwoTableDynamo, seed_nav: float = 1_000_000.0) -> OrderManager:
    return OrderManager(
        dynamo_client=dynamo,
        orders_table="orders",
        positions_table="test-positions",
        risk_state_table="test-risk-state",
        settings=_settings(seed_nav),
    )


# ── tests ────────────────────────────────────────────────────────────────────

class TestBug6MISNavCorruption:
    """
    Verify that MIS paper-close fills do NOT corrupt the NAV item after
    loss_validator's put_item has wiped realized_cash_flow.
    """

    @pytest.mark.asyncio
    async def test_buy_to_close_short_does_not_subtract_full_notional(self) -> None:
        """
        Session-17 Bug 6 exact scenario.

        A SHORT position (avg_entry=220) is MIS-closed with BUY at 224.39.
        The realized P&L is (220 - 224.39) × 300 = −1,317.

        Old (buggy) code: nav = seed − (224.39 × 300) = seed − 67,317 = 932,683.
        Fixed code:       nav = seed − 1,317 = 998,683.
        """
        seed = 1_000_000.0
        dynamo = _TwoTableDynamo(seed_nav=seed)
        mgr = _make_manager(dynamo, seed_nav=seed)

        # Seed the position as SHORT 300 @ 220 (simulates prior SELL entry).
        # We call apply_fill_to_position for the SELL entry first.
        await mgr.apply_fill_to_position(
            symbol="ADANIPORTS",
            side=OrderSide.SELL,
            filled_quantity=300.0,
            avg_fill_price=220.0,
            last_price=220.0,
            market_str="NSE",
        )

        # loss_validator put_item fires here (simulates the real race).
        # It resets portfolio_value to seed + realized_today (= seed + 0 at entry)
        # and does NOT write realized_cash_flow.
        dynamo._risk_state["NAV#CURRENT:STATE"] = {
            "PK": {"S": "NAV#CURRENT"},
            "SK": {"S": "STATE"},
            "portfolio_value": {"N": str(seed)},   # loss_validator reset
            "opening_nav":     {"N": str(seed)},
            "realized_pnl_today": {"N": "0"},
            # realized_cash_flow intentionally absent
        }

        # MIS closes the SHORT with a BUY at 224.39.
        close_price = 224.39
        await mgr.apply_fill_to_position(
            symbol="ADANIPORTS",
            side=OrderSide.BUY,
            filled_quantity=300.0,
            avg_fill_price=close_price,
            last_price=close_price,
            market_str="NSE",
        )

        realized_pnl = (220.0 - close_price) * 300.0  # ≈ −1_317

        nav = dynamo.nav_portfolio_value()
        expected = seed + realized_pnl  # ≈ 998_683

        # The buggy code gave 932,682.38 (seed − close_notional).
        # The fixed code must give seed + realized_pnl.
        assert abs(nav - expected) < 1.0, (
            f"NAV={nav:.2f} — expected ~{expected:.2f} (seed + realized_pnl). "
            f"Seed={seed}, close_notional={close_price * 300:.2f}, "
            f"realized_pnl={realized_pnl:.2f}"
        )
        # Explicitly verify the old corrupted value is NOT produced.
        buggy_nav = seed - (close_price * 300.0)
        assert abs(nav - buggy_nav) > 100.0, (
            f"NAV={nav:.2f} is suspiciously close to the buggy value {buggy_nav:.2f}"
        )

    @pytest.mark.asyncio
    async def test_sell_to_close_long_adds_pnl_not_full_notional(self) -> None:
        """
        Closing a LONG position via MIS SELL should add realized_pnl to NAV,
        not add the full sale notional (which would inflate NAV by the entry cost).
        """
        seed = 1_000_000.0
        dynamo = _TwoTableDynamo(seed_nav=seed)
        mgr = _make_manager(dynamo, seed_nav=seed)

        await mgr.apply_fill_to_position(
            symbol="M&M",
            side=OrderSide.BUY,
            filled_quantity=200.0,
            avg_fill_price=1850.0,
            last_price=1850.0,
            market_str="NSE",
        )

        # Simulate loss_validator put_item reset.
        dynamo._risk_state["NAV#CURRENT:STATE"] = {
            "PK": {"S": "NAV#CURRENT"},
            "SK": {"S": "STATE"},
            "portfolio_value": {"N": str(seed)},
            "opening_nav":     {"N": str(seed)},
            "realized_pnl_today": {"N": "0"},
        }

        # MIS closes LONG with SELL at 1907 (+₹57/share × 200).
        close_price = 1907.0
        await mgr.apply_fill_to_position(
            symbol="M&M",
            side=OrderSide.SELL,
            filled_quantity=200.0,
            avg_fill_price=close_price,
            last_price=close_price,
            market_str="NSE",
        )

        realized_pnl = (close_price - 1850.0) * 200.0  # +11,400

        nav = dynamo.nav_portfolio_value()
        expected = seed + realized_pnl  # 1,011,400

        # Old buggy value would be seed + full_notional (seed + 381,400 ≈ 1,381,400)
        # which is obviously wrong.
        assert abs(nav - expected) < 1.0, (
            f"NAV={nav:.2f}, expected={expected:.2f}"
        )
        buggy_nav = seed + (close_price * 200.0)
        assert nav < buggy_nav - 1.0, (
            f"NAV appears to include full close notional (buggy): {nav:.2f}"
        )

    @pytest.mark.asyncio
    async def test_entry_fill_does_not_update_nav(self) -> None:
        """
        Entry fills (delta=0) must not write to the NAV item at all.
        The risk_engine's loss_validator is the sole writer on entry.
        """
        seed = 1_000_000.0
        dynamo = _TwoTableDynamo(seed_nav=seed)
        # Remove the NAV item entirely to detect any spurious write.
        dynamo._risk_state.clear()

        mgr = _make_manager(dynamo, seed_nav=seed)
        await mgr.apply_fill_to_position(
            symbol="BAJFINANCE",
            side=OrderSide.BUY,
            filled_quantity=50.0,
            avg_fill_price=6800.0,
            last_price=6800.0,
            market_str="NSE",
        )

        # NAV item must still be absent — entry fills must not write it.
        assert "NAV#CURRENT:STATE" not in dynamo._risk_state, (
            "Entry fill must not write to the NAV item (loss_validator is the sole writer on entry)"
        )

    @pytest.mark.asyncio
    async def test_multiple_mis_closes_accumulate_correctly(self) -> None:
        """
        Closing two positions in sequence should accumulate P&L correctly
        without each write resetting the other's contribution.
        """
        seed = 1_000_000.0
        dynamo = _TwoTableDynamo(seed_nav=seed)
        mgr = _make_manager(dynamo, seed_nav=seed)

        # Seed two LONG positions.
        for symbol, entry, qty in [("SBIN", 620.0, 500), ("ICICIBANK", 1100.0, 250)]:
            await mgr.apply_fill_to_position(
                symbol=symbol,
                side=OrderSide.BUY,
                filled_quantity=float(qty),
                avg_fill_price=entry,
                last_price=entry,
                market_str="NSE",
            )

        # Simulate loss_validator reset after entries.
        dynamo._risk_state["NAV#CURRENT:STATE"] = {
            "PK": {"S": "NAV#CURRENT"}, "SK": {"S": "STATE"},
            "portfolio_value": {"N": str(seed)}, "opening_nav": {"N": str(seed)},
        }

        # MIS close 1: SBIN @ 632 (+₹12 × 500 = +6,000)
        await mgr.apply_fill_to_position(
            symbol="SBIN",
            side=OrderSide.SELL,
            filled_quantity=500.0,
            avg_fill_price=632.0,
            last_price=632.0,
            market_str="NSE",
        )
        # MIS close 2: ICICIBANK @ 1088 (−₹12 × 250 = −3,000)
        await mgr.apply_fill_to_position(
            symbol="ICICIBANK",
            side=OrderSide.SELL,
            filled_quantity=250.0,
            avg_fill_price=1088.0,
            last_price=1088.0,
            market_str="NSE",
        )

        pnl_sbin    = (632.0  - 620.0)  * 500.0   # +6,000
        pnl_icici   = (1088.0 - 1100.0) * 250.0   # −3,000
        expected_nav = seed + pnl_sbin + pnl_icici  # 1,003,000

        nav = dynamo.nav_portfolio_value()
        assert abs(nav - expected_nav) < 1.0, (
            f"NAV={nav:.2f}, expected={expected_nav:.2f}"
        )
