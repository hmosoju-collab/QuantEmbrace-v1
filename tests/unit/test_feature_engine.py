"""
Unit tests for FeatureEngine (Phase 5 — PHASE5-001).

Tests cover:
  - All 9 features: correct math + correct None-on-insufficient-history
  - Exact minimum-candle boundary conditions (n == min, n == min-1)
  - VWAP with zero volume (denominator guard)
  - volume_ratio with adv_20d=None, adv_20d=0
  - FeatureSet fields populated correctly
  - Canonical interval conversion
  - Empty candle list raises ValueError
  - ADX: result within 0–100 range
  - RSI: overbought / oversold extremes
  - MACD histogram: macd - signal invariant

Standalone runner: run  ``python test_feature_engine.py``  (no pytest needed).
"""

from __future__ import annotations

import importlib.util as _ilu
import math
import os
import sys
import types
import unittest
import logging

# ── Locate source files ───────────────────────────────────────────────────────

_HERE         = os.path.dirname(os.path.abspath(__file__))   # tests/unit/
_TESTS_DIR    = os.path.dirname(_HERE)                         # tests/
_SERVICES_DIR = os.path.join(os.path.dirname(_TESTS_DIR), "services")


def _load_module(rel_path: str, module_name: str):
    """Load a module from absolute path, returning the module object."""
    abs_path = os.path.join(_SERVICES_DIR, rel_path)
    spec     = _ilu.spec_from_file_location(module_name, abs_path)
    mod      = _ilu.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Module-level placeholders — no side-effects at import time ───────────────

_MODULES_SNAPSHOT: frozenset = frozenset()

FeatureSet: type | None          = None
FeatureEngine: type | None       = None
_compute_vwap: object            = None
_compute_rsi: object             = None
_compute_ema: object             = None
_ema_series: object              = None
_compute_atr: object             = None
_compute_adx: object             = None
_compute_macd: object            = None
_canonical_interval: object      = None


