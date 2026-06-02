"""
Unit tests for FeatureWriter (Phase 5 — PHASE5-002).

Tests cover:
  - DynamoDB item schema: PK, SK, all feature attributes
  - Dual-write: both LATEST and CANDLE#{ts} rows written per call
  - None features omitted from item (no "N": "None" noise)
  - TTL: LATEST = 24h, CANDLE = 7 days
  - Write failure: non-fatal — never raises, logs warning
  - _build_base_item: correct attribute types (S vs N)
  - Schema version attribute written as N
  - computed_at written as S (ISO string)

Standalone runner: ``python test_feature_writer.py``
"""

from __future__ import annotations

import asyncio
import importlib.util as _ilu
import os
import sys
import time
import types
import unittest
import logging
from datetime import datetime, timezone
from typing import Any


# ── Locate and load modules ───────────────────────────────────────────────────

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

_MODULES_SNAPSHOT: frozenset = frozenset()

FeatureSet: type | None       = None
FeatureWriter: type | None    = None
_build_base_item: object      = None
_LATEST_TTL: int | None       = None
_CANDLE_TTL: int | None       = None


def setUpModule() -> None:
    """Install stubs + load modules under test. Runs AFTER collection."""
    global _MODULES_SNAPSHOT, FeatureSet, FeatureWriter
    global _build_base_item, _LATEST_TTL, _CANDLE_TTL

    _MODULES_SNAPSHOT = frozenset(sys.modules.keys())

    # Register parent package namespaces
    for pkg in ("shared", "shared.models", "shared.logging", "data_ingestion",
                "data_ingestion.features"):
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

    # Load FeatureWriter
    _fw_mod = _load_module(
        os.path.join("data_ingestion", "features", "feature_writer.py"),
        "data_ingestion.features.feature_writer",
    )
    FeatureWriter    = _fw_mod.FeatureWriter
    _build_base_item = _fw_mod._build_base_item
    _LATEST_TTL      = _fw_mod._LATEST_TTL_SECONDS
    _CANDLE_TTL      = _fw_mod._CANDLE_TTL_SECONDS


def tearDownModule() -> None:
    """Remove every sys.modules key added during this module's test run."""
    added = frozenset(sys.modules.keys()) - _MODULES_SNAPSHOT
    for key in added:
        sys.modules.pop(key, None)


# ── Helpers ───────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 6, 9, 15, 0, tzinfo=timezone.utc)
_CANDLE_TIME = datetime(2026, 5, 6, 9, 14, 0, tzinfo=timezone.utc)


def _full_feature_set(**overrides) -> FeatureSet:
    defaults = dict(
        symbol="RELIANCE",
        market="NSE",
        interval="1m",
        candle_time=_CANDLE_TIME,
        candle_count=40,
        computed_at=_NOW,
        rsi_14=55.3,
        ema_9=100.5,
        ema_21=98.2,
        vwap=99.8,
        atr_14=2.5,
        adx_14=28.7,
        macd=0.45,
        macd_signal=0.30,
        macd_hist=0.15,
        volume_ratio=1.2,
    )
    defaults.update(overrides)
    return FeatureSet(**defaults)


def _sparse_feature_set() -> FeatureSet:
    """All optional features None (only 1 candle available)."""
    return FeatureSet(
        symbol="INFY",
        market="NSE",
        interval="1m",
        candle_time=_CANDLE_TIME,
        candle_count=1,
        computed_at=_NOW,
        rsi_14=None,
        ema_9=None,
        ema_21=None,
        vwap=99.0,
        atr_14=None,
        adx_14=None,
        macd=None,
        macd_signal=None,
        macd_hist=None,
        volume_ratio=None,
    )


class _MockDynamo:
    """Captures put_item calls."""
    def __init__(self, raise_on_call: int = -1):
        self.calls: list[dict] = []
        self._raise_on = raise_on_call
        self._call_count = 0

    def put_item(self, TableName: str, Item: dict) -> dict:
        self._call_count += 1
        if self._call_count == self._raise_on:
            raise RuntimeError("DynamoDB error")
        self.calls.append({"TableName": TableName, "Item": Item})
        return {}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ─────────────────────────────────────────────────────────────────────────────
