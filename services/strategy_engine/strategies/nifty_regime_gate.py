"""NiftyRegimeGate — intraday NIFTY 50 VWAP directional filter (ADR-031 follow-up).

Rule:
    BUY  only when last NIFTY 50 close >= NIFTY intraday VWAP.
    SELL only when last NIFTY 50 close <= NIFTY intraday VWAP.
    Fails-open (allows all directions) when no NIFTY data has been received,
    so paper sessions without NIFTY subscribed are not affected.

Rationale: avoids long entries in a falling-index environment and short
entries in a rising-index environment.  Both ORB and trend_15m benefit from
this alignment.  VWAP reversion is deliberately excluded (it is a counter-trend
strategy and regime alignment would suppress correct trades).

Usage in a strategy's on_bar():
    if self._nifty_gate is not None:
        self._nifty_gate.on_bar(bar)          # always feed ALL bars
    if self._nifty_gate is not None and bar.symbol == self._nifty_gate.symbol:
        return                                 # skip index itself as tradeable
    ...
    if self._nifty_gate is not None and not self._nifty_gate.is_allowed(direction):
        return                                 # regime blocks this direction
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from shared.logging.logger import get_logger
from shared.models.signal import Direction

if TYPE_CHECKING:
    from strategy_engine.strategies.base_strategy import Bar

logger = get_logger(__name__, service_name="strategy_engine")

_IST_OFFSET = timedelta(hours=5, minutes=30)


def _ist_date(dt: datetime) -> str:
    if dt.tzinfo is not None:
        ist = dt.astimezone(timezone.utc) + _IST_OFFSET
    else:
        ist = dt + _IST_OFFSET
    return ist.strftime("%Y-%m-%d")


class NiftyRegimeGate:
    """
    Intraday NIFTY 50 VWAP regime filter for directional strategies.

    Call on_bar() with EVERY bar the strategy receives (gate self-routes by symbol).
    Check is_allowed(direction) before emitting a signal.

    The gate auto-resets at the IST day boundary on the first NIFTY bar of each
    new day, so no external daily-reset call is required.
    """

    def __init__(self, nifty_symbol: str = "NIFTY 50") -> None:
        self._symbol       = nifty_symbol
        self._tpv: float   = 0.0   # cumulative typical-price × volume
        self._vol: float   = 0.0   # cumulative volume
        self._last_close: float | None = None
        self._current_date: str | None = None

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def vwap(self) -> float | None:
        if self._vol <= 0:
            return None
        return self._tpv / self._vol

    def on_bar(self, bar: "Bar") -> None:
        """Feed a bar to the gate.  Non-NIFTY bars are silently ignored."""
        if bar.symbol != self._symbol:
            return
        bar_date = _ist_date(bar.timestamp)
        if self._current_date != bar_date:
            self._reset()
            self._current_date = bar_date
        vol = max(float(bar.volume), 1.0)  # guard against 0-volume index bars
        tp  = (bar.high + bar.low + bar.close) / 3.0
        self._tpv       += tp * vol
        self._vol       += vol
        self._last_close = bar.close

    def is_allowed(self, direction: Direction) -> bool:
        """True if NIFTY regime permits this direction.  Fails-open when no data."""
        vwap  = self.vwap
        close = self._last_close
        if vwap is None or close is None:
            return True   # no NIFTY data yet → fail-open
        if direction == Direction.BUY:
            allowed = close >= vwap
        else:
            allowed = close <= vwap
        if not allowed:
            logger.debug(
                "nifty_regime_gate.blocked",
                direction=direction.value,
                nifty_close=round(close, 2),
                nifty_vwap=round(vwap, 2),
            )
        return allowed

    def _reset(self) -> None:
        self._tpv        = 0.0
        self._vol        = 0.0
        self._last_close = None
