"""Unit tests for IntradayTrend15mStrategy warm-start state persistence (ADR-031 follow-up)."""

from __future__ import annotations

import os
import sys
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from strategy_engine.strategies.base_strategy import StrategyState  # noqa: E402
from strategy_engine.strategies.intraday_trend_15m_strategy import (  # noqa: E402
    IntradayTrend15mStrategy,
    _today_ist,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _strategy() -> IntradayTrend15mStrategy:
    return IntradayTrend15mStrategy(name="trend_15m_test", symbols=[], market="NSE")


def _load_buffers(s: IntradayTrend15mStrategy, symbol: str, n: int) -> None:
    """Populate the strategy's OHLCV buffers with n synthetic bars."""
    for i in range(n):
        s._ensure(symbol)
        s._closes[symbol].append(100.0 + i * 0.1)
        s._highs[symbol].append(101.0 + i * 0.1)
        s._lows[symbol].append(99.0 + i * 0.1)
    s._bar_count[symbol] = n


def _state_with_buffers(
    symbol: str = "RELIANCE",
    n_bars: int = 60,
    saved_date: str | None = None,
) -> StrategyState:
    """Build a saved StrategyState with pre-filled OHLCV buffers."""
    closes = [100.0 + i * 0.1 for i in range(n_bars)]
    highs  = [101.0 + i * 0.1 for i in range(n_bars)]
    lows   = [99.0  + i * 0.1 for i in range(n_bars)]
    return StrategyState(
        strategy_name="trend_15m_test",
        custom_state={
            "saved_date": saved_date or _today_ist(),
            "closes":  {symbol: closes},
            "highs":   {symbol: highs},
            "lows":    {symbol: lows},
            "prev_fast_above":    {symbol: True},
            "signal_fired_today": {symbol: ["BUY"]},
            "bar_counts":         {symbol: n_bars},
        },
    )


# ── get_state serialization ───────────────────────────────────────────────────

class TestGetState:
    def test_empty_strategy_custom_state_is_empty_dicts(self):
        s = _strategy()
        state = s.get_state()
        assert state.custom_state["closes"] == {}
        assert state.custom_state["highs"]  == {}
        assert state.custom_state["lows"]   == {}

    def test_buffers_serialized_to_lists(self):
        s = _strategy()
        _load_buffers(s, "RELIANCE", 10)
        state = s.get_state()
        assert isinstance(state.custom_state["closes"]["RELIANCE"], list)
        assert len(state.custom_state["closes"]["RELIANCE"]) == 10

    def test_saved_date_is_today_ist(self):
        s = _strategy()
        state = s.get_state()
        assert state.custom_state["saved_date"] == _today_ist()

    def test_signal_fired_today_serialized_as_list(self):
        s = _strategy()
        s._ensure("RELIANCE")
        s._signal_fired_today["RELIANCE"] = {"BUY"}
        state = s.get_state()
        assert state.custom_state["signal_fired_today"]["RELIANCE"] == ["BUY"]

    def test_bar_counts_serialized(self):
        s = _strategy()
        _load_buffers(s, "RELIANCE", 52)
        state = s.get_state()
        assert state.custom_state["bar_counts"]["RELIANCE"] == 52

    def test_multiple_symbols_all_serialized(self):
        s = _strategy()
        for sym in ["RELIANCE", "INFY", "TCS"]:
            _load_buffers(s, sym, 20)
        state = s.get_state()
        assert set(state.custom_state["closes"].keys()) == {"RELIANCE", "INFY", "TCS"}

    def test_prev_fast_above_serialized(self):
        s = _strategy()
        s._ensure("RELIANCE")
        s._prev_fast_above["RELIANCE"] = False
        state = s.get_state()
        assert state.custom_state["prev_fast_above"]["RELIANCE"] is False


# ── initialize restore — same day ─────────────────────────────────────────────

class TestInitializeSameDay:
    async def test_buffers_restored_from_saved_state(self):
        s = _strategy()
        saved = _state_with_buffers("RELIANCE", n_bars=60)
        await s.initialize(saved)
        assert len(s._closes["RELIANCE"]) == 60

    async def test_highs_and_lows_restored(self):
        s = _strategy()
        saved = _state_with_buffers("RELIANCE", n_bars=60)
        await s.initialize(saved)
        assert len(s._highs["RELIANCE"]) == 60
        assert len(s._lows["RELIANCE"]) == 60

    async def test_bar_count_restored_same_day(self):
        s = _strategy()
        saved = _state_with_buffers("RELIANCE", n_bars=60)
        await s.initialize(saved)
        assert s._bar_count["RELIANCE"] == 60

    async def test_signal_fired_today_restored_same_day(self):
        s = _strategy()
        saved = _state_with_buffers("RELIANCE", n_bars=60)
        await s.initialize(saved)
        assert "BUY" in s._signal_fired_today["RELIANCE"]

    async def test_prev_fast_above_restored(self):
        s = _strategy()
        saved = _state_with_buffers("RELIANCE", n_bars=60)
        await s.initialize(saved)
        assert s._prev_fast_above["RELIANCE"] is True

    async def test_strategy_is_initialized(self):
        s = _strategy()
        saved = _state_with_buffers("RELIANCE", n_bars=60)
        await s.initialize(saved)
        assert s.is_initialized

    async def test_buffer_capped_at_maxlen(self):
        s = _strategy()
        n_bars = s._buf + 50  # more than buffer allows
        saved = _state_with_buffers("RELIANCE", n_bars=n_bars)
        await s.initialize(saved)
        assert len(s._closes["RELIANCE"]) == s._buf


# ── initialize restore — new day (overnight restart) ─────────────────────────

class TestInitializeNewDay:
    async def test_buffers_restored_across_day_boundary(self):
        s = _strategy()
        yesterday = "2026-06-12"
        saved = _state_with_buffers("RELIANCE", n_bars=60, saved_date=yesterday)
        await s.initialize(saved)
        # OHLCV buffers survive the day boundary
        assert len(s._closes["RELIANCE"]) == 60

    async def test_bar_count_reset_on_new_day(self):
        s = _strategy()
        yesterday = "2026-06-12"
        saved = _state_with_buffers("RELIANCE", n_bars=60, saved_date=yesterday)
        await s.initialize(saved)
        # bar_count must reset to 0 for new-day min_bars gate
        assert s._bar_count.get("RELIANCE", 0) == 0

    async def test_signal_fired_today_reset_on_new_day(self):
        s = _strategy()
        yesterday = "2026-06-12"
        saved = _state_with_buffers("RELIANCE", n_bars=60, saved_date=yesterday)
        await s.initialize(saved)
        # Daily signal tracking resets so the new day can fire fresh signals
        assert s._signal_fired_today.get("RELIANCE", set()) == set()


# ── initialize — cold start ────────────────────────────────────────────────────

class TestInitializeColdStart:
    async def test_none_state_cold_start(self):
        s = _strategy()
        await s.initialize(None)
        assert s.is_initialized
        assert s._closes == {}

    async def test_empty_custom_state_cold_start(self):
        s = _strategy()
        saved = StrategyState(strategy_name="trend_15m_test")
        await s.initialize(saved)
        assert s._closes == {}


# ── round-trip: get_state → initialize → get_state ───────────────────────────

class TestRoundTrip:
    async def test_round_trip_preserves_buffer_values(self):
        s1 = _strategy()
        _load_buffers(s1, "RELIANCE", 80)
        state = s1.get_state()

        s2 = _strategy()
        await s2.initialize(state)
        restored_state = s2.get_state()

        orig_closes = state.custom_state["closes"]["RELIANCE"]
        rt_closes   = restored_state.custom_state["closes"]["RELIANCE"]
        assert orig_closes == pytest.approx(rt_closes)

    async def test_round_trip_preserves_multiple_symbols(self):
        s1 = _strategy()
        for sym in ["RELIANCE", "INFY"]:
            _load_buffers(s1, sym, 50)
        state = s1.get_state()

        s2 = _strategy()
        await s2.initialize(state)

        assert "RELIANCE" in s2._closes
        assert "INFY" in s2._closes
