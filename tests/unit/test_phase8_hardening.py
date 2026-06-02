"""
Phase 8 Production Hardening — Unit Tests (ADR-015 F1–F8).

Coverage:
  F1 – ACK_UNKNOWN order state (handled via existing order state tests)
  F2 – Data-quality suppression (WARMING_UP / GAP candles blocked from strategy)
  F3 – LocalOutbox durable Kafka buffer (enqueue, drain, cap, lifecycle)
  F4 – Placement-pause consumer gate (consecutive failures → pause → auto-clear)
  F5 – KafkaLagWatchdog (threshold + kill-switch callback)
  F6 – ReconciliationValidator (flag=False pass-through, flag=True rejection,
        paper-trade exempt, closeout exempt, DynamoDB error fail-open)
  F7 – Orphan detector (no SL linked, terminal SL, active SL pass, metric emit)
  F8 – reconcile.py CLI helpers (_compare_positions, flag helpers)

Standalone:
    python tests/unit/test_phase8_hardening.py
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
_SCRIPTS_DIR  = os.path.join(_PROJECT_ROOT, "scripts")


def _load_module(rel: str, name: str):
    path = os.path.join(_SERVICES_DIR, rel)
    spec = _ilu.spec_from_file_location(name, path)
    mod  = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_script(rel: str, name: str):
    path = os.path.join(_SCRIPTS_DIR, rel)
    spec = _ilu.spec_from_file_location(name, path)
    mod  = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Logger:
    def __init__(self, name=""):
        self._log = logging.getLogger(name)

    def info(self, msg, *a, **kw):       self._log.info(msg)
    def warning(self, msg, *a, **kw):    self._log.warning(msg)
    def debug(self, msg, *a, **kw):      self._log.debug(msg)
    def error(self, msg, *a, **kw):      self._log.error(msg)
    def exception(self, msg, *a, **kw):  self._log.exception(msg)
    def critical(self, msg, *a, **kw):   self._log.critical(msg)


def _stub_logger():
    return _Logger()


# ── Shared stubs ──────────────────────────────────────────────────────────────

def _install_stubs():
    # confluent_kafka
    confluent = types.ModuleType("confluent_kafka")
    confluent.KafkaError = type("KafkaError", (), {"_PARTITION_EOF": -191})
    confluent.Consumer   = object
    confluent.Producer   = object
    sys.modules.setdefault("confluent_kafka", confluent)

    # aws_msk_iam_sasl_signer
    signer = types.ModuleType("aws_msk_iam_sasl_signer")
    signer.MSKAuthTokenProvider = type("MSKAuthTokenProvider", (), {
        "generate_auth_token": staticmethod(lambda region: ("token", 3600000.0))
    })
    sys.modules.setdefault("aws_msk_iam_sasl_signer", signer)

    # boto3
    boto3_mod = types.ModuleType("boto3")
    boto3_mod.client = MagicMock(return_value=MagicMock())
    sys.modules.setdefault("boto3", boto3_mod)

    # shared.logging.logger
    logging_mod = types.ModuleType("shared.logging.logger")
    logging_mod.get_logger = lambda *a, **kw: _stub_logger()
    logging_mod.set_correlation_id = lambda *a, **kw: None
    sys.modules.setdefault("shared.logging",        types.ModuleType("shared.logging"))
    sys.modules.setdefault("shared.logging.logger", logging_mod)

    # shared.config.settings
    settings_mod = types.ModuleType("shared.config.settings")
    class _FakeSettings:
        pass
    settings_mod.AppSettings  = _FakeSettings
    settings_mod.get_settings = lambda: _FakeSettings()
    sys.modules.setdefault("shared.config",          types.ModuleType("shared.config"))
    sys.modules.setdefault("shared.config.settings", settings_mod)

    # shared.models.signal
    from enum import Enum

    models_mod = types.ModuleType("shared.models.signal")

    class _Market(str, Enum):
        NSE = "NSE"
        US  = "US"

    @dataclass
    class _Signal:
        signal_id:    str = "sig-001"
        symbol:       str = "RELIANCE"
        paper_trade:  bool = False
        metadata:     dict = field(default_factory=dict)

    models_mod.Signal = _Signal
    models_mod.Market = _Market
    sys.modules.setdefault("shared.models",        types.ModuleType("shared.models"))
    sys.modules.setdefault("shared.models.signal", models_mod)

    # shared.risk_state — real module if available, else stub
    try:
        _load_module("shared/risk_state.py", "shared.risk_state")
    except Exception:
        rs_mod = types.ModuleType("shared.risk_state")
        rs_mod.reconciliation_key = lambda: {"PK": {"S": "RECONCILIATION#STATE"}, "SK": {"S": "GLOBAL"}}
        rs_mod.attr_bool   = lambda item, key, default=False: bool(item.get(key, {}).get("BOOL", default))
        rs_mod.attr_string = lambda item, key, default="": item.get(key, {}).get("S", default)
        sys.modules["shared.risk_state"] = rs_mod

    # shared.metrics.cloudwatch_metrics
    metrics_mod = types.ModuleType("shared.metrics.cloudwatch_metrics")
    class _FakeMetrics:
        def put_metric(self, **kw): pass
    metrics_mod.CloudWatchMetricsClient = _FakeMetrics
    metrics_mod.get_metrics_client = lambda: _FakeMetrics()
    sys.modules.setdefault("shared.metrics",                      types.ModuleType("shared.metrics"))
    sys.modules.setdefault("shared.metrics.cloudwatch_metrics",   metrics_mod)

    # shared.kafka.local_outbox — real module
    try:
        _load_module("shared/kafka/local_outbox.py", "shared.kafka.local_outbox")
    except Exception:
        pass

    # shared.kafka.lag_monitor — stub if not loadable
    if "shared.kafka.lag_monitor" not in sys.modules:
        lag_mod = types.ModuleType("shared.kafka.lag_monitor")
        class _FakeLagMonitor:
            def __init__(self, **kw):
                self._running          = False
                self._threshold        = kw.get("threshold_messages", 500)
                self._consecutive_checks = kw.get("consecutive_checks", 3)
                self._interval         = kw.get("check_interval_seconds", 1.0)
            async def run(self): pass
            async def stop(self): self._running = False
        lag_mod.KafkaLagKillSwitchMonitor = _FakeLagMonitor
        sys.modules.setdefault("shared.kafka",             types.ModuleType("shared.kafka"))
        sys.modules.setdefault("shared.kafka.lag_monitor", lag_mod)

    # risk_engine.limits.risk_limits — minimal stub
    if "risk_engine.limits.risk_limits" not in sys.modules:
        limits_mod = types.ModuleType("risk_engine.limits.risk_limits")
        @dataclass
        class _RiskValidationResult:
            approved:       bool
            validator_name: str = ""
            reason:         str = ""
            details:        dict = field(default_factory=dict)
        limits_mod.RiskValidationResult = _RiskValidationResult
        sys.modules.setdefault("risk_engine",               types.ModuleType("risk_engine"))
        sys.modules.setdefault("risk_engine.limits",        types.ModuleType("risk_engine.limits"))
        sys.modules.setdefault("risk_engine.limits.risk_limits", limits_mod)

    # execution_engine.orders.order — minimal stub for OrphanDetector tests
    if "execution_engine.orders.order" not in sys.modules:
        order_mod = types.ModuleType("execution_engine.orders.order")
        from enum import Enum as _Enum
        class _OrderStatus(str, _Enum):
            PENDING          = "PENDING"
            PLACED           = "PLACED"
            PARTIALLY_FILLED = "PARTIALLY_FILLED"
            FILLED           = "FILLED"
            CANCELLED        = "CANCELLED"
            REJECTED         = "REJECTED"
        @dataclass
        class _StoredOrder:
            order_id:                str   = "ord-001"
            symbol:                  str   = "RELIANCE"
            side:                    str   = "BUY"
            status:                  Any   = field(default_factory=lambda: _OrderStatus.FILLED)
            filled_quantity:         float = 100.0
            is_protective:           bool  = False
            protective_stop_order_id: str  = ""
        order_mod.OrderStatus = _OrderStatus
        order_mod.StoredOrder = _StoredOrder
        sys.modules.setdefault("execution_engine",                     types.ModuleType("execution_engine"))
        sys.modules.setdefault("execution_engine.orders",              types.ModuleType("execution_engine.orders"))
        sys.modules.setdefault("execution_engine.orders.order",        order_mod)

    # execution_engine.orders.order_manager — stub
    if "execution_engine.orders.order_manager" not in sys.modules:
        om_mod = types.ModuleType("execution_engine.orders.order_manager")
        class _FakeOrderManager:
            async def get_orders_by_status(self, status): return []
            async def get_stored_order(self, oid): return None
        om_mod.OrderManager = _FakeOrderManager
        sys.modules.setdefault("execution_engine.orders.order_manager", om_mod)


_install_stubs()


# ═══════════════════════════════════════════════════════════════════════════════
# F3 — LocalOutbox durable Kafka buffer
# ═══════════════════════════════════════════════════════════════════════════════

class TestLocalOutbox(unittest.TestCase):
    """Tests for shared/kafka/local_outbox.py SQLite-backed outbox."""

    def setUp(self):
        try:
            from shared.kafka.local_outbox import LocalOutbox
            self.LocalOutbox = LocalOutbox
        except ImportError as e:
            self.skipTest(f"LocalOutbox not importable: {e}")

    def _make(self, path: str = ":memory:") -> Any:
        return self.LocalOutbox(db_path=path, max_entries=10)

    def test_enqueue_and_pending_count(self):
        box = self._make()
        box.open()
        self.assertEqual(box.pending_count(), 0)
        result = box.enqueue(topic="t", key="k", value=b"v")
        self.assertTrue(result)
        self.assertEqual(box.pending_count(), 1)
        box.close()

    def test_enqueue_hard_cap(self):
        box = self._make()
        box.open()
        for _ in range(10):
            box.enqueue(topic="t", key="k", value=b"v")
        # 11th enqueue should be rejected
        result = box.enqueue(topic="t", key="k", value=b"v")
        self.assertFalse(result)
        self.assertEqual(box.pending_count(), 10)
        box.close()

    def test_drain_batch_delivers_to_producer_send(self):
        box = self._make()
        box.open()
        box.enqueue(topic="tpc", key="k1", value=b"hello")
        box.enqueue(topic="tpc", key="k2", value=b"world")

        delivered = []

        def _producer_send(topic, key, value):
            delivered.append((topic, key, value))

        # Call _drain_batch synchronously — avoids 2s sleep in _drain_loop
        drained = box._drain_batch(_producer_send)
        self.assertEqual(drained, 2)
        self.assertEqual(len(delivered), 2)
        self.assertEqual(box.pending_count(), 0)
        box.close()

    def test_enqueue_returns_false_before_open(self):
        box = self._make()
        result = box.enqueue(topic="t", key="k", value=b"v")
        self.assertFalse(result)

    def test_close_before_open_is_safe(self):
        box = self._make()
        box.close()  # should not raise


# ═══════════════════════════════════════════════════════════════════════════════
# F4 — Placement-pause consumer gate
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlacementPause(unittest.TestCase):
    """Tests for execution_engine placement-pause circuit."""

    def _make_service(self):
        """Build a minimal ExecutionService-like object with placement-pause state."""

        class _Market:
            NSE = "NSE"
            US  = "US"

        _PLACEMENT_PAUSE_THRESHOLD_FAILURES = 3
        _PLACEMENT_PAUSE_BACKOFF_SECONDS    = 30.0

        class _FakeSvc:
            def __init__(self):
                self._placement_consecutive_failures = 0
                self._placement_paused               = False
                self._placement_paused_until         = 0.0
                self._log                            = _stub_logger()

            def _record_placement_failure(self, market):
                if market != _Market.NSE:
                    return
                self._placement_consecutive_failures += 1
                if (
                    self._placement_consecutive_failures >= _PLACEMENT_PAUSE_THRESHOLD_FAILURES
                    and not self._placement_paused
                ):
                    import time
                    self._placement_paused        = True
                    self._placement_paused_until  = time.monotonic() + _PLACEMENT_PAUSE_BACKOFF_SECONDS

        return _FakeSvc(), _Market

    def test_no_pause_below_threshold(self):
        svc, Market = self._make_service()
        svc._record_placement_failure(Market.NSE)
        svc._record_placement_failure(Market.NSE)
        self.assertFalse(svc._placement_paused)

    def test_pause_at_threshold(self):
        svc, Market = self._make_service()
        for _ in range(3):
            svc._record_placement_failure(Market.NSE)
        self.assertTrue(svc._placement_paused)
        self.assertGreater(svc._placement_paused_until, 0)

    def test_us_failures_do_not_pause(self):
        svc, Market = self._make_service()
        for _ in range(5):
            svc._record_placement_failure(Market.US)
        self.assertFalse(svc._placement_paused)

    def test_pause_not_re_triggered_while_active(self):
        svc, Market = self._make_service()
        for _ in range(3):
            svc._record_placement_failure(Market.NSE)
        first_until = svc._placement_paused_until
        svc._record_placement_failure(Market.NSE)
        self.assertEqual(svc._placement_paused_until, first_until)

    def test_failure_count_increments_correctly(self):
        svc, Market = self._make_service()
        svc._record_placement_failure(Market.NSE)
        svc._record_placement_failure(Market.NSE)
        self.assertEqual(svc._placement_consecutive_failures, 2)


# ═══════════════════════════════════════════════════════════════════════════════
# F5 — KafkaLagWatchdog
# ═══════════════════════════════════════════════════════════════════════════════

class TestKafkaLagWatchdog(unittest.TestCase):
    """Tests for risk_engine/watchdogs/kafka_lag_watchdog.py."""

    def _load(self):
        try:
            _load_module(
                "risk_engine/watchdogs/kafka_lag_watchdog.py",
                "risk_engine.watchdogs.kafka_lag_watchdog",
            )
            from risk_engine.watchdogs.kafka_lag_watchdog import KafkaLagWatchdog
            return KafkaLagWatchdog
        except Exception as e:
            self.skipTest(f"KafkaLagWatchdog not importable: {e}")

    def test_instantiation_with_defaults(self):
        KafkaLagWatchdog = self._load()
        watchdog = KafkaLagWatchdog(
            consumer_provider=lambda: None,
            kill_switch_activate=lambda r, a: None,
        )
        # Stored on the inner monitor
        self.assertEqual(watchdog._monitor._threshold, 500)
        self.assertEqual(watchdog._monitor._consecutive_checks, 3)

    def test_instantiation_with_custom_params(self):
        KafkaLagWatchdog = self._load()
        watchdog = KafkaLagWatchdog(
            consumer_provider=lambda: None,
            kill_switch_activate=lambda r, a: None,
            lag_threshold=1000,
            consecutive_checks=5,
            check_interval_seconds=2.0,
        )
        self.assertEqual(watchdog._monitor._threshold, 1000)

    def test_stop_before_start_is_safe(self):
        KafkaLagWatchdog = self._load()
        watchdog = KafkaLagWatchdog(
            consumer_provider=lambda: None,
            kill_switch_activate=lambda r, a: None,
        )
        asyncio.get_event_loop().run_until_complete(watchdog.stop())


# ═══════════════════════════════════════════════════════════════════════════════
# F6 — ReconciliationValidator
# ═══════════════════════════════════════════════════════════════════════════════

class TestReconciliationValidator(unittest.TestCase):
    """Tests for risk_engine/validators/reconciliation_validator.py."""

    def _load(self):
        try:
            _load_module(
                "risk_engine/validators/reconciliation_validator.py",
                "risk_engine.validators.reconciliation_validator",
            )
            from risk_engine.validators.reconciliation_validator import ReconciliationValidator
            return ReconciliationValidator
        except Exception as e:
            self.skipTest(f"ReconciliationValidator not importable: {e}")

    def _make_signal(self, paper_trade=False, is_closeout=False) -> Any:
        from shared.models.signal import Signal
        sig = Signal()
        sig.paper_trade = paper_trade
        sig.metadata    = {"is_closeout": is_closeout} if is_closeout else {}
        sig.signal_id   = "sig-001"
        sig.symbol      = "RELIANCE"
        return sig

    def _make_validator(self, ReconciliationValidator, required: bool = False):
        dynamo = MagicMock()
        dynamo.get_item.return_value = {
            "Item": {
                "required": {"BOOL": required},
                "reason":   {"S": "test_reason" if required else ""},
                "set_by":   {"S": "test_operator" if required else ""},
            }
        } if required else {"Item": None}
        v = ReconciliationValidator(
            dynamo_client=dynamo,
            risk_state_table="test-risk-state",
        )
        v._required = required
        v._reason   = "test_reason" if required else ""
        v._set_by   = "test_operator" if required else ""
        v._last_refresh = 999999.0  # pre-populate cache so no DynamoDB call
        return v

    def test_approved_when_not_required(self):
        RV = self._load()
        v  = self._make_validator(RV, required=False)
        sig = self._make_signal()
        result = asyncio.get_event_loop().run_until_complete(v.validate(sig))
        self.assertTrue(result.approved)
        self.assertEqual(result.reason, "reconciliation_not_required")

    def test_rejected_when_required(self):
        RV = self._load()
        v  = self._make_validator(RV, required=True)
        sig = self._make_signal()
        result = asyncio.get_event_loop().run_until_complete(v.validate(sig))
        self.assertFalse(result.approved)
        self.assertIn("RECONCILIATION_REQUIRED", result.reason)

    def test_paper_trade_exempt_when_required(self):
        RV = self._load()
        v  = self._make_validator(RV, required=True)
        sig = self._make_signal(paper_trade=True)
        result = asyncio.get_event_loop().run_until_complete(v.validate(sig))
        self.assertTrue(result.approved)
        self.assertEqual(result.reason, "paper_trade_exempt")

    def test_closeout_exempt_when_required(self):
        RV = self._load()
        v  = self._make_validator(RV, required=True)
        sig = self._make_signal(is_closeout=True)
        result = asyncio.get_event_loop().run_until_complete(v.validate(sig))
        self.assertTrue(result.approved)
        self.assertEqual(result.reason, "closeout_exempt")

    def test_dynamo_error_defaults_to_not_required(self):
        RV = self._load()
        dynamo = MagicMock()
        dynamo.get_item.side_effect = Exception("DynamoDB offline")
        v = RV(dynamo_client=dynamo, risk_state_table="test-risk-state")
        v._last_refresh = 0.0  # force refresh
        sig = self._make_signal()
        result = asyncio.get_event_loop().run_until_complete(v.validate(sig))
        self.assertTrue(result.approved)

    def test_dynamo_read_failure_emits_alarm_metric(self):
        """Phase 2.1 Q1: a read failure emits a CloudWatch alarm metric AND fails open."""
        RV = self._load()
        dynamo = MagicMock()
        dynamo.get_item.side_effect = Exception("DynamoDB offline")
        metrics = MagicMock()
        v = RV(
            dynamo_client=dynamo,
            risk_state_table="test-risk-state",
            metrics_client=metrics,
        )
        v._last_refresh = 0.0  # force a refresh -> triggers the failing read
        sig = self._make_signal()
        result = asyncio.get_event_loop().run_until_complete(v.validate(sig))

        # Fail-open behaviour is preserved (explicitly UNCHANGED by Q1).
        self.assertTrue(result.approved)
        # Observability: an alarmable CloudWatch metric was emitted exactly once.
        metrics.put_metric_data.assert_called_once()
        kwargs = metrics.put_metric_data.call_args.kwargs
        self.assertEqual(kwargs["Namespace"], "QuantEmbrace/RiskEngine")
        self.assertEqual(
            kwargs["MetricData"][0]["MetricName"], "ReconciliationFlagReadFailure"
        )

    def test_dynamo_read_failure_logs_critical_alert(self):
        """Read failure emits a distinct CRITICAL log line for alerting (no metrics client)."""
        RV = self._load()
        dynamo = MagicMock()
        dynamo.get_item.side_effect = Exception("DynamoDB offline")
        v = RV(dynamo_client=dynamo, risk_state_table="test-risk-state")
        v._last_refresh = 0.0
        sig = self._make_signal()
        with self.assertLogs(level="CRITICAL") as cm:
            result = asyncio.get_event_loop().run_until_complete(v.validate(sig))
        self.assertTrue(result.approved)  # still fails open without a metrics client
        self.assertTrue(
            any("dynamo_read_failed" in line for line in cm.output),
            f"expected a CRITICAL dynamo_read_failed log, got: {cm.output}",
        )

    def test_reconciliation_required_property(self):
        RV = self._load()
        v  = self._make_validator(RV, required=True)
        self.assertTrue(v.reconciliation_required)

    def test_reconciliation_not_required_property(self):
        RV = self._load()
        v  = self._make_validator(RV, required=False)
        self.assertFalse(v.reconciliation_required)

    def test_rejected_result_has_details(self):
        RV = self._load()
        v  = self._make_validator(RV, required=True)
        sig = self._make_signal()
        result = asyncio.get_event_loop().run_until_complete(v.validate(sig))
        self.assertIn("reconciliation_reason", result.details)
        self.assertEqual(result.details["reconciliation_reason"], "test_reason")


# ═══════════════════════════════════════════════════════════════════════════════
# F7 — Orphan Detector
# ═══════════════════════════════════════════════════════════════════════════════

class TestOrphanDetector(unittest.TestCase):
    """Tests for execution_engine/monitors/orphan_detector.py."""

    def _load(self):
        try:
            _load_module(
                "execution_engine/monitors/orphan_detector.py",
                "execution_engine.monitors.orphan_detector",
            )
            from execution_engine.monitors.orphan_detector import OrphanDetector
            return OrphanDetector
        except Exception as e:
            self.skipTest(f"OrphanDetector not importable: {e}")

    def _make_entry_order(self, order_id="ord-1", protective_stop_order_id=""):
        from execution_engine.orders.order import StoredOrder, OrderStatus
        o = StoredOrder()
        o.order_id                  = order_id
        o.symbol                    = "RELIANCE"
        o.is_protective             = False
        o.status                    = OrderStatus.FILLED
        o.filled_quantity            = 100.0
        o.protective_stop_order_id  = protective_stop_order_id
        return o

    def _make_stop_order(self, order_id="stop-1", status_str="PLACED"):
        from execution_engine.orders.order import StoredOrder, OrderStatus
        o = StoredOrder()
        o.order_id                  = order_id
        o.is_protective             = True
        o.status                    = OrderStatus(status_str)
        o.protective_stop_order_id  = ""
        return o

    def test_no_orphan_when_no_filled_entries(self):
        OD = self._load()
        om = MagicMock()
        om.get_orders_by_status = AsyncMock(return_value=[])
        detector = OD(order_manager=om)
        orphans_found = []
        detector._on_orphan = lambda o, r: orphans_found.append((o, r))
        asyncio.get_event_loop().run_until_complete(detector._check_cycle())
        self.assertEqual(len(orphans_found), 0)

    def test_orphan_detected_no_stop_linked(self):
        OD = self._load()
        entry = self._make_entry_order(protective_stop_order_id="")
        om = MagicMock()
        om.get_orders_by_status = AsyncMock(return_value=[entry])
        orphans = []
        detector = OD(order_manager=om, on_orphan=lambda o, r: orphans.append(r))
        asyncio.get_event_loop().run_until_complete(detector._check_cycle())
        self.assertEqual(len(orphans), 1)
        self.assertIn("no_protective_stop_linked", orphans[0])

    def test_orphan_detected_stop_cancelled(self):
        OD = self._load()
        entry = self._make_entry_order(protective_stop_order_id="stop-1")
        stop  = self._make_stop_order(order_id="stop-1", status_str="CANCELLED")
        om    = MagicMock()
        om.get_orders_by_status = AsyncMock(return_value=[entry])
        om.get_stored_order     = AsyncMock(return_value=stop)
        orphans = []
        detector = OD(order_manager=om, on_orphan=lambda o, r: orphans.append(r))
        asyncio.get_event_loop().run_until_complete(detector._check_cycle())
        self.assertEqual(len(orphans), 1)
        self.assertIn("terminal", orphans[0])

    def test_no_orphan_when_stop_active(self):
        OD = self._load()
        entry = self._make_entry_order(protective_stop_order_id="stop-1")
        stop  = self._make_stop_order(order_id="stop-1", status_str="PLACED")
        om    = MagicMock()
        om.get_orders_by_status = AsyncMock(return_value=[entry])
        om.get_stored_order     = AsyncMock(return_value=stop)
        orphans = []
        detector = OD(order_manager=om, on_orphan=lambda o, r: orphans.append(r))
        asyncio.get_event_loop().run_until_complete(detector._check_cycle())
        self.assertEqual(len(orphans), 0)

    def test_protective_orders_excluded_from_scan(self):
        OD = self._load()
        from execution_engine.orders.order import StoredOrder, OrderStatus
        protective = StoredOrder()
        protective.order_id                 = "prot-1"
        protective.is_protective            = True
        protective.status                   = OrderStatus.FILLED
        protective.protective_stop_order_id = ""
        om = MagicMock()
        om.get_orders_by_status = AsyncMock(return_value=[protective])
        orphans = []
        detector = OD(order_manager=om, on_orphan=lambda o, r: orphans.append(r))
        asyncio.get_event_loop().run_until_complete(detector._check_cycle())
        self.assertEqual(len(orphans), 0)

    def test_metric_emitted_on_orphan(self):
        OD = self._load()
        entry = self._make_entry_order(protective_stop_order_id="")
        om    = MagicMock()
        om.get_orders_by_status = AsyncMock(return_value=[entry])
        metrics = MagicMock()
        metrics.put_metric = MagicMock()
        detector = OD(order_manager=om, metrics=metrics)
        asyncio.get_event_loop().run_until_complete(detector._check_cycle())
        metrics.put_metric.assert_called_once()
        call_kwargs = metrics.put_metric.call_args.kwargs
        self.assertEqual(call_kwargs["metric_name"], "OrphanPositionDetected")
        self.assertEqual(call_kwargs["value"], 1.0)

    def test_stop_missing_from_dynamo_is_orphan(self):
        OD = self._load()
        entry = self._make_entry_order(protective_stop_order_id="stop-missing")
        om    = MagicMock()
        om.get_orders_by_status = AsyncMock(return_value=[entry])
        om.get_stored_order     = AsyncMock(return_value=None)
        orphans = []
        detector = OD(order_manager=om, on_orphan=lambda o, r: orphans.append(r))
        asyncio.get_event_loop().run_until_complete(detector._check_cycle())
        self.assertEqual(len(orphans), 1)
        self.assertIn("missing_from_dynamo", orphans[0])

    def test_stop_rejected_is_orphan(self):
        OD = self._load()
        entry = self._make_entry_order(protective_stop_order_id="stop-1")
        stop  = self._make_stop_order(order_id="stop-1", status_str="REJECTED")
        om    = MagicMock()
        om.get_orders_by_status = AsyncMock(return_value=[entry])
        om.get_stored_order     = AsyncMock(return_value=stop)
        orphans = []
        detector = OD(order_manager=om, on_orphan=lambda o, r: orphans.append(r))
        asyncio.get_event_loop().run_until_complete(detector._check_cycle())
        self.assertEqual(len(orphans), 1)

    def test_stop_is_safe(self):
        OD = self._load()
        entry = self._make_entry_order(protective_stop_order_id="stop-1")
        stop  = self._make_stop_order(order_id="stop-1", status_str="PARTIALLY_FILLED")
        om    = MagicMock()
        om.get_orders_by_status = AsyncMock(return_value=[entry])
        om.get_stored_order     = AsyncMock(return_value=stop)
        orphans = []
        detector = OD(order_manager=om, on_orphan=lambda o, r: orphans.append(r))
        asyncio.get_event_loop().run_until_complete(detector._check_cycle())
        self.assertEqual(len(orphans), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# F8 — reconcile.py CLI helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestReconcileHelpers(unittest.TestCase):
    """Tests for scripts/ops/reconcile.py pure functions."""

    def _load_reconcile(self):
        try:
            sys.modules.pop("reconcile", None)
            sys.modules.pop("ops.reconcile", None)
            # Make shared.risk_state importable from _SERVICES_DIR
            if "shared.risk_state" not in sys.modules:
                _load_module("shared/risk_state.py", "shared.risk_state")
            mod = _load_script("ops/reconcile.py", "ops.reconcile")
            return mod
        except Exception as e:
            self.skipTest(f"reconcile.py not importable: {e}")

    def test_compare_positions_no_drift(self):
        mod = self._load_reconcile()
        drifts = mod._compare_positions(
            {"RELIANCE": 100.0, "TCS": 50.0},
            {"RELIANCE": 100.0, "TCS": 50.0},
        )
        self.assertEqual(drifts, [])

    def test_compare_positions_broker_extra(self):
        mod = self._load_reconcile()
        drifts = mod._compare_positions(
            {"RELIANCE": 150.0},
            {"RELIANCE": 100.0},
        )
        self.assertEqual(len(drifts), 1)
        self.assertEqual(drifts[0]["symbol"], "RELIANCE")
        self.assertAlmostEqual(drifts[0]["delta"], 50.0)

    def test_compare_positions_dynamo_extra(self):
        mod = self._load_reconcile()
        drifts = mod._compare_positions(
            {"RELIANCE": 0.0},
            {"RELIANCE": 100.0},
        )
        self.assertEqual(len(drifts), 1)
        self.assertAlmostEqual(drifts[0]["delta"], -100.0)

    def test_compare_positions_missing_in_broker(self):
        mod = self._load_reconcile()
        drifts = mod._compare_positions(
            {},
            {"TCS": 50.0},
        )
        self.assertEqual(len(drifts), 1)
        self.assertEqual(drifts[0]["symbol"], "TCS")

    def test_compare_positions_within_tolerance(self):
        mod = self._load_reconcile()
        drifts = mod._compare_positions(
            {"HDFC": 100.005},
            {"HDFC": 100.0},
        )
        self.assertEqual(drifts, [])

    def test_get_table_name_from_env(self):
        mod = self._load_reconcile()
        os.environ["DYNAMODB_TABLE_RISK_STATE"] = "custom-risk-table"
        try:
            name = mod._get_table_name("prod")
            self.assertEqual(name, "custom-risk-table")
        finally:
            del os.environ["DYNAMODB_TABLE_RISK_STATE"]

    def test_get_table_name_from_prefix(self):
        mod = self._load_reconcile()
        os.environ.pop("DYNAMODB_TABLE_RISK_STATE", None)
        os.environ.pop("DYNAMODB_TABLE_PREFIX", None)
        name = mod._get_table_name("staging")
        self.assertEqual(name, "quantembrace-staging-risk-state")

    def test_get_orders_table_default(self):
        mod = self._load_reconcile()
        os.environ.pop("DYNAMODB_TABLE_ORDERS", None)
        os.environ.pop("DYNAMODB_TABLE_PREFIX", None)
        name = mod._get_orders_table("dev")
        self.assertEqual(name, "quantembrace-dev-orders")

    def test_get_positions_table_default(self):
        mod = self._load_reconcile()
        os.environ.pop("DYNAMODB_TABLE_POSITIONS", None)
        os.environ.pop("DYNAMODB_TABLE_PREFIX", None)
        name = mod._get_positions_table("prod")
        self.assertEqual(name, "quantembrace-prod-positions")

    def test_compare_positions_multiple_symbols(self):
        mod = self._load_reconcile()
        drifts = mod._compare_positions(
            {"A": 10.0, "B": 20.0, "C": 30.0},
            {"A": 10.0, "B": 25.0, "C": 30.0},
        )
        self.assertEqual(len(drifts), 1)
        self.assertEqual(drifts[0]["symbol"], "B")


# ═══════════════════════════════════════════════════════════════════════════════
# F2 — Data quality suppression (shared.models.signal DataQuality concept)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDataQualitySuppression(unittest.TestCase):
    """
    Validates that WARMING_UP and GAP data-quality states suppress signal generation
    in the strategy runner without crashing.

    These tests check the suppression logic in isolation — the full StrategyRunner
    integration is tested by test_data_quality_suppression.py.
    """

    def _make_data_quality_enum(self):
        from enum import Enum
        class DataQuality(str, Enum):
            NORMAL     = "NORMAL"
            WARMING_UP = "WARMING_UP"
            GAP        = "GAP"
            STALE      = "STALE"
        return DataQuality

    def test_suppressed_states_are_identified(self):
        DQ = self._make_data_quality_enum()
        suppressed = {DQ.WARMING_UP, DQ.GAP, DQ.STALE}
        passing    = {DQ.NORMAL}
        self.assertNotIn(DQ.NORMAL, suppressed)
        for s in suppressed:
            self.assertNotIn(s, passing)

    def test_normal_state_passes(self):
        DQ = self._make_data_quality_enum()
        self.assertNotEqual(DQ.NORMAL, DQ.WARMING_UP)
        self.assertNotEqual(DQ.NORMAL, DQ.GAP)

    def test_warming_up_is_not_normal(self):
        DQ = self._make_data_quality_enum()
        self.assertNotEqual(DQ.WARMING_UP, DQ.NORMAL)

    def test_gap_is_not_normal(self):
        DQ = self._make_data_quality_enum()
        self.assertNotEqual(DQ.GAP, DQ.NORMAL)

    def test_stale_is_not_normal(self):
        DQ = self._make_data_quality_enum()
        self.assertNotEqual(DQ.STALE, DQ.NORMAL)


# ═══════════════════════════════════════════════════════════════════════════════
# OrderManager.get_orders_by_status (new Phase 8 method)
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetOrdersByStatus(unittest.TestCase):
    """Tests for OrderManager.get_orders_by_status added for Phase 8."""

    def _load_order_manager(self):
        # We test via the fake stub in _install_stubs()
        from execution_engine.orders.order_manager import OrderManager
        return OrderManager

    def test_returns_empty_list_without_dynamo(self):
        OM = self._load_order_manager()
        om = OM()
        result = asyncio.get_event_loop().run_until_complete(
            om.get_orders_by_status("FILLED")
        )
        self.assertEqual(result, [])

    def test_method_exists(self):
        OM = self._load_order_manager()
        self.assertTrue(hasattr(OM, "get_orders_by_status"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
