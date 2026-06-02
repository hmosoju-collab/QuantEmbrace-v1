"""
BLOCKER-001 regression tests — P&L race condition in _update_daily_pnl_and_nav.

Before the fix the method performed:
    1. get_item  → read current realized_pnl
    2. Python    → realized_today += realized_delta
    3. put_item  → overwrite (no conditional expression)

Two concurrent fills each reading the same initial value would silently drop
one fill's P&L contribution.  The daily loss limit then underestimated realized
losses, potentially allowing trading past the loss cap.

After the fix the method uses:
    update_item ... ADD realized_pnl :delta  (atomic at DynamoDB item level)

These tests verify:
    T01 — _update_daily_pnl_and_nav calls update_item (not get_item + put_item)
    T02 — ReturnValues=ALL_NEW is requested so the true post-update total is used
    T03 — Concurrent fills both contribute to the aggregate (simulated via
           sequential mock calls that prove the ADD accumulates correctly)
    T04 — First fill of the day (item does not exist) initialises correctly
    T05 — Negative delta (loss) is applied correctly
    T06 — record_fill sets the in-process cache to the returned atomic total
    T07 — _update_daily_pnl_and_nav no longer calls get_item
"""

from __future__ import annotations

import asyncio
from datetime import timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch
import pytest


# ---------------------------------------------------------------------------
# Minimal stubs so the module loads without real AWS / shared packages
# ---------------------------------------------------------------------------

def _make_settings(portfolio_value: float = 1_000_000.0) -> MagicMock:
    s = MagicMock()
    s.portfolio_value = portfolio_value
    s.aws.dynamodb_table_orders = "orders"
    s.aws.dynamodb_table_positions = "positions"
    s.aws.dynamodb_table_risk_state = "risk_state"
    return s


def _make_limits() -> MagicMock:
    limits = MagicMock()
    limits.get_portfolio_value.return_value = 1_000_000.0
    limits.get_limit.return_value = 2.0  # 2% max daily loss
    return limits


def _make_nav_key() -> dict:
    return {"PK": {"S": "NAV#CURRENT"}, "SK": {"S": "CURRENT"}}


def _build_validator(dynamo: Any, portfolio_value: float = 1_000_000.0):
    """Build a DailyLossValidator with mocked dependencies."""
    import importlib, sys, types

    # Stub out shared packages so the import doesn't need a real virtualenv
    for mod_name in [
        "shared", "shared.config", "shared.config.settings",
        "shared.logging", "shared.logging.logger",
        "shared.models", "shared.models.signal",
        "shared.risk_state", "shared.utils", "shared.utils.helpers",
        "risk_engine", "risk_engine.limits", "risk_engine.limits.risk_limits",
        "risk_engine.validators", "risk_engine.validators.common",
        "botocore", "botocore.exceptions",
    ]:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)

    # Wire minimal symbols the module references at import time
    from datetime import datetime, timezone as tz
    utc_now_fn = lambda: datetime.now(tz.utc)
    sys.modules["shared.utils.helpers"].utc_now = utc_now_fn
    sys.modules["shared.risk_state"].nav_key = _make_nav_key
    sys.modules["shared.logging.logger"].get_logger = lambda *a, **kw: MagicMock()
    sys.modules["shared.config.settings"].get_settings = lambda: _make_settings(portfolio_value)
    sys.modules["shared.config.settings"].AppSettings = MagicMock

    # ClientError stub
    class _ClientError(Exception):
        def __init__(self, response=None):
            self.response = response or {}
    sys.modules["botocore.exceptions"].ClientError = _ClientError

    # RiskValidationResult / RiskLimits stubs
    class _RVR:
        def __init__(self, **kw): self.__dict__.update(kw)
    sys.modules["risk_engine.limits.risk_limits"].RiskValidationResult = _RVR
    sys.modules["risk_engine.limits.risk_limits"].RiskLimits = MagicMock
    sys.modules["risk_engine.validators.common"].risk_data_unavailable_result = MagicMock()

    # Force re-import
    if "risk_engine.validators.loss_validator" in sys.modules:
        del sys.modules["risk_engine.validators.loss_validator"]

    from risk_engine.validators.loss_validator import DailyLossValidator

    limits = _make_limits()
    settings = _make_settings(portfolio_value)
    validator = DailyLossValidator(
        limits=limits,
        dynamo_client=dynamo,
        risk_state_table="risk_state",
        orders_table="orders",
        positions_table="positions",
        settings=settings,
    )
    return validator


