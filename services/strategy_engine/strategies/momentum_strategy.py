"""
Momentum Strategy — production-grade MA crossover with ATR-based risk sizing.

Logic:
    - Dual MA crossover (golden cross / death cross) for entry timing.
    - ATR (Average True Range) used for:
        * Dynamic stop-loss placement (entry ± atr_stop_multiplier × ATR)
        * Position sizing (risk a fixed % of capital per trade)
        * Take-profit target (entry ± atr_tp_multiplier × ATR)
    - Signals carry stop_loss and take_profit levels for use by the risk engine.
    - Minimum confidence gate prevents low-conviction noise trades.

Separation of concerns:
    - This strategy NEVER checks risk limits, queries positions, or calls brokers.
    - Position sizing here is indicative only; the risk engine may override qty.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime
from typing import Optional

from shared.logging.logger import get_logger

from strategy_engine.signals.signal import Direction, Signal
from strategy_engine.strategies.base_strategy import Bar, BaseStrategy

logger = get_logger(__name__, service_name="strategy_engine")


class MomentumStrategy(BaseStrategy):
    """
    MA crossover momentum strategy with ATR-based position sizing and stop-loss.

    Parameters:
        short_window:          Bars for the short-term SMA.
        long_window:           Bars for the long-term SMA.
        atr_period:            Bars for ATR calculation (default 14).
        atr_stop_multiplier:   Stop-loss = entry ± (multiplier × ATR). Default 2.0.
        atr_tp_multiplier:     Take-profit = entry ± (multiplier × ATR). Default 3.0.
        min_confidence:        Minimum confidence to emit a signal. Default 0.55.
        risk_pct_per_trade:    Fraction of capital to risk per trade (0.01 = 1%).
        capital:               Indicative capital for qty sizing. Risk engine may adjust.
    """

    def __init__(
        self,
        name: str = "momentum_v2",
        symbols: list[str] | None = None,
        market: str = "NSE",
        short_window: int = 10,
        long_window: int = 50,
        atr_period: int = 14,
        atr_stop_multiplier: float = 2.0,
        atr_tp_multiplier: float = 3.0,
        min_confidence: float = 0.55,
        risk_pct_per_trade: float = 0.01,
        capital: float = 1_000_000.0,
    ) -> None:
        super().__init__(name=name, symbols=symbols or [], market=market)
        if short_window >= long_window:
            raise ValueError(f"short_window ({short_window}) must be < long_window ({long_window})")
        if atr_period < 2:
            raise ValueError("atr_period must be >= 2")

        self._short_window = short_window
        self._long_window = long_window
        self._atr_period = atr_period
        self._atr_stop_mult = atr_stop_multiplier
        self._atr_tp_mult = atr_tp_multiplier
        self._min_confidence = min_confidence
        self._risk_pct = risk_pct_per_trade
        self._capital = capital

        # Per-symbol price history (close prices)
        self._closes: dict[str, deque[float]] = {}
        # Per-symbol true range history for ATR
        self._true_ranges: dict[str, deque[float]] = {}
        # Previous close for TR calculation
        self._prev_close: dict[str, float] = {}
        # Previous MA crossover state
        self._prev_short_above_long: dict[str, Optional[bool]] = {}
        # Pending signal consumed by generate_signal()
        self._pending_signal: Optional[Signal] = None

    # ─────────────────────────────────────────────────────────────────────────
    # Public interface
    # ─────────────────────────────────────────────────────────────────────────

    async def on_tick(
        self, symbol: str, price: float, volume: int, timestamp: datetime
    ) -> None:
        """Update close price buffer from tick data (no signal generation)."""
        if symbol not in self._closes:
            self._closes[symbol] = deque(maxlen=self._long_window)
        self._closes[symbol].append(price)

    async def on_bar(self, bar: Bar) -> None:
        """
        Process an OHLCV bar.

        Computes:
            - Rolling SMA crossover
            - ATR (Wilder smoothing)
            - Stop-loss and take-profit levels
            - Indicative position size
        """
        symbol = bar.symbol
        self._update_buffers(symbol, bar)

        closes = list(self._closes[symbol])
        true_ranges = list(self._true_ranges[symbol])

        # Need enough data for both windows and ATR
        required = max(self._long_window, self._atr_period)
        if len(closes) < required or len(true_ranges) < self._atr_period:
            return

        short_ma = _sma(closes, self._short_window)
        long_ma = _sma(closes, self._long_window)
        atr = _atr(true_ranges, self._atr_period)

        if atr <= 0:
            return

        short_above_long = short_ma > long_ma
        prev_state = self._prev_short_above_long.get(symbol)

        if prev_state is not None and short_above_long != prev_state:
            confidence = self._compute_confidence(short_ma, long_ma, atr, bar.close)
            if confidence >= self._min_confidence:
                self._pending_signal = self._build_signal(
                    bar=bar,
                    direction=Direction.BUY if short_above_long else Direction.SELL,
                    confidence=confidence,
                    atr=atr,
                    short_ma=short_ma,
                    long_ma=long_ma,
                )

        self._prev_short_above_long[symbol] = short_above_long

    async def generate_signal(self) -> Optional[Signal]:
        """Return the pending signal (if any) and clear the buffer."""
        signal = self._pending_signal
        self._pending_signal = None
        return signal

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _update_buffers(self, symbol: str, bar: Bar) -> None:
        """Update close and true-range buffers with the new bar."""
        if symbol not in self._closes:
            self._closes[symbol] = deque(maxlen=self._long_window)
            self._true_ranges[symbol] = deque(maxlen=self._atr_period * 3)

        self._closes[symbol].append(bar.close)

        # True Range = max(H-L, |H-prev_C|, |L-prev_C|)
        prev_close = self._prev_close.get(symbol, bar.close)
        tr = max(
            bar.high - bar.low,
            abs(bar.high - prev_close),
            abs(bar.low - prev_close),
        )
        self._true_ranges[symbol].append(tr)
        self._prev_close[symbol] = bar.close

    def _compute_confidence(
        self, short_ma: float, long_ma: float, atr: float, price: float
    ) -> float:
        """
        Confidence = MA divergence (% of price) scaled against 1 ATR.

        Intuition: a crossover where the MAs have separated by more than 1 ATR
        is a high-conviction event. We cap at 1.0 and floor at 0.0.
        """
        if price <= 0 or atr <= 0:
            return 0.0
        divergence_pct = abs(short_ma - long_ma) / price
        atr_pct = atr / price
        raw = divergence_pct / max(atr_pct, 1e-9)
        return max(0.0, min(1.0, raw))

    def _compute_quantity(self, price: float, stop_distance: float) -> int:
        """
        Risk-based position sizing.

        qty = (capital × risk_pct) / stop_distance
        Ensures that if the stop is hit, we lose at most risk_pct of capital.
        """
        if stop_distance <= 0 or price <= 0:
            return 1
        risk_capital = self._capital * self._risk_pct
        qty = risk_capital / stop_distance
        return max(1, math.floor(qty))

    def _build_signal(
        self,
        bar: Bar,
        direction: Direction,
        confidence: float,
        atr: float,
        short_ma: float,
        long_ma: float,
    ) -> Signal:
        """Construct a Signal with ATR-derived stop-loss and take-profit."""
        price = bar.close
        stop_distance = self._atr_stop_mult * atr
        tp_distance = self._atr_tp_mult * atr

        if direction == Direction.BUY:
            stop_loss = price - stop_distance
            take_profit = price + tp_distance
        else:
            stop_loss = price + stop_distance
            take_profit = price - tp_distance

        quantity = self._compute_quantity(price, stop_distance)

        signal = Signal(
            symbol=bar.symbol,
            market=self.market,
            direction=direction,
            quantity=quantity,
            confidence=confidence,
            strategy_name=self.name,
            price_at_signal=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata={
                "short_ma": round(short_ma, 4),
                "long_ma": round(long_ma, 4),
                "atr": round(atr, 4),
                "atr_stop_mult": self._atr_stop_mult,
                "atr_tp_mult": self._atr_tp_mult,
                "stop_distance": round(stop_distance, 4),
                "risk_reward_ratio": round(tp_distance / stop_distance, 2),
            },
        )

        logger.info(
            "Momentum signal: %s %s @ %.4f | qty=%d | SL=%.4f | TP=%.4f | "
            "conf=%.3f | short_ma=%.4f | long_ma=%.4f | ATR=%.4f",
            direction.value,
            bar.symbol,
            price,
            quantity,
            stop_loss,
            take_profit,
            confidence,
            short_ma,
            long_ma,
            atr,
        )

        return signal


# ─────────────────────────────────────────────────────────────────────────────
# Pure math helpers (module-level for easy unit testing)
# ─────────────────────────────────────────────────────────────────────────────


def _sma(values: list[float], window: int) -> float:
    """Simple moving average over the last *window* elements."""
    if len(values) < window:
        return float("nan")
    return sum(values[-window:]) / window


def _atr(true_ranges: list[float], period: int) -> float:
    """
    Average True Range using Wilder's EMA smoothing.

    First value: simple average of first *period* TRs.
    Subsequent: ATR_t = ((period - 1) × ATR_t-1 + TR_t) / period
    """
    if len(true_ranges) < period:
        return float("nan")

    # Seed with simple average of the first period TRs
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = ((period - 1) * atr + tr) / period
    return atr
