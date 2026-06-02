"""
Phase 2.1 / Q3 — Paper/live isolation regression tests.

Proves that ``ExecutionService._reconcile_state`` never queries a real broker
for a *paper* order on restart.

Why this matters:
    Paper orders are simulated by ``_handle_paper_order`` and are written to the
    orders table with a synthetic ``broker_order_id``/``order_id`` of the form
    ``PAPER-...`` and ``metadata["paper_trade"]=True``.  The only paper status
    that surfaces in ``get_open_orders`` is ``PARTIALLY_FILLED`` (FILLED and
    REJECTED are terminal).  Before the Q3 guard, startup reconciliation Pass 2
    would call ``_get_broker(market).get_order_status("PAPER-...")`` for such an
    order — issuing a real broker API call with a paper identifier and breaking
    the strict paper/live isolation invariant in CLAUDE.md.

These tests exercise the *real* ``ExecutionService._reconcile_state`` and
``_is_paper_order`` code.  The heavyweight imports of ``service.py`` (brokers,
Kafka, boto3, pollers) are replaced with in-memory stubs so the module imports
in a bare sandbox; the reconciliation logic itself is the genuine production
code.

Standalone:
    python -m pytest tests/unit/test_paper_broker_isolation.py -v
    python tests/unit/test_paper_broker_isolation.py
"""

from __future__ import annotations

import asyncio
import importlib.util as _ilu
import logging
import os
import sys
import types
import unittest
from enum import Enum
from unittest.mock import AsyncMock, MagicMock


# ── Path helpers ────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")


