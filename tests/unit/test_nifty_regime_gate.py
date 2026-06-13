"""Unit tests for NiftyRegimeGate (ADR-031 follow-up)."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from shared.models.signal import Direction  # noqa: E402
from strategy_engine.strategies.base_strategy import Bar, DataQuality  # noqa: E402
from strategy_engine.strategies.nifty_regime_gate import NiftyRegimeGate  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

_TS = datetime(2026, 6, 13, 4, 0, 0, tzinfo=UTC)  # 09:30 IST


def _bar(
    symbol: str = "NIFTY 50",
    close: float = 24_000.0,
    high: float | None = None,
    low: float | None = None,
    volume: int = 1_000_000,
    ts: datetime | None = None,
) -> Bar:
    return Bar(
        symbol=symbol,
        market="NSE",
        open=close,
        high=high or close + 50,
        low=low or close - 50,
        close=close,
        volume=volume,
        timestamp=ts or _TS,
        interval="minute",
        data_quality=DataQuality.NORMAL,
    )


# ── symbol property ───────────────────────────────────────────────────────────

class TestGateSymbol:
    def test_default_symbol_is_nifty_50(self):
        gate = NiftyRegimeGate()
        assert gate.symbol == "NIFTY 50"

    def test_custom_symbol_stored(self):
        gate = NiftyRegimeGate(nifty_symbol="NIFTY BANK")
        assert gate.symbol == "NIFTY BANK"


# ── fail-open behavior ────────────────────────────────────────────────────────

class TestFailOpen:
    def test_no_data_allows_buy(self):
        gate = NiftyRegimeGate()
        assert gate.is_allowed(Direction.BUY) is True

    def test_no_data_allows_sell(self):
        gate = NiftyRegimeGate()
        assert gate.is_allowed(Direction.SELL) is True

    def test_vwap_is_none_before_any_bar(self):
        gate = NiftyRegimeGate()
        assert gate.vwap is None

    def test_non_nifty_bar_ignored(self):
        gate = NiftyRegimeGate()
        gate.on_bar(_bar(symbol="RELIANCE", close=2500.0))
        assert gate.vwap is None
        assert gate.is_allowed(Direction.BUY) is True


# ── VWAP computation ──────────────────────────────────────────────────────────

class TestVwapComputation:
    def test_single_bar_vwap_equals_typical_price(self):
        gate = NiftyRegimeGate()
        gate.on_bar(_bar(close=24_000.0, high=24_050.0, low=23_950.0))
        expected_tp = (24_050.0 + 23_950.0 + 24_000.0) / 3.0
        assert gate.vwap == pytest.approx(expected_tp)

    def test_two_equal_bars_vwap_equals_typical_price(self):
        gate = NiftyRegimeGate()
        gate.on_bar(_bar(close=24_000.0, high=24_100.0, low=23_900.0))
        gate.on_bar(_bar(close=24_000.0, high=24_100.0, low=23_900.0))
        expected_tp = (24_100.0 + 23_900.0 + 24_000.0) / 3.0
        assert gate.vwap == pytest.approx(expected_tp)

    def test_higher_volume_bar_pulls_vwap(self):
        gate = NiftyRegimeGate()
        # Low-volume bar at 23_000, high-volume bar at 25_000
        gate.on_bar(_bar(close=23_000.0, high=23_050.0, low=22_950.0, volume=100))
        gate.on_bar(_bar(close=25_000.0, high=25_050.0, low=24_950.0, volume=9_900))
        vwap = gate.vwap
        # VWAP should be much closer to 25_000 than 23_000
        assert vwap > 24_500.0

    def test_zero_volume_bar_treated_as_volume_1(self):
        gate = NiftyRegimeGate()
        gate.on_bar(_bar(close=24_000.0, high=24_050.0, low=23_950.0, volume=0))
        assert gate.vwap is not None


# ── directional filtering ─────────────────────────────────────────────────────

class TestDirectionalFilter:
    def _gate_with_close_above_vwap(self) -> NiftyRegimeGate:
        """Gate where most recent close is above VWAP."""
        gate = NiftyRegimeGate()
        # First bar: typical low range → VWAP set low
        gate.on_bar(_bar(close=23_500.0, high=23_550.0, low=23_450.0, volume=10_000))
        # Second bar: high close → last close above the average VWAP
        gate.on_bar(_bar(close=24_500.0, high=24_550.0, low=24_450.0, volume=1))
        return gate

    def _gate_with_close_below_vwap(self) -> NiftyRegimeGate:
        """Gate where most recent close is below VWAP."""
        gate = NiftyRegimeGate()
        gate.on_bar(_bar(close=24_500.0, high=24_550.0, low=24_450.0, volume=10_000))
        gate.on_bar(_bar(close=23_500.0, high=23_550.0, low=23_450.0, volume=1))
        return gate

    def test_buy_allowed_when_close_above_vwap(self):
        gate = self._gate_with_close_above_vwap()
        assert gate.is_allowed(Direction.BUY) is True

    def test_sell_blocked_when_close_above_vwap(self):
        gate = self._gate_with_close_above_vwap()
        assert gate.is_allowed(Direction.SELL) is False

    def test_sell_allowed_when_close_below_vwap(self):
        gate = self._gate_with_close_below_vwap()
        assert gate.is_allowed(Direction.SELL) is True

    def test_buy_blocked_when_close_below_vwap(self):
        gate = self._gate_with_close_below_vwap()
        assert gate.is_allowed(Direction.BUY) is False

    def test_buy_allowed_when_close_equals_vwap(self):
        gate = NiftyRegimeGate()
        gate.on_bar(_bar(close=24_000.0, high=24_000.0, low=24_000.0, volume=1_000))
        # VWAP = TP = (24000+24000+24000)/3 = 24000; close == vwap
        assert gate.is_allowed(Direction.BUY) is True

    def test_sell_allowed_when_close_equals_vwap(self):
        gate = NiftyRegimeGate()
        gate.on_bar(_bar(close=24_000.0, high=24_000.0, low=24_000.0, volume=1_000))
        assert gate.is_allowed(Direction.SELL) is True


# ── IST day-boundary auto-reset ───────────────────────────────────────────────

class TestDayBoundaryReset:
    def test_new_ist_day_resets_vwap(self):
        gate = NiftyRegimeGate()
        gate.on_bar(_bar(close=24_000.0, ts=datetime(2026, 6, 12, 4, 0, tzinfo=UTC)))
        assert gate.vwap is not None

        # First bar of next IST day (2026-06-13)
        gate.on_bar(_bar(close=25_000.0, ts=datetime(2026, 6, 13, 4, 0, tzinfo=UTC)))
        # VWAP should only reflect the new day's bar
        new_tp = (25_000.0 + 25_050.0 + 24_950.0) / 3.0
        assert gate.vwap == pytest.approx(new_tp)

    def test_same_ist_day_accumulates(self):
        gate = NiftyRegimeGate()
        ts1 = datetime(2026, 6, 13, 3, 45, tzinfo=UTC)   # 09:15 IST
        ts2 = datetime(2026, 6, 13, 4, 0, tzinfo=UTC)    # 09:30 IST
        gate.on_bar(_bar(close=24_000.0, ts=ts1))
        gate.on_bar(_bar(close=24_000.0, ts=ts2))
        # Two bars accumulated
        assert gate._vol > 1.0


# ── integration: ORB strategy gate wiring ────────────────────────────────────

class TestORBGateWiring:
    def test_orb_has_gate_by_default(self):
        from strategy_engine.strategies.orb_strategy import ORBStrategy
        orb = ORBStrategy()
        assert orb._nifty_gate is not None

    def test_orb_gate_disabled(self):
        from strategy_engine.strategies.orb_strategy import ORBStrategy
        orb = ORBStrategy(enable_nifty_gate=False)
        assert orb._nifty_gate is None

    async def test_orb_nifty_bar_updates_gate(self):
        from strategy_engine.strategies.orb_strategy import ORBStrategy
        orb = ORBStrategy()
        nifty_bar = _bar(symbol="NIFTY 50", close=24_000.0)
        await orb.on_bar(nifty_bar)
        assert orb._nifty_gate.vwap is not None

    async def test_orb_nifty_bar_not_treated_as_stock(self):
        from strategy_engine.strategies.orb_strategy import ORBStrategy
        orb = ORBStrategy()
        nifty_bar = _bar(symbol="NIFTY 50", close=24_000.0)
        await orb.on_bar(nifty_bar)
        # NIFTY should not appear in ORB's per-symbol price history
        assert "NIFTY 50" not in orb._or_high


# ── integration: trend_15m strategy gate wiring ───────────────────────────────

class TestTrend15mGateWiring:
    def test_trend15m_has_gate_by_default(self):
        from strategy_engine.strategies.intraday_trend_15m_strategy import IntradayTrend15mStrategy
        s = IntradayTrend15mStrategy()
        assert s._nifty_gate is not None

    def test_trend15m_gate_disabled(self):
        from strategy_engine.strategies.intraday_trend_15m_strategy import IntradayTrend15mStrategy
        s = IntradayTrend15mStrategy(enable_nifty_gate=False)
        assert s._nifty_gate is None

    async def test_trend15m_nifty_bar_updates_gate(self):
        from strategy_engine.strategies.intraday_trend_15m_strategy import IntradayTrend15mStrategy
        s = IntradayTrend15mStrategy()
        nifty_bar = _bar(symbol="NIFTY 50", close=24_000.0)
        await s.on_bar(nifty_bar)
        assert s._nifty_gate.vwap is not None

    async def test_trend15m_nifty_bar_not_buffered_as_equity(self):
        from strategy_engine.strategies.intraday_trend_15m_strategy import IntradayTrend15mStrategy
        s = IntradayTrend15mStrategy()
        nifty_bar = _bar(symbol="NIFTY 50", close=24_000.0)
        await s.on_bar(nifty_bar)
        assert "NIFTY 50" not in s._closes
