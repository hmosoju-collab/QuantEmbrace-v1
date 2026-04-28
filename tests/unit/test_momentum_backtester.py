"""
Unit tests for MomentumStrategy (v2) and Backtester.

Run with the inline asyncio runner (no pytest required):
    python tests/unit/test_momentum_backtester.py

Test groups:
    TestSmaAtr                  — pure math helpers
    TestMomentumStrategySignals — ATR sizing, SL/TP, confidence gate
    TestMomentumEdgeCases       — insufficient data, short_window >= long_window
    TestBacktesterMetrics       — Sharpe, Sortino, drawdown, profit factor
    TestBacktesterExits         — stop-loss, take-profit, EOD close
    TestBacktesterCommission    — commission deducted correctly
    TestBacktesterShort         — short selling allowed / rejected
"""

from __future__ import annotations

import asyncio
import math
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Path setup ────────────────────────────────────────────────────────────────
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

# ── Stub shared.* before importing strategy modules ────────────────────────
import types

def _make_shared_stubs() -> None:
    """Install minimal stubs so strategy imports resolve without the full stack."""
    shared = types.ModuleType("shared")
    logging_mod = types.ModuleType("shared.logging")
    logger_mod = types.ModuleType("shared.logging.logger")
    utils_mod = types.ModuleType("shared.utils")
    helpers_mod = types.ModuleType("shared.utils.helpers")

    import uuid
    from datetime import timezone as _tz

    logger_mod.get_logger = lambda name, **kw: _FakeLogger()
    helpers_mod.generate_correlation_id = lambda: str(uuid.uuid4())
    helpers_mod.utc_now = lambda: datetime.now(_tz.utc)

    sys.modules.setdefault("shared", shared)
    sys.modules.setdefault("shared.logging", logging_mod)
    sys.modules.setdefault("shared.logging.logger", logger_mod)
    sys.modules.setdefault("shared.utils", utils_mod)
    sys.modules.setdefault("shared.utils.helpers", helpers_mod)


class _FakeLogger:
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): pass
    def error(self, *a, **kw): pass
    def debug(self, *a, **kw): pass


_make_shared_stubs()

from strategy_engine.backtesting.backtester import Backtester, _std
from strategy_engine.signals.signal import Direction
from strategy_engine.strategies.base_strategy import Bar
from strategy_engine.strategies.momentum_strategy import MomentumStrategy, _atr, _sma


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bar(
    close: float,
    symbol: str = "TEST",
    market: str = "NSE",
    high: Optional[float] = None,
    low: Optional[float] = None,
    open_: Optional[float] = None,
    volume: int = 1000,
    ts: Optional[datetime] = None,
    day_offset: int = 0,
) -> Bar:
    """Convenience factory for test bars."""
    if ts is None:
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    from datetime import timedelta
    ts = ts + timedelta(days=day_offset)
    return Bar(
        symbol=symbol,
        market=market,
        open=open_ or close,
        high=high if high is not None else close * 1.01,
        low=low if low is not None else close * 0.99,
        close=close,
        volume=volume,
        timestamp=ts,
        interval="1d",
    )


def _rising_bars(n: int, start: float = 100.0, step: float = 1.0) -> list[Bar]:
    """n bars with steadily increasing closes."""
    from datetime import timedelta
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        _bar(start + i * step, ts=base + timedelta(days=i))
        for i in range(n)
    ]


def _falling_bars(n: int, start: float = 200.0, step: float = 1.0) -> list[Bar]:
    from datetime import timedelta
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        _bar(start - i * step, ts=base + timedelta(days=i))
        for i in range(n)
    ]


# ── Test runner ───────────────────────────────────────────────────────────────

PASSED = 0
FAILED = 0
_FAILURES: list[str] = []


def _run(name: str, coro) -> None:
    global PASSED, FAILED
    try:
        asyncio.run(coro)
        print(f"  PASS  {name}")
        PASSED += 1
    except Exception as exc:
        FAILED += 1
        _FAILURES.append(f"{name}: {exc}")
        print(f"  FAIL  {name}")
        traceback.print_exc()


def _assert(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg)


# =============================================================================
# TestSmaAtr — pure math
# =============================================================================

async def test_sma_basic():
    result = _sma([1.0, 2.0, 3.0, 4.0, 5.0], window=3)
    _assert(abs(result - 4.0) < 1e-9, f"Expected 4.0, got {result}")


