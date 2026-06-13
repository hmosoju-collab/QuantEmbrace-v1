"""Unit tests for the AlphaForecast / AlphaOpportunity DTOs (ADR-031, P1)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
import os
import sys

import pytest

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from shared.events.schemas import (  # noqa: E402
    ALPHA_SCHEMA_VERSION,
    CANONICAL_ALPHA_OPPORTUNITIES_TOPIC,
    EventType,
    validate_event,
)
from shared.models.alpha import (  # noqa: E402
    AlphaForecast,
    AlphaOpportunity,
    FeatureContribution,
    compute_forecast_id,
)
from shared.models.signal import Direction  # noqa: E402

_TS = datetime(2026, 6, 15, 4, 0, 0, tzinfo=UTC)


def _forecast(**overrides) -> AlphaForecast:
    base = {
        "model_id": "alpha_orb_v2",
        "model_version": "2026-06-13",
        "alpha_family": "momentum",
        "symbol": "RELIANCE",
        "market": "NSE",
        "universe": "NIFTY50",
        "direction": Direction.BUY,
        "timeframe": "1m",
        "horizon_minutes": 15,
        "forecast_return_bps": 110.0,
        "confidence": 0.8,
        "decision_price": 2500.0,
        "decision_ts": _TS,
        "trace_id": "trace-abc",
        "top_features": (
            FeatureContribution("opening_range_breakout", 0.82),
            FeatureContribution("volume_zscore", 0.64),
        ),
    }
    base.update(overrides)
    return AlphaForecast(**base)


def test_model_version_mandatory():
    with pytest.raises(ValueError):
        _forecast(model_version="")


def test_forecast_is_frozen():
    f = _forecast()
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.confidence = 0.1  # type: ignore[misc]


def test_forecast_id_deterministic():
    assert _forecast().forecast_id == _forecast().forecast_id


def test_forecast_id_depends_on_model_version():
    """A re-tuned model version is a distinct observation — id must differ (#2)."""
    a = _forecast(model_version="2026-06-13")
    b = _forecast(model_version="2026-07-01")
    assert a.forecast_id != b.forecast_id


def test_forecast_id_depends_on_horizon():
    """Multi-horizon fan-out: same trigger, distinct ids per horizon (#1)."""
    h15 = _forecast(horizon_minutes=15)
    h30 = _forecast(horizon_minutes=30)
    h60 = _forecast(horizon_minutes=60)
    assert len({h15.forecast_id, h30.forecast_id, h60.forecast_id}) == 3


def test_forecast_id_matches_helper():
    f = _forecast()
    assert f.forecast_id == compute_forecast_id(
        f.model_id, f.model_version, f.symbol, f.direction, f.horizon_minutes, f.decision_ts
    )


def test_instrument_id_and_model_ref():
    f = _forecast()
    assert f.instrument_id == "NSE:RELIANCE"
    assert f.model_ref == "alpha_orb_v2@2026-06-13"


def test_to_from_dict_round_trip_preserves_id_and_features():
    f = _forecast()
    restored = AlphaForecast.from_dict(f.to_dict())
    assert restored.forecast_id == f.forecast_id
    assert restored.top_features == f.top_features
    assert restored.direction is Direction.BUY
    assert restored.to_dict() == f.to_dict()


def test_naive_decision_ts_is_treated_as_utc():
    naive = _forecast(decision_ts=datetime(2026, 6, 15, 4, 0, 0))
    aware = _forecast(decision_ts=_TS)
    assert naive.forecast_id == aware.forecast_id


def test_opportunity_round_trip():
    opp = AlphaOpportunity(
        forecast=_forecast(),
        rank=1,
        score=88.0,
        cycle_id="cycle123",
        cycle_ts=_TS,
        conflict_group_id="conf-1",
        published=True,
    )
    restored = AlphaOpportunity.from_dict(opp.to_dict())
    assert restored.forecast.forecast_id == opp.forecast.forecast_id
    assert restored.published is True
    assert restored.conflict_group_id == "conf-1"


def test_alpha_opportunity_event_validates_at_schema_v1():
    """The event envelope for alpha.opportunities validates under schema 1.0."""
    f = _forecast()
    event = {
        "event_id": "e1",
        "trace_id": f.trace_id,
        "event_type": EventType.ALPHA_OPPORTUNITY.value,
        "schema_version": ALPHA_SCHEMA_VERSION,
        "source": "alpha_engine",
        "published_time": _TS.isoformat(),
        "forecast_id": f.forecast_id,
        "cycle_id": "cycle123",
        "rank": 1,
        "score": 88.0,
        "model_id": f.model_id,
        "model_version": f.model_version,
        "alpha_family": f.alpha_family,
        "symbol": f.symbol,
        "instrument_id": f.instrument_id,
        "market": f.market,
        "universe": f.universe,
        "timeframe": f.timeframe,
        "direction": f.direction.value,
        "horizon_minutes": f.horizon_minutes,
        "forecast_return_bps": f.forecast_return_bps,
        "net_edge_bps": 90.0,
        "edge_band": ">=50",
        "confidence": f.confidence,
        "top_features": [fc.to_dict() for fc in f.top_features],
        "conflict_group_id": None,
        "decision_price": f.decision_price,
        "decision_ts": f.decision_ts.isoformat(),
        "expires_at": _TS.isoformat(),
        "shadow_mode": True,
    }
    assert validate_event(event, EventType.ALPHA_OPPORTUNITY) == []
    assert CANONICAL_ALPHA_OPPORTUNITIES_TOPIC == "alpha.opportunities"


def test_alpha_opportunity_event_missing_field_is_flagged():
    errors = validate_event(
        {
            "event_type": EventType.ALPHA_OPPORTUNITY.value,
            "schema_version": ALPHA_SCHEMA_VERSION,
        },
        EventType.ALPHA_OPPORTUNITY,
    )
    assert any("missing required fields" in e for e in errors)
    assert any("shadow_mode" in e for e in errors)
