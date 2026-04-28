"""
Abstract base class for all trading strategies.

Every strategy must implement on_tick(), on_bar(), and generate_signal().
Strategies ONLY produce signals — they never interact with brokers or risk
checks directly. This separation is a critical architectural invariant.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from strategy_engine.signals.signal import Signal


@dataclass
class Bar:
    """
    OHLCV bar representation.

    Used by strategies that operate on candlestick data rather than raw ticks.
    """

    symbol: str
    market: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp: datetime
    interval: str = "1min"  # e.g., "1min", "5min", "1h", "1d"


@dataclass
class StrategyState:
    """
    Persistent state for a strategy, enabling restart-safety.

    Strategies should serialize their state to this structure so it can be
    saved to DynamoDB and restored on service restart.
    """

    strategy_name: str
    positions: dict[str, int] = field(default_factory=dict)
    indicators: dict[str, float] = field(default_factory=dict)
    last_signal_time: Optional[datetime] = None
    custom_state: dict[str, Any] = field(default_factory=dict)


class BaseStrategy(ABC):
    """
    Abstract base class for trading strategies.

    Subclasses must implement:
        - on_tick(): Process a single market tick
        - on_bar(): Process an OHLCV bar
        - generate_signal(): Evaluate current state and optionally produce a signal

    Lifecycle:
        1. initialize() — called once on startup, restore state
        2. on_tick() / on_bar() — called for each market event
        3. generate_signal() — called after processing to check for signals
        4. save_state() — called periodically and on shutdown
    """

    def __init__(self, name: str, symbols: list[str], market: str) -> None:
        """
        Initialize the strategy.

        Args:
            name: Unique name for this strategy instance.
            symbols: List of symbols this strategy trades.
            market: Market identifier (e.g., "NSE", "US").
        """
        self.name = name
        self.symbols = symbols
        self.market = market
        self._state = StrategyState(strategy_name=name)
        self._is_initialized = False

    async def initialize(self, saved_state: Optional[StrategyState] = None) -> None:
        """
        Initialize the strategy, optionally restoring from saved state.

        Called once on service startup. Override to load historical data,
        pre-compute indicators, etc.

        Args:
            saved_state: Previously saved state for restart recovery.
        """
        if saved_state is not None:
            self._state = saved_state
        self._is_initialized = True

    @abstractmethod
    async def on_tick(
        self, symbol: str, price: float, volume: int, timestamp: datetime
    ) -> None:
        """
        Process a single market tick.

        Args:
            symbol: Trading symbol.
            price: Last traded price.
            volume: Tick volume.
            timestamp: Exchange timestamp.
        """
        ...

    @abstractmethod
    async def on_bar(self, bar: Bar) -> None:
        """
        Process an OHLCV bar.

        Args:
            bar: The OHLCV bar to process.
        """
        ...

    @abstractmethod
    async def generate_signal(self) -> Optional[Signal]:
        """
        Evaluate current strategy state and optionally generate a trading signal.

        Returns:
            A Signal if the strategy wants to trade, None otherwise.
        """
        ...

    def get_state(self) -> StrategyState:
        """Return the current strategy state for persistence."""
        return self._state

    @property
    def is_initialized(self) -> bool:
        """Whether the strategy has been initialized."""
        return self._is_initialized