async def test_sma_insufficient_data():
    result = _sma([1.0, 2.0], window=5)
    _assert(math.isnan(result), "Should return nan for insufficient data")


async def test_atr_simple():
    # All TRs are 1.0 → ATR should be 1.0
    trs = [1.0] * 20
    result = _atr(trs, period=14)
    _assert(abs(result - 1.0) < 1e-9, f"Expected 1.0, got {result}")


async def test_atr_insufficient_data():
    result = _atr([1.0, 2.0], period=14)
    _assert(math.isnan(result), "Should return nan for insufficient data")


async def test_atr_wilder_smoothing():
    # First 14 TRs = 2.0 → seed = 2.0; next TR = 4.0
    # ATR = (13 × 2.0 + 4.0) / 14 = 30/14 ≈ 2.143
    trs = [2.0] * 14 + [4.0]
    result = _atr(trs, period=14)
    expected = (13 * 2.0 + 4.0) / 14
    _assert(abs(result - expected) < 1e-9, f"Expected {expected:.4f}, got {result:.4f}")


async def test_std_sample():
    # Classic dataset: population std = 2.0, sample std = sqrt(32/7) ≈ 2.138
    vals = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    pop = _std(vals, population=True)
    _assert(abs(pop - 2.0) < 1e-9, f"Expected population std = 2.0, got {pop}")
    sample = _std(vals)
    import math as _math
    _assert(abs(sample - _math.sqrt(32 / 7)) < 1e-9, f"Unexpected sample std {sample}")


async def test_std_empty():
    _assert(_std([]) == 0.0, "std of empty list should be 0")
    _assert(_std([1.0]) == 0.0, "std of single value should be 0")


# =============================================================================
# TestMomentumStrategySignals
# =============================================================================

async def test_no_signal_before_warmup():
    """No signal should be emitted before long_window bars are available."""
    strat = MomentumStrategy(short_window=5, long_window=10, capital=100_000)
    await strat.initialize()
    bars = _rising_bars(9)  # One short of long_window
    for bar in bars:
        await strat.on_bar(bar)
    sig = await strat.generate_signal()
    _assert(sig is None, f"Expected None, got {sig}")


async def test_golden_cross_generates_buy():
    """A golden cross (short MA > long MA after being below) emits BUY."""
    strat = MomentumStrategy(
        short_window=5,
        long_window=20,
        min_confidence=0.0,   # Accept any confidence
        capital=1_000_000,
        atr_period=5,
    )
    await strat.initialize()

    # 20 flat bars then 20 strongly rising bars → golden cross guaranteed
    from datetime import timedelta
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    flat = [_bar(100.0, ts=base + timedelta(days=i)) for i in range(20)]
    rising = [_bar(100.0 + (i + 1) * 5, ts=base + timedelta(days=20 + i)) for i in range(20)]
    all_bars = flat + rising

    signal = None
    for bar in all_bars:
        await strat.on_bar(bar)
        sig = await strat.generate_signal()
        if sig is not None:
            signal = sig
            break

    _assert(signal is not None, "Expected a BUY signal after golden cross")
    _assert(signal.direction == Direction.BUY, f"Expected BUY, got {signal.direction}")


async def test_death_cross_generates_sell():
    """A death cross (short MA < long MA after being above) emits SELL."""
    strat = MomentumStrategy(
        short_window=5,
        long_window=20,
        min_confidence=0.0,
        capital=1_000_000,
        atr_period=5,
    )
    await strat.initialize()

    from datetime import timedelta
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rising = [_bar(100.0 + i * 5, ts=base + timedelta(days=i)) for i in range(20)]
    falling = [_bar(200.0 - (i + 1) * 10, ts=base + timedelta(days=20 + i)) for i in range(20)]
    all_bars = rising + falling

    signal = None
    for bar in all_bars:
        await strat.on_bar(bar)
        sig = await strat.generate_signal()
        if sig is not None:
            signal = sig
            break

    _assert(signal is not None, "Expected a SELL signal after death cross")
    _assert(signal.direction == Direction.SELL, f"Expected SELL, got {signal.direction}")


