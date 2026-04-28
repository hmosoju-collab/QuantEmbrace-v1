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
from datetime import datetime, timezone
from typing import Any, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger

from execution_engine.auth.zerodha_auth import TokenExpiredError, ZerodhaTokenManager
from execution_engine.brokers.base_broker import BrokerAPIError, BrokerClient, QuoteCallback
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
        self._needs_authentication = False  # set True when token is missing/expired
        self._order_rate_limiter = _RateLimiter(max_requests=10, per_seconds=1.0)
        self._historical_rate_limiter = _RateLimiter(max_requests=3, per_seconds=1.0)

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
            raise BrokerAPIError("Zerodha", str(exc)) from exc

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
            raise BrokerAPIError("Zerodha", str(exc)) from exc

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
            raise BrokerAPIError("Zerodha", str(exc)) from exc

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
            raise BrokerAPIError("Zerodha", str(exc)) from exc

    async def subscribe_quotes(
        self, symbols: list[str], callback: QuoteCallback
    ) -> None:
        """
        Subscribe to real-time quotes via Kite Ticker WebSocket.

        Args:
            symbols: List of NSE/BSE symbols to subscribe.
            callback: Async callback for each quote update.
        """
        logger.info("Zerodha quote subscription requested for %d symbols", len(symbols))
        # TODO: Implement KiteTicker WebSocket subscription
        # This requires instrument token lookup and WebSocket lifecycle management

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
            "tag": order.order_id[:20],  # Kite tag max 20 chars
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
