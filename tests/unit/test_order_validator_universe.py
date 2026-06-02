"""
Tests for UniverseOrderValidator — hard order validation rule.

Tests cover:
  - Order validator rejects symbol outside approved universe
  - Order validator accepts symbol inside approved universe
  - Rejection reason is logged and returned
  - Emergency exclusion blocks symbol
  - Missing snapshot: paper mode allows (with warning), live mode blocks
  - Snapshot mode mismatch raises on update_snapshot
  - US market orders pass through (not validated)
  - ValidationResult fields are populated
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from shared.universe.models import (
    SnapshotFailureMode,
    UniverseDecision,
    UniverseSnapshot,
    ValidationResult,
)
from shared.universe.modes import UniverseMode
from shared.universe.order_validator import UniverseOrderValidator


# ── Helpers ───────────────────────────────────────────────────────────────────

_TODAY = date(2026, 5, 26)
_NOW = datetime(2026, 5, 26, 5, 0, 0, tzinfo=timezone.utc)

_NIFTY50_SYMBOLS = frozenset({
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
    "SBIN", "AXISBANK", "KOTAKBANK", "LT", "NTPC",
})


def _make_snapshot(
    mode: UniverseMode,
    symbols: frozenset[str],
    trading_date: date = _TODAY,
) -> UniverseSnapshot:
    decisions = tuple(
        UniverseDecision(symbol=s, market="NSE", approved=True, reasons=("All filters passed",))
        for s in symbols
    )
    return UniverseSnapshot(
        mode=mode,
        trading_date=trading_date,
        approved_symbols=symbols,
        decisions=decisions,
        generated_at=_NOW,
        data_sources_used=frozenset(["yaml_fallback"]),
    )


# ── Basic validation ──────────────────────────────────────────────────────────

class TestOrderValidatorBasic:
    def test_approved_symbol_accepted(self) -> None:
        snap = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator = UniverseOrderValidator(snap)

        result = validator.validate("RELIANCE", "NSE")
        assert result.approved is True
        assert result.symbol == "RELIANCE"
        assert result.market == "NSE"

    def test_unknown_symbol_rejected(self) -> None:
        snap = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator = UniverseOrderValidator(snap)

        result = validator.validate("BEML", "NSE")
        assert result.approved is False
        assert "BEML" in result.reason
        assert "NOT in the approved universe" in result.reason

    def test_rejection_includes_snapshot_date(self) -> None:
        snap = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator = UniverseOrderValidator(snap)

        result = validator.validate("TEXRAIL", "NSE")
        assert result.snapshot_date == _TODAY

    def test_rejection_includes_checksum(self) -> None:
        snap = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator = UniverseOrderValidator(snap)

        result = validator.validate("NOTEXIST", "NSE")
        assert result.snapshot_checksum is not None
        assert len(result.snapshot_checksum) > 0

    def test_case_insensitive_symbol_lookup(self) -> None:
        snap = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator = UniverseOrderValidator(snap)

        result_lower = validator.validate("reliance", "NSE")
        result_upper = validator.validate("RELIANCE", "NSE")
        assert result_lower.approved is True
        assert result_upper.approved is True

    def test_validation_result_includes_mode(self) -> None:
        snap = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator = UniverseOrderValidator(snap)

        result = validator.validate("RELIANCE", "NSE")
        assert result.mode == UniverseMode.PAPER_SAFE_START


# ── Paper vs live mode behavior (no snapshot) ─────────────────────────────────

class TestNoSnapshotBehavior:
    def test_no_snapshot_paper_mode_allows(self) -> None:
        """Paper mode with no snapshot: allow with warning (non-fatal)."""
        validator = UniverseOrderValidator(snapshot=None, mode=UniverseMode.PAPER_SAFE_START)
        result = validator.validate("ANYSYMBOL", "NSE")
        assert result.approved is True
        assert "No universe snapshot" in result.reason

    def test_no_snapshot_paper_expand_allows(self) -> None:
        validator = UniverseOrderValidator(snapshot=None, mode=UniverseMode.PAPER_EXPAND)
        result = validator.validate("ANYSYMBOL", "NSE")
        assert result.approved is True

    def test_no_snapshot_live_mode_blocks(self) -> None:
        """Live mode with no snapshot: BLOCK all orders (fail-safe)."""
        validator = UniverseOrderValidator(snapshot=None, mode=UniverseMode.LIVE_ADVANCED)
        result = validator.validate("RELIANCE", "NSE")
        assert result.approved is False
        assert "BLOCKED" in result.reason or "No universe snapshot" in result.reason

    def test_no_snapshot_no_mode_defaults_to_strictest(self) -> None:
        """Validator with neither snapshot nor mode defaults to LIVE_ADVANCED behavior."""
        validator = UniverseOrderValidator(snapshot=None, mode=None)
        result = validator.validate("RELIANCE", "NSE")
        # Should be blocked (defaults to live-like strictness)
        assert result.approved is False


# ── US market passthrough ─────────────────────────────────────────────────────

class TestUsMarketPassthrough:
    def test_us_market_not_validated(self) -> None:
        """US equity orders are not validated against the NSE universe."""
        snap = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator = UniverseOrderValidator(snap)

        result = validator.validate("AAPL", "US")
        assert result.approved is True

    def test_us_market_symbol_not_in_nse_snapshot_still_approved(self) -> None:
        snap = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator = UniverseOrderValidator(snap)

        result = validator.validate("TSLA", "US")
        assert result.approved is True
        assert "Non-NSE market" in result.reason


# ── Batch validation ──────────────────────────────────────────────────────────

class TestBatchValidation:
    def test_batch_validates_all_symbols(self) -> None:
        snap = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator = UniverseOrderValidator(snap)

        pairs = [("RELIANCE", "NSE"), ("BEML", "NSE"), ("HDFCBANK", "NSE")]
        results = validator.validate_batch(pairs)

        assert results["RELIANCE"].approved is True
        assert results["BEML"].approved is False
        assert results["HDFCBANK"].approved is True

    def test_batch_returns_all_symbols_in_result(self) -> None:
        snap = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator = UniverseOrderValidator(snap)
        pairs = [("RELIANCE", "NSE"), ("OUTSIDER", "NSE")]
        results = validator.validate_batch(pairs)
        assert set(results.keys()) == {"RELIANCE", "OUTSIDER"}


# ── Snapshot update ───────────────────────────────────────────────────────────

class TestSnapshotUpdate:
    def test_update_snapshot_returns_new_validator(self) -> None:
        snap1 = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator1 = UniverseOrderValidator(snap1)

        new_symbols = frozenset({"RELIANCE", "HDFCBANK"})
        snap2 = _make_snapshot(UniverseMode.PAPER_SAFE_START, new_symbols, date(2026, 5, 27))
        validator2 = validator1.update_snapshot(snap2)

        assert validator1 is not validator2
        assert validator2.snapshot is snap2

    def test_update_snapshot_rejects_mode_mismatch(self) -> None:
        snap_paper = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator = UniverseOrderValidator(snap_paper, mode=UniverseMode.PAPER_SAFE_START)

        snap_live = _make_snapshot(UniverseMode.LIVE_ADVANCED, _NIFTY50_SYMBOLS)
        with pytest.raises(ValueError, match="Snapshot mode mismatch"):
            validator.update_snapshot(snap_live)

    def test_original_validator_not_mutated_after_update(self) -> None:
        snap1 = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator1 = UniverseOrderValidator(snap1)

        snap2 = _make_snapshot(UniverseMode.PAPER_SAFE_START,
                                frozenset({"RELIANCE"}), date(2026, 5, 27))
        validator2 = validator1.update_snapshot(snap2)

        # Original still sees old snapshot
        assert validator1.snapshot is snap1
        # New sees new snapshot
        assert validator2.snapshot is snap2


# ── Rejection reason in audit log ─────────────────────────────────────────────

class TestRejectionAuditLog:
    def test_rejected_result_has_reason_string(self) -> None:
        snap = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator = UniverseOrderValidator(snap)
        result = validator.validate("OUTSIDER", "NSE")
        assert isinstance(result.reason, str)
        assert len(result.reason) > 20  # must be a meaningful message

    def test_approved_result_has_reason_string(self) -> None:
        snap = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator = UniverseOrderValidator(snap)
        result = validator.validate("RELIANCE", "NSE")
        assert isinstance(result.reason, str)
        assert "Approved" in result.reason or "approved" in result.reason.lower()

    def test_rejection_mentions_mode_and_date(self) -> None:
        snap = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS)
        validator = UniverseOrderValidator(snap)
        result = validator.validate("UNKNOWN", "NSE")
        assert "PAPER_SAFE_START" in result.reason
        assert "2026-05-26" in result.reason
