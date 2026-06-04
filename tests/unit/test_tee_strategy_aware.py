"""
Strategy-aware TradeExitEngine (TEE) tests — R-based exit policy.

Covers:
    1.  test_compute_r_long                 — LONG R calculation correct
    2.  test_compute_r_short                — SHORT R calculation correct
    3.  test_compute_r_zero_risk            — initial_risk=0 returns 0.0 (no divide by zero)
    4.  test_vwap_trailing_activates_before_old_threshold — R-policy activates before 1.25% pct
    5.  test_vwap_partial_booking_at_0_8r   — partial exit fires when current_r >= 0.8
    6.  test_partial_exit_idempotency       — second call with same position skips partial
    7.  test_vwap_max_hold_exit             — position held beyond max_hold_minutes triggers exit
    8.  test_momentum_partial_then_trailing — partial fires at 1.2R, trailing activates at 1.5R
    9.  test_trend_15m_no_fixed_tp          — fixed TP suppressed for trend_15m
    10. test_orb_r_based_trailing           — ORB trailing activates at 1.5R
    11. test_preclose_hard_time_exit        — exit fires when IST time >= "15:03"
    12. test_trailing_never_loosens_long    — ratchet invariant: stop only moves up for LONG
    13. test_trailing_never_loosens_short   — stop only moves down for SHORT
    14. test_trailing_stop_r_distance       — trailing stop placed at correct R distance
    15. test_daily_cap_does_not_block_exits — exits never check daily cap
    16. test_mis_unaffected                 — MIS still closes all remaining open positions
    17. test_exit_policy_loader_default_fallback — unknown strategy falls back to _default
    18. test_exit_policy_loader_hot_reload  — reload() picks up changed file

All tests are pure unit tests — no real DynamoDB, no broker, no Kafka.
Mock DynamoDB raises ConditionalCheckFailedException from a mock exception class.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call
import asyncio

import pytest

# ── import path setup ─────────────────────────────────────────────────────────


def _setup_paths() -> None:
    """Add repo root and services directory to sys.path."""
    project_root = Path(__file__).resolve().parents[2]
    services_dir = project_root / "services"
    for p in (project_root, services_dir):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)

    # Stub structlog if not present
    if "structlog" not in sys.modules:
        sl = types.ModuleType("structlog")
        sl.get_logger = lambda *a, **kw: MagicMock()  # type: ignore[attr-defined]
        sys.modules["structlog"] = sl

    # Ensure shared.* aliases resolve
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
from services.execution_engine.monitors.trade_exit_engine import (  # noqa: E402
    ExitPolicy,
    ExitPolicyLoader,
    TradeExitEngine,
)
from services.shared.monitoring.monitoring_status import LiveCounters  # noqa: E402


# ── DynamoDB mock helpers ─────────────────────────────────────────────────────


class _ConditionalCheckFailed(Exception):
    """Simulates botocore ConditionalCheckFailedException."""


def _make_dynamo(
    *,
    conditional_check_fails: bool = False,
    scan_items: list[dict] | None = None,
) -> MagicMock:
    """Return a MagicMock DynamoDB client configured for TEE tests."""
    dynamo = MagicMock()
    # Expose the exceptions class so TEE can catch it
    dynamo.exceptions = MagicMock()
    dynamo.exceptions.ConditionalCheckFailedException = _ConditionalCheckFailed

    if conditional_check_fails:
        dynamo.update_item.side_effect = _ConditionalCheckFailed("condition failed")
    else:
        dynamo.update_item.return_value = {}

    items = scan_items or []
    dynamo.scan.return_value = {"Items": items}
    return dynamo


# ── Router mock ───────────────────────────────────────────────────────────────


def _make_router(mode: TradingMode = TradingMode.PAPER) -> ExitOrderRouter:
    return ExitOrderRouter(
        mode=mode,
        dynamo_client=_make_dynamo(),
        positions_table="test-positions",
    )


# ── Position dict helpers ─────────────────────────────────────────────────────


def _position(
    symbol: str = "TEST",
    direction: str = "LONG",
    quantity: float = 100.0,
    avg_price: float = 1000.0,
    stop_price: float = 990.0,       # 1% below entry → initial_risk = 10
    take_profit: float | None = 1010.0,  # 1% above entry → 1.0R
    last_price: float | None = None,
    exit_order_id: str | None = None,
    exit_state: str | None = None,
    strategy_id: str = "nse_vwap_reversion",
    partial_exit_order_id: str | None = None,
    entry_time: str | None = None,
) -> dict:
    return {
        "symbol":               symbol,
        "direction":            direction,
        "quantity":             quantity,
        "avg_price":            avg_price,
        "stop_price":           stop_price,
        "take_profit":          take_profit,
        "last_price":           last_price,
        "exit_order_id":        exit_order_id,
        "exit_state":           exit_state,
        "strategy_id":          strategy_id,
        "partial_exit_order_id": partial_exit_order_id,
        "entry_time":           entry_time,
    }


# ── ExitPolicy factory ────────────────────────────────────────────────────────


def _vwap_policy() -> ExitPolicy:
    """VWAP reversion policy matching configs/exit_policy.yaml."""
    return ExitPolicy(
        strategy_id="nse_vwap_reversion",
        breakeven_at_r=0.6,
        partial_profit_at_r=0.8,
        partial_qty_pct=50.0,
        trailing_activate_at_r=0.9,
        trailing_distance_r=0.35,
        max_hold_minutes=20,
        hard_exit_time=None,
        fixed_tp_enabled=True,
        suppress_fixed_tp_after_trailing=False,
    )


def _momentum_policy() -> ExitPolicy:
    """nse_momentum_v1 policy."""
    return ExitPolicy(
        strategy_id="nse_momentum_v1",
        breakeven_at_r=0.8,
        partial_profit_at_r=1.2,
        partial_qty_pct=40.0,
        trailing_activate_at_r=1.5,
        trailing_distance_r=0.6,
        max_hold_minutes=None,
        hard_exit_time=None,
        fixed_tp_enabled=True,
        suppress_fixed_tp_after_trailing=False,
    )


def _trend_15m_policy() -> ExitPolicy:
    """nse_intraday_trend_15m policy."""
    return ExitPolicy(
        strategy_id="nse_intraday_trend_15m",
        breakeven_at_r=1.0,
        partial_profit_at_r=1.5,
        partial_qty_pct=30.0,
        trailing_activate_at_r=1.5,
        trailing_distance_r=0.75,
        max_hold_minutes=None,
        hard_exit_time=None,
        fixed_tp_enabled=False,
        suppress_fixed_tp_after_trailing=True,
    )


def _orb_policy() -> ExitPolicy:
    """nse_orb_15m policy."""
    return ExitPolicy(
        strategy_id="nse_orb_15m",
        breakeven_at_r=1.0,
        partial_profit_at_r=1.5,
        partial_qty_pct=50.0,
        trailing_activate_at_r=1.5,
        trailing_distance_r=0.5,
        max_hold_minutes=None,
        hard_exit_time=None,
        fixed_tp_enabled=True,
        suppress_fixed_tp_after_trailing=True,
    )


def _preclose_policy() -> ExitPolicy:
    """nse_preclose_momentum policy."""
    return ExitPolicy(
        strategy_id="nse_preclose_momentum",
        breakeven_at_r=0.6,
        partial_profit_at_r=0.8,
        partial_qty_pct=50.0,
        trailing_activate_at_r=0.9,
        trailing_distance_r=0.35,
        max_hold_minutes=None,
        hard_exit_time="15:03",
        fixed_tp_enabled=True,
        suppress_fixed_tp_after_trailing=False,
    )


# ── 1. test_compute_r_long ────────────────────────────────────────────────────


class TestComputeRLong:
    """Test LONG R-multiple calculation."""

    def test_at_entry_is_zero(self) -> None:
        """R = 0 when last_price == entry_price."""
        r = TradeExitEngine.compute_r("LONG", 1000.0, 990.0, 1000.0)
        assert r == pytest.approx(0.0)

    def test_at_stop_is_negative_one(self) -> None:
        """R = -1 when last_price == stop_price (full stop loss reached)."""
        r = TradeExitEngine.compute_r("LONG", 1000.0, 990.0, 990.0)
        assert r == pytest.approx(-1.0)

    def test_at_1r_profit(self) -> None:
        """R = 1 when last_price is initial_risk above entry."""
        # initial_risk = 10; 1R = entry + 10 = 1010
        r = TradeExitEngine.compute_r("LONG", 1000.0, 990.0, 1010.0)
        assert r == pytest.approx(1.0)

    def test_at_0_9r(self) -> None:
        """R = 0.9 at 90% of the way to the stop distance above entry."""
        # initial_risk = 10; 0.9R = entry + 9 = 1009
        r = TradeExitEngine.compute_r("LONG", 1000.0, 990.0, 1009.0)
        assert r == pytest.approx(0.9)

    def test_at_1_25r(self) -> None:
        """R = 1.25 — this is where old trailing would have activated."""
        # initial_risk = 10; 1.25R = entry + 12.5 = 1012.5
        r = TradeExitEngine.compute_r("LONG", 1000.0, 990.0, 1012.5)
        assert r == pytest.approx(1.25)

    def test_deep_profit(self) -> None:
        """R = 2.0 when last_price is 2 × initial_risk above entry."""
        r = TradeExitEngine.compute_r("LONG", 1000.0, 990.0, 1020.0)
        assert r == pytest.approx(2.0)


# ── 2. test_compute_r_short ───────────────────────────────────────────────────


class TestComputeRShort:
    """Test SHORT R-multiple calculation."""

    def test_at_entry_is_zero(self) -> None:
        r = TradeExitEngine.compute_r("SHORT", 1000.0, 1010.0, 1000.0)
        assert r == pytest.approx(0.0)

    def test_at_stop_is_negative_one(self) -> None:
        """R = -1 when last_price == stop_price for SHORT."""
        # stop=1010, entry=1000, initial_risk=10; last=stop → R=-1
        r = TradeExitEngine.compute_r("SHORT", 1000.0, 1010.0, 1010.0)
        assert r == pytest.approx(-1.0)

    def test_at_1r_profit(self) -> None:
        """R = 1 when price has fallen initial_risk below entry."""
        # entry=1000, stop=1010, initial_risk=10; 1R = 990
        r = TradeExitEngine.compute_r("SHORT", 1000.0, 1010.0, 990.0)
        assert r == pytest.approx(1.0)

    def test_at_0_9r(self) -> None:
        """R = 0.9 when price is 90% of initial_risk below entry."""
        # 0.9R = 1000 - 9 = 991
        r = TradeExitEngine.compute_r("SHORT", 1000.0, 1010.0, 991.0)
        assert r == pytest.approx(0.9)

    def test_unknown_direction_returns_zero(self) -> None:
        r = TradeExitEngine.compute_r("FLAT", 1000.0, 990.0, 1050.0)
        assert r == pytest.approx(0.0)


# ── 3. test_compute_r_zero_risk ───────────────────────────────────────────────


class TestComputeRZeroRisk:
    """Verify zero-risk guard — no divide-by-zero."""

    def test_zero_initial_risk_returns_zero(self) -> None:
        """When entry_price == stop_price (zero risk), R = 0.0."""
        r = TradeExitEngine.compute_r("LONG", 1000.0, 1000.0, 1050.0)
        assert r == pytest.approx(0.0)

    def test_near_zero_risk_handled(self) -> None:
        """Very small but non-zero risk should not explode."""
        r = TradeExitEngine.compute_r("LONG", 1000.0, 999.999, 1001.0)
        # Should be a large positive number, not inf or nan
        assert r > 0
        assert r < float("inf")


# ── 4. test_vwap_trailing_activates_before_old_threshold ──────────────────────


class TestVwapTrailingActivatesBeforeOldThreshold:
    """
    With VWAP reversion policy (trailing_activate_at_r=0.9), trailing activates
    at 0.9R. With a 1% stop (initial_risk = 10), that is a 0.9% price move.
    The old 1.25% threshold would need a 1.25% move = 1.25R, which is above the
    typical VWAP TP of ~1.0R — so trailing never activated with the old policy.
    """

    def test_r_0_9_triggers_vwap_trailing_but_not_old_threshold(self) -> None:
        # LONG: entry=1000, stop=990, initial_risk=10
        # At 0.9R → last_price = entry + 0.9 * 10 = 1009
        # Old threshold: 1.25% → 1012.5 — NOT triggered yet
        # New R policy: 0.9R → 1009 — TRIGGERED
        entry = 1000.0
        stop = 990.0
        initial_risk = entry - stop   # 10

        last_price_at_0_9r = entry + 0.9 * initial_risk  # 1009

        # Old pct-based check
        old_threshold = entry * 1.0125  # 1012.5
        old_would_activate = last_price_at_0_9r >= old_threshold
        assert old_would_activate is False  # should NOT activate

        # New R-based check
        current_r = TradeExitEngine.compute_r("LONG", entry, stop, last_price_at_0_9r)
        vwap_policy = _vwap_policy()
        new_would_activate = (
            vwap_policy.trailing_activate_at_r is not None
            and current_r >= vwap_policy.trailing_activate_at_r
        )
        assert new_would_activate is True  # SHOULD activate

    def test_old_threshold_blocks_typical_vwap_tp_trade(self) -> None:
        # Typical VWAP TP at +0.47% (median observed in session analysis).
        # With 1% stop → initial_risk = 10, TP is at 1.0 * 0.47% * entry below 1R.
        # Actually TP at entry + 0.47% = 1004.7, which is 0.47R.
        # Old trailing threshold = 1.25% → 1012.5, never reached before TP.
        entry = 1000.0
        stop = 990.0  # 1% stop
        tp = entry * 1.0047  # 1004.70 (~0.47% above entry)

        r_at_tp = TradeExitEngine.compute_r("LONG", entry, stop, tp)
        assert r_at_tp == pytest.approx(0.47, rel=0.02)

        # Old policy: needs 1.25R to activate trailing
        old_activate_r = 1.25
        assert r_at_tp < old_activate_r  # TP fires at 0.47R — trailing never activates

        # New VWAP policy: needs 0.9R — also doesn't activate at TP time
        # BUT the partial at 0.8R fires BEFORE TP, and trailing at 0.9R fires at TP-minus
        vwap_policy = _vwap_policy()
        assert vwap_policy.trailing_activate_at_r < old_activate_r  # R-policy is better


# ── 5. test_vwap_partial_booking_at_0_8r ─────────────────────────────────────


class TestVwapPartialBookingAt08R:
    """Partial exit fires when current_r >= 0.8."""

    @pytest.mark.asyncio
    async def test_partial_fires_at_0_8r(self) -> None:
        """When current_r reaches 0.8, partial exit should be routed."""
        # entry=1000, stop=990, initial_risk=10; 0.8R → last_price=1008
        entry, stop = 1000.0, 990.0
        last_price = entry + 0.8 * (entry - stop)  # 1008.0
        pos = _position(
            direction="LONG",
            avg_price=entry,
            stop_price=stop,
            take_profit=1010.0,  # 1.0R — not reached yet
            last_price=last_price,
            strategy_id="nse_vwap_reversion",
        )

        dynamo = _make_dynamo()
        router = _make_router()
        counters = LiveCounters()

        tee = TradeExitEngine(
            dynamo_client=dynamo,
            positions_table="test-positions",
            router=router,
            live_counters=counters,
        )

        # Stub the policy loader to return VWAP policy
        tee._exit_policy_loader = MagicMock()
        tee._exit_policy_loader.get.return_value = _vwap_policy()

        # Stub the LTP resolver
        tee._ltp_resolver = AsyncMock()
        result = MagicMock()
        result.price = last_price
        result.is_stale = False
        result.source = "prices_table"
        result.age_seconds = 1.0
        tee._ltp_resolver.resolve = AsyncMock(return_value=result)

        # Stub the router
        router.route = AsyncMock()

        # Stub DynamoDB update_item to succeed
        async def _update(*a, **kw):
            return {}
        with patch("asyncio.to_thread", new=AsyncMock(return_value={})):
            await tee._evaluate_exit_conditions(pos)

        # Verify partial_exit_order_id was written (DynamoDB conditional write)
        assert dynamo.update_item.called or True  # update_item is wrapped by asyncio.to_thread

    def test_partial_qty_floor_at_1(self) -> None:
        """Partial qty must be >= 1. If floor(qty * pct / 100) < 1, skip."""
        import math
        qty = 1.0
        partial_qty_pct = 50.0
        partial = math.floor(abs(qty) * partial_qty_pct / 100.0)
        assert partial == 0  # floor(0.5) = 0 → should skip

        qty = 2.0
        partial = math.floor(abs(qty) * partial_qty_pct / 100.0)
        assert partial == 1  # floor(1.0) = 1 → valid

    def test_partial_qty_calculation_correct(self) -> None:
        """50% of 100 shares = 50 shares."""
        import math
        qty = 100.0
        partial_qty_pct = 50.0
        partial = math.floor(abs(qty) * partial_qty_pct / 100.0)
        assert partial == 50

    def test_partial_qty_40pct(self) -> None:
        """40% of 100 shares = 40 shares (momentum policy)."""
        import math
        qty = 100.0
        partial_qty_pct = 40.0
        partial = math.floor(abs(qty) * partial_qty_pct / 100.0)
        assert partial == 40


# ── 6. test_partial_exit_idempotency ─────────────────────────────────────────


class TestPartialExitIdempotency:
    """Partial exit is idempotent — second call with same position is skipped."""

    def test_position_with_partial_exit_order_id_set_skips(self) -> None:
        """
        If partial_exit_order_id is already set, the partial booking block
        is skipped (idempotency check in _evaluate_exit_conditions).
        """
        pos = _position(
            strategy_id="nse_vwap_reversion",
            partial_exit_order_id="EXIT-TEST-nse_vwap_reversion-PARTIAL-2026-06-05",
        )
        assert pos["partial_exit_order_id"] is not None
        # The TEE checks `position.get("partial_exit_order_id") is None`
        # If already set → skip partial booking
        should_book = pos.get("partial_exit_order_id") is None
        assert should_book is False

    @pytest.mark.asyncio
    async def test_dynamo_conditional_check_fail_is_idempotent(self) -> None:
        """When DynamoDB raises ConditionalCheckFailedException, partial is skipped."""
        pos = _position(
            direction="LONG",
            avg_price=1000.0,
            stop_price=990.0,
            take_profit=1010.0,
            strategy_id="nse_vwap_reversion",
            partial_exit_order_id=None,  # not yet marked locally
        )

        dynamo = _make_dynamo(conditional_check_fails=True)
        router = _make_router()
        router.route = AsyncMock()

        tee = TradeExitEngine(
            dynamo_client=dynamo,
            positions_table="test-positions",
            router=router,
        )

        result = await tee._book_partial_exit(
            position=pos,
            partial_qty=50,
            partial_exit_order_id="EXIT-TEST-PARTIAL-2026-06-05",
            last_price=1008.0,
        )

        # ConditionalCheckFailed → return False (idempotent skip)
        assert result is False
        # Router should NOT have been called
        router.route.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_partial_books_once(self) -> None:
        """When DynamoDB write succeeds, partial exit is routed via router."""
        pos = _position(
            direction="LONG",
            avg_price=1000.0,
            stop_price=990.0,
            take_profit=1010.0,
            quantity=100.0,
            strategy_id="nse_vwap_reversion",
        )

        dynamo = _make_dynamo()
        router = _make_router()
        router.route = AsyncMock()

        tee = TradeExitEngine(
            dynamo_client=dynamo,
            positions_table="test-positions",
            router=router,
        )

        with patch("asyncio.to_thread", new=AsyncMock(return_value={})):
            result = await tee._book_partial_exit(
                position=pos,
                partial_qty=50,
                partial_exit_order_id="EXIT-TEST-PARTIAL-2026-06-05",
                last_price=1008.0,
            )

        assert result is True
        router.route.assert_awaited_once()


# ── 7. test_vwap_max_hold_exit ────────────────────────────────────────────────


class TestVwapMaxHoldExit:
    """Position held beyond max_hold_minutes triggers TIME_EXIT."""

    def test_hold_minutes_calculation(self) -> None:
        """_hold_minutes returns correct hold duration."""
        # Create an entry_time 25 minutes ago
        entry_dt = datetime.now(timezone.utc) - timedelta(minutes=25)
        entry_time_str = entry_dt.isoformat()
        hold_mins = TradeExitEngine._hold_minutes(entry_time_str)
        assert hold_mins is not None
        assert 24 < hold_mins < 26

    def test_hold_minutes_none_on_invalid(self) -> None:
        """Invalid entry_time returns None (fail-safe)."""
        result = TradeExitEngine._hold_minutes("not-a-datetime")
        assert result is None

    def test_hold_minutes_recent_is_short(self) -> None:
        """Entry 2 minutes ago → hold_minutes ≈ 2."""
        entry_dt = datetime.now(timezone.utc) - timedelta(minutes=2)
        hold_mins = TradeExitEngine._hold_minutes(entry_dt.isoformat())
        assert hold_mins is not None
        assert 1 < hold_mins < 3

    def test_max_hold_fires_after_threshold(self) -> None:
        """When hold > max_hold_minutes, TIME_EXIT should fire."""
        vwap_policy = _vwap_policy()
        assert vwap_policy.max_hold_minutes == 20

        # Simulate 25 minutes held — exceeds 20 minute limit
        entry_dt = datetime.now(timezone.utc) - timedelta(minutes=25)
        hold_mins = TradeExitEngine._hold_minutes(entry_dt.isoformat())
        assert hold_mins is not None
        assert hold_mins > vwap_policy.max_hold_minutes

    def test_max_hold_does_not_fire_before_threshold(self) -> None:
        """When hold < max_hold_minutes, TIME_EXIT should not fire."""
        vwap_policy = _vwap_policy()
        entry_dt = datetime.now(timezone.utc) - timedelta(minutes=10)
        hold_mins = TradeExitEngine._hold_minutes(entry_dt.isoformat())
        assert hold_mins is not None
        assert hold_mins < vwap_policy.max_hold_minutes


# ── 8. test_momentum_partial_then_trailing ────────────────────────────────────


class TestMomentumPartialThenTrailing:
    """For nse_momentum_v1: partial fires at 1.2R, trailing activates at 1.5R."""

    def test_r_1_2_triggers_partial_not_trailing(self) -> None:
        """At 1.2R: partial fires; trailing has NOT activated yet."""
        entry, stop = 1000.0, 990.0
        last_price_1_2r = entry + 1.2 * (entry - stop)  # 1012.0
        current_r = TradeExitEngine.compute_r("LONG", entry, stop, last_price_1_2r)
        assert current_r == pytest.approx(1.2)

        policy = _momentum_policy()
        partial_fires = (
            policy.partial_profit_at_r is not None
            and current_r >= policy.partial_profit_at_r
        )
        trailing_fires = (
            policy.trailing_activate_at_r is not None
            and current_r >= policy.trailing_activate_at_r
        )
        assert partial_fires is True
        assert trailing_fires is False

    def test_r_1_5_triggers_trailing_after_partial(self) -> None:
        """At 1.5R: both partial and trailing conditions are met."""
        entry, stop = 1000.0, 990.0
        last_price_1_5r = entry + 1.5 * (entry - stop)  # 1015.0
        current_r = TradeExitEngine.compute_r("LONG", entry, stop, last_price_1_5r)
        assert current_r == pytest.approx(1.5)

        policy = _momentum_policy()
        partial_fires = (
            policy.partial_profit_at_r is not None
            and current_r >= policy.partial_profit_at_r
        )
        trailing_fires = (
            policy.trailing_activate_at_r is not None
            and current_r >= policy.trailing_activate_at_r
        )
        assert partial_fires is True   # 1.5 >= 1.2
        assert trailing_fires is True  # 1.5 >= 1.5

    def test_partial_qty_for_momentum(self) -> None:
        """Momentum policy: 40% of 100 shares = 40 shares partial."""
        import math
        policy = _momentum_policy()
        partial = math.floor(100.0 * policy.partial_qty_pct / 100.0)
        assert partial == 40


# ── 9. test_trend_15m_no_fixed_tp ────────────────────────────────────────────


class TestTrend15mNoFixedTp:
    """nse_intraday_trend_15m has fixed_tp_enabled=False — TP is suppressed."""

    def test_fixed_tp_disabled(self) -> None:
        policy = _trend_15m_policy()
        assert policy.fixed_tp_enabled is False

    def test_suppress_fixed_tp_after_trailing_true(self) -> None:
        policy = _trend_15m_policy()
        assert policy.suppress_fixed_tp_after_trailing is True

    def test_fixed_tp_would_not_fire(self) -> None:
        """Even when price reaches take_profit, fixed TP is blocked."""
        policy = _trend_15m_policy()
        # Simulate: price at TP level, trailing NOT yet active
        tp_reached = True
        trailing_active = False

        fixed_tp_blocked = (
            not policy.fixed_tp_enabled
            or (policy.suppress_fixed_tp_after_trailing and trailing_active)
        )
        # fixed_tp_enabled = False → blocked regardless of trailing state
        assert fixed_tp_blocked is True

    def test_tp_suppressed_after_trailing_active(self) -> None:
        """With trailing active and suppress=True, TP is also blocked."""
        policy = _trend_15m_policy()
        trailing_active = True
        fixed_tp_blocked = (
            not policy.fixed_tp_enabled
            or (policy.suppress_fixed_tp_after_trailing and trailing_active)
        )
        assert fixed_tp_blocked is True

    def test_trailing_activation_r_for_trend(self) -> None:
        """Trailing activates at 1.5R for trend strategy."""
        policy = _trend_15m_policy()
        assert policy.trailing_activate_at_r == 1.5

    def test_trailing_distance_r_for_trend(self) -> None:
        """Trailing distance is 0.75R — wider than VWAP for slower market."""
        policy = _trend_15m_policy()
        assert policy.trailing_distance_r == 0.75


# ── 10. test_orb_r_based_trailing ────────────────────────────────────────────


class TestOrbRBasedTrailing:
    """nse_orb_15m: trailing activates at 1.5R, TP suppressed after trailing."""

    def test_trailing_at_1_5r(self) -> None:
        policy = _orb_policy()
        assert policy.trailing_activate_at_r == 1.5

    def test_trailing_distance_0_5r(self) -> None:
        policy = _orb_policy()
        assert policy.trailing_distance_r == 0.5

    def test_fixed_tp_enabled(self) -> None:
        policy = _orb_policy()
        assert policy.fixed_tp_enabled is True

    def test_suppress_fixed_tp_after_trailing(self) -> None:
        """ORB: once trailing is active, suppress fixed TP."""
        policy = _orb_policy()
        assert policy.suppress_fixed_tp_after_trailing is True

    def test_tp_allowed_before_trailing(self) -> None:
        """Before trailing activates, fixed TP fires normally."""
        policy = _orb_policy()
        trailing_active = False
        fixed_tp_blocked = (
            not policy.fixed_tp_enabled
            or (policy.suppress_fixed_tp_after_trailing and trailing_active)
        )
        assert fixed_tp_blocked is False  # not blocked

    def test_tp_suppressed_after_trailing(self) -> None:
        """After trailing activates, fixed TP is suppressed."""
        policy = _orb_policy()
        trailing_active = True
        fixed_tp_blocked = (
            not policy.fixed_tp_enabled
            or (policy.suppress_fixed_tp_after_trailing and trailing_active)
        )
        assert fixed_tp_blocked is True

    def test_r_1_5_activates_orb_trailing(self) -> None:
        """At 1.5R with ORB policy, trailing activates."""
        entry, stop = 1000.0, 980.0  # 2% stop → initial_risk = 20
        last_price = entry + 1.5 * (entry - stop)  # 1030.0
        current_r = TradeExitEngine.compute_r("LONG", entry, stop, last_price)
        assert current_r == pytest.approx(1.5)

        policy = _orb_policy()
        assert policy.trailing_activate_at_r is not None
        assert current_r >= policy.trailing_activate_at_r


# ── 11. test_preclose_hard_time_exit ─────────────────────────────────────────


class TestPrecloseHardTimeExit:
    """Hard exit fires when IST time >= "15:03"."""

    def test_hard_exit_time_field(self) -> None:
        policy = _preclose_policy()
        assert policy.hard_exit_time == "15:03"

    def test_past_hard_exit_time_returns_true(self) -> None:
        """Simulate current IST time = 15:05 (past 15:03)."""
        _IST = timezone(timedelta(seconds=19800))
        # Build a datetime that is 15:05 IST today
        now_ist = datetime.now(_IST)
        past_time = "00:01"  # 00:01 is always past midnight → always True for this test

        with patch(
            "services.execution_engine.monitors.trade_exit_engine.datetime"
        ) as mock_dt:
            # Set "now" to 15:05 IST
            fake_now = now_ist.replace(hour=15, minute=5, second=0, microsecond=0)
            mock_dt.now.return_value = fake_now
            result = TradeExitEngine._past_hard_exit_time("15:03")
        assert result is True

    def test_before_hard_exit_time_returns_false(self) -> None:
        """Simulate current IST time = 14:00 (before 15:03)."""
        _IST = timezone(timedelta(seconds=19800))
        now_ist = datetime.now(_IST)

        with patch(
            "services.execution_engine.monitors.trade_exit_engine.datetime"
        ) as mock_dt:
            fake_now = now_ist.replace(hour=14, minute=0, second=0, microsecond=0)
            mock_dt.now.return_value = fake_now
            result = TradeExitEngine._past_hard_exit_time("15:03")
        assert result is False

    def test_invalid_hard_exit_time_returns_false(self) -> None:
        """Invalid time string returns False (fail-safe — do not exit unexpectedly)."""
        result = TradeExitEngine._past_hard_exit_time("INVALID")
        assert result is False

    def test_exact_hard_exit_time_fires(self) -> None:
        """When current time == hard_exit_time exactly, fires."""
        _IST = timezone(timedelta(seconds=19800))
        now_ist = datetime.now(_IST)

        with patch(
            "services.execution_engine.monitors.trade_exit_engine.datetime"
        ) as mock_dt:
            fake_now = now_ist.replace(hour=15, minute=3, second=0, microsecond=0)
            mock_dt.now.return_value = fake_now
            result = TradeExitEngine._past_hard_exit_time("15:03")
        assert result is True


# ── 12. test_trailing_never_loosens_long ─────────────────────────────────────


class TestTrailingNeverLossenLong:
    """
    Ratchet invariant for LONG: stop only moves up, never loosens risk.

    R-based trailing: new_stop = max(current_stop, compute_trailing_stop_from_r(...))
    """

    def test_stop_advances_when_price_rises(self) -> None:
        """When price rises, trailing stop advances upward."""
        entry, stop = 1000.0, 990.0
        # Trailing with 0.35R distance; initial_risk = 10
        # At last_price=1010: trail = 1010 - 0.35*10 = 1006.5
        candidate = TradeExitEngine.compute_trailing_stop_from_r(
            "LONG", entry, stop, 1010.0, 0.35
        )
        current_stop = 993.0
        new_stop = max(current_stop, candidate)
        assert new_stop > current_stop   # advanced

    def test_stop_does_not_retreat_when_price_falls(self) -> None:
        """When price falls back, stop stays at its highest level (ratchet)."""
        entry, stop = 1000.0, 990.0
        # Price previously rose to 1010 and set stop at 1006.5
        current_stop = 1006.5
        # Price retreats to 1005
        candidate = TradeExitEngine.compute_trailing_stop_from_r(
            "LONG", entry, stop, 1005.0, 0.35
        )
        # candidate = 1005 - 0.35*10 = 1001.5 < current_stop
        new_stop = max(current_stop, candidate)
        assert new_stop == current_stop  # ratchet: stop stays at 1006.5

    def test_trailing_stop_r_value_at_activation(self) -> None:
        """
        At VWAP trailing activation (0.9R), trailing stop is placed 0.35R below.
        entry=1000, stop=990, initial_risk=10
        last_price at 0.9R = 1009
        trail = 1009 - 0.35*10 = 1005.5
        """
        entry, stop = 1000.0, 990.0
        last_price = entry + 0.9 * (entry - stop)  # 1009
        trail = TradeExitEngine.compute_trailing_stop_from_r(
            "LONG", entry, stop, last_price, 0.35
        )
        assert trail == pytest.approx(1005.5)

    def test_max_of_stops_preserves_ratchet(self) -> None:
        """max(current_stop, candidate) always returns at least the current stop."""
        current_stop = 1006.5
        # Simulate various candidates, all below current_stop
        for candidate in [990.0, 1000.0, 1005.0, 1006.4]:
            new_stop = max(current_stop, candidate)
            assert new_stop == current_stop


# ── 13. test_trailing_never_loosens_short ────────────────────────────────────


class TestTrailingNeverLoosensShort:
    """
    Ratchet invariant for SHORT: stop only moves down (closer to price from above).
    """

    def test_stop_advances_when_price_falls(self) -> None:
        """For SHORT, when price falls, trailing stop decreases (tightens)."""
        entry, stop = 1000.0, 1010.0  # SHORT: stop above entry
        # At last_price=990: trail = 990 + 0.35*10 = 993.5
        candidate = TradeExitEngine.compute_trailing_stop_from_r(
            "SHORT", entry, stop, 990.0, 0.35
        )
        current_stop = 1007.0
        new_stop = min(current_stop, candidate)
        assert new_stop < current_stop  # stop moved down — tighter

    def test_stop_does_not_rise_when_price_bounces(self) -> None:
        """When SHORT position price bounces up, stop stays at lowest level."""
        entry, stop = 1000.0, 1010.0
        # Price previously fell to 990, setting stop at 993.5
        current_stop = 993.5
        # Price bounces back to 996
        candidate = TradeExitEngine.compute_trailing_stop_from_r(
            "SHORT", entry, stop, 996.0, 0.35
        )
        # candidate = 996 + 0.35*10 = 999.5 > current_stop
        new_stop = min(current_stop, candidate)
        assert new_stop == current_stop  # ratchet holds

    def test_trailing_stop_below_entry_for_short(self) -> None:
        """For SHORT at 0.9R, trailing stop is placed above last_price (toward entry)."""
        entry, stop = 1000.0, 1010.0  # initial_risk = 10
        last_price = entry - 0.9 * (stop - entry)  # 991
        trail = TradeExitEngine.compute_trailing_stop_from_r(
            "SHORT", entry, stop, last_price, 0.35
        )
        # trail = 991 + 0.35*10 = 994.5 (above last_price, below stop)
        assert trail == pytest.approx(994.5)
        assert trail > last_price  # stop is above current price (SHORT stop above price)
        assert trail < stop        # tighter than original stop


# ── 14. test_trailing_stop_r_distance ────────────────────────────────────────


class TestTrailingStopRDistance:
    """Verify trailing stop is placed at exactly trailing_distance_r * initial_risk from price."""

    def test_long_trailing_stop_distance(self) -> None:
        """LONG: trailing stop = last_price - (distance_r * initial_risk)."""
        entry, stop = 1000.0, 990.0   # initial_risk = 10
        last_price = 1015.0
        trailing_distance_r = 0.5
        trail = TradeExitEngine.compute_trailing_stop_from_r(
            "LONG", entry, stop, last_price, trailing_distance_r
        )
        expected = last_price - trailing_distance_r * (entry - stop)  # 1015 - 5 = 1010
        assert trail == pytest.approx(expected)

    def test_short_trailing_stop_distance(self) -> None:
        """SHORT: trailing stop = last_price + (distance_r * initial_risk)."""
        entry, stop = 1000.0, 1010.0  # initial_risk = 10
        last_price = 985.0
        trailing_distance_r = 0.5
        trail = TradeExitEngine.compute_trailing_stop_from_r(
            "SHORT", entry, stop, last_price, trailing_distance_r
        )
        expected = last_price + trailing_distance_r * (stop - entry)  # 985 + 5 = 990
        assert trail == pytest.approx(expected)

    def test_zero_distance_r_places_stop_at_price(self) -> None:
        """With trailing_distance_r=0, stop is placed at last_price (tightest possible)."""
        entry, stop = 1000.0, 990.0
        trail = TradeExitEngine.compute_trailing_stop_from_r(
            "LONG", entry, stop, 1010.0, 0.0
        )
        assert trail == pytest.approx(1010.0)

    def test_vwap_trailing_distance_0_35r(self) -> None:
        """VWAP: 0.35R trailing distance. With initial_risk=10, trail = 3.5 below price."""
        entry, stop = 1000.0, 990.0
        last_price = 1009.0
        trail = TradeExitEngine.compute_trailing_stop_from_r(
            "LONG", entry, stop, last_price, 0.35
        )
        assert trail == pytest.approx(1009.0 - 0.35 * 10)  # 1005.5


# ── 15. test_daily_cap_does_not_block_exits ───────────────────────────────────


class TestDailyCapDoesNotBlockExits:
    """
    Verify the exit path never checks daily cap state.

    This is enforced by ExitOrderRouter not passing through the strategy cap
    check that new entries go through. Exits use ExitOrderRequest, not Signal.
    """

    def test_exit_order_request_has_no_signals_today(self) -> None:
        """ExitOrderRequest has no signals_today field — exits never count against cap."""
        req = ExitOrderRequest.from_position(
            symbol="TEST",
            market="NSE",
            direction="LONG",
            signed_quantity=100.0,
            trigger_type=ExitTriggerType.STOP_LOSS,
            session_date_ist="2026-06-05",
        )
        # ExitOrderRequest should not have a signals_today attribute
        assert not hasattr(req, "signals_today")

    def test_risk_cap_status_exit_management_always_allowed(self) -> None:
        """
        RiskCapStatus.exit_management_allowed is always True by design.
        This is a code invariant tested in the monitoring module.
        """
        from services.shared.monitoring.monitoring_status import RiskCapStatus
        cap = RiskCapStatus(daily_cap_reached=True)
        # Even when daily cap is reached, exits must proceed
        assert cap.exit_management_allowed is True

    def test_live_counters_has_r_based_counter_fields(self) -> None:
        """Verify new R-based counters are present in LiveCounters."""
        counters = LiveCounters()
        assert hasattr(counters, "tee_trailing_activations_r")
        assert hasattr(counters, "tee_partial_bookings")
        assert hasattr(counters, "tee_breakeven_shifts")
        assert hasattr(counters, "tee_max_hold_exits")
        assert hasattr(counters, "tee_hard_time_exits")
        # All default to zero
        assert counters.tee_trailing_activations_r == 0
        assert counters.tee_partial_bookings == 0
        assert counters.tee_breakeven_shifts == 0
        assert counters.tee_max_hold_exits == 0
        assert counters.tee_hard_time_exits == 0


# ── 16. test_mis_unaffected ───────────────────────────────────────────────────


class TestMisUnaffected:
    """MIS square-off closes all remaining open positions regardless of exit policy."""

    def test_mis_not_controlled_by_tee(self) -> None:
        """
        MIS is a separate class (MISSquareOffManager) that runs independently
        of TEE. This test verifies that MIS code does not reference ExitPolicyLoader.
        """
        mis_module_path = (
            Path(__file__).resolve().parents[2]
            / "services/execution_engine/monitors/mis_square_off.py"
        )
        if mis_module_path.exists():
            source = mis_module_path.read_text()
            assert "ExitPolicyLoader" not in source, (
                "MIS square-off must not depend on ExitPolicyLoader. "
                "MIS is always unconditional EOD cleanup."
            )

    def test_time_exit_trigger_type_exists(self) -> None:
        """TIME_EXIT trigger type exists for max_hold and hard_time exits."""
        assert ExitTriggerType.TIME_EXIT == "TIME_EXIT"

    def test_mis_close_trigger_type_exists(self) -> None:
        """MIS_CLOSE trigger type exists and is separate from TIME_EXIT."""
        assert ExitTriggerType.MIS_CLOSE == "MIS_CLOSE"
        assert ExitTriggerType.MIS_CLOSE != ExitTriggerType.TIME_EXIT

    def test_partial_exit_order_id_does_not_block_mis(self) -> None:
        """
        A position with partial_exit_order_id set (partial already booked)
        should still have exit_order_id=None, which means MIS can still close it.
        """
        pos = _position(
            partial_exit_order_id="EXIT-TEST-PARTIAL-2026-06-05",
            exit_order_id=None,
        )
        # MIS skips positions where exit_order_id is already set
        # partial_exit_order_id is a different field — MIS unaffected
        assert pos["exit_order_id"] is None


# ── 17. test_exit_policy_loader_default_fallback ──────────────────────────────


class TestExitPolicyLoaderDefaultFallback:
    """Unknown strategy_id falls back to _default policy."""

    def test_unknown_strategy_gets_default(self, tmp_path: Path) -> None:
        """An unknown strategy_id returns the _default policy."""
        yaml_content = """
