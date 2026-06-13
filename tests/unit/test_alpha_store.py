"""Unit tests for the Alpha Engine forecast + registry stores (ADR-031, P2)."""

from __future__ import annotations

from datetime import UTC, datetime
import os
import sys

import pytest

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _alpha_fakes import FakeTable  # noqa: E402

from alpha_engine.store.forecast_store import ForecastStore  # noqa: E402
from alpha_engine.store.registry_store import (  # noqa: E402
    AlphaRegistryStore,
    RegistryError,
)
from shared.models.alpha import AlphaForecast, AlphaOpportunity  # noqa: E402
from shared.models.signal import Direction  # noqa: E402

_TS = datetime(2026, 6, 15, 4, 0, 0, tzinfo=UTC)


def _opportunity(*, published: bool = True, suppressed_by: str = "") -> AlphaOpportunity:
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
        forecast=f, rank=1, score=72.0, cycle_id="c1", cycle_ts=_TS,
        published=published, suppressed_by=suppressed_by,
    )


# ── ForecastStore ──────────────────────────────────────────────────────────────

def test_put_opportunity_is_idempotent():
    store = ForecastStore(table=FakeTable())
    assert store.put_opportunity(_opportunity()) is True
    assert store.put_opportunity(_opportunity()) is False  # same forecast_id → no-op


def test_get_returns_stored_forecast_with_labels_pending():
    table = FakeTable()
    store = ForecastStore(table=table)
    opp = _opportunity()
    store.put_opportunity(opp)
    got = store.get(
        trade_date="2026-06-15",
        market="NSE",
        decision_ts_iso=opp.forecast.to_dict()["decision_ts"],
        forecast_id=opp.forecast.forecast_id,
    )
    assert got is not None
    assert got["label_status"] == "PENDING"
    assert got["net_edge_bps"] == 90.0
    assert got["published"] is True


def test_suppressed_forecast_is_still_stored():
    store = ForecastStore(table=FakeTable())
    assert store.put_opportunity(_opportunity(published=False, suppressed_by="edge_floor")) is True


# ── AlphaRegistryStore ─────────────────────────────────────────────────────────

def _registry() -> AlphaRegistryStore:
    return AlphaRegistryStore(table=FakeTable())


def test_register_version_defaults_to_shadow_with_lineage():
    reg = _registry()
    rec = reg.register_version(
        model_id="alpha_orb_v2",
        model_version="2026-06-13",
        alpha_family="momentum",
        hypothesis="ORB clears cost in trending opens",
        experiment_id="EXP-2026-001",
        change_summary="initial ORB v2",
    )
    assert rec["status"] == "SHADOW"
    assert rec["hypothesis"]
    assert len(rec["status_history"]) == 1


@pytest.mark.parametrize("missing", ["hypothesis", "experiment_id", "change_summary"])
def test_register_version_requires_lineage(missing):
    reg = _registry()
    kwargs = {
        "model_id": "alpha_orb_v2",
        "model_version": "2026-06-13",
        "alpha_family": "momentum",
        "hypothesis": "h",
        "experiment_id": "EXP-1",
        "change_summary": "s",
    }
    kwargs[missing] = ""
    with pytest.raises(RegistryError):
        reg.register_version(**kwargs)


def test_register_duplicate_version_raises():
    reg = _registry()
    args = {
        "model_id": "alpha_orb_v2", "model_version": "2026-06-13", "alpha_family": "momentum",
        "hypothesis": "h", "experiment_id": "EXP-1", "change_summary": "s",
    }
    reg.register_version(**args)
    with pytest.raises(RegistryError):
        reg.register_version(**args)


def test_first_version_becomes_champion():
    reg = _registry()
    reg.register_version(
        model_id="alpha_orb_v2", model_version="2026-06-13", alpha_family="momentum",
        hypothesis="h", experiment_id="EXP-1", change_summary="s",
    )
    meta = reg.get_meta("alpha_orb_v2")
    assert meta["champion_model_version"] == "2026-06-13"
    assert meta["challenger_model_version"] is None


def test_set_status_appends_history():
    reg = _registry()
    reg.register_version(
        model_id="alpha_orb_v2", model_version="2026-06-13", alpha_family="momentum",
        hypothesis="h", experiment_id="EXP-1", change_summary="s",
    )
    reg.set_status(
        model_id="alpha_orb_v2", model_version="2026-06-13",
        status="RESEARCH", reason="promising IC", decided_by="operator",
    )
    rec = reg.get_version("alpha_orb_v2", "2026-06-13")
    assert rec["status"] == "RESEARCH"
    assert [h["status"] for h in rec["status_history"]] == ["SHADOW", "RESEARCH"]
    assert rec["status_history"][-1]["decided_by"] == "operator"


def test_set_status_rejects_invalid_status():
    reg = _registry()
    reg.register_version(
        model_id="alpha_orb_v2", model_version="2026-06-13", alpha_family="momentum",
        hypothesis="h", experiment_id="EXP-1", change_summary="s",
    )
    with pytest.raises(RegistryError):
        reg.set_status(
            model_id="alpha_orb_v2", model_version="2026-06-13",
            status="LIVE", reason="x", decided_by="op",
        )


def test_set_challenger_updates_meta_and_audit():
    reg = _registry()
    for v in ("2026-06-13", "2026-07-01"):
        reg.register_version(
            model_id="alpha_orb_v2", model_version=v, alpha_family="momentum",
            hypothesis="h", experiment_id="EXP-1", change_summary="s",
        )
    reg.set_challenger(
        model_id="alpha_orb_v2", model_version="2026-07-01",
        reason="A/B vs champion", decided_by="operator",
    )
    meta = reg.get_meta("alpha_orb_v2")
    assert meta["champion_model_version"] == "2026-06-13"
    assert meta["challenger_model_version"] == "2026-07-01"
    assert meta["change_history"][-1]["role"] == "challenger_model_version"


def test_set_role_for_unregistered_version_raises():
    reg = _registry()
    with pytest.raises(RegistryError):
        reg.set_champion(
            model_id="ghost", model_version="2026-01-01", reason="x", decided_by="op"
        )
