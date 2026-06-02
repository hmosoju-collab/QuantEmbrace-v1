"""
CandleBarAdapter — bridges IntradayCandleStream output to BaseStrategy.on_bar().

The IntradayCandleStream (data_ingestion) fires an ``on_candle`` callback with a
``CandleData`` object for every new confirmed 1m candle.  Gate 4 strategies consume
candle data, not raw ticks.  This adapter:

  1. Converts ``CandleData`` → ``Bar`` (the strategy engine's internal candle type).
  2. Registers an arbitrary number of strategies per candle interval.
  3. Dispatches each new bar to all registered strategies asynchronously.
  4. Calls ``strategy.generate_signal()`` after ``on_bar()`` and collects pending
     signals into an internal queue for the service layer to drain.

Interval routing:
  Strategies declare which interval they consume (e.g. "minute", "5minute",
  "15minute") via the ``candle_interval`` class attribute.  The adapter dispatches
  only to strategies whose interval matches the arriving candle.

Thread-safety:
  ``on_candle`` is called from ``IntradayCandleStream`` which runs in an asyncio
  event loop.  All dispatch is async.  No locks required as long as the adapter
  and all strategies share the same event loop.

Usage::

    from data_ingestion.candle_stream import IntradayCandleStream
    from strategy_engine.strategies.candle_adapter import CandleBarAdapter
    from strategy_engine.strategies.orb_strategy import ORBStrategy

    adapter = CandleBarAdapter()
    adapter.register(ORBStrategy())

    stream = IntradayCandleStream(
        ...,
        on_candle=adapter.on_candle,   # sync callback — adapter queues internally
    )
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Callable, Optional

from shared.logging.logger import get_logger
from shared.models.signal import Signal

from strategy_engine.strategies.base_strategy import Bar, BaseStrategy

logger = get_logger(__name__, service_name="strategy_engine")

# Type alias for the signal consumer callback
SignalCallback = Callable[[Signal], None]


class CandleBarAdapter:
    """
    Converts CandleData → Bar and dispatches to registered strategies.

    Args:
        on_signal:  Optional callback fired when a strategy produces a signal.
                    If not provided, signals are queued internally and drained
                    via ``drain_signals()``.
        loop:       Optional event loop.  Defaults to the running loop.
    """

    def __init__(
        self,
        on_signal: Optional[SignalCallback] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._strategies: list[BaseStrategy] = []
        self._on_signal   = on_signal
        self._loop        = loop
        self._signal_queue: list[Signal] = []

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self, strategy: BaseStrategy) -> None:
        """
        Register a strategy to receive candle dispatches.

        The strategy must be initialized (``initialize()`` called) before the
        first candle arrives — or call ``initialize()`` here if needed.

        Args:
            strategy: An initialized ``BaseStrategy`` instance.
        """
        self._strategies.append(strategy)
        logger.info(
            "candle_adapter.registered",
            strategy=strategy.name,
            interval=getattr(strategy, "candle_interval", "minute"),
        )

    def unregister(self, strategy_name: str) -> None:
        """Remove a strategy by name."""
        before = len(self._strategies)
        self._strategies = [s for s in self._strategies if s.name != strategy_name]
        logger.info(
            "candle_adapter.unregistered",
            strategy=strategy_name,
            removed=before - len(self._strategies),
        )

    # ── Candle entry point (called by IntradayCandleStream) ───────────────────

    def on_candle(self, candle_data: object) -> None:
        """
        Sync callback — called by IntradayCandleStream for every new candle.

        Schedules async dispatch on the event loop.  If no loop is running
        (e.g. in tests), runs the coroutine directly.
        """
        try:
            loop = self._loop or asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._dispatch(candle_data), loop=loop)
            else:
                loop.run_until_complete(self._dispatch(candle_data))
        except RuntimeError:
            # No event loop in test context — run synchronously
            asyncio.run(self._dispatch(candle_data))

    # ── Internal async dispatch ───────────────────────────────────────────────

    async def _dispatch(self, candle_data: object) -> None:
        """Convert CandleData → Bar and fan out to matching strategies."""
        bar = self._to_bar(candle_data)
        if bar is None:
            return

        candle_interval = getattr(candle_data, "interval", "minute")

        for strategy in self._strategies:
            strategy_interval = getattr(strategy, "candle_interval", "minute")
            if strategy_interval != candle_interval:
                continue

            try:
                await strategy.on_bar(bar)
                signal = await strategy.generate_signal()
                if signal is not None:
                    self._handle_signal(signal, strategy.name)
            except Exception:
                logger.exception(
                    "candle_adapter.dispatch_error",
                    strategy=strategy.name,
                    symbol=bar.symbol,
                )

    def _to_bar(self, candle_data: object) -> Optional[Bar]:
        """
        Convert a CandleData object to a Bar.

        Handles both the real CandleData from candle_stream.py and plain dicts
        (useful for testing without importing the data_ingestion package).
        """
        try:
            if isinstance(candle_data, dict):
                return Bar(
                    symbol    = candle_data["instrument"],
                    market    = _extract_market(candle_data["instrument"]),
                    open      = float(candle_data["open"]),
                    high      = float(candle_data["high"]),
                    low       = float(candle_data["low"]),
                    close     = float(candle_data["close"]),
                    volume    = int(candle_data["volume"]),
                    timestamp = _parse_dt(candle_data.get("datetime")),
                    interval  = candle_data.get("interval", "minute"),
                )
            # Real CandleData object (duck-typed — no import to avoid circular deps)
            return Bar(
                symbol    = candle_data.instrument,   # type: ignore[attr-defined]
                market    = _extract_market(candle_data.instrument),  # type: ignore
                open      = float(candle_data.open),   # type: ignore[attr-defined]
                high      = float(candle_data.high),   # type: ignore[attr-defined]
                low       = float(candle_data.low),    # type: ignore[attr-defined]
                close     = float(candle_data.close),  # type: ignore[attr-defined]
                volume    = int(candle_data.volume),   # type: ignore[attr-defined]
                timestamp = candle_data.dt,            # type: ignore[attr-defined]
                interval  = candle_data.interval,      # type: ignore[attr-defined]
            )
        except Exception:
            logger.exception("candle_adapter.parse_error")
            return None

    def _handle_signal(self, signal: Signal, strategy_name: str) -> None:
        """Route a generated signal to the callback or internal queue."""
        logger.info(
            "candle_adapter.signal_generated",
            strategy=strategy_name,
            symbol=signal.symbol,
            direction=signal.direction.value,
            confidence=signal.confidence,
            paper_trade=signal.metadata.get("paper_trade", False),
        )
        if self._on_signal is not None:
            try:
                self._on_signal(signal)
            except Exception:
                logger.exception("candle_adapter.signal_callback_error", strategy=strategy_name)
        else:
            self._signal_queue.append(signal)

    # ── Signal drain (for service layer) ─────────────────────────────────────

    def drain_signals(self) -> list[Signal]:
        """
        Return and clear all queued signals.

        Called by the strategy service's main loop when no on_signal callback
        is registered.  Signals are published to signals.pending (Kafka) from here.
        """
        signals = list(self._signal_queue)
        self._signal_queue.clear()
        return signals

    @property
    def strategy_count(self) -> int:
        """Number of registered strategies."""
        return len(self._strategies)


# ── Utilities ─────────────────────────────────────────────────────────────────

def _extract_market(instrument: str) -> str:
    """Extract exchange prefix: 'NSE:RELIANCE' → 'NSE'. Default 'NSE'."""
    if ":" in instrument:
        return instrument.split(":")[0]
    return "NSE"


def _parse_dt(value: object) -> datetime:
    """Parse a datetime from ISO string, datetime, or None."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    from datetime import timezone
    return datetime.now(tz=timezone.utc)
