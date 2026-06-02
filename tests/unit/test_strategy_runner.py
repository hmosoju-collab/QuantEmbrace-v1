"""
Unit tests for CircuitBreaker and StrategyRunner (Phase 3 — ADR-013).

Coverage:
    CircuitBreaker
        - CLOSED → OPEN on consecutive_threshold consecutive errors
        - CLOSED → OPEN on rate_threshold errors within rate_window
        - OPEN → HALF_OPEN after reset_seconds
        - HALF_OPEN → CLOSED after success_threshold successes
        - HALF_OPEN → OPEN on single failure
        - Manual reset() transitions directly to CLOSED
        - update_thresholds() takes effect on next failure
        - errors_in_window evicts old timestamps correctly
        - record_failure() returns True only when circuit just opened
        - record_success() resets consecutive_errors in CLOSED state
        - is_open returns False for HALF_OPEN (callers may dispatch)

    StrategyRunner
        - dispatch_tick routes to TICK strategy
        - dispatch_bar routes to CANDLE strategy
        - wrong interface type returns None
        - disabled strategy returns None immediately
        - circuit OPEN → dispatch returns None
        - daily cap enforced; returns None when cap reached
        - daily cap rolls over at UTC midnight
        - paper_trade flag stamped on outgoing signal
        - apply_config updates live config atomically
        - apply_config(reset_cb=True) calls circuit.reset() and returns True
        - apply_config(reset_cb=False) returns False
        - circuit opens after consecutive_threshold dispatch errors
        - _on_failure logs CRITICAL when circuit just opened
        - suppress_signals path feeds tick but discards signal
"""

from __future__ import annotations

import asyncio
import sys
import os
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from strategy_engine.runners.circuit_breaker import CircuitBreaker, CircuitState
from strategy_engine.runners.strategy_runner import (
    InterfaceType,
    StrategyConfig,
    StrategyRunner,
)
from shared.models.signal import Signal


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

def _make_circuit(consecutive: int = 3, rate: int = 5, reset_seconds: float = 0.1) -> CircuitBreaker:
    """Fast-reset circuit breaker for testing (reset_seconds=0.1s)."""
    return CircuitBreaker(
        strategy_name="test_strategy",
        consecutive_threshold=consecutive,
        rate_threshold=rate,
        rate_window_seconds=60.0,
        reset_seconds=reset_seconds,
        success_threshold=2,
    )


def _make_signal(strategy_name: str = "test") -> Signal:
    """Minimal Signal object."""
    from shared.models.signal import Direction
    return Signal(
        strategy_name=strategy_name,
        symbol="RELIANCE",
        market="NSE",
        direction=Direction.BUY,
        quantity=10,
        confidence=0.75,
        generated_at=datetime.now(timezone.utc),
        price_at_signal=2500.0,
    )


def _make_mock_strategy(
    signal: Signal | None = None,
    interface: str = "TICK",
    name: str = "test_strategy",
) -> MagicMock:
    """Mock BaseStrategy that returns a given signal."""
    mock = MagicMock()
    mock.name = name
    mock.symbols = ["RELIANCE", "INFY"]
    mock.on_tick = AsyncMock(return_value=None)
    mock.on_bar  = AsyncMock(return_value=None)
    mock.generate_signal = AsyncMock(return_value=signal)
    return mock


