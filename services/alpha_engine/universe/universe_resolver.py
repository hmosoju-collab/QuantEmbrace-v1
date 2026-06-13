"""UniverseResolver — maps an NSE symbol to its index universe (ADR-031 rev 2).

Per-universe IC is one of the headline research outputs (e.g. "VWAP works in
NIFTY50 but not MIDCAP"). v1 resolves offline from the curated
``configs/universe_modes.yaml`` index lists (no network): NIFTY50 and NIFTYNEXT50
are covered; MIDCAP100/SMALLCAP require the live CSV feed and resolve to UNKNOWN
until that is wired. Membership is cached per IST trading day.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import os
from pathlib import Path

import yaml

from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="alpha_engine")

_IST = timezone(timedelta(hours=5, minutes=30))

# Map the YAML index keys to the canonical universe buckets (shared.models.alpha).
_INDEX_TO_UNIVERSE: list[tuple[str, str]] = [
    ("NIFTY_50", "NIFTY50"),
    ("NIFTY_NEXT_50", "NIFTYNEXT50"),
]


def _default_config_path() -> Path:
    env = os.environ.get("ALPHA_UNIVERSE_CONFIG")
    if env:
        return Path(env)
    # services/alpha_engine/universe/universe_resolver.py -> project root is parents[3]
    return Path(__file__).resolve().parents[3] / "configs" / "universe_modes.yaml"


class UniverseResolver:
    def __init__(self, *, config_path: str | Path | None = None, now=None) -> None:
        self._config_path = Path(config_path) if config_path else _default_config_path()
        self._now = now or (lambda: datetime.now(UTC))
        self._cache_date: str | None = None
        self._map: dict[str, str] = {}

    def resolve(self, symbol: str) -> str:
        self._ensure_loaded()
        return self._map.get(symbol.upper(), "UNKNOWN")

    def _ensure_loaded(self) -> None:
        today = self._now().astimezone(_IST).date().isoformat()
        if self._cache_date == today and self._map:
            return
        self._map = self._load()
        self._cache_date = today

    def _load(self) -> dict[str, str]:
        if not self._config_path.exists():
            logger.warning(
                "alpha_engine.universe_config_missing path=%s — all symbols UNKNOWN",
                self._config_path,
            )
            return {}
        try:
            data = yaml.safe_load(self._config_path.read_text()) or {}
        except Exception:
            logger.exception("alpha_engine.universe_config_unreadable path=%s", self._config_path)
            return {}

        index_symbols = data.get("index_symbols", {})
        mapping: dict[str, str] = {}
        # First match wins (NIFTY50 before NIFTYNEXT50), so a NIFTY50 name is never
        # downgraded if it also appears in a broader list.
        for index_key, universe in _INDEX_TO_UNIVERSE:
            for sym in index_symbols.get(index_key, {}).get("symbols", []) or []:
                mapping.setdefault(str(sym).upper(), universe)
        logger.info("alpha_engine.universe_loaded count=%d", len(mapping))
        return mapping