# ---------------------------------------------------------------------------
# T01 — update_item is called, not get_item + put_item
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T01_uses_update_item_not_get_then_put() -> None:
    """_update_daily_pnl_and_nav must call update_item, never get_item."""
    dynamo = MagicMock()
    # update_item returns the atomic total
    dynamo.update_item.return_value = {
        "Attributes": {"realized_pnl": {"N": "-500.0"}}
    }
    validator = _build_validator(dynamo)

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    today_str = datetime.now(timezone.utc).date().isoformat()

    await validator._update_daily_pnl_and_nav(
        today_str=today_str,
        realized_delta=-500.0,
        now_iso=now_iso,
    )

    # update_item must have been called
    assert dynamo.update_item.called, "update_item was not called"

    # get_item must NOT have been called (old race-prone pattern)
    assert not dynamo.get_item.called, (
        "get_item was called — this indicates the old race-prone "
        "read-modify-write pattern is still in use"
    )


# ---------------------------------------------------------------------------
# T02 — ReturnValues=ALL_NEW is requested
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T02_requests_all_new_return_values() -> None:
    """update_item must request ReturnValues=ALL_NEW to get the true atomic total."""
    dynamo = MagicMock()
    dynamo.update_item.return_value = {
        "Attributes": {"realized_pnl": {"N": "-300.0"}}
    }
    validator = _build_validator(dynamo)

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    today_str = datetime.now(timezone.utc).date().isoformat()

    await validator._update_daily_pnl_and_nav(
        today_str=today_str,
        realized_delta=-300.0,
        now_iso=now_iso,
    )

    call_kwargs = dynamo.update_item.call_args[1]
    assert call_kwargs.get("ReturnValues") == "ALL_NEW", (
        f"Expected ReturnValues='ALL_NEW', got {call_kwargs.get('ReturnValues')!r}"
    )


# ---------------------------------------------------------------------------
# T03 — Concurrent fills both contribute (ADD accumulates correctly)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T03_concurrent_fills_both_counted() -> None:
    """
    Simulate two fills arriving concurrently.

    With the old get → put pattern:
        Fill A reads 0, computes -500, writes -500.
        Fill B reads 0, computes -300, writes -300.
        Final: -300  (Fill A is lost!)

    With update_item ADD:
        DynamoDB atomically applies -500 then -300 (or vice versa).
        Final: -800  (both fills counted)

    We simulate this by verifying that the ADD expression carries :delta,
    not a computed absolute value — meaning each call passes only its own
    delta and trusts DynamoDB to accumulate, regardless of call ordering.
    """
    captured_deltas: list[float] = []

    def fake_update_item(**kwargs):
        expr_vals = kwargs.get("ExpressionAttributeValues", {})
        delta = float(expr_vals[":delta"]["N"])
        captured_deltas.append(delta)
        # Simulate DynamoDB accumulating: sum of all deltas so far
        accumulated = sum(captured_deltas)
        return {"Attributes": {"realized_pnl": {"N": str(accumulated)}}}

    dynamo = MagicMock()
    dynamo.update_item.side_effect = fake_update_item
    validator = _build_validator(dynamo)

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    today_str = datetime.now(timezone.utc).date().isoformat()

    # Two fills arrive: -500 and -300
    result_a = await validator._update_daily_pnl_and_nav(
        today_str=today_str, realized_delta=-500.0, now_iso=now_iso
    )
    result_b = await validator._update_daily_pnl_and_nav(
        today_str=today_str, realized_delta=-300.0, now_iso=now_iso
    )

    # Each call passed only its own delta (not a pre-computed absolute total)
    assert captured_deltas == [-500.0, -300.0], (
        f"Expected each fill to pass its own delta; got {captured_deltas}"
    )
    # The second call returns the accumulated total
    assert result_b == -800.0, (
        f"Expected final realized_pnl=-800.0 (both fills counted), got {result_b}"
    )

    # Under the old pattern, result_b would be -300.0 (fill A lost).
    assert result_b != -300.0, (
        "result_b == -300.0 suggests the old overwrite pattern is still in use"
    )


