"""Unit tests for AlphaModelRegistry build + bootstrap registration (ADR-031, P3)."""

from __future__ import annotations

import os
import sys

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _alpha_fakes import FakeTable  # noqa: E402

from alpha_engine.models.registry import MODEL_SPECS, AlphaModelRegistry  # noqa: E402
from alpha_engine.store.registry_store import AlphaRegistryStore  # noqa: E402


def _builder() -> tuple[AlphaModelRegistry, AlphaRegistryStore]:
    store = AlphaRegistryStore(table=FakeTable())
    builder = AlphaModelRegistry(
        store,
        symbols=["RELIANCE", "TCS"],
        horizons_minutes=[15, 30, 60],
        universe_resolver=lambda _s: "NIFTY50",
    )
    return builder, store


def test_specs_cover_the_three_production_strategies():
    assert set(MODEL_SPECS) == {"alpha_orb_v2", "alpha_vwap_rev_v2", "alpha_trend_15m"}
    assert MODEL_SPECS["alpha_trend_15m"].timeframe == "15m"
    assert MODEL_SPECS["alpha_orb_v2"].timeframe == "1m"


def test_build_models_constructs_real_strategy_adapters():
    builder, _ = _builder()
    models = builder.build_models(["alpha_orb_v2", "alpha_vwap_rev_v2", "alpha_trend_15m"], "2026-06-13")
    assert len(models) == 3
    assert {m.model_id for m in models} == {"alpha_orb_v2", "alpha_vwap_rev_v2", "alpha_trend_15m"}
    assert all(m.model_version == "2026-06-13" for m in models)


def test_build_bootstrap_registers_shadow_champion():
    builder, store = _builder()
    builder.build_models(["alpha_orb_v2"], "2026-06-13")
    meta = store.get_meta("alpha_orb_v2")
    assert meta is not None
    assert meta["champion_model_version"] == "2026-06-13"
    version = store.get_version("alpha_orb_v2", "2026-06-13")
    assert version["status"] == "SHADOW"
    assert version["experiment_id"].startswith("EXP-BOOT-")
    assert version["hypothesis"]  # lineage present even for the auto-registration


def test_unknown_model_id_is_skipped():
    builder, _ = _builder()
    models = builder.build_models(["alpha_orb_v2", "alpha_made_up"], "2026-06-13")
    assert {m.model_id for m in models} == {"alpha_orb_v2"}


def test_champion_and_challenger_both_loaded():
    builder, store = _builder()
    # First build registers champion 2026-06-13; add a challenger and rebuild.
    builder.build_models(["alpha_orb_v2"], "2026-06-13")
    store.register_version(
        model_id="alpha_orb_v2", model_version="2026-07-01", alpha_family="momentum",
        hypothesis="atr-normalized", experiment_id="EXP-2026-081", change_summary="v3",
    )
    store.set_challenger(
        model_id="alpha_orb_v2", model_version="2026-07-01",
        reason="A/B", decided_by="operator",
    )
    models = builder.build_models(["alpha_orb_v2"], "2026-06-13")
    assert sorted(m.model_version for m in models) == ["2026-06-13", "2026-07-01"]
