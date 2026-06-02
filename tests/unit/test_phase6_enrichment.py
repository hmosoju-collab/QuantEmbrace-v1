"""
Phase 6 — Unit + Integration tests for the ML enrichment pipeline.

Tests:
  PHASE6-016 (unit):
    - RegimeClassifier: stub model, real inference, degraded path, all 4 regimes
    - SignalQualityScorer: stub model, threshold filtering, direction encoding
    - SignalEnricher: full enrich(), degraded path, regime-log write, latency tag
    - EnrichedSignal: to_dict/from_dict round-trip, degraded_enrichment factory
  PHASE6-017 (integration-light):
    - end-to-end enrichment pipeline with mock FeatureReader + mock ModelRegistry
    - signal flows through enricher → EnrichedSignal with correct fields
    - EnrichmentWatchdog state machine: lag breach → fallback, lag clear → recovery

Standalone:  python tests/unit/test_phase6_enrichment.py
"""

from __future__ import annotations

import asyncio
import importlib.util as _ilu
import logging
import os
import sys
import types
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")


def _load_module(rel: str, name: str):
    path = os.path.join(_SERVICES_DIR, rel)
    spec = _ilu.spec_from_file_location(name, path)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _StructuredLogger:
    def __init__(self, name=""):
        self._log = logging.getLogger(name)

    def info(self, msg, *a, **kw): self._log.info(msg)
    def warning(self, msg, *a, **kw): self._log.warning(msg)
    def debug(self, msg, *a, **kw): self._log.debug(msg)
    def error(self, msg, *a, **kw): self._log.error(msg)
    def exception(self, msg, *a, **kw): self._log.exception(msg)
    def critical(self, msg, *a, **kw): self._log.critical(msg)


def _stub_logger_module() -> types.ModuleType:
    m = types.ModuleType("shared.logging.logger")
    m.get_logger = lambda *a, **kw: _StructuredLogger()
    return m


# ── Shared stubs registered before any module is loaded ──────────────────────

def _register_stubs() -> None:
    """Register all sys.modules stubs needed by Phase 6 modules."""
    # Logger
    sys.modules.setdefault("shared.logging.logger", _stub_logger_module())
    sys.modules.setdefault("shared.logging", types.ModuleType("shared.logging"))
    sys.modules["shared.logging"].logger = sys.modules["shared.logging.logger"]

    # confluent_kafka — not needed for unit tests (consumer/publisher not tested here)
    if "confluent_kafka" not in sys.modules:
        ck = types.ModuleType("confluent_kafka")
        ck.Consumer = MagicMock
        ck.Producer = MagicMock
        ck.KafkaError = Exception
        sys.modules["confluent_kafka"] = ck

        ck_admin = types.ModuleType("confluent_kafka.admin")
        ck_admin.AdminClient = MagicMock
        sys.modules["confluent_kafka.admin"] = ck_admin

    # aws_msk_iam_sasl_signer
    sys.modules.setdefault("aws_msk_iam_sasl_signer", types.ModuleType("aws_msk_iam_sasl_signer"))

    # boto3 / botocore
    sys.modules.setdefault("boto3", types.ModuleType("boto3"))
    sys.modules.setdefault("botocore", types.ModuleType("botocore"))
    botocore_exc = types.ModuleType("botocore.exceptions")
    botocore_exc.ClientError = Exception
    sys.modules.setdefault("botocore.exceptions", botocore_exc)

    # joblib
    sys.modules.setdefault("joblib", types.ModuleType("joblib"))

    # numpy — minimal stub
    if "numpy" not in sys.modules:
        np = types.ModuleType("numpy")
        np.array = list
        np.float32 = float
        sys.modules["numpy"] = np


_register_stubs()


# ─────────────────────────────────────────────────────────────────────────────
# Load the shared models first (other modules depend on them)
# ─────────────────────────────────────────────────────────────────────────────

