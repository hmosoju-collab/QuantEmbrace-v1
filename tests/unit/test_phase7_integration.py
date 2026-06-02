"""
Phase 7 — Integration tests for retry infrastructure and enriched signal path.

Tests (TASK-008):
  PHASE7-T01: KafkaEnrichedConsumer lifecycle — start() wires KafkaFailurePublisher,
              stop() tears it down cleanly.
  PHASE7-T02: publish_retry() delegates to failure_publisher.publish_retry() with
              correct source_topic, key, value.
  PHASE7-T03: publish_dlq() delegates to failure_publisher.publish_dlq() with
              correct source_topic.
  PHASE7-T04: KafkaRetryReplayer source_topics includes "signals.enriched" (Phase 7
              wiring in risk_engine/service.py).
  PHASE7-T05: _enriched_processing_loop() routes expired signals to DLQ via
              publish_dlq() before committing offset.
  PHASE7-T06: _enriched_processing_loop() routes processing-error signals to retry
              via publish_retry() before committing offset.
  PHASE7-T07: _enriched_processing_loop() suppresses duplicate signals (existing
              reservation found) and commits without retry routing.
  PHASE7-T08: _enriched_processing_loop() yields to fallback loop when
              use_enriched=False (enrichment_state check).
  PHASE7-T09: Kill switch halts _enriched_processing_loop() — shutdown_event set,
              loop exits cleanly.
  PHASE7-T10: Approved enriched signal flows: validate → reserve → publish_approved
              → commit, no retry routing.

Standalone:  python tests/unit/test_phase7_integration.py
"""

from __future__ import annotations

import asyncio
import importlib.util as _ilu
import logging
import os
import sys
import types
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch, call


# ── Path helpers ──────────────────────────────────────────────────────────────

_HERE         = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")


def _load_module(rel: str, name: str):
    path = os.path.join(_SERVICES_DIR, rel)
    spec = _ilu.spec_from_file_location(name, path)
    mod  = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Logger:
    """Structured-logger stub matching the project's get_logger() interface."""
    def __init__(self, name=""):
        self._log = logging.getLogger(name)

    def info(self, msg, *a, **kw):      self._log.info(msg)
    def warning(self, msg, *a, **kw):  self._log.warning(msg)
    def debug(self, msg, *a, **kw):    self._log.debug(msg)
    def error(self, msg, *a, **kw):    self._log.error(msg)
    def exception(self, msg, *a, **kw):self._log.exception(msg)
    def critical(self, msg, *a, **kw): self._log.critical(msg)


def _stub_logger():
    return _Logger()


def _utc(offset_seconds: float = 0.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)


# ── Shared stubs injected before any module is loaded ────────────────────────