def setUpModule() -> None:
    """Install stubs + load modules under test. Runs AFTER collection."""
    global _MODULES_SNAPSHOT, FeatureSet, FeatureEngine
    global _compute_vwap, _compute_rsi, _compute_ema, _ema_series
    global _compute_atr, _compute_adx, _compute_macd, _canonical_interval

    _MODULES_SNAPSHOT = frozenset(sys.modules.keys())

    # Register parent package namespaces
    for pkg in ("shared", "shared.models", "data_ingestion", "data_ingestion.features"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []
            sys.modules[pkg] = m

    # Load FeatureSet and register on shared.models
    _fs_mod = _load_module(
        os.path.join("shared", "models", "feature_set.py"),
        "shared.models.feature_set",
    )
    FeatureSet = _fs_mod.FeatureSet
    sys.modules["shared.models"].FeatureSet = FeatureSet  # type: ignore

    # Load FeatureEngine
    _fe_mod = _load_module(
        os.path.join("data_ingestion", "features", "feature_engine.py"),
        "data_ingestion.features.feature_engine",
    )
    FeatureEngine       = _fe_mod.FeatureEngine
    _compute_vwap       = _fe_mod._compute_vwap
    _compute_rsi        = _fe_mod._compute_rsi
    _compute_ema        = _fe_mod._compute_ema
    _ema_series         = _fe_mod._ema_series
    _compute_atr        = _fe_mod._compute_atr
    _compute_adx        = _fe_mod._compute_adx
    _compute_macd       = _fe_mod._compute_macd
    _canonical_interval = _fe_mod._canonical_interval


def tearDownModule() -> None:
    """Remove every sys.modules key added during this module's test run."""
    added = frozenset(sys.modules.keys()) - _MODULES_SNAPSHOT
    for key in added:
        sys.modules.pop(key, None)

# ── Helpers ───────────────────────────────────────────────────────────────────

from datetime import datetime, timezone

def _dt(i: int = 0):
    return datetime(2026, 5, 6, 9, i % 60, tzinfo=timezone.utc)


class _Candle:
    """Minimal candle stub."""
    def __init__(self, open, high, low, close, volume, interval="1m", i=0):
        self.open       = float(open)
        self.high       = float(high)
        self.low        = float(low)
        self.close      = float(close)
        self.volume     = int(volume)
        self.interval   = interval
        self.market     = "NSE"
        self.instrument = "TESTSTOCK"
        self.dt         = _dt(i)


def _flat_candles(n: int, price: float = 100.0, volume: int = 1000) -> list[_Candle]:
    """n candles with constant OHLCV."""
    return [_Candle(price, price, price, price, volume, i=i) for i in range(n)]


def _trending_candles(n: int, start: float = 100.0, step: float = 1.0) -> list[_Candle]:
    """n candles with linearly increasing close."""
    return [
        _Candle(
            open=start + i * step,
            high=start + i * step + 0.5,
            low=start + i * step - 0.5,
            close=start + i * step,
            volume=1000,
            i=i,
        )
        for i in range(n)
    ]


def _declining_candles(n: int, start: float = 200.0, step: float = 1.0) -> list[_Candle]:
    return [
        _Candle(
            open=start - i * step,
            high=start - i * step + 0.5,
            low=start - i * step - 0.5,
            close=start - i * step,
            volume=1000,
            i=i,
        )
        for i in range(n)
    ]


try:
    import pytest
except ModuleNotFoundError:
    import builtins as _builtins
    class _Approx:
        def __init__(self, v, abs=None, rel=None):
            self._v = v
            self._abs = abs if abs is not None else 1e-6
        def __eq__(self, o):
            return _builtins.abs(o - self._v) <= self._abs
        def __repr__(self):
            return f"approx({self._v}, abs={self._abs})"
    class pytest:  # type: ignore
        @staticmethod
        def approx(v, abs=None, rel=None):
            return _Approx(v, abs=abs, rel=rel)


# ─────────────────────────────────────────────────────────────────────────────
# TestCanonicalInterval
# ─────────────────────────────────────────────────────────────────────────────

class TestCanonicalInterval(unittest.TestCase):
    def test_kite_minute_to_1m(self):
        self.assertEqual(_canonical_interval("minute"), "1m")

    def test_kite_5minute_to_5m(self):
        self.assertEqual(_canonical_interval("5minute"), "5m")

    def test_kite_15minute_to_15m(self):
        self.assertEqual(_canonical_interval("15minute"), "15m")

    def test_already_canonical(self):
        self.assertEqual(_canonical_interval("1m"), "1m")

    def test_unknown_passthrough(self):
        self.assertEqual(_canonical_interval("custom"), "custom")


# ─────────────────────────────────────────────────────────────────────────────
# TestEmptyCandleList
# ─────────────────────────────────────────────────────────────────────────────

class TestEmptyCandleList(unittest.TestCase):
    def test_raises_value_error(self):
        eng = FeatureEngine()
        with self.assertRaises(ValueError):
            eng.compute([])


# ─────────────────────────────────────────────────────────────────────────────
# TestVWAP
# ─────────────────────────────────────────────────────────────────────────────

class TestVWAP(unittest.TestCase):
    def test_single_candle(self):
        c = [_Candle(100, 110, 90, 105, 1000)]
        tp = (110 + 90 + 105) / 3
        result = _compute_vwap(c)
        self.assertAlmostEqual(result, tp, places=6)

    def test_equal_prices_equal_volumes(self):
        cs = _flat_candles(5, price=100.0)
        # typical price = (100+100+100)/3 = 100
        self.assertAlmostEqual(_compute_vwap(cs), 100.0, places=6)

    def test_zero_volume_returns_none(self):
        cs = [_Candle(100, 110, 90, 105, 0) for _ in range(5)]
        self.assertIsNone(_compute_vwap(cs))

    def test_weighted_average(self):
        c1 = _Candle(100, 100, 100, 100, 100)  # tp=100, vol=100 → 10000
        c2 = _Candle(200, 200, 200, 200, 400)  # tp=200, vol=400 → 80000
        result = _compute_vwap([c1, c2])
        # (100*100 + 200*400) / (100+400) = (10000+80000)/500 = 180.0
        self.assertAlmostEqual(result, 180.0, places=6)


# ─────────────────────────────────────────────────────────────────────────────
# TestEMA
# ─────────────────────────────────────────────────────────────────────────────

class TestEMA(unittest.TestCase):
    def test_ema9_needs_9_candles(self):
        closes = [100.0] * 9
        result = _compute_ema(closes, 9)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 100.0, places=6)

    def test_ema9_insufficient_returns_none(self):
        closes = [100.0] * 8
        self.assertIsNone(_compute_ema(closes, 9))

    def test_ema21_needs_21_candles(self):
        closes = [100.0] * 21
        self.assertIsNotNone(_compute_ema(closes, 21))

    def test_ema21_insufficient_returns_none(self):
        closes = [100.0] * 20
        self.assertIsNone(_compute_ema(closes, 21))

    def test_ema_seed_equals_sma(self):
        closes = [1.0, 2.0, 3.0, 4.0, 5.0]  # SMA(5) = 3.0
        series = _ema_series(closes, 5)
        self.assertAlmostEqual(series[4], 3.0, places=6)

    def test_ema_single_new_value_after_seed(self):
        # EMA(3) on [1,2,3,10]: seed=SMA([1,2,3])=2, alpha=0.5
        # EMA step = 10 * 0.5 + 2 * 0.5 = 6.0
        closes = [1.0, 2.0, 3.0, 10.0]
        series = _ema_series(closes, 3)
        self.assertAlmostEqual(series[3], 6.0, places=6)

    def test_trending_up_ema_below_price(self):
        closes = _trending_candles(30, start=100.0, step=1.0)
        closes_vals = [c.close for c in closes]
        ema = _compute_ema(closes_vals, 9)
        # EMA lags; last close = 129, ema should be below 129
        self.assertLess(ema, 129.0)
        self.assertGreater(ema, 100.0)


