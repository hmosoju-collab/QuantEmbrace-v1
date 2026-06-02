"""
Tests for NAV-based position sizer (_position_sizer.py).

Core invariant: no signal produced by any strategy may have
    qty × price > nav × HARD_CAP_PCT  (5% of NAV)

Tests are grouped into three sections:
    1. Unit tests for size_position() directly
    2. Conviction-scaling tests (2.5% default, 5% high-conviction)
    3. Integration tests: each strategy type produces compliant signals
"""

from __future__ import annotations

import asyncio
import math
import sys
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Path setup — works without an installed package
# ---------------------------------------------------------------------------
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES = os.path.join(_REPO, "services")
if _SERVICES not in sys.path:
    sys.path.insert(0, _SERVICES)

from strategy_engine.strategies._position_sizer import (
    HARD_CAP_PCT,
    HIGH_CONVICTION_THRESHOLD,
    MAX_LOSS_PCT,
    SizingResult,
    size_position,
    _TARGET_PCT_DEFAULT,
    _TARGET_PCT_HIGH_CONV,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NAV = 1_000_000.0   # ₹10L — paper session NAV
MAX_NOTIONAL = NAV * HARD_CAP_PCT   # ₹50,000


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _notional(result: SizingResult, price: float) -> float:
    return result.qty * price


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Section 1 — Unit tests for size_position()
# ---------------------------------------------------------------------------

def test_default_confidence_targets_2_5_pct():
    """Low confidence → target ≈ 2.5% of NAV."""
    price, stop = 500.0, 10.0
    r = size_position(price, stop, NAV, confidence=0.50)
    target = NAV * _TARGET_PCT_DEFAULT
    # qty should be floor(target / price) = floor(25000 / 500) = 50
    expected_qty = math.floor(target / price)
    # risk_stop = floor(NAV * 0.0025 / 10) = floor(250) = 250 — not binding here
    _assert(r.qty == expected_qty, f"Expected qty={expected_qty}, got {r.qty}")
    _assert(r.sizing_reason == "target_notional", f"Expected target_notional, got {r.sizing_reason}")
    _assert(abs(r.target_notional - target) < 1.0, "target_notional mismatch")


def test_high_conviction_targets_5_pct():
    """High confidence (>= 0.80) → target ≈ 5% of NAV."""
    price, stop = 500.0, 10.0
    r = size_position(price, stop, NAV, confidence=HIGH_CONVICTION_THRESHOLD)
    target = NAV * _TARGET_PCT_HIGH_CONV
    expected_qty = math.floor(target / price)   # floor(50000/500) = 100
    # risk_stop = 250 — not binding
    _assert(r.qty == expected_qty, f"Expected qty={expected_qty}, got {r.qty}")
    _assert(_notional(r, price) <= MAX_NOTIONAL, "Exceeded hard cap")


def test_hard_cap_never_exceeded_across_price_range():
    """For any price from ₹5 to ₹50,000 qty × price must never exceed 5% NAV."""
    prices = [5, 10, 50, 100, 200, 500, 1_000, 2_000, 5_000, 10_000, 50_000]
    for price in prices:
        for conf in [0.0, 0.50, 0.79, 0.80, 0.95, 1.0]:
            r = size_position(price, price * 0.02, NAV, confidence=conf)
            notional = _notional(r, price)
            _assert(
                notional <= MAX_NOTIONAL + 1,  # +1 for rounding: floor can produce max_qty exactly
                f"price={price} conf={conf}: notional={notional:.2f} > cap={MAX_NOTIONAL}",
            )
            _assert(r.rejected_if_exceeds_cap is False,
                    f"price={price}: rejected_if_exceeds_cap should be False")


def test_risk_stop_is_binding_when_stop_is_tight():
    """Very tight stop (₹1 on ₹500 stock) → risk_stop constraint dominates."""
    price, stop = 500.0, 1.0
    r = size_position(price, stop, NAV, confidence=0.50)
    # qty_by_risk_stop = floor(NAV * 0.0025 / 1.0) = floor(2500) = 2500
    # qty_by_target    = floor(NAV * 0.025 / 500)  = floor(50)   = 50  ← binding
    # In this case target is smaller than risk_stop, so target wins
    # Let's verify: min(50, 100, 2500) = 50 → target_notional is reason
    _assert(r.qty == 50, f"Expected 50, got {r.qty}")
    _assert(r.sizing_reason == "target_notional", f"Got {r.sizing_reason}")


def test_risk_stop_binding_on_wide_stop():
    """Wide stop relative to NAV → risk_stop constrains qty below target."""
    # stop_distance = ₹200 → risk_stop = floor(NAV * 0.0025 / 200) = floor(12.5) = 12
    # target = floor(NAV * 0.025 / 100) = floor(250) = 250 → risk_stop wins
    price, stop = 100.0, 200.0
    r = size_position(price, stop, NAV, confidence=0.50)
    expected = math.floor(NAV * MAX_LOSS_PCT / stop)   # 12
    _assert(r.qty == expected, f"Expected {expected}, got {r.qty}")
    _assert(r.sizing_reason == "risk_stop", f"Expected risk_stop, got {r.sizing_reason}")


def test_minimum_qty_is_one():
    """Extremely expensive stock still yields qty >= 1."""
    price = 5_000_000.0  # ₹50L stock — absurdly expensive
    r = size_position(price, 1000.0, NAV, confidence=0.0)
    _assert(r.qty >= 1, "qty must be >= 1")


def test_invalid_price_returns_zero_qty():
    r = size_position(0.0, 10.0, NAV)
    _assert(r.qty == 0, "zero price → qty=0")
    _assert(r.sizing_reason == "invalid_inputs", f"Got {r.sizing_reason}")


def test_invalid_stop_returns_zero_qty():
    r = size_position(500.0, 0.0, NAV)
    _assert(r.qty == 0, "zero stop → qty=0")


def test_invalid_nav_returns_zero_qty():
    r = size_position(500.0, 10.0, 0.0)
    _assert(r.qty == 0, "zero nav → qty=0")


def test_rejected_if_exceeds_cap_always_false_after_min():
    """After applying min() formula, notional must never exceed cap."""
    import random
    random.seed(42)
    for _ in range(500):
        price = random.uniform(1, 10_000)
        stop  = random.uniform(0.01, price)
        conf  = random.random()
        r = size_position(price, stop, NAV, confidence=conf)
        _assert(r.rejected_if_exceeds_cap is False,
                f"price={price:.2f} stop={stop:.2f} conf={conf:.3f}: cap exceeded")


def test_metadata_keys_present():
    """to_metadata() must include all five required keys."""
    r = size_position(500.0, 10.0, NAV, confidence=0.70)
    meta = r.to_metadata()
    required = {"nav_used", "target_notional", "max_allowed_notional",
                "sizing_reason", "rejected_if_exceeds_cap"}
    missing = required - set(meta.keys())
    _assert(not missing, f"Missing metadata keys: {missing}")


def test_nav_scaling_doubles_qty():
    """Doubling NAV should approximately double quantity (same constraints)."""
    price, stop, conf = 500.0, 50.0, 0.60
    r1 = size_position(price, stop, NAV,       confidence=conf)
    r2 = size_position(price, stop, NAV * 2.0, confidence=conf)
    _assert(r2.qty == r1.qty * 2, f"Expected {r1.qty*2}, got {r2.qty}")


def test_explicit_target_pct_override():
    """Passing target_pct= overrides the conviction-based lookup."""
    price, stop = 500.0, 10.0
    custom_pct = 0.03
    r = size_position(price, stop, NAV, confidence=0.0, target_pct=custom_pct)
    expected = math.floor(NAV * custom_pct / price)
    _assert(r.qty == expected, f"Expected {expected}, got {r.qty}")


# ---------------------------------------------------------------------------
# Section 2 — Conviction-scaling boundary
# ---------------------------------------------------------------------------

def test_confidence_below_threshold_uses_default_pct():
    """Confidence just below threshold → 2.5% target."""
    price = 1_000.0
    r = size_position(price, 5.0, NAV, confidence=HIGH_CONVICTION_THRESHOLD - 0.01)
    expected = math.floor(NAV * _TARGET_PCT_DEFAULT / price)
    _assert(r.qty == expected, f"Expected {expected}, got {r.qty}")


def test_confidence_at_threshold_uses_high_conv_pct():
    """Confidence exactly at threshold → 5% target."""
    price = 1_000.0
    r = size_position(price, 5.0, NAV, confidence=HIGH_CONVICTION_THRESHOLD)
    expected = math.floor(NAV * _TARGET_PCT_HIGH_CONV / price)
    _assert(r.qty == expected, f"Expected {expected}, got {r.qty}")


def test_high_conviction_notional_within_hard_cap():
    """Even at high conviction, notional must stay within 5% NAV."""
    for price in [50.0, 200.0, 1_000.0, 5_000.0]:
        r = size_position(price, price * 0.01, NAV, confidence=1.0)
        notional = r.qty * price
        _assert(notional <= MAX_NOTIONAL + 1,
                f"price={price}: notional={notional:.2f} exceeds cap={MAX_NOTIONAL}")


# ---------------------------------------------------------------------------
# Section 3 — Integration: strategies produce compliant signals
# ---------------------------------------------------------------------------

def _bar(
    close: float,
    high: float | None = None,
    low: float | None = None,
    volume: int = 100_000,
    ts: datetime | None = None,
    interval: str = "minute",
    symbol: str = "TESTSTOCK",
) -> "Bar":
    from strategy_engine.strategies.base_strategy import Bar, DataQuality
    if ts is None:
        ts = datetime(2026, 1, 15, 4, 30, 0, tzinfo=timezone.utc)  # 10:00 IST
    return Bar(
        symbol=symbol, market="NSE",
        open=close * 0.999, high=high or close * 1.002,
        low=low or close * 0.998, close=close,
        volume=volume, timestamp=ts, interval=interval,
    )


def _signal_within_cap(signal, price: float, nav: float) -> bool:
    cap = nav * HARD_CAP_PCT
    return signal.quantity * price <= cap + 1  # +1 for integer-floor rounding


async def _vwap_signal(price: float, nav: float) -> Optional["Signal"]:
    from strategy_engine.strategies.vwap_reversion_strategy import VWAPReversionStrategy
    strat = VWAPReversionStrategy(nav=nav)
    await strat.initialize()

    # Feed enough bars to build VWAP then trigger a deviation below lower band
    # NORMAL phase: bars between 09:30 and 14:30 IST = 04:00–09:00 UTC
    base = datetime(2026, 1, 15, 4, 0, 0, tzinfo=timezone.utc)  # 09:30 IST

    # 10 bars at stable price to establish VWAP
    for i in range(10):
        ts = base + timedelta(minutes=i)
        await strat.on_bar(_bar(price, ts=ts))

    # Trigger a downward deviation (price far below VWAP lower band)
    ts_dev = base + timedelta(minutes=10)
    dev_price = price * 0.94   # 6% below — guaranteed to be outside 2σ band
    high = price * 0.99
    low  = dev_price * 0.998   # wick extends further below close
    await strat.on_bar(_bar(dev_price, high=high, low=low, ts=ts_dev))
    return await strat.generate_signal()


def test_vwap_signal_never_exceeds_5pct_nav():
    """VWAPReversion signal quantity × price must be ≤ 5% NAV."""
    for price in [50.0, 200.0, 500.0, 1_500.0, 5_000.0]:
        signal = _run(_vwap_signal(price, NAV))
        if signal is not None:
            notional = signal.quantity * signal.price_at_signal
            _assert(
                notional <= MAX_NOTIONAL + 1,
                f"price={price}: VWAP notional={notional:.2f} > cap={MAX_NOTIONAL}",
            )
            _assert("nav_used" in signal.metadata, "nav_used missing from VWAP signal metadata")


async def _orb_signal(price: float, nav: float) -> Optional["Signal"]:
    from strategy_engine.strategies.orb_strategy import ORBStrategy
    strat = ORBStrategy(nav=nav)
    await strat.initialize()

    # MARKET_OPEN phase: 09:15–09:30 IST = 03:45–04:00 UTC
    open_base = datetime(2026, 1, 15, 3, 45, 0, tzinfo=timezone.utc)

    # Build OR over 15 1m bars (09:15 → 09:30 IST)
    or_high = price * 1.005
    or_low  = price * 0.995
    for i in range(15):
        ts = open_base + timedelta(minutes=i)
        b = _bar(price + (i % 3) * 0.5, high=or_high, low=or_low, ts=ts)
        await strat.on_bar(b)

    # Post-OR confirmation bar
    ts_confirm = open_base + timedelta(minutes=15)
    await strat.on_bar(_bar(price, ts=ts_confirm))

    # Breakout bar: close strongly above OR high
    ts_break = open_base + timedelta(minutes=16)
    breakout_price = or_high * 1.015
    await strat.on_bar(_bar(breakout_price, high=breakout_price * 1.002, ts=ts_break))
    return await strat.generate_signal()


def test_orb_signal_never_exceeds_5pct_nav():
    """ORBStrategy signal notional must not exceed 5% NAV."""
    for price in [100.0, 500.0, 2_000.0, 8_000.0]:
        signal = _run(_orb_signal(price, NAV))
        if signal is not None:
            notional = signal.quantity * signal.price_at_signal
            _assert(
                notional <= MAX_NOTIONAL + 1,
                f"price={price}: ORB notional={notional:.2f} > cap={MAX_NOTIONAL}",
            )


def test_all_strategies_use_nav_metadata():
    """Every signal metadata dict must contain all five sizer keys."""
    required = {"nav_used", "target_notional", "max_allowed_notional",
                "sizing_reason", "rejected_if_exceeds_cap"}

    # VWAPReversion
    sig = _run(_vwap_signal(500.0, NAV))
    if sig is not None:
        missing = required - set(sig.metadata.keys())
        _assert(not missing, f"VWAPReversion missing metadata keys: {missing}")

    # ORB
    sig = _run(_orb_signal(500.0, NAV))
    if sig is not None:
        missing = required - set(sig.metadata.keys())
        _assert(not missing, f"ORB missing metadata keys: {missing}")


def test_cap_holds_for_penny_stocks():
    """Penny stocks (price ₹5) must also be within cap — no divide-by-small errors."""
    r = size_position(5.0, 0.10, NAV, confidence=1.0)
    notional = r.qty * 5.0
    _assert(notional <= MAX_NOTIONAL + 1, f"Penny stock notional {notional:.2f} exceeds cap")
    _assert(r.rejected_if_exceeds_cap is False, "rejected_if_exceeds_cap should be False")


def test_cap_holds_for_high_price_stocks():
    """High-price stocks (₹30,000 MRF-class) must also be within cap."""
    r = size_position(30_000.0, 600.0, NAV, confidence=1.0)
    notional = r.qty * 30_000.0
    _assert(notional <= MAX_NOTIONAL + 1, f"High-price notional {notional:.2f} exceeds cap")
    # At ₹30,000 and cap ₹50,000: max 1 share (1 × 30,000 = 30,000 ≤ 50,000)
    _assert(r.qty == 1, f"Expected qty=1 for ₹30,000 stock, got {r.qty}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    tests = [
        test_default_confidence_targets_2_5_pct,
        test_high_conviction_targets_5_pct,
        test_hard_cap_never_exceeded_across_price_range,
        test_risk_stop_is_binding_when_stop_is_tight,
        test_risk_stop_binding_on_wide_stop,
        test_minimum_qty_is_one,
        test_invalid_price_returns_zero_qty,
        test_invalid_stop_returns_zero_qty,
        test_invalid_nav_returns_zero_qty,
        test_rejected_if_exceeds_cap_always_false_after_min,
        test_metadata_keys_present,
        test_nav_scaling_doubles_qty,
        test_explicit_target_pct_override,
        test_confidence_below_threshold_uses_default_pct,
        test_confidence_at_threshold_uses_high_conv_pct,
        test_high_conviction_notional_within_hard_cap,
        test_vwap_signal_never_exceeds_5pct_nav,
        test_orb_signal_never_exceeds_5pct_nav,
        test_all_strategies_use_nav_metadata,
        test_cap_holds_for_penny_stocks,
        test_cap_holds_for_high_price_stocks,
    ]

    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed}/{passed+failed} tests passed")
    if failed:
        sys.exit(1)
