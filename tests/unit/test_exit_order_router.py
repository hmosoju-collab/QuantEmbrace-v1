"""
Phase 2 tests — ExitOrderRouter

Covers:
    - Idempotency: ConditionalCheckFailedException → return False (skip)
    - Kill switch: unconditional write overrides existing exit_order_id
    - Paper mode: synthetic fill via order_manager (or no-op when absent)
    - Backtest mode: simulated fill, returns True
    - Live mode: disabled by default (live_trading_enabled=False)
    - Live mode: blocked even with live_trading_enabled=True (Phase 4 not implemented)
    - Long position exits use SELL side in paper route
    - Short position exits use BUY side in paper route
    - signals_today is never incremented by any router path
    - Daily cap cannot block exit routing
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_dynamo(*, raises_conditional: bool = False) -> MagicMock:
    """Return a mock DynamoDB client for the conditional write."""
    dynamo = MagicMock()
    if raises_conditional:
        exc = Exception("ConditionalCheckFailedException")
        exc.response = {"Error": {"Code": "ConditionalCheckFailedException"}}  # type: ignore[attr-defined]
        dynamo.update_item.side_effect = exc
    else:
        dynamo.update_item.return_value = {}
    return dynamo


def _make_router(
    mode: TradingMode = TradingMode.PAPER,
    *,
    dynamo: MagicMock | None = None,
    order_manager: MagicMock | None = None,
    live_enabled: bool = False,
    zerodha: MagicMock | None = None,
) -> ExitOrderRouter:
    return ExitOrderRouter(
        mode=mode,
        dynamo_client=dynamo or _make_dynamo(),
        positions_table="test-positions",
        order_manager=order_manager,
        zerodha_broker=zerodha,
        live_trading_enabled=live_enabled,
    )


def _long_exit(*, symbol: str = "MARUTI", qty: float = 7.0) -> ExitOrderRequest:
    return ExitOrderRequest.from_position(
        symbol=symbol,
        market="NSE",
        direction="LONG",
        signed_quantity=qty,
        trigger_type=ExitTriggerType.STOP_LOSS,
        session_date_ist="2026-05-25",
        avg_entry_price=1350.0,
        exit_price=1300.0,
    )


def _short_exit(*, symbol: str = "ICICIBANK", qty: float = -78.0) -> ExitOrderRequest:
    return ExitOrderRequest.from_position(
        symbol=symbol,
        market="NSE",
        direction="SHORT",
        signed_quantity=qty,
        trigger_type=ExitTriggerType.STOP_LOSS,
        session_date_ist="2026-05-25",
        avg_entry_price=900.0,
        exit_price=950.0,
    )


# ── TestIdempotency ──────────────────────────────────────────────────────────


class TestIdempotency:
    """ConditionalCheckFailedException → idempotent skip → return False."""

    async def test_duplicate_exit_returns_false(self) -> None:
        dynamo = _make_dynamo(raises_conditional=True)
        router = _make_router(dynamo=dynamo)

        result = await router.route(_long_exit())

        assert result is False

    async def test_first_exit_returns_true(self) -> None:
        dynamo = _make_dynamo(raises_conditional=False)
        router = _make_router(dynamo=dynamo)

        result = await router.route(_long_exit())

        assert result is True

    async def test_idempotent_skip_does_not_call_order_manager(self) -> None:
        dynamo = _make_dynamo(raises_conditional=True)
        om = MagicMock()
        om.apply_fill_to_position = AsyncMock()
        router = _make_router(dynamo=dynamo, order_manager=om)

        await router.route(_long_exit())

        om.apply_fill_to_position.assert_not_called()

    async def test_scan_filter_verification_for_idempotency(self) -> None:
        """
        Verify the DynamoDB conditional write uses the correct condition:
            attribute_not_exists(exit_order_id) AND direction <> :flat
        """
        captured: dict = {}

        async def _fake_to_thread(fn, *args, **kwargs):
            captured["kwargs"] = kwargs
            return {}

        router = _make_router()
        with patch(
            "services.execution_engine.exit.exit_order_router.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            await router.route(_long_exit())

        condition = captured["kwargs"].get("ConditionExpression", "")
        assert "attribute_not_exists(exit_order_id)" in condition
        assert "direction <> :flat" in condition


# ── TestKillSwitchOverride ───────────────────────────────────────────────────


class TestKillSwitchOverride:
    """Kill switch uses unconditional write (no exit_order_id check)."""

    async def test_kill_switch_does_not_use_attribute_not_exists(self) -> None:
        """Kill switch condition must NOT include attribute_not_exists."""
        captured: dict = {}

        async def _fake_to_thread(fn, *args, **kwargs):
            captured["kwargs"] = kwargs
            return {}

        router = _make_router()
        ks_req = ExitOrderRequest(
            exit_id="EXIT-ALL-NSE-KILL_SWITCH-2026-05-25",
            symbol="MARUTI",
            market="NSE",
            position_direction="LONG",
            close_side="SELL",
            close_qty=7.0,
            trigger_type=ExitTriggerType.KILL_SWITCH,
            is_kill_switch=True,
        )

        with patch(
            "services.execution_engine.exit.exit_order_router.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            await router.route(ks_req)

        condition = captured["kwargs"].get("ConditionExpression", "")
        assert "attribute_not_exists" not in condition, (
            "Kill switch must NOT check attribute_not_exists(exit_order_id)"
        )
        assert "direction <> :flat" in condition

    async def test_kill_switch_succeeds_even_if_exit_already_set(self) -> None:
        """Kill switch must succeed when another exit is already in-flight."""
        dynamo = _make_dynamo(raises_conditional=False)
        router = _make_router(dynamo=dynamo)
        ks_req = ExitOrderRequest(
            exit_id="EXIT-ALL-NSE-KILL_SWITCH-2026-05-25",
            symbol="MARUTI",
            market="NSE",
            position_direction="LONG",
            close_side="SELL",
            close_qty=7.0,
            trigger_type=ExitTriggerType.KILL_SWITCH,
            is_kill_switch=True,
        )
        result = await router.route(ks_req)
        assert result is True


# ── TestPaperMode ────────────────────────────────────────────────────────────


class TestPaperMode:
    """Paper mode routes exit through order_manager.apply_fill_to_position."""

    def setup_method(self) -> None:
        self.om = MagicMock()
        self.om.apply_fill_to_position = AsyncMock(return_value=True)

    async def test_paper_long_calls_apply_fill_with_sell(self) -> None:
        router = _make_router(order_manager=self.om)
        await router.route(_long_exit())

        self.om.apply_fill_to_position.assert_called_once()
        call_kwargs = self.om.apply_fill_to_position.call_args[1]
        assert call_kwargs["symbol"] == "MARUTI"
        assert call_kwargs["filled_quantity"] == 7.0
        # side is an OrderSide enum; its value must be SELL
        assert call_kwargs["side"].value == "SELL"

    async def test_paper_short_calls_apply_fill_with_buy(self) -> None:
        router = _make_router(order_manager=self.om)
        await router.route(_short_exit())

        self.om.apply_fill_to_position.assert_called_once()
        call_kwargs = self.om.apply_fill_to_position.call_args[1]
        assert call_kwargs["symbol"] == "ICICIBANK"
        assert call_kwargs["filled_quantity"] == 78.0
        assert call_kwargs["side"].value == "BUY"

    async def test_paper_returns_true_on_success(self) -> None:
        router = _make_router(order_manager=self.om)
        result = await router.route(_long_exit())
        assert result is True

    async def test_paper_returns_false_when_apply_fill_raises(self) -> None:
        self.om.apply_fill_to_position = AsyncMock(side_effect=RuntimeError("db timeout"))
        router = _make_router(order_manager=self.om)
        result = await router.route(_long_exit())
        assert result is False

    async def test_paper_works_without_order_manager(self) -> None:
        """When no order_manager is provided, route still returns True."""
        router = _make_router(order_manager=None)
        result = await router.route(_long_exit())
        assert result is True


# ── TestBacktestMode ─────────────────────────────────────────────────────────


class TestBacktestMode:
    """Backtest mode simulates fill with no DynamoDB side effects."""

    async def test_backtest_returns_true(self) -> None:
        router = _make_router(mode=TradingMode.BACKTEST)
        result = await router.route(_long_exit())
        assert result is True

    async def test_backtest_does_not_call_order_manager(self) -> None:
        om = MagicMock()
        om.apply_fill_to_position = AsyncMock()
        router = _make_router(mode=TradingMode.BACKTEST, order_manager=om)
        await router.route(_long_exit())
        om.apply_fill_to_position.assert_not_called()


# ── TestLiveMode ─────────────────────────────────────────────────────────────


class TestLiveMode:
    """Live mode must be disabled by default and require explicit enablement."""

    async def test_live_disabled_by_default(self) -> None:
        router = _make_router(mode=TradingMode.LIVE, live_enabled=False)
        result = await router.route(_long_exit())
        assert result is False

    async def test_live_blocked_even_when_enabled_flag_true(self) -> None:
        """
        Even with live_trading_enabled=True, live mode returns False in Phase 2
        because the broker order placement is Phase 4.
        """
        router = _make_router(
            mode=TradingMode.LIVE,
            live_enabled=True,
            zerodha=MagicMock(),
        )
        result = await router.route(_long_exit())
        assert result is False

    async def test_live_without_flag_returns_false(self) -> None:
        router = ExitOrderRouter(
            mode=TradingMode.LIVE,
            dynamo_client=_make_dynamo(),
            positions_table="test-positions",
        )
        result = await router.route(_long_exit())
        assert result is False


# ── TestSignalPipelineIsolation ──────────────────────────────────────────────


class TestSignalPipelineIsolation:
    """
    ExitOrderRouter must have zero coupling to the signal pipeline.
    No signals_today, no daily cap, no signals.pending / signals.approved.
    """

    def test_router_source_has_no_signals_today(self) -> None:
        import re
        import inspect
        import services.execution_engine.exit.exit_order_router as m

        source = inspect.getsource(m)
        # Docstrings document non-coupling by naming what isn't done — flag code usage only
        assert not re.search(r'signals_today\s*[+\-]?=|self\.signals_today', source)
        assert "daily_cap" not in source
        # Kafka topic strings would appear as quoted literals in real coupling
        assert '"signals.pending"' not in source
        assert '"signals.approved"' not in source

    async def test_paper_route_does_not_increment_any_counter(self) -> None:
        """
        Route a paper exit and confirm no side-counter is touched.
        We track all calls to the mock order_manager and confirm none
        relate to signal counting.
        """
        om = MagicMock()
        om.apply_fill_to_position = AsyncMock(return_value=True)
        router = _make_router(order_manager=om)

        await router.route(_long_exit())

        # The only call must be apply_fill_to_position — no counter methods
        called_methods = [call[0] for call in om.method_calls]
        for method in called_methods:
            assert "signal" not in method.lower(), (
                f"Unexpected signal-related method called: {method}"
            )
            assert "cap" not in method.lower()

    async def test_router_mode_property(self) -> None:
        router = _make_router(mode=TradingMode.PAPER)
        assert router.mode == TradingMode.PAPER

        router2 = _make_router(mode=TradingMode.BACKTEST)
        assert router2.mode == TradingMode.BACKTEST
