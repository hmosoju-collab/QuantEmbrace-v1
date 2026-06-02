"""
Unit tests for PHASE5-FU-001 — Feature pipeline wiring in DataIngestionService.

Tests cover:
  - _setup_feature_pipeline() instantiates FeatureEngine, FeatureWriter, FeatureArchiver
  - FeatureArchiver.on_phase_change is registered with MarketPhaseGovernor
  - IntradayCandleStream receives feature_engine and feature_writer
  - Graceful degradation: ZerodhaBrokerClient ImportError → candle stream skipped
  - Graceful degradation: InstrumentLoader failure → archiver runs with 0 symbols
  - stop() cancels the candle stream task (no hang)
  - POST_CLOSE phase event reaches archiver via phase governor

Pattern follows the standalone importlib approach used in Phase 4/5 tests.

Isolation: all sys.modules stubs are installed in setUpModule() and removed in
tearDownModule() so this file does not pollute the combined pytest run when
strategy_engine, shared, or execution_engine real packages are imported by
other test files.

Standalone runner: ``python test_data_ingestion_feature_wiring.py``
"""

from __future__ import annotations

import asyncio
import importlib.util as _ilu
import os
import sys
import types
import unittest
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call


# ── Locate service directory ───────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Stub class definitions — pure Python, no sys.modules side effects at
# module level.  All actual sys.modules installations happen inside
# setUpModule() so pytest collection cannot pollute the combined test run.
# ─────────────────────────────────────────────────────────────────────────────

class _Logger:
    def __init__(self, name): self._log = logging.getLogger(name)
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): pass
    def debug(self, *a, **kw): pass
    def error(self, *a, **kw): pass
    def exception(self, *a, **kw): pass
    def critical(self, *a, **kw): pass


class _AWSConfig:
    region                    = "ap-south-1"
    s3_bucket                 = "qe-market-data"
    dynamodb_table_prefix     = "quantembrace"
    dynamodb_table_prices     = "quantembrace-latest-prices"
    dynamodb_table_features   = "quantembrace-features"

class _ZerodhaConfig:
    class _Secret:
        def get_secret_value(self): return "fake"
    api_key      = _Secret()
    access_token = _Secret()

class _AlpacaConfig:
    class _Secret:
        def get_secret_value(self): return "fake"
    api_key    = _Secret()
    api_secret = _Secret()
    data_url   = "https://paper-api.alpaca.markets"

class _AppSettings:
    class _Env:
        value = "test"
    environment = _Env()
    aws         = _AWSConfig()
    zerodha     = _ZerodhaConfig()
    alpaca      = _AlpacaConfig()


class _HealthServer:
    def __init__(self, **kw): pass
    async def start(self): pass
    async def stop(self): pass
    def add_check(self, *a, **kw): pass
    def set_ready(self, *a, **kw): pass


class _MarketPhase:
    POST_CLOSE = "POST_CLOSE"
    def __init__(self, v): self.value = v
    def __eq__(self, other): return self.value == (other.value if hasattr(other, "value") else other)

