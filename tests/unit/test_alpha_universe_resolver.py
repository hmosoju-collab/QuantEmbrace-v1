"""Unit tests for the Alpha Engine UniverseResolver (ADR-031, P3)."""

from __future__ import annotations

from datetime import UTC, datetime
import os
import sys

import yaml

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from alpha_engine.universe.universe_resolver import UniverseResolver  # noqa: E402

_CONFIG = {
    "index_symbols": {
        "NIFTY_50": {"symbols": ["RELIANCE", "TCS", "INFY"]},
        "NIFTY_NEXT_50": {"symbols": ["DLF", "ZOMATO", "TCS"]},  # TCS overlaps -> NIFTY50 wins
    }
}


def _resolver(tmp_path) -> UniverseResolver:
    path = tmp_path / "universe_modes.yaml"
    path.write_text(yaml.safe_dump(_CONFIG))
    return UniverseResolver(config_path=str(path))


def test_nifty50_member_resolves(tmp_path):
    r = _resolver(tmp_path)
    assert r.resolve("RELIANCE") == "NIFTY50"
    assert r.resolve("infy") == "NIFTY50"  # case-insensitive


def test_next50_member_resolves(tmp_path):
    assert _resolver(tmp_path).resolve("DLF") == "NIFTYNEXT50"


def test_overlap_prefers_nifty50(tmp_path):
    assert _resolver(tmp_path).resolve("TCS") == "NIFTY50"


def test_unknown_symbol(tmp_path):
    assert _resolver(tmp_path).resolve("SOMERANDOMCO") == "UNKNOWN"


def test_missing_config_returns_unknown(tmp_path):
    r = UniverseResolver(config_path=str(tmp_path / "does_not_exist.yaml"))
    assert r.resolve("RELIANCE") == "UNKNOWN"


def test_membership_is_cached_per_day(tmp_path):
    path = tmp_path / "universe_modes.yaml"
    path.write_text(yaml.safe_dump(_CONFIG))
    r = UniverseResolver(config_path=str(path), now=lambda: datetime(2026, 6, 15, tzinfo=UTC))
    assert r.resolve("RELIANCE") == "NIFTY50"
    # Overwrite the file with empty membership; same day -> cached, still NIFTY50.
    path.write_text(yaml.safe_dump({"index_symbols": {}}))
    assert r.resolve("RELIANCE") == "NIFTY50"
