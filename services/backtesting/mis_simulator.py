"""MIS (intraday) square-off simulator for the backtesting lab.

Mirrors production `mis_square_off.py`: NSE MIS positions are auto-squared by the
broker ~15:15 IST; the platform proactively closes at **15:05 IST** (deadline
15:10) so it controls the fill instead of the broker.

**MIS is final exposure cleanup, never a profit-booking mechanism.** It only acts
on positions still open at/after the square-off time, and always closes at market.
Backtest-only — no broker APIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

# IST minutes-of-day helpers operate on tz-aware timestamps converted to IST.
IST = "Asia/Kolkata"


def _hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class MISConfig:
    square_off_ist: str = "15:05"   # proactive close begins
    deadline_ist: str = "15:10"     # must be flat by here
    broker_auto_ist: str = "15:15"  # broker last-resort (no code path)

    @property
    def square_off_time(self) -> time:
        return _hhmm(self.square_off_ist)

    @property
    def deadline_time(self) -> time:
        return _hhmm(self.deadline_ist)


class MISSimulator:
    """Decides and applies the 15:05 IST square-off for still-open positions."""

    def __init__(self, config: MISConfig | None = None) -> None:
        self._cfg = config or MISConfig()

    @property
    def config(self) -> MISConfig:
        return self._cfg

    def should_square_off(self, ts) -> bool:
        """True once the bar time (IST) reaches the square-off time."""
        ist = ts.tz_convert(IST) if getattr(ts, "tz", None) is not None else ts
        return ist.time() >= self._cfg.square_off_time

    def past_deadline(self, ts) -> bool:
        ist = ts.tz_convert(IST) if getattr(ts, "tz", None) is not None else ts
        return ist.time() >= self._cfg.deadline_time

    @staticmethod
    def square_off_price(bar) -> float:
        """MIS closes at market — use the bar close (no profit optimisation)."""
        return float(bar.close)
