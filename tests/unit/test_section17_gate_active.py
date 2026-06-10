"""
Tests for §17 gate_active field in MonitoringStatusRenderer.

Covers:
    1. gate_active = True when CONFIDENCE_BELOW_THRESHOLD count > 0
    2. gate_active = True when REWARD_RISK_TOO_LOW count > 0
    3. gate_active = True when MAX_TRADES_PER_SYMBOL_REACHED count > 0
    4. gate_active = False when all counters are 0
    5. gate_active = True when multiple counters are non-zero
    6. The rendered §17 includes the "Gate active" row
"""

from __future__ import annotations

from dataclasses import field, dataclass
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from shared.monitoring.monitoring_status import (
    MonitoringStatusRenderer,
    MonitoringStatusSnapshot,
    NettingProtectionStatus,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _minimal_snapshot(quality_gate_rejections: dict) -> MonitoringStatusSnapshot:
    """Create the minimum viable MonitoringStatusSnapshot with given QG counts."""
    return MonitoringStatusSnapshot(
        overall_status="GREEN",
        timestamp_ist="2026-06-05 09:30:00 IST",
        trading_mode="PAPER",
        live_trading_enabled=False,
        broker_live_calls="DISABLED",
        kill_switch_active=False,
        session_safety="SAFE",
        verdict_line="All clear.",
        quality_gate_rejections=quality_gate_rejections,
        netting_status=NettingProtectionStatus(),
        performance_status=None,  # triggers the unavailable branch
    )


def _render_s17(qg: dict) -> str:
    """Render §17 for a snapshot with the given quality gate rejection dict."""
    snap = _minimal_snapshot(qg)
    renderer = MonitoringStatusRenderer()
    return renderer._s17(snap)


# ── Test 1: gate_active True when CONFIDENCE_BELOW_THRESHOLD > 0 ─────────────

def test_gate_active_true_when_confidence_below_threshold() -> None:
    """When CONFIDENCE_BELOW_THRESHOLD > 0, gate_active must be true in §17."""
    output = _render_s17({"CONFIDENCE_BELOW_THRESHOLD": 3, "REWARD_RISK_TOO_LOW": 0, "MAX_TRADES_PER_SYMBOL_REACHED": 0})
    assert "| Gate active | true |" in output, (
        "Gate active must be 'true' when CONFIDENCE_BELOW_THRESHOLD > 0"
    )


# ── Test 2: gate_active True when REWARD_RISK_TOO_LOW > 0 ────────────────────

def test_gate_active_true_when_reward_risk_too_low() -> None:
    """When REWARD_RISK_TOO_LOW > 0, gate_active must be true in §17."""
    output = _render_s17({"CONFIDENCE_BELOW_THRESHOLD": 0, "REWARD_RISK_TOO_LOW": 5, "MAX_TRADES_PER_SYMBOL_REACHED": 0})
    assert "| Gate active | true |" in output, (
        "Gate active must be 'true' when REWARD_RISK_TOO_LOW > 0"
    )


# ── Test 3: gate_active True when MAX_TRADES_PER_SYMBOL_REACHED > 0 ──────────

def test_gate_active_true_when_max_trades_per_symbol_reached() -> None:
    """When MAX_TRADES_PER_SYMBOL_REACHED > 0, gate_active must be true in §17."""
    output = _render_s17({"CONFIDENCE_BELOW_THRESHOLD": 0, "REWARD_RISK_TOO_LOW": 0, "MAX_TRADES_PER_SYMBOL_REACHED": 2})
    assert "| Gate active | true |" in output, (
        "Gate active must be 'true' when MAX_TRADES_PER_SYMBOL_REACHED > 0"
    )


# ── Test 4: gate_active False when all counters are 0 ────────────────────────

def test_gate_active_false_when_all_counters_zero() -> None:
    """When all quality gate counters are 0, gate_active must be false in §17."""
    output = _render_s17({"CONFIDENCE_BELOW_THRESHOLD": 0, "REWARD_RISK_TOO_LOW": 0, "MAX_TRADES_PER_SYMBOL_REACHED": 0})
    assert "| Gate active | false |" in output, (
        "Gate active must be 'false' when all counters are 0"
    )


# ── Test 5: gate_active True when multiple counters non-zero ──────────────────

def test_gate_active_true_when_multiple_counters_non_zero() -> None:
    """When multiple quality gate counters are non-zero, gate_active must be true."""
    output = _render_s17({"CONFIDENCE_BELOW_THRESHOLD": 10, "REWARD_RISK_TOO_LOW": 3, "MAX_TRADES_PER_SYMBOL_REACHED": 7})
    assert "| Gate active | true |" in output


# ── Test 6: gate_active False when no quality gate data (empty dict) ──────────

def test_gate_active_false_when_empty_qg_dict() -> None:
    """When quality_gate_rejections is empty, all counters default to 0 and gate_active is false."""
    output = _render_s17({})
    assert "| Gate active | false |" in output, (
        "Gate active must be 'false' when quality_gate_rejections is empty"
    )


# ── Test 7: rendered §17 includes the Gate active row ─────────────────────────

def test_s17_renders_gate_active_row() -> None:
    """The rendered §17 markdown must contain a '| Gate active |' table row."""
    output = _render_s17({"CONFIDENCE_BELOW_THRESHOLD": 1})
    assert "| Gate active |" in output, "§17 must contain a Gate active row"


# ── Test 8: Session 12 note appears when _SESSION12_NOTE_ACTIVE is True ───────

def test_s17_contains_session12_note() -> None:
    """§17 must include the Session 12 note (stale-image rebuild context)."""
    output = _render_s17({})
    assert "Session 12 is the first valid quality-gate test" in output, (
        "§17 must contain the Session 12 context note while "
        "_SESSION12_NOTE_ACTIVE = True"
    )