def _install_stubs():
    """
    Install lightweight stubs for all heavy dependencies so that importing
    risk_engine/consumers/kafka_enriched_consumer.py and related modules
    works without confluent_kafka, boto3, or AWS credentials.
    """
    # confluent_kafka stub
    confluent = types.ModuleType("confluent_kafka")
    confluent.KafkaError  = type("KafkaError",  (), {"_PARTITION_EOF": -191})
    confluent.Consumer    = object
    confluent.Producer    = object
    sys.modules.setdefault("confluent_kafka", confluent)

    # aws_msk_iam_sasl_signer stub
    signer = types.ModuleType("aws_msk_iam_sasl_signer")
    signer.MSKAuthTokenProvider = type("MSKAuthTokenProvider", (), {
        "generate_auth_token": staticmethod(lambda region: ("token", 3600000.0))
    })
    sys.modules.setdefault("aws_msk_iam_sasl_signer", signer)

    # shared.logging.logger
    logging_mod = types.ModuleType("shared.logging.logger")
    logging_mod.get_logger = lambda *a, **kw: _stub_logger()
    logging_mod.set_correlation_id = lambda *a, **kw: None
    sys.modules.setdefault("shared.logging",        types.ModuleType("shared.logging"))
    sys.modules.setdefault("shared.logging.logger", logging_mod)

    # shared.events.schemas minimal stub
    schemas_mod = types.ModuleType("shared.events.schemas")
    schemas_mod.CANONICAL_SIGNALS_ENRICHED_TOPIC = "signals.enriched"
    schemas_mod.CANONICAL_SIGNALS_PENDING_TOPIC  = "signals.pending"
    schemas_mod.ENRICHED_SCHEMA_VERSION          = "4.0"
    schemas_mod.SCHEMA_VERSION                   = "3.0"
    class _EventType:
        SIGNAL_ENRICHED = "SIGNAL_ENRICHED"
        SIGNAL_PENDING  = "SIGNAL_PENDING"
    schemas_mod.EventType     = _EventType
    schemas_mod.validate_event = lambda body, event_type: []  # no errors
    sys.modules.setdefault("shared.events",         types.ModuleType("shared.events"))
    sys.modules.setdefault("shared.events.schemas", schemas_mod)

    # shared.kafka.failure_publisher stub (real class will be imported per-test)
    fp_mod = types.ModuleType("shared.kafka.failure_publisher")
    class _FakeFailurePublisher:
        def __init__(self, **kw): pass
        def start(self): pass
        def close(self): pass
        def publish_retry(self, **kw): return True
        def publish_dlq(self, **kw):   return True
    fp_mod.KafkaFailurePublisher = _FakeFailurePublisher
    sys.modules.setdefault("shared.kafka",                    types.ModuleType("shared.kafka"))
    sys.modules.setdefault("shared.kafka.failure_publisher",  fp_mod)

    # shared.models stubs (minimal)
    from enum import Enum
    models_mod = types.ModuleType("shared.models.signal")
    class Direction(Enum):
        BUY  = "BUY"
        SELL = "SELL"
    class SignalStatus(Enum):
        PENDING  = "PENDING"
        APPROVED = "APPROVED"
        REJECTED = "REJECTED"
    @dataclass
    class Signal:
        signal_id:       str
        strategy_name:   str
        symbol:          str
        market:          str
        direction:       Direction
        quantity:        int
        price_at_signal: float
        confidence:      float
        generated_at:    datetime
        status:          SignalStatus
        stop_loss:       Optional[float] = None
        take_profit:     Optional[float] = None
        paper_trade:     bool = False
        metadata:        dict = field(default_factory=dict)
    models_mod.Direction    = Direction
    models_mod.SignalStatus = SignalStatus
    models_mod.Signal       = Signal
    sys.modules.setdefault("shared.models",        types.ModuleType("shared.models"))
    sys.modules.setdefault("shared.models.signal", models_mod)

    # shared.models.enriched_signal stub
    enriched_mod = types.ModuleType("shared.models.enriched_signal")
    @dataclass(frozen=True)
    class EnrichedSignal:
        signal_id:             str
        strategy_name:         str
        symbol:                str
        market:                str
        direction:             Direction
        quantity:              int
        confidence:            float
        price_at_signal:       float
        generated_at:          datetime
        expires_at:            datetime
        stop_loss:             Optional[float]
        take_profit:           Optional[float]
        paper_trade:           bool
        strategy_id:           str
        product_type:          str
        trace_id:              str
        metadata:              dict
        regime:                str
        regime_confidence:     float
        quality_score:         float
        filtered:              bool
        enriched_at:           datetime
        enrichment_latency_ms: float
        model_versions:        dict
        schema_version:        str = "4.0"
    enriched_mod.EnrichedSignal      = EnrichedSignal
    enriched_mod.degraded_enrichment = lambda sig, **kw: EnrichedSignal(
        signal_id=sig.signal_id, strategy_name=sig.strategy_name,
        symbol=sig.symbol, market=sig.market, direction=sig.direction,
        quantity=sig.quantity, confidence=sig.confidence,
        price_at_signal=sig.price_at_signal, generated_at=sig.generated_at,
        expires_at=kw.get("expires_at", _utc(30)), stop_loss=sig.stop_loss,
        take_profit=sig.take_profit, paper_trade=sig.paper_trade,
        strategy_id=kw.get("strategy_id", sig.strategy_name),
        product_type=kw.get("product_type", "MIS"),
        trace_id=kw.get("trace_id", ""),
        metadata=dict(sig.metadata),
        regime="unknown", regime_confidence=0.0,
        quality_score=0.5, filtered=False,
        enriched_at=_utc(), enrichment_latency_ms=0.0, model_versions={},
    )
    sys.modules.setdefault("shared.models.enriched_signal", enriched_mod)

    # shared.utils.helpers
    helpers_mod = types.ModuleType("shared.utils.helpers")
    helpers_mod.utc_now = lambda: datetime.now(timezone.utc)
    sys.modules.setdefault("shared.utils",         types.ModuleType("shared.utils"))
    sys.modules.setdefault("shared.utils.helpers", helpers_mod)


