"""
Base Broker — abstract interface for all broker integrations.

Defines the ``BrokerClient`` protocol that all broker implementations
must satisfy. Strategy and risk code must always use this abstraction.
Direct broker client usage outside the execution layer is prohibited.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Coroutine

from execution_engine.orders.order import (
    OrderRequest,
    OrderResponse,
    OrderStatus,
    OrderStatusUpdate,
)


# Type alias for the quote callback used by streaming subscriptions
QuoteCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class BrokerClient(ABC):
    """
    Abstract base class for broker integrations.

    All broker-specific logic is encapsulated behind this interface so that
    execution, risk, and strategy code remain broker-agnostic.

    Implementations must handle:
        - Authentication and token management.
        - Client-side rate limiting.
        - Error translation to standard exceptions.
    """

    @property
    @abstractmethod
    def broker_name(self) -> str:
        """Human-readable broker name (e.g., 'Zerodha', 'Alpaca')."""
        ...

    @property
    @abstractmethod
    def supported_markets(self) -> list[str]:
        """List of market identifiers this broker handles (e.g., ['NSE', 'BSE'])."""
        ...

    @abstractmethod
    async def connect(self) -> None:
        """
        Establish connection and authenticate with the broker.

        Should be called once during service startup. Implementations
        must handle token refresh if applicable.
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully disconnect from the broker."""
        ...

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """
        Submit an order to the broker.

        Args:
            order: The order to place.

        Returns:
            OrderResponse with broker-assigned ID and initial status.

        Raises:
            BrokerAPIError: If the broker rejects the request.
        """
        ...

    async def find_order_by_client_order_id(
        self,
        client_order_id: str,
        order: OrderRequest | None = None,
    ) -> OrderResponse | None:
        """
        Return an existing broker order for a client idempotency key, if supported.

        Execution uses this before retrying broker placement. If a previous
        request reached the broker but the acknowledgement was lost, this lookup
        prevents a second live order from being submitted.
        """
        return None

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> OrderStatusUpdate:
        """
        Cancel an open order.

        Args:
            broker_order_id: The broker-assigned order ID.

        Returns:
            OrderStatusUpdate reflecting the cancellation.
        """
        ...

    @abstractmethod
    async def get_order_status(self, broker_order_id: str) -> OrderStatusUpdate:
        """
        Query the current status of an order.

        Args:
            broker_order_id: The broker-assigned order ID.

        Returns:
            OrderStatusUpdate with the latest status and fill info.
        """
        ...

    @abstractmethod
    async def get_positions(self) -> list[dict[str, Any]]:
        """
        Retrieve all current positions from the broker.

        Returns:
            List of position dictionaries with symbol, quantity,
            average price, and market value.
        """
        ...

    @abstractmethod
    async def subscribe_quotes(
        self, symbols: list[str], callback: QuoteCallback
    ) -> None:
        """
        Subscribe to real-time quote updates for the given symbols.

        Args:
            symbols: List of symbols to subscribe to.
            callback: Async callback invoked on each quote update.
        """
        ...


class BrokerAPIError(Exception):
    """
    Raised when a broker API call fails with a transient/retryable error.

    Use this for 5xx server errors, network timeouts, and rate-limit (429)
    responses where retrying after a backoff is appropriate.

    Attributes:
        broker: Name of the broker.
        status_code: HTTP status code (if applicable).
        message: Error message from the broker.
    """

    def __init__(
        self,
        broker: str,
        message: str,
        status_code: int | None = None,
    ) -> None:
        self.broker = broker
        self.status_code = status_code
        super().__init__(f"[{broker}] {message} (status={status_code})")


class NonRetryableBrokerError(BrokerAPIError):
    """
    Raised for permanent broker errors that must NOT be retried.

    Retrying these errors wastes broker API quota and can cause unintended
    side effects (e.g. multiple order placements if the first "failed" due
    to a misread 400 response that actually succeeded server-side).

    Raise this for:
        - 400 Bad Request: invalid symbol, bad quantity, malformed order
        - 403 Forbidden: account suspended, permission denied
        - 422 Unprocessable: insufficient margin, risk limit exceeded at broker
        - Any error whose message explicitly identifies a permanent failure
          (e.g. "Symbol not found", "Account restricted")

    The RetryHandler catches this exception class and re-raises immediately
    without sleeping or incrementing the retry counter. The circuit breaker
    is NOT tripped by non-retryable errors — they indicate a logic error in
    the order parameters, not an infrastructure failure.

    Examples::

        # Bad symbol — do not retry
        raise NonRetryableBrokerError("Alpaca", "symbol XYZW not found", status_code=400)

        # Insufficient funds — do not retry (funds won't appear between retries)
        raise NonRetryableBrokerError("Zerodha", "insufficient margin", status_code=422)

        # Rate limit — DO retry (use BrokerAPIError, not NonRetryableBrokerError)
        raise BrokerAPIError("Alpaca", "rate limit exceeded", status_code=429)
    """