# ── Minimal stub logger (swallows structured-logging kwargs) ─────────────────
class _Logger:
    def __init__(self, *a, **k):
        self._l = logging.getLogger("test.exec_isolation")

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def critical(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass


# ── Order enums shared between the stub `order` module and the assertions ────
# These objects are registered as `execution_engine.orders.order`, so the very
# same OrderStatus members are used by service.py's comparisons AND by the test.
class OrderStatus(str, Enum):
    PENDING = "PENDING"
    ACK_UNKNOWN = "ACK_UNKNOWN"
    PLACED = "PLACED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    FLATTENING = "FLATTENING"


class Market(str, Enum):
    NSE = "NSE"
    US = "US"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    STOP_LOSS_MARKET = "SL-M"
    TRAILING_STOP = "TRAILING_STOP"


class ProductType(str, Enum):
    CNC = "CNC"
    MIS = "MIS"
    NRML = "NRML"
    DAY = "DAY"


# Empty placeholder classes — service.py references these names in method
# annotations evaluated at class-body time (e.g. `OrderRequest | StoredOrder`),
# so they must be real classes that support the `|` operator.
class OrderRequest:  # noqa: D401 - stub
    pass


class OrderResponse:  # noqa: D401 - stub
    pass


class StoredOrder:  # noqa: D401 - stub
    pass


def _reg(name: str, **attrs):
    """Register a stub module in sys.modules with the given attributes."""
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def _install_stubs() -> None:
    """Install stub modules for every top-level import in execution_engine/service.py."""
    # Third-party
    _reg(
        "confluent_kafka",
        KafkaError=type("KafkaError", (), {"_PARTITION_EOF": -191}),
        Consumer=object,
        Producer=object,
    )
    _reg(
        "aws_msk_iam_sasl_signer",
        MSKAuthTokenProvider=type(
            "MSKAuthTokenProvider",
            (),
            {"generate_auth_token": staticmethod(lambda region: ("token", 1.0))},
        ),
    )
    boto3_mod = _reg("boto3")
    boto3_mod.client = MagicMock(return_value=MagicMock())

    # Parent packages (so dotted imports resolve)
    for pkg in (
        "execution_engine",
        "execution_engine.brokers",
        "execution_engine.consumers",
        "execution_engine.orders",
        "execution_engine.polling",
        "execution_engine.publishers",
        "execution_engine.monitors",
        "execution_engine.retry",
        "shared",
        "shared.config",
        "shared.health",
        "shared.kafka",
        "shared.logging",
        "shared.metrics",
        "shared.zerodha",
    ):
        sys.modules[pkg] = types.ModuleType(pkg)

    # First-party leaf modules — expose only the names service.py imports.
    _reg("execution_engine.brokers.alpaca_broker", AlpacaBroker=type("AlpacaBroker", (), {}))
    _reg(
        "execution_engine.brokers.base_broker",
        BrokerClient=type("BrokerClient", (), {}),
        NonRetryableBrokerError=type("NonRetryableBrokerError", (Exception,), {}),
    )
    _reg(
        "execution_engine.brokers.zerodha_broker",
        ZerodhaBrokerClient=type("ZerodhaBrokerClient", (), {}),
    )
    _reg(
        "execution_engine.consumers.kafka_approved_consumer",
        ApprovedSignalEvent=type("ApprovedSignalEvent", (), {}),
        KafkaApprovedConsumer=type("KafkaApprovedConsumer", (), {}),
    )
    _reg(
        "execution_engine.consumers.kafka_kill_switch_listener",
        KafkaExecutionKillSwitchListener=type("KafkaExecutionKillSwitchListener", (), {}),
    )
    _reg(
        "execution_engine.orders.order",
        Market=Market,
        OrderRequest=OrderRequest,
        OrderResponse=OrderResponse,
        OrderSide=OrderSide,
        OrderStatus=OrderStatus,
        OrderType=OrderType,
        ProductType=ProductType,
        StoredOrder=StoredOrder,
    )
    _reg("execution_engine.orders.order_manager", OrderManager=type("OrderManager", (), {}))
    _reg("execution_engine.polling.bulk_order_poller", BulkOrderPoller=type("BulkOrderPoller", (), {}))
    _reg("execution_engine.polling.live_quote_poller", LiveQuotePoller=type("LiveQuotePoller", (), {}))
    _reg("execution_engine.polling.position_monitor", PositionMonitor=type("PositionMonitor", (), {}))
    _reg(
        "execution_engine.publishers.kafka_order_events_publisher",
        KafkaOrderEventsPublisher=type("KafkaOrderEventsPublisher", (), {}),
    )
    _reg("execution_engine.monitors.orphan_detector", OrphanDetector=type("OrphanDetector", (), {}))
    _reg("execution_engine.retry.retry_handler", RetryHandler=type("RetryHandler", (), {}))

    _reg(
        "shared.config.settings",
        AppSettings=type("AppSettings", (), {}),
        get_settings=lambda: MagicMock(),
    )
    _reg("shared.health.health_server", HealthServer=type("HealthServer", (), {}))
    _reg("shared.health.loop_health", LoopHealthTracker=type("LoopHealthTracker", (), {}))
    _reg("shared.kafka.retry_replayer", KafkaRetryReplayer=type("KafkaRetryReplayer", (), {}))
    _reg(
        "shared.logging.logger",
        get_logger=lambda *a, **k: _Logger(),
        set_correlation_id=lambda *a, **k: None,
    )
    _reg("shared.metrics.cloudwatch_metrics", get_metrics_client=lambda *a, **k: MagicMock())
    _reg(
        "shared.risk_state",
        attr_bool=lambda *a, **k: False,
        attr_string=lambda *a, **k: "",
        attr_number=lambda *a, **k: 0.0,
        kill_switch_key=lambda: {"PK": {"S": "KILLSWITCH"}, "SK": {"S": "GLOBAL"}},
        kill_switch_item=lambda **kw: {},  # Phase 7.1 — required by test_paper_preflight_check
        kill_switch_resource_key=lambda: {"PK": "KILLSWITCH", "SK": "GLOBAL"},
        nav_key=lambda: {"PK": {"S": "NAV#CURRENT"}, "SK": {"S": "STATE"}},
        risk_decision_key=lambda *a: {"PK": {"S": "RISK_DECISION"}, "SK": {"S": "DECISION"}},
    )
    _reg("shared.zerodha.market_phase", MarketPhaseGovernor=type("MarketPhaseGovernor", (), {}))
    _reg(
        "shared.zerodha.rate_limiter",
        EndpointClass=type("EndpointClass", (), {}),
        Priority=type("Priority", (), {}),
        ZerodhaRateLimiter=type("ZerodhaRateLimiter", (), {}),
    )


def _load_service_module():
    """Load the REAL execution_engine/service.py with heavyweight deps stubbed.

    sys.modules is snapshotted and restored so the stubs don't leak into other
    test modules that may run in the same pytest session — the loaded service
    module has already bound the names it imported.
    """
    saved = dict(sys.modules)
    try:
        _install_stubs()
        path = os.path.join(_SERVICES_DIR, "execution_engine", "service.py")
        spec = _ilu.spec_from_file_location("execution_engine.service", path)
        mod = _ilu.module_from_spec(spec)
        sys.modules["execution_engine.service"] = mod
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod
    finally:
        for key in list(sys.modules.keys()):
            if key not in saved:
                del sys.modules[key]
        sys.modules.update(saved)


_SERVICE = _load_service_module()
ExecutionService = _SERVICE.ExecutionService


# ── Test doubles ─────────────────────────────────────────────────────────────
class _OrderManagerStub:
    def __init__(self, orders):
        self._orders = list(orders)

    async def get_open_orders(self):
        return list(self._orders)


def _make_order(*, order_id, broker_order_id, status, metadata=None, market=Market.NSE):
    """Build a duck-typed open-order record matching the fields _reconcile_state reads."""
    o = types.SimpleNamespace()
    o.order_id = order_id
    o.broker_order_id = broker_order_id
    o.status = status
    o.metadata = dict(metadata or {})
    o.market = market
    o.symbol = "RELIANCE"
    o.side = OrderSide.BUY
    o.signal_id = "sig-1"
    o.risk_decision_id = "rd-1"
    o.order_type = OrderType.MARKET
    o.limit_price = None
    o.stop_price = None
    o.product_type = ProductType.MIS
    o.filled_quantity = 0.0
    o.avg_fill_price = 0.0
    return o


def _new_svc(orders, broker_calls):
    """Construct an ExecutionService without running __init__ and wire test doubles."""
    svc = ExecutionService.__new__(ExecutionService)
    svc._order_manager = _OrderManagerStub(orders)

    def _get_broker(market):
        # Record every real-broker resolution; if a paper order ever reaches
        # here the test must fail.
        broker_calls.append(market)
        broker = MagicMock()
        broker.get_order_status = AsyncMock(
            return_value=types.SimpleNamespace(
                new_status=OrderStatus.PARTIALLY_FILLED,
                filled_quantity=0.0,
                avg_fill_price=0.0,
            )
        )
        return broker

    svc._get_broker = _get_broker
    return svc


# ── Tests ─────────────────────────────────────────────────────────────────────
class TestIsPaperOrderHelper(unittest.TestCase):
    def test_detects_paper_by_broker_order_id_prefix(self):
        order = _make_order(order_id="X", broker_order_id="PAPER-AB12", status=OrderStatus.PARTIALLY_FILLED)
        self.assertTrue(ExecutionService._is_paper_order(order))

    def test_detects_paper_by_metadata_flag(self):
        order = _make_order(
            order_id="X", broker_order_id="BRK-1", status=OrderStatus.PARTIALLY_FILLED,
            metadata={"paper_trade": True},
        )
        self.assertTrue(ExecutionService._is_paper_order(order))

    def test_detects_paper_by_order_id_prefix(self):
        order = _make_order(order_id="PAPER-ZZ", broker_order_id="", status=OrderStatus.PARTIALLY_FILLED)
        self.assertTrue(ExecutionService._is_paper_order(order))

    def test_real_order_is_not_paper(self):
        order = _make_order(order_id="ORD-1", broker_order_id="BRK-1", status=OrderStatus.PARTIALLY_FILLED)
        self.assertFalse(ExecutionService._is_paper_order(order))


class TestReconcileSkipsPaperOrders(unittest.TestCase):
    def test_partially_filled_paper_order_never_calls_broker(self):
        """A PARTIALLY_FILLED paper order must NOT trigger any real broker call."""
        paper = _make_order(
            order_id="PAPER-ABC123",
            broker_order_id="PAPER-ABC123",
            status=OrderStatus.PARTIALLY_FILLED,
            metadata={"paper_trade": True},
        )
        broker_calls: list = []
        svc = _new_svc([paper], broker_calls)

        asyncio.run(svc._reconcile_state())

        self.assertEqual(
            broker_calls,
            [],
            "real broker was queried for a paper order during reconciliation — "
            "paper/live isolation violated",
        )

    def test_paper_order_with_only_broker_prefix_is_skipped(self):
        """Even without the metadata flag, a PAPER- broker id must be skipped."""
        paper = _make_order(
            order_id="PAPER-NOMETA",
            broker_order_id="PAPER-NOMETA",
            status=OrderStatus.PARTIALLY_FILLED,
            metadata={},  # no paper_trade flag — prefix alone must be enough
        )
        broker_calls: list = []
        svc = _new_svc([paper], broker_calls)
        asyncio.run(svc._reconcile_state())
        self.assertEqual(broker_calls, [])

    def test_real_partially_filled_order_still_reconciles_via_broker(self):
        """Guard must be paper-specific: a real PARTIALLY_FILLED order is still checked."""
        real = _make_order(
            order_id="ORD-REAL-1",
            broker_order_id="BRK-REAL-1",
            status=OrderStatus.PARTIALLY_FILLED,
            metadata={},
        )
        broker_calls: list = []
        svc = _new_svc([real], broker_calls)
        asyncio.run(svc._reconcile_state())
        self.assertEqual(
            broker_calls,
            [Market.NSE],
            "real order should still be reconciled against the broker exactly once",
        )

    def test_mixed_batch_only_real_orders_hit_broker(self):
        """With paper + real orders mixed, only the real one reaches the broker."""
        paper = _make_order(
            order_id="PAPER-MIX",
            broker_order_id="PAPER-MIX",
            status=OrderStatus.PARTIALLY_FILLED,
            metadata={"paper_trade": True},
        )
        real = _make_order(
            order_id="ORD-MIX",
            broker_order_id="BRK-MIX",
            status=OrderStatus.PARTIALLY_FILLED,
            metadata={},
        )
        broker_calls: list = []
        svc = _new_svc([paper, real], broker_calls)
        asyncio.run(svc._reconcile_state())
        self.assertEqual(broker_calls, [Market.NSE])


if __name__ == "__main__":
    unittest.main(verbosity=2)