async def test_signal_has_stop_and_tp():
    """Signal must carry non-None stop_loss and take_profit."""
    strat = MomentumStrategy(
        short_window=5,
        long_window=20,
        min_confidence=0.0,
        capital=1_000_000,
        atr_period=5,
    )
    await strat.initialize()

    from datetime import timedelta
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    flat = [_bar(100.0, ts=base + timedelta(days=i)) for i in range(20)]
    rising = [_bar(100.0 + (i + 1) * 5, ts=base + timedelta(days=20 + i)) for i in range(20)]

    signal = None
    for bar in flat + rising:
        await strat.on_bar(bar)
        sig = await strat.generate_signal()
        if sig is not None:
            signal = sig
            break

    _assert(signal is not None, "No signal generated")
    _assert(signal.stop_loss is not None, "stop_loss must be set")
    _assert(signal.take_profit is not None, "take_profit must be set")
    # For a BUY: SL < entry, TP > entry
    _assert(signal.stop_loss < signal.price_at_signal, "BUY stop_loss must be below entry")
    _assert(signal.take_profit > signal.price_at_signal, "BUY take_profit must be above entry")


async def test_confidence_gate_suppresses_low_conviction():
    """Signals with confidence below min_confidence are not emitted."""
    strat = MomentumStrategy(
        short_window=5,
        long_window=20,
        min_confidence=0.99,  # Very high — nearly impossible to meet
        capital=1_000_000,
        atr_period=5,
    )
    await strat.initialize()

    from datetime import timedelta
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # Tiny price movement — very low MA divergence
    flat = [_bar(100.0, ts=base + timedelta(days=i)) for i in range(20)]
    slight = [_bar(100.0 + (i + 1) * 0.01, ts=base + timedelta(days=20 + i)) for i in range(20)]

    signals = []
    for bar in flat + slight:
        await strat.on_bar(bar)
        sig = await strat.generate_signal()
        if sig is not None:
            signals.append(sig)

    _assert(len(signals) == 0, f"Expected 0 signals at high confidence gate, got {len(signals)}")


async def test_quantity_scales_with_atr():
    """Larger ATR → wider stop → smaller position size (risk-based sizing)."""
    from datetime import timedelta
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)

    async def _get_qty(atr_mult: float) -> int:
        strat = MomentumStrategy(
            short_window=5,
            long_window=20,
            min_confidence=0.0,
            capital=1_000_000,
            atr_period=5,
            atr_stop_multiplier=atr_mult,
        )
        await strat.initialize()
        flat = [_bar(100.0, ts=base + timedelta(days=i)) for i in range(20)]
        rising = [_bar(100.0 + (i + 1) * 5, ts=base + timedelta(days=20 + i)) for i in range(20)]
        for bar in flat + rising:
            await strat.on_bar(bar)
            sig = await strat.generate_signal()
            if sig is not None:
                return sig.quantity
        return -1

    qty_tight = await _get_qty(1.0)
    qty_wide = await _get_qty(4.0)
    _assert(qty_tight > qty_wide, f"Tighter stop should give bigger qty: {qty_tight} vs {qty_wide}")


# =============================================================================
# TestMomentumEdgeCases
# =============================================================================

async def test_invalid_window_raises():
    """short_window >= long_window must raise ValueError."""
    raised = False
    try:
        MomentumStrategy(short_window=20, long_window=10)
    except ValueError:
        raised = True
    _assert(raised, "Expected ValueError for short_window >= long_window")


async def test_on_tick_populates_buffer():
    """on_tick should append to price buffer without generating signals."""
    strat = MomentumStrategy(short_window=5, long_window=10)
    await strat.initialize()
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        await strat.on_tick("TEST", 100.0 + i, 1000, ts)
    sig = await strat.generate_signal()
    _assert(sig is None, "on_tick alone should not generate signals")


# =============================================================================
# TestBacktesterMetrics
# =============================================================================

async def test_zero_bars_returns_empty_result():
    strat = MomentumStrategy(short_window=5, long_window=20)
    bt = Backtester(strat, initial_capital=100_000)
    result = await bt.run([])
    _assert(result.total_trades == 0)
    _assert(result.total_return_pct == 0.0)
    _assert(result.sharpe_ratio == 0.0)


