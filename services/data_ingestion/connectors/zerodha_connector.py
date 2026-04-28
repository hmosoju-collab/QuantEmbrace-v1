"""
Zerodha Kite Connect WebSocket connector for NSE India real-time market data.

Uses KiteTicker (kiteconnect library) which runs a dedicated background thread.
All tick callbacks are dispatched back to the asyncio event loop using
asyncio.run_coroutine_threadsafe() so the rest of the pipeline stays async.

Instrument token mapping:
    Kite Ticker works with integer "instrument tokens", not human-readable
    symbols. On startup we fetch the full NSE instrument list from the Kite
    REST API and cache it locally (refreshed every 6 hours). Subsequent
    restarts use the cached file to avoid hammering the API.

Install: pip install kiteconnect
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from shared.logging.logger import get_logger

from data_ingestion.connectors.base import (
    BaseConnector,
    Market,
    NormalizedTick,
    TickCallback,
)

logger = get_logger(__name__, service_name="data_ingestion")

# ── Optional dependency guard ─────────────────────────────────────────────────
# The file is importable without kiteconnect installed; failures surface at
# connect() time with a clear message.
try:
    from kiteconnect import KiteConnect, KiteTicker  # type: ignore[import]
    _KITECONNECT_AVAILABLE = True
except ImportError:
    _KITECONNECT_AVAILABLE = False
    KiteConnect = None  # type: ignore[assignment]
    KiteTicker = None   # type: ignore[assignment]

# ── Instrument cache config ───────────────────────────────────────────────────
_CACHE_DIR = Path(os.environ.get("INSTRUMENT_CACHE_DIR", "/tmp"))
_CACHE_TTL_HOURS = 6   # Re-fetch instrument list every 6 hours


class ZerodhaConnector(BaseConnector):
    """
    Zerodha Kite Ticker WebSocket connector for real-time NSE market data.

    Thread model:
        KiteTicker runs a dedicated OS thread internally. Tick callbacks
        originate from that thread. We bridge back to the asyncio event
        loop via ``asyncio.run_coroutine_threadsafe()``.

    Reconnection:
        KiteTicker handles reconnection automatically (exponential backoff).
        On reconnect the ``_on_connect_callback`` fires again; we re-subscribe
        the token list so the stream is restored without service restart.

    Usage::

        connector = ZerodhaConnector(
            api_key="your_api_key",
            access_token="session_access_token",  # refreshed daily
            on_tick=async_handler,
        )
        await connector.connect()
        await connector.subscribe(["RELIANCE", "TCS", "INFY"])
        # ticks now flow to async_handler
    """

    def __init__(
        self,
        api_key: str,
        access_token: str,
        on_tick: Optional[TickCallback] = None,
    ) -> None:
        """
        Initialize the Zerodha connector.

        Args:
            api_key: Zerodha Kite Connect API key.
            access_token: Daily session access token (expires ~06:00 IST next day).
            on_tick: Async callback invoked for each normalized tick.
        """
        super().__init__(on_tick=on_tick)
        self._api_key = api_key
        self._access_token = access_token
        self._ticker: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connect_event: asyncio.Event = asyncio.Event()

        # symbol -> instrument_token  (e.g., "RELIANCE" -> 738561)
        self._instrument_map: dict[str, int] = {}
        # instrument_token -> symbol  (reverse lookup for incoming ticks)
        self._reverse_map: dict[int, str] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """
        Load instrument tokens and open the Kite Ticker WebSocket.

        Steps:
            1. Check kiteconnect is installed.
            2. Load NSE instrument token map (cache → API fallback).
            3. Create KiteTicker and register callbacks.
            4. Start KiteTicker in background thread (threaded=True).
            5. Wait (up to 30 s) for the on_connect callback to fire.

        Raises:
            ImportError: kiteconnect is not installed.
            ConnectionError: WebSocket did not connect within 30 seconds.
        """
        if not _KITECONNECT_AVAILABLE:
            raise ImportError(
                "kiteconnect is not installed. "
                "Install it with: pip install kiteconnect"
            )

        logger.info("Connecting to Zerodha Kite Ticker WebSocket")

        # Capture event loop for thread-safe dispatch from KiteTicker's thread
        self._loop = asyncio.get_running_loop()
        self._connect_event.clear()

        # Load instrument map (blocking I/O — run off event loop)
        await asyncio.to_thread(self._load_instrument_tokens)

        # Create KiteTicker with reconnection enabled (auto reconnect up to
        # 50 attempts with exponential backoff — Kite library default)
        self._ticker = KiteTicker(
            self._api_key,
            self._access_token,
            reconnect=True,
            reconnect_max_tries=50,
        )

        # Register all callbacks
        self._ticker.on_ticks = self._on_ticks_callback
        self._ticker.on_connect = self._on_connect_callback
        self._ticker.on_close = self._on_close_callback
        self._ticker.on_error = self._on_error_callback
        self._ticker.on_reconnect = self._on_reconnect_callback
        self._ticker.on_noreconnect = self._on_noreconnect_callback

        # Start in a background OS thread — this is non-blocking
        self._ticker.connect(threaded=True)

        # Block until on_connect fires (or timeout)
        try:
            await asyncio.wait_for(self._connect_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            raise ConnectionError(
                "Zerodha Kite Ticker did not connect within 30 seconds. "
                "Check your API key and access token."
            )

        logger.info(
            "Zerodha Kite Ticker connected. Instrument map: %d symbols",
            len(self._instrument_map),
        )

    async def disconnect(self) -> None:
        """
        Gracefully close the Kite Ticker WebSocket and its background thread.
        """
        logger.info("Disconnecting from Zerodha Kite Ticker")
        if self._ticker is not None:
            self._ticker.close()
            self._ticker = None
        self._connected = False
        logger.info("Zerodha Kite Ticker disconnected")

    async def subscribe(self, symbols: list[str]) -> None:
        """
        Subscribe to real-time tick data for the given NSE symbols.

        Resolves symbols to integer instrument tokens using the cached map.
        Uses MODE_FULL which provides last_price, depth (bid/ask), volume,
        and exchange_timestamp.

        Args:
            symbols: List of NSE trading symbols (e.g., ["RELIANCE", "TCS"]).
        """
        if self._ticker is None:
            raise RuntimeError("connect() must be called before subscribe()")

        resolved: list[int] = []
        missing: list[str] = []

        for symbol in symbols:
            token = self._instrument_map.get(symbol)
            if token is not None:
                resolved.append(token)
            else:
                missing.append(symbol)

        if missing:
            logger.warning(
                "These symbols were not found in the Zerodha instrument map "
                "(check spelling or whether they are NSE-listed): %s",
                missing,
            )

        if resolved:
            self._ticker.subscribe(resolved)
            self._ticker.set_mode(self._ticker.MODE_FULL, resolved)
            # Track only the symbols we actually subscribed to
            subscribed = [s for s in symbols if s not in missing]
            self._subscribed_symbols.extend(subscribed)
            logger.info(
                "Subscribed to %d Zerodha instrument tokens: %s",
                len(resolved),
                subscribed,
            )

    async def unsubscribe(self, symbols: list[str]) -> None:
        """
        Unsubscribe from tick data for the given NSE symbols.

        Args:
            symbols: List of NSE symbols to unsubscribe from.
        """
        tokens = [
            self._instrument_map[s]
            for s in symbols
            if s in self._instrument_map
        ]
        if self._ticker is not None and tokens:
            self._ticker.unsubscribe(tokens)
        self._subscribed_symbols = [
            s for s in self._subscribed_symbols if s not in symbols
        ]
        logger.info("Unsubscribed from Zerodha symbols: %s", symbols)

    # ── Tick normalisation ────────────────────────────────────────────────────

    def _normalize_tick(self, raw_tick: Any) -> NormalizedTick:
        """
        Convert a Kite Ticker tick dict (MODE_FULL) to NormalizedTick.

        Kite MODE_FULL tick fields:
            instrument_token  — integer token
            last_price        — last traded price
            volume_traded     — cumulative volume for the session
            depth             — {"buy": [{price, quantity, orders}×5],
                                 "sell": [{price, quantity, orders}×5]}
            exchange_timestamp — datetime (IST, may be naive)
            ...many more OHLC / OI fields (ignored here)

        Args:
            raw_tick: Raw dict from KiteTicker ``on_ticks`` callback.

        Returns:
            Normalized tick in UTC with bid/ask from top-of-book depth.
        """
        instrument_token = raw_tick.get("instrument_token", 0)
        symbol = self._reverse_map.get(instrument_token, "UNKNOWN")

        # Extract top-of-book bid/ask from order depth
        depth = raw_tick.get("depth", {})
        buy_levels = depth.get("buy", [])
        sell_levels = depth.get("sell", [])
        best_bid = float(buy_levels[0].get("price", 0.0)) if buy_levels else 0.0
        best_ask = float(sell_levels[0].get("price", 0.0)) if sell_levels else 0.0

        # Kite timestamps are IST (Asia/Kolkata) and may be timezone-naive
        ts = raw_tick.get("exchange_timestamp")
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                # Assume IST (UTC+05:30) and convert to UTC
                try:
                    from zoneinfo import ZoneInfo  # Python 3.9+
                    ts = ts.replace(tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc)
                except Exception:
                    # Fallback: treat as UTC (slight timestamp error, non-critical)
                    ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        return NormalizedTick(
            symbol=symbol,
            market=Market.NSE,
            last_price=float(raw_tick.get("last_price", 0.0)),
            bid=best_bid,
            ask=best_ask,
            volume=int(raw_tick.get("volume_traded", 0)),
            timestamp=ts,
            broker="zerodha",
            raw=raw_tick,
        )

    # ── KiteTicker callbacks (called from background thread) ─────────────────

    def _on_ticks_callback(self, ws: Any, ticks: list[dict[str, Any]]) -> None:
        """
        Handle a batch of ticks from KiteTicker (runs in background thread).

        Each tick is normalized and dispatched to the async on_tick handler
        using run_coroutine_threadsafe() to safely cross the thread boundary.

        Args:
            ws: KiteTicker websocket instance.
            ticks: List of raw tick dicts from the Kite feed.
        """
        if not ticks or self._on_tick is None or self._loop is None:
            return

        for raw_tick in ticks:
            try:
                normalized = self._normalize_tick(raw_tick)
                asyncio.run_coroutine_threadsafe(
                    self._on_tick(normalized), self._loop
                )
            except Exception:
                logger.exception(
                    "Error normalizing Zerodha tick for token %s",
                    raw_tick.get("instrument_token"),
                )

    def _on_connect_callback(self, ws: Any, response: Any) -> None:
        """
        Callback on successful WebSocket connection.

        Also fires on reconnect — we re-subscribe the token list so ticks
        resume after a dropped connection without needing a service restart.

        Args:
            ws: KiteTicker websocket instance.
            response: Connection response from the server.
        """
        logger.info("Zerodha WebSocket connected (response=%s)", response)
        self._connected = True

        # Signal the connect() coroutine that the WS is up
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._connect_event.set)

        # Re-subscribe if we already had subscriptions (reconnect scenario)
        if self._subscribed_symbols and self._ticker is not None:
            tokens = [
                self._instrument_map[s]
                for s in self._subscribed_symbols
                if s in self._instrument_map
            ]
            if tokens:
                self._ticker.subscribe(tokens)
                self._ticker.set_mode(self._ticker.MODE_FULL, tokens)
                logger.info(
                    "Re-subscribed %d tokens after reconnect", len(tokens)
                )

    def _on_close_callback(self, ws: Any, code: int, reason: str) -> None:
        """Callback on WebSocket close."""
        logger.warning(
            "Zerodha WebSocket closed: code=%d reason=%s", code, reason
        )
        self._connected = False

    def _on_error_callback(self, ws: Any, code: int, reason: str) -> None:
        """Callback on WebSocket error."""
        logger.error(
            "Zerodha WebSocket error: code=%d reason=%s", code, reason
        )

    def _on_reconnect_callback(self, ws: Any, attempts_count: int) -> None:
        """Callback on reconnect attempt."""
        logger.info(
            "Zerodha WebSocket reconnecting (attempt %d of 50)", attempts_count
        )

    def _on_noreconnect_callback(self, ws: Any) -> None:
        """Callback when max reconnect attempts are exhausted."""
        logger.critical(
            "Zerodha WebSocket exhausted all reconnect attempts — "
            "data feed has stopped. Manual restart required."
        )
        self._connected = False

    # ── Instrument token management ───────────────────────────────────────────

    def _load_instrument_tokens(self) -> None:
        """
        Build the symbol→token mapping from cache or Kite REST API.

        Cache strategy:
            - Cache file: /tmp/kite_instruments_YYYY-MM-DD.json
            - If today's cache exists, load it (skip API call).
            - Otherwise fetch all NSE instruments from Kite API and cache.
            - On API failure, log a warning and continue with an empty map
              (the connector will run but subscriptions will silently fail).
        """
        cache_file = _CACHE_DIR / f"kite_instruments_{date.today().isoformat()}.json"

        if cache_file.exists():
            try:
                with open(cache_file) as f:
                    data = json.load(f)
                self._instrument_map = data["symbol_to_token"]
                self._reverse_map = {
                    int(token): symbol
                    for symbol, token in data["symbol_to_token"].items()
                }
                logger.info(
                    "Loaded %d Zerodha instrument tokens from cache (%s)",
                    len(self._instrument_map),
                    cache_file.name,
                )
                return
            except Exception:
                logger.warning(
                    "Instrument cache read failed — will refresh from API",
                    exc_info=True,
                )

        self._fetch_and_cache_instruments(cache_file)

    def _fetch_and_cache_instruments(self, cache_file: Path) -> None:
        """
        Fetch all NSE instruments from the Kite REST API and cache locally.

        The Kite ``instruments("NSE")`` call returns ~10,000 entries as a
        list of dicts. We extract tradingsymbol → instrument_token pairs.

        Args:
            cache_file: Path to write the cache JSON file.
        """
        if not _KITECONNECT_AVAILABLE or KiteConnect is None:
            logger.warning(
                "kiteconnect not available — instrument map will be empty. "
                "Subscriptions will not work until kiteconnect is installed."
            )
            return

        try:
            kite = KiteConnect(api_key=self._api_key)
            kite.set_access_token(self._access_token)
            instruments = kite.instruments("NSE")

            self._instrument_map = {
                inst["tradingsymbol"]: inst["instrument_token"]
                for inst in instruments
                if inst.get("exchange") == "NSE"
            }
            self._reverse_map = {v: k for k, v in self._instrument_map.items()}

            # Persist cache
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(cache_file, "w") as f:
                json.dump({"symbol_to_token": self._instrument_map}, f)

            logger.info(
                "Fetched %d NSE instrument tokens from Kite API and cached to %s",
                len(self._instrument_map),
                cache_file.name,
            )
        except Exception:
            logger.exception(
                "Failed to fetch instrument tokens from Kite API. "
                "Subscriptions will fail until tokens are loaded."
            )
