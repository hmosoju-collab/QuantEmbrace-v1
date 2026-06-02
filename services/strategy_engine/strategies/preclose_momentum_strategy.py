"""
Pre-Close Momentum Strategy — directional bias trade in the PRE_CLOSE window.

Logic:
    1. Operates on 5-minute confirmed candles (``candle_interval = "5minute"``).
    2. At 14:30 IST, evaluate the last ``lookback_bars`` 5m candles (default 6 = 30 min).
       Build a directional bias score:
         - Count bullish bars (close > open) vs bearish bars (close < open).
         - Measure cumulative close-to-close momentum.
         - Require the last bar to be consistent with the bias direction.
    3. Entry fires if:
         a. bias_score ≥ ``min_bias_score`` (default 0.60).
         b. Current price is on the correct side of the VWAP.
         c. ATR(14) confirms volatility is within ``max_atr_pct`` of price (default 2%).
    4. Stop-loss: ``atr_stop_multiplier`` × ATR(14) from entry.
    5. Take-profit: one fixed distance = price × ``tp_pct`` (default 0.5%), or ATR-based if larger.
    6. Maximum 1 signal per symbol per session (pre-close is a single-shot trade).
    7. Position must be fully closed before 15:10 IST to avoid Zerodha MIS auto-square-off.
       This is signalled in metadata: ``must_close_by`` = "15:10 IST".

Phase gates:
    Only fires during PRE_CLOSE phase (14:45–15:10 IST window).
    No signals after 15:10 IST (too close to auto-square-off at 15:15).

Separation of concerns:
    Never checks positions, broker state, or risk limits.  Signal only.
    The ``must_close_by`` metadata hint is advisory — the MIS manager in
    execution_engine enforces the actual close.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from shared.logging.logger import get_logger
from shared.models.signal import Direction, Signal

from strategy_engine.strategies._math import atr_wilder, vwap
from strategy_engine.strategies._position_sizer import size_position
from strategy_engine.strategies.base_strategy import Bar, BaseStrategy

logger = get_logger(__name__, service_name="strategy_engine")

_IST_FIRE_START  = 14 * 60 + 45   # 14:45 IST — earliest signal time
_IST_FIRE_END    = 15 * 60 + 10   # 15:10 IST — no signals after this
_IST_MARKET_OPEN = 9  * 60 + 15   # 09:15 IST — VWAP accumulation start


def _ist_min(dt: datetime) -> int:
    if dt.tzinfo is not None:
        ist = dt.astimezone(timezone.utc) + timedelta(hours=5, minutes=30)
    else:
        ist = dt + timedelta(hours=5, minutes=30)
    return ist.hour * 60 + ist.minute


class PreCloseMomentumStrategy(BaseStrategy):
    """
    Pre-close directional momentum trade on confirmed 5m candles.

    Class attribute ``candle_interval = "5minute"`` routes 5m candles here.

    Parameters:
        lookback_bars:       Number of 5m bars to compute bias from. Default 6 (30 min).
        min_bias_score:      Minimum directional bias score (0–1). Default 0.60.
        max_atr_pct:         Max ATR as % of price — reject if market too volatile. Default 2.0.
        atr_period:          ATR period for stop sizing. Default 14.
        atr_stop_mult:       Stop = entry ± mult × ATR. Default 1.0.
        tp_pct:              Take-profit as % of entry price. Default 0.5 (50 bps).
        capital:             Indicative capital for qty sizing.
        risk_pct_per_trade:  Fraction of capital to risk per trade.
        paper_trade:         If True, tag signal metadata with paper_trade=True.
    """

    candle_interval: str = "5minute"

    def __init__(
        self,
        name: str = "preclose_momentum",
        symbols: list[str] | None = None,
        market: str = "NSE",
        lookback_bars: int = 6,
        min_bias_score: float = 0.60,
        max_atr_pct: float = 2.0,
        atr_period: int = 14,
        atr_stop_mult: float = 1.0,
        tp_pct: float = 0.5,
        nav: float = 1_000_000.0,
        paper_trade: bool = True,
    ) -> None:
        super().__init__(name=name, symbols=symbols or [], market=market)
        self._lookback      = lookback_bars
        self._min_bias      = min_bias_score
        self._max_atr_pct   = max_atr_pct / 100.0
        self._atr_p         = atr_period
        self._atr_stop_m    = atr_stop_mult
        self._tp_pct        = tp_pct / 100.0
        self._nav           = nav
        self._paper         = paper_trade

        _buf = max(lookback_bars + 2, atr_period * 2)
        self._closes:  dict[str, deque[float]] = {}
        self._opens:   dict[str, deque[float]] = {}
        self._highs:   dict[str, deque[float]] = {}
        self._lows:    dict[str, deque[float]] = {}
        self._volumes: dict[str, deque[int]]   = {}

        # Intraday OHLCV for VWAP (5m bars since open)
        self._vwap_h: dict[str, list[float]] = {}
        self._vwap_l: dict[str, list[float]] = {}
        self._vwap_c: dict[str, list[float]] = {}
        self._vwap_v: dict[str, list[int]]   = {}

        self._signal_fired: dict[str, bool] = {}
        self._pending_signal: Optional[Signal] = None
        self._buf = _buf

    # ── BaseStrategy interface ────────────────────────────────────────────────

    async def on_tick(self, symbol: str, price: float, volume: int, timestamp: datetime) -> None:
        pass

    async def on_bar(self, bar: Bar) -> None:
        symbol = bar.symbol
        self._ensure(symbol)

        self._closes[symbol].append(bar.close)
        self._opens[symbol].append(bar.open)
        self._highs[symbol].append(bar.high)
        self._lows[symbol].append(bar.low)
        self._volumes[symbol].append(bar.volume)

        ist = _ist_min(bar.timestamp)

        # Accumulate intraday data for VWAP from market open onwards
        if ist >= _IST_MARKET_OPEN:
            self._vwap_h[symbol].append(bar.high)
            self._vwap_l[symbol].append(bar.low)
            self._vwap_c[symbol].append(bar.close)
            self._vwap_v[symbol].append(bar.volume)

        # Phase gate: only fire in the pre-close window
        if not (_IST_FIRE_START <= ist < _IST_FIRE_END):
            return

        if self._signal_fired.get(symbol, False):
            return

        closes = list(self._closes[symbol])
        opens  = list(self._opens[symbol])
        highs  = list(self._highs[symbol])
        lows   = list(self._lows[symbol])

        if len(closes) < self._lookback:
            return

        # ATR check
        atr_v = atr_wilder(highs, lows, closes, self._atr_p)
        if math.isnan(atr_v) or atr_v <= 0:
            return

        price = bar.close
        if atr_v / price > self._max_atr_pct:
            logger.debug(
                "preclose.atr_too_high",
                symbol=symbol,
                atr_pct=round(atr_v / price * 100, 2),
                max_pct=self._max_atr_pct * 100,
            )
            return

        # Directional bias score from last N bars
        direction, bias_score = self._compute_bias(
            closes[-self._lookback:],
            opens[-self._lookback:],
        )
        if direction is None or bias_score < self._min_bias:
            return

        # VWAP filter: price must be on the correct side
        vwap_v = vwap(
            self._vwap_h[symbol], self._vwap_l[symbol],
            self._vwap_c[symbol], self._vwap_v[symbol],
        )
        if not math.isnan(vwap_v):
            if direction == Direction.BUY  and price < vwap_v:
                return
            if direction == Direction.SELL and price > vwap_v:
                return

        # Build signal
        stop_dist  = self._atr_stop_m * atr_v
        tp_dist    = max(price * self._tp_pct, stop_dist * 1.5)
        stop_loss  = (price - stop_dist) if direction == Direction.BUY else (price + stop_dist)
        take_profit = (price + tp_dist)  if direction == Direction.BUY else (price - tp_dist)
        confidence = min(1.0, 0.5 + (bias_score - self._min_bias))

        sizing = size_position(price, stop_dist, self._nav, confidence)
        if sizing.qty == 0 or sizing.rejected_if_exceeds_cap:
            return

        logger.info(
            "preclose.signal",
            symbol=symbol,
            direction=direction.value,
            price=price,
            bias_score=round(bias_score, 3),
            atr=round(atr_v, 4),
            vwap=round(vwap_v, 4) if not math.isnan(vwap_v) else None,
            confidence=round(confidence, 3),
        )

        self._pending_signal = Signal(
            symbol=bar.symbol, market=self.market, direction=direction,
            quantity=sizing.qty, confidence=round(confidence, 4), strategy_name=self.name,
            price_at_signal=price, stop_loss=round(stop_loss, 4), take_profit=round(take_profit, 4),
            metadata={
                "bias_score":    round(bias_score, 4),
                "atr":           round(atr_v, 4),
                "vwap":          round(vwap_v, 4) if not math.isnan(vwap_v) else None,
                "stop_distance": round(stop_dist, 4),
                "must_close_by": "15:10 IST",
                "paper_trade":   self._paper,
                "strategy_version": "preclose_mom_v1",
                **sizing.to_metadata(),
            },
        )
        self._signal_fired[symbol] = True

    async def generate_signal(self) -> Optional[Signal]:
        signal = self._pending_signal
        self._pending_signal = None
        return signal

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _compute_bias(
        self,
        closes: list[float],
        opens: list[float],
    ) -> tuple[Optional[Direction], float]:
        """
        Compute directional bias from the last N bars.

        Returns (direction, score) where score ∈ [0, 1].
        Returns (None, 0) if no clear bias.

        Score components:
          - Bullish/bearish bar ratio (how many bars close > open)
          - Cumulative close-to-close momentum direction
          - Whether the final bar is consistent with the bias
        """
        n = len(closes)
        if n < 2:
            return None, 0.0

        bull_bars = sum(1 for c, o in zip(closes, opens) if c > o)
        bear_bars = n - bull_bars

        # Cumulative return direction
        total_return = (closes[-1] - closes[0]) / closes[0] if closes[0] > 0 else 0.0
        return_sign = 1 if total_return > 0 else -1 if total_return < 0 else 0

        if return_sign == 0:
            return None, 0.0

        direction = Direction.BUY if return_sign > 0 else Direction.SELL

        # Consistency: last bar must be in the same direction
        last_bullish = closes[-1] > opens[-1]
        if direction == Direction.BUY  and not last_bullish:
            return None, 0.0
        if direction == Direction.SELL and last_bullish:
            return None, 0.0

        # Score: bar ratio + momentum magnitude
        bar_ratio    = bull_bars / n if direction == Direction.BUY else bear_bars / n
        momentum_mag = min(0.3, abs(total_return) * 100)   # cap at 30 bps contribution
        score        = bar_ratio * 0.7 + momentum_mag

        return direction, min(1.0, score)

    def _ensure(self, symbol: str) -> None:
        if symbol not in self._closes:
            self._closes[symbol]  = deque(maxlen=self._buf)
            self._opens[symbol]   = deque(maxlen=self._buf)
            self._highs[symbol]   = deque(maxlen=self._buf)
            self._lows[symbol]    = deque(maxlen=self._buf)
            self._volumes[symbol] = deque(maxlen=self._buf)
            self._vwap_h[symbol]  = []
            self._vwap_l[symbol]  = []
            self._vwap_c[symbol]  = []
            self._vwap_v[symbol]  = []
            self._signal_fired[symbol] = False

    def reset_daily(self) -> None:
        """Reset all session state. Call at POST_CLOSE."""
        for symbol in list(self._signal_fired.keys()):
            self._signal_fired[symbol] = False
            self._vwap_h[symbol] = []
            self._vwap_l[symbol] = []
            self._vwap_c[symbol] = []
            self._vwap_v[symbol] = []
        logger.info("preclose_momentum.daily_reset", strategy=self.name)
