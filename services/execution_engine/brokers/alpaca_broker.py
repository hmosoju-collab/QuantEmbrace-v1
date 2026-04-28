"""
Alpaca Broker Client — full BrokerClient implementation for US equities.

Uses the alpaca-py library (NOT the deprecated alpaca-trade-api).

Key features:
  - Paper vs live trading: controlled by ``ALPACA_USE_PAPER=true/false`` env var
    or ``settings.alpaca.use_paper`` (default: True for safety).
  - Credentials: loaded from AWS Secrets Manager first
    (``quantembrace/{env}/alpaca/api-credentials``), falling back to env vars.
  - Order update stream: ``TradingStream`` receives fill/cancel notifications
    in real-time so the execution engine doesn't need to poll.
  - Rate limiting: token-bucket enforces the 200 req/min Alpaca limit.
  - Circuit breaker: inherited RetryHandler wraps all API calls.

Install: pip install alpaca-py

Paper trading endpoint:  https://paper-api.alpaca.markets
Live trading endpoint:   https://api.alpaca.markets
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Callable, Coroutine, Optional

from shared.aws.clients import get_secretsmanager_client
from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger

from execution_engine.brokers.base_broker import BrokerAPIError, BrokerClient, QuoteCallback
from execution_engine.orders.order import (
    OrderRequest,
    OrderResponse,
    OrderStatus,
    OrderStatusUpdate,
    OrderType,
    Position,
)

logger = get_logger(__name__, service_name="execution_engine")

# ── Optional dependency guard ─────────────────────────────────────────────────
try:
    from alpaca.trading.client import TradingClient                     # type: ignore[import]
    from alpaca.trading.stream import TradingStream                     # type: ignore[import]
    from alpaca.trading.requests import (                               # type: ignore[import]
        MarketOrderRequest,
        LimitOrderRequest,
        StopOrderRequest,
        StopLimitOrderRequest,
    )
    from alpaca.trading.enums import (                                  # type: ignore[import]
        OrderSide as AlpacaSide,
        TimeInForce as AlpacaTIF,
        OrderStatus as AlpacaOrderStatus,
    )
    _ALPACA_AVAILABLE = True
except ImportError:
    _ALPACA_AVAILABLE = False
    TradingClient = None    # type: ignore[assignment,misc]
    TradingStream = None    # type: ignore[assignment,misc]

# Secrets Manager path: quantembrace/{env}/alpaca/api-credentials
# Secret value JSON: {"api_key": "...", "api_secret": "..."}
_SECRETS_PATH_TEMPLATE = "quantembrace/{env}/alpaca/api-credentials"


# ── Token-bucket rate limiter ─────────────────────────────────────────────────

class _RateLimiter:
    """Token-bucket rate limiter: 200 requests per 60 seconds (Alpaca limit)."""

    def __init__(self, max_requests: int = 200, per_seconds: float = 60.0) -> None:
        self._max = float(max_requests)
        self._per = per_seconds
        self._tokens = self._max
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self._max,
                self._tokens + (now - self._last) * (self._max / self._per),
            )
            self._last = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) * (self._per / self._max)
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


# ── Main broker client ────────────────────────────────────────────────────────

class AlpacaBroker(BrokerClient):
    """
    Alpaca broker implementation for US equity trading.

    Supports all order types in the ``BrokerClient`` interface plus
    fractional shares, extended hours, and real-time order update streaming.

    Paper vs live mode:
        Controlled by ``ALPACA_USE_PAPER`` environment variable (or
        ``settings.alpaca.use_paper``). Defaults to **True** (paper) so
        that a misconfigured deployment never accidentally places real trades.

    Credentials:
        1. Try AWS Secrets Manager (``quantembrace/{env}/alpaca/api-credentials``).
        2. Fall back to environment variables / settings.

    Usage::

        broker = AlpacaBroker()
        await broker.connect()
        response = await broker.place_order(order_request)
    """

    def __init__(self, settings: Optional[AppSettings] = None) -> None:
        """
        Initialize the Alpaca broker client.

        Args:
            settings: Application settings. Loaded from environment if None.
        """
        self._settings = settings or get_settings()
        self._client: Any = None          # alpaca-py TradingClient (sync REST)
        self._stream: Any = None          # alpaca-py TradingStream (async WS)
        self._stream_task: Optional[asyncio.Task[None]] = None
        self._connected = False
        self._rate_limiter = _RateLimiter(max_requests=200, per_seconds=60.0)

        # Registered callbacks for order update events (fill, cancel, etc.)
        self._order_update_callbacks: list[Callable[[dict[str, Any]], Coroutine]] = []

    # ── BrokerClient interface ────────────────────────────────────────────────

    @property
    def broker_name(self) -> str:
        return "Alpaca"

    @property
    def supported_markets(self) -> list[str]:
        return ["US"]

    async def connect(self) -> None:
        """
        Authenticate with Alpaca and verify the account is active.

        Steps:
            1. Load credentials (Secrets Manager → env var fallback).
            2. Detect paper vs live mode.
            3. Create ``TradingClient`` and verify via ``get_account()``.
            4. Initialize ``TradingStream`` for order update notifications.

        Raises:
            ImportError: alpaca-py is not installed.
            BrokerAPIError: Account is inactive or credentials are invalid.
        """
        if not _ALPACA_AVAILABLE:
            raise ImportError(
                "alpaca-py is not installed. Run: pip install alpaca-py"
            )

        api_key, api_secret = await self._load_credentials()
        paper = self._is_paper_mode()

        logger.info(
            "Connecting to Alpaca (%s mode)", "paper" if paper else "LIVE"
        )

        try:
            self._client = TradingClient(
                api_key=api_key,
                secret_key=api_secret,
                paper=paper,
            )

            # Verify credentials and log account state
            account = await asyncio.to_thread(self._client.get_account)
            if account.status.value not in ("ACTIVE", "active"):
                raise BrokerAPIError(
                    "Alpaca",
                    f"Account is not active (status={account.status})",
                )

            logger.info(
                "Alpaca connected: id=%s status=%s equity=%s "
                "buying_power=%s paper=%s",
                account.id,
                account.status,
                account.equity,
                account.buying_power,
                paper,
            )

            # Initialize order update stream
            self._stream = TradingStream(
                api_key=api_key,
                secret_key=api_secret,
                paper=paper,
            )
            self._stream.subscribe_trade_updates(self._on_trade_update)

            self._connected = True

        except BrokerAPIError:
            raise
        except Exception as exc:
            raise BrokerAPIError("Alpaca", f"Connection failed: {exc}") from exc

    async def disconnect(self) -> None:
        """Stop the order update stream and reset the client."""
        logger.info("Disconnecting from Alpaca")

        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass

        self._client = None
        self._stream = None
        self._connected = False
        logger.info("Alpaca disconnected")

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """
        Submit an order to Alpaca.

        Translates the generic ``OrderRequest`` to the correct alpaca-py
        request model (Market / Limit / Stop / StopLimit). Fractional
        quantities are passed directly — Alpaca supports sub-share trading.

        Args:
            order: The order to place.

        Returns:
            ``OrderResponse`` with the Alpaca-assigned broker order ID.

        Raises:
            BrokerAPIError: Alpaca rejected the order or the client is not connected.
        """
        self._require_connected()
        await self._rate_limiter.acquire()

        try:
            request = self._build_order_request(order)
            result = await asyncio.to_thread(self._client.submit_order, request)

            logger.info(
                "Alpaca order placed: internal=%s broker=%s symbol=%s "
                "side=%s qty=%s type=%s",
                order.order_id,
                result.id,
                order.symbol,
                order.side.value,
                order.quantity,
                order.order_type.value,
            )

            return OrderResponse(
                order_id=order.order_id,
                broker_order_id=str(result.id),
                status=OrderStatus.PLACED,
                symbol=order.symbol,
                market=order.market,
                broker_message=str(result.status),
            )

        except BrokerAPIError:
            raise
        except Exception as exc:
            logger.exception("Alpaca order placement failed for %s", order.order_id)
            raise BrokerAPIError("Alpaca", str(exc)) from exc

    async def cancel_order(self, broker_order_id: str) -> OrderStatusUpdate:
        """
        Cancel an open Alpaca order.

        Args:
            broker_order_id: UUID string assigned by Alpaca.

        Returns:
            ``OrderStatusUpdate`` with status CANCELLED.
        """
        self._require_connected()
        await self._rate_limiter.acquire()

        try:
            await asyncio.to_thread(
                self._client.cancel_order_by_id, broker_order_id
            )
            logger.info("Alpaca order cancelled: broker_id=%s", broker_order_id)
            return OrderStatusUpdate(
                order_id="",
                broker_order_id=broker_order_id,
                previous_status=OrderStatus.PLACED,
                new_status=OrderStatus.CANCELLED,
                broker_message="cancelled",
            )
        except Exception as exc:
            logger.exception("Alpaca cancel failed: broker_id=%s", broker_order_id)
            raise BrokerAPIError("Alpaca", str(exc)) from exc

    async def get_order_status(self, broker_order_id: str) -> OrderStatusUpdate:
        """
        Query the current status of an Alpaca order.

        Args:
            broker_order_id: UUID string assigned by Alpaca.

        Returns:
            ``OrderStatusUpdate`` with latest fill information.
        """
        self._require_connected()
        await self._rate_limiter.acquire()

        try:
            order = await asyncio.to_thread(
                self._client.get_order_by_id, broker_order_id
            )
            status = _translate_alpaca_status(str(order.status))
            filled_qty = float(order.filled_qty or 0)
            avg_price = float(order.filled_avg_price or 0)

            return OrderStatusUpdate(
                order_id="",
                broker_order_id=broker_order_id,
                previous_status=OrderStatus.PLACED,
                new_status=status,
                filled_quantity=filled_qty,
                avg_fill_price=avg_price,
                broker_message=str(order.status),
            )
        except Exception as exc:
            logger.exception("Alpaca status query failed: broker_id=%s", broker_order_id)
            raise BrokerAPIError("Alpaca", str(exc)) from exc

    async def get_positions(self) -> list[Position]:
        """
        Retrieve all current Alpaca positions as normalized ``Position`` objects.

        Returns:
            List of ``Position`` pydantic models. Empty list if no open positions.
        """
        self._require_connected()
        await self._rate_limiter.acquire()

        try:
            raw_positions = await asyncio.to_thread(
                self._client.get_all_positions
            )
            return [self._normalize_position(p) for p in raw_positions]
        except Exception as exc:
            logger.exception("Failed to fetch Alpaca positions")
            raise BrokerAPIError("Alpaca", str(exc)) from exc

    async def subscribe_quotes(
        self, symbols: list[str], callback: QuoteCallback
    ) -> None:
        """
        Register a callback for real-time order update events (fills, cancels).

        Note: For Alpaca the execution engine needs *order* updates, not quote
        updates. This method registers the callback against the TradingStream
        trade_updates channel and starts the stream background task if not
        already running.

        Args:
            symbols: Ignored for Alpaca — order updates cover all symbols.
            callback: Async callback invoked with a dict containing event
                      type and order details on each update.
        """
        self._order_update_callbacks.append(callback)
        logger.info(
            "Registered order update callback (total=%d)",
            len(self._order_update_callbacks),
        )
        # Start the stream background task on first subscription
        await self._start_stream()

    # ── Order update stream ───────────────────────────────────────────────────

    async def _start_stream(self) -> None:
        """Start the TradingStream background task if not already running."""
        if self._stream is None:
            logger.warning("TradingStream not initialized — connect() first")
            return
        if self._stream_task is None or self._stream_task.done():
            self._stream_task = asyncio.create_task(
                self._run_stream(), name="alpaca_trading_stream"
            )
            logger.info("Alpaca TradingStream background task started")

    async def _run_stream(self) -> None:
        """Background coroutine that runs the TradingStream until cancelled."""
        try:
            logger.info("Alpaca TradingStream running")
            await self._stream.run()
        except asyncio.CancelledError:
            logger.info("Alpaca TradingStream task cancelled")
        except Exception:
            logger.exception(
                "Alpaca TradingStream exited with error — order updates stopped"
            )

    async def _on_trade_update(self, update: Any) -> None:
        """
        Handle a trade update event from the Alpaca TradingStream.

        Translates the alpaca-py update object into a plain dict and
        fans it out to all registered callbacks.

        Args:
            update: alpaca-py ``TradeUpdate`` object containing event type
                    and order details.
        """
        try:
            event_type = str(getattr(update, "event", "unknown"))
            order = getattr(update, "order", None)

            payload: dict[str, Any] = {
                "event": event_type,
                "broker_order_id": str(getattr(order, "id", "")),
                "symbol": str(getattr(order, "symbol", "")),
                "status": str(getattr(order, "status", "")),
                "filled_qty": float(getattr(order, "filled_qty", 0) or 0),
                "filled_avg_price": float(
                    getattr(order, "filled_avg_price", 0) or 0
                ),
                "client_order_id": str(getattr(order, "client_order_id", "")),
            }

            logger.debug(
                "Alpaca trade update: event=%s order=%s symbol=%s status=%s",
                event_type,
                payload["broker_order_id"],
                payload["symbol"],
                payload["status"],
            )

            for cb in self._order_update_callbacks:
                try:
                    await cb(payload)
                except Exception:
                    logger.exception("Order update callback raised an exception")

        except Exception:
            logger.exception("Error processing Alpaca trade update")

    # ── Credential loading ────────────────────────────────────────────────────

    async def _load_credentials(self) -> tuple[str, str]:
        """
        Load Alpaca API credentials.

        Priority:
            1. AWS Secrets Manager (``quantembrace/{env}/alpaca/api-credentials``)
            2. ``ALPACA_API_KEY`` / ``ALPACA_API_SECRET`` environment variables
               (via pydantic settings)

        Returns:
            Tuple of (api_key, api_secret).
        """
        env = self._settings.environment.value
        secret_name = _SECRETS_PATH_TEMPLATE.format(env=env)

        try:
            sm = get_secretsmanager_client()
            response = await asyncio.to_thread(
                sm.get_secret_value, SecretId=secret_name
            )
            secret = json.loads(response["SecretString"])
            api_key = secret["api_key"]
            api_secret = secret["api_secret"]
            logger.info(
                "Alpaca credentials loaded from Secrets Manager (%s)", secret_name
            )
            return api_key, api_secret
        except Exception:
            logger.info(
                "Secrets Manager unavailable or secret not found (%s) — "
                "falling back to environment variables",
                secret_name,
            )

        # Fallback: pydantic settings (env vars)
        return (
            self._settings.alpaca.api_key.get_secret_value(),
            self._settings.alpaca.api_secret.get_secret_value(),
        )

    def _is_paper_mode(self) -> bool:
        """
        Determine whether to use paper or live trading.

        Checks (in priority order):
            1. ``ALPACA_USE_PAPER`` environment variable ("true" / "false").
            2. ``settings.alpaca.use_paper`` (default: True).

        Returns:
            True for paper trading, False for live trading.
        """
        env_val = os.environ.get("ALPACA_USE_PAPER", "").lower()
        if env_val in ("true", "1", "yes"):
            return True
        if env_val in ("false", "0", "no"):
            return False
        return self._settings.alpaca.use_paper

    # ── Order translation ─────────────────────────────────────────────────────

    def _build_order_request(self, order: OrderRequest) -> Any:
        """
        Translate a generic ``OrderRequest`` to the correct alpaca-py request model.

        Alpaca request models (alpaca-py):
            MarketOrderRequest  — no price params
            LimitOrderRequest   — requires limit_price
            StopOrderRequest    — requires stop_price
            StopLimitOrderRequest — requires both

        Fractional quantities are passed directly; alpaca-py handles them.

        Args:
            order: Generic order request from the execution engine.

        Returns:
            An alpaca-py order request model ready for ``client.submit_order()``.
        """
        side = AlpacaSide.BUY if order.side.value == "BUY" else AlpacaSide.SELL
        tif = _map_time_in_force(order.time_in_force)

        common: dict[str, Any] = {
            "symbol": order.symbol,
            "qty": order.quantity,
            "side": side,
            "time_in_force": tif,
            "client_order_id": order.order_id,  # idempotency key
        }
        if order.extended_hours:
            common["extended_hours"] = True

        ot = order.order_type
        if ot == OrderType.MARKET:
            return MarketOrderRequest(**common)
        if ot == OrderType.LIMIT:
            return LimitOrderRequest(limit_price=order.limit_price, **common)
        if ot in (OrderType.STOP, OrderType.STOP_LOSS_MARKET):
            return StopOrderRequest(stop_price=order.stop_price, **common)
        if ot == OrderType.STOP_LIMIT:
            return StopLimitOrderRequest(
                limit_price=order.limit_price,
                stop_price=order.stop_price,
                **common,
            )
        # Default to market for unknown types
        logger.warning(
            "Unknown order type %s — defaulting to MARKET", ot.value
        )
        return MarketOrderRequest(**common)

    # ── Position normalisation ────────────────────────────────────────────────

    @staticmethod
    def _normalize_position(raw: Any) -> Position:
        """
        Convert an alpaca-py ``Position`` object to a normalized ``Position`` model.

        Alpaca position attributes:
            symbol, qty, avg_entry_price, current_price, market_value,
            unrealized_pl, unrealized_plpc, side, exchange, asset_class

        Args:
            raw: alpaca-py Position object.

        Returns:
            Normalized ``Position`` pydantic model.
        """
        qty = float(getattr(raw, "qty", 0) or 0)
        return Position(
            symbol=str(getattr(raw, "symbol", "")),
            market="US",
            side=str(getattr(raw, "side", "long")),
            quantity=qty,
            average_entry_price=float(getattr(raw, "avg_entry_price", 0) or 0),
            current_price=float(getattr(raw, "current_price", 0) or 0),
            market_value=float(getattr(raw, "market_value", 0) or 0),
            unrealized_pnl=float(getattr(raw, "unrealized_pl", 0) or 0),
            unrealized_pnl_pct=float(getattr(raw, "unrealized_plpc", 0) or 0),
            broker="alpaca",
            exchange=str(getattr(raw, "exchange", "")),
            asset_class=str(getattr(raw, "asset_class", "equity")),
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _require_connected(self) -> None:
        """Raise BrokerAPIError if not connected."""
        if not self._connected or self._client is None:
            raise BrokerAPIError(
                "Alpaca",
                "Not connected — call connect() first",
            )


# ── Module-level translation helpers ─────────────────────────────────────────

def _translate_alpaca_status(alpaca_status: str) -> OrderStatus:
    """
    Map an Alpaca order status string to internal ``OrderStatus``.

    Args:
        alpaca_status: Raw status string from Alpaca API.

    Returns:
        Corresponding ``OrderStatus`` enum value.
    """
    _MAP = {
        "new": OrderStatus.PLACED,
        "accepted": OrderStatus.PLACED,
        "pending_new": OrderStatus.PENDING,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "filled": OrderStatus.FILLED,
        "done_for_day": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELLED,
        "cancelled": OrderStatus.CANCELLED,
        "expired": OrderStatus.CANCELLED,
        "replaced": OrderStatus.PLACED,
        "rejected": OrderStatus.REJECTED,
        "held": OrderStatus.PENDING,
        "accepted_for_bidding": OrderStatus.PLACED,
    }
    return _MAP.get(alpaca_status.lower(), OrderStatus.PLACED)


def _map_time_in_force(tif: str) -> Any:
    """
    Map a time-in-force string to an alpaca-py ``TimeInForce`` enum.

    Args:
        tif: Time-in-force string (e.g., "DAY", "GTC", "IOC").

    Returns:
        ``AlpacaTIF`` enum value (defaults to DAY).
    """
    if not _ALPACA_AVAILABLE:
        return tif
    _MAP = {
        "DAY": AlpacaTIF.DAY,
        "GTC": AlpacaTIF.GTC,
        "IOC": AlpacaTIF.IOC,
        "FOK": AlpacaTIF.FOK,
        "OPG": AlpacaTIF.OPG,
        "CLS": AlpacaTIF.CLS,
    }
    return _MAP.get(tif.upper(), AlpacaTIF.DAY)