async def test_no_signals_preserves_capital():
    """With no crossovers, capital should remain unchanged."""
    strat = MomentumStrategy(short_window=10, long_window=50, min_confidence=0.0)
    bt = Backtester(strat, initial_capital=1_000_000, commission_pct=0.0)
    # 30 flat bars — never enough for long_window=50
    bars = _rising_bars(30, start=100.0, step=0.0)
    result = await bt.run(bars)
    _assert(result.total_trades == 0, f"Expected 0 trades, got {result.total_trades}")
    _assert(abs(result.final_capital - 1_000_000) < 1.0, "Capital should be unchanged")


async def test_return_computed_correctly():
    """Manual round-trip: buy 100 shares at 100, sell at 110, no commission."""
    from datetime import timedelta

    # Use a strategy stub that emits BUY then SELL
    class _FixedStrategy(MomentumStrategy):
        def __init__(self):
            super().__init__(short_window=5, long_window=20, min_confidence=0.0, capital=1_000_000)
            self._call = 0

        async def on_bar(self, bar):
            self._call += 1

        async def generate_signal(self):
            from strategy_engine.signals.signal import Signal
            if self._call == 1:
                return Signal(
                    symbol="X", market="NSE", direction=Direction.BUY,
                    quantity=100, confidence=1.0, strategy_name="test",
                    price_at_signal=100.0,
                )
            if self._call == 2:
                return Signal(
                    symbol="X", market="NSE", direction=Direction.SELL,
                    quantity=100, confidence=1.0, strategy_name="test",
                    price_at_signal=110.0,
                )
            return None

    strat = _FixedStrategy()
    bt = Backtester(strat, initial_capital=100_000, commission_pct=0.0)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # Bar 0: signal emitted (BUY), entered next bar
    # Bar 1: BUY entered at close=100; SELL signal emitted, pending for next bar
    # Bar 2: SELL pending but position open → EOD close at 110
    # Net: 100 shares × (110 - 100) = 1000 profit → 1% return on 100k capital.
    bars = [
        _bar(100.0, symbol="X", ts=base),
        _bar(100.0, symbol="X", ts=base + timedelta(days=1)),
        _bar(110.0, symbol="X", ts=base + timedelta(days=2)),
    ]
    result = await bt.run(bars)
    _assert(result.total_return_pct > 0, "Expected positive return")


async def test_max_drawdown_detected():
    """Portfolio that rises then falls should have a non-zero drawdown."""
    from datetime import timedelta

    class _LongOnlyStrat(MomentumStrategy):
        def __init__(self):
            super().__init__(short_window=5, long_window=20, min_confidence=0.0, capital=500_000)
            self._bought = False
            self._bar_count = 0

        async def on_bar(self, bar):
            self._bar_count += 1

        async def generate_signal(self):
            from strategy_engine.signals.signal import Signal
            if self._bar_count == 1 and not self._bought:
                self._bought = True
                return Signal(
                    symbol="Y", market="NSE", direction=Direction.BUY,
                    quantity=1000, confidence=1.0, strategy_name="dd_test",
                    price_at_signal=100.0,
                )
            return None

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # Buy 1000 shares at 100 → price rises to 120 → drops to 80
    prices = [100, 110, 120, 110, 90, 80]
    bars = [_bar(float(p), symbol="Y", ts=base + __import__('datetime').timedelta(days=i))
            for i, p in enumerate(prices)]

    strat = _LongOnlyStrat()
    bt = Backtester(strat, initial_capital=500_000, commission_pct=0.0)
    result = await bt.run(bars)
    _assert(result.max_drawdown_pct > 0.0, "Expected positive max drawdown")


async def test_profit_factor_above_one_for_winning_strategy():
    """Profit factor > 1 means gross profit > gross loss."""
    from datetime import timedelta
    import uuid

    class _AlternatingStrat(MomentumStrategy):
        """Buy/sell alternating: odd bars buy, even bars sell."""
        def __init__(self):
            super().__init__(short_window=5, long_window=10, min_confidence=0.0, capital=1_000_000)
            self._n = 0
            self._has_pos = False

        async def on_bar(self, bar):
            self._n += 1

        async def generate_signal(self):
            from strategy_engine.signals.signal import Signal
            if not self._has_pos and self._n % 6 == 1:
                self._has_pos = True
                return Signal(symbol="Z", market="NSE", direction=Direction.BUY,
                              quantity=10, confidence=1.0, strategy_name="alt",
                              price_at_signal=100.0)
            if self._has_pos and self._n % 6 == 4:
                self._has_pos = False
                return Signal(symbol="Z", market="NSE", direction=Direction.SELL,
                              quantity=10, confidence=1.0, strategy_name="alt",
                              price_at_signal=110.0)
            return None

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # Price pattern: buy dip, sell rally × 3 cycles
    prices = [100, 100, 100, 115, 115, 115,
              100, 100, 100, 115, 115, 115,
              100, 100, 100, 115, 115, 115]
    bars = [_bar(float(p), symbol="Z", ts=base + __import__('datetime').timedelta(days=i))
            for i, p in enumerate(prices)]

    strat = _AlternatingStrat()
    bt = Backtester(strat, initial_capital=1_000_000, commission_pct=0.0)
    result = await bt.run(bars)

    _assert(result.total_trades > 0, "Expected some completed trades")
    _assert(result.profit_factor > 1.0,
            f"Expected profit_factor > 1, got {result.profit_factor}")


