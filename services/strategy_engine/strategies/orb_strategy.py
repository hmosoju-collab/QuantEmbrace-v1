"""
ORB Strategy — Opening Range Breakout on confirmed 1m candles.

Logic:
    1. Opening Range (OR) is defined by the candles from market open to
       ``or_end_minutes`` minutes after open (default 15 minutes = 09:15–09:30 IST).
       The OR high/low are the extremes across all candles in that window.
    2. After OR is confirmed (or_end_minutes elapsed), watch for a candle whose
       close breaks ABOVE the OR high (BUY signal) or BELOW the OR low (SELL signal).
    3. Requires a minimum OR range to avoid trading noise on flat open days:
       OR range must be ≥ ``min_or_range_atr_mult`` × ATR(14) of prior bars.
    4. Volume confirmation: the breakout bar's volume must exceed
       ``volume_multiplier`` × 10-bar average volume.
    5. ATR-based stop: stop placed at OR midpoint (tight) or ATR stop (wider),
       whichever is tighter in the direction of the trade.
    6. Take-profit: ``rr_ratio`` × stop distance (default 2:1).

Phase awareness:
    OR formation: MARKET_OPEN (09:15–09:30 IST)
    Breakout watch: NORMAL (09:30–14:45 IST)
    Strategy resets daily at POST_CLOSE.

One signal per symbol per direction per day.  After a BUY signal fires, no more
BUY signals for that symbol today.  SELL signals are independent (allows fading
a failed breakout).

Separation of concerns:
    This strategy NEVER checks risk limits, margin, or existing positions.
    It only produces Signal objects.  The risk engine decides whether to execute.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from shared.logging.logger import get_logger
from shared.models.signal import Direction, Signal
from shared.utils.helpers import generate_correlation_id, utc_now

from strategy_engine.strategies._math import atr_wilder
from strategy_engine.strategies._position_sizer import size_position
from strategy_engine.strategies.base_strategy import Bar, BaseStrategy

logger = get_logger(__name__, service_name="strategy_engine")

# IST offset for phase detection inside the strategy
_IST_OFFSET_HOURS = 5.5   # UTC+5:30


class ORBStrategy(BaseStrategy):
    """
    Opening Range Breakout strategy consuming confirmed 1m candles.

    Class attribute ``candle_interval = "minute"`` tells CandleBarAdapter
    to route 1-minute candles to this strategy.

    Parameters:
        or_end_minutes:         Minutes after market open for OR formation. Default 15.
                                  09:15 IST open + 15m = 09:30 IST OR end.
        min_or_range_atr_mult:  OR range must be ≥ this × prior ATR(14). Default 0.5.
        volume_multiplier:      Breakout bar volume must be ≥ this × 10-bar avg. Default 1.5.
        atr_period:             ATR period for stop sizing. Default 14.
        atr_stop_multiplier:    Stop = OR midpoint OR entry ± mult×ATR, whichever is tighter. Default 1.5.
        rr_ratio:               Reward:risk ratio for take-profit. Default 2.0.
        min_confidence:         Minimum confidence gate. Default 0.65.
        capital:                Indicative capital for qty sizing (risk engine may adjust).
        risk_pct_per_trade:     Fraction of capital to risk per trade.
        paper_trade:            If True, tag signal metadata with paper_trade=True.
    """

    candle_interval: str = "minute"

    def __init__(
        self,
        name: str = "orb_15m",
        symbols: list[str] | None = None,
        market: str = "NSE",
        or_end_minutes: int = 15,
        min_or_range_atr_mult: float = 0.5,
        volume_multiplier: float = 1.5,
        atr_period: int = 14,
        atr_stop_multiplier: float = 1.5,
        rr_ratio: float = 2.0,
        min_confidence: float = 0.65,
        nav: float = 1_000_000.0,
        paper_trade: bool = True,
    ) -> None:
        super().__init__(name=name, symbols=symbols or [], market=market)
        self._or_end_minutes      = or_end_minutes
        self._min_or_range_mult   = min_or_range_atr_mult
        self._vol_multiplier      = volume_multiplier
        self._atr_period          = atr_period
        self._atr_stop_mult       = atr_stop_multiplier
        self._rr_ratio            = rr_ratio
        self._min_confidence      = min_confidence
        self._nav                 = nav
        self._paper_trade         = paper_trade

        # Per-symbol state
        self._or_high:  dict[str, Optional[float]] = {}
        self._or_low:   dict[str, Optional[float]] = {}
        self._or_confirmed: dict[str, bool]  = {}
        self._signal_fired: dict[str, set[str]] = {}  # symbol → {BUY, SELL}

        # Rolling price history for ATR
        self._highs:   dict[str, deque[float]] = {}
        self._lows:    dict[str, deque[float]] = {}
        self._closes:  dict[str, deque[float]] = {}
        self._volumes: dict[str, deque[int]]   = {}

        self._pending_signal: Optional[Signal] = None

        # Market open time in IST minutes-of-day
        self._market_open_ist_min = 9 * 60 + 15   # 09:15 IST

    # ── BaseStrategy interface ────────────────────────────────────────────────

    async def on_tick(self, symbol: str, price: float, volume: int, timestamp: datetime) -> None:
        """ORB operates on bars only — tick feed is ignored."""

    async def on_bar(self, bar: Bar) -> None:
        """Process a 1m confirmed candle."""
        symbol = bar.symbol
        self._ensure_buffers(symbol)

        # Append to rolling history
        self._highs[symbol].append(bar.high)
        self._lows[symbol].append(bar.low)
        self._closes[symbol].append(bar.close)
        self._volumes[symbol].append(bar.volume)

        ist_min = _ist_minutes_of_day(bar.timestamp)
        or_end_ist_min = self._market_open_ist_min + self._or_end_minutes

        # Phase 1: Build the Opening Range
        if self._market_open_ist_min <= ist_min < or_end_ist_min:
            if self._or_high.get(symbol) is None:
                self._or_high[symbol] = bar.high
                self._or_low[symbol]  = bar.low
            else:
                self._or_high[symbol] = max(self._or_high[symbol], bar.high)
                self._or_low[symbol]  = min(self._or_low[symbol],  bar.low)
            self._or_confirmed[symbol] = False
            return

        # Phase 2: Confirm OR on first bar after or_end
        if ist_min == or_end_ist_min and not self._or_confirmed.get(symbol, False):
            or_high = self._or_high.get(symbol)
            or_low  = self._or_low.get(symbol)
            if or_high is not None and or_low is not None:
                # Check OR range is meaningful
                closes = list(self._closes[symbol])
                highs  = list(self._highs[symbol])
                lows   = list(self._lows[symbol])
                atr    = atr_wilder(highs, lows, closes, self._atr_period)
                or_range = or_high - or_low
                min_range = self._min_or_range_mult * atr if not math.isnan(atr) else 0.0

                if or_range >= min_range:
                    self._or_confirmed[symbol] = True
                    logger.info(
                        "orb.range_confirmed",
                        symbol=symbol,
                        or_high=or_high,
                        or_low=or_low,
                        or_range=round(or_range, 2),
                        atr=round(atr, 2) if not math.isnan(atr) else None,
                    )
                else:
                    logger.info(
                        "orb.range_too_narrow",
                        symbol=symbol,
                        or_range=round(or_range, 2),
                        min_range=round(min_range, 2),
                    )
            return

        # Phase 3: Breakout watch (after OR confirmed, during NORMAL phase)
        if not self._or_confirmed.get(symbol, False):
            return

        # Only fire during NORMAL phase (09:30–14:45 IST)
        if ist_min < or_end_ist_min or ist_min >= 14 * 60 + 45:
            return

        or_high = self._or_high.get(symbol)
        or_low  = self._or_low.get(symbol)
        if or_high is None or or_low is None:
            return

        fired = self._signal_fired.get(symbol, set())

        # Volume confirmation
        volumes = list(self._volumes[symbol])
        avg_vol_10 = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else 0.0
        vol_ok = bar.volume >= self._vol_multiplier * avg_vol_10 if avg_vol_10 > 0 else True

        if not vol_ok:
            return

        closes  = list(self._closes[symbol])
        highs_l = list(self._highs[symbol])
        lows_l  = list(self._lows[symbol])
        atr     = atr_wilder(highs_l, lows_l, closes, self._atr_period)
        if math.isnan(atr) or atr <= 0:
            return

        # BUY breakout: close above OR high
        if bar.close > or_high and "BUY" not in fired:
            signal = self._build_signal(
                bar=bar, direction=Direction.BUY,
                or_high=or_high, or_low=or_low, atr=atr,
            )
            if signal is not None:
                self._pending_signal = signal
                fired.add("BUY")
                self._signal_fired[symbol] = fired

        # SELL breakdown: close below OR low
        elif bar.close < or_low and "SELL" not in fired:
            signal = self._build_signal(
                bar=bar, direction=Direction.SELL,
                or_high=or_high, or_low=or_low, atr=atr,
            )
            if signal is not None:
                self._pending_signal = signal
                fired.add("SELL")
                self._signal_fired[symbol] = fired

    async def generate_signal(self) -> Optional[Signal]:
        signal = self._pending_signal
        self._pending_signal = None
        return signal

    # ── Day reset ─────────────────────────────────────────────────────────────

    def reset_daily(self) -> None:
        """Reset OR state for all symbols.  Called at POST_CLOSE."""
        for symbol in list(self._or_high.keys()):
            self._or_high[symbol]      = None
            self._or_low[symbol]       = None
            self._or_confirmed[symbol] = False
            self._signal_fired[symbol] = set()
        logger.info("orb.daily_reset", strategy=self.name)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ensure_buffers(self, symbol: str) -> None:
        buf = self._atr_period * 3
        if symbol not in self._highs:
            self._highs[symbol]   = deque(maxlen=buf)
            self._lows[symbol]    = deque(maxlen=buf)
            self._closes[symbol]  = deque(maxlen=buf)
            self._volumes[symbol] = deque(maxlen=20)
            self._or_high[symbol] = None
            self._or_low[symbol]  = None
            self._or_confirmed[symbol] = False
            self._signal_fired[symbol] = set()

    def _build_signal(
        self,
        bar: Bar,
        direction: Direction,
        or_high: float,
        or_low: float,
        atr: float,
    ) -> Optional[Signal]:
        """Build a Signal with ATR-derived stop and R:R take-profit."""
        price     = bar.close
        or_mid    = (or_high + or_low) / 2.0
        or_range  = or_high - or_low

        if direction == Direction.BUY:
            # Stop: OR midpoint or ATR stop, whichever is closer to entry
            stop_atr  = price - self._atr_stop_mult * atr
            stop_mid  = or_mid
            stop_loss = max(stop_atr, stop_mid)   # tighter (higher) stop
        else:
            stop_atr  = price + self._atr_stop_mult * atr
            stop_mid  = or_mid
            stop_loss = min(stop_atr, stop_mid)   # tighter (lower) stop

        stop_distance = abs(price - stop_loss)
        if stop_distance <= 0:
            return None

        tp_distance = stop_distance * self._rr_ratio
        take_profit = (price + tp_distance) if direction == Direction.BUY else (price - tp_distance)

        # Confidence: how far above/below the OR boundary the close is, in ATR units
        boundary   = or_high if direction == Direction.BUY else or_low
        breakout_delta = abs(price - boundary)
        confidence = min(1.0, 0.5 + breakout_delta / (atr * 2.0))

        if confidence < self._min_confidence:
            logger.debug(
                "orb.signal_below_confidence",
                symbol=bar.symbol,
                confidence=round(confidence, 3),
                min_confidence=self._min_confidence,
            )
            return None

        sizing = size_position(price, stop_distance, self._nav, confidence)
        if sizing.qty == 0 or sizing.rejected_if_exceeds_cap:
            return None

        return Signal(
            symbol          = bar.symbol,
            market          = self.market,
            direction       = direction,
            quantity        = sizing.qty,
            confidence      = round(confidence, 4),
            strategy_name   = self.name,
            price_at_signal = price,
            stop_loss       = round(stop_loss, 4),
            take_profit     = round(take_profit, 4),
            metadata={
                "or_high":         round(or_high, 4),
                "or_low":          round(or_low, 4),
                "or_range":        round(or_range, 4),
                "or_mid":          round(or_mid, 4),
                "atr":             round(atr, 4),
                "stop_distance":   round(stop_distance, 4),
                "rr_ratio":        self._rr_ratio,
                "paper_trade":     self._paper_trade,
                "strategy_version":"orb_v1",
                **sizing.to_metadata(),
            },
        )


# ── Utilities ─────────────────────────────────────────────────────────────────

def _ist_minutes_of_day(dt: datetime) -> int:
    """Return minutes-of-day in IST from a UTC (or tz-aware) datetime."""
    if dt.tzinfo is not None:
        from datetime import timedelta
        ist = dt.astimezone(timezone.utc) + timedelta(hours=5, minutes=30)
    else:
        from datetime import timedelta
        ist = dt + timedelta(hours=5, minutes=30)
    return ist.hour * 60 + ist.minute