class _MarketPhaseGovernor:
    def __init__(self):
        self._listeners = []
        self._running = False

    def add_listener(self, fn):
        self._listeners.append(fn)

    def fire(self, phase_name: str):
        for fn in self._listeners:
            fn(phase_name)

    async def run(self) -> None:
        """Stub background loop — runs until stop() is called."""
        self._running = True
        try:
            while self._running:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Signal the run() loop to exit."""
        self._running = False


class _FeatureEngine:
    pass

class _FeatureWriter:
    def __init__(self, dynamo_client, features_table): pass  # must match real signature

class _FeatureArchiver:
    def __init__(self, **kw):
        self._registered = False
    def on_phase_change(self, phase_name: str):
        self._registered = True   # called means it was registered


class _IntradayCandleStream:
    def __init__(self, **kw):
        self.zerodha          = kw.get("zerodha")
        self.feature_engine   = kw.get("feature_engine")
        self.feature_writer   = kw.get("feature_writer")
        self.instrument_tokens = kw.get("instrument_tokens", {})
        self._phase_governor  = kw.get("phase_governor")
        self._stopped         = False

        # Mirror real candle_stream: register self as a phase listener so
        # the ordering test can verify the stream is already registered
        # before governor.run() fires its initial broadcast.
        if self._phase_governor is not None:
            self._phase_governor.add_listener(self._on_phase_change)

    def _on_phase_change(self, phase_name: str) -> None:
        """Stub phase handler — just records that a broadcast was received."""
        self._last_phase = phase_name

    async def start(self):
        # Simulate blocking until stopped
        while not self._stopped:
            await asyncio.sleep(0.01)

    async def stop(self):
        self._stopped = True


class _InstrumentLoader:
    def __init__(self, config_path=None): pass
    def load(self): pass
    def get_all_symbols(self, market):
        if market == "NSE":
            return ["RELIANCE", "TCS", "INFY"]
        return []
    def summary(self): return "3 NSE instruments"


class _ZerodhaBrokerClient:
    """Stub matching the ZerodhaBrokerClient interface used by _setup_feature_pipeline()."""

    # Fake token map matching the 3 symbols returned by _InstrumentLoader
    _TOKEN_MAP: dict[str, int] = {
        "NSE:RELIANCE": 738561,
        "NSE:TCS":      2953217,
        "NSE:INFY":     408065,
    }

    def __init__(self, settings=None): pass

    async def connect(self): pass
    async def disconnect(self): pass

    async def get_instrument_tokens(
        self, exchange: str = "NSE", symbols=None
    ) -> dict[str, int]:
        """Return filtered token map matching the given symbols."""
        if symbols is None:
            return dict(self._TOKEN_MAP)
        return {k: v for k, v in self._TOKEN_MAP.items() if k.split(":", 1)[1] in symbols}


# ─────────────────────────────────────────────────────────────────────────────
# Module-level placeholder — set by setUpModule(), used by all test classes.
# ─────────────────────────────────────────────────────────────────────────────

DataIngestionService: type | None = None

# Keys present in sys.modules BEFORE setUpModule installs stubs; used by
# tearDownModule to restore the module namespace exactly.
_MODULES_SNAPSHOT: frozenset = frozenset()
_MISSING = object()
_MODULES_ORIGINALS: dict[str, Any] = {}
_PARENT_ATTR_ORIGINALS: dict[tuple[str, str], Any] = {}

_OVERWRITTEN_MODULE_KEYS = (
    "shared.logging.logger",
    "shared.config.settings",
    "shared.health.health_server",
    "shared.zerodha.market_phase",
    "data_ingestion.connectors.alpaca_connector",
    "data_ingestion.connectors.zerodha_connector",
    "data_ingestion.processors.tick_processor",
    "data_ingestion.publishers.kafka_tick_publisher",
    "data_ingestion.storage.s3_writer",
    "data_ingestion.storage.dynamo_writer",
    "data_ingestion.features.feature_engine",
    "data_ingestion.features.feature_writer",
    "data_ingestion.features.feature_archiver",
    "data_ingestion.candle_stream",
    "strategy_engine.universe.instrument_loader",
    "execution_engine.brokers.zerodha_broker",
    "data_ingestion.service",
)

_OVERWRITTEN_PARENT_ATTRS = (
    ("shared.logging", "logger"),
    ("shared.config", "settings"),
    ("shared.zerodha", "market_phase"),
    ("strategy_engine.universe", "instrument_loader"),
)


# ─────────────────────────────────────────────────────────────────────────────
# setUpModule / tearDownModule — install and remove all stubs
# ─────────────────────────────────────────────────────────────────────────────

def setUpModule() -> None:  # noqa: N802  (unittest convention is CamelCase here)
    """Install all stub modules into sys.modules, then load the service under test.

    This runs once before any test in this file executes.  Crucially it runs
    AFTER pytest collection, so strategy_engine / shared / execution_engine
    stubs never shadow real packages during collection of other test files.
    """
    global DataIngestionService, _MODULES_SNAPSHOT, _MODULES_ORIGINALS, _PARENT_ATTR_ORIGINALS

    # Snapshot keys that already exist so tearDownModule can restore exactly.
    _MODULES_SNAPSHOT = frozenset(sys.modules.keys())
    _MODULES_ORIGINALS = {
        key: sys.modules.get(key, _MISSING)
        for key in _OVERWRITTEN_MODULE_KEYS
    }
    _PARENT_ATTR_ORIGINALS = {
        (module_name, attr): getattr(sys.modules[module_name], attr, _MISSING)
        for module_name, attr in _OVERWRITTEN_PARENT_ATTRS
        if module_name in sys.modules
    }

    # ── Package namespace stubs ────────────────────────────────────────────
    for pkg in (
        "shared", "shared.config", "shared.logging", "shared.health",
        "shared.zerodha", "shared.models",
        "data_ingestion", "data_ingestion.connectors", "data_ingestion.processors",
        "data_ingestion.publishers", "data_ingestion.storage",
        "data_ingestion.features",
        "strategy_engine", "strategy_engine.universe",
        "execution_engine", "execution_engine.brokers",
    ):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []
            sys.modules[pkg] = m

    # ── shared.logging.logger ──────────────────────────────────────────────
    _log_mod = types.ModuleType("shared.logging.logger")
    _log_mod.get_logger = lambda *a, **kw: _Logger(str(a))   # type: ignore
    _log_mod.set_correlation_id = lambda *a, **kw: None       # type: ignore
    sys.modules["shared.logging.logger"] = _log_mod
    sys.modules["shared.logging"].logger = _log_mod           # type: ignore

    # ── AppSettings ────────────────────────────────────────────────────────
    _settings_mod = types.ModuleType("shared.config.settings")
    _settings_mod.AppSettings  = _AppSettings       # type: ignore
    _settings_mod.get_settings = lambda: _AppSettings()  # type: ignore
    sys.modules["shared.config.settings"] = _settings_mod
    sys.modules["shared.config"].settings = _settings_mod  # type: ignore

    # ── HealthServer ───────────────────────────────────────────────────────
    _health_mod = types.ModuleType("shared.health.health_server")
    _health_mod.HealthServer = _HealthServer  # type: ignore
    sys.modules["shared.health.health_server"] = _health_mod

    # ── MarketPhase + MarketPhaseGovernor ──────────────────────────────────
    _mp_mod = types.ModuleType("shared.zerodha.market_phase")
    _mp_mod.MarketPhase         = _MarketPhase        # type: ignore
    _mp_mod.MarketPhaseGovernor = _MarketPhaseGovernor  # type: ignore
    sys.modules["shared.zerodha.market_phase"] = _mp_mod
    sys.modules["shared.zerodha"].market_phase = _mp_mod  # type: ignore

    # ── Connectors, processor, publisher, storage ──────────────────────────
    for _name, _cls_name in [
        ("data_ingestion.connectors.alpaca_connector", "AlpacaConnector"),
        ("data_ingestion.connectors.zerodha_connector", "ZerodhaConnector"),
        ("data_ingestion.connectors.base", "BaseConnector"),
        ("data_ingestion.processors.tick_processor", "TickProcessor"),
        ("data_ingestion.publishers.kafka_tick_publisher", "KafkaTickPublisher"),
        ("data_ingestion.storage.s3_writer", "S3Writer"),
        ("data_ingestion.storage.dynamo_writer", "DynamoWriter"),
    ]:
        _m = types.ModuleType(_name)
        _stub = MagicMock()
        setattr(_m, _cls_name, _stub)
        sys.modules[_name] = _m

    # Wire connector instances
    for _cn in ("AlpacaConnector", "ZerodhaConnector"):
        _conn = sys.modules[
            "data_ingestion.connectors.alpaca_connector" if "Alpaca" in _cn
            else "data_ingestion.connectors.zerodha_connector"
        ]
        _inst = MagicMock()
        _inst.is_connected = True
        _inst.connect      = AsyncMock()
        _inst.disconnect   = AsyncMock()
        _inst.subscribe    = AsyncMock()
        getattr(_conn, _cn).return_value = _inst

    # KafkaTickPublisher
    _kafka_pub  = sys.modules["data_ingestion.publishers.kafka_tick_publisher"].KafkaTickPublisher
    _kafka_inst = MagicMock()
    _kafka_inst.start         = AsyncMock()
    _kafka_inst.stop          = AsyncMock()
    _kafka_inst.pending_count = 0
    _kafka_pub.return_value   = _kafka_inst

    # S3Writer / DynamoWriter
    for _mod_name, _attr in [
        ("data_ingestion.storage.s3_writer",    "S3Writer"),
        ("data_ingestion.storage.dynamo_writer", "DynamoWriter"),
    ]:
        _stor  = sys.modules[_mod_name]
        _inst2 = MagicMock()
        _inst2.flush = AsyncMock()
        getattr(_stor, _attr).return_value = _inst2

    # ── Feature components ─────────────────────────────────────────────────
    _fe_mod = types.ModuleType("data_ingestion.features.feature_engine")
    _fe_mod.FeatureEngine = _FeatureEngine  # type: ignore
    sys.modules["data_ingestion.features.feature_engine"] = _fe_mod

    _fw_mod = types.ModuleType("data_ingestion.features.feature_writer")
    _fw_mod.FeatureWriter = _FeatureWriter  # type: ignore
    sys.modules["data_ingestion.features.feature_writer"] = _fw_mod

    _fa_mod = types.ModuleType("data_ingestion.features.feature_archiver")
    _fa_mod.FeatureArchiver = _FeatureArchiver  # type: ignore
    sys.modules["data_ingestion.features.feature_archiver"] = _fa_mod

    # ── IntradayCandleStream ───────────────────────────────────────────────
    _cs_mod = types.ModuleType("data_ingestion.candle_stream")
    _cs_mod.IntradayCandleStream = _IntradayCandleStream  # type: ignore
    sys.modules["data_ingestion.candle_stream"] = _cs_mod

    # ── InstrumentLoader ───────────────────────────────────────────────────
    _il_mod = types.ModuleType("strategy_engine.universe.instrument_loader")
    _il_mod.InstrumentLoader = _InstrumentLoader  # type: ignore
    sys.modules["strategy_engine.universe.instrument_loader"] = _il_mod
    sys.modules["strategy_engine.universe"].instrument_loader = _il_mod  # type: ignore

    # ── ZerodhaBrokerClient ────────────────────────────────────────────────
    _zb_mod = types.ModuleType("execution_engine.brokers.zerodha_broker")
    _zb_mod.ZerodhaBrokerClient = _ZerodhaBrokerClient  # type: ignore
    sys.modules["execution_engine.brokers.zerodha_broker"] = _zb_mod

    # ── Load the service under test ────────────────────────────────────────
    _svc_mod = _load_module(
        os.path.join("data_ingestion", "service.py"),
        "data_ingestion.service",
    )
    DataIngestionService = _svc_mod.DataIngestionService


def tearDownModule() -> None:  # noqa: N802
    """Restore every sys.modules entry and package attr touched by setUpModule."""
    added = frozenset(sys.modules.keys()) - _MODULES_SNAPSHOT
    for key in added:
        sys.modules.pop(key, None)
    for key, original in _MODULES_ORIGINALS.items():
        if original is _MISSING:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = original
    for (module_name, attr), original in _PARENT_ATTR_ORIGINALS.items():
        parent = sys.modules.get(module_name)
        if parent is None:
            continue
        if original is _MISSING:
            try:
                delattr(parent, attr)
            except AttributeError:
                pass
        else:
            setattr(parent, attr, original)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_service():
    return DataIngestionService(
        settings=_AppSettings(),
        instruments_config_path="/fake/instruments.yaml",
    )


def _run_setup(svc) -> None:
    """Call _setup_feature_pipeline() directly with boto3 patched out."""
    boto3_mock = MagicMock()
    boto3_mock.client.return_value = MagicMock()
    with patch.dict(sys.modules, {"boto3": boto3_mock}):
        _run(svc._setup_feature_pipeline())


# ─────────────────────────────────────────────────────────────────────────────
# TestFeaturePipelineWiring
# ─────────────────────────────────────────────────────────────────────────────

class TestFeaturePipelineWiring(unittest.TestCase):
    """Verify _setup_feature_pipeline() produces the correct object graph."""

    def setUp(self):
        self.svc = _make_service()
        _run_setup(self.svc)

    def test_feature_archiver_instantiated(self):
        self.assertIsNotNone(
            self.svc._feature_archiver,
            "_feature_archiver should be set after _setup_feature_pipeline()",
        )

    def test_feature_archiver_is_correct_type(self):
        self.assertIsInstance(self.svc._feature_archiver, _FeatureArchiver)

    def test_phase_governor_instantiated(self):
        self.assertIsNotNone(self.svc._phase_governor)

    def test_phase_governor_is_correct_type(self):
        self.assertIsInstance(self.svc._phase_governor, _MarketPhaseGovernor)

    def test_archiver_registered_with_governor(self):
        """on_phase_change must be in the governor's listener list."""
        archiver = self.svc._feature_archiver
        governor = self.svc._phase_governor
        self.assertIn(
            archiver.on_phase_change,
            governor._listeners,
            "FeatureArchiver.on_phase_change not registered with MarketPhaseGovernor",
        )

    def test_candle_stream_instantiated(self):
        self.assertIsNotNone(
            self.svc._candle_stream,
            "_candle_stream should be set when ZerodhaBrokerClient is available",
        )

    def test_candle_stream_has_feature_engine(self):
        self.assertIsNotNone(self.svc._candle_stream.feature_engine)

    def test_candle_stream_has_feature_writer(self):
        self.assertIsNotNone(self.svc._candle_stream.feature_writer)

    def test_candle_stream_task_created(self):
        self.assertIsNotNone(
            self.svc._candle_stream_task,
            "candle_stream_task should be an asyncio.Task",
        )
        self.assertIsInstance(self.svc._candle_stream_task, asyncio.Task)

    def test_phase_governor_task_created(self):
        """Bug 2 regression: asyncio.create_task(governor.run()) must be called."""
        self.assertIsNotNone(
            self.svc._phase_governor_task,
            "_phase_governor_task should be set after _setup_feature_pipeline(). "
            "If None, governor.run() was never started — POST_CLOSE will never fire.",
        )
        self.assertIsInstance(self.svc._phase_governor_task, asyncio.Task)

    def test_candle_stream_receives_non_empty_token_map(self):
        """Finding 1 regression: candle stream must have a non-empty instrument_tokens dict.

        An empty dict means the round-robin queue is empty and no candles
        are ever fetched, producing zero live features.
        get_instrument_tokens() must be called after broker.connect().
        """
        stream = self.svc._candle_stream
        self.assertIsNotNone(stream, "_candle_stream must be set for this test")
        self.assertGreater(
            len(stream.instrument_tokens),
            0,
            "instrument_tokens must be non-empty — "
            "get_instrument_tokens() was not called or returned {}. "
            "Without tokens the candle queue is empty and no features are generated.",
        )
        # Every key must follow the {EXCHANGE}:{SYMBOL} format
        for key in stream.instrument_tokens:
            self.assertIn(":", key, f"Token map key '{key}' missing exchange prefix")

    def test_candle_stream_registered_with_governor_before_task_start(self):
        """Finding 2 regression: stream must be registered as a governor listener
        before governor.run() fires its initial phase broadcast.

        After setup, the candle stream's phase handler must be in the governor's
        listener list. If the governor task started before the stream was created,
        the initial broadcast would have been missed and the stream would be stuck
        in POST_CLOSE indefinitely.
        """
        governor = self.svc._phase_governor
        stream   = self.svc._candle_stream
        self.assertIsNotNone(stream, "_candle_stream must be set for this test")
        self.assertIn(
            stream._on_phase_change,
            governor._listeners,
            "IntradayCandleStream._on_phase_change is not in governor._listeners. "
            "Either the stream was not passed phase_governor=, or governor.run() "
            "was started before the stream registered — causing the initial "
            "phase broadcast to be missed.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# TestPostCloseArchiveTrigger
# ─────────────────────────────────────────────────────────────────────────────

class TestPostCloseArchiveTrigger(unittest.TestCase):
    """Verify POST_CLOSE phase event reaches the archiver via the governor."""

    def setUp(self):
        self.svc = _make_service()
        _run_setup(self.svc)

    def test_post_close_fires_archiver_listener(self):
        """Firing POST_CLOSE from the governor should reach the archiver."""
        archiver = self.svc._feature_archiver
        governor = self.svc._phase_governor
        self.assertFalse(archiver._registered)
        # Simulate MarketPhaseGovernor emitting POST_CLOSE
        governor.fire("POST_CLOSE")
        self.assertTrue(
            archiver._registered,
            "FeatureArchiver.on_phase_change was not called on POST_CLOSE",
        )


# ─────────────────────────────────────────────────────────────────────────────
# TestGracefulDegradation
# ─────────────────────────────────────────────────────────────────────────────

class TestGracefulDegradation(unittest.TestCase):
    """Verify the pipeline degrades gracefully on partial failures."""

    def test_no_candle_stream_when_broker_import_fails(self):
        """If ZerodhaBrokerClient is not importable, candle stream is None."""
        svc = _make_service()
        boto3_mock = MagicMock()
        boto3_mock.client.return_value = MagicMock()

        # Shadow the broker module to simulate ImportError
        saved = sys.modules.get("execution_engine.brokers.zerodha_broker")
        sys.modules.pop("execution_engine.brokers.zerodha_broker", None)

        try:
            with patch.dict(sys.modules, {"boto3": boto3_mock}):
                _run(svc._setup_feature_pipeline())
        finally:
            if saved is not None:
                sys.modules["execution_engine.brokers.zerodha_broker"] = saved

        # Archiver should still be wired even without candle stream
        self.assertIsNotNone(svc._feature_archiver)
        self.assertIsNone(
            svc._candle_stream,
            "candle_stream must be None when ZerodhaBrokerClient is unavailable",
        )
        self.assertIsNone(svc._candle_stream_task)

    def test_archiver_has_zero_symbols_on_loader_failure(self):
        """If InstrumentLoader raises, FeatureArchiver is still created (0 symbols)."""
        svc = _make_service()
        boto3_mock = MagicMock()
        boto3_mock.client.return_value = MagicMock()

        # Make InstrumentLoader raise on load()
        class _BrokenLoader:
            def __init__(self, **kw): pass
            def load(self): raise RuntimeError("YAML missing")
            def get_all_symbols(self, *a): return []

        saved_il = sys.modules.get("strategy_engine.universe.instrument_loader")
        _broken  = types.ModuleType("strategy_engine.universe.instrument_loader")
        _broken.InstrumentLoader = _BrokenLoader  # type: ignore
        sys.modules["strategy_engine.universe.instrument_loader"] = _broken

        try:
            with patch.dict(sys.modules, {"boto3": boto3_mock}):
                _run(svc._setup_feature_pipeline())
        finally:
            if saved_il is not None:
                sys.modules["strategy_engine.universe.instrument_loader"] = saved_il

        # Archiver should be present even with 0 symbols
        self.assertIsNotNone(svc._feature_archiver)
        self.assertIsNotNone(svc._phase_governor)

    def test_candle_stream_skipped_when_no_nse_symbols(self):
        """Finding 1 fail-closed regression: if InstrumentLoader returns zero NSE symbols,
        the candle stream must NOT be started — never pass symbols=None to
        get_instrument_tokens(), which would silently pull all ~1800 NSE instruments.
        """
        svc = _make_service()
        boto3_mock = MagicMock()
        boto3_mock.client.return_value = MagicMock()

        # InstrumentLoader succeeds but returns an empty symbol list
        class _EmptyLoader:
            def __init__(self, **kw): pass
            def load(self): pass
            def get_all_symbols(self, market): return []  # empty universe

        saved_il = sys.modules.get("strategy_engine.universe.instrument_loader")
        _empty   = types.ModuleType("strategy_engine.universe.instrument_loader")
        _empty.InstrumentLoader = _EmptyLoader  # type: ignore
        sys.modules["strategy_engine.universe.instrument_loader"] = _empty

        try:
            with patch.dict(sys.modules, {"boto3": boto3_mock}):
                _run(svc._setup_feature_pipeline())
        finally:
            if saved_il is not None:
                sys.modules["strategy_engine.universe.instrument_loader"] = saved_il

        # Archiver and governor must still be wired (they don't depend on symbols)
        self.assertIsNotNone(svc._feature_archiver)
        self.assertIsNotNone(svc._phase_governor)

        # Candle stream must NOT be started — fail-closed, not expanded to all NSE
        self.assertIsNone(
            svc._candle_stream,
            "candle_stream must be None when nse_symbols is empty. "
            "Starting with symbols=None would silently expand to all ~1800 NSE instruments.",
        )
        self.assertIsNone(
            svc._candle_stream_task,
            "candle_stream_task must be None when nse_symbols is empty.",
        )

    def test_setup_does_not_raise_on_total_failure(self):
        """Even if boto3 import fails, _setup_feature_pipeline must not raise."""
        svc = _make_service()
        # Corrupt boto3 so that .client() raises
        boto3_mock = MagicMock()
        boto3_mock.client.side_effect = RuntimeError("boto3 unavailable")

        with patch.dict(sys.modules, {"boto3": boto3_mock}):
            try:
                _run(svc._setup_feature_pipeline())
            except Exception as exc:
                self.fail(
                    f"_setup_feature_pipeline() raised unexpectedly: {exc}"
                )


# ─────────────────────────────────────────────────────────────────────────────
# TestCandleStreamShutdown
# ─────────────────────────────────────────────────────────────────────────────

class TestCandleStreamShutdown(unittest.TestCase):
    """Verify stop() cleanly cancels both the candle stream and governor tasks."""

    def test_stop_cancels_candle_stream_task(self):
        """stop() should cancel the background candle_stream_task without hanging."""
        svc = _make_service()
        _run_setup(svc)

        async def _run_stop():
            # Give the candle stream and governor tasks a moment to start
            await asyncio.sleep(0.05)
            # Simulate the rest of the stop() teardown (mocked dependencies)
            svc._running      = True  # pretend service was running
            svc._health_server  = _HealthServer()
            svc._kafka_publisher = None
            svc._s3_writer      = None
            svc._dynamo_writer   = None
            await svc.stop()

        _run(_run_stop())

        # After stop(), the candle stream task should be done
        self.assertTrue(
            svc._candle_stream_task.done(),
            "candle_stream_task should be done after stop()",
        )
        self.assertTrue(
            svc._candle_stream._stopped,
            "IntradayCandleStream.stop() should have been called",
        )

    def test_stop_cancels_governor_task(self):
        """stop() should also cancel the phase governor background task."""
        svc = _make_service()
        _run_setup(svc)

        async def _run_stop():
            await asyncio.sleep(0.05)
            svc._running      = True
            svc._health_server  = _HealthServer()
            svc._kafka_publisher = None
            svc._s3_writer      = None
            svc._dynamo_writer   = None
            await svc.stop()

        _run(_run_stop())

        self.assertTrue(
            svc._phase_governor_task.done(),
            "_phase_governor_task should be done after stop(). "
            "If still running, governor.run() loop was not cancelled.",
        )


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
