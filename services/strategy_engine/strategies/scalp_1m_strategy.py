"""
Scalp 1m Strategy — EMA(9/21) crossover with volume + momentum confirmation.

Logic:
    1. Compute EMA(9) and EMA(21) on 1-minute confirmed candles.
    2. Entry on a confirmed crossover: short EMA crosses above long EMA (BUY)
       or below (SELL).
    3. Volume filter: the crossover bar's volume must be ≥ ``volume_multiplier`` ×
       rolling 10-bar average volume.  Prevents signals on low-liquidity candles
       where spreads widen and slippage is material.
    4. Momentum filter: the bar's body (|close - open|) must be ≥
       ``min_body_atr_pct`` × ATR(14).  Eliminates doji/spinning-top bars where
       the direction is ambiguous.
    5. Stop-loss: largest of ATR floor, spread floor, tick floor, pct floor.
       Never sits inside normal bid-ask noise.
    6. Take-profit: max(rr_ratio × stop_dist, min_target_pct × price).
    7. Viability filter: reject if net expected edge < min_net_edge_pct after
       all round-trip transaction costs.
    8. Spread filter: reject if estimated spread > max_spread_pct.
    9. Daily signal cap: max ``max_signals_per_day`` per symbol per direction.

Phase gates:
    Only fires during NORMAL phase (09:30–14:45 IST).
    Silent during MARKET_OPEN (first 15 min) to avoid gap noise.
    Silent during PRE_CLOSE to avoid thin liquidity.

Separation of concerns:
    Never checks positions, margin, or risk limits.  Signal only.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from shared.logging.logger import get_logger
from shared.models.signal import Direction, Signal

from strategy_engine.strategies._math import atr_wilder, ema_series
from strategy_engine.strategies._position_sizer import size_position
from strategy_engine.strategies.base_strategy import Bar, BaseStrategy

logger = get_logger(__name__, service_name="strategy_engine")

_IST_NORMAL_START = 9 * 60 + 30   # 09:30 IST
_IST_NORMAL_END   = 14 * 60 + 45  # 14:45 IST

# NSE standard minimum tick size for equity instruments
_TICK_SIZE_NSE: float = 0.05

# Estimated round-trip transaction costs (brokerage + STT + exchange + GST).
# Conservative estimate for intraday scalp: ~15 bps all-in.
# Zerodha: ₹20/order or 0.03% intraday; STT 0.025% sell; exchange 0.00375%;
# GST 18% on brokerage. For a ₹50k notional round-trip ≈ 12–18 bps.
_COST_ROUNDTRIP_PCT: float = 0.15

# Candle staleness threshold — reject if bar is older than this
_MAX_CANDLE_AGE_SECONDS: float = 120.0


def _ist_min(dt: datetime) -> int:
    if dt.tzinfo is not None:
        ist = dt.astimezone(timezone.utc) + timedelta(hours=5, minutes=30)
    else:
        ist = dt + timedelta(hours=5, minutes=30)
    return ist.hour * 60 + ist.minute


@dataclass
class ScalpStopResult:
    """Result of the stop-floor computation, carried into signal metadata."""
    stop_distance: float
    stop_distance_source: str      # ATR | SPREAD_FLOOR | TICK_FLOOR | PCT_FLOOR
    tp_distance: float
    tp_distance_source: str        # RR | PCT_FLOOR
    atr_stop_distance: float
    spread_floor_distance: float
    tick_floor_distance: float
    pct_floor_distance: float
    spread_estimate: float
    net_expected_edge_pct: float


class Scalp1mStrategy(BaseStrategy):
    """
    1-minute scalp strategy using EMA crossover with volume + body confirmation.

    Class attribute ``candle_interval = "minute"`` routes 1m candles here.

    Parameters:
        fast_ema_period:          Fast EMA period. Default 9.
        slow_ema_period:          Slow EMA period. Default 21.
        atr_period:               ATR period for stop sizing. Default 14.
        atr_stop_multiplier:      Base ATR stop multiplier. Default 0.5.
        rr_ratio:                 Target R:R ratio. Default 1.5.
        volume_multiplier:        Crossover bar volume filter. Default 1.5.
        min_body_atr_pct:         Bar body minimum as fraction of ATR. Default 0.3.
        min_confidence:           Minimum EMA spread confidence gate. Default 0.55.
        max_signals_per_day:      Max signals per symbol per direction. Default 3.
        min_stop_pct:             Stop floor: min % of entry price. Default 0.15.
        min_target_pct:           TP floor: min % of entry price. Default 0.25.
        min_spread_multiple:      Stop floor: multiples of estimated spread. Default 3.
        min_tick_multiple:        Stop floor: multiples of tick size. Default 5.
        max_spread_pct:           Reject if estimated spread > this %. Default 0.08.
        min_net_edge_pct:         Reject if net edge < this % after costs. Default 0.12.
        reject_if_spread_unavailable: Reject when bar range is zero. Default True.
        reject_if_ltp_stale:      Reject when candle timestamp > 2 min old. Default True.
        nav:                      Portfolio NAV for position sizing.
        paper_trade:              Tag signals as paper if True.
    """

    candle_interval: str = "minute"

    def __init__(
        self,
        name: str = "scalp_1m",
        symbols: list[str] | None = None,
        market: str = "NSE",
        fast_ema_period: int = 9,
        slow_ema_period: int = 21,
        atr_period: int = 14,
        atr_stop_multiplier: float = 0.5,
        rr_ratio: float = 1.5,
        volume_multiplier: float = 1.5,
        min_body_atr_pct: float = 0.3,
        min_confidence: float = 0.55,
        max_signals_per_day: int = 3,
        min_stop_pct: float = 0.15,
        min_target_pct: float = 0.25,
        min_spread_multiple: float = 3.0,
        min_tick_multiple: float = 5.0,
        max_spread_pct: float = 0.08,
        min_net_edge_pct: float = 0.12,
        reject_if_spread_unavailable: bool = True,
        reject_if_ltp_stale: bool = True,
        nav: float = 1_000_000.0,
        paper_trade: bool = True,
    ) -> None:
        super().__init__(name=name, symbols=symbols or [], market=market)
        if fast_ema_period >= slow_ema_period:
            raise ValueError(f"fast_ema_period ({fast_ema_period}) must be < slow_ema_period ({slow_ema_period})")

        self._fast    = fast_ema_period
        self._slow    = slow_ema_period
        self._atr_p   = atr_period
        self._atr_stop_mult = atr_stop_multiplier
        self._rr      = rr_ratio
        self._vol_mult = volume_multiplier
        self._min_body_pct = min_body_atr_pct
        self._min_conf = min_confidence
        self._max_sig  = max_signals_per_day
        self._nav      = nav
        self._paper    = paper_trade

        # Floor / filter config
        self._min_stop_pct   = min_stop_pct / 100.0
        self._min_target_pct = min_target_pct / 100.0
        self._min_spread_mult = min_spread_multiple
        self._min_tick_mult   = min_tick_multiple
        self._max_spread_pct  = max_spread_pct / 100.0
        self._min_net_edge_pct = min_net_edge_pct
        self._reject_if_spread_unavailable = reject_if_spread_unavailable
        self._reject_if_ltp_stale = reject_if_ltp_stale

        # Per-symbol buffers
        _buf = slow_ema_period * 3
        self._closes:  dict[str, deque[float]] = {}
        self._highs:   dict[str, deque[float]] = {}
        self._lows:    dict[str, deque[float]] = {}
        self._volumes: dict[str, deque[int]]   = {}
        self._bar_ranges: dict[str, deque[float]] = {}  # recent H-L ranges for spread estimation

        self._prev_fast_above: dict[str, Optional[bool]] = {}
        self._signal_count:    dict[str, dict[str, int]] = {}

        self._pending_signal: Optional[Signal] = None
        self._buf_size = _buf

        # Rejection counters (diagnostic)
        self._rejected_spread_wide: int = 0
        self._rejected_net_edge: int = 0
        self._rejected_stale: int = 0
        self._rejected_spread_unavailable: int = 0
        self._rejected_tp_inside_spread: int = 0

    # ── BaseStrategy interface ────────────────────────────────────────────────

    async def on_tick(self, symbol: str, price: float, volume: int, timestamp: datetime) -> None:
        """Scalp operates on confirmed 1m bars only."""

    async def on_bar(self, bar: Bar) -> None:
        """Process a 1m candle and check for EMA crossover signal."""
        symbol = bar.symbol
        self._ensure_buffers(symbol)

        bar_range = bar.high - bar.low
        self._closes[symbol].append(bar.close)
        self._highs[symbol].append(bar.high)
        self._lows[symbol].append(bar.low)
        self._volumes[symbol].append(bar.volume)
        self._bar_ranges[symbol].append(bar_range)

        # Phase gate: only act during NORMAL session
        ist = _ist_min(bar.timestamp)
        if not (_IST_NORMAL_START <= ist < _IST_NORMAL_END):
            return

        closes = list(self._closes[symbol])
        if len(closes) < self._slow + 2:
            return

        fast_series = ema_series(closes, self._fast)
        slow_series = ema_series(closes, self._slow)
        if len(fast_series) < 2 or len(slow_series) < 2:
            return

        fast_now  = fast_series[-1]
        slow_now  = slow_series[-1]
        fast_prev = fast_series[-2]
        slow_prev = slow_series[-2]

        if any(math.isnan(x) for x in [fast_now, slow_now, fast_prev, slow_prev]):
            return

        fast_above_now  = fast_now  > slow_now
        fast_above_prev = fast_prev > slow_prev

        if fast_above_now == fast_above_prev:
            self._prev_fast_above[symbol] = fast_above_now
            return

        direction = Direction.BUY if fast_above_now else Direction.SELL

        # Cap daily signals
        counts = self._signal_count.get(symbol, {"BUY": 0, "SELL": 0})
        if counts.get(direction.value, 0) >= self._max_sig:
            logger.debug("scalp_1m.daily_cap_reached", symbol=symbol, direction=direction.value)
            self._prev_fast_above[symbol] = fast_above_now
            return

        # Staleness filter
        if self._reject_if_ltp_stale:
            age = (datetime.now(timezone.utc) - bar.timestamp.replace(tzinfo=timezone.utc)
                   if bar.timestamp.tzinfo is None
                   else datetime.now(timezone.utc) - bar.timestamp).total_seconds()
            if age > _MAX_CANDLE_AGE_SECONDS:
                self._rejected_stale += 1
                logger.info(
                    "scalp_1m.rejected_stale_candle",
                    symbol=symbol, age_seconds=round(age, 1),
                )
                self._prev_fast_above[symbol] = fast_above_now
                return

        # Volume confirmation
        volumes = list(self._volumes[symbol])
        avg_vol = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else 0.0
        if avg_vol > 0 and bar.volume < self._vol_mult * avg_vol:
            logger.debug("scalp_1m.low_volume", symbol=symbol, bar_volume=bar.volume)
            self._prev_fast_above[symbol] = fast_above_now
            return

        # ATR
        highs  = list(self._highs[symbol])
        lows   = list(self._lows[symbol])
        atr    = atr_wilder(highs, lows, closes, self._atr_p)
        if math.isnan(atr) or atr <= 0:
            self._prev_fast_above[symbol] = fast_above_now
            return

        # Bar body filter
        body = abs(bar.close - bar.open)
        if body < self._min_body_pct * atr:
            logger.debug("scalp_1m.doji_bar", symbol=symbol)
            self._prev_fast_above[symbol] = fast_above_now
            return

        # Confidence
        spread     = abs(fast_now - slow_now)
        confidence = min(1.0, 0.5 + spread / (atr * 2.0))
        if confidence < self._min_conf:
            self._prev_fast_above[symbol] = fast_above_now
            return

        # Spread availability check before floor calculation
        recent_ranges = list(self._bar_ranges[symbol])
        if self._reject_if_spread_unavailable and bar_range <= 0:
            self._rejected_spread_unavailable += 1
            logger.info("scalp_1m.rejected_spread_unavailable", symbol=symbol)
            self._prev_fast_above[symbol] = fast_above_now
            return

        signal = self._build_signal(bar, direction, fast_now, slow_now, atr, confidence, recent_ranges)
        if signal is not None:
            self._pending_signal = signal
        counts[direction.value] = counts.get(direction.value, 0) + 1
        self._signal_count[symbol] = counts
        self._prev_fast_above[symbol] = fast_above_now

    async def generate_signal(self) -> Optional[Signal]:
        signal = self._pending_signal
        self._pending_signal = None
        return signal

    # ── Stop-floor logic ─────────────────────────────────────────────────────

    def _estimate_spread(self, price: float, recent_ranges: list[float]) -> float:
        """Estimate bid-ask spread from recent bar ranges.

        Uses median of recent H-L ranges × 10% as a conservative spread proxy.
        Falls back to 5 bps of price if no range data available.
        Typical NSE liquid stock spread: 5–20 paise.
        """
        if recent_ranges:
            # Use median of last 5 bar ranges to avoid outliers
            sample = sorted(recent_ranges[-5:])
            median_range = sample[len(sample) // 2]
            spread_from_range = median_range * 0.10
        else:
            spread_from_range = 0.0
        spread_from_pct = price * 0.0005   # 5 bps minimum (NSE liquid stock floor)
        return max(spread_from_range, spread_from_pct)

    def _compute_stop_floor(
        self,
        price: float,
        atr: float,
        recent_ranges: list[float],
    ) -> ScalpStopResult:
        """Compute stop distance using all four floors, take the maximum.

        Floors:
          1. ATR floor:    0.5 × ATR(14)
          2. Spread floor: min_spread_multiple × estimated_spread
          3. Tick floor:   min_tick_multiple × tick_size (₹0.05 on NSE)
          4. Pct floor:    min_stop_pct × entry_price

        The binding floor (largest) becomes the stop distance.
        TP = max(rr_ratio × stop_dist, min_target_pct × price).
        """
        spread_est = self._estimate_spread(price, recent_ranges)

        atr_dist    = self._atr_stop_mult * atr
        spread_dist = self._min_spread_mult * spread_est
        tick_dist   = self._min_tick_mult * _TICK_SIZE_NSE
        pct_dist    = self._min_stop_pct * price

        candidates = [
            (atr_dist,    "ATR"),
            (spread_dist, "SPREAD_FLOOR"),
            (tick_dist,   "TICK_FLOOR"),
            (pct_dist,    "PCT_FLOOR"),
        ]
        stop_distance, stop_source = max(candidates, key=lambda x: x[0])

        rr_tp  = stop_distance * self._rr
        pct_tp = self._min_target_pct * price
        if rr_tp >= pct_tp:
            tp_distance, tp_source = rr_tp, "RR"
        else:
            tp_distance, tp_source = pct_tp, "PCT_FLOOR"

        gross_edge_pct = (tp_distance / price) * 100.0
        net_edge_pct   = gross_edge_pct - _COST_ROUNDTRIP_PCT

        return ScalpStopResult(
            stop_distance=stop_distance,
            stop_distance_source=stop_source,
            tp_distance=tp_distance,
            tp_distance_source=tp_source,
            atr_stop_distance=atr_dist,
            spread_floor_distance=spread_dist,
            tick_floor_distance=tick_dist,
            pct_floor_distance=pct_dist,
            spread_estimate=spread_est,
            net_expected_edge_pct=net_edge_pct,
        )

    def _check_viability(
        self,
        price: float,
        result: ScalpStopResult,
    ) -> tuple[bool, str]:
        """Return (is_viable, reject_reason).

        Checks (in order):
          1. Estimated spread too wide (> max_spread_pct).
          2. TP too small relative to spread (< 2 × spread).
          3. Net expected edge too small (< min_net_edge_pct after costs).
        """
        spread_pct = (result.spread_estimate / price) * 100.0

        if spread_pct > self._max_spread_pct * 100.0:
            return False, (
                f"spread_too_wide: {spread_pct:.3f}% > {self._max_spread_pct * 100:.2f}%"
            )

        if result.tp_distance < 2.0 * result.spread_estimate:
            return False, (
                f"tp_inside_spread: tp={result.tp_distance:.4f} < 2×spread={2*result.spread_estimate:.4f}"
            )

        if result.net_expected_edge_pct < self._min_net_edge_pct:
            return False, (
                f"net_edge_too_small: {result.net_expected_edge_pct:.3f}% < {self._min_net_edge_pct}%"
            )

        return True, ""

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _ensure_buffers(self, symbol: str) -> None:
        if symbol not in self._closes:
            self._closes[symbol]     = deque(maxlen=self._buf_size)
            self._highs[symbol]      = deque(maxlen=self._buf_size)
            self._lows[symbol]       = deque(maxlen=self._buf_size)
            self._volumes[symbol]    = deque(maxlen=20)
            self._bar_ranges[symbol] = deque(maxlen=10)
            self._prev_fast_above[symbol] = None
            self._signal_count[symbol]    = {"BUY": 0, "SELL": 0}

    def _build_signal(
        self,
        bar: Bar,
        direction: Direction,
        fast_ema: float,
        slow_ema: float,
        atr: float,
        confidence: float,
        recent_ranges: list[float],
    ) -> Optional[Signal]:
        price = bar.close

        # Compute hardened stop floors
        floor = self._compute_stop_floor(price, atr, recent_ranges)

        # Viability filter
        viable, reject_reason = self._check_viability(price, floor)
        if not viable:
            if "spread_too_wide" in reject_reason:
                self._rejected_spread_wide += 1
            elif "tp_inside_spread" in reject_reason:
                self._rejected_tp_inside_spread += 1
            else:
                self._rejected_net_edge += 1
            logger.info(
                "scalp_1m.rejected_viability",
                symbol=bar.symbol,
                direction=direction.value,
                price=price,
                reject_reason=reject_reason,
                stop_distance=round(floor.stop_distance, 4),
                stop_source=floor.stop_distance_source,
                tp_distance=round(floor.tp_distance, 4),
                net_edge_pct=round(floor.net_expected_edge_pct, 3),
                atr=round(atr, 4),
            )
            return None

        stop_loss   = (price - floor.stop_distance) if direction == Direction.BUY else (price + floor.stop_distance)
        take_profit = (price + floor.tp_distance)   if direction == Direction.BUY else (price - floor.tp_distance)

        sizing = size_position(price, floor.stop_distance, self._nav, confidence)
        if sizing.qty == 0 or sizing.rejected_if_exceeds_cap:
            return None

        logger.info(
            "scalp_1m.signal",
            symbol=bar.symbol,
            direction=direction.value,
            price=price,
            fast_ema=round(fast_ema, 4),
            slow_ema=round(slow_ema, 4),
            atr=round(atr, 4),
            stop_distance=round(floor.stop_distance, 4),
            stop_source=floor.stop_distance_source,
            tp_distance=round(floor.tp_distance, 4),
            tp_source=floor.tp_distance_source,
            net_edge_pct=round(floor.net_expected_edge_pct, 3),
            confidence=round(confidence, 3),
        )

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
                "fast_ema":               round(fast_ema, 4),
                "slow_ema":               round(slow_ema, 4),
                "ema_spread":             round(abs(fast_ema - slow_ema), 4),
                "atr":                    round(atr, 4),
                "bar_volume":             bar.volume,
                "stop_distance":          round(floor.stop_distance, 4),
                "stop_distance_source":   floor.stop_distance_source,
                "atr_stop_distance":      round(floor.atr_stop_distance, 4),
                "spread_floor_distance":  round(floor.spread_floor_distance, 4),
                "tick_floor_distance":    round(floor.tick_floor_distance, 4),
                "pct_floor_distance":     round(floor.pct_floor_distance, 4),
                "tp_distance":            round(floor.tp_distance, 4),
                "tp_distance_source":     floor.tp_distance_source,
                "spread_estimate":        round(floor.spread_estimate, 4),
                "net_expected_edge_pct":  round(floor.net_expected_edge_pct, 3),
                "rr_ratio":               self._rr,
                "paper_trade":            self._paper,
                "strategy_version":       "scalp_1m_v2",
                **sizing.to_metadata(),
            },
        )

    def reset_daily(self) -> None:
        """Reset daily signal counts. Call at POST_CLOSE."""
        for symbol in self._signal_count:
            self._signal_count[symbol] = {"BUY": 0, "SELL": 0}
        logger.info(
            "scalp_1m.daily_reset",
            strategy=self.name,
            rejected_spread_wide=self._rejected_spread_wide,
            rejected_net_edge=self._rejected_net_edge,
            rejected_stale=self._rejected_stale,
            rejected_spread_unavailable=self._rejected_spread_unavailable,
            rejected_tp_inside_spread=self._rejected_tp_inside_spread,
        )
        self._rejected_spread_wide = 0
        self._rejected_net_edge = 0
        self._rejected_stale = 0
        self._rejected_spread_unavailable = 0
        self._rejected_tp_inside_spread = 0
