"""
Unit tests for FeatureReader (Phase 5 — PHASE5-003).

Tests cover:
  - Happy path: fresh item → returns FeatureSet
  - Item not found → returns None
  - DynamoDB error → returns None (graceful degradation)
  - Staleness: threshold math for 1m / 5m / 15m
  - Stale item returns None; fresh item returns FeatureSet
  - schema_version mismatch → returns None
  - Missing computed_at → returns None
  - Malformed item (unparseable datetime) → returns None
  - All optional fields correctly None when absent from item
  - All optional fields populated when present in item
  - _staleness_threshold_seconds: formula correctness
  - _parse_interval_minutes: known + unknown intervals

Standalone runner: ``python test_feature_reader.py``
"""

from __future__ import annotations

import asyncio
import importlib.util as _ilu
import os
import sys
import types
import unittest
import logging
from datetime import datetime, timedelta, timezone
from typing import Any


# ── Module loading ────────────────────────────────────────────────────────────

_HERE         = os.path.dirname(os.path.abspath(__file__))   # tests/unit/
_TESTS_DIR    = os.path.dirname(_HERE)                         # tests/
_SERVICES_DIR = os.path.join(os.path.dirname(_TESTS_DIR), "services")


def _load_module(rel: str, name: str):
    path = os.path.join(_SERVICES_DIR, rel)
    spec = _ilu.spec_from_file_location(name, path)
    mod  = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Stub class — pure definition, no side-effects at import time ──────────────

class _StructuredLogger:
    def __init__(self, name): self._log = logging.getLogger(name)
    def info(self, msg, *a, **kw): self._log.info(msg)
    def warning(self, msg, *a, **kw): self._log.warning(msg)
    def debug(self, msg, *a, **kw): self._log.debug(msg)
    def error(self, msg, *a, **kw): self._log.error(msg)
    def exception(self, msg, *a, **kw): self._log.exception(msg)
    def critical(self, msg, *a, **kw): self._log.critical(msg)


# ── Module-level placeholders — no side-effects at import time ───────────────

_MODULES_SNAPSHOT: frozenset        = frozenset()

FeatureSet: type | None              = None
FeatureReader: type | None           = None
_parse_item: object                  = None
_staleness_threshold_seconds: object = None
_parse_interval_minutes: object      = None