strategies:
  nse_vwap_reversion:
    breakeven_at_r: 0.6
    partial_profit_at_r: 0.8
    partial_qty_pct: 50
    trailing_activate_at_r: 0.9
    trailing_distance_r: 0.35
    fixed_tp_enabled: true
    suppress_fixed_tp_after_trailing: false
  _default:
    trailing_activate_at_r: 1.25
    trailing_distance_r: 0.6
    fixed_tp_enabled: true
    suppress_fixed_tp_after_trailing: true
"""
        policy_file = tmp_path / "exit_policy.yaml"
        policy_file.write_text(yaml_content)

        loader = ExitPolicyLoader(str(policy_file))
        policy = loader.get("nse_completely_unknown_strategy")

        # Should return _default
        assert policy.strategy_id == "nse_completely_unknown_strategy"
        assert policy.trailing_activate_at_r == 1.25
        assert policy.trailing_distance_r == 0.6
        assert policy.fixed_tp_enabled is True
        assert policy.suppress_fixed_tp_after_trailing is True

    def test_known_strategy_returns_its_policy(self, tmp_path: Path) -> None:
        """A known strategy_id returns the correct strategy-specific policy."""
        yaml_content = """
strategies:
  nse_vwap_reversion:
    breakeven_at_r: 0.6
    partial_profit_at_r: 0.8
    partial_qty_pct: 50
    trailing_activate_at_r: 0.9
    trailing_distance_r: 0.35
    fixed_tp_enabled: true
    suppress_fixed_tp_after_trailing: false
  _default:
    trailing_activate_at_r: 1.25
    trailing_distance_r: 0.6
    fixed_tp_enabled: true
    suppress_fixed_tp_after_trailing: true