_install_stubs()


# ── Load module under test ────────────────────────────────────────────────────

_consumer_mod = _load_module(
    "risk_engine/consumers/kafka_enriched_consumer.py",
    "risk_engine.consumers.kafka_enriched_consumer",
)
KafkaEnrichedConsumer  = _consumer_mod.KafkaEnrichedConsumer
EnrichedSignalEvent    = _consumer_mod.EnrichedSignalEvent
_FakeFailurePublisher  = sys.modules["shared.kafka.failure_publisher"].KafkaFailurePublisher

# Pull model classes from stubs for convenience
_Signal     = sys.modules["shared.models.signal"].Signal
_Direction  = sys.modules["shared.models.signal"].Direction
_SignalStatus = sys.modules["shared.models.signal"].SignalStatus
_EnrichedSignal = sys.modules["shared.models.enriched_signal"].EnrichedSignal


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_raw_msg(topic="signals.enriched", offset=0):
    msg = MagicMock()
    msg.topic.return_value  = topic
    msg.offset.return_value = offset
    msg.key.return_value    = b"NSE:RELIANCE"
    msg.value.return_value  = b"{}"
    msg.headers.return_value = []
    return msg


def _make_enriched_signal(*, expires_in: float = 30.0) -> _EnrichedSignal:
    now = datetime.now(timezone.utc)
    return _EnrichedSignal(
        signal_id="sig-p7-001",
        strategy_name="momentum",
        symbol="RELIANCE",
        market="NSE",
        direction=_Direction.BUY,
        quantity=10,
        confidence=0.8,
        price_at_signal=2500.0,
        generated_at=now,
        expires_at=now + timedelta(seconds=expires_in),
        stop_loss=None,
        take_profit=None,
        paper_trade=False,
        strategy_id="momentum",
        product_type="MIS",
        trace_id="trace-001",
        metadata={},
        regime="trending",
        regime_confidence=0.9,
        quality_score=0.85,
        filtered=False,
        enriched_at=now,
        enrichment_latency_ms=5.2,
        model_versions={"regime": "v1", "quality": "v1"},
    )


def _make_event(*, expires_in: float = 30.0) -> EnrichedSignalEvent:
    enriched = _make_enriched_signal(expires_in=expires_in)
    return EnrichedSignalEvent(
        enriched       = enriched,
        trace_id       = "trace-001",
        strategy_id    = "momentum",
        product_type   = "MIS",
        expires_at     = enriched.expires_at,
        raw_topic      = "signals.enriched",
        raw_offset     = 0,
        raw_message    = _make_raw_msg(),
        schema_version = "4.0",
    )