# ─────────────────────────────────────────────────────────────────────────────
# TestRSI
# ─────────────────────────────────────────────────────────────────────────────

class TestRSI(unittest.TestCase):
    def test_needs_15_candles(self):
        closes = [100.0] * 15
        self.assertIsNotNone(_compute_rsi(closes))

    def test_14_candles_returns_none(self):
        closes = [100.0] * 14
        self.assertIsNone(_compute_rsi(closes))

    def test_flat_prices_rsi_undefined_but_not_crash(self):
        # All gains=0, all losses=0 → RS=undefined.  avg_loss==0 → RSI=100
        closes = [100.0] * 20
        result = _compute_rsi(closes)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 100.0, places=6)

    def test_all_up_days_rsi_100(self):
        closes = [100.0 + i for i in range(20)]
        result = _compute_rsi(closes)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 100.0, places=6)

    def test_all_down_days_rsi_near_0(self):
        closes = [200.0 - i for i in range(20)]
        result = _compute_rsi(closes)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 0.0, places=6)

    def test_rsi_in_range(self):
        import random
        random.seed(42)
        closes = [100.0 + random.gauss(0, 2) for _ in range(50)]
        result = _compute_rsi(closes)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result, 0.0)
        self.assertLessEqual(result, 100.0)

    def test_exact_boundary_15_candles(self):
        closes = [100.0 + i * 0.5 for i in range(15)]
        result = _compute_rsi(closes)
        self.assertIsNotNone(result)


# ─────────────────────────────────────────────────────────────────────────────
# TestATR
# ─────────────────────────────────────────────────────────────────────────────

class TestATR(unittest.TestCase):
    def _h_l_c(self, candles):
        return (
            [c.high for c in candles],
            [c.low for c in candles],
            [c.close for c in candles],
        )

    def test_needs_15_candles(self):
        cs = _flat_candles(15, price=100.0)
        h, l, c = self._h_l_c(cs)
        result = _compute_atr(h, l, c)
        self.assertIsNotNone(result)

    def test_14_candles_returns_none(self):
        cs = _flat_candles(14, price=100.0)
        h, l, c = self._h_l_c(cs)
        self.assertIsNone(_compute_atr(h, l, c))

    def test_flat_candles_atr_zero(self):
        cs = _flat_candles(20, price=100.0)
        h, l, c = self._h_l_c(cs)
        result = _compute_atr(h, l, c)
        self.assertAlmostEqual(result, 0.0, places=6)

    def test_atr_positive_for_volatile_candles(self):
        cs = [_Candle(100, 110, 90, 100, 1000, i=i) for i in range(20)]
        h, l, c = self._h_l_c(cs)
        result = _compute_atr(h, l, c)
        self.assertIsNotNone(result)
        self.assertGreater(result, 0.0)

    def test_atr_equals_range_for_consistent_candles(self):
        # All candles: high=110, low=90, close=100 — consistent 20 range
        cs = [_Candle(100, 110, 90, 100, 1000, i=i) for i in range(20)]
        h = [c.high for c in cs]
        l = [c.low  for c in cs]
        c = [c.close for c in cs]
        result = _compute_atr(h, l, c)
        # After seed: ATR = SMA(14 TRs) = 20; subsequent smoothing stays at 20
        self.assertAlmostEqual(result, 20.0, places=4)


# ─────────────────────────────────────────────────────────────────────────────
# TestADX
# ─────────────────────────────────────────────────────────────────────────────