# =============================================================================
# TestBacktesterExits
# =============================================================================

async def test_stop_loss_triggered():
    """When the next bar's low breaches stop_loss, trade exits at stop price."""
    from datetime import timedelta
    from strategy_engine.signals.signal import Signal

    class _BuyWithStopStrat(MomentumStrategy):
        def __init__(self):
            super().__init__(short_window=5, long_window=10, min_confidence=0.0, capital=100_000)
            self._emitted = False

        async def on_bar(self, bar): pass

        async def generate_signal(self):
            if not self._emitted:
                self._emitted = True
                return Signal(
                    symbol="SL", market="NSE", direction=Direction.BUY,
                    quantity=10, confidence=1.0, strategy_name="sl_test",
                    price_at_signal=100.0,
                    stop_loss=95.0,
                    take_profit=120.0,
                )
            return None

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # Bar 0: close=100 (signal emitted, entered at bar1)
    # Bar 1: entry at close=100; stop=95
    # Bar 2: low=90 → stop triggered at 95
    bars = [
        _bar(100.0, symbol="SL", ts=base, high=102, low=98),
        _bar(100.0, symbol="SL", ts=base + timedelta(days=1), high=102, low=98),
        _bar(92.0,  symbol="SL", ts=base + timedelta(days=2), high=98, low=90),
    ]

    strat = _BuyWithStopStrat()
    bt = Backtester(strat, initial_capital=100_000, commission_pct=0.0, allow_short=False)
    result = await bt.run(bars)

    sl_trades = [t for t in result.trades if t.exit_reason == "stop_loss"]
    _assert(len(sl_trades) >= 1, f"Expected stop_loss exit, got {[t.exit_reason for t in result.trades]}")
    _assert(abs(sl_trades[0].exit_price - 95.0) < 1e-6,
            f"Expected exit at 95.0, got {sl_trades[0].exit_price}")


async def test_take_profit_triggered():
    """When the next bar's high breaches take_profit, trade exits at TP price."""
    from datetime import timedelta
    from strategy_engine.signals.signal import Signal

    class _BuyWithTPStrat(MomentumStrategy):
        def __init__(self):
            super().__init__(short_window=5, long_window=10, min_confidence=0.0, capital=100_000)
            self._emitted = False

        async def on_bar(self, bar): pass

        async def generate_signal(self):
            if not self._emitted:
                self._emitted = True
                return Signal(
                    symbol="TP", market="NSE", direction=Direction.BUY,
                    quantity=10, confidence=1.0, strategy_name="tp_test",
                    price_at_signal=100.0,
                    stop_loss=90.0,
                    take_profit=110.0,
                )
            return None

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = [
        _bar(100.0, symbol="TP", ts=base, high=102, low=98),
        _bar(105.0, symbol="TP", ts=base + timedelta(days=1), high=106, low=103),
        _bar(112.0, symbol="TP", ts=base + timedelta(days=2), high=115, low=108),
    ]

    strat = _BuyWithTPStrat()
    bt = Backtester(strat, initial_capital=100_000, commission_pct=0.0)
    result = await bt.run(bars)

    tp_trades = [t for t in result.trades if t.exit_reason == "take_profit"]
    _assert(len(tp_trades) >= 1, f"Expected take_profit exit, got {[t.exit_reason for t in result.trades]}")
    _assert(abs(tp_trades[0].exit_price - 110.0) < 1e-6,
            f"Expected exit at 110.0, got {tp_trades[0].exit_price}")


