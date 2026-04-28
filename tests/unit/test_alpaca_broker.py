"""
Unit tests for AlpacaBroker.

All tests mock out alpaca-py and boto3 so no real network calls are made.
Run with:  pytest tests/unit/test_alpaca_broker.py -v
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_settings(paper: bool = True) -> Any:
    """Return a minimal mock settings object."""
    s = MagicMock()
    s.environment.value = "development"
    s.alpaca.api_key.get_secret_value.return_value = "PKTEST_KEY"
    s.alpaca.api_secret.get_secret_value.return_value = "TEST_SECRET"
    s.alpaca.use_paper = paper
    s.alpaca.base_url = "https://paper-api.alpaca.markets"
    return s


def _make_alpaca_order(
    order_id: str = "alpaca-uuid-123",
    status: str = "new",
    filled_qty: float = 0.0,
    filled_avg_price: float = 0.0,
    client_order_id: str = "internal-order-id",
) -> Any:
    """Return a minimal mock Alpaca order object."""
    o = MagicMock()
    o.id = order_id
    o.status = MagicMock()
    o.status.__str__ = lambda self: status
    o.filled_qty = filled_qty
    o.filled_avg_price = filled_avg_price
    o.client_order_id = client_order_id
    return o


def _make_alpaca_position(
    symbol: str = "AAPL",
    qty: float = 10.0,
    avg_entry_price: float = 150.00,
    current_price: float = 155.00,
    market_value: float = 1550.00,
    unrealized_pl: float = 50.00,
    unrealized_plpc: float = 0.033,
    side: str = "long",
    exchange: str = "NASDAQ",
    asset_class: str = "us_equity",
) -> Any:
    """Return a minimal mock Alpaca position object."""
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    p.avg_entry_price = avg_entry_price
    p.current_price = current_price
    p.market_value = market_value
    p.unrealized_pl = unrealized_pl
    p.unrealized_plpc = unrealized_plpc
    p.side = side
    p.exchange = exchange
    p.asset_class = asset_class
    return p


def _make_account(status: str = "ACTIVE", equity: str = "100000", buying_power: str = "200000") -> Any:
    account = MagicMock()
    account.id = "test-account-id"
    account.status = MagicMock()
    account.status.value = status
    account.equity = equity
    account.buying_power = buying_power
    return account


# ── Imports under test (with alpaca-py mocked at import time) ─────────────────

# We patch the alpaca-py imports before importing our module
@pytest.fixture(autouse=True)
def mock_alpaca_imports():
    """
    Mock all alpaca-py imports so tests run without the package installed.
    """
    mock_trading_client_class = MagicMock()
    mock_trading_stream_class = MagicMock()
    mock_market_request = MagicMock()
    mock_limit_request = MagicMock()
    mock_stop_request = MagicMock()
    mock_stop_limit_request = MagicMock()
    mock_side_enum = MagicMock()
    mock_side_enum.BUY = "buy"
    mock_side_enum.SELL = "sell"
    mock_tif_enum = MagicMock()
    mock_tif_enum.DAY = "day"
    mock_tif_enum.GTC = "gtc"

    with patch.dict(
        "sys.modules",
        {
            "alpaca": MagicMock(),
            "alpaca.trading": MagicMock(),
            "alpaca.trading.client": MagicMock(TradingClient=mock_trading_client_class),
            "alpaca.trading.stream": MagicMock(TradingStream=mock_trading_stream_class),
            "alpaca.trading.requests": MagicMock(
                MarketOrderRequest=mock_market_request,
                LimitOrderRequest=mock_limit_request,
                StopOrderRequest=mock_stop_request,
                StopLimitOrderRequest=mock_stop_limit_request,
            ),
            "alpaca.trading.enums": MagicMock(
                OrderSide=mock_side_enum,
                TimeInForce=mock_tif_enum,
                OrderStatus=MagicMock(),
            ),
        },
    ):
        yield {
            "TradingClient": mock_trading_client_class,
            "TradingStream": mock_trading_stream_class,
        }


@pytest.fixture
def broker(mock_alpaca_imports):
    """Return a fresh AlpacaBroker with mock settings, not yet connected."""
    # Import here so the sys.modules patch above is already active
    import importlib
    import sys
    # Force re-import to pick up the mocked modules
    if "execution_engine.brokers.alpaca_broker" in sys.modules:
        del sys.modules["execution_engine.brokers.alpaca_broker"]

    # Also patch _ALPACA_AVAILABLE to True
    with patch("execution_engine.brokers.alpaca_broker._ALPACA_AVAILABLE", True):
        from execution_engine.brokers.alpaca_broker import AlpacaBroker
        b = AlpacaBroker(settings=_make_settings(paper=True))
        b._client = MagicMock()  # pre-wire a mock client
        b._connected = True
        return b


@pytest.fixture
def order_request():
    """Return a sample OrderRequest for AAPL BUY MARKET."""
    from execution_engine.orders.order import OrderRequest, OrderSide, OrderType
    return OrderRequest(
        signal_id="signal-001",
        risk_decision_id="risk-001",
        symbol="AAPL",
        market="US",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=10.0,
    )


# ── connect() tests ───────────────────────────────────────────────────────────

class TestConnect:

    @pytest.mark.asyncio
    async def test_connect_paper_mode_uses_paper_client(self, mock_alpaca_imports):
        """connect() passes paper=True to TradingClient when use_paper=True."""
        with patch("execution_engine.brokers.alpaca_broker._ALPACA_AVAILABLE", True), \
             patch("execution_engine.brokers.alpaca_broker.TradingClient") as mock_client_cls, \
             patch("execution_engine.brokers.alpaca_broker.TradingStream"), \
             patch("execution_engine.brokers.alpaca_broker.get_secretsmanager_client") as mock_sm:

            mock_sm.return_value.get_secret_value.side_effect = Exception("no secret")
            mock_instance = MagicMock()
            mock_instance.get_account = MagicMock(return_value=_make_account())
            mock_client_cls.return_value = mock_instance

            from execution_engine.brokers.alpaca_broker import AlpacaBroker
            b = AlpacaBroker(settings=_make_settings(paper=True))

            with patch.dict("os.environ", {"ALPACA_USE_PAPER": "true"}):
                await b.connect()

            mock_client_cls.assert_called_once()
            call_kwargs = mock_client_cls.call_args[1]
            assert call_kwargs["paper"] is True

    @pytest.mark.asyncio
    async def test_connect_live_mode_env_override(self, mock_alpaca_imports):
        """ALPACA_USE_PAPER=false env var forces live mode even if settings.use_paper=True."""
        with patch("execution_engine.brokers.alpaca_broker._ALPACA_AVAILABLE", True), \
             patch("execution_engine.brokers.alpaca_broker.TradingClient") as mock_client_cls, \
             patch("execution_engine.brokers.alpaca_broker.TradingStream"), \
             patch("execution_engine.brokers.alpaca_broker.get_secretsmanager_client") as mock_sm:

            mock_sm.return_value.get_secret_value.side_effect = Exception("no secret")
            mock_instance = MagicMock()
            mock_instance.get_account = MagicMock(return_value=_make_account())
            mock_client_cls.return_value = mock_instance

            from execution_engine.brokers.alpaca_broker import AlpacaBroker
            b = AlpacaBroker(settings=_make_settings(paper=True))  # settings says paper

            with patch.dict("os.environ", {"ALPACA_USE_PAPER": "false"}):
                await b.connect()

            call_kwargs = mock_client_cls.call_args[1]
            assert call_kwargs["paper"] is False  # env var wins

    @pytest.mark.asyncio
    async def test_connect_secrets_manager_credentials(self, mock_alpaca_imports):
        """connect() uses Secrets Manager credentials when available."""
        with patch("execution_engine.brokers.alpaca_broker._ALPACA_AVAILABLE", True), \
             patch("execution_engine.brokers.alpaca_broker.TradingClient") as mock_client_cls, \
             patch("execution_engine.brokers.alpaca_broker.TradingStream"), \
             patch("execution_engine.brokers.alpaca_broker.get_secretsmanager_client") as mock_sm:

            mock_sm.return_value.get_secret_value = MagicMock(
                return_value={"SecretString": json.dumps({"api_key": "SM_KEY", "api_secret": "SM_SECRET"})}
            )
            mock_instance = MagicMock()
            mock_instance.get_account = MagicMock(return_value=_make_account())
            mock_client_cls.return_value = mock_instance

            from execution_engine.brokers.alpaca_broker import AlpacaBroker
            b = AlpacaBroker(settings=_make_settings())

            with patch.dict("os.environ", {"ALPACA_USE_PAPER": "true"}):
                await b.connect()

            call_kwargs = mock_client_cls.call_args[1]
            assert call_kwargs["api_key"] == "SM_KEY"
            assert call_kwargs["secret_key"] == "SM_SECRET"

    @pytest.mark.asyncio
    async def test_connect_raises_on_inactive_account(self, mock_alpaca_imports):
        """connect() raises BrokerAPIError when account status is not ACTIVE."""
        with patch("execution_engine.brokers.alpaca_broker._ALPACA_AVAILABLE", True), \
             patch("execution_engine.brokers.alpaca_broker.TradingClient") as mock_client_cls, \
             patch("execution_engine.brokers.alpaca_broker.TradingStream"), \
             patch("execution_engine.brokers.alpaca_broker.get_secretsmanager_client") as mock_sm:

            mock_sm.return_value.get_secret_value.side_effect = Exception("no secret")
            mock_instance = MagicMock()
            mock_instance.get_account = MagicMock(return_value=_make_account(status="INACTIVE"))
            mock_client_cls.return_value = mock_instance

            from execution_engine.brokers.alpaca_broker import AlpacaBroker
            from execution_engine.brokers.base_broker import BrokerAPIError
            b = AlpacaBroker(settings=_make_settings())

            with patch.dict("os.environ", {"ALPACA_USE_PAPER": "true"}), \
                 pytest.raises(BrokerAPIError, match="not active"):
                await b.connect()


# ── place_order() tests ───────────────────────────────────────────────────────

class TestPlaceOrder:

    @pytest.mark.asyncio
    async def test_place_market_order_returns_response(self, broker, order_request):
        """place_order() returns OrderResponse with broker-assigned ID on success."""
        with patch("execution_engine.brokers.alpaca_broker.MarketOrderRequest") as mock_req_cls, \
             patch("execution_engine.brokers.alpaca_broker._ALPACA_AVAILABLE", True):

            mock_order = _make_alpaca_order(order_id="alpaca-999", status="new")
            broker._client.submit_order = MagicMock(return_value=mock_order)

            from execution_engine.brokers.alpaca_broker import AlpacaBroker
            from execution_engine.orders.order import OrderStatus

            response = await broker.place_order(order_request)

            assert response.broker_order_id == "alpaca-999"
            assert response.status == OrderStatus.PLACED
            assert response.symbol == "AAPL"

    @pytest.mark.asyncio
    async def test_place_order_raises_when_not_connected(self, order_request):
        """place_order() raises BrokerAPIError when client is None."""
        with patch("execution_engine.brokers.alpaca_broker._ALPACA_AVAILABLE", True):
            from execution_engine.brokers.alpaca_broker import AlpacaBroker
            from execution_engine.brokers.base_broker import BrokerAPIError

            b = AlpacaBroker(settings=_make_settings())
            # Intentionally not connected
            with pytest.raises(BrokerAPIError, match="Not connected"):
                await b.place_order(order_request)

    @pytest.mark.asyncio
    async def test_place_order_wraps_broker_exception(self, broker, order_request):
        """place_order() wraps unexpected exceptions as BrokerAPIError."""
        with patch("execution_engine.brokers.alpaca_broker._ALPACA_AVAILABLE", True), \
             patch("execution_engine.brokers.alpaca_broker.MarketOrderRequest"):

            broker._client.submit_order = MagicMock(
                side_effect=RuntimeError("broker down")
            )

            from execution_engine.brokers.base_broker import BrokerAPIError
            with pytest.raises(BrokerAPIError):
                await broker.place_order(order_request)

    @pytest.mark.asyncio
    async def test_place_limit_order_uses_limit_request(self, broker):
        """place_order() selects LimitOrderRequest for LIMIT order type."""
        from execution_engine.orders.order import OrderRequest, OrderSide, OrderType

        limit_order = OrderRequest(
            signal_id="sig-002",
            risk_decision_id="risk-002",
            symbol="MSFT",
            market="US",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=5.0,
            limit_price=280.00,
        )

        with patch("execution_engine.brokers.alpaca_broker.LimitOrderRequest") as mock_limit_cls, \
             patch("execution_engine.brokers.alpaca_broker._ALPACA_AVAILABLE", True):

            mock_order = _make_alpaca_order(order_id="alpaca-456", status="new")
            broker._client.submit_order = MagicMock(return_value=mock_order)
            mock_limit_cls.return_value = MagicMock()

            await broker.place_order(limit_order)

            mock_limit_cls.assert_called_once()
            call_kwargs = mock_limit_cls.call_args[1]
            assert call_kwargs["limit_price"] == 280.00


# ── cancel_order() tests ──────────────────────────────────────────────────────

class TestCancelOrder:

    @pytest.mark.asyncio
    async def test_cancel_order_calls_api(self, broker):
        """cancel_order() calls cancel_order_by_id and returns CANCELLED status."""
        from execution_engine.orders.order import OrderStatus

        broker._client.cancel_order_by_id = MagicMock(return_value=None)

        result = await broker.cancel_order("alpaca-uuid-999")

        broker._client.cancel_order_by_id.assert_called_once_with("alpaca-uuid-999")
        assert result.new_status == OrderStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_order_raises_on_api_error(self, broker):
        """cancel_order() raises BrokerAPIError when the API call fails."""
        from execution_engine.brokers.base_broker import BrokerAPIError

        broker._client.cancel_order_by_id = MagicMock(
            side_effect=Exception("order not found")
        )

        with pytest.raises(BrokerAPIError):
            await broker.cancel_order("alpaca-bad-id")


# ── get_order_status() tests ──────────────────────────────────────────────────

class TestGetOrderStatus:

    @pytest.mark.asyncio
    async def test_get_order_status_filled(self, broker):
        """get_order_status() maps 'filled' to OrderStatus.FILLED with fill data."""
        from execution_engine.orders.order import OrderStatus

        broker._client.get_order_by_id = MagicMock(
            return_value=_make_alpaca_order(
                status="filled",
                filled_qty=10.0,
                filled_avg_price=151.23,
            )
        )

        result = await broker.get_order_status("alpaca-filled-id")

        assert result.new_status == OrderStatus.FILLED
        assert result.filled_quantity == 10.0
        assert result.avg_fill_price == 151.23

    @pytest.mark.asyncio
    async def test_get_order_status_partial_fill(self, broker):
        """get_order_status() maps 'partially_filled' to PARTIALLY_FILLED."""
        from execution_engine.orders.order import OrderStatus

        broker._client.get_order_by_id = MagicMock(
            return_value=_make_alpaca_order(
                status="partially_filled",
                filled_qty=5.0,
                filled_avg_price=150.00,
            )
        )

        result = await broker.get_order_status("alpaca-partial-id")
        assert result.new_status == OrderStatus.PARTIALLY_FILLED
        assert result.filled_quantity == 5.0

    @pytest.mark.asyncio
    async def test_get_order_status_cancelled(self, broker):
        """get_order_status() maps 'canceled' to CANCELLED."""
        from execution_engine.orders.order import OrderStatus

        broker._client.get_order_by_id = MagicMock(
            return_value=_make_alpaca_order(status="canceled")
        )

        result = await broker.get_order_status("alpaca-cancelled-id")
        assert result.new_status == OrderStatus.CANCELLED


# ── get_positions() tests ─────────────────────────────────────────────────────

class TestGetPositions:

    @pytest.mark.asyncio
    async def test_get_positions_returns_normalized_models(self, broker):
        """get_positions() returns a list of Position pydantic models."""
        from execution_engine.orders.order import Position

        broker._client.get_all_positions = MagicMock(
            return_value=[
                _make_alpaca_position("AAPL", qty=10.0, current_price=155.0),
                _make_alpaca_position("MSFT", qty=5.0, current_price=280.0),
            ]
        )

        positions = await broker.get_positions()

        assert len(positions) == 2
        assert all(isinstance(p, Position) for p in positions)
        assert positions[0].symbol == "AAPL"
        assert positions[0].quantity == 10.0
        assert positions[0].market == "US"
        assert positions[0].broker == "alpaca"
        assert positions[1].symbol == "MSFT"

    @pytest.mark.asyncio
    async def test_get_positions_empty(self, broker):
        """get_positions() returns an empty list when no positions are open."""
        broker._client.get_all_positions = MagicMock(return_value=[])
        positions = await broker.get_positions()
        assert positions == []


# ── Status translation tests ──────────────────────────────────────────────────

class TestStatusTranslation:

    def test_all_known_statuses_translate(self):
        """All Alpaca status strings in the map translate without KeyError."""
        from execution_engine.brokers.alpaca_broker import _translate_alpaca_status
        from execution_engine.orders.order import OrderStatus

        cases = {
            "new": OrderStatus.PLACED,
            "accepted": OrderStatus.PLACED,
            "pending_new": OrderStatus.PENDING,
            "partially_filled": OrderStatus.PARTIALLY_FILLED,
            "filled": OrderStatus.FILLED,
            "done_for_day": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELLED,
            "cancelled": OrderStatus.CANCELLED,
            "expired": OrderStatus.CANCELLED,
            "rejected": OrderStatus.REJECTED,
        }
        for alpaca_status, expected in cases.items():
            assert _translate_alpaca_status(alpaca_status) == expected, alpaca_status

    def test_unknown_status_defaults_to_placed(self):
        """Unknown Alpaca status strings default to PLACED without crashing."""
        from execution_engine.brokers.alpaca_broker import _translate_alpaca_status
        from execution_engine.orders.order import OrderStatus

        result = _translate_alpaca_status("some_future_status")
        assert result == OrderStatus.PLACED


# ── Paper mode detection tests ────────────────────────────────────────────────

class TestPaperMode:

    def test_env_var_true_overrides_settings_false(self):
        """ALPACA_USE_PAPER=true forces paper mode even if settings says live."""
        with patch("execution_engine.brokers.alpaca_broker._ALPACA_AVAILABLE", True):
            from execution_engine.brokers.alpaca_broker import AlpacaBroker
            b = AlpacaBroker(settings=_make_settings(paper=False))

            import os
            with patch.dict(os.environ, {"ALPACA_USE_PAPER": "true"}):
                assert b._is_paper_mode() is True

    def test_env_var_false_overrides_settings_true(self):
        """ALPACA_USE_PAPER=false forces live mode even if settings says paper."""
        with patch("execution_engine.brokers.alpaca_broker._ALPACA_AVAILABLE", True):
            from execution_engine.brokers.alpaca_broker import AlpacaBroker
            b = AlpacaBroker(settings=_make_settings(paper=True))

            import os
            with patch.dict(os.environ, {"ALPACA_USE_PAPER": "false"}):
                assert b._is_paper_mode() is False

    def test_settings_fallback_when_env_not_set(self):
        """When ALPACA_USE_PAPER is absent, settings.alpaca.use_paper is used."""
        with patch("execution_engine.brokers.alpaca_broker._ALPACA_AVAILABLE", True):
            from execution_engine.brokers.alpaca_broker import AlpacaBroker
            b_paper = AlpacaBroker(settings=_make_settings(paper=True))
            b_live = AlpacaBroker(settings=_make_settings(paper=False))

            import os
            env = {k: v for k, v in os.environ.items() if k != "ALPACA_USE_PAPER"}
            with patch.dict(os.environ, env, clear=True):
                assert b_paper._is_paper_mode() is True
                assert b_live._is_paper_mode() is False


# ── Rate limiter tests ────────────────────────────────────────────────────────

class TestRateLimiter:

    @pytest.mark.asyncio
    async def test_rate_limiter_allows_burst(self):
        """Rate limiter allows up to max_requests without waiting."""
        from execution_engine.brokers.alpaca_broker import _RateLimiter

        rl = _RateLimiter(max_requests=5, per_seconds=60.0)
        # Should complete instantly — no sleep needed for first 5 tokens
        for _ in range(5):
            await rl.acquire()

    @pytest.mark.asyncio
    async def test_rate_limiter_token_refill(self):
        """Rate limiter refills tokens over time."""
        from execution_engine.brokers.alpaca_broker import _RateLimiter

        rl = _RateLimiter(max_requests=1, per_seconds=0.1)
        await rl.acquire()  # consume the only token
        # Tokens refill at 1/0.1 = 10 per second; after 0.15s we should have ~1.5 tokens
        await asyncio.sleep(0.15)
        await rl.acquire()  # should succeed without a long wait
