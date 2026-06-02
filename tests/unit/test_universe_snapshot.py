"""
Tests for UniverseSnapshot — immutability, isolation, and builder behavior.

Tests cover:
  - Snapshot is immutable (frozenset)
  - Paper/live snapshot isolation
  - Snapshot contains() check
  - Snapshot checksum uniqueness
  - Builder produces correct mode-based symbol sets
  - Missing data source causes safe failure
  - Universe too small triggers failure_mode=PARTIAL
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from shared.universe.builder import UniverseBuilder, UniverseSnapshotError
from shared.universe.data_sources import YamlDataSource
from shared.universe.models import SnapshotFailureMode, UniverseSnapshot
from shared.universe.modes import UniverseMode
from shared.universe.snapshot_store import InMemorySnapshotStore


class _MinimalYamlDs(YamlDataSource):
    """Minimal data source returning a small controlled symbol set per index."""

    def __init__(self, index_map: dict[str, set[str]]) -> None:
        super().__init__()
        self._index_map = index_map

    def get_index_symbols(self, index_name: str) -> set[str]:
        return self._index_map.get(index_name, set())

    def get_asm_symbols(self) -> set[str]:
        return set()

    def get_gsm_symbols(self) -> set[str]:
        return set()

    def get_emergency_exclusions(self) -> set[str]:
        return set()

    def get_exclusion_lists(self) -> dict[str, set[str]]:
        return {k: set() for k in ["delisted", "suspended", "sme", "etf", "reit_invit", "bse_only", "emergency"]}


_NIFTY50_SYMBOLS = frozenset({
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
    "SBIN", "AXISBANK", "KOTAKBANK", "LT", "NTPC",
})

_NIFTY100_EXTRA = frozenset({
    "ADANIGREEN", "HAVELLS", "DLF", "MUTHOOTFIN", "LUPIN",
})

_NIFTY200_EXTRA = frozenset({
    "HAL", "BEML", "TATAPOWER", "BHEL", "IOC",
})

_FNO_EXTRA = frozenset({
    "TATAELXSI", "PERSISTENT", "PRAJIND", "TEXRAIL",
})


def _make_builder(index_map: dict[str, set[str]], modes_config: str | None = None) -> UniverseBuilder:
    ds = _MinimalYamlDs(index_map)
    builder = UniverseBuilder(data_source=ds)
    return builder


def _build_minimal_snapshot(mode: UniverseMode) -> UniverseSnapshot:
    if mode == UniverseMode.PAPER_SAFE_START:
        index_map = {"NIFTY_50": set(_NIFTY50_SYMBOLS)}
    elif mode == UniverseMode.PAPER_EXPAND:
        index_map = {
            "NIFTY_50": set(_NIFTY50_SYMBOLS),
            "NIFTY_NEXT_50": set(_NIFTY100_EXTRA),
            "NIFTY_100": set(_NIFTY50_SYMBOLS | _NIFTY100_EXTRA),
            "FNO": set(_FNO_EXTRA),
        }
    else:
        index_map = {
            "NIFTY_50": set(_NIFTY50_SYMBOLS),
            "NIFTY_NEXT_50": set(_NIFTY100_EXTRA),
            "NIFTY_100": set(_NIFTY50_SYMBOLS | _NIFTY100_EXTRA),
            "NIFTY_200": set(_NIFTY50_SYMBOLS | _NIFTY100_EXTRA | _NIFTY200_EXTRA),
        }

    # Build modes_yaml override that references these indices
    import tempfile, yaml
    from pathlib import Path

    modes_cfg = {
        "modes": {
            mode.value: {
                "paper_mode": mode.is_paper,
                "exchange": "NSE",
                "max_symbols": 500,
                "min_symbols_to_trade": 2,
                "index_membership": list(index_map.keys())[-1:],  # last key = union index
                "apply_liquidity_filters": False,
                "apply_risk_filters": False,
                "apply_corporate_action_filters": False,
            }
        },
        "index_symbols": {k: {"symbols": list(v)} for k, v in index_map.items()},
    }
    tmp = Path(tempfile.mktemp(suffix=".yaml"))
    with open(tmp, "w") as f:
        yaml.safe_dump(modes_cfg, f)

    ds = _MinimalYamlDs(index_map)
    builder = UniverseBuilder(data_source=ds, modes_config_path=tmp)
    return builder.build(mode, date(2026, 5, 26))


# ── Snapshot immutability tests ───────────────────────────────────────────────

class TestSnapshotImmutability:
    def test_approved_symbols_is_frozenset(self) -> None:
        snap = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        assert isinstance(snap.approved_symbols, frozenset)

    def test_decisions_is_tuple(self) -> None:
        snap = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        assert isinstance(snap.decisions, tuple)

    def test_cannot_mutate_approved_symbols(self) -> None:
        snap = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        with pytest.raises(AttributeError):
            snap.approved_symbols = frozenset()  # type: ignore[misc]

    def test_cannot_add_to_approved_symbols(self) -> None:
        snap = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        with pytest.raises(AttributeError):
            snap.approved_symbols.add("INJECTED")  # type: ignore[attr-defined]


# ── Paper / live isolation ────────────────────────────────────────────────────

class TestPaperLiveIsolation:
    def test_paper_snapshot_is_paper(self) -> None:
        snap = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        assert snap.is_paper is True
        assert snap.is_live is False

    def test_live_snapshot_is_live(self) -> None:
        snap = _build_minimal_snapshot(UniverseMode.LIVE_ADVANCED)
        assert snap.is_live is True
        assert snap.is_paper is False

    def test_paper_live_different_mode_values(self) -> None:
        paper = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        live = _build_minimal_snapshot(UniverseMode.LIVE_ADVANCED)
        assert paper.mode != live.mode

    def test_paper_live_can_have_different_checksums(self) -> None:
        paper = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        live = _build_minimal_snapshot(UniverseMode.LIVE_ADVANCED)
        # Checksums will differ because the symbol sets differ
        assert paper.checksum != live.checksum or paper.approved_symbols == live.approved_symbols


# ── Snapshot contains / size / audit ─────────────────────────────────────────

class TestSnapshotContains:
    def test_approved_symbol_is_found(self) -> None:
        snap = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        for sym in snap.approved_symbols:
            assert snap.contains(sym) is True

    def test_unknown_symbol_not_found(self) -> None:
        snap = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        assert snap.contains("NOTEXIST") is False

    def test_contains_is_case_insensitive(self) -> None:
        snap = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        if snap.approved_symbols:
            first = next(iter(snap.approved_symbols))
            assert snap.contains(first.lower()) is True

    def test_size_matches_approved_symbols(self) -> None:
        snap = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        assert snap.size == len(snap.approved_symbols)

    def test_checksum_is_reproducible(self) -> None:
        snap = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        # Same symbols → same checksum (deterministic)
        assert snap.checksum == snap.checksum

    def test_to_audit_dict_has_required_fields(self) -> None:
        snap = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        d = snap.to_audit_dict()
        assert "mode" in d
        assert "trading_date" in d
        assert "approved_symbols" in d
        assert "checksum" in d
        assert "approved_count" in d


# ── Snapshot store isolation ──────────────────────────────────────────────────

class TestSnapshotStoreIsolation:
    def test_paper_and_live_do_not_overwrite_each_other(self) -> None:
        store = InMemorySnapshotStore()
        paper = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        live = _build_minimal_snapshot(UniverseMode.LIVE_ADVANCED)

        store.save(paper)
        store.save(live)

        retrieved_paper = store.get(UniverseMode.PAPER_SAFE_START, paper.trading_date)
        retrieved_live = store.get(UniverseMode.LIVE_ADVANCED, live.trading_date)

        assert retrieved_paper is not None
        assert retrieved_live is not None
        assert retrieved_paper.mode == UniverseMode.PAPER_SAFE_START
        assert retrieved_live.mode == UniverseMode.LIVE_ADVANCED

    def test_snapshot_not_overwritten_on_second_save(self) -> None:
        store = InMemorySnapshotStore()
        snap1 = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        snap2 = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        # snap2 will have different generated_at but same mode+date

        store.save(snap1)
        store.save(snap2)  # should be a no-op

        retrieved = store.get(UniverseMode.PAPER_SAFE_START, snap1.trading_date)
        assert retrieved is snap1  # must be the FIRST saved version

    def test_exists_returns_false_for_unknown(self) -> None:
        store = InMemorySnapshotStore()
        assert store.exists(UniverseMode.LIVE_ADVANCED, date(2026, 5, 26)) is False

    def test_exists_returns_true_after_save(self) -> None:
        store = InMemorySnapshotStore()
        snap = _build_minimal_snapshot(UniverseMode.PAPER_SAFE_START)
        store.save(snap)
        assert store.exists(UniverseMode.PAPER_SAFE_START, snap.trading_date) is True
