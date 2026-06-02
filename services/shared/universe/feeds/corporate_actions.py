"""
NSE corporate action event fetcher.

Fetches pending and recent corporate actions for NSE-listed equities via
the NSE public API. Used by CorporateActionFilter to exclude symbols within
the post-event exclusion window (e.g. 5 trading days after a stock split).

API path: /api/corporates-corporateActions?index=equities&symbol={SYMBOL}

Response: list of records with ex_date, purpose, and other metadata.

Purpose string → CorporateActionType mapping handles the most common NSE
announcement formats. Unrecognised purposes are silently ignored (the filter
only acts on events it can classify).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from shared.universe.models import CorporateActionEvent, CorporateActionType

if TYPE_CHECKING:
    from shared.universe.feeds.nse_client import NseHttpClient

logger = logging.getLogger(__name__)

_CA_PATH = "/api/corporates-corporateActions?index=equities&symbol={symbol}"

# Maps NSE purpose string fragments (uppercase) → CorporateActionType.
# Order matters: more specific strings must appear before their substrings.
_PURPOSE_MAP: list[tuple[str, CorporateActionType]] = [
    ("FACE VALUE SPLIT",    CorporateActionType.STOCK_SPLIT),
    ("STOCK SPLIT",         CorporateActionType.STOCK_SPLIT),
    ("SPLIT",               CorporateActionType.STOCK_SPLIT),
    ("BONUS",               CorporateActionType.BONUS_ISSUE),
    ("RIGHTS",              CorporateActionType.RIGHTS_ISSUE),
    ("AMALGAMATION",        CorporateActionType.MERGER),
    ("MERGER",              CorporateActionType.MERGER),
    ("SCHEME OF ARRANGEMENT", CorporateActionType.DEMERGER),
    ("DEMERGER",            CorporateActionType.DEMERGER),
    ("SUSPENSION",          CorporateActionType.SUSPENSION),
    ("DELISTING",           CorporateActionType.DELISTING),
    ("TRADING HALT",        CorporateActionType.TRADING_HALT),
    # Dividends last — we don't filter on dividends, but capture them for completeness.
    ("INTERIM DIVIDEND",    CorporateActionType.DIVIDEND),
    ("FINAL DIVIDEND",      CorporateActionType.DIVIDEND),
    ("SPECIAL DIVIDEND",    CorporateActionType.DIVIDEND),
    ("DIVIDEND",            CorporateActionType.DIVIDEND),
]

# NSE date formats observed in corporate action records
_DATE_FORMATS = ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")


def _parse_nse_date(s: str) -> Optional[date]:
    s = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _classify_purpose(purpose: str) -> Optional[CorporateActionType]:
    upper = purpose.upper()
    for fragment, ca_type in _PURPOSE_MAP:
        if fragment in upper:
            return ca_type
    return None


class CorporateActionFetcher:
    """
    Fetches corporate action events for NSE-listed symbols.

    Results are cached per symbol per trading day. During universe build,
    each candidate symbol is queried once; subsequent calls for the same
    (symbol, date) are instant.

    Args:
        nse_client: An NseHttpClient instance for API calls.
    """

    def __init__(self, nse_client: "NseHttpClient") -> None:
        self._client = nse_client
        self._cache: dict[tuple[str, date], list[CorporateActionEvent]] = {}

    def _today(self) -> date:
        return datetime.now(timezone.utc).date()

    def get_corporate_actions(
        self,
        symbol: str,
        as_of: date,
        window_days: int = 60,
    ) -> list[CorporateActionEvent]:
        """
        Return corporate action events for a symbol within a look-back/look-ahead window.

        Args:
            symbol:      NSE symbol (case-insensitive).
            as_of:       Reference date for window calculation.
            window_days: Days before and after as_of to include.

        Returns:
            List of CorporateActionEvent sorted by ex_date descending.

        Raises:
            ConnectionError: If the NSE API is unreachable.
        """
        sym = symbol.upper()
        today = self._today()
        cache_key = (sym, today)

        if cache_key in self._cache:
            return self._cache[cache_key]

        path = _CA_PATH.format(symbol=sym)
        logger.debug("corporate_actions.fetch symbol=%s", sym)

        try:
            payload = self._client.get_json(path)
        except Exception as exc:
            raise ConnectionError(
                f"NSE corporate actions API unavailable for {sym}: {exc}"
            ) from exc

        records: list[object] = (
            payload if isinstance(payload, list)
            else payload.get("data", [])
        )

        cutoff_past = as_of - timedelta(days=window_days)
        cutoff_future = as_of + timedelta(days=window_days)
        events: list[CorporateActionEvent] = []

        for record in records:
            if not isinstance(record, dict):
                continue
            # ex_date field varies across NSE API versions
            ex_str = (
                record.get("exDate") or record.get("ex_date")
                or record.get("exdate") or record.get("EX_DATE") or ""
            )
            ex_date = _parse_nse_date(ex_str)
            if ex_date is None:
                continue
            if not (cutoff_past <= ex_date <= cutoff_future):
                continue

            purpose = (
                record.get("purpose") or record.get("action")
                or record.get("subject") or record.get("PURPOSE") or ""
            ).strip()
            ca_type = _classify_purpose(purpose)
            if ca_type is None:
                continue

            events.append(CorporateActionEvent(
                symbol=sym,
                action_type=ca_type,
                ex_date=ex_date,
                announced_at=None,
                details=purpose,
                data_source="nse_api",
            ))

        events.sort(key=lambda e: e.ex_date, reverse=True)
        self._cache[cache_key] = events
        logger.debug(
            "corporate_actions.ok symbol=%s count=%d", sym, len(events)
        )
        return events