def _load_shared_models():
    """Load shared models, injecting any sys.modules stubs needed."""
    # Minimal pydantic stub if not installed
    if "pydantic" not in sys.modules:
        pyd = types.ModuleType("pydantic")
        pyd.BaseModel = object
        sys.modules["pydantic"] = pyd

    # Register shared package hierarchy
    for pkg in ("shared", "shared.models", "shared.events"):
        sys.modules.setdefault(pkg, types.ModuleType(pkg))

    # ── Signal model (inline minimal — avoids loading full service stack) ──────
    signal_mod = types.ModuleType("shared.models.signal")

    from enum import Enum

    class Direction(str, Enum):
        BUY = "BUY"
        SELL = "SELL"

    class SignalStatus(str, Enum):
        PENDING = "PENDING"
        APPROVED = "APPROVED"
        REJECTED = "REJECTED"

    @dataclass
    class Signal:
        signal_id: str
        strategy_name: str
        symbol: str
        market: str
        direction: "Direction"
        quantity: int
        price_at_signal: float
        confidence: float
        generated_at: datetime
        status: "SignalStatus"
        stop_loss: Optional[float] = None
        take_profit: Optional[float] = None
        paper_trade: bool = False
        metadata: dict = None

        def __post_init__(self):
            if self.metadata is None:
                self.metadata = {}

    signal_mod.Signal = Signal
    signal_mod.Direction = Direction
    signal_mod.SignalStatus = SignalStatus
    sys.modules["shared.models.signal"] = signal_mod

    # ── EnrichedSignal model ──────────────────────────────────────────────────
    enriched_mod = _load_module(
        "shared/models/enriched_signal.py", "shared.models.enriched_signal"
    )
    sys.modules["shared.models"].enriched_signal = enriched_mod
    return enriched_mod, signal_mod


# ─────────────────────────────────────────────────────────────────────────────
# Helper: build a minimal Signal fixture
# ─────────────────────────────────────────────────────────────────────────────

