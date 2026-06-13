"""Unit tests for the Alpha Engine KillSwitchGate (ADR-031, P2)."""

from __future__ import annotations

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

from alpha_engine.gates.kill_switch_gate import KillSwitchGate  # noqa: E402
from shared.risk_state import kill_switch_resource_key  # noqa: E402

pytestmark = pytest.mark.asyncio


def _seed(table: FakeTable, *, active: bool) -> None:
    key = kill_switch_resource_key()
    table.put_item(
        Item={
            "PK": key["PK"],
            "SK": key["SK"],
            "active": active,
            "status": "ACTIVE" if active else "INACTIVE",
        }
    )


async def test_inactive_when_no_record():
    gate = KillSwitchGate(table=FakeTable(), poll_interval_seconds=0.0)
    assert await gate.is_active() is False


async def test_active_when_record_active():
    table = FakeTable()
    _seed(table, active=True)
    gate = KillSwitchGate(table=table, poll_interval_seconds=0.0)
    assert await gate.is_active() is True


async def test_inactive_when_record_inactive():
    table = FakeTable()
    _seed(table, active=False)
    gate = KillSwitchGate(table=table, poll_interval_seconds=0.0)
    assert await gate.is_active() is False


async def test_ttl_cache_avoids_reread():
    table = FakeTable()
    _seed(table, active=True)
    gate = KillSwitchGate(table=table, poll_interval_seconds=60.0)
    assert await gate.is_active() is True
    # Flip the row, but within the cache window the gate keeps the cached value.
    _seed(table, active=False)
    assert await gate.is_active() is True


async def test_failsafe_keeps_last_known_state_on_read_error():
    healthy = FakeTable()
    _seed(healthy, active=True)
    gate = KillSwitchGate(table=healthy, poll_interval_seconds=0.0)
    assert await gate.is_active() is True
    # Swap in a table that errors on read — gate must keep the last known (ACTIVE).
    gate._table = FakeTable(raise_on_get=True)
    assert await gate.is_active() is True
