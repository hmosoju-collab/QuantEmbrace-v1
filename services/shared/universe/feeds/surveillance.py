"""
NSE ASM / GSM surveillance list downloader.

NSE publishes the Additional Surveillance Measure (ASM) and Graded Surveillance
Measure (GSM) lists on its website. This module fetches them via the NSE API and
caches the result for the trading day.

ASM stages 1–4: short-term volatility surveillance.
GSM stages 1–6: fundamental/price-anomaly graded surveillance (stricter).

Any symbol on either list is excluded from the trading universe.

API paths (verified 2026-05; NSE may rename without notice):
  ASM: GET /api/asm
  GSM: GET /api/gsm

Expected response shape:
  {"asmList": [{"symbol": "XYZ", "stage": 1, ...}, ...]}
  or a bare list — the fetcher handles both.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from shared.universe.feeds.nse_client import NseHttpClient

logger = logging.getLogger(__name__)

_ASM_PATH = "/api/asm"
_GSM_PATH = "/api/gsm"


def _extract_symbols(payload: object) -> set[str]:
    """Pull symbol strings from NSE ASM/GSM response (handles list or dict)."""
    records: list[object] = (
        payload if isinstance(payload, list)
        else payload.get("asmList", payload.get("gsmList", []))  # type: ignore[union-attr]
    )
    symbols: set[str] = set()
    for entry in records:
        if not isinstance(entry, dict):
            continue
        sym = (entry.get("symbol") or entry.get("SYMBOL") or "").strip().upper()
        if sym:
            symbols.add(sym)
    return symbols


class SurveillanceFetcher:
    """
    Fetches NSE ASM and GSM surveillance symbol lists.

    Results are cached per trading date (one fetch per day per list).

    Args:
        nse_client: An NseHttpClient instance used for API calls.
    """

    def __init__(self, nse_client: "NseHttpClient") -> None:
        self._client = nse_client
        self._asm_cache: Optional[tuple[date, set[str]]] = None
        self._gsm_cache: Optional[tuple[date, set[str]]] = None

    def _today(self) -> date:
        return datetime.now(timezone.utc).date()

    def get_asm_symbols(self) -> set[str]:
        """
        Return the set of NSE symbols currently on the ASM list.

        The result is cached for the calendar day; call once per trading session.

        Raises:
            ConnectionError: If the NSE ASM endpoint is unreachable.
        """
        today = self._today()
        if self._asm_cache is not None and self._asm_cache[0] == today:
            return self._asm_cache[1]

        logger.info("surveillance.fetch_asm date=%s", today)
        try:
            payload = self._client.get_json(_ASM_PATH)
        except Exception as exc:
            raise ConnectionError(f"NSE ASM API unavailable: {exc}") from exc

        symbols = _extract_symbols(payload)
        self._asm_cache = (today, symbols)
        logger.info("surveillance.asm_ok date=%s count=%d", today, len(symbols))
        return symbols

    def get_gsm_symbols(self) -> set[str]:
        """
        Return the set of NSE symbols currently on the GSM list.

        The result is cached for the calendar day.

        Raises:
            ConnectionError: If the NSE GSM endpoint is unreachable.
        """
        today = self._today()
        if self._gsm_cache is not None and self._gsm_cache[0] == today:
            return self._gsm_cache[1]

        logger.info("surveillance.fetch_gsm date=%s", today)
        try:
            payload = self._client.get_json(_GSM_PATH)
        except Exception as exc:
            raise ConnectionError(f"NSE GSM API unavailable: {exc}") from exc

        symbols = _extract_symbols(payload)
        self._gsm_cache = (today, symbols)
        logger.info("surveillance.gsm_ok date=%s count=%d", today, len(symbols))
        return symbols