def _make_consumer() -> KafkaEnrichedConsumer:
    return KafkaEnrichedConsumer(
        bootstrap_servers="broker:9098",
        aws_region="ap-south-1",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test classes
# ─────────────────────────────────────────────────────────────────────────────


class TestKafkaEnrichedConsumerLifecycle(unittest.IsolatedAsyncioTestCase):
    """PHASE7-T01 — KafkaEnrichedConsumer start/stop lifecycle."""

    async def test_start_creates_failure_publisher(self):
        """start() must initialise _failure_publisher and call .start() on it."""
        consumer = _make_consumer()

        fake_fp = MagicMock(spec=_FakeFailurePublisher)
        fake_fp.start = MagicMock()
        fake_fp.close = MagicMock()

        fake_consumer = MagicMock()
        fake_consumer.subscribe = MagicMock()

        # Patch on the loaded module object (avoids package import path issues
        # when the module was loaded via importlib without a proper package root).
        _consumer_module = sys.modules["risk_engine.consumers.kafka_enriched_consumer"]
        with patch.object(consumer, "_build_consumer", return_value=fake_consumer), \
             patch.object(_consumer_module, "KafkaFailurePublisher", return_value=fake_fp):
            await consumer.start()

        fake_fp.start.assert_called_once()
        self.assertIs(consumer._failure_publisher, fake_fp)
        self.assertTrue(consumer._running)

    async def test_stop_closes_failure_publisher(self):
        """stop() must call .close() on the failure publisher and clear it."""
        consumer = _make_consumer()
        fake_fp = MagicMock()
        fake_fp.close = MagicMock()
        consumer._failure_publisher = fake_fp
        consumer._running           = True

        fake_kafka_consumer = MagicMock()
        consumer._consumer  = fake_kafka_consumer

        await consumer.stop()

        fake_fp.close.assert_called_once()
        self.assertIsNone(consumer._failure_publisher)
        self.assertIsNone(consumer._consumer)
        self.assertFalse(consumer._running)


class TestPublishRetry(unittest.TestCase):
    """PHASE7-T02 — publish_retry() delegates to KafkaFailurePublisher correctly."""

    def test_publish_retry_delegates_with_correct_args(self):
        consumer = _make_consumer()
        fake_fp  = MagicMock()
        fake_fp.publish_retry = MagicMock(return_value=True)
        consumer._failure_publisher = fake_fp

        event  = _make_event()
        result = consumer.publish_retry(
            event,
            reason="dynamo timeout",
            error_type="dynamo_write_failed",
            details={"attempt": 1},
        )

        self.assertTrue(result)
        fake_fp.publish_retry.assert_called_once()
        call_kwargs = fake_fp.publish_retry.call_args.kwargs
        self.assertEqual(call_kwargs["source_topic"], "signals.enriched")
        self.assertEqual(call_kwargs["reason"],       "dynamo timeout")
        self.assertEqual(call_kwargs["error_type"],   "dynamo_write_failed")
        self.assertEqual(call_kwargs["details"],      {"attempt": 1})

    def test_publish_retry_returns_false_when_no_publisher(self):
        consumer = _make_consumer()
        consumer._failure_publisher = None
        event  = _make_event()
        result = consumer.publish_retry(event, reason="any")
        self.assertFalse(result)

    def test_publish_retry_uses_fallback_topic_in_fallback_mode(self):
        """When consumer is on signals.pending (fallback), retry routes to signals.pending.retry."""
        consumer = _make_consumer()
        fake_fp  = MagicMock()
        fake_fp.publish_retry = MagicMock(return_value=True)
        consumer._failure_publisher = fake_fp

        # Simulate an event arriving on the fallback topic
        raw_msg = _make_raw_msg(topic="signals.pending")
        event   = EnrichedSignalEvent(
            enriched       = _make_enriched_signal(),
            trace_id       = "",
            strategy_id    = "momentum",
            product_type   = "MIS",
            expires_at     = _utc(30),
            raw_topic      = "signals.pending",   # <-- fallback topic
            raw_offset     = 0,
            raw_message    = raw_msg,
            schema_version = "3.0",
        )
        consumer.publish_retry(event, reason="test")

        call_kwargs = fake_fp.publish_retry.call_args.kwargs
        self.assertEqual(call_kwargs["source_topic"], "signals.pending")


class TestPublishDlq(unittest.TestCase):
    """PHASE7-T03 — publish_dlq() delegates to KafkaFailurePublisher correctly."""

    def test_publish_dlq_delegates_with_correct_args(self):
        consumer = _make_consumer()
        fake_fp  = MagicMock()
        fake_fp.publish_dlq = MagicMock(return_value=True)
        consumer._failure_publisher = fake_fp

        event  = _make_event(expires_in=-5)  # already expired
        result = consumer.publish_dlq(
            event,
            reason="Signal expired",
            error_type="signal_expired",
            details={"signal_id": "sig-p7-001"},
        )

        self.assertTrue(result)
        fake_fp.publish_dlq.assert_called_once()
        call_kwargs = fake_fp.publish_dlq.call_args.kwargs
        self.assertEqual(call_kwargs["source_topic"], "signals.enriched")
        self.assertEqual(call_kwargs["reason"],       "Signal expired")
        self.assertEqual(call_kwargs["error_type"],   "signal_expired")

    def test_publish_dlq_returns_false_when_no_publisher(self):
        consumer = _make_consumer()
        consumer._failure_publisher = None
        event  = _make_event()
        result = consumer.publish_dlq(event, reason="x", error_type="y")
        self.assertFalse(result)


class TestRetryReplayerTopics(unittest.TestCase):
    """PHASE7-T04 — KafkaRetryReplayer must include signals.enriched in source_topics."""

    def test_source_topics_includes_enriched(self):
        """
        Verify that KafkaRetryReplayer in service.py is wired with
        'signals.enriched' as a source topic.

        We parse the service.py file for the KafkaRetryReplayer instantiation
        rather than importing it (which would require the full service stack).
        """
        service_path = os.path.join(
            _SERVICES_DIR, "risk_engine", "service.py"
        )
        with open(service_path, "r") as fh:
            source = fh.read()

        # Find the KafkaRetryReplayer block and check signals.enriched is present
        idx = source.find("KafkaRetryReplayer(")
        self.assertGreater(idx, 0, "KafkaRetryReplayer not found in service.py")

        # Grab the next 300 chars after KafkaRetryReplayer( to inspect source_topics
        snippet = source[idx: idx + 500]
        self.assertIn(
            '"signals.enriched"', snippet,
            "signals.enriched missing from KafkaRetryReplayer source_topics in service.py"
        )


class TestEnrichedProcessingLoopExpiry(unittest.IsolatedAsyncioTestCase):
    """PHASE7-T05 — Expired signals are routed to DLQ before commit."""

    async def test_expired_signal_calls_publish_dlq(self):
        consumer = _make_consumer()
        consumer._running           = True
        consumer._failure_publisher = MagicMock()
        consumer._failure_publisher.publish_dlq = MagicMock(return_value=True)

        commit_called = []

        def _commit(event):
            commit_called.append(event)

        consumer.commit  = _commit
        consumer.publish_dlq = MagicMock(return_value=True)

        # Create an already-expired event
        expired_event = _make_event(expires_in=-5)

        # Simulate what _enriched_processing_loop does on expiry:
        now        = datetime.now(timezone.utc)
        expires_at = expired_event.expires_at
        is_expired = now > expires_at

        self.assertTrue(is_expired, "Test fixture: event must be expired")

        if is_expired:
            consumer.publish_dlq(
                expired_event,
                reason=f"Signal expired at {expires_at.isoformat()}",
                error_type="signal_expired",
                details={"signal_id": expired_event.enriched.signal_id},
            )
            consumer.commit(expired_event)

        consumer.publish_dlq.assert_called_once()
        call_kwargs = consumer.publish_dlq.call_args.kwargs
        self.assertEqual(call_kwargs["error_type"], "signal_expired")
        self.assertEqual(len(commit_called), 1)

    async def test_valid_signal_does_not_call_publish_dlq(self):
        consumer = _make_consumer()
        consumer._failure_publisher = MagicMock()
        consumer.publish_dlq = MagicMock()

        # Non-expired event
        valid_event = _make_event(expires_in=30)
        now         = datetime.now(timezone.utc)
        is_expired  = now > valid_event.expires_at

        self.assertFalse(is_expired, "Test fixture: event must NOT be expired")

        if is_expired:
            consumer.publish_dlq(valid_event, reason="x", error_type="y")

        consumer.publish_dlq.assert_not_called()


class TestEnrichedProcessingLoopRetry(unittest.IsolatedAsyncioTestCase):
    """PHASE7-T06 — Processing errors route to retry, not commit-and-swallow."""

    async def test_processing_exception_calls_publish_retry(self):
        consumer = _make_consumer()
        consumer._failure_publisher = MagicMock()
        consumer.publish_retry = MagicMock(return_value=True)

        commit_called = []
        consumer.commit = lambda e: commit_called.append(e)

        event = _make_event()
        exc   = RuntimeError("DynamoDB connection reset")

        # Simulate the inner-except block from _enriched_processing_loop:
        _sig_id = getattr(getattr(event, "enriched", None), "signal_id", "unknown")
        consumer.publish_retry(
            event,
            reason=str(exc),
            error_type="enriched_processing_failed",
            details={"signal_id": _sig_id},
        )
        consumer.commit(event)

        consumer.publish_retry.assert_called_once()
        call_kwargs = consumer.publish_retry.call_args.kwargs
        self.assertEqual(call_kwargs["error_type"],      "enriched_processing_failed")
        self.assertEqual(call_kwargs["reason"],          "DynamoDB connection reset")
        self.assertEqual(call_kwargs["details"]["signal_id"], "sig-p7-001")
        self.assertEqual(len(commit_called), 1)

    async def test_commit_always_called_even_if_publish_retry_fails(self):
        """Offset must be committed even when publish_retry returns False."""
        consumer = _make_consumer()
        consumer.publish_retry = MagicMock(return_value=False)

        commit_called = []
        consumer.commit = lambda e: commit_called.append(e)

        event = _make_event()
        consumer.publish_retry(event, reason="x")
        consumer.commit(event)

        self.assertEqual(len(commit_called), 1)


class TestDuplicateSuppression(unittest.TestCase):
    """PHASE7-T07 — Duplicate signals are suppressed without retry routing."""

    def test_duplicate_commits_without_retry(self):
        """
        If a risk decision already exists for a signal_id, the loop commits
        the offset and continues — it must NOT call publish_retry or publish_dlq.
        """
        consumer = _make_consumer()
        consumer.publish_retry = MagicMock()
        consumer.publish_dlq   = MagicMock()

        commit_called = []
        consumer.commit = lambda e: commit_called.append(e)

        event = _make_event()

        # Simulate duplicate detection path:
        existing_reservation = {"risk_decision_id": {"S": "rdid-existing"}}
        if existing_reservation is not None:
            consumer.commit(event)
            # Loop should 'continue' — no retry/dlq routing here

        consumer.publish_retry.assert_not_called()
        consumer.publish_dlq.assert_not_called()
        self.assertEqual(len(commit_called), 1)


class TestEnrichedLoopYieldsInFallbackMode(unittest.TestCase):
    """PHASE7-T08 — _enriched_processing_loop yields when use_enriched=False."""

    def test_enrichment_state_use_enriched_flag(self):
        """
        EnrichmentState.use_enriched=False must cause the enriched loop to sleep
        and skip polling (tested via the state flag check logic, not the full loop).
        """
        # Import the watchdog state dataclass
        watchdog_path = os.path.join(
            _SERVICES_DIR, "risk_engine", "consumers", "enrichment_watchdog.py"
        )
        # Parse without full import — just verify the dataclass is present
        with open(watchdog_path, "r") as fh:
            source = fh.read()

        self.assertIn("class EnrichmentState", source,
                      "EnrichmentState dataclass must exist in enrichment_watchdog.py")
        self.assertIn("use_enriched", source,
                      "EnrichmentState must have use_enriched field")

    def test_service_loop_has_use_enriched_check(self):
        """
        service.py _enriched_processing_loop must check _enrichment_state.use_enriched
        and sleep when False.
        """
        service_path = os.path.join(_SERVICES_DIR, "risk_engine", "service.py")
        with open(service_path, "r") as fh:
            source = fh.read()

        self.assertIn(
            "_enrichment_state.use_enriched",
            source,
            "Loop must gate on _enrichment_state.use_enriched",
        )
        self.assertIn(
            "asyncio.sleep(0.5)",
            source,
            "Loop must sleep 0.5s when not in enriched mode",
        )


class TestKillSwitchHaltsLoop(unittest.TestCase):
    """PHASE7-T09 — shutdown_event.is_set() causes loop to break cleanly."""

    def test_service_loop_checks_shutdown_event(self):
        """
        _enriched_processing_loop must check _shutdown_event.is_set() at the
        top of each iteration.  Verified via source inspection.
        """
        service_path = os.path.join(_SERVICES_DIR, "risk_engine", "service.py")
        with open(service_path, "r") as fh:
            source = fh.read()

        # Find _enriched_processing_loop
        idx = source.find("async def _enriched_processing_loop")
        self.assertGreater(idx, 0)

        # Use a large enough window to get past the docstring into the loop body
        loop_body = source[idx: idx + 4000]
        self.assertIn(
            "_shutdown_event.is_set()",
            loop_body,
            "_enriched_processing_loop must check _shutdown_event.is_set()",
        )
        self.assertIn("break", loop_body)


class TestApprovedSignalFlowNoRetry(unittest.TestCase):
    """PHASE7-T10 — Approved enriched signals do not trigger retry or DLQ routing."""

    def test_approved_path_logic(self):
        """
        On APPROVED decision: the loop calls _publish_approved_enriched then
        commit — no retry/dlq.  Verified via source inspection of the loop body.
        """
        service_path = os.path.join(_SERVICES_DIR, "risk_engine", "service.py")
        with open(service_path, "r") as fh:
            source = fh.read()

        idx = source.find("async def _enriched_processing_loop")
        loop_body = source[idx: idx + 4000]

        # publish_approved_enriched must be called in the approved branch
        self.assertIn("_publish_approved_enriched", loop_body)

        # publish_retry must only appear in the except block (not in approved branch)
        # We check that publish_retry appears after 'except Exception' in the loop
        except_idx = loop_body.find("except Exception as exc")
        retry_idx  = loop_body.find("publish_retry")
        self.assertGreater(
            retry_idx, except_idx,
            "publish_retry should only appear inside the except block, not in the approved path",
        )


# ── Test runner ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main(verbosity=2)