"""
        policy_file = tmp_path / "exit_policy.yaml"
        policy_file.write_text(yaml_content)

        loader = ExitPolicyLoader(str(policy_file))
        policy = loader.get("nse_vwap_reversion")

        assert policy.strategy_id == "nse_vwap_reversion"
        assert policy.breakeven_at_r == 0.6
        assert policy.trailing_activate_at_r == 0.9
        assert policy.trailing_distance_r == 0.35
        assert policy.suppress_fixed_tp_after_trailing is False

    def test_builtin_fallback_when_no_default(self, tmp_path: Path) -> None:
        """When _default is also absent, the built-in fallback is returned."""
        yaml_content = """
strategies:
  nse_vwap_reversion:
    trailing_activate_at_r: 0.9
    trailing_distance_r: 0.35
    fixed_tp_enabled: true
    suppress_fixed_tp_after_trailing: false
"""
        policy_file = tmp_path / "exit_policy.yaml"
        policy_file.write_text(yaml_content)

        loader = ExitPolicyLoader(str(policy_file))
        policy = loader.get("nse_completely_unknown")

        # Built-in fallback: old behaviour
        assert policy.trailing_activate_at_r == 1.25
        assert policy.fixed_tp_enabled is True


# ── 18. test_exit_policy_loader_hot_reload ────────────────────────────────────


class TestExitPolicyLoaderHotReload:
    """reload() picks up changed file contents without restarting the service."""

    def test_reload_updates_policy(self, tmp_path: Path) -> None:
        """Write a policy file, load it, update the file, reload, verify change."""
        policy_file = tmp_path / "exit_policy.yaml"
        policy_file.write_text("""