# TestBuildBaseItem
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildBaseItem(unittest.TestCase):
    def setUp(self):
        self.fs = _full_feature_set()

    def test_symbol_is_S_type(self):
        item = _build_base_item(self.fs)
        self.assertEqual(item["symbol"]["S"], "RELIANCE")

    def test_market_is_S_type(self):
        item = _build_base_item(self.fs)
        self.assertEqual(item["market"]["S"], "NSE")

    def test_interval_is_S_type(self):
        item = _build_base_item(self.fs)
        self.assertEqual(item["interval"]["S"], "1m")

    def test_candle_time_is_S_type(self):
        item = _build_base_item(self.fs)
        self.assertIn("S", item["candle_time"])
        self.assertIn("2026-05-06", item["candle_time"]["S"])

    def test_candle_count_is_N_type(self):
        item = _build_base_item(self.fs)
        self.assertEqual(item["candle_count"]["N"], "40")

    def test_computed_at_is_S_type(self):
        item = _build_base_item(self.fs)
        self.assertIn("S", item["computed_at"])
        self.assertIn("2026-05-06", item["computed_at"]["S"])

    def test_schema_version_is_N_type(self):
        item = _build_base_item(self.fs)
        self.assertEqual(item["schema_version"]["N"], "1")

    def test_rsi_14_is_N_type(self):
        item = _build_base_item(self.fs)
        self.assertIn("N", item["rsi_14"])
        self.assertAlmostEqual(float(item["rsi_14"]["N"]), 55.3, places=3)

    def test_none_features_omitted(self):
        fs = _sparse_feature_set()
        item = _build_base_item(fs)
        for key in ("rsi_14", "ema_9", "ema_21", "atr_14", "adx_14",
                    "macd", "macd_signal", "macd_hist", "volume_ratio"):
            self.assertNotIn(key, item, f"{key} should not be in item when None")

    def test_vwap_written_when_not_none(self):
        fs = _sparse_feature_set()
        item = _build_base_item(fs)
        self.assertIn("vwap", item)
        self.assertAlmostEqual(float(item["vwap"]["N"]), 99.0, places=6)

    def test_all_features_present_when_full(self):
        item = _build_base_item(self.fs)
        for key in ("rsi_14", "ema_9", "ema_21", "vwap", "atr_14", "adx_14",
                    "macd", "macd_signal", "macd_hist", "volume_ratio"):
            self.assertIn(key, item, f"{key} missing from item")


# ─────────────────────────────────────────────────────────────────────────────
# TestDualWrite
# ─────────────────────────────────────────────────────────────────────────────

class TestDualWrite(unittest.TestCase):
    def test_two_put_item_calls_per_write(self):
        dynamo = _MockDynamo()
        writer = FeatureWriter(dynamo, "test-features")
        _run(writer.write(_full_feature_set()))
        self.assertEqual(len(dynamo.calls), 2)

    def test_latest_sk_written(self):
        dynamo = _MockDynamo()
        writer = FeatureWriter(dynamo, "test-features")
        _run(writer.write(_full_feature_set()))
        sks = [c["Item"]["SK"]["S"] for c in dynamo.calls]
        self.assertIn("LATEST", sks)

    def test_candle_sk_written(self):
        dynamo = _MockDynamo()
        writer = FeatureWriter(dynamo, "test-features")
        _run(writer.write(_full_feature_set()))
        sks = [c["Item"]["SK"]["S"] for c in dynamo.calls]
        candle_sks = [s for s in sks if s.startswith("CANDLE#")]
        self.assertEqual(len(candle_sks), 1)

    def test_candle_sk_contains_candle_time(self):
        dynamo = _MockDynamo()
        writer = FeatureWriter(dynamo, "test-features")
        _run(writer.write(_full_feature_set()))
        sks = [c["Item"]["SK"]["S"] for c in dynamo.calls]
        candle_sk = next(s for s in sks if s.startswith("CANDLE#"))
        self.assertIn("2026-05-06T09:14:00", candle_sk)

    def test_pk_format(self):
        dynamo = _MockDynamo()
        writer = FeatureWriter(dynamo, "test-features")
        _run(writer.write(_full_feature_set()))
        pks = {c["Item"]["PK"]["S"] for c in dynamo.calls}
        self.assertEqual(pks, {"FEATURE#NSE#RELIANCE#1m"})

    def test_table_name_used(self):
        dynamo = _MockDynamo()
        writer = FeatureWriter(dynamo, "my-features-table")
        _run(writer.write(_full_feature_set()))
        tables = {c["TableName"] for c in dynamo.calls}
        self.assertEqual(tables, {"my-features-table"})