class TestADX(unittest.TestCase):
    def _h_l_c(self, candles):
        return (
            [c.high for c in candles],
            [c.low for c in candles],
            [c.close for c in candles],
        )

    def test_needs_28_candles(self):
        cs = _trending_candles(28, start=100.0, step=1.0)
        h, l, c = self._h_l_c(cs)
        result = _compute_adx(h, l, c)
        self.assertIsNotNone(result)

    def test_27_candles_returns_none(self):
        cs = _trending_candles(27, start=100.0, step=1.0)
        h, l, c = self._h_l_c(cs)
        self.assertIsNone(_compute_adx(h, l, c))

    def test_adx_in_range_0_100(self):
        import random
        random.seed(42)
        cs = [
            _Candle(
                open=100 + random.gauss(0, 2),
                high=100 + abs(random.gauss(0, 2)) + 1,
                low=100 - abs(random.gauss(0, 2)),
                close=100 + random.gauss(0, 2),
                volume=1000,
                i=i,
            )
            for i in range(40)
        ]
        h, l, c = self._h_l_c(cs)
        result = _compute_adx(h, l, c)
        if result is not None:
            self.assertGreaterEqual(result, 0.0)
            self.assertLessEqual(result, 100.0)

    def test_flat_candles_adx_low(self):
        # Non-directional market → low ADX
        cs = _flat_candles(40, price=100.0)
        h, l, c = self._h_l_c(cs)
        result = _compute_adx(h, l, c)
        if result is not None:
            self.assertLess(result, 10.0)


# ─────────────────────────────────────────────────────────────────────────────
# TestMACD
# ─────────────────────────────────────────────────────────────────────────────