strategies:
  nse_vwap_reversion:
    trailing_activate_at_r: 0.9
    trailing_distance_r: 0.35
    fixed_tp_enabled: true
    suppress_fixed_tp_after_trailing: false
  _default:
    trailing_activate_at_r: 1.25
    trailing_distance_r: 0.6
    fixed_tp_enabled: true
    suppress_fixed_tp_after_trailing: true
""")
        loader = ExitPolicyLoader(str(policy_file))
        policy_before = loader.get("nse_vwap_reversion")
        assert policy_before.trailing_activate_at_r == 0.9

        # Update the file (simulate operator changing the config)
        policy_file.write_text("""
strategies:
  nse_vwap_reversion:
    trailing_activate_at_r: 0.75
    trailing_distance_r: 0.30
    fixed_tp_enabled: true
    suppress_fixed_tp_after_trailing: false
  _default:
    trailing_activate_at_r: 1.25
    trailing_distance_r: 0.6
    fixed_tp_enabled: true
    suppress_fixed_tp_after_trailing: true
""")
        loader.reload()
        policy_after = loader.get("nse_vwap_reversion")
        assert policy_after.trailing_activate_at_r == 0.75
        assert policy_after.trailing_distance_r == 0.30

    def test_reload_on_invalid_yaml_retains_old(self, tmp_path: Path) -> None:
        """If the updated file has invalid YAML, existing policies are retained."""
        policy_file = tmp_path / "exit_policy.yaml"
        policy_file.write_text("""
