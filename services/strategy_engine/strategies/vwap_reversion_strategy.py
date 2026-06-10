"""
VWAP Reversion Strategy — mean-reversion to intraday VWAP on 1m candles.

Logic:
    1. Computes rolling intraday VWAP from all 1m candles since market open
       (09:15 IST).  VWAP resets each session at POST_CLOSE.
    2. Computes upper/lower VWAP bands at ±``band_std`` volume-weighted standard
       deviations from VWAP.
    3. Entry conditions (BOTH must be true):
       a. Price has deviated beyond the band (close outside ±band_std σ).
       b. Reversal candle pattern: the bar shows a wick rejection toward VWAP.
          Defined as: the bar's close is closer to VWAP than the bar's extreme
          (wick pokes through the band but closes back inside).
    4. Direction: BUY when below lower band with reversal wick, SELL above upper band.
    5. ATR-based stop: entry ± ``atr_stop_multiplier`` × ATR(14).
       Minimum stop = max(half band width, ``min_stop_pct`` of price).
    6. Take-profit: VWAP itself (mean-reversion target).
    7. Reward:risk gate (vwap_rev_v2): distance-to-VWAP must be ≥
       ``min_reward_risk`` × stop distance — entries barely beyond the band
       have RR ≈ 1 and lose after costs. Plus the universal viability gate
       (strategies/_viability.py).
    8. Confidence: based on how many standard deviations the close is from VWAP.
       More deviation + wick → higher confidence.
    9. Direction-aware cooldown (``signal_cooldown_bars`` per symbol+direction),
       max ``max_signals_per_symbol_direction`` per day per (symbol, direction)
       (stops averaging into trend days), and a strategy-wide
       ``max_signals_per_day`` budget.

Phase gates:
    NORMAL only (10:15–14:30 IST).
    Does NOT fire before 10:15 — VWAP bands are unreliable without 60 min of history.
    Does NOT fire if intraday VWAP history < min_vwap_bars (default 60).
    Does NOT fire in the last 15 minutes before PRE_CLOSE (liquidity thins).

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

from strategy_engine.strategies._math import atr_wilder, vwap_bands
from strategy_engine.strategies._position_sizer import size_position
from strategy_engine.strategies._viability import MIN_STOP_PCT, check_signal_viability
from strategy_engine.strategies.base_strategy import Bar, BaseStrategy

logger = get_logger(__name__, service_name="strategy_engine")

_IST_MARKET_OPEN = 9 * 60 + 15   # 09:15 IST
_IST_NORMAL_START = 9 * 60 + 30  # 09:30 IST
# 10:15 IST — entries need 60 minutes of VWAP history; the old 09:45 start
# fired on statistically thin bands exactly when volatility is highest.
_IST_VWAP_START   = 10 * 60 + 15
_IST_NORMAL_END   = 14 * 60 + 30 # 14:30 IST (stop 15min before pre-close)


def _ist_min(dt: datetime) -> int:
    if dt.tzinfo is not None:
        ist = dt.astimezone(timezone.utc) + timedelta(hours=5, minutes=30)
    else:
        ist = dt + timedelta(hours=5, minutes=30)
    return ist.hour * 60 + ist.minute


class VWAPReversionStrategy(BaseStrategy):
    """
    Intraday VWAP mean-reversion strategy on confirmed 1m candles.

    Class attribute ``candle_interval = "minute"`` routes 1m candles here.

    Parameters:
        band_std:             VWAP band width in standard deviations. Default 2.0.
        atr_period:           ATR period for stop sizing. Default 14.
        atr_stop_multiplier:  Stop = entry ± mult × ATR. Default 1.0.
        min_confidence:       Minimum confidence gate. Default 0.60.
        signal_cooldown_bars: Bars to wait after a signal before firing again. Default 5.
        min_wick_ratio:       Minimum wick/bar-range ratio for reversal confirmation. Default 0.4.
        capital:              Indicative capital for qty sizing.
        risk_pct_per_trade:   Fraction of capital to risk per trade.
        paper_trade:          If True, tag signal metadata with paper_trade=True.
    """

    candle_interval: str = "minute"

    def __init__(
        self,
        name: str = "vwap_reversion",
        symbols: list[str] | None = None,
        market: str = "NSE",
        band_std: float = 2.5,
        atr_period: int = 14,
        atr_stop_multiplier: float = 1.0,
        min_confidence: float = 0.60,
        signal_cooldown_bars: int = 15,
        min_wick_ratio: float = 0.4,
        min_vwap_bars: int = 60,
        min_reward_risk: float = 1.5,
        min_stop_pct: float = MIN_STOP_PCT,
        max_signals_per_day: int = 6,
        max_signals_per_symbol_direction: int = 2,
        nav: float = 1_000_000.0,
        paper_trade: bool = True,
    ) -> None:
        super().__init__(name=name, symbols=symbols or [], market=market)
        self._band_std         = band_std
        self._atr_p            = atr_period
        self._atr_stop_mult    = atr_stop_multiplier
        self._min_conf         = min_confidence
        self._cooldown         = signal_cooldown_bars
        self._min_wick_ratio   = min_wick_ratio
        # Minimum candles in the intraday VWAP history before signals are valid;
        # prevents entries on thin early-session VWAP with few data points
        self._min_vwap_bars    = min_vwap_bars
        # Mean reversion at RR ≈ 1 needs ~65% WR after costs — committee gate:
        # distance-to-VWAP must be ≥ min_reward_risk × stop distance.
        self._min_rr           = min_reward_risk
        self._min_stop_pct     = min_stop_pct
        self._max_signals_per_day = max_signals_per_day
        self._max_per_sym_dir  = max_signals_per_symbol_direction
        self._nav              = nav
        self._paper            = paper_trade

        # Per-symbol intraday OHLCV accumulators (reset each session)
        _buf = 400   # max 400 1m candles per session (market hours ~375m)
        self._intraday_highs:   dict[str, list[float]] = {}
        self._intraday_lows:    dict[str, list[float]] = {}
        self._intraday_closes:  dict[str, list[float]] = {}
        self._intraday_volumes: dict[str, list[int]]   = {}

        # Rolling ATR buffers (separate — ATR uses full history, not just intraday)
        self._atr_highs:  dict[str, deque[float]] = {}
        self._atr_lows:   dict[str, deque[float]] = {}
        self._atr_closes: dict[str, deque[float]] = {}

        # Direction-aware cooldown: per-symbol bar index + last signal index
        # per (symbol, direction). The old per-symbol counter let the strategy
        # re-fire the same direction every 5 bars into a trend day.
        self._bar_index: dict[str, int] = {}
        self._last_signal_idx: dict[tuple[str, str], int] = {}
        # Daily budgets
        self._signals_today: int = 0
        self._sym_dir_count: dict[tuple[str, str], int] = {}

        self._pending_signal: Optional[Signal] = None

    # ── BaseStrategy interface ────────────────────────────────────────────────

    async def on_tick(self, symbol: str, price: float, volume: int, timestamp: datetime) -> None:
        """VWAP strategy operates on confirmed 1m bars only."""

    async def on_bar(self, bar: Bar) -> None:
        symbol = bar.symbol
        self._ensure_buffers(symbol)

        ist = _ist_min(bar.timestamp)

        # Reset intraday accumulators at the session start
        if ist < _IST_MARKET_OPEN:
            self._intraday_highs[symbol]   = []
            self._intraday_lows[symbol]    = []
            self._intraday_closes[symbol]  = []
            self._intraday_volumes[symbol] = []

        # Accumulate intraday candles
        if ist >= _IST_MARKET_OPEN:
            self._intraday_highs[symbol].append(bar.high)
            self._intraday_lows[symbol].append(bar.low)
            self._intraday_closes[symbol].append(bar.close)
            self._intraday_volumes[symbol].append(bar.volume)

        # ATR buffer (longer history)
        self._atr_highs[symbol].append(bar.high)
        self._atr_lows[symbol].append(bar.low)
        self._atr_closes[symbol].append(bar.close)

        # Bar index tick (drives the direction-aware cooldown)
        self._bar_index[symbol] = self._bar_index.get(symbol, 0) + 1

        # Phase gate: VWAP entries wait until 10:15 so bands form on 60min of data
        if not (_IST_VWAP_START <= ist < _IST_NORMAL_END):
            return

        # Strategy-wide daily signal budget (committee cap: 6/day)
        if self._signals_today >= self._max_signals_per_day:
            return

        n_intraday = len(self._intraday_closes.get(symbol, []))
        if n_intraday < self._min_vwap_bars:
            return

        # Compute VWAP + bands
        i_h = self._intraday_highs[symbol]
        i_l = self._intraday_lows[symbol]
        i_c = self._intraday_closes[symbol]
        i_v = self._intraday_volumes[symbol]

        vwap_val, upper_band, lower_band = vwap_bands(i_h, i_l, i_c, i_v, self._band_std)

        if any(math.isnan(x) for x in [vwap_val, upper_band, lower_band]):
            return

        # ATR for stop sizing
        atr = atr_wilder(
            list(self._atr_highs[symbol]),
            list(self._atr_lows[symbol]),
            list(self._atr_closes[symbol]),
            self._atr_p,
        )
        if math.isnan(atr) or atr <= 0:
            return

        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return

        # ── BUY: price below lower band + wick rejection upward ──────────────
        if bar.close < lower_band:
            lower_wick = bar.close - bar.low
            wick_ratio = lower_wick / bar_range
            if wick_ratio >= self._min_wick_ratio and self._can_fire(symbol, Direction.BUY):
                # Lower wick pokes below band, close rises back — rejection confirmed
                deviation_std = (vwap_val - bar.close) / (vwap_val - lower_band) if (vwap_val - lower_band) != 0 else 0
                confidence = min(1.0, 0.5 + deviation_std * 0.25 + wick_ratio * 0.25)
                if confidence >= self._min_conf:
                    self._emit(bar, Direction.BUY, vwap_val, upper_band, lower_band, atr, confidence)

        # ── SELL: price above upper band + wick rejection downward ───────────
        elif bar.close > upper_band:
            upper_wick = bar.high - bar.close
            wick_ratio = upper_wick / bar_range
            if wick_ratio >= self._min_wick_ratio and self._can_fire(symbol, Direction.SELL):
                deviation_std = (bar.close - vwap_val) / (upper_band - vwap_val) if (upper_band - vwap_val) != 0 else 0
                confidence = min(1.0, 0.5 + deviation_std * 0.25 + wick_ratio * 0.25)
                if confidence >= self._min_conf:
                    self._emit(bar, Direction.SELL, vwap_val, upper_band, lower_band, atr, confidence)

    async def generate_signal(self) -> Optional[Signal]:
        signal = self._pending_signal
        self._pending_signal = None
        return signal

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _can_fire(self, symbol: str, direction: Direction) -> bool:
        """Direction-aware cooldown + per-(symbol, direction) daily budget."""
        key = (symbol, direction.value)
        if self._sym_dir_count.get(key, 0) >= self._max_per_sym_dir:
            return False
        last = self._last_signal_idx.get(key)
        if last is not None and self._bar_index.get(symbol, 0) - last < self._cooldown:
            return False
        return True

    def _emit(
        self,
        bar: Bar,
        direction: Direction,
        vwap_val: float,
        upper_band: float,
        lower_band: float,
        atr: float,
        confidence: float,
    ) -> None:
        price      = bar.close
        stop_dist  = max(
            self._atr_stop_mult * atr,
            abs(upper_band - lower_band) / 2,
            (self._min_stop_pct / 100.0) * price,
        )
        stop_loss  = (price - stop_dist) if direction == Direction.BUY else (price + stop_dist)
        take_profit = vwap_val   # Mean-reversion target

        if stop_dist <= 0:
            return

        # Reward:risk gate — the distance back to VWAP is all the trade can
        # earn; entries barely beyond the band have RR ≈ 1 and lose after costs.
        reward = abs(vwap_val - price)
        if reward < self._min_rr * stop_dist:
            logger.info(
                "vwap_reversion.rejected_rr",
                symbol=bar.symbol,
                direction=direction.value,
                price=price,
                reward=round(reward, 4),
                stop_distance=round(stop_dist, 4),
                reward_risk=round(reward / stop_dist, 2),
                min_reward_risk=self._min_rr,
            )
            return

        viability = check_signal_viability(
            price, stop_dist, reward, min_stop_pct=self._min_stop_pct
        )
        if not viability.viable:
            logger.info(
                "vwap_reversion.rejected_viability",
                symbol=bar.symbol,
                direction=direction.value,
                price=price,
                reject_reason=viability.reason,
            )
            return

        sizing = size_position(price, stop_dist, self._nav, confidence)
        if sizing.qty == 0 or sizing.rejected_if_exceeds_cap:
            return

        logger.info(
            "vwap_reversion.signal",
            symbol=bar.symbol,
            direction=direction.value,
            price=price,
            vwap=round(vwap_val, 4),
            upper=round(upper_band, 4),
            lower=round(lower_band, 4),
            atr=round(atr, 4),
            confidence=round(confidence, 3),
        )

        self._pending_signal = Signal(
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
                "vwap":           round(vwap_val, 4),
                "upper_band":     round(upper_band, 4),
                "lower_band":     round(lower_band, 4),
                "band_std":       self._band_std,
                "atr":            round(atr, 4),
                "stop_distance":  round(stop_dist, 4),
                "reward_risk":    round(reward / stop_dist, 4),
                "paper_trade":    self._paper,
                "strategy_version": "vwap_rev_v2",
                **viability.to_metadata(),
                **sizing.to_metadata(),
            },
        )
        key = (bar.symbol, direction.value)
        self._last_signal_idx[key] = self._bar_index.get(bar.symbol, 0)
        self._sym_dir_count[key] = self._sym_dir_count.get(key, 0) + 1
        self._signals_today += 1

    def _ensure_buffers(self, symbol: str) -> None:
        if symbol not in self._intraday_closes:
            self._intraday_highs[symbol]   = []
            self._intraday_lows[symbol]    = []
            self._intraday_closes[symbol]  = []
            self._intraday_volumes[symbol] = []
            self._atr_highs[symbol]  = deque(maxlen=60)
            self._atr_lows[symbol]   = deque(maxlen=60)
            self._atr_closes[symbol] = deque(maxlen=60)
            self._bar_index[symbol]  = 0

    def reset_daily(self) -> None:
        """Reset all intraday state. Call at POST_CLOSE."""
        for symbol in list(self._intraday_closes.keys()):
            self._intraday_highs[symbol]   = []
            self._intraday_lows[symbol]    = []
            self._intraday_closes[symbol]  = []
            self._intraday_volumes[symbol] = []
            self._bar_index[symbol]        = 0
        self._last_signal_idx.clear()
        self._sym_dir_count.clear()
        self._signals_today = 0
        logger.info("vwap_reversion.daily_reset", strategy=self.name)
