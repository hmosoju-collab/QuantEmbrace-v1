"""
NseCsvFeed — fetches NSE index constituents and F&O eligibility from
NSE archive CSV files (no session cookies required).

Archive URLs are stable public files updated daily by NSE:
  indices  → https://archives.nseindia.com/content/indices/ind_{name}list.csv
  fo_lots  → https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv

Results are cached per trading date so the network hit happens once per day
at universe build time, not on the trading hot path.

Fallback: if a download fails, returns an empty set and logs a warning.
The caller (NseCsvDataSource) then falls back to the YAML source.
"""

from __future__ import annotations

import csv
import io
import logging
import time
import urllib.request
from datetime import date, datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_TIMEOUT = 15.0
_REQUEST_GAP = 0.5  # seconds between consecutive downloads

# Config name → NSE archive CSV URL
_INDEX_CSV_URLS: dict[str, str] = {
    "NIFTY_50":          "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTY_NEXT_50":     "https://archives.nseindia.com/content/indices/ind_niftynext50list.csv",
    "NIFTY_100":         "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "NIFTY_200":         "https://archives.nseindia.com/content/indices/ind_nifty200list.csv",
    "NIFTY_500":         "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "NIFTY_MIDCAP_100":  "https://archives.nseindia.com/content/indices/ind_niftymidcap100list.csv",
    "NIFTY_SMALLCAP_100": "https://archives.nseindia.com/content/indices/ind_niftysmallcap100list.csv",
}
_FNO_LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"

# Index/derivative symbols present in the F&O lots file that are NOT equity stocks.
_FNO_NON_EQUITY = frozenset({
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
    "SENSEX", "BANKEX", "VIX", "INDIAVIX",
})


def _fetch_csv(url: str) -> Optional[str]:
    """Download a CSV URL and return body text, or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8-sig", errors="replace")
    except Exception as exc:
        logger.warning("csv_feed.download_failed url=%s error=%s", url, exc)
        return None


def _parse_index_csv(text: str) -> set[str]:
    """Parse NSE index constituent CSV (columns: Company Name, Industry, Symbol, …)."""
    symbols: set[str] = set()
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        sym = (row.get("Symbol") or row.get("SYMBOL") or "").strip().upper()
        if sym and not sym.startswith("DUMMY"):
            symbols.add(sym)
    return symbols


def _parse_fno_csv(text: str) -> set[str]:
    """Parse NSE fo_mktlots.csv. Column 1 (index 1) is SYMBOL."""
    symbols: set[str] = set()
    lines = text.splitlines()
    for row in csv.reader(lines[1:]):  # skip header
        if len(row) < 2:
            continue
        sym = row[1].strip().upper()
        if sym and sym not in _FNO_NON_EQUITY and sym != "SYMBOL":
            symbols.add(sym)
    return symbols


class NseCsvFeed:
    """
    Downloads NSE index/F&O constituent data from public archive CSVs.

    Results are cached per (index_name, trading_date). The index composition
    only changes at quarterly rebalance; once fetched it is stable for the day.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, date], set[str]] = {}
        self._last_request_at: float = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _REQUEST_GAP:
            time.sleep(_REQUEST_GAP - elapsed)

    def _today(self) -> date:
        return datetime.now(timezone.utc).date()

    def get_index_symbols(self, index_name: str) -> set[str]:
        """Return live NSE index constituents. Empty set on failure (caller uses YAML)."""
        today = self._today()
        key = (index_name, today)
        if key in self._cache:
            return self._cache[key]

        if index_name == "FNO":
            return self.get_fno_symbols()

        url = _INDEX_CSV_URLS.get(index_name)
        if not url:
            logger.warning("csv_feed.unknown_index index=%s — no CSV URL mapped", index_name)
            return set()

        self._throttle()
        self._last_request_at = time.monotonic()
        text = _fetch_csv(url)
        if text is None:
            return set()

        symbols = _parse_index_csv(text)
        self._cache[key] = symbols
        logger.info("csv_feed.index_ok index=%s count=%d", index_name, len(symbols))
        return symbols

    def get_fno_symbols(self) -> set[str]:
        """Return live F&O eligible equity symbols."""
        today = self._today()
        key = ("FNO", today)
        if key in self._cache:
            return self._cache[key]

        self._throttle()
        self._last_request_at = time.monotonic()
        text = _fetch_csv(_FNO_LOTS_URL)
        if text is None:
            return set()

        symbols = _parse_fno_csv(text)
        self._cache[key] = symbols
        logger.info("csv_feed.fno_ok count=%d", len(symbols))
        return symbols
