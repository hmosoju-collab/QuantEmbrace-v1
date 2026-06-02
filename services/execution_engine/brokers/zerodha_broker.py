"""
Zerodha Kite Connect Broker Client.

Implements the ``BrokerClient`` interface for NSE/BSE Indian markets via
Zerodha's Kite Connect API. Handles daily token refresh (tokens expire at
~07:30 IST), client-side rate limiting, and order type translation.

Rate limits:
    - 10 requests/second for order APIs.
    - 3 requests/second for historical data APIs.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.utils.helpers import utc_now

from execution_engine.auth.zerodha_auth import TokenExpiredError, ZerodhaTokenManager
from execution_engine.brokers.base_broker import (
    BrokerAPIError,
    BrokerClient,
    NonRetryableBrokerError,
    QuoteCallback,
)
from execution_engine.orders.order import (
    OrderRequest,
    OrderResponse,
    OrderStatus,
    OrderStatusUpdate,
    OrderType,
    ProductType,
)

logger = get_logger(__name__, service_name="execution_engine")

# Zerodha order variety mapping
_VARIETY_REGULAR = "regular"
_VARIETY_AMO = "amo"

# Zerodha exchange mapping
_EXCHANGE_NSE = "NSE"
_EXCHANGE_BSE = "BSE"
_EXCHANGE_NFO = "NFO"


class _RateLimiter:
    """
    Token-bucket rate limiter for client-side request throttling.

    Ensures we do not exceed the broker's rate limits and risk getting
    blocked or throttled.
    """

    def __init__(self, max_requests: int, per_seconds: float = 1.0) -> None:
        self._max_requests = max_requests
        self._per_seconds = per_seconds
        self._tokens = float(max_requests)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a request token is available."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self._max_requests,
                self._tokens + elapsed * (self._max_requests / self._per_seconds),
            )
            self._last_refill = now

            if self._tokens < 1.0:
                wait_time = (1.0 - self._tokens) * (self._per_seconds / self._max_requests)
                await asyncio.sleep(wait_time)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


class _OrderPlacementLimiter:
    """Client-side guard for Kite order placement count limits."""

    def __init__(
        self,
        max_per_second: int = 10,
        max_per_minute: int = 400,
        max_per_day: int = 5000,
    ) -> None:
        self._max_per_second = max_per_second
        self._max_per_minute = max_per_minute
        self._max_per_day = max_per_day
        self._second_window: deque[float] = deque()
        self._minute_window: deque[float] = deque()
        self._day_window: deque[float] = deque()
        self._day_key = datetime.utcnow().date()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                self._prune()
                if len(self._day_window) >= self._max_per_day:
                    raise BrokerAPIError(
                        "Zerodha",
                        "Daily Kite order placement cap exhausted "
                        f"({self._max_per_day}/day)",
                    )
                wait_seconds = 0.0
                now = time.monotonic()
                if len(self._second_window) >= self._max_per_second:
                    wait_seconds = max(wait_seconds, 1.0 - (now - self._second_window[0]))
                if len(self._minute_window) >= self._max_per_minute:
                    wait_seconds = max(wait_seconds, 60.0 - (now - self._minute_window[0]))
                if wait_seconds <= 0:
                    self._second_window.append(now)
                    self._minute_window.append(now)
                    self._day_window.append(now)
                    return
                await asyncio.sleep(min(wait_seconds, 1.0))

    def _prune(self) -> None:
        today = datetime.utcnow().date()
        if today != self._day_key:
            self._day_key = today
            self._day_window.clear()
        now = time.monotonic()
        while self._second_window and now - self._second_window[0] >= 1.0:
            self._second_window.popleft()
        while self._minute_window and now - self._minute_window[0] >= 60.0:
            self._minute_window.popleft()


class ZerodhaBrokerClient(BrokerClient):
    """
    Zerodha Kite Connect broker implementation.

    Connects to Kite Connect REST API for order management and Kite Ticker
    WebSocket for real-time quotes. Handles the daily token expiry cycle.

    Supported order types:
        - MARKET, LIMIT, SL (stop-loss limit), SL-M (stop-loss market).

    Supported product types:
        - CNC (delivery), MIS (intraday), NRML (F&O normal margin).
    """

    def __init__(
        self,
        settings: Optional[AppSettings] = None,
        dynamo_client: Any = None,
        secrets_client: Any = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._kite: Any = None  # kiteconnect.KiteConnect instance
        self._ticker: Any = None  # kiteconnect.KiteTicker instance
        self._connected = False
        # Stored so token refresh loop can re-subscribe after hot-swap
        self._subscribed_symbols: list[str] = []
        self._subscribed_callback: Optional[QuoteCallback] = None
        self._needs_authentication = False  # set True when token is missing/expired
        self._order_rate_limiter = _RateLimiter(max_requests=10, per_seconds=1.0)
        self._quote_rate_limiter = _RateLimiter(max_requests=1, per_seconds=1.0)
        self._historical_rate_limiter = _RateLimiter(max_requests=3, per_seconds=1.0)
        rate_cfg = getattr(self._settings, "zerodha_rate_limit", None)
        self._order_count_limiter = _OrderPlacementLimiter(
            max_per_second=getattr(rate_cfg, "max_orders_per_second", 10),
            max_per_minute=getattr(rate_cfg, "max_orders_per_minute", 400),
            max_per_day=getattr(rate_cfg, "max_orders_per_day", 5000),
        )

        # Token manager — owns credential loading + DynamoDB token lifecycle
        self._token_manager = ZerodhaTokenManager(
            dynamo_client=dynamo_client,
            secrets_client=secrets_client,
            settings=self._settings,
        )

    @property
    def broker_name(self) -> str:
        """Return broker name."""
        return "Zerodha"

    @property
    def supported_markets(self) -> list[str]:
        """Return supported markets."""
        return ["NSE", "BSE", "NFO"]

    @property
    def needs_authentication(self) -> bool:
        """True when no valid token is available — operator must run login CLI."""
        return self._needs_authentication

    @property
    def token_manager(self) -> ZerodhaTokenManager:
        """Expose token manager for the login CLI and integration tests."""
        return self._token_manager

    async def connect(self) -> None:
        """
        Initialize the Kite Connect client and authenticate.

        Token resolution order:
            1. DynamoDB (via ``ZerodhaTokenManager.get_valid_token()``).
            2. ``ZERODHA_ACCESS_TOKEN`` env var (legacy fallback for local dev).

        If no valid token is found, the broker enters ``needs_authentication``
        mode: it initialises the Kite client but does not set ``_connected``.
        Callers must check ``needs_authentication`` and present the login URL
        from ``token_manager.get_login_url()`` to the operator.
        """
        try:
            from kiteconnect import KiteConnect  # type: ignore[import]
        except ImportError:
            logger.error("kiteconnect package not installed — pip install kiteconnect")
            raise

        try:
            api_key, _ = await self._token_manager.load_credentials()
        except ValueError as exc:
            logger.error("Zerodha credential loading failed: %s", exc)
            raise BrokerAPIError("Zerodha", str(exc)) from exc

        self._kite = KiteConnect(api_key=api_key)
        # Set a network-level timeout on the underlying requests.Session so every
        # kiteconnect HTTP call (including historical_data) raises ReadTimeout after
        # 12 seconds. Without this, asyncio.wait_for(timeout=15.0) fires but Python
        # 3.11's _cancel_and_wait awaits the thread until the OS TCP timeout (~75-120s),
        # stalling the candle stream for minutes per instrument instead of 15 seconds.
        self._kite.reqsession.timeout = (5, 12)  # (connect_timeout, read_timeout)

        # --- Try DynamoDB token first ---
        try:
            access_token = await self._token_manager.get_valid_token()
            self._kite.set_access_token(access_token)
            self._connected = True
            self._needs_authentication = False
            logger.info("Zerodha authenticated via DynamoDB token")
            return
        except TokenExpiredError as exc:
            logger.warning(
                "Zerodha token missing or expired: %s — "
                "entering needs_authentication mode. "
                "Run 'python scripts/zerodha_login.py' to refresh.",
                exc,
            )

        # --- Env-var fallback (local dev convenience) ---
        try:
            env_token = self._settings.zerodha.access_token.get_secret_value()
            if env_token:
                self._kite.set_access_token(env_token)
                self._connected = True
                self._needs_authentication = False
                logger.warning(
                    "Zerodha authenticated via ZERODHA_ACCESS_TOKEN env var — "
                    "this token is NOT persisted and will not survive a restart. "
                    "Use 'python scripts/zerodha_login.py' for production auth."
                )
                return
        except Exception:
            pass

        # No token available — mark as needing auth but don't raise
        # so the service can start and reject orders gracefully
        self._needs_authentication = True
        login_url = await self._token_manager.get_login_url()
        logger.error(
            "Zerodha NOT authenticated — no valid access token available. "
            "Open this URL to log in: %s",
            login_url,
        )

    async def disconnect(self) -> None:
        """Disconnect from Kite Connect and close any WebSocket connections."""
        if self._ticker:
            self._ticker.close()
            self._ticker = None
        self._connected = False
        logger.info("Disconnected from Zerodha Kite Connect")

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """
        Place an order via Kite Connect API.

        Translates the generic OrderRequest into Kite Connect parameters
        and submits the order.

        Args:
            order: The order to place.

        Returns:
            OrderResponse with the Kite-assigned order ID.
        """
        if self._needs_authentication:
            raise BrokerAPIError(
                "Zerodha",
                "No valid access token — run 'python scripts/zerodha_login.py' to authenticate",
            )
        if not self._connected or self._kite is None:
            raise BrokerAPIError("Zerodha", "Not connected — call connect() first")

        await self._order_count_limiter.acquire()
        await self._order_rate_limiter.acquire()

        try:
            kite_params = self._translate_order(order)

            broker_order_id = await asyncio.to_thread(
                self._kite.place_order,
                variety=kite_params.pop("variety", _VARIETY_REGULAR),
                **kite_params,
            )

            logger.info(
                "Zerodha order placed: %s -> broker_id=%s",
                order.order_id,
                broker_order_id,
            )

            return OrderResponse(
                order_id=order.order_id,
                broker_order_id=str(broker_order_id),
                status=OrderStatus.PLACED,
                symbol=order.symbol,
                market=order.market,
            )

        except Exception as exc:
            logger.exception("Zerodha order placement failed for %s", order.order_id)
            _raise_classified_zerodha_error(exc)

    async def find_order_by_client_order_id(
        self,
        client_order_id: str,
        order: OrderRequest | None = None,
    ) -> OrderResponse | None:
        """
        Find an existing Kite order by QuantEmbrace's broker idempotency tag.

        Kite does not expose a true idempotency-key API for order placement.
        The safest available recovery path is to tag every order with a stable
        20-character key and scan today's broker orders before retrying.
        """
        if not client_order_id:
            return None
        all_orders = await self.get_all_orders()
        for broker_order in all_orders:
            if str(broker_order.get("tag", "")) != client_order_id[:20]:
                continue
            status = self._translate_status(str(broker_order.get("status", "")))
            return OrderResponse(
                order_id=order.order_id if order is not None else client_order_id,
                broker_order_id=str(broker_order.get("order_id", "")),
                status=status,
                symbol=(
                    order.symbol if order is not None else str(broker_order.get("tradingsymbol", ""))
                ),
                market=order.market if order is not None else "NSE",
                filled_quantity=float(broker_order.get("filled_quantity", 0) or 0),
                avg_fill_price=float(broker_order.get("average_price", 0) or 0),
                broker_message=str(broker_order.get("status_message", "")),
            )
        return None

    async def cancel_order(self, broker_order_id: str) -> OrderStatusUpdate:
        """
        Cancel an open order on Zerodha.

        Args:
            broker_order_id: Kite-assigned order ID.

        Returns:
            OrderStatusUpdate reflecting the cancellation.
        """
        if not self._connected or self._kite is None:
            raise BrokerAPIError("Zerodha", "Not connected")

        await self._order_rate_limiter.acquire()

        try:
            await asyncio.to_thread(
                self._kite.cancel_order,
                variety=_VARIETY_REGULAR,
                order_id=broker_order_id,
            )

            return OrderStatusUpdate(
                order_id="",  # Caller must map back to internal order_id
                broker_order_id=broker_order_id,
                previous_status=OrderStatus.PLACED,
                new_status=OrderStatus.CANCELLED,
                broker_message="Order cancelled successfully",
            )

        except Exception as exc:
            logger.exception("Zerodha cancel failed for broker_order_id=%s", broker_order_id)
            _raise_classified_zerodha_error(exc)

    async def get_order_status(self, broker_order_id: str) -> OrderStatusUpdate:
        """
        Query the current status of a Zerodha order.

        Args:
            broker_order_id: Kite-assigned order ID.

        Returns:
            OrderStatusUpdate with latest status and fill information.
        """
        if not self._connected or self._kite is None:
            raise BrokerAPIError("Zerodha", "Not connected")

        await self._order_rate_limiter.acquire()

        try:
            order_history = await asyncio.to_thread(
                self._kite.order_history, order_id=broker_order_id
            )

            if not order_history:
                raise BrokerAPIError("Zerodha", f"No order history for {broker_order_id}")

            latest = order_history[-1]
            status = self._translate_status(latest.get("status", ""))

            return OrderStatusUpdate(
                order_id="",
                broker_order_id=broker_order_id,
                previous_status=OrderStatus.PLACED,
                new_status=status,
                filled_quantity=float(latest.get("filled_quantity", 0)),
                avg_fill_price=float(latest.get("average_price", 0)),
                broker_message=latest.get("status_message", ""),
            )

        except BrokerAPIError:
            raise
        except Exception as exc:
            logger.exception("Zerodha status query failed for %s", broker_order_id)
            _raise_classified_zerodha_error(exc)

    async def get_margins(self) -> dict[str, Any]:
        """Return available cash and margin usage from Kite. Returns zeroes when not connected."""
        if not self._connected or self._kite is None:
            return {"available_cash": 0.0, "used_margin": 0.0, "collateral": 0.0}
        try:
            data = await asyncio.to_thread(self._kite.margins)
            equity = data.get("equity", {})
            return {
                "available_cash": float(equity.get("available", {}).get("cash", 0)),
                "used_margin": float(equity.get("utilised", {}).get("debits", 0)),
                "collateral": float(equity.get("available", {}).get("collateral", 0)),
            }
        except Exception:
            return {"available_cash": 0.0, "used_margin": 0.0, "collateral": 0.0}

    async def get_positions(self) -> list[dict[str, Any]]:
        """
        Retrieve all positions from Zerodha.

        Returns:
            List of position dictionaries combining net and day positions.
        """
        if not self._connected or self._kite is None:
            raise BrokerAPIError("Zerodha", "Not connected")

        await self._order_rate_limiter.acquire()

        try:
            positions = await asyncio.to_thread(self._kite.positions)
            net_positions = positions.get("net", [])

            return [
                {
                    "symbol": pos["tradingsymbol"],
                    "exchange": pos["exchange"],
                    "quantity": pos["quantity"],
                    "average_price": pos["average_price"],
                    "last_price": pos["last_price"],
                    "pnl": pos["pnl"],
                    "product": pos["product"],
                }
                for pos in net_positions
            ]

        except Exception as exc:
            logger.exception("Failed to fetch Zerodha positions")
            _raise_classified_zerodha_error(exc)

    async def get_all_orders(self) -> list[dict[str, Any]]:
        """
        Fetch ALL of today's orders in a single Kite Connect API call.

        This is the O(1) replacement for calling ``get_order_status()`` per
        open order (O(N) calls). One call returns every order placed today
        regardless of status. The caller (``BulkOrderPoller``) filters to
        the subset it is tracking.

        Rate cost: 1 API call per invocation (counts toward 10 req/sec).

        Returns:
            List of raw Kite order dicts. Relevant fields per order:
                order_id, status, filled_quantity, average_price,
                status_message, tradingsymbol, transaction_type, quantity.

        Raises:
            BrokerAPIError: If not connected or Kite raises an exception.
        """
        if not self._connected or self._kite is None:
            raise BrokerAPIError("Zerodha", "Not connected — call connect() first")

        await self._order_rate_limiter.acquire()

        try:
            orders = await asyncio.to_thread(self._kite.orders)
            return orders if orders else []

        except Exception as exc:
            logger.exception("Zerodha get_all_orders failed")
            _raise_classified_zerodha_error(exc)

    async def get_batch_quotes(
        self, instruments: list[str]
    ) -> dict[str, dict[str, Any]]:
        """
        Fetch live bid/ask quotes for up to 500 instruments in a single API call.

        Uses ``kite.quote()`` which accepts a list of exchange:symbol strings
        (e.g. ``["NSE:RELIANCE", "NSE:INFY"]``) and returns full market depth
        per instrument in one response.

        Rate cost: 1 API call per invocation regardless of instrument count.
        Used by ``LiveQuotePoller`` every 2 seconds for the full watchlist.

        Args:
            instruments: List of ``"{EXCHANGE}:{SYMBOL}"`` strings.
                         Kite supports up to 500 per request.

        Returns:
            Dict keyed by ``"{EXCHANGE}:{SYMBOL}"``. Each value contains:
                last_price, bid, ask, volume, circuit_limit_lower,
                circuit_limit_upper, ohlc, depth (best 5 bids/asks).

        Raises:
            BrokerAPIError: If not connected or Kite raises.
        """
        if not self._connected or self._kite is None:
            raise BrokerAPIError("Zerodha", "Not connected — call connect() first")
        if not instruments:
            return {}

        await self._quote_rate_limiter.acquire()

        try:
            raw = await asyncio.to_thread(self._kite.quote, instruments)
            return raw if raw else {}

        except Exception as exc:
            logger.exception(
                "Zerodha get_batch_quotes failed for %d instruments", len(instruments)
            )
            _raise_classified_zerodha_error(exc)

    async def get_historical_candles(
        self,
        instrument_token: int,
        from_dt: datetime,
        to_dt: datetime,
        interval: str = "minute",
        continuous: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Fetch historical OHLCV candles for an instrument.

        Uses ``kite.historical_data()``. This call uses the **separate
        3 req/sec historical data rate limit** — it does NOT share the
        10 req/sec order API budget. The ``_historical_rate_limiter``
        enforces this independent limit.

        Used by ``IntradayCandleStream`` (1m candles, round-robin, 3 req/s)
        and ``scripts/zerodha/candle_prefetch.py`` (pre-market bulk download).

        Args:
            instrument_token: Zerodha numeric instrument token (not symbol string).
                              Obtain from ``kite.instruments()`` or the
                              instrument token cache built during ``connect()``.
            from_dt:          Start of the candle range (inclusive), UTC.
            to_dt:            End of the candle range (inclusive), UTC.
            interval:         Candle width. Kite accepts:
                              "minute", "3minute", "5minute", "10minute",
                              "15minute", "30minute", "60minute", "day".
            continuous:       True for F&O continuous contract data.

        Returns:
            List of candle dicts, each containing:
                date (datetime), open, high, low, close, volume.
            Ordered oldest → newest.

        Raises:
            BrokerAPIError: If not connected or Kite raises.
        """
        if not self._connected or self._kite is None:
            raise BrokerAPIError("Zerodha", "Not connected — call connect() first")

        # Historical data uses the SEPARATE 3 req/sec limit
        await self._historical_rate_limiter.acquire()

        # kiteconnect ignores tzinfo on datetime objects and treats them as IST.
        # Convert UTC → IST strings before the API call so the window is correct.
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]
        _IST = ZoneInfo("Asia/Kolkata")
        from_ist = from_dt.astimezone(_IST).strftime("%Y-%m-%d %H:%M:%S")
        to_ist   = to_dt.astimezone(_IST).strftime("%Y-%m-%d %H:%M:%S")

        try:
            candles = await asyncio.wait_for(
                asyncio.to_thread(
                    self._kite.historical_data,
                    instrument_token,
                    from_ist,
                    to_ist,
                    interval,
                    continuous=continuous,
                ),
                timeout=15.0,
            )
            return candles if candles else []

        except asyncio.TimeoutError:
            logger.warning(
                "Zerodha get_historical_candles timed out after 15s: token=%s interval=%s",
                instrument_token,
                interval,
            )
            raise BrokerAPIError(
                "Zerodha",
                f"historical_data timed out after 15s (token={instrument_token}, interval={interval})",
            )

        except Exception as exc:
            logger.exception(
                "Zerodha get_historical_candles failed: token=%s interval=%s",
                instrument_token,
                interval,
            )
            _raise_classified_zerodha_error(exc)

    async def get_instrument_tokens(
        self,
        exchange: str = "NSE",
        symbols: Optional[set[str]] = None,
    ) -> dict[str, int]:
        """
        Fetch Zerodha instrument tokens for a given exchange.

        Returns a dict mapping ``"{exchange}:{tradingsymbol}"`` → numeric
        ``instrument_token``.  If *symbols* is provided, only instruments whose
        ``tradingsymbol`` is in that set are included; otherwise every instrument
        for the exchange is returned (≈ 1 800 for NSE).

        This call uses Zerodha's **instrument-list endpoint** which is served
        from a CDN and does **not** count against the 10 req/sec order API
        budget.  The response is ≈ 3 MB of JSON so we run it in a thread
        executor to keep the event loop unblocked.

        Args:
            exchange: Exchange code — ``"NSE"``, ``"BSE"``, ``"NFO"``, etc.
            symbols:  Optional set of plain tradingsymbols to filter on (e.g.
                      ``{"RELIANCE", "INFY"}``).  Pass ``None`` to return all.

        Returns:
            ``{"NSE:RELIANCE": 738561, "NSE:INFY": 408065, ...}``

        Raises:
            BrokerAPIError: If not connected or Kite raises.
        """
        if not self._connected or self._kite is None:
            raise BrokerAPIError("Zerodha", "Not connected — call connect() first")

        try:
            # kite.instruments() is synchronous; run in executor to avoid
            # blocking the event loop on a 3 MB JSON parse.
            loop = asyncio.get_running_loop()
            raw: list[dict] = await loop.run_in_executor(
                None, lambda: self._kite.instruments(exchange)
            )
        except Exception as exc:
            logger.exception(
                "Zerodha get_instrument_tokens failed: exchange=%s", exchange
            )
            _raise_classified_zerodha_error(exc)

        result: dict[str, int] = {}
        for item in raw:
            tradingsymbol: str = item.get("tradingsymbol", "")
            token = item.get("instrument_token")
            if not tradingsymbol or token is None:
                continue
            if symbols is None or tradingsymbol in symbols:
                result[f"{exchange}:{tradingsymbol}"] = int(token)

        logger.info(
            "zerodha.get_instrument_tokens",
            exchange=exchange,
            raw_count=len(raw),
            filtered_count=len(result),
        )
        return result

    async def subscribe_quotes(
        self, symbols: list[str], callback: QuoteCallback
    ) -> None:
        """
        Subscribe to real-time quotes via Kite Ticker WebSocket.

        Resolves instrument tokens for the given symbols, then starts a
        KiteTicker WebSocket in a background thread.  Ticks are bridged
        from the KiteTicker thread back to the running asyncio event loop
        via ``asyncio.run_coroutine_threadsafe``.

        The subscription is stored so ``start_token_refresh_loop`` can
        re-subscribe automatically after a daily token hot-swap.

        Args:
            symbols: NSE symbols as ``"RELIANCE"`` or ``"NSE:RELIANCE"`` strings.
            callback: Async callable receiving a quote dict per tick.
                      Dict keys: symbol, market, last_price, bid, ask,
                      bid_quantity, ask_quantity, volume, instrument_token,
                      timestamp.
        """
        if not self._connected or self._kite is None:
            raise BrokerAPIError("Zerodha", "Not connected — call connect() first")
        if not symbols:
            return

        try:
            from kiteconnect import KiteTicker  # type: ignore[import]
        except ImportError:
            logger.error("kiteconnect package not installed — pip install kiteconnect")
            raise

        # KiteTicker requires numeric instrument tokens, not symbol strings.
        # Resolve via the instruments CDN endpoint (does not count against rate limit).
        plain_symbols: set[str] = {s.split(":", 1)[1] if ":" in s else s for s in symbols}
        token_map = await self.get_instrument_tokens(exchange="NSE", symbols=plain_symbols)
        token_to_symbol: dict[int, str] = {
            int(tok): key.split(":", 1)[1]
            for key, tok in token_map.items()
        }
        instrument_tokens = list(token_to_symbol.keys())

        if not instrument_tokens:
            logger.warning(
                "zerodha.subscribe_quotes: no tokens resolved for %d symbols — stream not started",
                len(symbols),
            )
            return

        api_key: str = self._token_manager._api_key or ""
        access_token: str = getattr(self._kite, "access_token", "") or ""
        loop = asyncio.get_running_loop()

        def _on_ticks(ws: Any, ticks: list[dict]) -> None:
            for tick in ticks:
                raw_token = tick.get("instrument_token")
                symbol = token_to_symbol.get(int(raw_token) if raw_token is not None else -1)
                if not symbol:
                    continue
                depth = tick.get("depth") or {}
                buy_levels = depth.get("buy") or []
                sell_levels = depth.get("sell") or []
                quote: dict[str, Any] = {
                    "symbol": symbol,
                    "market": "NSE",
                    "last_price": float(tick.get("last_price") or 0.0),
                    "bid": float(buy_levels[0].get("price", 0.0) if buy_levels else 0.0),
                    "ask": float(sell_levels[0].get("price", 0.0) if sell_levels else 0.0),
                    "bid_quantity": int(buy_levels[0].get("quantity", 0) if buy_levels else 0),
                    "ask_quantity": int(sell_levels[0].get("quantity", 0) if sell_levels else 0),
                    "volume": int(tick.get("volume") or 0),
                    "instrument_token": raw_token,
                    "timestamp": tick.get("timestamp"),
                }
                asyncio.run_coroutine_threadsafe(callback(quote), loop)

        def _on_connect(ws: Any, response: Any) -> None:
            logger.info(
                "zerodha.kite_ticker.connected — subscribing %d instruments",
                len(instrument_tokens),
            )
            ws.subscribe(instrument_tokens)
            ws.set_mode(ws.MODE_FULL, instrument_tokens)

        def _on_close(ws: Any, code: Any, reason: Any) -> None:
            logger.warning("zerodha.kite_ticker.disconnected code=%s reason=%s", code, reason)

        def _on_error(ws: Any, code: Any, reason: Any) -> None:
            logger.error("zerodha.kite_ticker.error code=%s reason=%s", code, reason)

        def _on_reconnect(ws: Any, attempts_count: int) -> None:
            logger.info("zerodha.kite_ticker.reconnecting attempt=%d", attempts_count)

        def _on_noreconnect(ws: Any) -> None:
            logger.critical("zerodha.kite_ticker.no_reconnect — all retry attempts exhausted")

        # Close any existing ticker before starting a fresh one
        if self._ticker is not None:
            try:
                self._ticker.close()
            except Exception:
                pass
            self._ticker = None

        ticker = KiteTicker(api_key=api_key, access_token=access_token)
        ticker.on_ticks = _on_ticks
        ticker.on_connect = _on_connect
        ticker.on_close = _on_close
        ticker.on_error = _on_error
        ticker.on_reconnect = _on_reconnect
        ticker.on_noreconnect = _on_noreconnect

        self._ticker = ticker
        self._subscribed_symbols = list(symbols)
        self._subscribed_callback = callback
        # threaded=True starts KiteTicker in a daemon background thread;
        # this call returns immediately without blocking the event loop.
        ticker.connect(threaded=True)
        logger.info(
            "zerodha.subscribe_quotes.started symbols=%d tokens=%d",
            len(symbols),
            len(instrument_tokens),
        )

    async def refresh_access_token(self, request_token: str) -> str:
        """
        Exchange a request_token for a new access_token and apply it immediately.

        Delegates to ``ZerodhaTokenManager.exchange_request_token()``, which
        stores the token in DynamoDB with a TTL. After this call the broker
        is fully connected and ready to place orders.

        Args:
            request_token: The one-time token from the Kite login redirect.

        Returns:
            The new access token.
        """
        try:
            access_token = await self._token_manager.exchange_request_token(request_token)
        except Exception as exc:
            logger.exception("Zerodha token refresh failed")
            raise BrokerAPIError("Zerodha", str(exc)) from exc

        # Apply the new token to the in-memory KiteConnect client
        if self._kite is not None:
            self._kite.set_access_token(access_token)

        self._connected = True
        self._needs_authentication = False
        logger.info("Zerodha access token refreshed and applied — broker is now connected")
        return access_token

    async def start_token_refresh_loop(self) -> None:
        """
        Background loop — hot-swaps the Zerodha access token daily at 02:00 UTC.

        Wakes at 01:55 UTC (5 min before expiry) and polls DynamoDB every 30s
        until the operator stores a fresh token via ``scripts/zerodha_login.py``.
        Applies the new token in-memory so no container restart is needed.

        Also handles the startup case: if the service starts without a valid token
        (``needs_authentication=True``), polls immediately for up to 30 minutes.
        """
        # If starting without a token, immediately poll so the operator can
        # provide one via zerodha_login.py without restarting the service.
        if self._needs_authentication:
            logger.warning(
                "zerodha.token_refresh: starting in needs_authentication mode — "
                "polling DynamoDB for a valid token (run 'python scripts/zerodha_login.py')"
            )
            new_token = await self._wait_for_fresh_token()
            if new_token:
                await self._apply_fresh_token(new_token)
            else:
                logger.critical(
                    "zerodha.token_refresh: no valid token received after 30 minutes"
                )

        while True:
            secs = self._seconds_until_token_refresh()
            logger.info(
                "zerodha.token_refresh.scheduled — waking in %.0f minutes (01:55 UTC)",
                secs / 60,
            )
            await asyncio.sleep(secs)

            logger.warning(
                "zerodha.token_refresh: Zerodha token expires in ~5 minutes (02:00 UTC). "
                "Run 'python scripts/zerodha_login.py' now to prevent a trading halt."
            )
            new_token = await self._wait_for_fresh_token()
            if new_token:
                await self._apply_fresh_token(new_token)
            else:
                self._needs_authentication = True
                self._connected = False
                logger.critical(
                    "zerodha.token_refresh: no new token found after 30 minutes. "
                    "Broker is disconnected. Run 'python scripts/zerodha_login.py' immediately."
                )

    async def _apply_fresh_token(self, new_token: str) -> None:
        """Apply a freshly obtained access token to the live Kite client in-memory."""
        if self._kite is not None:
            self._kite.set_access_token(new_token)
        self._token_manager._cached_token = new_token
        self._connected = True
        self._needs_authentication = False
        logger.info("zerodha.token_refresh.applied — new token hot-swapped, no restart needed")

        # Re-subscribe the Kite Ticker WebSocket with the new token
        if self._subscribed_symbols and self._subscribed_callback is not None:
            logger.info(
                "zerodha.token_refresh: re-subscribing %d symbols with new token",
                len(self._subscribed_symbols),
            )
            try:
                await self.subscribe_quotes(self._subscribed_symbols, self._subscribed_callback)
            except Exception:
                logger.exception("zerodha.token_refresh: failed to re-subscribe quotes")

    async def _wait_for_fresh_token(
        self,
        poll_interval: float = 30.0,
        max_wait_secs: float = 1800.0,
    ) -> Optional[str]:
        """
        Poll DynamoDB every ``poll_interval`` seconds until a fresh token appears.

        A token is 'fresh' if its ``expires_at`` is more than 30 minutes from now,
        distinguishing it from the current about-to-expire token.

        Returns:
            New access token string, or None if ``max_wait_secs`` elapsed.
        """
        deadline = time.monotonic() + max_wait_secs
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                # Bypass in-memory cache to force a DynamoDB read each poll cycle
                self._token_manager._cached_token = None
                self._token_manager._cached_expires_at = None
                token, expires_at = await self._token_manager._load_token_from_dynamo()
            except Exception:
                logger.debug("zerodha.token_refresh: DynamoDB read failed — will retry")
                continue
            if token and expires_at and expires_at > utc_now() + timedelta(minutes=30):
                logger.info(
                    "zerodha.token_refresh: fresh token found (expires %s)",
                    expires_at.isoformat(),
                )
                return token
        return None

    @staticmethod
    def _seconds_until_token_refresh() -> float:
        """Return seconds until 01:55 UTC (5 min before the 02:00 UTC expiry boundary)."""
        now = datetime.now(timezone.utc)
        target = now.replace(hour=1, minute=55, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        return max(0.0, (target - now).total_seconds())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _translate_order(self, order: OrderRequest) -> dict[str, Any]:
        """Translate a generic OrderRequest to Kite Connect parameters."""
        exchange = _EXCHANGE_NSE  # Default; could use order.metadata for exchange override

        # Map order type
        kite_order_type_map = {
            OrderType.MARKET: "MARKET",
            OrderType.LIMIT: "LIMIT",
            OrderType.STOP: "SL-M",
            OrderType.STOP_LIMIT: "SL",
            OrderType.STOP_LOSS_MARKET: "SL-M",
        }
        kite_order_type = kite_order_type_map.get(order.order_type, "MARKET")

        # Map product type
        kite_product_map = {
            ProductType.CNC: "CNC",
            ProductType.MIS: "MIS",
            ProductType.NRML: "NRML",
            ProductType.DAY: "MIS",  # Default intraday for unmapped
        }
        product = kite_product_map.get(order.product_type, "MIS")

        params: dict[str, Any] = {
            "variety": _VARIETY_REGULAR,
            "exchange": exchange,
            "tradingsymbol": order.symbol,
            "transaction_type": order.side.value,
            "quantity": int(order.quantity),
            "order_type": kite_order_type,
            "product": product,
            "tag": str(order.metadata.get("broker_idempotency_key", order.order_id))[:20],
        }

        if order.limit_price is not None:
            params["price"] = order.limit_price
        if order.stop_price is not None:
            params["trigger_price"] = order.stop_price

        return params

    @staticmethod
    def _translate_status(kite_status: str) -> OrderStatus:
        """Translate Kite Connect order status to internal OrderStatus."""
        status_map = {
            "COMPLETE": OrderStatus.FILLED,
            "REJECTED": OrderStatus.REJECTED,
            "CANCELLED": OrderStatus.CANCELLED,
            "OPEN": OrderStatus.PLACED,
            "PENDING": OrderStatus.PENDING,
            "TRIGGER PENDING": OrderStatus.PLACED,
        }
        return status_map.get(kite_status.upper(), OrderStatus.PLACED)


# ── Error classification ──────────────────────────────────────────────────────

# Lowercase substrings found in Kite Connect rejection messages that identify
# permanent order parameter failures — these will not resolve on retry.
_NON_RETRYABLE_ZERODHA_KEYWORDS: tuple[str, ...] = (
    "insufficient funds",
    "insufficient margin",
    "insufficient balance",
    "margin shortfall",
    "margin exceeded",
    "exceeds available margin",
    "invalid instrument",
    "instrument not found",
    "scrip not found",
    "trading symbol not found",
    "not found",
    "invalid quantity",
    "quantity should be",
    "lot size",
    "order not found",
    "already cancelled",
    "already complete",
    "account blocked",
    "account suspended",
    "account restricted",
    "permission denied",
    "segment not activated",
    "not subscribed",
    "not enabled for trading",
    "cannot be placed",
    "order type not allowed",
    "product type not allowed",
)


def _raise_classified_zerodha_error(exc: Exception) -> None:
    """
    Inspect a Zerodha/kiteconnect exception and raise the correct error subclass.

    kiteconnect raises typed exception subclasses that map directly onto
    permanent vs transient categories:

    Non-retryable (``NonRetryableBrokerError``):
        - ``InputException``       Bad parameters — invalid symbol, bad qty, wrong order type.
        - ``PermissionException``  Account not authorized for the segment/product.
        - ``OrderException``       Order-level rejection — insufficient margin, lot size, etc.
        - ``DataException``        Symbol/instrument lookup failure.

    Token errors (``NonRetryableBrokerError``):
        - ``TokenException``       Access token expired or revoked. Retrying with the same
                                   token will always fail. The token manager must run a
                                   re-auth cycle; this cannot be resolved by the RetryHandler.

    Retryable (``BrokerAPIError``):
        - ``NetworkException``     Transient TCP/SSL failure.
        - ``GeneralException``     Kite 5xx server error.
        - All other exceptions.

    This function always raises — it never returns.

    Args:
        exc: The raw exception from kiteconnect or asyncio.to_thread.

    Raises:
        NonRetryableBrokerError: Permanent failure; do not retry.
        BrokerAPIError: Transient failure; safe to retry.
    """
    # Attempt kiteconnect exception type classification.
    # Import inside the function so the module can load even if kiteconnect
    # is not installed (the broker will raise ImportError in connect() anyway).
    try:
        from kiteconnect import exceptions as ke  # type: ignore[import]

        if isinstance(exc, (ke.InputException, ke.DataException)):
            raise NonRetryableBrokerError("Zerodha", str(exc)) from exc

        if isinstance(exc, ke.TokenException):
            # Token has expired. Mark explicitly as non-retryable — the
            # RetryHandler cannot fix auth; the token manager must re-auth.
            raise NonRetryableBrokerError(
                "Zerodha",
                f"Access token expired or revoked — re-authentication required: {exc}",
            ) from exc

        if isinstance(exc, ke.PermissionException):
            raise NonRetryableBrokerError("Zerodha", str(exc)) from exc

        if isinstance(exc, ke.OrderException):
            # OrderException covers both permanent rejections (margin, lot size)
            # and edge cases. Treat all as non-retryable: the order parameters
            # cannot change between retries, so retrying is pointless.
            raise NonRetryableBrokerError("Zerodha", str(exc)) from exc

        if isinstance(exc, (ke.NetworkException, ke.GeneralException)):
            raise BrokerAPIError("Zerodha", str(exc)) from exc

    except ImportError:
        # kiteconnect not available — fall through to keyword classification.
        pass

    # Keyword-based fallback for environments without kiteconnect installed
    # (e.g., unit tests with mocked exceptions) or unclassified exception types.
    exc_lower = str(exc).lower()
    if any(kw in exc_lower for kw in _NON_RETRYABLE_ZERODHA_KEYWORDS):
        raise NonRetryableBrokerError("Zerodha", str(exc)) from exc

    raise BrokerAPIError("Zerodha", str(exc)) from exc
