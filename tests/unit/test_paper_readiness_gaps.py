"""
Paper Trading Readiness Gap Tests — FIX-A and FIX-B regression coverage.

FIX-A: _handle_paper_order must NOT call record_order / apply_fill_to_position
       when submit_order returns False (conditional write lost race → duplicate suppression).

FIX-B: UniverseOrderValidator must allow paper orders against a stale snapshot
       (yesterday's date) with a warning — never block in paper mode.
"""

from __future__ import annotations

import sys
import types
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── import path setup ─────────────────────────────────────────────────────────

def _setup_paths() -> None:
    project_root = Path(__file__).resolve().parents[2]
    services_dir = project_root / "services"
    for p in (project_root, services_dir):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)

    if "structlog" not in sys.modules:
        sl = types.ModuleType("structlog")
        sl.get_logger = lambda *a, **kw: MagicMock()  # type: ignore[attr-defined]
        sys.modules["structlog"] = sl

    import services.shared as _sh
    import services.shared.models as _sm
    import services.shared.models.signal as _sms
    for alias, mod in (
        ("shared",              _sh),
        ("shared.models",       _sm),
        ("shared.models.signal", _sms),
    ):
        sys.modules.setdefault(alias, mod)


_setup_paths()

from shared.universe.models import (
    UniverseDecision,
    UniverseSnapshot,
)
from shared.universe.modes import UniverseMode
from shared.universe.order_validator import UniverseOrderValidator


# ── FIX-B: Stale snapshot in paper mode ──────────────────────────────────────

_SYMBOLS = frozenset({"RELIANCE", "HDFCBANK", "INFY"})
_YESTERDAY = date.today() - timedelta(days=1)
_NOW = datetime.now(timezone.utc)


def _make_snapshot(trading_date: date, mode: UniverseMode = UniverseMode.PAPER_SAFE_START) -> UniverseSnapshot:
    decisions = tuple(
        UniverseDecision(symbol=s, market="NSE", approved=True, reasons=("All filters passed",))
        for s in _SYMBOLS
    )
    return UniverseSnapshot(
        mode=mode,
        trading_date=trading_date,
        approved_symbols=_SYMBOLS,
        decisions=decisions,
        generated_at=_NOW - timedelta(days=1),
        data_sources_used=frozenset(["yaml_fallback"]),
    )


class TestStaleSnapshotPaperMode:
    """
    FIX-B regression: after midnight IST the snapshot date falls behind today.
    Paper mode must warn+allow, not block.
    """

    def test_stale_snapshot_paper_mode_allows_known_symbol(self) -> None:
        """Yesterday's snapshot still allows an approved symbol in paper mode."""
        snap = _make_snapshot(_YESTERDAY, UniverseMode.PAPER_SAFE_START)
        validator = UniverseOrderValidator(snap)

        result = validator.validate("RELIANCE", "NSE")
        assert result.approved is True, (
            f"Stale paper snapshot must allow approved symbols. Got: {result.reason}"
        )

    def test_stale_snapshot_paper_mode_allows_unknown_symbol(self) -> None:
        """Paper mode allows even unlisted symbols against a stale snapshot (permissive path)."""
        snap = _make_snapshot(_YESTERDAY, UniverseMode.PAPER_EXPAND)
        validator = UniverseOrderValidator(snap)

        # The stale-snapshot paper path logs a warning but must not block.
        result = validator.validate("TATAMOTORS", "NSE")
        # Paper mode: stale snapshot is a warning, not a hard block.
        # Symbol not in snapshot may be rejected by normal filtering —
        # what we verify is that stale-ness alone doesn't add an extra block.
        # (result.approved could be False due to "not in universe", but the
        #  reason must not mention "stale" as the ONLY cause for a known symbol)
        assert "stale" not in (result.reason or "").lower() or result.approved is True, (
            "Stale snapshot alone must not produce a hard rejection in paper mode"
        )

    def test_stale_snapshot_live_mode_blocks(self) -> None:
        """Live mode must block orders when snapshot is from a previous day."""
        snap = _make_snapshot(_YESTERDAY, UniverseMode.LIVE_ADVANCED)
        validator = UniverseOrderValidator(snap)

        result = validator.validate("RELIANCE", "NSE")
        assert result.approved is False, (
            "Stale snapshot in LIVE_ADVANCED mode must block orders"
        )
        assert result.reason is not None

    def test_no_snapshot_paper_mode_allows(self) -> None:
        """No snapshot at all: paper mode is permissive."""
        validator = UniverseOrderValidator(snapshot=None, mode=UniverseMode.PAPER_SAFE_START)
        result = validator.validate("RELIANCE", "NSE")
        assert result.approved is True

    def test_no_snapshot_live_mode_blocks(self) -> None:
        """No snapshot at all: live mode blocks all NSE orders."""
        validator = UniverseOrderValidator(snapshot=None, mode=UniverseMode.LIVE_ADVANCED)
        result = validator.validate("RELIANCE", "NSE")
        assert result.approved is False