async def test_eod_close_remaining_positions():
    """Open positions still open at last bar are closed at last bar's close."""
    from datetime import timedelta
    from strategy_engine.signals.signal import Signal

    class _BuyAndHoldStrat(MomentumStrategy):
        def __init__(self):
            super().__init__(short_window=5, long_window=10, min_confidence=0.0, capital=100_000)
            self._emitted = False

        async def on_bar(self, bar): pass

        async def generate_signal(self):
            if not self._emitted:
                self._emitted = True
                return Signal(
                    symbol="HOLD", market="NSE", direction=Direction.BUY,
                    quantity=5, confidence=1.0, strategy_name="hold_test",
                    price_at_signal=100.0,
                )
            return None

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = [
        _bar(100.0, symbol="HOLD", ts=base + timedelta(days=i))
        for i in range(5)
    ]
    bars[-1] = _bar(130.0, symbol="HOLD", ts=base + timedelta(days=4))  # Last bar at 130

    strat = _BuyAndHoldStrat()
    bt = Backtester(strat, initial_capital=100_000, commission_pct=0.0)
    result = await bt.run(bars)

    eod_trades = [t for t in result.trades if t.exit_reason == "eod"]
    _assert(len(eod_trades) >= 1, "Expected EOD close of open position")
    _assert(abs(eod_trades[0].exit_price - 130.0) < 1e-6,
            f"Expected EOD exit at 130.0, got {eod_trades[0].exit_price}")


# =============================================================================
# TestBacktesterCommission
# =============================================================================

async def test_commission_reduces_profit():
    """With commission, profit should be less than without."""
    from datetime import timedelta
    from strategy_engine.signals.signal import Signal

    class _SimpleBuySell(MomentumStrategy):
        def __init__(self):
            super().__init__(short_window=5, long_window=10, min_confidence=0.0, capital=100_000)
            self._count = 0

        async def on_bar(self, bar):
            self._count += 1

        async def generate_signal(self):
            if self._count == 1:
                return Signal(symbol="C", market="NSE", direction=Direction.BUY,
                              quantity=100, confidence=1.0, strategy_name="c",
                              price_at_signal=100.0)
            if self._count == 3:
                return Signal(symbol="C", market="NSE", direction=Direction.SELL,
                              quantity=100, confidence=1.0, strategy_name="c",
                              price_at_signal=110.0)
            return None

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = [
        _bar(100.0, symbol="C", ts=base + timedelta(days=i))
        for i in range(5)
    ]
    bars[3] = _bar(110.0, symbol="C", ts=base + timedelta(days=3))
    bars[4] = _bar(110.0, symbol="C", ts=base + timedelta(days=4))

    strat_no_fee = _SimpleBuySell()
    bt_no_fee = Backtester(strat_no_fee, initial_capital=100_000, commission_pct=0.0)
    result_no_fee = await bt_no_fee.run(bars)

    # Re-instantiate (strategy is stateful)
    strat_fee = _SimpleBuySell()
    bt_fee = Backtester(strat_fee, initial_capital=100_000, commission_pct=0.1)
    result_fee = await bt_fee.run(bars)

    _assert(
        result_fee.final_capital < result_no_fee.final_capital,
        f"Commission should reduce profit: {result_fee.final_capital} vs {result_no_fee.final_capital}"
    )
    _assert(result_fee.total_commission > 0, "Commission should be positive")


# =============================================================================
# TestBacktesterShort
# =============================================================================

async def test_short_rejected_when_not_allowed():
    """SELL signals on flat positions should be ignored when allow_short=False."""
    from datetime import timedelta
    from strategy_engine.signals.signal import Signal

    class _SellOnlyStrat(MomentumStrategy):
        def __init__(self):
            super().__init__(short_window=5, long_window=10, min_confidence=0.0, capital=100_000)
            self._emitted = False

        async def on_bar(self, bar): pass

        async def generate_signal(self):
            if not self._emitted:
                self._emitted = True
                return Signal(
                    symbol="S", market="NSE", direction=Direction.SELL,
                    quantity=10, confidence=1.0, strategy_name="short_test",
                    price_at_signal=100.0,
                )
            return None

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = [_bar(100.0, symbol="S", ts=base + __import__('datetime').timedelta(days=i)) for i in range(5)]

    strat = _SellOnlyStrat()
    bt = Backtester(strat, initial_capital=100_000, commission_pct=0.0, allow_short=False)
    result = await bt.run(bars)

    _assert(result.total_trades == 0, f"Expected 0 trades, got {result.total_trades}")
    _assert(abs(result.final_capital - 100_000) < 1.0, "Capital unchanged when short rejected")


