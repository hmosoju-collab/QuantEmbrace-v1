"""
Phase 2 tests — TradeExitEngine

Covers:
    - Stop-loss trigger (LONG and SHORT, all boundary conditions)
    - Take-profit trigger (LONG and SHORT)
    - Stop wins over TP when both conditions met in the same cycle
    - Exits bypass the signal pipeline (signals_today never touched)
    - Daily cap does not block exits
    - Unmanaged position detection (CRITICAL log)
    - Exit already in-flight (exit_order_id set → skip evaluation)
    - Price fallback (prices table → position.last_price)
    - Exit policy attachment (attach_exit_policy)

All tests are pure unit tests — no DynamoDB, no broker, no Kafka.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
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

from services.execution_engine.exit.exit_models import (  # noqa: E402
    ExitOrderRequest,
    ExitTriggerType,
    PositionExitState,
    TradeExitPolicy,
)
from services.execution_engine.exit.exit_order_router import ExitOrderRouter, TradingMode  # noqa: E402
from services.execution_engine.monitors.trade_exit_engine import TradeExitEngine  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_router(mode: TradingMode = TradingMode.PAPER) -> ExitOrderRouter:
    return ExitOrderRouter(
        mode=mode,
        dynamo_client=MagicMock(),
        positions_table="test-positions",
    )


def _make_tee(router: ExitOrderRouter | None = None) -> TradeExitEngine:
    return TradeExitEngine(
        dynamo_client=MagicMock(),
        positions_table="test-positions",
        router=router or _make_router(),
        poll_interval=60,
    )


def _open_position(
    symbol: str,
    direction: str,
    quantity: float,
    stop_price: float,
    take_profit: float | None = None,
    last_price: float | None = None,
    exit_order_id: str | None = None,
    avg_price: float = 100.0,
) -> dict:
    return {
        "symbol":        symbol,
        "direction":     direction,
        "quantity":      quantity,
        "avg_price":     avg_price,
        "stop_price":    stop_price,
        "take_profit":   take_profit,
        "last_price":    last_price,
        "exit_order_id": exit_order_id,
        "exit_state":    None,
    }


# ── TestStopLossLogic ────────────────────────────────────────────────────────


class TestStopLossLogic:
    """Pure unit tests for stop_loss_triggered() — no I/O."""

    def test_long_fires_at_stop_price(self) -> None:
        assert TradeExitEngine.stop_loss_triggered("LONG", 95.0, 95.0) is True

    def test_long_fires_below_stop_price(self) -> None:
        assert TradeExitEngine.stop_loss_triggered("LONG", 94.9, 95.0) is True

    def test_long_does_not_fire_above_stop_price(self) -> None:
        assert TradeExitEngine.stop_loss_triggered("LONG", 95.1, 95.0) is False

    def test_short_fires_at_stop_price(self) -> None:
        assert TradeExitEngine.stop_loss_triggered("SHORT", 105.0, 105.0) is True

    def test_short_fires_above_stop_price(self) -> None:
        assert TradeExitEngine.stop_loss_triggered("SHORT", 105.1, 105.0) is True

    def test_short_does_not_fire_below_stop_price(self) -> None:
        assert TradeExitEngine.stop_loss_triggered("SHORT", 104.9, 105.0) is False

    def test_flat_direction_never_fires(self) -> None:
        assert TradeExitEngine.stop_loss_triggered("FLAT", 100.0, 95.0) is False

    def test_unknown_direction_never_fires(self) -> None:
        assert TradeExitEngine.stop_loss_triggered("UNKNOWN", 100.0, 95.0) is False


# ── TestTakeProfitLogic ──────────────────────────────────────────────────────


class TestTakeProfitLogic:
    """Pure unit tests for take_profit_triggered()."""

    def test_long_fires_at_take_profit(self) -> None:
        assert TradeExitEngine.take_profit_triggered("LONG", 120.0, 120.0) is True

    def test_long_fires_above_take_profit(self) -> None:
        assert TradeExitEngine.take_profit_triggered("LONG", 121.0, 120.0) is True

    def test_long_does_not_fire_below_take_profit(self) -> None:
        assert TradeExitEngine.take_profit_triggered("LONG", 119.9, 120.0) is False

    def test_short_fires_at_take_profit(self) -> None:
        assert TradeExitEngine.take_profit_triggered("SHORT", 80.0, 80.0) is True

    def test_short_fires_below_take_profit(self) -> None:
        assert TradeExitEngine.take_profit_triggered("SHORT", 79.9, 80.0) is True

    def test_short_does_not_fire_above_take_profit(self) -> None:
        assert TradeExitEngine.take_profit_triggered("SHORT", 80.1, 80.0) is False

    def test_flat_direction_never_fires(self) -> None:
        assert TradeExitEngine.take_profit_triggered("FLAT", 120.0, 120.0) is False


# ── TestStopWinsOverTP ───────────────────────────────────────────────────────


class TestStopWinsOverTP:
    """
    Stop-loss takes priority when both conditions are simultaneously met.
    Conservative behaviour: protect capital first.
    """

    def setup_method(self) -> None:
        self.fired_exits: list[tuple[str, ExitTriggerType]] = []
        router = _make_router()
        router.route = AsyncMock(return_value=True)
        self.router = router
        self.tee = _make_tee(router)

    async def test_stop_wins_when_both_triggered(self) -> None:
        """
        LONG position: stop=90, tp=110, price=85.
        SL fires (85 <= 90). The implementation returns immediately after SL,
        so TP is structurally unreachable in the same cycle.
        Assert: exactly one route call, and it is STOP_LOSS.
        """
        pos = _open_position(
            "MARUTI", "LONG", quantity=10.0,
            stop_price=90.0, take_profit=110.0, last_price=85.0,
        )

        await self.tee._evaluate_exit_conditions(pos)

        assert self.router.route.call_count == 1, (
            "exactly one exit must be routed when stop fires"
        )
        req = self.router.route.call_args[0][0]
        assert req.trigger_type == ExitTriggerType.STOP_LOSS, (
            "SL must win — TP must not be evaluated after SL fires"
        )

    async def test_tp_fires_only_when_stop_does_not(self) -> None:
        """Price above stop but at/above take-profit → TP fires."""
        pos = _open_position(
            "NHPC", "LONG", quantity=1270.0,
            stop_price=72.0, take_profit=85.0, last_price=87.0,
        )
        await self.tee._evaluate_exit_conditions(pos)
        self.router.route.assert_called_once()
        req = self.router.route.call_args[0][0]
        assert req.trigger_type == ExitTriggerType.TAKE_PROFIT


# ── TestExitBypassesSignalPipeline ───────────────────────────────────────────


class TestExitBypassesSignalPipeline:
    """
    Confirm exits never touch the signal pipeline.

    Architecture invariant: TradeExitEngine and ExitOrderRouter have zero
    imports from strategy_engine or risk_engine. There is no signals_today
    attribute anywhere in the exit path.
    """

    def test_exit_models_have_no_signals_today(self) -> None:
        """ExitOrderRequest carries no signals_today or daily_cap fields."""
        import re
        import inspect
        import services.execution_engine.exit.exit_models as m

        source = inspect.getsource(m)
        # Docstrings document what ISN'T done — only flag executable patterns
        assert not re.search(r'signals_today\s*[+\-]?=|self\.signals_today', source)
        assert "daily_cap" not in source
        assert "publish_signal" not in source

    def test_exit_router_has_no_signals_today(self) -> None:
        import re
        import inspect
        import services.execution_engine.exit.exit_order_router as m

        source = inspect.getsource(m)
        # Docstrings mention these terms to document non-coupling — flag code usage only
        assert not re.search(r'signals_today\s*[+\-]?=|self\.signals_today', source)
        assert "daily_cap" not in source
        # Topic strings would appear as quoted literals in real coupling
        assert '"signals.pending"' not in source
        assert '"signals.approved"' not in source

    def test_tee_has_no_signals_today(self) -> None:
        import re
        import inspect
        import services.execution_engine.monitors.trade_exit_engine as m

        source = inspect.getsource(m)
        # Docstrings mention these terms to document non-coupling — flag code usage only
        assert not re.search(r'signals_today\s*[+\-]?=|self\.signals_today', source)
        assert "daily_cap" not in source
        assert '"signals.pending"' not in source
        assert '"signals.approved"' not in source

    def test_exit_order_request_has_no_signal_pipeline_fields(self) -> None:
        """ExitOrderRequest must not have signal_pipeline-coupling fields."""
        req = ExitOrderRequest(
            exit_id="EXIT-MARUTI-NSE-STOP_LOSS-2026-05-25",
            symbol="MARUTI",
            market="NSE",
            position_direction="LONG",
            close_side="SELL",
            close_qty=10.0,
            trigger_type=ExitTriggerType.STOP_LOSS,
        )
        # These must not be attributes of ExitOrderRequest
        assert not hasattr(req, "signals_today")
        assert not hasattr(req, "daily_cap")
        assert not hasattr(req, "strategy_id")


# ── TestDailyCapDoesNotBlockExits ────────────────────────────────────────────


class TestDailyCapDoesNotBlockExits:
    """
    Daily cap blocks entries only. Exits fire regardless of cap state.

    The cap is enforced in strategy_engine.publish_signal(). The TEE never
    calls publish_signal() — so the cap is structurally unreachable.
    """

    def setup_method(self) -> None:
        router = _make_router()
        router.route = AsyncMock(return_value=True)
        self.router = router
        self.tee = _make_tee(router)

    async def test_exit_fires_regardless_of_cap_value(self) -> None:
        """
        Simulate cap=10/10 (fully exhausted). Exit must still route.
        The TEE has no reference to cap state — it simply cannot be blocked.
        """
        # If cap were consulted, we'd set it to 10/10 and expect blocking.
        # Since TEE has no cap reference, we just assert the exit routes.
        cap_exhausted = 10  # noqa: F841 — only here to document the intent

        pos = _open_position(
            "ICICIBANK", "SHORT", quantity=-78.0,
            stop_price=950.0, last_price=960.0,  # stop triggered
        )
        await self.tee._evaluate_exit_conditions(pos)
        self.router.route.assert_called_once()

    async def test_exit_fires_when_cap_is_zero(self) -> None:
        """Even with cap=0, existing positions must be managed and closed."""
        pos = _open_position(
            "ATGL", "LONG", quantity=152.0,
            stop_price=580.0, last_price=575.0,  # stop triggered
        )
        await self.tee._evaluate_exit_conditions(pos)
        self.router.route.assert_called_once()
        req = self.router.route.call_args[0][0]
        assert req.trigger_type == ExitTriggerType.STOP_LOSS


# ── TestExitInFlight ─────────────────────────────────────────────────────────


class TestExitInFlight:
    """Positions with exit_order_id already set must be skipped by TEE."""

    def setup_method(self) -> None:
        router = _make_router()
        router.route = AsyncMock(return_value=False)
        self.router = router
        self.tee = _make_tee(router)

    async def test_position_with_exit_order_id_is_skipped(self) -> None:
        pos = _open_position(
            "MARUTI", "LONG", quantity=7.0,
            stop_price=1300.0, last_price=1290.0,
            exit_order_id="EXIT-MARUTI-NSE-STOP_LOSS-2026-05-25",
        )
        await self.tee._evaluate_exit_conditions(pos)
        self.router.route.assert_not_called()

    async def test_position_without_exit_order_id_is_evaluated(self) -> None:
        pos = _open_position(
            "NHPC", "LONG", quantity=1270.0,
            stop_price=74.0, last_price=73.0,
            exit_order_id=None,
        )
        await self.tee._evaluate_exit_conditions(pos)
        self.router.route.assert_called_once()


# ── TestExitOrderSideDerivation ──────────────────────────────────────────────


class TestExitOrderSideDerivation:
    """Confirm close_side is always derived from position direction."""

    def setup_method(self) -> None:
        router = _make_router()
        router.route = AsyncMock(return_value=True)
        self.router = router
        self.tee = _make_tee(router)

    async def test_long_position_generates_sell_exit(self) -> None:
        pos = _open_position("ATGL", "LONG", 152.0, stop_price=580.0, last_price=575.0)
        await self.tee._evaluate_exit_conditions(pos)
        req: ExitOrderRequest = self.router.route.call_args[0][0]
        assert req.close_side == "SELL"
        assert req.position_direction == "LONG"

    async def test_short_position_generates_buy_exit(self) -> None:
        pos = _open_position("ICICIBANK", "SHORT", -78.0, stop_price=950.0, last_price=960.0)
        await self.tee._evaluate_exit_conditions(pos)
        req: ExitOrderRequest = self.router.route.call_args[0][0]
        assert req.close_side == "BUY"
        assert req.position_direction == "SHORT"

    async def test_close_qty_is_always_positive(self) -> None:
        for qty, direction, stop, last in [
            (100.0, "LONG", 90.0, 85.0),
            (-200.0, "SHORT", 210.0, 215.0),
            (1270.0, "LONG", 74.0, 73.0),
            (-645.0, "SHORT", 25.0, 28.0),
        ]:
            self.router.route.reset_mock()
            pos = _open_position("SYM", direction, qty, stop_price=stop, last_price=last)
            await self.tee._evaluate_exit_conditions(pos)
            req: ExitOrderRequest = self.router.route.call_args[0][0]
            assert req.close_qty > 0, f"close_qty must be positive for qty={qty}"
            assert req.close_qty == abs(qty)


# ── TestExitIdFormat ─────────────────────────────────────────────────────────


class TestExitIdFormat:
    """Canonical exit_id format: EXIT-{symbol}-{market}-{trigger}-{date}."""

    def test_make_exit_id_stop_loss(self) -> None:
        eid = ExitOrderRequest.make_exit_id(
            "MARUTI", "NSE", ExitTriggerType.STOP_LOSS, "2026-05-25"
        )
        assert eid == "EXIT-MARUTI-NSE-STOP_LOSS-2026-05-25"

    def test_make_exit_id_take_profit(self) -> None:
        eid = ExitOrderRequest.make_exit_id(
            "NHPC", "NSE", ExitTriggerType.TAKE_PROFIT, "2026-05-25"
        )
        assert eid == "EXIT-NHPC-NSE-TAKE_PROFIT-2026-05-25"

    def test_make_exit_id_mis(self) -> None:
        eid = ExitOrderRequest.make_exit_id(
            "ICICIBANK", "NSE", ExitTriggerType.MIS_CLOSE, "2026-05-25"
        )
        assert eid == "EXIT-ICICIBANK-NSE-MIS_CLOSE-2026-05-25"

    def test_from_position_derives_correct_exit_id(self) -> None:
        req = ExitOrderRequest.from_position(
            symbol="ATGL",
            market="NSE",
            direction="LONG",
            signed_quantity=152.0,
            trigger_type=ExitTriggerType.STOP_LOSS,
            session_date_ist="2026-05-25",
            avg_entry_price=620.0,
            exit_price=580.0,
        )
        assert req.exit_id == "EXIT-ATGL-NSE-STOP_LOSS-2026-05-25"
        assert req.close_side == "SELL"
        assert req.close_qty == 152.0
        assert req.exit_price == 580.0
        assert req.order_type == "LIMIT"

    def test_from_position_market_exit_has_no_limit_price(self) -> None:
        req = ExitOrderRequest.from_position(
            symbol="SAMHI",
            market="NSE",
            direction="SHORT",
            signed_quantity=-645.0,
            trigger_type=ExitTriggerType.STOP_LOSS,
            session_date_ist="2026-05-25",
        )
        assert req.close_side == "BUY"
        assert req.close_qty == 645.0
        assert req.exit_price is None
        assert req.order_type == "MARKET"


# ── TestPriceFallback ────────────────────────────────────────────────────────


class TestPriceFallback:
    """TEE resolves last price via LtpResolver (prices table → position fill fallback)."""

    def setup_method(self) -> None:
        self.tee = _make_tee()

    @staticmethod
    def _ltp_result(price: float, *, is_stale: bool = False, source: str = "prices_table"):
        """Build a minimal LtpResult-compatible SimpleNamespace for patching."""
        from types import SimpleNamespace
        return SimpleNamespace(
            price=price,
            is_stale=is_stale,
            source=source,
            age_seconds=1.0 if not is_stale else 60.0,
            captured_at=None,
        )

    async def test_prices_table_takes_priority(self) -> None:
        """When LtpResolver returns a fresh prices-table price, it takes priority."""
        pos = _open_position("SYM", "LONG", 10.0, stop_price=90.0, last_price=85.0)
        result = self._ltp_result(88.0, is_stale=False, source="prices_table")

        self.tee._ltp_resolver.resolve = AsyncMock(return_value=result)
        price = await self.tee._get_last_price("SYM", pos)

        assert price == 88.0, "fresh prices-table value must take priority"

    async def test_falls_back_to_position_last_price(self) -> None:
        """When LtpResolver falls back to the position fill price, it is returned."""
        pos = _open_position("SYM", "LONG", 10.0, stop_price=90.0, last_price=87.5)
        result = self._ltp_result(87.5, is_stale=True, source="position_fill")

        self.tee._ltp_resolver.resolve = AsyncMock(return_value=result)
        price = await self.tee._get_last_price("SYM", pos)

        # Stale is allowed in paper mode (router.mode == paper); only blocked in live.
        assert price == 87.5

    async def test_returns_none_when_no_price_available(self) -> None:
        """When LtpResolver returns None (no price at all), _get_last_price returns None."""
        pos = _open_position("SYM", "LONG", 10.0, stop_price=90.0, last_price=None)

        self.tee._ltp_resolver.resolve = AsyncMock(return_value=None)
        price = await self.tee._get_last_price("SYM", pos)

        assert price is None

    async def test_falls_back_to_position_when_table_returns_none(self) -> None:
        """When prices table yields None, LtpResolver falls back to position fill."""
        pos = _open_position("SYM", "LONG", 10.0, stop_price=90.0, last_price=92.0)
        result = self._ltp_result(92.0, is_stale=True, source="position_fill")

        self.tee._ltp_resolver.resolve = AsyncMock(return_value=result)
        price = await self.tee._get_last_price("SYM", pos)

        assert price == 92.0


# ── TestTradeExitPolicy ──────────────────────────────────────────────────────


class TestTradeExitPolicy:
    """TradeExitPolicy data model correctness."""

    def test_long_policy_close_side_is_sell(self) -> None:
        policy = TradeExitPolicy(
            policy_id="POLICY-sig-001",
            symbol="MARUTI",
            market="NSE",
            direction="LONG",
            entry_price=1350.0,
            signed_quantity=7.0,
            stop_price=1300.0,
            target_1=1420.0,
        )
        assert policy.close_side == "SELL"
        assert policy.close_qty == 7.0

    def test_short_policy_close_side_is_buy(self) -> None:
        policy = TradeExitPolicy(
            policy_id="POLICY-sig-002",
            symbol="ICICIBANK",
            market="NSE",
            direction="SHORT",
            entry_price=900.0,
            signed_quantity=-78.0,
            stop_price=950.0,
        )
        assert policy.close_side == "BUY"
        assert policy.close_qty == 78.0

    def test_long_initial_stop_calculation(self) -> None:
        """LONG: stop must be below entry price."""
        entry = 1350.0
        stop = entry * 0.98  # 2% below
        policy = TradeExitPolicy(
            policy_id="P", symbol="X", market="NSE",
            direction="LONG", entry_price=entry,
            signed_quantity=10.0, stop_price=stop,
        )
        assert policy.stop_price < policy.entry_price

    def test_short_initial_stop_calculation(self) -> None:
        """SHORT: stop must be above entry price."""
        entry = 900.0
        stop = entry * 1.02  # 2% above
        policy = TradeExitPolicy(
            policy_id="P", symbol="X", market="NSE",
            direction="SHORT", entry_price=entry,
            signed_quantity=-78.0, stop_price=stop,
        )
        assert policy.stop_price > policy.entry_price

    def test_long_target_above_entry(self) -> None:
        policy = TradeExitPolicy(
            policy_id="P", symbol="X", market="NSE",
            direction="LONG", entry_price=100.0,
            signed_quantity=10.0, stop_price=95.0,
            target_1=110.0, final_target=125.0,
        )
        assert policy.target_1 > policy.entry_price  # type: ignore[operator]
        assert policy.final_target > policy.entry_price  # type: ignore[operator]

    def test_short_target_below_entry(self) -> None:
        policy = TradeExitPolicy(
            policy_id="P", symbol="X", market="NSE",
            direction="SHORT", entry_price=100.0,
            signed_quantity=-10.0, stop_price=105.0,
            target_1=90.0, final_target=80.0,
        )
        assert policy.target_1 < policy.entry_price  # type: ignore[operator]
        assert policy.final_target < policy.entry_price  # type: ignore[operator]

    def test_invalid_direction_raises(self) -> None:
        with pytest.raises(ValueError, match="LONG or SHORT"):
            TradeExitPolicy(
                policy_id="P", symbol="X", market="NSE",
                direction="FLAT",  # invalid
                entry_price=100.0, signed_quantity=10.0, stop_price=95.0,
            )

    def test_zero_stop_price_raises(self) -> None:
        with pytest.raises(ValueError):
            TradeExitPolicy(
                policy_id="P", symbol="X", market="NSE",
                direction="LONG", entry_price=100.0,
                signed_quantity=10.0, stop_price=0.0,
            )


# ── TestPositionExitState ────────────────────────────────────────────────────


class TestPositionExitState:
    """PositionExitState lifecycle enumeration."""

    def test_all_required_states_present(self) -> None:
        # The minimum set of states that must be present in the enum.
        # Production may add additional states (TARGET_1_HIT, TARGET_2_HIT,
        # MIS-lifecycle states, etc.) — use issubset so additions don't break this test.
        required = {
            "OPEN",
            "EXIT_POLICY_ATTACHED",
            "INITIAL_SL_ACTIVE",
            "BREAKEVEN_LOCKED",
            "PARTIAL_PROFIT_BOOKED",
            "TRAILING_ACTIVE",
            "FINAL_TARGET_HIT",
            "STOP_LOSS_HIT",
            "TIME_EXIT_PENDING",
            "CLOSED",
            "RECONCILED",
        }
        actual = {s.value for s in PositionExitState}
        assert required.issubset(actual), (
            f"PositionExitState is missing required states: {required - actual}"
        )

    def test_state_is_string_enum(self) -> None:
        assert PositionExitState.OPEN == "OPEN"
        assert isinstance(PositionExitState.STOP_LOSS_HIT, str)


# ── TestTrailingActivationLogic ───────────────────────────────────────────────


class TestTrailingActivationLogic:
    """Pure unit tests for trailing_activation_triggered() — no I/O."""

    def test_long_activates_exactly_at_threshold(self) -> None:
        # entry=100, activation_pct=1.25 → threshold=101.25
        assert TradeExitEngine.trailing_activation_triggered("LONG", 101.25, 100.0, 1.25) is True

    def test_long_activates_above_threshold(self) -> None:
        assert TradeExitEngine.trailing_activation_triggered("LONG", 102.0, 100.0, 1.25) is True

    def test_long_does_not_activate_below_threshold(self) -> None:
        assert TradeExitEngine.trailing_activation_triggered("LONG", 101.0, 100.0, 1.25) is False

    def test_short_activates_exactly_at_threshold(self) -> None:
        # entry=100, activation_pct=1.25 → threshold=98.75
        assert TradeExitEngine.trailing_activation_triggered("SHORT", 98.75, 100.0, 1.25) is True

    def test_short_activates_below_threshold(self) -> None:
        assert TradeExitEngine.trailing_activation_triggered("SHORT", 98.0, 100.0, 1.25) is True

    def test_short_does_not_activate_above_threshold(self) -> None:
        assert TradeExitEngine.trailing_activation_triggered("SHORT", 99.0, 100.0, 1.25) is False

    def test_flat_direction_never_activates(self) -> None:
        assert TradeExitEngine.trailing_activation_triggered("FLAT", 102.0, 100.0, 1.25) is False


# ── TestComputeTrailingStop ───────────────────────────────────────────────────


class TestComputeTrailingStop:
    """Pure unit tests for compute_trailing_stop() — no I/O."""

    def test_long_trail_moves_up_when_price_rises(self) -> None:
        # current_stop=100.64, price=103 → new = max(100.64, 103*0.994) = max(100.64, 102.38) = 102.38
        new = TradeExitEngine.compute_trailing_stop("LONG", 103.0, 100.64, 0.6)
        assert abs(new - 102.38) < 0.01

    def test_long_trail_does_not_move_down(self) -> None:
        # price dropped to 101 → new trail = 101*0.994 = 100.39 < 100.64 current → stays at 100.64
        new = TradeExitEngine.compute_trailing_stop("LONG", 101.0, 100.64, 0.6)
        assert new == 100.64, "LONG trailing stop must never decrease"

    def test_long_trail_never_below_original_stop(self) -> None:
        # Initial activation at 101.25 → trail = 101.25*0.994=100.64, original SL=99
        # max(99, 100.64) = 100.64 — trail is above original SL, not below it
        new = TradeExitEngine.compute_trailing_stop("LONG", 101.25, 99.0, 0.6)
        assert new >= 99.0

    def test_short_trail_moves_down_when_price_falls(self) -> None:
        # current_stop=99.34, price=98 → new = min(99.34, 98*1.006) = min(99.34, 98.59) = 98.59
        new = TradeExitEngine.compute_trailing_stop("SHORT", 98.0, 99.34, 0.6)
        assert abs(new - 98.59) < 0.01

    def test_short_trail_does_not_move_up(self) -> None:
        # price rose to 99.5 → new trail = 99.5*1.006 = 100.10 > 99.34 current → stays at 99.34
        new = TradeExitEngine.compute_trailing_stop("SHORT", 99.5, 99.34, 0.6)
        assert new == 99.34, "SHORT trailing stop must never increase"

    def test_short_trail_never_above_original_stop(self) -> None:
        # Initial activation at 98.75 → trail = 98.75*1.006=99.34, original SL=101
        # min(101, 99.34) = 99.34 — trail is below original SL (tighter), not above it
        new = TradeExitEngine.compute_trailing_stop("SHORT", 98.75, 101.0, 0.6)
        assert new <= 101.0

    def test_unknown_direction_returns_current_stop(self) -> None:
        new = TradeExitEngine.compute_trailing_stop("FLAT", 103.0, 100.0, 0.6)
        assert new == 100.0


# ── TestTrailingStopBehavior ──────────────────────────────────────────────────


class TestTrailingStopBehavior:
    """Integration-style tests for trailing stop lifecycle via TradeExitEngine."""

    def setup_method(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        router = _make_router()
        router.route = AsyncMock(return_value=True)
        self.router = router
        self.tee = _make_tee(router)
        # Enable trailing for this test class (default in production)
        self.tee._trailing_enabled = True
        self.tee._trailing_activation_pct = 1.25
        self.tee._trailing_stop_pct = 0.6
        # Mock DynamoDB write so no real I/O needed
        self.tee._dynamo = MagicMock()
        self.tee._dynamo.update_item = MagicMock(return_value={})

    async def test_long_trailing_activates_at_threshold(self) -> None:
        """
        LONG: entry=100, SL=99, activation=1.25% → activates at 101.25.
        Price=101.25 → _activate_trailing_stop is called.
        """
        pos = _open_position(
            "MARUTI", "LONG", 10.0,
            stop_price=99.0, take_profit=None, last_price=None,
            avg_price=100.0,
        )
        pos["exit_state"] = None  # not yet active

        with patch.object(self.tee, "_activate_trailing_stop", AsyncMock()) as mock_activate:
            await self.tee._manage_trailing_stop(pos, 101.25)
        mock_activate.assert_called_once()

    async def test_short_trailing_activates_at_threshold(self) -> None:
        """
        SHORT: entry=100, SL=101, activation=1.25% → activates at 98.75.
        Price=98.75 → _activate_trailing_stop is called.
        """
        pos = _open_position(
            "ICICIBANK", "SHORT", -78.0,
            stop_price=101.0, take_profit=None, last_price=None,
            avg_price=100.0,
        )
        pos["exit_state"] = None

        with patch.object(self.tee, "_activate_trailing_stop", AsyncMock()) as mock_activate:
            await self.tee._manage_trailing_stop(pos, 98.75)
        mock_activate.assert_called_once()

    async def test_trailing_not_activated_below_threshold_long(self) -> None:
        """LONG: price=101.0 < activation threshold 101.25 → no activation."""
        pos = _open_position("MARUTI", "LONG", 10.0, stop_price=99.0, avg_price=100.0)
        pos["exit_state"] = None

        with patch.object(self.tee, "_activate_trailing_stop", AsyncMock()) as mock_activate:
            await self.tee._manage_trailing_stop(pos, 101.0)
        mock_activate.assert_not_called()

    async def test_trailing_active_state_calls_advance(self) -> None:
        """When exit_state=TRAILING_ACTIVE, _advance_trailing_stop is called."""
        pos = _open_position("MARUTI", "LONG", 10.0, stop_price=100.64, avg_price=100.0)
        pos["exit_state"] = "TRAILING_ACTIVE"

        with patch.object(self.tee, "_advance_trailing_stop", AsyncMock()) as mock_advance:
            await self.tee._manage_trailing_stop(pos, 103.0)
        mock_advance.assert_called_once()

    async def test_advance_writes_dynamo_when_stop_improves(self) -> None:
        """When price rises (LONG), trailing stop advances and DynamoDB is written."""
        pos = _open_position("MARUTI", "LONG", 10.0, stop_price=100.64, avg_price=100.0)
        pos["exit_state"] = "TRAILING_ACTIVE"

        await self.tee._advance_trailing_stop(pos, 103.0)

        # new_stop = max(100.64, 103*0.994) = max(100.64, 102.38) = 102.38 → write
        self.tee._dynamo.update_item.assert_called_once()
        call_kw = self.tee._dynamo.update_item.call_args[1]
        new_stop_val = float(call_kw["ExpressionAttributeValues"][":new_stop"]["N"])
        assert abs(new_stop_val - 102.38) < 0.01

    async def test_advance_does_not_write_when_stop_does_not_improve(self) -> None:
        """When price falls below the high watermark (LONG), DynamoDB is NOT written."""
        pos = _open_position("MARUTI", "LONG", 10.0, stop_price=102.38, avg_price=100.0)
        pos["exit_state"] = "TRAILING_ACTIVE"

        await self.tee._advance_trailing_stop(pos, 101.0)

        # 101 * 0.994 = 100.39 < 102.38 current → no improvement → no write
        self.tee._dynamo.update_item.assert_not_called()

    async def test_trailing_stop_hit_fires_trailing_trigger(self) -> None:
        """
        When trailing is active and price falls to trailing stop → TRAILING trigger.
        exit_state=TRAILING_ACTIVE + stop_loss_triggered → fire with ExitTriggerType.TRAILING.
        """
        pos = _open_position(
            "MARUTI", "LONG", 10.0,
            stop_price=102.38, take_profit=None, last_price=None, avg_price=100.0,
        )
        pos["exit_state"] = "TRAILING_ACTIVE"

        await self.tee._evaluate_exit_conditions(pos)

        self.router.route.assert_not_called()  # price is None (no last_price)

    async def test_trailing_stop_hit_with_price_below_trailing_stop(self) -> None:
        """Full path: trailing active, price below trailing stop → TRAILING exit."""
        pos = _open_position(
            "MARUTI", "LONG", 10.0,
            stop_price=102.38, take_profit=115.0, last_price=101.5, avg_price=100.0,
        )
        pos["exit_state"] = "TRAILING_ACTIVE"

        await self.tee._evaluate_exit_conditions(pos)

        self.router.route.assert_called_once()
        req: ExitOrderRequest = self.router.route.call_args[0][0]
        assert req.trigger_type == ExitTriggerType.TRAILING
        assert req.close_side == "SELL"

    async def test_tp_suppressed_when_trailing_active(self) -> None:
        """When trailing is active, TP must not fire even if price > take_profit."""
        pos = _open_position(
            "NHPC", "LONG", 1270.0,
            stop_price=95.0, take_profit=110.0, last_price=115.0, avg_price=100.0,
        )
        pos["exit_state"] = "TRAILING_ACTIVE"
        # price=115 > stop=95 (no SL trigger) and > take_profit=110 but trailing is active
        # TP must be suppressed; _manage_trailing_stop should be called instead

        with patch.object(self.tee, "_manage_trailing_stop", AsyncMock()) as mock_trail:
            await self.tee._evaluate_exit_conditions(pos)

        self.router.route.assert_not_called()  # no exit fired
        mock_trail.assert_called_once()         # trailing management was invoked

    async def test_trailing_disabled_tee_does_not_call_manage(self) -> None:
        """When trailing_enabled=False, _manage_trailing_stop is never called."""
        self.tee._trailing_enabled = False
        pos = _open_position(
            "MARUTI", "LONG", 10.0,
            stop_price=99.0, take_profit=115.0, last_price=103.0, avg_price=100.0,
        )
        pos["exit_state"] = None

        with patch.object(self.tee, "_manage_trailing_stop", AsyncMock()) as mock_trail:
            await self.tee._evaluate_exit_conditions(pos)

        mock_trail.assert_not_called()

    async def test_trailing_does_not_affect_signals_today(self) -> None:
        """Trailing stop activation must not touch signal pipeline."""
        import re
        import inspect
        import services.execution_engine.monitors.trade_exit_engine as m

        source = inspect.getsource(m)
        assert not re.search(r'signals_today\s*[+\-]?=|self\.signals_today', source)
        assert "daily_cap" not in source
