"""Shadow-isolation tests for the Alpha Engine publisher (ADR-031, P2).

The single most important governance guarantee: the Alpha Engine can publish ONLY
to ``alpha.opportunities`` and imports no broker SDK.
"""

from __future__ import annotations

from datetime import UTC, datetime
import importlib
import os
import pkgutil
import sys

import pytest

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from alpha_engine.publishers.alpha_shadow_publisher import (  # noqa: E402
    ALLOWED_TOPICS,
    AlphaShadowPublisher,
    ShadowIsolationError,
    build_alpha_opportunity_event,
)
from shared.events.schemas import EventType, validate_event  # noqa: E402
from shared.models.alpha import AlphaForecast, AlphaOpportunity  # noqa: E402
from shared.models.signal import Direction  # noqa: E402

_TS = datetime(2026, 6, 15, 4, 0, 0, tzinfo=UTC)


def _opportunity() -> AlphaOpportunity:
    f = AlphaForecast(
        model_id="alpha_orb_v2",
        model_version="2026-06-13",
        alpha_family="momentum",
        symbol="RELIANCE",
        market="NSE",
        universe="NIFTY50",
        direction=Direction.BUY,
        timeframe="1m",
        horizon_minutes=15,
        forecast_return_bps=110.0,
        confidence=0.8,
        decision_price=2500.0,
        decision_ts=_TS,
        trace_id="t",
        net_edge_bps=90.0,
        edge_band=">=50",
    )
    return AlphaOpportunity(
        forecast=f, rank=1, score=72.0, cycle_id="c1", cycle_ts=_TS, published=True
    )


def test_allowlist_is_exactly_alpha_opportunities():
    assert ALLOWED_TOPICS == frozenset({"alpha.opportunities"})
    assert isinstance(ALLOWED_TOPICS, frozenset)


@pytest.mark.parametrize("topic", ["signals.pending", "signals.approved", "orders.events", "risk.kill-switch"])
def test_publish_to_trading_topic_raises(topic):
    pub = AlphaShadowPublisher("localhost:9092")  # no start() — guard is pre-producer
    with pytest.raises(ShadowIsolationError):
        pub.publish(topic=topic, key="NSE:RELIANCE", event={"shadow_mode": True})


def test_publish_opportunity_event_is_shadow_and_valid():
    event = build_alpha_opportunity_event(_opportunity())
    assert event["shadow_mode"] is True
    assert event["event_type"] == EventType.ALPHA_OPPORTUNITY.value
    assert validate_event(event, EventType.ALPHA_OPPORTUNITY) == []


def test_publish_without_shadow_mode_flag_raises():
    pub = AlphaShadowPublisher("localhost:9092")
    bad = build_alpha_opportunity_event(_opportunity())
    bad["shadow_mode"] = False
    with pytest.raises(ShadowIsolationError):
        pub.publish(topic="alpha.opportunities", key="NSE:RELIANCE", event=bad)


def test_alpha_engine_imports_no_broker_sdk():
    """Importing every alpha_engine module must not pull in a broker SDK."""
    import alpha_engine  # noqa: F401

    forbidden = {"kiteconnect", "alpaca", "alpaca_trade_api"}
    pkg = sys.modules["alpha_engine"]
    for mod in pkgutil.walk_packages(pkg.__path__, prefix="alpha_engine."):
        importlib.import_module(mod.name)
    leaked = forbidden & set(sys.modules)
    assert not leaked, f"alpha_engine must not import broker SDKs; found {leaked}"