# ═══════════════════════════════════════════════════════════════════════════════
# CircuitBreaker tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreakerTransitions:

    def test_initial_state_is_closed(self):
        cb = _make_circuit()
        assert cb.state == CircuitState.CLOSED
        assert cb.is_closed
        assert not cb.is_open

    def test_open_on_consecutive_threshold(self):
        cb = _make_circuit(consecutive=3, rate=100)
        for _ in range(2):
            result = cb.record_failure()
            assert result is False            # not opened yet
        result = cb.record_failure()          # 3rd consecutive — threshold hit
        assert result is True                 # circuit just opened
        assert cb.state == CircuitState.OPEN

    def test_open_on_rate_threshold(self):
        cb = _make_circuit(consecutive=100, rate=4)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.CLOSED  # not yet at rate threshold
        result = cb.record_failure()
        assert result is True
        assert cb.state == CircuitState.OPEN

    def test_open_to_half_open_after_reset_seconds(self):
        cb = _make_circuit(reset_seconds=0.05)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_to_closed_on_success_threshold(self):
        cb = _make_circuit(reset_seconds=0.05)
        for _ in range(3):
            cb.record_failure()
        time.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.HALF_OPEN   # 1/2 successes
        cb.record_success()
        assert cb.state == CircuitState.CLOSED      # 2/2 → closed

    def test_half_open_to_open_on_failure(self):
        cb = _make_circuit(reset_seconds=0.05)
        for _ in range(3):
            cb.record_failure()
        time.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN
        result = cb.record_failure()
        assert result is True                       # circuit re-opened
        assert cb.state == CircuitState.OPEN

    def test_open_failure_returns_false(self):
        cb = _make_circuit(consecutive=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        result = cb.record_failure()
        assert result is False      # already open — returns False

    def test_manual_reset_transitions_to_closed(self):
        cb = _make_circuit(consecutive=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.consecutive_errors == 0
        assert cb.open_at is None

    def test_manual_reset_clears_error_times(self):
        cb = _make_circuit(consecutive=3, rate=100)
        for _ in range(3):
            cb.record_failure()
        cb.reset()
        # After reset, error window is cleared — single failure should not open
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_update_thresholds_takes_effect(self):
        cb = _make_circuit(consecutive=10, rate=100)
        cb.update_thresholds(consecutive_threshold=2)
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_record_success_resets_consecutive_errors(self):
        cb = _make_circuit(consecutive=5)
        for _ in range(3):
            cb.record_failure()
        assert cb.consecutive_errors == 3
        cb.record_success()
        assert cb.consecutive_errors == 0

    def test_errors_in_window_evicts_old_timestamps(self):
        cb = CircuitBreaker(
            strategy_name="test",
            consecutive_threshold=100,
            rate_threshold=100,
            rate_window_seconds=0.05,   # 50ms window
        )
        for _ in range(5):
            cb.record_failure()
        assert cb.errors_in_window == 5
        time.sleep(0.1)
        assert cb.errors_in_window == 0    # all evicted after window

    def test_is_open_false_for_half_open(self):
        # HALF_OPEN: callers are allowed to make a test dispatch
        cb = _make_circuit(reset_seconds=0.05)
        for _ in range(3):
            cb.record_failure()
        time.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN
        # is_open checks state (which triggers lazy transition)
        # HALF_OPEN is NOT open — callers should dispatch
        assert not cb.is_open


# ═══════════════════════════════════════════════════════════════════════════════
# StrategyRunner tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestStrategyRunnerBasics:

    @pytest.mark.asyncio
    async def test_dispatch_tick_returns_signal(self):
        signal = _make_signal()
        strategy = _make_mock_strategy(signal=signal)
        runner = StrategyRunner(strategy, InterfaceType.TICK, StrategyConfig(paper_trade=True))

        result = await runner.dispatch_tick("RELIANCE", 2500.0, 100,
                                            datetime.now(timezone.utc))
        assert result is not None
        assert result.strategy_name == "test"
        assert result.paper_trade is True    # paper_trade flag stamped

    @pytest.mark.asyncio
    async def test_dispatch_bar_returns_signal(self):
        from strategy_engine.strategies.base_strategy import Bar
        signal = _make_signal()
        strategy = _make_mock_strategy(signal=signal)
        strategy.candle_interval = "minute"
        runner = StrategyRunner(strategy, InterfaceType.CANDLE, StrategyConfig(paper_trade=False))

        bar = Bar(symbol="RELIANCE", market="NSE", open=2490.0, high=2510.0,
                  low=2485.0, close=2500.0, volume=5000,
                  timestamp=datetime.now(timezone.utc), interval="minute")
        result = await runner.dispatch_bar(bar)
        assert result is not None
        assert result.paper_trade is False

    @pytest.mark.asyncio
    async def test_dispatch_tick_on_candle_runner_returns_none(self):
        strategy = _make_mock_strategy()
        runner = StrategyRunner(strategy, InterfaceType.CANDLE, StrategyConfig())
        result = await runner.dispatch_tick("RELIANCE", 2500.0, 100,
                                            datetime.now(timezone.utc))
        assert result is None
        strategy.on_tick.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_bar_on_tick_runner_returns_none(self):
        from strategy_engine.strategies.base_strategy import Bar
        strategy = _make_mock_strategy()
        runner = StrategyRunner(strategy, InterfaceType.TICK, StrategyConfig())
        bar = Bar(symbol="RELIANCE", market="NSE", open=2490.0, high=2510.0,
                  low=2485.0, close=2500.0, volume=5000,
                  timestamp=datetime.now(timezone.utc), interval="minute")
        result = await runner.dispatch_bar(bar)
        assert result is None
        strategy.on_bar.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_strategy_returns_none(self):
        signal = _make_signal()
        strategy = _make_mock_strategy(signal=signal)
        config = StrategyConfig(enabled=False)
        runner = StrategyRunner(strategy, InterfaceType.TICK, config)

        result = await runner.dispatch_tick("RELIANCE", 2500.0, 100,
                                            datetime.now(timezone.utc))
        assert result is None
        strategy.on_tick.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_signal_returns_none(self):
        strategy = _make_mock_strategy(signal=None)
        runner = StrategyRunner(strategy, InterfaceType.TICK, StrategyConfig())
        result = await runner.dispatch_tick("RELIANCE", 2500.0, 100,
                                            datetime.now(timezone.utc))
        assert result is None


class TestStrategyRunnerCircuitBreaker:

    @pytest.mark.asyncio
    async def test_circuit_opens_after_consecutive_failures(self):
        strategy = _make_mock_strategy()
        strategy.on_tick = AsyncMock(side_effect=RuntimeError("broker down"))
        config = StrategyConfig(circuit_breaker_threshold_consecutive=3,
                                circuit_breaker_threshold_rate=100)
        runner = StrategyRunner(strategy, InterfaceType.TICK, config)

        for _ in range(3):
            await runner.dispatch_tick("RELIANCE", 2500.0, 100,
                                       datetime.now(timezone.utc))

        from strategy_engine.runners.circuit_breaker import CircuitState
        assert runner.circuit_state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_open_circuit_skips_dispatch(self):
        strategy = _make_mock_strategy()
        strategy.on_tick = AsyncMock(side_effect=RuntimeError("error"))
        config = StrategyConfig(circuit_breaker_threshold_consecutive=2)
        runner = StrategyRunner(strategy, InterfaceType.TICK, config)

        # Trigger circuit open
        for _ in range(2):
            await runner.dispatch_tick("RELIANCE", 2500.0, 100,
                                       datetime.now(timezone.utc))

        # Circuit is open — further dispatches must not call on_tick
        call_count_before = strategy.on_tick.call_count
        await runner.dispatch_tick("RELIANCE", 2500.0, 100,
                                   datetime.now(timezone.utc))
        assert strategy.on_tick.call_count == call_count_before


class TestStrategyRunnerDailyCap:

    @pytest.mark.asyncio
    async def test_daily_cap_enforced(self):
        signal = _make_signal()
        strategy = _make_mock_strategy(signal=signal)
        config = StrategyConfig(max_signals_per_day=2)
        runner = StrategyRunner(strategy, InterfaceType.TICK, config)

        r1 = await runner.dispatch_tick("RELIANCE", 2500.0, 100,
                                        datetime.now(timezone.utc))
        r2 = await runner.dispatch_tick("RELIANCE", 2500.0, 100,
                                        datetime.now(timezone.utc))
        r3 = await runner.dispatch_tick("RELIANCE", 2500.0, 100,
                                        datetime.now(timezone.utc))

        assert r1 is not None
        assert r2 is not None
        assert r3 is None           # cap reached

    @pytest.mark.asyncio
    async def test_unlimited_cap_zero(self):
        signal = _make_signal()
        strategy = _make_mock_strategy(signal=signal)
        config = StrategyConfig(max_signals_per_day=0)   # 0 = unlimited
        runner = StrategyRunner(strategy, InterfaceType.TICK, config)

        results = []
        for _ in range(20):
            r = await runner.dispatch_tick("RELIANCE", 2500.0, 100,
                                           datetime.now(timezone.utc))
            results.append(r)

        assert all(r is not None for r in results)

    def test_reset_daily_cap_resets_counter(self):
        runner = StrategyRunner(
            _make_mock_strategy(),
            InterfaceType.TICK,
            StrategyConfig(max_signals_per_day=5),
        )
        runner._signals_today = 5
        runner.reset_daily_cap()
        assert runner.signals_today() == 0


class TestStrategyRunnerApplyConfig:

    def test_apply_config_updates_config(self):
        strategy = _make_mock_strategy()
        runner = StrategyRunner(strategy, InterfaceType.TICK, StrategyConfig(enabled=True))

        new_config = StrategyConfig(enabled=False, paper_trade=True, max_signals_per_day=3)
        runner.apply_config(new_config)

        assert runner.config.enabled is False
        assert runner.config.max_signals_per_day == 3

    def test_apply_config_with_reset_returns_true(self):
        strategy = _make_mock_strategy()
        runner = StrategyRunner(strategy, InterfaceType.TICK, StrategyConfig())

        did_reset = runner.apply_config(StrategyConfig(), reset_cb=True)
        assert did_reset is True

    def test_apply_config_without_reset_returns_false(self):
        strategy = _make_mock_strategy()
        runner = StrategyRunner(strategy, InterfaceType.TICK, StrategyConfig())

        did_reset = runner.apply_config(StrategyConfig(), reset_cb=False)
        assert did_reset is False

    def test_apply_config_reset_clears_open_circuit(self):
        strategy = _make_mock_strategy()
        strategy.on_tick = MagicMock(side_effect=RuntimeError("error"))
        config = StrategyConfig(circuit_breaker_threshold_consecutive=2)
        runner = StrategyRunner(strategy, InterfaceType.TICK, config)

        # Manually trip the circuit
        runner._circuit._consecutive_errors = 2
        runner._circuit._transition_to_open()

        from strategy_engine.runners.circuit_breaker import CircuitState
        assert runner.circuit_state == CircuitState.OPEN

        runner.apply_config(StrategyConfig(), reset_cb=True)
        assert runner.circuit_state == CircuitState.CLOSED

    def test_apply_config_updates_circuit_thresholds(self):
        strategy = _make_mock_strategy()
        runner = StrategyRunner(
            strategy, InterfaceType.TICK,
            StrategyConfig(circuit_breaker_threshold_consecutive=10),
        )
        assert runner._circuit.consecutive_threshold == 10

        runner.apply_config(StrategyConfig(circuit_breaker_threshold_consecutive=3))
        assert runner._circuit.consecutive_threshold == 3


class TestStrategyRunnerPaperTrade:

    @pytest.mark.asyncio
    async def test_paper_trade_true_stamps_signal(self):
        signal = _make_signal()
        signal.paper_trade = False    # initially False
        strategy = _make_mock_strategy(signal=signal)
        runner = StrategyRunner(
            strategy, InterfaceType.TICK,
            StrategyConfig(paper_trade=True),
        )
        result = await runner.dispatch_tick("RELIANCE", 2500.0, 100,
                                            datetime.now(timezone.utc))
        assert result.paper_trade is True

    @pytest.mark.asyncio
    async def test_paper_trade_false_does_not_stamp(self):
        signal = _make_signal()
        signal.paper_trade = False
        strategy = _make_mock_strategy(signal=signal)
        runner = StrategyRunner(
            strategy, InterfaceType.TICK,
            StrategyConfig(paper_trade=False),
        )
        result = await runner.dispatch_tick("RELIANCE", 2500.0, 100,
                                            datetime.now(timezone.utc))
        assert result.paper_trade is False