def setUpModule() -> None:
    """Install stubs + load modules under test. Runs AFTER collection."""
    global _MODULES_SNAPSHOT, FeatureSet, FeatureReader
    global _parse_item, _staleness_threshold_seconds, _parse_interval_minutes

    _MODULES_SNAPSHOT = frozenset(sys.modules.keys())

    # Register parent package namespaces
    for pkg in ("shared", "shared.models", "shared.logging", "shared.features"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []
            sys.modules[pkg] = m

    # Install logger stub
    _logging_mod = types.ModuleType("shared.logging.logger")
    _logging_mod.get_logger = lambda name, **_: _StructuredLogger(name)  # type: ignore
    _logging_mod.set_correlation_id = lambda *_, **__: None  # type: ignore
    sys.modules["shared.logging.logger"] = _logging_mod
    sys.modules["shared.logging"].logger = _logging_mod  # type: ignore

    # Load FeatureSet
    _fs_mod = _load_module(
        os.path.join("shared", "models", "feature_set.py"),
        "shared.models.feature_set",
    )
    FeatureSet = _fs_mod.FeatureSet
    sys.modules["shared.models"].FeatureSet = FeatureSet  # type: ignore

    # Load FeatureReader
    _fr_mod = _load_module(
        os.path.join("shared", "features", "feature_reader.py"),
        "shared.features.feature_reader",
    )
    FeatureReader                = _fr_mod.FeatureReader
    _parse_item                  = _fr_mod._parse_item
    _staleness_threshold_seconds = _fr_mod._staleness_threshold_seconds
    _parse_interval_minutes      = _fr_mod._parse_interval_minutes


def tearDownModule() -> None:
    """Remove every sys.modules key added during this module's test run."""
    added = frozenset(sys.modules.keys()) - _MODULES_SNAPSHOT
    for key in added:
        sys.modules.pop(key, None)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_utc():
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _fresh_computed_at(interval: str = "1m", margin_seconds: int = 30) -> datetime:
    """Return a computed_at that is within the staleness threshold."""
    threshold = _staleness_threshold_seconds(interval)
    return _now_utc() - timedelta(seconds=threshold - margin_seconds)


def _stale_computed_at(interval: str = "1m", extra_seconds: int = 30) -> datetime:
    """Return a computed_at that exceeds the staleness threshold."""
    threshold = _staleness_threshold_seconds(interval)
    return _now_utc() - timedelta(seconds=threshold + extra_seconds)


def _full_item(computed_at: datetime, interval: str = "1m") -> dict[str, Any]:
    candle_time = computed_at - timedelta(minutes=1)
    return {
        "PK": {"S": f"FEATURE#NSE#RELIANCE#{interval}"},
        "SK": {"S": "LATEST"},
        "symbol":         {"S": "RELIANCE"},
        "market":         {"S": "NSE"},
        "interval":       {"S": interval},
        "candle_time":    {"S": _iso(candle_time)},
        "candle_count":   {"N": "40"},
        "computed_at":    {"S": _iso(computed_at)},
        "schema_version": {"N": "1"},
        "rsi_14":         {"N": "55.3"},
        "ema_9":          {"N": "100.5"},
        "ema_21":         {"N": "98.2"},
        "vwap":           {"N": "99.8"},
        "atr_14":         {"N": "2.5"},
        "adx_14":         {"N": "28.7"},
        "macd":           {"N": "0.45"},
        "macd_signal":    {"N": "0.30"},
        "macd_hist":      {"N": "0.15"},
        "volume_ratio":   {"N": "1.2"},
        "ttl":            {"N": "9999999999"},
    }


def _sparse_item(computed_at: datetime, interval: str = "1m") -> dict[str, Any]:
    """Only vwap populated; all other optional features absent."""
    candle_time = computed_at - timedelta(minutes=1)
    return {
        "PK": {"S": f"FEATURE#NSE#INFY#{interval}"},
        "SK": {"S": "LATEST"},
        "symbol":         {"S": "INFY"},
        "market":         {"S": "NSE"},
        "interval":       {"S": interval},
        "candle_time":    {"S": _iso(candle_time)},
        "candle_count":   {"N": "1"},
        "computed_at":    {"S": _iso(computed_at)},
        "schema_version": {"N": "1"},
        "vwap":           {"N": "99.0"},
    }


class _MockDynamo:
    """Returns a pre-configured item or simulates an error."""
    def __init__(self, item=None, raise_error=False):
        self._item       = item
        self._raise_error = raise_error

    def get_item(self, **_) -> dict:
        if self._raise_error:
            raise RuntimeError("DynamoDB connection error")
        if self._item is None:
            return {}
        return {"Item": self._item}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ─────────────────────────────────────────────────────────────────────────────
# TestStalenessThreshold
# ─────────────────────────────────────────────────────────────────────────────

class TestStalenessThreshold(unittest.TestCase):
    def test_1m_threshold_is_300s(self):
        # max(300, 2*1*60+60) = max(300, 180) = 300
        self.assertEqual(_staleness_threshold_seconds("1m"), 300.0)

    def test_5m_threshold_is_660s(self):
        # max(300, 2*5*60+60) = max(300, 660) = 660
        self.assertEqual(_staleness_threshold_seconds("5m"), 660.0)

    def test_15m_threshold_is_1860s(self):
        # max(300, 2*15*60+60) = max(300, 1860) = 1860
        self.assertEqual(_staleness_threshold_seconds("15m"), 1860.0)

    def test_unknown_interval_uses_1m_as_fallback(self):
        # Unknown → 1 minute → max(300, 2*1*60+60) = 300
        self.assertEqual(_staleness_threshold_seconds("unknown"), 300.0)

    def test_kite_minute_string(self):
        self.assertEqual(_staleness_threshold_seconds("minute"), 300.0)


class TestParseIntervalMinutes(unittest.TestCase):
    def test_1m(self):
        self.assertEqual(_parse_interval_minutes("1m"), 1)

    def test_5m(self):
        self.assertEqual(_parse_interval_minutes("5m"), 5)

    def test_15m(self):
        self.assertEqual(_parse_interval_minutes("15m"), 15)

    def test_60m(self):
        self.assertEqual(_parse_interval_minutes("60m"), 60)

    def test_unknown_defaults_to_1(self):
        self.assertEqual(_parse_interval_minutes("xyz"), 1)


# ─────────────────────────────────────────────────────────────────────────────
# TestParseItem
# ─────────────────────────────────────────────────────────────────────────────

class TestParseItem(unittest.TestCase):
    def test_fresh_full_item_returns_feature_set(self):
        item = _full_item(_fresh_computed_at("1m"))
        result = _parse_item(item, "1m")
        self.assertIsNotNone(result)
        self.assertIsInstance(result, FeatureSet)

    def test_stale_item_returns_none(self):
        item = _full_item(_stale_computed_at("1m"))
        result = _parse_item(item, "1m")
        self.assertIsNone(result)

    def test_schema_version_1_accepted(self):
        item = _full_item(_fresh_computed_at())
        item["schema_version"] = {"N": "1"}
        self.assertIsNotNone(_parse_item(item, "1m"))

    def test_schema_version_2_rejected(self):
        item = _full_item(_fresh_computed_at())
        item["schema_version"] = {"N": "2"}
        self.assertIsNone(_parse_item(item, "1m"))

    def test_schema_version_0_rejected(self):
        item = _full_item(_fresh_computed_at())
        item["schema_version"] = {"N": "0"}
        self.assertIsNone(_parse_item(item, "1m"))

    def test_missing_computed_at_returns_none(self):
        item = _full_item(_fresh_computed_at())
        del item["computed_at"]
        self.assertIsNone(_parse_item(item, "1m"))

    def test_unparseable_computed_at_returns_none(self):
        item = _full_item(_fresh_computed_at())
        item["computed_at"] = {"S": "not-a-date"}
        self.assertIsNone(_parse_item(item, "1m"))

    def test_full_item_all_features_populated(self):
        item = _full_item(_fresh_computed_at())
        fs = _parse_item(item, "1m")
        self.assertIsNotNone(fs)
        self.assertAlmostEqual(fs.rsi_14, 55.3, places=3)
        self.assertAlmostEqual(fs.ema_9, 100.5, places=3)
        self.assertAlmostEqual(fs.ema_21, 98.2, places=3)
        self.assertAlmostEqual(fs.vwap, 99.8, places=3)
        self.assertAlmostEqual(fs.atr_14, 2.5, places=3)
        self.assertAlmostEqual(fs.adx_14, 28.7, places=3)
        self.assertAlmostEqual(fs.macd, 0.45, places=3)
        self.assertAlmostEqual(fs.macd_signal, 0.30, places=3)
        self.assertAlmostEqual(fs.macd_hist, 0.15, places=3)
        self.assertAlmostEqual(fs.volume_ratio, 1.2, places=3)

    def test_sparse_item_absent_features_are_none(self):
        item = _sparse_item(_fresh_computed_at())
        fs = _parse_item(item, "1m")
        self.assertIsNotNone(fs)
        self.assertIsNone(fs.rsi_14)
        self.assertIsNone(fs.ema_9)
        self.assertIsNone(fs.ema_21)
        self.assertIsNone(fs.atr_14)
        self.assertIsNone(fs.adx_14)
        self.assertIsNone(fs.macd)
        self.assertIsNone(fs.macd_signal)
        self.assertIsNone(fs.macd_hist)
        self.assertIsNone(fs.volume_ratio)

    def test_sparse_item_vwap_populated(self):
        item = _sparse_item(_fresh_computed_at())
        fs = _parse_item(item, "1m")
        self.assertIsNotNone(fs)
        self.assertAlmostEqual(fs.vwap, 99.0, places=3)

    def test_symbol_and_market_populated(self):
        item = _full_item(_fresh_computed_at())
        fs = _parse_item(item, "1m")
        self.assertEqual(fs.symbol, "RELIANCE")
        self.assertEqual(fs.market, "NSE")

    def test_candle_count_populated(self):
        item = _full_item(_fresh_computed_at())
        fs = _parse_item(item, "1m")
        self.assertEqual(fs.candle_count, 40)

    def test_schema_version_in_feature_set(self):
        item = _full_item(_fresh_computed_at())
        fs = _parse_item(item, "1m")
        self.assertEqual(fs.schema_version, 1)


# ─────────────────────────────────────────────────────────────────────────────
# TestFeatureReader — async get_latest
# ─────────────────────────────────────────────────────────────────────────────

class TestFeatureReaderGetLatest(unittest.TestCase):
    def test_happy_path_returns_feature_set(self):
        item   = _full_item(_fresh_computed_at("1m"))
        dynamo = _MockDynamo(item=item)
        reader = FeatureReader(dynamo, "test-features")
        result = _run(reader.get_latest("NSE", "RELIANCE", "1m"))
        self.assertIsNotNone(result)
        self.assertIsInstance(result, FeatureSet)

    def test_item_not_found_returns_none(self):
        dynamo = _MockDynamo(item=None)
        reader = FeatureReader(dynamo, "test-features")
        result = _run(reader.get_latest("NSE", "RELIANCE", "1m"))
        self.assertIsNone(result)

    def test_dynamo_error_returns_none(self):
        dynamo = _MockDynamo(raise_error=True)
        reader = FeatureReader(dynamo, "test-features")
        result = _run(reader.get_latest("NSE", "RELIANCE", "1m"))
        self.assertIsNone(result)

    def test_stale_1m_item_returns_none(self):
        item   = _full_item(_stale_computed_at("1m"), interval="1m")
        dynamo = _MockDynamo(item=item)
        reader = FeatureReader(dynamo, "test-features")
        result = _run(reader.get_latest("NSE", "RELIANCE", "1m"))
        self.assertIsNone(result)

    def test_fresh_5m_item_returns_feature_set(self):
        item   = _full_item(_fresh_computed_at("5m"), interval="5m")
        dynamo = _MockDynamo(item=item)
        reader = FeatureReader(dynamo, "test-features")
        result = _run(reader.get_latest("NSE", "RELIANCE", "5m"))
        self.assertIsNotNone(result)

    def test_stale_5m_item_returns_none(self):
        item   = _full_item(_stale_computed_at("5m"), interval="5m")
        dynamo = _MockDynamo(item=item)
        reader = FeatureReader(dynamo, "test-features")
        result = _run(reader.get_latest("NSE", "RELIANCE", "5m"))
        self.assertIsNone(result)

    def test_fresh_15m_item_returns_feature_set(self):
        item   = _full_item(_fresh_computed_at("15m"), interval="15m")
        dynamo = _MockDynamo(item=item)
        reader = FeatureReader(dynamo, "test-features")
        result = _run(reader.get_latest("NSE", "RELIANCE", "15m"))
        self.assertIsNotNone(result)

    def test_stale_15m_item_returns_none(self):
        item   = _full_item(_stale_computed_at("15m"), interval="15m")
        dynamo = _MockDynamo(item=item)
        reader = FeatureReader(dynamo, "test-features")
        result = _run(reader.get_latest("NSE", "RELIANCE", "15m"))
        self.assertIsNone(result)

    def test_wrong_schema_version_returns_none(self):
        item = _full_item(_fresh_computed_at("1m"))
        item["schema_version"] = {"N": "99"}
        dynamo = _MockDynamo(item=item)
        reader = FeatureReader(dynamo, "test-features")
        result = _run(reader.get_latest("NSE", "RELIANCE", "1m"))
        self.assertIsNone(result)


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
