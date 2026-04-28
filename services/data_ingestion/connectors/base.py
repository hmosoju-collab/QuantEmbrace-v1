"""
Abstract base class for market data connectors.

All broker-specific connectors (Zerodha, Alpaca, etc.) must implement this
interface to ensure consistent behavior across the data ingestion pipeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine, Optional


class Market(str, Enum):
    """Supported markets."""
    NSE = "NSE"
    US = "US"


@dataclass
class NormalizedTick:
    """
    Broker-agnostic normalized tick representation.

    All connectors must convert their native tick format into this structure
    before passing to the tick processor.
    """
    symbol: str
    market: Market
    last_price: float
    bid: float
    ask: float
    volume: int
    timestamp: datetime
    broker: str
    raw: dict[str, Any] = field(default_factory=dict)


# Type alias for the tick callback
TickCallback = Callable[[NormalizedTick], Coroutine[Any, Any, None]]


class BaseConnector(ABC):
    """
    Abstract base class for market data WebSocket connectors.

    Subclasses must implement connect(), disconnect(), and subscribe().
    The connector invokes `on_tick` callback for each received tick after
    normalizing to the NormalizedTick format.
    """

    def __init__(self, on_tick: Optional[TickCallback] = None) -> None:
        """
        Initialize the connector.

        Args:
            on_tick: Async callback invoked for each normalized tick.
        """
        self._on_tick = on_tick
        self._connected = False
        self._subscribed_symbols: list[str] = []

    @abstractmethod
    async def connect(self) -> None:
        """
        Establish WebSocket connection to the broker.

        Must handle reconnection logic internally.
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Gracefully disconnect from the broker.

        Must ensure all pending data is flushed before returning.
        """
        ...

    @abstractmethod
    async def subscribe(self, symbols: list[str]) -> None:
        """
        Subscribe to market data for the given symbols.

        Args:
            symbols: List of instrument symbols to subscribe to.
        """
        ...

    @abstractmethod
    async def unsubscribe(self, symbols: list[str]) -> None:
        """
        Unsubscribe from market data for the given symbols.

        Args:
            symbols: List of instrument symbols to unsubscribe from.
        """
        ...

    @abstractmethod
    def _normalize_tick(self, raw_tick: Any) -> NormalizedTick:
        """
        Convert a broker-specific tick into a NormalizedTick.

        Args:
            raw_tick: Raw tick data from the broker WebSocket.

        Returns:
            Normalized tick suitable for the processing pipeline.
        """
        ...

    @property
    def is_connected(self) -> bool:
        """Whether the connector is currently connected."""
        return self._connected

    @property
    def subscribed_symbols(self) -> list[str]:
        """List of currently subscribed symbols."""
        return self._subscribed_symbols.copy()