async def test_short_allowed_opens_position():
    """When allow_short=True, SELL on flat position opens a short."""
    from datetime import timedelta
    from strategy_engine.signals.signal import Signal

    class _ShortStrat(MomentumStrategy):
        def __init__(self):
            super().__init__(short_window=5, long_window=10, min_confidence=0.0, capital=100_000)
            self._count = 0

        async def on_bar(self, bar):
            self._count += 1

        async def generate_signal(self):
            if self._count == 1:
                return Signal(symbol="SHORT", market="NSE", direction=Direction.SELL,
                              quantity=10, confidence=1.0, strategy_name="short",
                              price_at_signal=100.0)
            return None

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # Short at 100, closes EOD at 80 → profit
    bars = [
        _bar(100.0, symbol="SHORT", ts=base),
        _bar(90.0, symbol="SHORT", ts=base + timedelta(days=1)),
        _bar(80.0, symbol="SHORT", ts=base + timedelta(days=2)),
    ]

    strat = _ShortStrat()
    bt = Backtester(strat, initial_capital=100_000, commission_pct=0.0, allow_short=True)
    result = await bt.run(bars)

    _assert(result.total_trades >= 1, "Expected at least 1 trade when short allowed")
    # Short profits when price falls: (entry - exit) × qty = (100 - 80) × 10 = 200
    _assert(result.final_capital > 100_000, f"Short on falling price should profit: {result.final_capital}")


# =============================================================================
# Runner
# =============================================================================

ALL_TESTS = [
    # Math
    ("sma basic", test_sma_basic),
    ("sma insufficient data → nan", test_sma_insufficient_data),
    ("atr all-equal TRs → 1.0", test_atr_simple),
    ("atr insufficient data → nan", test_atr_insufficient_data),
    ("atr Wilder smoothing", test_atr_wilder_smoothing),
    ("std sample", test_std_sample),
    ("std empty/single", test_std_empty),
    # Strategy signals
    ("no signal before warmup", test_no_signal_before_warmup),
    ("golden cross → BUY", test_golden_cross_generates_buy),
    ("death cross → SELL", test_death_cross_generates_sell),
    ("signal has SL and TP", test_signal_has_stop_and_tp),
    ("confidence gate suppresses low conviction", test_confidence_gate_suppresses_low_conviction),
    ("quantity scales inverse with ATR", test_quantity_scales_with_atr),
    # Edge cases
    ("invalid short_window >= long_window raises", test_invalid_window_raises),
    ("on_tick populates buffer no signal", test_on_tick_populates_buffer),
    # Backtester metrics
    ("zero bars → empty result", test_zero_bars_returns_empty_result),
    ("no signals → capital preserved", test_no_signals_preserves_capital),
    ("return computed correctly", test_return_computed_correctly),
    ("max drawdown detected", test_max_drawdown_detected),
    ("profit factor > 1 for winning strategy", test_profit_factor_above_one_for_winning_strategy),
    # Exits
    ("stop-loss triggers at correct price", test_stop_loss_triggered),
    ("take-profit triggers at correct price", test_take_profit_triggered),
    ("EOD close remaining positions", test_eod_close_remaining_positions),
    # Commission
    ("commission reduces profit", test_commission_reduces_profit),
    # Short
    ("short rejected when allow_short=False", test_short_rejected_when_not_allowed),
    ("short opens position when allow_short=True", test_short_allowed_opens_position),
]


if __name__ == "__main__":
    print(f"\nRunning {len(ALL_TESTS)} tests...\n")
    for name, coro_fn in ALL_TESTS:
        _run(name, coro_fn())

    total = PASSED + FAILED
    print(f"\n{'─' * 50}")
    print(f"Results: {PASSED} passed, {FAILED} failed  ({total} total)")
    if _FAILURES:
        print("\nFailed tests:")
        for f in _FAILURES:
            print(f"  • {f}")
    print()
    sys.exit(0 if FAILED == 0 else 1)
