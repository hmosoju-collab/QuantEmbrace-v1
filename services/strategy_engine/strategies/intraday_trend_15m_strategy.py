"""
Intraday Trend 15m Strategy — EMA crossover with ADX trend confirmation.

Logic:
    1. Operates on 15-minute confirmed candles (``candle_interval = "15minute"``).
    2. EMA(9) / EMA(21) crossover on 15m bars provides entry direction.
    3. ADX(14) filter: only trade when ADX > ``adx_threshold`` (default 25).
       This suppresses signals in choppy/ranging markets where EMA crossovers
       produce false positives at a high rate.
    4. Trend filter: the price must be on the correct side of EMA(50) to confirm
       the broader intraday trend.  BUY only when close > EMA(50); SELL when < EMA(50).
    5. Stop-loss: ``atr_stop_multiplier`` × ATR(14) below entry (BUY) or above (SELL).
    6. Take-profit: ``rr_ratio`` × stop distance (default 2.5:1).
    7. One signal per symbol per day (trend is a slower concept; no re-entry).

Phase gates:
    Fires during NORMAL phase only (09:30–14:45 IST).
    Requires at least ``min_bars`` 15m candles since market open (default 4 = 1 hour).
    No signals in the last 30 minutes before PRE_CLOSE.

Separation of concerns:
    Never checks positions, broker state, or risk limits.  Signal only.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from shared.logging.logger import get_logger
from shared.models.signal import Direction, Signal

from strategy_engine.strategies._math import adx, atr_wilder, ema_series
from strategy_engine.strategies._position_sizer import size_position
from strategy_engine.strategies.base_strategy import Bar, BaseStrategy

logger = get_logger(__name__, service_name="strategy_engine")

_IST_NORMAL_START = 9 * 60 + 30    # 09:30 IST
_IST_NORMAL_END   = 14 * 60 + 15   # 14:15 IST (30m buffer before pre-close)


def _ist_min(dt: datetime) -> int:
    if dt.tzinfo is not None:
        ist = dt.astimezone(timezone.utc) + timedelta(hours=5, minutes=30)
    else:
        ist = dt + timedelta(hours=5, minutes=30)
    return ist.hour * 60 + ist.minute


class IntradayTrend15mStrategy(BaseStrategy):
    """
    15-minute trend-following strategy with ADX confirmation.

    Class attribute ``candle_interval = "15minute"`` routes 15m candles here.
    The IntradayCandleStream must be running a separate 15minute interval stream
    (or the service layer must aggregate 1m candles into 15m bars before dispatch).

    Parameters:
        fast_ema:          Fast EMA period. Default 9.
        slow_ema:          Slow EMA period. Default 21.
        trend_ema:         Trend filter EMA period. Default 50.
        adx_period:        ADX period. Default 14.
        adx_threshold:     Minimum ADX for trend confirmation. Default 25.
        atr_period:        ATR period for stop sizing. Default 14.
        atr_stop_mult:     Stop = entry ± mult × ATR. Default 1.5.
        rr_ratio:          Reward:risk ratio. Default 2.5.
        min_bars:          Minimum 15m bars before signals are allowed. Default 4.
        min_confidence:    Minimum confidence gate. Default 0.65.
        capital:           Indicative capital for qty sizing.
        risk_pct_per_trade: Fraction of capital to risk per trade.
        paper_trade:       If True, tag signal metadata with paper_trade=True.
    """

    candle_interval: str = "15minute"

    def __init__(
        self,
        name: str = "intraday_trend_15m",
        symbols: list[str] | None = None,
        market: str = "NSE",
        fast_ema: int = 9,
        slow_ema: int = 21,
        trend_ema: int = 50,
        adx_period: int = 14,
        adx_threshold: float = 25.0,
        atr_period: int = 14,
        atr_stop_mult: float = 1.5,
        rr_ratio: float = 2.5,
        min_bars: int = 4,
        min_confidence: float = 0.65,
        nav: float = 1_000_000.0,
        paper_trade: bool = True,
    ) -> None:
        super().__init__(name=name, symbols=symbols or [], market=market)
        self._fast        = fast_ema
        self._slow        = slow_ema
        self._trend       = trend_ema
        self._adx_p       = adx_period
        self._adx_thresh  = adx_threshold
        self._atr_p       = atr_period
        self._atr_stop_m  = atr_stop_mult
        self._rr          = rr_ratio
        self._min_bars    = min_bars
        self._min_conf    = min_confidence
        self._nav         = nav
        self._paper       = paper_trade

        _buf = max(trend_ema, adx_period) * 3
        self._closes: dict[str, deque[float]] = {}
        self._highs:  dict[str, deque[float]] = {}
        self._lows:   dict[str, deque[float]] = {}
        self._prev_fast_above: dict[str, Optional[bool]] = {}
        self._signal_fired_today: dict[str, set[str]] = {}
        self._bar_count: dict[str, int] = {}
        self._pending_signal: Optional[Signal] = None
        self._buf = _buf

    # ── BaseStrategy interface ────────────────────────────────────────────────

    async def on_tick(self, symbol: str, price: float, volume: int, timestamp: datetime) -> None:
        pass

    async def on_bar(self, bar: Bar) -> None:
        symbol = bar.symbol
        self._ensure(symbol)

        self._closes[symbol].append(bar.close)
        self._highs[symbol].append(bar.high)
        self._lows[symbol].append(bar.low)
        self._bar_count[symbol] = self._bar_count.get(symbol, 0) + 1

        ist = _ist_min(bar.timestamp)
        if not (_IST_NORMAL_START <= ist < _IST_NORMAL_END):
            return

        if self._bar_count[symbol] < self._min_bars:
            return

        if self._signal_fired_today.get(symbol) == {"BUY", "SELL"}:
            return

        closes = list(self._closes[symbol])
        if len(closes) < self._trend + 2:
            return

        fast_s  = ema_series(closes, self._fast)
        slow_s  = ema_series(closes, self._slow)
        trend_s = ema_series(closes, self._trend)

        fast_now, slow_now = fast_s[-1], slow_s[-1]
        fast_prev, slow_prev = fast_s[-2], slow_s[-2]
        trend_ema_val = trend_s[-1]

        if any(math.isnan(x) for x in [fast_now, slow_now, fast_prev, slow_prev, trend_ema_val]):
            return

        fast_above_now  = fast_now  > slow_now
        fast_above_prev = fast_prev > slow_prev

        if fast_above_now == fast_above_prev:
            self._prev_fast_above[symbol] = fast_above_now
            return

        direction = Direction.BUY if fast_above_now else Direction.SELL

        if direction.value in self._signal_fired_today.get(symbol, set()):
            self._prev_fast_above[symbol] = fast_above_now
            return

        # Trend filter: price must be on the correct side of EMA(50)
        price = bar.close
        if direction == Direction.BUY  and price < trend_ema_val:
            self._prev_fast_above[symbol] = fast_above_now
            return
        if direction == Direction.SELL and price > trend_ema_val:
            self._prev_fast_above[symbol] = fast_above_now
            return

        highs  = list(self._highs[symbol])
        lows   = list(self._lows[symbol])
        atr_v  = atr_wilder(highs, lows, closes, self._atr_p)
        adx_v  = adx(highs, lows, closes, self._adx_p)

        if math.isnan(atr_v) or atr_v <= 0:
            self._prev_fast_above[symbol] = fast_above_now
            return

        # ADX filter
        if math.isnan(adx_v) or adx_v < self._adx_thresh:
            logger.debug(
                "trend_15m.adx_too_low",
                symbol=symbol,
                adx=round(adx_v, 1) if not math.isnan(adx_v) else None,
                threshold=self._adx_thresh,
            )
            self._prev_fast_above[symbol] = fast_above_now
            return

        # Confidence: EMA spread in ATR units + ADX contribution
        spread     = abs(fast_now - slow_now)
        conf_ema   = min(0.4, spread / (atr_v * 2.0))
        conf_adx   = min(0.4, (adx_v - self._adx_thresh) / 50.0)
        confidence = min(1.0, 0.2 + conf_ema + conf_adx)

        if confidence < self._min_conf:
            self._prev_fast_above[symbol] = fast_above_now
            return

        signal = self._build(bar, direction, fast_now, slow_now, trend_ema_val, atr_v, adx_v, confidence)
        if signal is not None:
            self._pending_signal = signal
        fired = self._signal_fired_today.get(symbol, set())
        fired.add(direction.value)
        self._signal_fired_today[symbol] = fired
        self._prev_fast_above[symbol] = fast_above_now

    async def generate_signal(self) -> Optional[Signal]:
        signal = self._pending_signal
        self._pending_signal = None
        return signal

    def _ensure(self, symbol: str) -> None:
        if symbol not in self._closes:
            self._closes[symbol]  = deque(maxlen=self._buf)
            self._highs[symbol]   = deque(maxlen=self._buf)
            self._lows[symbol]    = deque(maxlen=self._buf)
            self._prev_fast_above[symbol]    = None
            self._signal_fired_today[symbol] = set()
            self._bar_count[symbol] = 0

    def _build(
        self, bar: Bar, direction: Direction,
        fast_ema: float, slow_ema: float, trend_ema: float,
        atr_v: float, adx_v: float, confidence: float,
    ) -> Signal:
        price      = bar.close
        stop_dist  = self._atr_stop_m * atr_v
        tp_dist    = stop_dist * self._rr
        stop_loss  = (price - stop_dist) if direction == Direction.BUY else (price + stop_dist)
        take_profit = (price + tp_dist)  if direction == Direction.BUY else (price - tp_dist)

        sizing = size_position(price, stop_dist, self._nav, confidence)
        if sizing.qty == 0 or sizing.rejected_if_exceeds_cap:
            return None

        logger.info(
            "trend_15m.signal",
            symbol=bar.symbol,
            direction=direction.value,
            price=price,
            adx=round(adx_v, 1),
            fast_ema=round(fast_ema, 4),
            slow_ema=round(slow_ema, 4),
            trend_ema=round(trend_ema, 4),
            confidence=round(confidence, 3),
        )

        return Signal(
            symbol=bar.symbol, market=self.market, direction=direction,
            quantity=sizing.qty, confidence=round(confidence, 4), strategy_name=self.name,
            price_at_signal=price, stop_loss=round(stop_loss, 4), take_profit=round(take_profit, 4),
            metadata={
                "fast_ema": round(fast_ema, 4), "slow_ema": round(slow_ema, 4),
                "trend_ema": round(trend_ema, 4), "adx": round(adx_v, 2),
                "atr": round(atr_v, 4), "rr_ratio": self._rr,
                "paper_trade": self._paper, "strategy_version": "trend_15m_v1",
                **sizing.to_metadata(),
            },
        )

    def reset_daily(self) -> None:
        """Reset daily signal tracking. Call at POST_CLOSE."""
        for symbol in self._signal_fired_today:
            self._signal_fired_today[symbol] = set()
        for symbol in self._bar_count:
            self._bar_count[symbol] = 0
        logger.info("trend_15m.daily_reset", strategy=self.name)