# ─────────────────────────────────────────────────────────────────────────────
# TestTTL
# ─────────────────────────────────────────────────────────────────────────────

class TestTTL(unittest.TestCase):
    def _get_ttls(self, fs):
        dynamo = _MockDynamo()
        writer = FeatureWriter(dynamo, "test-features")
        before = int(time.time())
        _run(writer.write(fs))
        after  = int(time.time())
        result = {}
        for c in dynamo.calls:
            sk  = c["Item"]["SK"]["S"]
            ttl = int(c["Item"]["ttl"]["N"])
            result[sk] = ttl
        return result, before, after

    def test_latest_ttl_24h(self):
        ttls, before, after = self._get_ttls(_full_feature_set())
        expected_lo = before + _LATEST_TTL
        expected_hi = after  + _LATEST_TTL
        self.assertGreaterEqual(ttls["LATEST"], expected_lo)
        self.assertLessEqual(   ttls["LATEST"], expected_hi)

    def test_candle_ttl_7d(self):
        ttls, before, after = self._get_ttls(_full_feature_set())
        candle_sk = next(k for k in ttls if k.startswith("CANDLE#"))
        expected_lo = before + _CANDLE_TTL
        expected_hi = after  + _CANDLE_TTL
        self.assertGreaterEqual(ttls[candle_sk], expected_lo)
        self.assertLessEqual(   ttls[candle_sk], expected_hi)

    def test_candle_ttl_longer_than_latest(self):
        ttls, _, _ = self._get_ttls(_full_feature_set())
        candle_sk = next(k for k in ttls if k.startswith("CANDLE#"))
        self.assertGreater(ttls[candle_sk], ttls["LATEST"])


# ─────────────────────────────────────────────────────────────────────────────
# TestFailureHandling
# ─────────────────────────────────────────────────────────────────────────────

class TestFailureHandling(unittest.TestCase):
    def test_dynamo_error_does_not_raise(self):
        # Raise on first call (LATEST or CANDLE row)
        dynamo = _MockDynamo(raise_on_call=1)
        writer = FeatureWriter(dynamo, "test-features")
        # Should not raise
        try:
            _run(writer.write(_full_feature_set()))
        except Exception as e:
            self.fail(f"write() raised unexpectedly: {e}")

    def test_both_writes_fail_silently(self):
        class _AlwaysError:
            def put_item(self, **_): raise RuntimeError("always fails")
        writer = FeatureWriter(_AlwaysError(), "test-features")
        try:
            _run(writer.write(_full_feature_set()))
        except Exception as e:
            self.fail(f"write() raised unexpectedly: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# TestSparseWrite
# ─────────────────────────────────────────────────────────────────────────────

class TestSparseWrite(unittest.TestCase):
    """Validate that None-feature items are correctly persisted (no N:None)."""

    def test_none_features_absent_in_written_item(self):
        dynamo = _MockDynamo()
        writer = FeatureWriter(dynamo, "test-features")
        _run(writer.write(_sparse_feature_set()))
        for call in dynamo.calls:
            item = call["Item"]
            for key in ("rsi_14", "ema_9", "ema_21", "atr_14", "adx_14",
                        "macd", "macd_signal", "macd_hist", "volume_ratio"):
                self.assertNotIn(key, item, f"{key} present in sparse item — should be absent")

    def test_vwap_present_in_sparse_item(self):
        dynamo = _MockDynamo()
        writer = FeatureWriter(dynamo, "test-features")
        _run(writer.write(_sparse_feature_set()))
        for call in dynamo.calls:
            self.assertIn("vwap", call["Item"])


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
