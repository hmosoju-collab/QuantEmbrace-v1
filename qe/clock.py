"""Clock abstraction — the axis that makes backtest, paper, and live one engine.

The engine reads the current instant and trading date only through a Clock. In
backtest the clock is event-time (SimClock, driven by the panel); in paper/live
it is wall-time (WallClock, IST). Nothing else in the engine knows which mode it
is in — that is the whole point of RA-1 §2.3.
"""

from datetime import UTC, date, datetime, time
from typing import Protocol
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")

_MARKET_TZ = {"NSE": IST, "US": NY}
_MARKET_CLOSE_TIME = {"NSE": time(15, 30), "US": time(16, 0)}


def market_tz(market: str) -> ZoneInfo:
    """Trading-date timezone for a market string (ADR-041 P3). Fail closed on
    an unregistered market rather than silently defaulting to IST."""
    try:
        return _MARKET_TZ[market]
    except KeyError:
        raise ValueError(
            f"no timezone registered for market={market!r} (known: {sorted(_MARKET_TZ)})"
        ) from None


def market_close_time(market: str) -> time:
    """Local trading-close wall-clock time for a market string (ADR-041 P5,
    used by `qe.engine.paper.sim_clock_at`). Fail closed, same as market_tz."""
    try:
        return _MARKET_CLOSE_TIME[market]
    except KeyError:
        raise ValueError(
            f"no close time registered for market={market!r} (known: {sorted(_MARKET_CLOSE_TIME)})"
        ) from None


class Clock(Protocol):
    kind: str

    def now(self) -> datetime: ...
    def today(self) -> date: ...


class SimClock:
    """Event-time clock pinned to a historical instant (the current bar)."""

    kind = "sim"

    def __init__(self, as_of: datetime):
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=IST)
        self._t = as_of

    def now(self) -> datetime:
        return self._t

    def today(self) -> date:
        # Read the date in the instant's OWN tz, not a forced IST conversion
        # (ADR-041 P5) — an NY-tz instant near NYSE close converts to a
        # DIFFERENT calendar date in IST (16:00 ET = 01:30 IST next day),
        # which silently mislabeled a US paper session's trading date.
        return self._t.date()

    def advance_to(self, instant: datetime) -> None:
        self._t = instant if instant.tzinfo else instant.replace(tzinfo=IST)


class WallClock:
    """Real wall-clock, reported in a given market's tz (default IST/NSE)."""

    kind = "wall"

    def __init__(self, tz: ZoneInfo = IST):
        self.tz = tz

    def now(self) -> datetime:
        return datetime.now(UTC).astimezone(self.tz)

    def today(self) -> date:
        return self.now().date()
