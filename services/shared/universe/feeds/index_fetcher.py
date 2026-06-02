"""
NSE index constituent fetcher.

Fetches the current members of major NSE indices from the NSE public API.
Index composition is rebalanced quarterly for NIFTY 50/100/200/500 and
updated periodically for the F&O eligible list.

API path: /api/equity-stockIndices?index={URL-encoded index name}

Response shape:
  {"data": [{"symbol": "RELIANCE", "meta": {...}}, ...], "metadata": {...}}

Internal config names → NSE API index labels:
  NIFTY_50         → "NIFTY 50"
  NIFTY_NEXT_50    → "NIFTY NEXT 50"
  NIFTY_100        → "NIFTY 100"
  NIFTY_200        → "NIFTY 200"
  NIFTY_500        → "NIFTY 500"
  NIFTY_MIDCAP_100 → "NIFTY MIDCAP 100"
  FNO              → "SECURITIES IN F&O"
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote

if TYPE_CHECKING:
    from shared.universe.feeds.nse_client import NseHttpClient

logger = logging.getLogger(__name__)

_INDEX_API_PATH = "/api/equity-stockIndices?index={encoded}"

# Maps internal YAML config keys → NSE API index name strings.
# Extend here when adding new indices to universe_modes.yaml.
_CONFIG_TO_NSE: dict[str, str] = {
    "NIFTY_50":          "NIFTY 50",
    "NIFTY_NEXT_50":     "NIFTY NEXT 50",
    "NIFTY_100":         "NIFTY 100",
    "NIFTY_200":         "NIFTY 200",
    "NIFTY_500":         "NIFTY 500",
    "NIFTY_MIDCAP_100":  "NIFTY MIDCAP 100",
    "NIFTY_SMALLCAP_100": "NIFTY SMALLCAP 100",
    "FNO":               "SECURITIES IN F&O",
}


class IndexConstituentFetcher:
    """
    Fetches NSE index constituent lists from the NSE equity-stockIndices API.

    Results are cached per (index_name, trading_date). The NSE index composition
    only changes at quarterly rebalance; once fetched for a trading day it is
    stable for the rest of that day.

    Args:
        nse_client: An NseHttpClient instance for API calls.
    """

    def __init__(self, nse_client: "NseHttpClient") -> None:
        self._client = nse_client
        self._cache: dict[tuple[str, date], set[str]] = {}

    def _today(self) -> date:
        return datetime.now(timezone.utc).date()

    def get_index_symbols(self, index_name: str) -> set[str]:
        """
        Return the set of NSE symbol strings in the given index.

        Args:
            index_name: Config-side name (e.g. "NIFTY_50", "FNO").

        Returns:
            Set of uppercase NSE symbol strings. Empty set if unrecognised.

        Raises:
            ConnectionError: If the NSE API is unreachable.
            ValueError: If index_name has no mapping to an NSE API label.
        """
        today = self._today()
        cache_key = (index_name, today)
        if cache_key in self._cache:
            return self._cache[cache_key]

        nse_label = _CONFIG_TO_NSE.get(index_name)
        if nse_label is None:
            logger.warning(
                "index_fetcher.unknown_index config_name=%s — no NSE label mapping; "
                "returning empty (add to _CONFIG_TO_NSE if this is a new index)",
                index_name,
            )
            return set()

        path = _INDEX_API_PATH.format(encoded=quote(nse_label))
        logger.info(
            "index_fetcher.fetch config=%s nse=%s", index_name, nse_label
        )

        try:
            payload = self._client.get_json(path)
        except Exception as exc:
            raise ConnectionError(
                f"NSE index API unavailable for '{index_name}' ({nse_label}): {exc}"
            ) from exc

        symbols: set[str] = set()
        for entry in payload.get("data", []):
            sym = (entry.get("symbol") or entry.get("SYMBOL") or "").strip().upper()
            if sym:
                symbols.add(sym)

        self._cache[cache_key] = symbols
        logger.info(
            "index_fetcher.ok config=%s nse=%s count=%d", index_name, nse_label, len(symbols)
        )
        return symbols