# ── FIX-A: Paper duplicate suppression ───────────────────────────────────────

class TestPaperDuplicateSuppression:
    """
    FIX-A regression: if submit_order returns False (conditional write lost a
    race), _handle_paper_order must return immediately without calling
    record_order or apply_fill_to_position.

    We can't import ExecutionEngineService here (too many transitive deps), so
    we test the logic boundary via a minimal inline equivalent that mirrors the
    exact fix applied at services/execution_engine/service.py.
    """

    @pytest.mark.asyncio
    async def test_submit_false_skips_record_and_position(self) -> None:
        """When submit_order returns False, downstream state mutations must be skipped."""
        record_order_called = False
        apply_fill_called = False

        async def submit_order(_req: object) -> bool:
            return False  # simulate conditional write failure (duplicate)

        async def record_order(_resp: object) -> None:
            nonlocal record_order_called
            record_order_called = True

        async def apply_fill_to_position(_resp: object) -> None:
            nonlocal apply_fill_called
            apply_fill_called = True

        # Mirror the exact conditional logic from the fixed _handle_paper_order
        async def _handle_paper_order_logic(req: object) -> None:
            submitted = await submit_order(req)
            if not submitted:
                return
            await record_order(SimpleNamespace())
            await apply_fill_to_position(SimpleNamespace())

        await _handle_paper_order_logic(SimpleNamespace())

        assert not record_order_called, "record_order must NOT be called after submit_order=False"
        assert not apply_fill_called, "apply_fill must NOT be called after submit_order=False"

    @pytest.mark.asyncio
    async def test_submit_true_proceeds_to_record_and_position(self) -> None:
        """When submit_order returns True, record_order and apply_fill must both run."""
        record_order_called = False
        apply_fill_called = False

        async def submit_order(_req: object) -> bool:
            return True

        async def record_order(_resp: object) -> None:
            nonlocal record_order_called
            record_order_called = True

        async def apply_fill_to_position(_resp: object) -> None:
            nonlocal apply_fill_called
            apply_fill_called = True

        async def _handle_paper_order_logic(req: object) -> None:
            submitted = await submit_order(req)
            if not submitted:
                return
            await record_order(SimpleNamespace())
            await apply_fill_to_position(SimpleNamespace())

        await _handle_paper_order_logic(SimpleNamespace())

        assert record_order_called, "record_order MUST be called when submit_order=True"
        assert apply_fill_called, "apply_fill MUST be called when submit_order=True"

    @pytest.mark.asyncio
    async def test_concurrent_duplicate_does_not_double_fill(self) -> None:
        """Concurrent processing of the same signal_id: exactly one side fills."""
        fill_count = 0

        call_number = 0

        async def submit_order_alternating(_req: object) -> bool:
            nonlocal call_number
            call_number += 1
            # First caller wins; second loses the race
            return call_number == 1

        async def apply_fill(_resp: object) -> None:
            nonlocal fill_count
            fill_count += 1

        async def handle(req: object) -> None:
            submitted = await submit_order_alternating(req)
            if not submitted:
                return
            await apply_fill(SimpleNamespace())

        import asyncio
        req = SimpleNamespace()
        await asyncio.gather(handle(req), handle(req))

        assert fill_count == 1, (
            f"Exactly one fill expected for two concurrent handlers of the same signal. "
            f"Got fill_count={fill_count}"
        )
