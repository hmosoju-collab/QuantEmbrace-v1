"""
ORB Strategy — Opening Range Breakout on confirmed 1m candles.

Logic (orb_v2 — Week-1 rebuild, committee report 2026-06-10):
    1. Opening Range (OR) is defined by the candles from market open to
       ``or_end_minutes`` minutes after open (default 15 minutes = 09:15–09:30 IST).
       The OR high/low are the extremes across all candles in that window.
    2. After OR is confirmed, watch for a candle whose close breaks ABOVE
       OR high + ``breakout_buffer_frac``×OR range (BUY) or BELOW
       OR low − buffer (SELL). A 1-tick poke of the boundary is not a breakout.
    3. Minimum OR range: ≥ ``min_or_range_atr_mult`` × ATR(14) of prior bars
       AND ≥ ``min_or_range_pct`` of price (dead opens are skipped entirely).
    4. Volume confirmation: breakout bar volume ≥ ``volume_multiplier`` ×
       10-bar average; REJECTS when average volume is unavailable.
    5. Stop: the WIDER of OR midpoint, ``atr_stop_multiplier`` × ATR(14) on
       internally aggregated 5m bars, and ``min_stop_pct`` of price. Never
       sized from 1m ATR (bid/ask noise; cost can exceed 1R).
    6. Take-profit: ``rr_ratio`` × stop distance (default 2:1).
    7. Universal viability gate (strategies/_viability.py): no signal whose
       target cannot clear 2.5× the 0.20% round-trip cost.
    8. Strategy-wide budget: max ``max_signals_per_day`` signals (default 6).

Phase awareness:
    OR formation: MARKET_OPEN (09:15–09:30 IST)
    Breakout watch: 09:30 – ``breakout_window_end_ist_min`` (default 11:30 IST)
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
from strategy_engine.strategies._viability import check_signal_viability
from strategy_engine.strategies.base_strategy import Bar, BaseStrategy
from strategy_engine.strategies.nifty_regime_gate import NiftyRegimeGate

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
        min_or_range_pct: float = 0.5,
        breakout_buffer_frac: float = 0.15,
        breakout_window_end_ist_min: int = 11 * 60 + 30,
        volume_multiplier: float = 1.5,
        atr_period: int = 14,
        atr_stop_multiplier: float = 1.5,
        min_stop_pct: float = 0.45,
        rr_ratio: float = 2.0,
        min_confidence: float = 0.65,
        max_signals_per_day: int = 6,
        nav: float = 1_000_000.0,
        paper_trade: bool = True,
        enable_nifty_gate: bool = True,
        nifty_symbol: str = "NIFTY 50",
    ) -> None:
        super().__init__(name=name, symbols=symbols or [], market=market)
        self._nifty_gate: NiftyRegimeGate | None = (
            NiftyRegimeGate(nifty_symbol) if enable_nifty_gate else None
        )
        self._or_end_minutes      = or_end_minutes
        self._min_or_range_mult   = min_or_range_atr_mult
        self._min_or_range_pct    = min_or_range_pct
        self._buffer_frac         = breakout_buffer_frac
        self._breakout_end_ist    = breakout_window_end_ist_min
        self._vol_multiplier      = volume_multiplier
        self._atr_period          = atr_period
        self._atr_stop_mult       = atr_stop_multiplier
        self._min_stop_pct        = min_stop_pct
        self._rr_ratio            = rr_ratio
        self._min_confidence      = min_confidence
        self._max_signals_per_day = max_signals_per_day
        self._nav                 = nav
        self._paper_trade         = paper_trade

        # Per-symbol state
        self._or_high:  dict[str, Optional[float]] = {}
        self._or_low:   dict[str, Optional[float]] = {}
        self._or_confirmed: dict[str, bool]  = {}
        self._signal_fired: dict[str, set[str]] = {}  # symbol → {BUY, SELL}
        self._signals_today: int = 0

        # Rolling price history for ATR
        self._highs:   dict[str, deque[float]] = {}
        self._lows:    dict[str, deque[float]] = {}
        self._closes:  dict[str, deque[float]] = {}
        self._volumes: dict[str, deque[int]]   = {}

        # 5-minute bars aggregated from the 1m feed — stop sizing uses
        # ATR(14) on these, never on 1m bars (1m ATR is bid/ask noise).
        self._h5: dict[str, deque[float]] = {}
        self._l5: dict[str, deque[float]] = {}
        self._c5: dict[str, deque[float]] = {}
        self._cur5_bucket: dict[str, Optional[int]] = {}
        self._cur5_h: dict[str, float] = {}
        self._cur5_l: dict[str, float] = {}
        self._cur5_c: dict[str, float] = {}

        self._pending_signal: Optional[Signal] = None

        # Market open time in IST minutes-of-day
        self._market_open_ist_min = 9 * 60 + 15   # 09:15 IST

    # ── BaseStrategy interface ────────────────────────────────────────────────

    async def on_tick(self, symbol: str, price: float, volume: int, timestamp: datetime) -> None:
        """ORB operates on bars only — tick feed is ignored."""

    async def on_bar(self, bar: Bar) -> None:
        """Process a 1m confirmed candle."""
        if self._nifty_gate is not None:
            self._nifty_gate.on_bar(bar)
        symbol = bar.symbol
        # Skip the NIFTY index itself — it is not a tradeable equity instrument
        if self._nifty_gate is not None and symbol == self._nifty_gate.symbol:
            return
        self._ensure_buffers(symbol)

        # Append to rolling history
        self._highs[symbol].append(bar.high)
        self._lows[symbol].append(bar.low)
        self._closes[symbol].append(bar.close)
        self._volumes[symbol].append(bar.volume)

        ist_min = _ist_minutes_of_day(bar.timestamp)
        self._aggregate_5m(symbol, bar, ist_min)
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
                min_range_atr = self._min_or_range_mult * atr if not math.isnan(atr) else 0.0
                # Absolute floor: a range below min_or_range_pct of price is a
                # dead open — any "breakout" of it is noise relative to costs.
                min_range = max(min_range_atr, (self._min_or_range_pct / 100.0) * bar.close)

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

        # Only fire during the morning breakout window (09:30–11:30 IST default).
        # Afternoon "breakouts" of a 15-minute morning range are noise.
        if ist_min < or_end_ist_min or ist_min >= self._breakout_end_ist:
            return

        # Strategy-wide daily signal budget (committee cap: 6/day)
        if self._signals_today >= self._max_signals_per_day:
            return

        or_high = self._or_high.get(symbol)
        or_low  = self._or_low.get(symbol)
        if or_high is None or or_low is None:
            return

        fired = self._signal_fired.get(symbol, set())

        # Volume confirmation — REJECT when average volume is unavailable.
        # (The old `else True` fallback silently waved through every breakout
        # on symbols with no volume history.)
        volumes = list(self._volumes[symbol])
        avg_vol_10 = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else 0.0
        if avg_vol_10 <= 0 or bar.volume < self._vol_multiplier * avg_vol_10:
            return
        vol_ratio = bar.volume / avg_vol_10

        or_range = or_high - or_low
        if or_range <= 0:
            return
        buffer = self._buffer_frac * or_range

        # BUY breakout: close above OR high + buffer (a 1-tick poke is not a breakout)
        if bar.close > or_high + buffer and "BUY" not in fired:
            if self._nifty_gate is None or self._nifty_gate.is_allowed(Direction.BUY):
                signal = self._build_signal(
                    bar=bar, direction=Direction.BUY,
                    or_high=or_high, or_low=or_low, vol_ratio=vol_ratio,
                )
                if signal is not None:
                    self._pending_signal = signal
                    self._signals_today += 1
                    fired.add("BUY")
                    self._signal_fired[symbol] = fired

        # SELL breakdown: close below OR low - buffer
        elif bar.close < or_low - buffer and "SELL" not in fired:
            if self._nifty_gate is None or self._nifty_gate.is_allowed(Direction.SELL):
                signal = self._build_signal(
                    bar=bar, direction=Direction.SELL,
                    or_high=or_high, or_low=or_low, vol_ratio=vol_ratio,
                )
                if signal is not None:
                    self._pending_signal = signal
                    self._signals_today += 1
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
        for symbol in list(self._cur5_bucket.keys()):
            self._h5[symbol].clear()
            self._l5[symbol].clear()
            self._c5[symbol].clear()
            self._cur5_bucket[symbol] = None
        self._signals_today = 0
        logger.info("orb.daily_reset", strategy=self.name)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ensure_buffers(self, symbol: str) -> None:
        buf = self._atr_period * 3
        if symbol not in self._highs:
            self._highs[symbol]   = deque(maxlen=buf)
            self._lows[symbol]    = deque(maxlen=buf)
            self._closes[symbol]  = deque(maxlen=buf)
            self._volumes[symbol] = deque(maxlen=20)
            self._h5[symbol] = deque(maxlen=buf)
            self._l5[symbol] = deque(maxlen=buf)
            self._c5[symbol] = deque(maxlen=buf)
            self._cur5_bucket[symbol] = None
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
        vol_ratio: float,
    ) -> Optional[Signal]:
        """Build a Signal with a wide (cost-viable) stop and R:R take-profit."""
        price     = bar.close
        or_mid    = (or_high + or_low) / 2.0
        or_range  = or_high - or_low

        # Stop: the WIDER of OR midpoint, 1.5×ATR(5m), and the absolute pct
        # floor. Breakouts normally retest the boundary — a stop tighter than
        # the OR structure guarantees the retest kills the trade.
        atr5 = self._atr_5m(bar.symbol)
        stop_mid_dist  = abs(price - or_mid)
        stop_atr_dist  = self._atr_stop_mult * atr5 if atr5 > 0 else 0.0
        stop_floor     = (self._min_stop_pct / 100.0) * price
        stop_distance  = max(stop_mid_dist, stop_atr_dist, stop_floor)
        stop_loss = (price - stop_distance) if direction == Direction.BUY else (price + stop_distance)

        tp_distance = stop_distance * self._rr_ratio
        take_profit = (price + tp_distance) if direction == Direction.BUY else (price - tp_distance)

        # Confidence: entries NEAR the boundary on strong volume are the good
        # ones. (The old formula rewarded chase distance and pinned at 1.0.)
        boundary   = or_high if direction == Direction.BUY else or_low
        breakout_delta  = abs(price - boundary)
        overshoot_frac  = breakout_delta / or_range
        proximity_score = max(0.0, 1.0 - overshoot_frac / 0.5)
        vol_score       = min(1.0, max(0.0, (vol_ratio - self._vol_multiplier) / 3.0))
        confidence      = 0.45 + 0.30 * proximity_score + 0.25 * vol_score

        if confidence < self._min_confidence:
            logger.debug(
                "orb.signal_below_confidence",
                symbol=bar.symbol,
                confidence=round(confidence, 3),
                min_confidence=self._min_confidence,
            )
            return None

        viability = check_signal_viability(price, stop_distance, tp_distance)
        if not viability.viable:
            logger.info(
                "orb.rejected_viability",
                symbol=bar.symbol,
                direction=direction.value,
                price=price,
                reject_reason=viability.reason,
                stop_distance=round(stop_distance, 4),
                tp_distance=round(tp_distance, 4),
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
                "atr_5m":          round(atr5, 4) if atr5 > 0 else None,
                "stop_distance":   round(stop_distance, 4),
                "vol_ratio":       round(vol_ratio, 2),
                "rr_ratio":        self._rr_ratio,
                "paper_trade":     self._paper_trade,
                "strategy_version":"orb_v2",
                **viability.to_metadata(),
                **sizing.to_metadata(),
            },
        )

    # ── 5-minute aggregation (for stop-sizing ATR) ────────────────────────────

    def _aggregate_5m(self, symbol: str, bar: Bar, ist_min: int) -> None:
        """Fold the 1m feed into rolling 5m bars used only for ATR stop sizing."""
        bucket = ist_min // 5
        cur = self._cur5_bucket.get(symbol)
        if cur is None:
            self._cur5_bucket[symbol] = bucket
            self._cur5_h[symbol] = bar.high
            self._cur5_l[symbol] = bar.low
            self._cur5_c[symbol] = bar.close
            return
        if bucket != cur:
            # Close out the completed 5m bar before starting the new bucket
            self._h5[symbol].append(self._cur5_h[symbol])
            self._l5[symbol].append(self._cur5_l[symbol])
            self._c5[symbol].append(self._cur5_c[symbol])
            self._cur5_bucket[symbol] = bucket
            self._cur5_h[symbol] = bar.high
            self._cur5_l[symbol] = bar.low
            self._cur5_c[symbol] = bar.close
        else:
            self._cur5_h[symbol] = max(self._cur5_h[symbol], bar.high)
            self._cur5_l[symbol] = min(self._cur5_l[symbol], bar.low)
            self._cur5_c[symbol] = bar.close

    def _atr_5m(self, symbol: str) -> float:
        """ATR(atr_period) on completed 5m bars; 0.0 while history is short
        (early session) — the pct stop floor then carries the stop width."""
        h5 = list(self._h5.get(symbol, []))
        if len(h5) < self._atr_period + 1:
            return 0.0
        atr = atr_wilder(h5, list(self._l5[symbol]), list(self._c5[symbol]), self._atr_period)
        return 0.0 if math.isnan(atr) or atr <= 0 else atr


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