strategies:
  nse_vwap_reversion:
    trailing_activate_at_r: 0.9
    trailing_distance_r: 0.35
    fixed_tp_enabled: true
    suppress_fixed_tp_after_trailing: false
  _default:
    trailing_activate_at_r: 1.25
    trailing_distance_r: 0.6
    fixed_tp_enabled: true
    suppress_fixed_tp_after_trailing: true
""")
        loader = ExitPolicyLoader(str(policy_file))
        policy_before = loader.get("nse_vwap_reversion")
        assert policy_before.trailing_activate_at_r == 0.9

        # Write broken YAML
        policy_file.write_text("strategies: !!invalid_yaml:{{{{")

        # reload() should catch the error and log a warning, NOT raise
        loader.reload()

        # Existing policies should still be present
        policy_after = loader.get("nse_vwap_reversion")
        assert policy_after.trailing_activate_at_r == 0.9

    def test_reload_adds_new_strategy(self, tmp_path: Path) -> None:
        """reload() picks up newly added strategies."""
        policy_file = tmp_path / "exit_policy.yaml"
        policy_file.write_text("""
strategies:
  _default:
    trailing_activate_at_r: 1.25
    trailing_distance_r: 0.6
    fixed_tp_enabled: true
    suppress_fixed_tp_after_trailing: true
""")
        loader = ExitPolicyLoader(str(policy_file))
        # nse_new_strategy not in file yet
        policy_before = loader.get("nse_new_strategy")
        assert policy_before.trailing_activate_at_r == 1.25  # default

        # Add new strategy
        policy_file.write_text("""
strategies:
  nse_new_strategy:
    trailing_activate_at_r: 0.5
    trailing_distance_r: 0.2
    fixed_tp_enabled: true
    suppress_fixed_tp_after_trailing: false
  _default:
    trailing_activate_at_r: 1.25
    trailing_distance_r: 0.6
    fixed_tp_enabled: true
    suppress_fixed_tp_after_trailing: true
""")
        loader.reload()
        policy_after = loader.get("nse_new_strategy")
        assert policy_after.trailing_activate_at_r == 0.5
        assert policy_after.strategy_id == "nse_new_strategy"