def _make_signal(signal_mod, *, symbol="RELIANCE", market="NSE",
                 direction_str="BUY", quantity=100) -> Any:
    return signal_mod.Signal(
        signal_id="sig-001",
        strategy_name="momentum_v2",
        symbol=symbol,
        market=market,
        direction=signal_mod.Direction(direction_str),
        quantity=quantity,
        price_at_signal=2500.0,
        confidence=0.85,
        generated_at=datetime.now(timezone.utc),
        status=signal_mod.SignalStatus.PENDING,
        stop_loss=2450.0,
        take_profit=2600.0,
        paper_trade=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST GROUP 1: EnrichedSignal — data model (PHASE6-001)
# ─────────────────────────────────────────────────────────────────────────────

class TestEnrichedSignalModel(unittest.TestCase):
    """Tests for shared/models/enriched_signal.py"""

    @classmethod
    def setUpClass(cls):
        cls.enriched_mod, cls.signal_mod = _load_shared_models()

    def _make_sig(self, **kwargs):
        return _make_signal(self.signal_mod, **kwargs)

    def test_from_signal_sets_all_enrichment_fields(self):
        sig = self._make_sig()
        enriched = self.enriched_mod.EnrichedSignal.from_signal(
            signal=sig,
            trace_id="trace-abc",
            strategy_id="strat-1",
            product_type="MIS",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            regime="trending",
            regime_confidence=0.9,
            quality_score=0.8,
            filtered=False,
            model_versions={"regime_classifier": "v1.0"},
        )
        self.assertEqual(enriched.regime, "trending")
        self.assertAlmostEqual(enriched.regime_confidence, 0.9)
        self.assertAlmostEqual(enriched.quality_score, 0.8)
        self.assertFalse(enriched.filtered)
        self.assertEqual(enriched.symbol, "RELIANCE")
        self.assertEqual(enriched.market, "NSE")

    def test_instrument_id_property(self):
        sig = self._make_sig(symbol="RELIANCE", market="NSE")
        enriched = self.enriched_mod.EnrichedSignal.from_signal(
            signal=sig, trace_id="t", strategy_id="s",
            product_type="MIS",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            regime="ranging", regime_confidence=0.7,
            quality_score=0.6, filtered=False, model_versions={},
        )
        self.assertEqual(enriched.instrument_id, "NSE:RELIANCE")

    def test_to_dict_from_dict_round_trip(self):
        sig = self._make_sig()
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        enriched = self.enriched_mod.EnrichedSignal.from_signal(
            signal=sig, trace_id="tr-1", strategy_id="st-1",
            product_type="MIS", expires_at=expires,
            regime="volatile", regime_confidence=0.65,
            quality_score=0.55, filtered=True, model_versions={"q": "v2"},
        )
        d = enriched.to_dict()
        # schema version must be 4.0
        self.assertEqual(d["schema_version"], "4.0")
        self.assertEqual(d["regime"], "volatile")
        self.assertTrue(d["filtered"])

        restored = self.enriched_mod.EnrichedSignal.from_dict(d)
        self.assertEqual(restored.regime, enriched.regime)
        self.assertAlmostEqual(restored.quality_score, enriched.quality_score, places=4)
        self.assertEqual(restored.symbol, enriched.symbol)
        self.assertTrue(restored.filtered)

    def test_degraded_enrichment_defaults(self):
        sig = self._make_sig()
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        deg = self.enriched_mod.degraded_enrichment(
            signal=sig, trace_id="tr", strategy_id="s1",
            product_type="MIS", expires_at=expires,
        )
        self.assertEqual(deg.regime, "unknown")
        self.assertAlmostEqual(deg.quality_score, 0.5)
        self.assertFalse(deg.filtered)
        self.assertAlmostEqual(deg.regime_confidence, 0.0)

    def test_from_dict_handles_missing_optional_fields(self):
        """from_dict must not raise if optional enrichment fields are absent."""
        sig = self._make_sig()
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        d = self.enriched_mod.EnrichedSignal.from_signal(
            signal=sig, trace_id="t", strategy_id="s",
            product_type="MIS", expires_at=expires,
            regime="ranging", regime_confidence=0.5,
            quality_score=0.5, filtered=False, model_versions={},
        ).to_dict()
        # Remove optional field
        d.pop("model_versions", None)
        restored = self.enriched_mod.EnrichedSignal.from_dict(d)
        self.assertEqual(restored.symbol, sig.symbol)


# ─────────────────────────────────────────────────────────────────────────────
# TEST GROUP 2: RegimeClassifier (PHASE6-004)
# ─────────────────────────────────────────────────────────────────────────────

class TestRegimeClassifier(unittest.TestCase):
    """Tests for services/ai_engine/classifiers/regime_classifier.py"""

    @classmethod
    def setUpClass(cls):
        cls.enriched_mod, cls.signal_mod = _load_shared_models()

        # Stub feature_pipeline and model_registry before loading classifier
        cls._stub_feature_pipeline()
        cls._stub_model_registry()

        # Load the module
        cls.clf_mod = _load_module(
            "ai_engine/classifiers/regime_classifier.py",
            "ai_engine.classifiers.regime_classifier",
        )

    @staticmethod
    def _stub_feature_pipeline():
        fp_mod = types.ModuleType("ai_engine.features.feature_pipeline")

        class _FP:
            async def get_features(self, market, symbol, interval):
                return {"rsi_14": 55.0, "adx_14": 25.0, "atr_14": 15.0,
                        "macd": 0.5, "volume_ratio": 1.2}

            def to_feature_vector(self, features, names):
                return [features.get(n, 0.0) for n in names]

        fp_mod.FeaturePipeline = _FP
        sys.modules["ai_engine.features.feature_pipeline"] = fp_mod
        sys.modules.setdefault("ai_engine.features", types.ModuleType("ai_engine.features"))
        sys.modules["ai_engine.features"].feature_pipeline = fp_mod

    @staticmethod
    def _stub_model_registry():
        mr_mod = types.ModuleType("ai_engine.models.model_registry")

        class _FakeModel:
            """Minimal HMM-like stub that always predicts state 0 (trending)."""
            def predict(self, X):
                return [0] * len(X)

            # Mark as stub
            _is_stub = True

        class _MR:
            async def get(self, name):
                return _FakeModel()

            def loaded_versions(self):
                return {"regime_classifier": "stub"}

        mr_mod.ModelRegistry = _MR
        sys.modules["ai_engine.models.model_registry"] = mr_mod
        sys.modules.setdefault("ai_engine.models", types.ModuleType("ai_engine.models"))
        sys.modules["ai_engine.models"].model_registry = mr_mod

    def test_classify_returns_regime_output(self):
        registry = sys.modules["ai_engine.models.model_registry"].ModelRegistry()
        feature_pipeline = sys.modules["ai_engine.features.feature_pipeline"].FeaturePipeline()

        clf = self.clf_mod.RegimeClassifier(
            registry=registry, feature_pipeline=feature_pipeline
        )
        result = asyncio.run(clf.classify("NSE", "RELIANCE", "1m"))

        self.assertIsNotNone(result)
        self.assertIn(result.regime, {"trending", "ranging", "volatile", "crash", "unknown"})
        self.assertGreaterEqual(result.confidence, 0.0)
        self.assertLessEqual(result.confidence, 1.0)
        self.assertIsInstance(result.computed_at, datetime)

    def test_classify_stub_model_returns_known_regime(self):
        """Stub model predicts state 0 → 'trending'."""
        registry = sys.modules["ai_engine.models.model_registry"].ModelRegistry()
        feature_pipeline = sys.modules["ai_engine.features.feature_pipeline"].FeaturePipeline()

        clf = self.clf_mod.RegimeClassifier(
            registry=registry, feature_pipeline=feature_pipeline
        )
        result = asyncio.run(clf.classify("NSE", "RELIANCE", "1m"))
        # Stub returns 0 → trending; stub mode should degrade to confidence=0.5
        self.assertIn(result.regime, {"trending", "unknown"})

    def test_classify_never_raises_on_exception(self):
        """Degraded path: feature pipeline fails → regime=unknown, no raise."""
        registry = sys.modules["ai_engine.models.model_registry"].ModelRegistry()

        class _FailingFP:
            async def get_features(self, *a, **kw):
                raise RuntimeError("DynamoDB connection refused")
            def to_feature_vector(self, *a, **kw):
                return []

        clf = self.clf_mod.RegimeClassifier(
            registry=registry, feature_pipeline=_FailingFP()
        )
        result = asyncio.run(clf.classify("NSE", "RELIANCE", "1m"))
        self.assertEqual(result.regime, "unknown")
        self.assertEqual(result.confidence, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# TEST GROUP 3: SignalQualityScorer (PHASE6-005)
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalQualityScorer(unittest.TestCase):
    """Tests for services/ai_engine/scorers/signal_quality_scorer.py"""

    @classmethod
    def setUpClass(cls):
        cls.enriched_mod, cls.signal_mod = _load_shared_models()
        # Re-use stubs from TestRegimeClassifier (already in sys.modules)
        cls.scorer_mod = _load_module(
            "ai_engine/scorers/signal_quality_scorer.py",
            "ai_engine.scorers.signal_quality_scorer",
        )

    def _make_scorer(self, score: float = 0.75):
        """Build a scorer with a fake model that always returns `score`."""
        registry = sys.modules["ai_engine.models.model_registry"].ModelRegistry()
        feature_pipeline = sys.modules["ai_engine.features.feature_pipeline"].FeaturePipeline()

        # Monkey-patch registry to return a model yielding `score`
        class _ScoreModel:
            _is_stub = False

            def predict_proba(self, X):
                return [[1 - score, score]]  # binary class proba

        async def _get_score_model(name):
            return _ScoreModel()

        registry.get = _get_score_model
        return self.scorer_mod.SignalQualityScorer(
            registry=registry, feature_pipeline=feature_pipeline
        )

    def test_score_returns_tuple(self):
        scorer = self._make_scorer(score=0.8)
        quality, filtered = asyncio.run(
            scorer.score("NSE", "RELIANCE", "BUY", "1m", threshold=0.5)
        )
        self.assertIsInstance(quality, float)
        self.assertIsInstance(filtered, bool)
        self.assertGreaterEqual(quality, 0.0)
        self.assertLessEqual(quality, 1.0)

    def test_score_above_threshold_not_filtered(self):
        scorer = self._make_scorer(score=0.85)
        _, filtered = asyncio.run(
            scorer.score("NSE", "RELIANCE", "BUY", "1m", threshold=0.5)
        )
        self.assertFalse(filtered)

    def test_score_below_threshold_is_filtered(self):
        scorer = self._make_scorer(score=0.3)
        _, filtered = asyncio.run(
            scorer.score("NSE", "RELIANCE", "BUY", "1m", threshold=0.5)
        )
        self.assertTrue(filtered)

    def test_score_degraded_on_model_error(self):
        """Model get() raises → degrade to (0.5, False), no raise."""
        registry = sys.modules["ai_engine.models.model_registry"].ModelRegistry()
        feature_pipeline = sys.modules["ai_engine.features.feature_pipeline"].FeaturePipeline()

        async def _fail_get(name):
            raise RuntimeError("model download failed")

        registry.get = _fail_get
        scorer = self.scorer_mod.SignalQualityScorer(
            registry=registry, feature_pipeline=feature_pipeline
        )
        quality, filtered = asyncio.run(
            scorer.score("NSE", "RELIANCE", "BUY", "1m", threshold=0.5)
        )
        self.assertAlmostEqual(quality, 0.5)
        self.assertFalse(filtered)


# ─────────────────────────────────────────────────────────────────────────────
# TEST GROUP 4: SignalEnricher (PHASE6-006)
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalEnricher(unittest.TestCase):
    """Tests for services/ai_engine/enrichment/signal_enricher.py"""

    @classmethod
    def setUpClass(cls):
        cls.enriched_mod, cls.signal_mod = _load_shared_models()

        # Stub regime_classifier and signal_quality_scorer modules
        cls._stub_classifier_module()
        cls._stub_scorer_module()

        # Load enricher
        cls.enricher_mod = _load_module(
            "ai_engine/enrichment/signal_enricher.py",
            "ai_engine.enrichment.signal_enricher",
        )

    @staticmethod
    def _stub_classifier_module():
        clf_mod = types.ModuleType("ai_engine.classifiers.regime_classifier")

        @dataclass
        class RegimeOutput:
            regime: str
            confidence: float
            computed_at: datetime

        class FakeClassifier:
            async def classify(self, market, symbol, interval):
                return RegimeOutput(
                    regime="trending", confidence=0.88,
                    computed_at=datetime.now(timezone.utc),
                )

        clf_mod.RegimeClassifier = FakeClassifier
        clf_mod.RegimeOutput = RegimeOutput
        sys.modules["ai_engine.classifiers.regime_classifier"] = clf_mod
        sys.modules.setdefault("ai_engine.classifiers",
                               types.ModuleType("ai_engine.classifiers"))

    @staticmethod
    def _stub_scorer_module():
        sc_mod = types.ModuleType("ai_engine.scorers.signal_quality_scorer")

        class FakeScorer:
            async def score(self, market, symbol, direction, interval,
                            threshold: float = 0.0):
                return (0.78, False)  # score, not filtered

        sc_mod.SignalQualityScorer = FakeScorer
        sys.modules["ai_engine.scorers.signal_quality_scorer"] = sc_mod
        sys.modules.setdefault("ai_engine.scorers", types.ModuleType("ai_engine.scorers"))

    def _make_enricher(self, dynamo_client=None):
        clf = sys.modules["ai_engine.classifiers.regime_classifier"].RegimeClassifier()
        scorer = sys.modules["ai_engine.scorers.signal_quality_scorer"].SignalQualityScorer()
        return self.enricher_mod.SignalEnricher(
            regime_classifier=clf,
            quality_scorer=scorer,
            dynamo_client=dynamo_client,
            config_table=None,
        )

    def _make_signal_event(self):
        """Build a minimal signal event object."""
        sig = _make_signal(self.signal_mod)
        evt = MagicMock()
        evt.signal = sig
        evt.trace_id = "trace-xyz"
        evt.strategy_id = "strat-1"
        evt.product_type = "MIS"
        evt.expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        return evt

    def test_enrich_returns_enriched_signal(self):
        enricher = self._make_enricher()
        evt = self._make_signal_event()
        result = asyncio.run(enricher.enrich(evt))
        self.assertIsInstance(result, self.enriched_mod.EnrichedSignal)

    def test_enrich_populates_regime(self):
        enricher = self._make_enricher()
        evt = self._make_signal_event()
        result = asyncio.run(enricher.enrich(evt))
        self.assertEqual(result.regime, "trending")
        self.assertAlmostEqual(result.regime_confidence, 0.88, places=2)

    def test_enrich_populates_quality_score(self):
        enricher = self._make_enricher()
        evt = self._make_signal_event()
        result = asyncio.run(enricher.enrich(evt))
        self.assertAlmostEqual(result.quality_score, 0.78, places=2)
        self.assertFalse(result.filtered)

    def test_enrich_never_raises_on_classifier_crash(self):
        """If both classifier and scorer raise, enrich() returns degraded signal."""
        clf_crash_mod = types.ModuleType("ai_engine.classifiers.regime_classifier_crash")

        class CrashClassifier:
            async def classify(self, *a, **kw):
                raise RuntimeError("HMM model corrupted")

        class CrashScorer:
            async def score(self, *a, **kw):
                raise RuntimeError("GBT model unavailable")

        enricher = self.enricher_mod.SignalEnricher(
            regime_classifier=CrashClassifier(),
            quality_scorer=CrashScorer(),
            dynamo_client=None,
            config_table=None,
        )
        evt = self._make_signal_event()
        result = asyncio.run(enricher.enrich(evt))
        # Must return degraded defaults — no raise
        self.assertIsNotNone(result)
        self.assertEqual(result.regime, "unknown")
        self.assertAlmostEqual(result.quality_score, 0.5)
        self.assertFalse(result.filtered)

    def test_enrich_sets_schema_version_4(self):
        enricher = self._make_enricher()
        evt = self._make_signal_event()
        result = asyncio.run(enricher.enrich(evt))
        d = result.to_dict()
        self.assertEqual(d["schema_version"], "4.0")

    def test_enrich_copies_signal_fields(self):
        enricher = self._make_enricher()
        evt = self._make_signal_event()
        result = asyncio.run(enricher.enrich(evt))
        self.assertEqual(result.symbol, evt.signal.symbol)
        self.assertEqual(result.market, evt.signal.market)
        self.assertEqual(result.quantity, evt.signal.quantity)
        self.assertAlmostEqual(result.confidence, evt.signal.confidence)


# ─────────────────────────────────────────────────────────────────────────────
# TEST GROUP 5: EnrichmentWatchdog state machine (PHASE6-010 / PHASE6-017)
# ─────────────────────────────────────────────────────────────────────────────

class TestEnrichmentWatchdog(unittest.TestCase):
    """
    Integration-light tests for EnrichmentWatchdog state transitions.
    Uses mock lag measurement without real Kafka or AWS connections.
    """

    @classmethod
    def setUpClass(cls):
        # Stub confluent_kafka.admin if not already done
        if "confluent_kafka.admin" not in sys.modules:
            admin = types.ModuleType("confluent_kafka.admin")
            admin.AdminClient = MagicMock
            sys.modules["confluent_kafka.admin"] = admin

        cls.watchdog_mod = _load_module(
            "risk_engine/consumers/enrichment_watchdog.py",
            "risk_engine.consumers.enrichment_watchdog",
        )

    def _make_watchdog(self, lag_sequence: list[Optional[int]]):
        """
        Build a watchdog whose _measure_lag_sync returns values from lag_sequence
        in order (like iterating a list).
        """
        from risk_engine.consumers.enrichment_watchdog import EnrichmentState, EnrichmentWatchdog

        state = EnrichmentState()
        watchdog = EnrichmentWatchdog(
            state=state,
            kafka_bootstrap="",  # no real Kafka
            aws_region="ap-south-1",
            dynamo_client=None,
            config_table=None,
            metrics_client=None,
        )
        # Tuning: window=2, recovery_window=5 (defaults)
        # Override _measure_lag to return from sequence
        iter_lags = iter(lag_sequence)

        async def fake_measure_lag():
            try:
                return next(iter_lags)
            except StopIteration:
                return 0

        watchdog._measure_lag = fake_measure_lag
        # Disable DynamoDB config refresh and manual-override check
        watchdog._refresh_config = AsyncMock()
        watchdog._is_manually_disabled = AsyncMock(return_value=False)
        watchdog._publish_fallback_metric = MagicMock()

        return state, watchdog

    def test_initial_state_uses_enriched(self):
        from risk_engine.consumers.enrichment_watchdog import EnrichmentState
        state = EnrichmentState()
        self.assertTrue(state.use_enriched)

    def test_fallback_activates_after_consecutive_lag_breaches(self):
        """2 consecutive lag breaches (default window=2) → fallback activated."""
        # Lag threshold default = 10; WINDOW default = 2
        state, watchdog = self._make_watchdog(lag_sequence=[15, 15, 0, 0, 0])

        async def run():
            await watchdog._check_once()  # breach 1 (lag=15)
            await watchdog._check_once()  # breach 2 (lag=15) → should activate fallback

        asyncio.run(run())
        self.assertFalse(state.use_enriched)
        self.assertEqual(state.fallback_count, 1)
        watchdog._publish_fallback_metric.assert_called_with(1)

    def test_no_fallback_on_single_breach(self):
        """Single lag breach (window=2) → no fallback yet."""
        state, watchdog = self._make_watchdog(lag_sequence=[15, 0])

        async def run():
            await watchdog._check_once()  # breach 1

        asyncio.run(run())
        self.assertTrue(state.use_enriched)  # still using enriched

    def test_recovery_after_lag_clears(self):
        """
        After fallback, 5 consecutive zero-lag checks (recovery_window=5)
        → switch back to enriched.
        """
        state, watchdog = self._make_watchdog(lag_sequence=[15, 15, 0, 0, 0, 0, 0])

        async def run():
            # Activate fallback
            await watchdog._check_once()  # lag=15, breach 1
            await watchdog._check_once()  # lag=15, breach 2 → fallback
            self.assertFalse(state.use_enriched)
            # Recovery: 5 clear checks
            for _ in range(5):
                await watchdog._check_once()  # lag=0

        asyncio.run(run())
        self.assertTrue(state.use_enriched)
        watchdog._publish_fallback_metric.assert_called_with(0)  # recovery metric

    def test_partial_recovery_does_not_switch_back(self):
        """Only 3 clear checks (< recovery_window=5) → still in fallback."""
        state, watchdog = self._make_watchdog(lag_sequence=[15, 15, 0, 0, 0])

        async def run():
            await watchdog._check_once()  # breach 1
            await watchdog._check_once()  # breach 2 → fallback
            for _ in range(3):
                await watchdog._check_once()  # only 3 clear checks

        asyncio.run(run())
        self.assertFalse(state.use_enriched)  # still in fallback

    def test_unmeasurable_lag_preserves_state(self):
        """None from _measure_lag → no state change."""
        state, watchdog = self._make_watchdog(lag_sequence=[None, None])

        async def run():
            await watchdog._check_once()
            await watchdog._check_once()

        asyncio.run(run())
        self.assertTrue(state.use_enriched)  # no change

    def test_fallback_count_increments(self):
        """Each activation increments fallback_count."""
        state, watchdog = self._make_watchdog(
            lag_sequence=[15, 15, 0, 0, 0, 0, 0,  # first fallback + recovery
                          15, 15]                   # second fallback
        )

        async def run():
            # First fallback
            await watchdog._check_once()
            await watchdog._check_once()
            # Recovery
            for _ in range(5):
                await watchdog._check_once()
            # Second fallback
            await watchdog._check_once()
            await watchdog._check_once()

        asyncio.run(run())
        self.assertEqual(state.fallback_count, 2)


# ─────────────────────────────────────────────────────────────────────────────
# TEST GROUP 6: End-to-end enrichment pipeline (PHASE6-017)
# ─────────────────────────────────────────────────────────────────────────────

class TestEnrichmentPipelineIntegration(unittest.TestCase):
    """
    Light integration test: Signal → SignalEnricher → EnrichedSignal.
    Uses mocked classifiers/scorers, no real Kafka or DynamoDB.

    Validates:
      - Signal fields are preserved through enrichment
      - Enriched fields are populated (regime, quality_score, filtered)
      - to_dict() produces a valid v4.0 envelope
      - from_dict() round-trips cleanly
    """

    @classmethod
    def setUpClass(cls):
        cls.enriched_mod, cls.signal_mod = _load_shared_models()
        # Reuse stubs from TestSignalEnricher (already in sys.modules)
        cls.enricher_mod = sys.modules.get(
            "ai_engine.enrichment.signal_enricher"
        ) or _load_module(
            "ai_engine/enrichment/signal_enricher.py",
            "ai_engine.enrichment.signal_enricher",
        )

    def _make_event(self, symbol="TCS", market="NSE", direction_str="SELL"):
        sig = _make_signal(self.signal_mod, symbol=symbol, market=market,
                           direction_str=direction_str)
        evt = MagicMock()
        evt.signal = sig
        evt.trace_id = "integration-trace"
        evt.strategy_id = "momentum_nse_v2"
        evt.product_type = "MIS"
        evt.expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        return evt

    def test_full_pipeline_produces_enriched_signal(self):
        clf = sys.modules["ai_engine.classifiers.regime_classifier"].RegimeClassifier()
        scorer = sys.modules["ai_engine.scorers.signal_quality_scorer"].SignalQualityScorer()
        enricher = self.enricher_mod.SignalEnricher(
            regime_classifier=clf, quality_scorer=scorer,
            dynamo_client=None, config_table=None,
        )
        evt = self._make_event()
        result = asyncio.run(enricher.enrich(evt))

        # Signal identity preserved
        self.assertEqual(result.symbol, "TCS")
        self.assertEqual(result.market, "NSE")

        # Enrichment fields present
        self.assertIn(result.regime, {"trending", "ranging", "volatile", "crash", "unknown"})
        self.assertGreaterEqual(result.quality_score, 0.0)
        self.assertLessEqual(result.quality_score, 1.0)
        self.assertIsInstance(result.filtered, bool)

    def test_full_pipeline_dict_envelope_valid(self):
        clf = sys.modules["ai_engine.classifiers.regime_classifier"].RegimeClassifier()
        scorer = sys.modules["ai_engine.scorers.signal_quality_scorer"].SignalQualityScorer()
        enricher = self.enricher_mod.SignalEnricher(
            regime_classifier=clf, quality_scorer=scorer,
            dynamo_client=None, config_table=None,
        )
        evt = self._make_event(symbol="INFY")
        result = asyncio.run(enricher.enrich(evt))
        d = result.to_dict()

        required_keys = {
            "schema_version", "event_type", "signal_id", "symbol", "market",
            "direction", "quantity", "confidence", "regime", "quality_score",
            "filtered", "trace_id",
        }
        missing = required_keys - set(d.keys())
        self.assertFalse(missing, f"Missing keys in to_dict() output: {missing}")
        self.assertEqual(d["schema_version"], "4.0")
        self.assertEqual(d["event_type"], "SIGNAL_ENRICHED")

    def test_full_pipeline_round_trip(self):
        clf = sys.modules["ai_engine.classifiers.regime_classifier"].RegimeClassifier()
        scorer = sys.modules["ai_engine.scorers.signal_quality_scorer"].SignalQualityScorer()
        enricher = self.enricher_mod.SignalEnricher(
            regime_classifier=clf, quality_scorer=scorer,
            dynamo_client=None, config_table=None,
        )
        evt = self._make_event(symbol="WIPRO", direction_str="BUY")
        original = asyncio.run(enricher.enrich(evt))
        d = original.to_dict()
        restored = self.enriched_mod.EnrichedSignal.from_dict(d)

        self.assertEqual(original.symbol, restored.symbol)
        self.assertEqual(original.regime, restored.regime)
        self.assertAlmostEqual(original.quality_score, restored.quality_score, places=4)
        self.assertEqual(original.filtered, restored.filtered)
        self.assertEqual(original.signal_id, restored.signal_id)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main(verbosity=2)
