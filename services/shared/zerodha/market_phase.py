"""
IST Market Phase Governor for NSE trading sessions.

Tracks the current NSE market phase based on Indian Standard Time (IST,
UTC+5:30) and broadcasts phase transitions to registered listeners.  All
Zerodha polling services (``BulkOrderPoller``, ``PositionMonitor``,
``LiveQuotePoller``, ``IntradayCandleStream``) and the rate limiter use the
current phase to adjust their polling intervals and budget allocations.

Market phases (NSE, IST):

    PRE_OPEN      08:00 – 09:00  Pre-market order collection
    PRE_AUCTION   09:00 – 09:15  Call auction and price discovery
    MARKET_OPEN   09:15 – 09:30  Opening burst — high volume, tight fills
    NORMAL        09:30 – 14:45  Main trading session
    PRE_CLOSE     14:45 – 15:20  Pre-close + MIS auto-square-off window
    CLOSING       15:20 – 15:30  Closing call auction
    POST_CLOSE    15:30 – 08:00  After-hours (next day PRE_OPEN)

Budget table (req/sec per phase, total = 10):

    Operation          PRE_OPEN  MKT_OPEN  NORMAL  PRE_CLOSE  POST
    place_order            0         4        2        3        0
    bulk_fill_poll         0         3        2        3        1
    get_positions          0         1        1        2        1
    get_margins            0         1        1        1        0
    get_quotes (batch)     0        .5        2       .5        0
    candle_stream*         3         3        3        3        0
    reconcile/audit        2         0        0        0        2
    RESERVE                5         1        1        1        3

The ``RESERVE`` row guarantees CRITICAL-priority API calls (emergency cancel,
kill switch) always have tokens available regardless of other activity.
``candle_stream`` uses Zerodha's separate historical-data API budget and is
listed for operator visibility, not deducted from the 10 req/sec order API cap.

Usage::

    governor = MarketPhaseGovernor()
    governor.add_listener(rate_limiter.set_market_phase)
    governor.add_listener(bulk_poller.on_phase_change)
    asyncio.create_task(governor.run())

    # Anywhere you need the current phase:
    phase = governor.current_phase()
    if phase == MarketPhase.PRE_CLOSE:
        # Reduce position sizes, monitor for auto-square-off
        ...
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time
from enum import Enum
from typing import Callable
from zoneinfo import ZoneInfo

from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="execution_engine")

_IST = ZoneInfo("Asia/Kolkata")

# ── Market phase enum ─────────────────────────────────────────────────────────


class MarketPhase(str, Enum):
    """NSE IST market phases with associated polling behaviour."""

    PRE_OPEN    = "PRE_OPEN"     # 08:00 – 09:00 IST
    PRE_AUCTION = "PRE_AUCTION"  # 09:00 – 09:15 IST
    MARKET_OPEN = "MARKET_OPEN"  # 09:15 – 09:30 IST  ← opening burst
    NORMAL      = "NORMAL"       # 09:30 – 14:45 IST  ← main session
    PRE_CLOSE   = "PRE_CLOSE"    # 14:45 – 15:20 IST  ← MIS auto-square-off
    CLOSING     = "CLOSING"      # 15:20 – 15:30 IST
    POST_CLOSE  = "POST_CLOSE"   # 15:30+ IST  (until next PRE_OPEN)


# ── Budget table ──────────────────────────────────────────────────────────────

#: Advisory budget allocation (req/sec) per phase per operation category.
#: The rate limiter does not enforce these per-category — they are used by
#: polling services to self-regulate and by ``rate_monitor.py`` for dashboards.
PHASE_BUDGET: dict[MarketPhase, dict[str, float]] = {
    MarketPhase.PRE_OPEN: {
        "place_order":      0.0,
        "bulk_fill_poll":   0.0,
        "get_positions":    0.0,
        "get_margins":      0.0,
        "get_quotes":       0.0,
        "candle_stream":    3.0,
        "reconcile_audit":  2.0,
        "reserve":          5.0,
    },
    MarketPhase.PRE_AUCTION: {
        "place_order":      0.0,
        "bulk_fill_poll":   0.5,
        "get_positions":    0.5,
        "get_margins":      0.5,
        "get_quotes":       0.5,
        "candle_stream":    2.0,
        "reconcile_audit":  1.0,
        "reserve":          5.0,
    },
    MarketPhase.MARKET_OPEN: {
        "place_order":      4.0,
        "bulk_fill_poll":   3.0,
        "get_positions":    1.0,
        "get_margins":      0.5,
        "get_quotes":       0.5,   # spread/circuit checks for ORB entries
        "candle_stream":    3.0,   # separate historical-data API budget
        "reconcile_audit":  0.0,
        "reserve":          1.0,
    },
    MarketPhase.NORMAL: {
        "place_order":      2.0,
        "bulk_fill_poll":   2.0,
        "get_positions":    1.0,
        "get_margins":      1.0,
        "get_quotes":       2.0,
        "candle_stream":    1.0,
        "reconcile_audit":  0.0,
        "reserve":          1.0,
    },
    MarketPhase.PRE_CLOSE: {
        "place_order":      3.0,
        "bulk_fill_poll":   3.0,
        "get_positions":    2.0,
        "get_margins":      0.5,
        "get_quotes":       0.5,   # spread/circuit checks for pre-close strategy
        "candle_stream":    3.0,   # separate historical-data API budget
        "reconcile_audit":  0.0,
        "reserve":          1.0,
    },
    MarketPhase.CLOSING: {
        "place_order":      1.0,
        "bulk_fill_poll":   2.0,
        "get_positions":    2.0,
        "get_margins":      1.0,
        "get_quotes":       0.0,
        "candle_stream":    1.0,
        "reconcile_audit":  0.0,
        "reserve":          3.0,
    },
    MarketPhase.POST_CLOSE: {
        "place_order":      0.0,
        "bulk_fill_poll":   1.0,
        "get_positions":    1.0,
        "get_margins":      0.0,
        "get_quotes":       0.0,
        "candle_stream":    0.0,
        "reconcile_audit":  2.0,
        "reserve":          3.0,
    },
}

# ── Phase schedule ────────────────────────────────────────────────────────────

#: IST time → phase that starts at that time.
#: The governor iterates this table in sorted order to find the current phase.
_PHASE_SCHEDULE: list[tuple[time, MarketPhase]] = sorted(
    [
        (time(8, 0),  MarketPhase.PRE_OPEN),
        (time(9, 0),  MarketPhase.PRE_AUCTION),
        (time(9, 15), MarketPhase.MARKET_OPEN),
        (time(9, 30), MarketPhase.NORMAL),
        (time(14, 45), MarketPhase.PRE_CLOSE),
        (time(15, 20), MarketPhase.CLOSING),
        (time(15, 30), MarketPhase.POST_CLOSE),
    ],
    key=lambda t: t[0],
)

# How frequently to check for a phase transition (seconds).
_CHECK_INTERVAL_SECONDS: float = 10.0

# ── Governor ─────────────────────────────────────────────────────────────────


class MarketPhaseGovernor:
    """
    IST-clock-aware market phase tracker for NSE trading sessions.

    Runs as a background ``asyncio`` task.  Checks the IST clock every 10
    seconds and broadcasts a ``MarketPhase`` change to all registered
    listeners whenever the phase boundary is crossed.

    Listeners are simple callables: ``(phase_name: str) -> None``.
    They are called synchronously (not awaited) so they must be fast.
    If a listener needs to do async work it should set an internal flag
    and handle it in its own loop.

    Polling services register via ``add_listener()``:

        governor.add_listener(rate_limiter.set_market_phase)
        governor.add_listener(bulk_poller.on_phase_change)
    """

    def __init__(self) -> None:
        self._phase   = self._compute_phase()
        self._listeners: list[Callable[[str], None]] = []
        self._running = False

    # ── Public API ────────────────────────────────────────────────────────────

    def current_phase(self) -> MarketPhase:
        """Return the current market phase."""
        return self._phase

    def current_budget(self) -> dict[str, float]:
        """Return the advisory budget allocation for the current phase."""
        return PHASE_BUDGET[self._phase]

    def add_listener(self, listener: Callable[[str], None]) -> None:
        """
        Register a listener to be called on phase transitions.

        Args:
            listener: Callable that accepts the phase name string.
        """
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[str], None]) -> None:
        """Remove a previously registered listener."""
        self._listeners.remove(listener)

    def is_market_open(self) -> bool:
        """True during MARKET_OPEN, NORMAL, PRE_CLOSE, and CLOSING phases."""
        return self._phase in (
            MarketPhase.MARKET_OPEN,
            MarketPhase.NORMAL,
            MarketPhase.PRE_CLOSE,
            MarketPhase.CLOSING,
        )

    def is_mis_window(self) -> bool:
        """True during PRE_CLOSE — MIS auto-square-off is imminent."""
        return self._phase == MarketPhase.PRE_CLOSE

    def is_candle_prefetch_time(self) -> bool:
        """True during PRE_OPEN — run candle_prefetch.py now."""
        return self._phase == MarketPhase.PRE_OPEN

    async def run(self) -> None:
        """
        Background phase monitoring loop.

        Checks the IST clock every 10 seconds and triggers listeners when
        the phase changes.  Run via ``asyncio.create_task(governor.run())``.
        """
        self._running = True
        logger.info(
            "market_phase_governor.started",
            initial_phase=self._phase.value,
        )

        # Notify listeners of the current phase at startup
        self._broadcast(self._phase)

        try:
            while self._running:
                await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
                new_phase = self._compute_phase()
                if new_phase != self._phase:
                    old = self._phase
                    self._phase = new_phase
                    logger.info(
                        "market_phase_governor.transition",
                        from_phase=old.value,
                        to_phase=new_phase.value,
                        ist_time=self._now_ist().strftime("%H:%M:%S"),
                    )
                    self._broadcast(new_phase)
        except asyncio.CancelledError:
            logger.info(
                "market_phase_governor.cancelled",
                phase=self._phase.value,
                ist_time=self._now_ist().strftime("%H:%M:%S"),
            )
            raise
        except Exception:
            logger.exception(
                "market_phase_governor.fatal_error",
                phase=self._phase.value,
                ist_time=self._now_ist().strftime("%H:%M:%S"),
            )
            raise
        finally:
            self._running = False
            logger.info("market_phase_governor.exited", phase=self._phase.value)

    async def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False
        logger.info("market_phase_governor.stopped")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _compute_phase(self) -> MarketPhase:
        """
        Determine current market phase from the IST wall clock.

        Walks the phase schedule in reverse chronological order and returns
        the phase whose start time is the most recent past boundary.

        Returns POST_CLOSE if current time is before 08:00 IST
        (i.e. pre-market of the next trading day).
        """
        now_ist = self._now_ist().time()

        # Walk schedule newest-first; return the first boundary we've passed
        for boundary_time, phase in reversed(_PHASE_SCHEDULE):
            if now_ist >= boundary_time:
                return phase

        # Before 08:00 IST — previous trading day's POST_CLOSE
        return MarketPhase.POST_CLOSE

    @staticmethod
    def _now_ist() -> datetime:
        """Return current datetime in IST."""
        return datetime.now(tz=_IST)

    def _broadcast(self, phase: MarketPhase) -> None:
        """Call all registered listeners with the new phase name."""
        for listener in self._listeners:
            try:
                listener(phase.value)
            except Exception:
                logger.exception(
                    "market_phase_governor.listener_error",
                    phase=phase.value,
                )
