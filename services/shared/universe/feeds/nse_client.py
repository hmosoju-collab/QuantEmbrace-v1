"""
NseHttpClient — managed HTTP session for the NSE public API.

NSE's interactive API (equity-stockIndices, asm, gsm, corporateActions) requires
a valid session cookie that can only be obtained by visiting the homepage first.
This client handles that transparently and retries once on session expiry.

Rate limit: NSE blocks rapid-fire requests. Keep min_request_gap >= 1.0s.

NSE occasionally changes endpoint paths and response schemas. When an endpoint
breaks, update the relevant feed module and retest — do not change this file.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

_NSE_BASE = "https://www.nseindia.com"

# Mimics a real browser session — NSE returns 401/empty JSON without these.
_SESSION_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "keep-alive",
}


class NseHttpClient:
    """
    HTTP client for the NSE public website API.

    Establishes a session by visiting the NSE homepage (sets auth cookies),
    then reuses that session for all API calls. Refreshes automatically when
    the session approaches expiry or a 401/403 is received.

    All methods are synchronous — this client is called once at universe build
    time (pre-market), not on the trading hot path.

    Args:
        session_ttl_seconds: How long to treat a session as valid before refreshing.
                             NSE sessions appear to last ~5 minutes in practice.
        request_timeout:     Per-request timeout in seconds.
        min_request_gap:     Minimum seconds between consecutive requests.
    """

    def __init__(
        self,
        session_ttl_seconds: int = 270,
        request_timeout: float = 15.0,
        min_request_gap: float = 1.1,
    ) -> None:
        self._session = requests.Session()
        self._session.headers.update(_SESSION_HEADERS)
        self._ttl = session_ttl_seconds
        self._timeout = request_timeout
        self._gap = min_request_gap
        self._valid_until: float = 0.0
        self._last_request_at: float = 0.0

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _throttle(self) -> None:
        """Enforce minimum gap between requests."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._gap:
            time.sleep(self._gap - elapsed)

    def _refresh_session(self) -> None:
        """Visit NSE homepage to obtain fresh session cookies."""
        logger.info("nse_client.session_refresh — visiting homepage for cookies")
        self._throttle()
        try:
            self._session.get(_NSE_BASE, timeout=self._timeout)
            self._last_request_at = time.monotonic()
            self._valid_until = time.monotonic() + self._ttl
            logger.debug(
                "nse_client.session_ok cookies=%d", len(self._session.cookies)
            )
        except Exception as exc:
            logger.error("nse_client.session_refresh_failed error=%s", exc)
            raise ConnectionError(f"Cannot establish NSE session: {exc}") from exc

    def _is_session_valid(self) -> bool:
        return time.monotonic() < self._valid_until

    # ── Public API ────────────────────────────────────────────────────────────

    def get_json(self, path: str) -> Any:
        """
        Fetch JSON from an NSE API path.

        Args:
            path: URL path relative to NSE base URL, e.g.
                  "/api/equity-stockIndices?index=NIFTY%2050"

        Returns:
            Parsed JSON (dict or list).

        Raises:
            ConnectionError: If the request fails after one session retry.
            requests.HTTPError: On non-retryable HTTP errors.
        """
        if not self._is_session_valid():
            self._refresh_session()

        url = f"{_NSE_BASE}{path}"
        logger.debug("nse_client.get url=%s", url)

        self._throttle()
        try:
            resp = self._session.get(url, timeout=self._timeout)
            self._last_request_at = time.monotonic()
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status in (401, 403):
                # Session cookie expired mid-flight — refresh and retry once.
                logger.warning(
                    "nse_client.session_expired status=%d path=%s — refreshing and retrying",
                    status, path,
                )
                self._refresh_session()
                self._throttle()
                resp = self._session.get(url, timeout=self._timeout)
                self._last_request_at = time.monotonic()
                resp.raise_for_status()
                return resp.json()
            raise
        except requests.RequestException as exc:
            raise ConnectionError(f"NSE API request failed for {path}: {exc}") from exc
