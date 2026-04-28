"""
Alpaca WebSocket connector for US equity real-time market data.

Uses the alpaca-py library's StockDataStream, which is async-native and
runs within the existing asyncio event loop (no thread-bridging required).

We subscribe to both trades and quotes:
  - Trades: give us the last executed price (last_price, volume).
  - Quotes: give us live bid/ask spread.

On each trade event the tick is dispatched with the trade price. On each
quote event the tick is dispatched with bid/ask updated (price = mid-point).
This lets the strategy engine see both execution prices and spread changes.

Data feeds:
  - "iex"  — free, delayed ~15 min during market hours (fine for backtests)
  - "sip"  — paid, real-time consolidated tape

Install: pip install alpaca-py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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
try:
    from alpaca.data.live import StockDataStream          # type: ignore[import]
    from alpaca.data.models import Trade, Quote           # type: ignore[import]
    _ALPACA_AVAILABLE = True
except ImportError:
    _ALPACA_AVAILABLE = False
    StockDataStream = None  # type: ignore[assignment,misc]
    Trade = None            # type: ignore[assignment,misc]
    Quote = None            # type: ignore[assignment,misc]

# Default data feed — override via ALPACA_DATA_FEED env var
import os as _os
_DEFAULT_FEED: str = _os.environ.get("ALPACA_DATA_FEED", "iex")


class AlpacaConnector(BaseConnector):
    """
    Alpaca real-time market data connector for US equities.

    Async-native: StockDataStream.run() is a coroutine. We launch it as
    a background asyncio.Task so it runs concurrently with the rest of
    the data ingestion pipeline.

    Reconnection:
        alpaca-py handles WebSocket reconnection internally. The stream
        task will remain alive through transient disconnects.

    Usage::

        connector = AlpacaConnector(
            api_key="PKXXX",
            api_secret="secretXXX",
            on_tick=async_handler,
        )
        await connector.connect()
        await connector.subscribe(["AAPL", "MSFT", "GOOGL"])
        # trade and quote ticks now flow to async_handler
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = "https://data.alpaca.markets",
        data_feed: str = _DEFAULT_FEED,
        on_tick: Optional[TickCallback] = None,
    ) -> None:
        """
        Initialize the Alpaca connector.

        Args:
            api_key: Alpaca API key ID.
            api_secret: Alpaca API secret key.
            base_url: Alpaca data API base URL (used for REST; stream uses wss://).
            data_feed: "iex" (free) or "sip" (paid real-time consolidated tape).
            on_tick: Async callback invoked for each normalized tick.
        """
        super().__init__(on_tick=on_tick)
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url
        self._data_feed = data_feed
        self._stream: Any = None
        self._stream_task: Optional[asyncio.Task[None]] = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """
        Initialize the Alpaca StockDataStream.

        This does not open the WebSocket yet — the connection is established
        when subscribe() starts the background stream task.

        Raises:
            ImportError: alpaca-py is not installed.
        """
        if not _ALPACA_AVAILABLE:
            raise ImportError(
                "alpaca-py is not installed. "
                "Install it with: pip install alpaca-py"
            )

        logger.info(
            "Initializing Alpaca StockDataStream (feed=%s)", self._data_feed
        )

        self._stream = StockDataStream(
            api_key=self._api_key,
            secret_key=self._api_secret,
            feed=self._data_feed,
        )
        self._connected = True
        logger.info("Alpaca StockDataStream initialized")

    async def disconnect(self) -> None:
        """
        Stop the Alpaca stream and cancel the background task.
        """
        logger.info("Disconnecting from Alpaca data stream")

        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                logger.warning("Error stopping Alpaca stream", exc_info=True)

        if self._stream_task is not None and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except (asyncio.CancelledError, Exception):
                pass  # expected on cancel

        self._connected = False
        logger.info("Alpaca data stream disconnected")

    async def subscribe(self, symbols: list[str]) -> None:
        """
        Subscribe to trades and quotes for the given US equity symbols.

        Registers async handlers with the stream and starts the background
        WebSocket task if it is not already running.

        Args:
            symbols: List of US equity symbols (e.g., ["AAPL", "MSFT"]).
        """
        if self._stream is None:
            raise RuntimeError("connect() must be called before subscribe()")

        if not symbols:
            logger.warning("subscribe() called with empty symbol list — nothing to do")
            return

        # ── Register trade handler ────────────────────────────────────────────
        async def _trade_handler(trade: Any) -> None:
            """Handle a trade event (price, size, timestamp)."""
            try:
                normalized = self._normalize_trade(trade)
                if self._on_tick is not None:
                    await self._on_tick(normalized)
            except Exception:
                logger.exception(
                    "Error processing Alpaca trade for %s",
                    getattr(trade, "symbol", "unknown"),
                )

        # ── Register quote handler ────────────────────────────────────────────
        async def _quote_handler(quote: Any) -> None:
            """Handle a quote event (bid/ask prices and sizes)."""
            try:
                normalized = self._normalize_quote(quote)
                if self._on_tick is not None:
                    await self._on_tick(normalized)
            except Exception:
                logger.exception(
                    "Error processing Alpaca quote for %s",
                    getattr(quote, "symbol", "unknown"),
                )

        # Subscribe to both trades and quotes for all symbols
        self._stream.subscribe_trades(_trade_handler, *symbols)
        self._stream.subscribe_quotes(_quote_handler, *symbols)
        self._subscribed_symbols.extend(symbols)

        logger.info("Subscribed to Alpaca trades+quotes for: %s", symbols)

        # Start the WebSocket stream as a background task (if not already running)
        if self._stream_task is None or self._stream_task.done():
            self._stream_task = asyncio.create_task(
                self._run_stream(), name="alpaca_stream"
            )
            logger.info("Alpaca stream background task started")

    async def unsubscribe(self, symbols: list[str]) -> None:
        """
        Unsubscribe from trades and quotes for the given symbols.

        Args:
            symbols: List of US equity symbols to unsubscribe from.
        """
        if self._stream is not None:
            try:
                self._stream.unsubscribe_trades(*symbols)
                self._stream.unsubscribe_quotes(*symbols)
            except Exception:
                logger.warning(
                    "Error unsubscribing from Alpaca symbols %s", symbols, exc_info=True
                )

        self._subscribed_symbols = [
            s for s in self._subscribed_symbols if s not in symbols
        ]
        logger.info("Unsubscribed from Alpaca symbols: %s", symbols)

    # ── Tick normalisation ────────────────────────────────────────────────────

    def _normalize_tick(self, raw_tick: Any) -> NormalizedTick:
        """
        Thin dispatcher — delegates to _normalize_trade or _normalize_quote
        based on the object type.

        Args:
            raw_tick: Either an alpaca-py Trade or Quote object.
        """
        if hasattr(raw_tick, "price"):
            return self._normalize_trade(raw_tick)
        return self._normalize_quote(raw_tick)

    def _normalize_trade(self, trade: Any) -> NormalizedTick:
        """
        Normalize an Alpaca trade event to NormalizedTick.

        Alpaca Trade attributes (alpaca-py):
            symbol     — trading symbol
            price      — execution price
            size       — trade size (shares)
            timestamp  — timezone-aware datetime (UTC)
            id         — trade ID
            exchange   — exchange code

        Args:
            trade: alpaca-py Trade object.

        Returns:
            NormalizedTick with last_price=trade.price, bid=ask=trade.price.
        """
        price = float(getattr(trade, "price", 0.0))
        ts = self._ensure_utc(getattr(trade, "timestamp", None))

        return NormalizedTick(
            symbol=getattr(trade, "symbol", "UNKNOWN"),
            market=Market.US,
            last_price=price,
            bid=price,    # bid/ask unknown for a trade event; use price
            ask=price,
            volume=int(getattr(trade, "size", 0)),
            timestamp=ts,
            broker="alpaca",
            raw=self._to_dict(trade),
        )

    def _normalize_quote(self, quote: Any) -> NormalizedTick:
        """
        Normalize an Alpaca quote event to NormalizedTick.

        Alpaca Quote attributes (alpaca-py):
            symbol      — trading symbol
            bid_price   — best bid price
            ask_price   — best ask price
            bid_size    — bid size
            ask_size    — ask size
            timestamp   — timezone-aware datetime (UTC)

        Args:
            quote: alpaca-py Quote object.

        Returns:
            NormalizedTick where last_price = (bid + ask) / 2 (mid-point).
        """
        bid = float(getattr(quote, "bid_price", 0.0))
        ask = float(getattr(quote, "ask_price", 0.0))
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else bid or ask
        size = int(getattr(quote, "bid_size", 0))
        ts = self._ensure_utc(getattr(quote, "timestamp", None))

        return NormalizedTick(
            symbol=getattr(quote, "symbol", "UNKNOWN"),
            market=Market.US,
            last_price=mid,
            bid=bid,
            ask=ask,
            volume=size,
            timestamp=ts,
            broker="alpaca",
            raw=self._to_dict(quote),
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _run_stream(self) -> None:
        """
        Background coroutine that runs the Alpaca WebSocket stream.

        Runs until cancelled (via disconnect()) or the stream raises an
        unrecoverable error.
        """
        try:
            logger.info("Alpaca stream running (feed=%s)", self._data_feed)
            await self._stream.run()
        except asyncio.CancelledError:
            logger.info("Alpaca stream task cancelled — shutting down")
        except Exception:
            logger.exception(
                "Alpaca stream exited with an unexpected error — "
                "data feed has stopped. Check logs and restart service."
            )
            self._connected = False

    @staticmethod
    def _ensure_utc(ts: Any) -> datetime:
        """
        Coerce an Alpaca timestamp to a UTC-aware datetime.

        Alpaca-py returns timezone-aware UTC datetimes. This is a safety
        guard for None or unexpected types.

        Args:
            ts: Raw timestamp value from alpaca-py event.

        Returns:
            UTC-aware datetime.
        """
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                return ts.replace(tzinfo=timezone.utc)
            return ts.astimezone(timezone.utc)
        return datetime.now(timezone.utc)

    @staticmethod
    def _to_dict(obj: Any) -> dict:
        """
        Convert an alpaca-py model object to a plain dict for the raw field.

        Args:
            obj: alpaca-py Trade or Quote object.

        Returns:
            Dict representation, or empty dict on failure.
        """
        try:
            if hasattr(obj, "model_dump"):
                return obj.model_dump()           # pydantic v2
            if hasattr(obj, "dict"):
                return obj.dict()                 # pydantic v1
            if hasattr(obj, "__dict__"):
                return vars(obj)
        except Exception:
            pass
        return {}