# ---------------------------------------------------------------------------
# T04 — First fill of the day initialises correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T04_first_fill_of_day() -> None:
    """
    DynamoDB ADD on a non-existent item initialises the attribute to 0 then
    applies the delta.  The mock simulates this behaviour.
    """
    dynamo = MagicMock()
    dynamo.update_item.return_value = {
        # DynamoDB would return the delta itself (0 + delta = delta)
        "Attributes": {"realized_pnl": {"N": "-750.0"}}
    }
    validator = _build_validator(dynamo)

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    today_str = datetime.now(timezone.utc).date().isoformat()

    result = await validator._update_daily_pnl_and_nav(
        today_str=today_str,
        realized_delta=-750.0,
        now_iso=now_iso,
    )

    assert result == -750.0
    assert dynamo.update_item.call_count == 1


# ---------------------------------------------------------------------------
# T05 — Loss delta updates limits and returns correct total
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T05_loss_delta_updates_portfolio_value() -> None:
    """After a fill, limits.update_portfolio_value is called with the correct NAV."""
    dynamo = MagicMock()
    dynamo.update_item.return_value = {
        "Attributes": {"realized_pnl": {"N": "-2000.0"}}
    }
    validator = _build_validator(dynamo, portfolio_value=100_000.0)

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    today_str = datetime.now(timezone.utc).date().isoformat()

    result = await validator._update_daily_pnl_and_nav(
        today_str=today_str,
        realized_delta=-2000.0,
        now_iso=now_iso,
    )

    assert result == -2000.0
    # NAV = opening_nav (100_000) + realized (-2000) = 98_000
    validator._limits.update_portfolio_value.assert_called_once_with(98_000.0)


# ---------------------------------------------------------------------------
# T06 — record_fill sets cache to atomic total (not a locally computed value)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T06_record_fill_cache_set_to_atomic_total() -> None:
    """
    After record_fill completes, _cached_realized_pnl must equal the value
    returned by DynamoDB (the true atomic total), not a locally computed sum.

    If two fills run concurrently, each sees its own atomic total from DynamoDB.
    The last one to update the cache wins — but both are correct totals.
    """
    dynamo = MagicMock()

    # Fill dedup: first put_item succeeds (no ConditionalCheckFailedException)
    from botocore.exceptions import ClientError
    dynamo.put_item.return_value = {}

    # _apply_fill_to_daily_symbol_pnl reads symbol P&L row (empty on first fill)
    dynamo.get_item.return_value = {"Item": None}

    # update_item returns the atomic total
    dynamo.update_item.return_value = {
        "Attributes": {"realized_pnl": {"N": "-1500.0"}}
    }

    # _get_unrealized_pnl scan — empty positions table
    dynamo.scan.return_value = {"Items": []}

    validator = _build_validator(dynamo)

    await validator.record_fill(
        order_id="ord-001",
        symbol="RELIANCE",
        market="NSE",
        direction="SELL",
        quantity=10.0,
        price=2500.0,
        fill_id="fill-001",
    )

    # Cache must reflect the DynamoDB-returned total, not a locally computed value
    assert validator._cached_realized_pnl == -1500.0, (
        f"Expected cache=-1500.0 (atomic DynamoDB total), got {validator._cached_realized_pnl}"
    )


# ---------------------------------------------------------------------------
# T07 — Verify UpdateExpression contains ADD clause for realized_pnl
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T07_update_expression_contains_add_clause() -> None:
    """The UpdateExpression must use ADD (not SET) for realized_pnl."""
    dynamo = MagicMock()
    dynamo.update_item.return_value = {
        "Attributes": {"realized_pnl": {"N": "-100.0"}}
    }
    validator = _build_validator(dynamo)

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    today_str = datetime.now(timezone.utc).date().isoformat()

    await validator._update_daily_pnl_and_nav(
        today_str=today_str,
        realized_delta=-100.0,
        now_iso=now_iso,
    )

    call_kwargs = dynamo.update_item.call_args[1]
    update_expr: str = call_kwargs.get("UpdateExpression", "")

    assert "ADD" in update_expr.upper(), (
        f"UpdateExpression must contain ADD clause; got: {update_expr!r}"
    )
    assert "realized_pnl" in update_expr, (
        f"ADD clause must target realized_pnl; got: {update_expr!r}"
    )
    # Ensure realized_pnl is NOT in a SET clause (that would overwrite, not accumulate)
    set_part = ""
    if "SET" in update_expr.upper():
        # Extract everything between SET and ADD
        upper = update_expr.upper()
        set_start = upper.index("SET") + 3
        add_start = upper.index("ADD") if "ADD" in upper else len(update_expr)
        set_part = update_expr[set_start:add_start]
    assert "realized_pnl" not in set_part, (
        f"realized_pnl must not appear in SET clause (would overwrite); "
        f"SET section: {set_part!r}"
    )