class TestMACD(unittest.TestCase):
    def test_needs_35_candles(self):
        closes = [100.0 + i * 0.1 for i in range(35)]
        ml, ms, mh = _compute_macd(closes)
        self.assertIsNotNone(ml)
        self.assertIsNotNone(ms)
        self.assertIsNotNone(mh)

    def test_34_candles_returns_none_tuple(self):
        closes = [100.0] * 34
        ml, ms, mh = _compute_macd(closes)
        self.assertIsNone(ml)
        self.assertIsNone(ms)
        self.assertIsNone(mh)

    def test_histogram_equals_macd_minus_signal(self):
        closes = [100.0 + i * 0.5 for i in range(40)]
        ml, ms, mh = _compute_macd(closes)
        self.assertIsNotNone(mh)
        self.assertAlmostEqual(mh, ml - ms, places=10)

    def test_strong_uptrend_macd_positive(self):
        # Strong uptrend: fast EMA > slow EMA → MACD > 0
        closes = [100.0 + i * 2.0 for i in range(40)]
        ml, ms, mh = _compute_macd(closes)
        self.assertIsNotNone(ml)
        self.assertGreater(ml, 0.0)

    def test_strong_downtrend_macd_negative(self):
        closes = [200.0 - i * 2.0 for i in range(40)]
        ml, ms, mh = _compute_macd(closes)
        self.assertIsNotNone(ml)
        self.assertLess(ml, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# TestVolumeRatio
# ─────────────────────────────────────────────────────────────────────────────

class TestVolumeRatio(unittest.TestCase):
    def _compute(self, candles, adv):
        eng = FeatureEngine()
        fs = eng.compute(candles, adv_20d=adv)
        return fs.volume_ratio

    def test_none_when_adv_none(self):
        cs = _flat_candles(5, volume=1000)
        self.assertIsNone(self._compute(cs, None))

    def test_none_when_adv_zero(self):
        cs = _flat_candles(5, volume=1000)
        self.assertIsNone(self._compute(cs, 0.0))

    def test_correct_ratio(self):
        cs = _flat_candles(5, volume=2000)
        # Last candle volume = 2000, adv = 1000 → ratio = 2.0
        result = self._compute(cs, 1000.0)
        self.assertAlmostEqual(result, 2.0, places=6)

    def test_fractional_ratio(self):
        cs = _flat_candles(5, volume=500)
        result = self._compute(cs, 1000.0)
        self.assertAlmostEqual(result, 0.5, places=6)


# ─────────────────────────────────────────────────────────────────────────────
# TestFeatureEngineCompute — integration / FeatureSet structure
# ─────────────────────────────────────────────────────────────────────────────

class TestFeatureEngineCompute(unittest.TestCase):
    def setUp(self):
        self.engine = FeatureEngine()

    def test_single_candle_returns_feature_set(self):
        cs = [_Candle(100, 110, 90, 105, 1000)]
        fs = self.engine.compute(cs)
        self.assertIsInstance(fs, FeatureSet)
        self.assertEqual(fs.candle_count, 1)
        self.assertEqual(fs.symbol, "TESTSTOCK")
        self.assertEqual(fs.market, "NSE")
        self.assertEqual(fs.schema_version, 1)

    def test_single_candle_only_vwap_populated(self):
        cs = [_Candle(100, 110, 90, 105, 1000)]
        fs = self.engine.compute(cs)
        self.assertIsNotNone(fs.vwap)
        self.assertIsNone(fs.rsi_14)
        self.assertIsNone(fs.ema_9)
        self.assertIsNone(fs.ema_21)
        self.assertIsNone(fs.atr_14)
        self.assertIsNone(fs.adx_14)
        self.assertIsNone(fs.macd)
        self.assertIsNone(fs.macd_signal)
        self.assertIsNone(fs.macd_hist)

    def test_35_candles_all_features_except_adv(self):
        cs = _trending_candles(35)
        fs = self.engine.compute(cs)
        self.assertIsNotNone(fs.vwap)
        self.assertIsNotNone(fs.ema_9)
        self.assertIsNotNone(fs.ema_21)
        self.assertIsNotNone(fs.rsi_14)
        self.assertIsNotNone(fs.atr_14)
        self.assertIsNotNone(fs.adx_14)
        self.assertIsNotNone(fs.macd)
        self.assertIsNotNone(fs.macd_signal)
        self.assertIsNotNone(fs.macd_hist)
        self.assertIsNone(fs.volume_ratio)  # no adv_20d passed

    def test_35_candles_with_adv_all_populated(self):
        cs = _trending_candles(35)
        fs = self.engine.compute(cs, adv_20d=10_000.0)
        self.assertIsNotNone(fs.volume_ratio)

    def test_candle_time_is_last_candle_dt(self):
        cs = _trending_candles(5)
        fs = self.engine.compute(cs)
        self.assertEqual(fs.candle_time, cs[-1].dt)

    def test_interval_converted_to_canonical(self):
        cs = [_Candle(100, 110, 90, 105, 1000, interval="minute")]
        fs = self.engine.compute(cs)
        self.assertEqual(fs.interval, "1m")

    def test_computed_at_is_utc_aware(self):
        cs = _flat_candles(1)
        fs = self.engine.compute(cs)
        self.assertIsNotNone(fs.computed_at.tzinfo)

    def test_candle_count_matches_input(self):
        cs = _trending_candles(20)
        fs = self.engine.compute(cs)
        self.assertEqual(fs.candle_count, 20)

    def test_boundary_n_equals_ema9_min(self):
        cs = _flat_candles(9)
        fs = self.engine.compute(cs)
        self.assertIsNotNone(fs.ema_9)
        self.assertIsNone(fs.ema_21)

    def test_boundary_n_equals_ema21_min(self):
        cs = _flat_candles(21)
        fs = self.engine.compute(cs)
        self.assertIsNotNone(fs.ema_21)

    def test_boundary_n_equals_rsi_min(self):
        cs = _flat_candles(15)
        fs = self.engine.compute(cs)
        self.assertIsNotNone(fs.rsi_14)

    def test_boundary_n_14_rsi_none(self):
        cs = _flat_candles(14)
        fs = self.engine.compute(cs)
        self.assertIsNone(fs.rsi_14)

    def test_boundary_n_equals_adx_min(self):
        cs = _trending_candles(28)
        fs = self.engine.compute(cs)
        self.assertIsNotNone(fs.adx_14)

    def test_boundary_n_27_adx_none(self):
        cs = _trending_candles(27)
        fs = self.engine.compute(cs)
        self.assertIsNone(fs.adx_14)

    def test_boundary_n_equals_macd_min(self):
        cs = _trending_candles(35)
        fs = self.engine.compute(cs)
        self.assertIsNotNone(fs.macd)

    def test_boundary_n_34_macd_none(self):
        cs = _trending_candles(34)
        fs = self.engine.compute(cs)
        self.assertIsNone(fs.macd)

    def test_macd_hist_invariant(self):
        cs = _trending_candles(40)
        fs = self.engine.compute(cs)
        if fs.macd is not None and fs.macd_signal is not None:
            self.assertAlmostEqual(fs.macd_hist, fs.macd - fs.macd_signal, places=10)

    def test_rsi_bounds(self):
        cs = _trending_candles(40)
        fs = self.engine.compute(cs)
        if fs.rsi_14 is not None:
            self.assertGreaterEqual(fs.rsi_14, 0.0)
            self.assertLessEqual(fs.rsi_14, 100.0)

    def test_adx_bounds(self):
        cs = _trending_candles(40)
        fs = self.engine.compute(cs)
        if fs.adx_14 is not None:
            self.assertGreaterEqual(fs.adx_14, 0.0)
            self.assertLessEqual(fs.adx_14, 100.0)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner
# ─────────────────────────────────────────────────────────────────────────────

def _run_tests() -> int:
    setUpModule()
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    tearDownModule()
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
