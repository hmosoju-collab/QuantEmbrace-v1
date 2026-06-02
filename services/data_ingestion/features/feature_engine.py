"""
FeatureEngine — pure, stateless technical feature computation.

Takes a chronological list of OHLCV candles (same symbol and interval) and
returns a ``FeatureSet``.  No I/O, no side-effects.  Thread-safe.

Features computed
-----------------
  RSI(14)          — Wilder's smoothed RSI (Relative Strength Index)
  EMA(9)           — Exponential Moving Average, 9 periods
  EMA(21)          — Exponential Moving Average, 21 periods
  VWAP             — Rolling Volume-Weighted Average Price over the window
  ATR(14)          — Wilder's Average True Range
  ADX(14)          — Wilder's Average Directional Index
  MACD(12,26,9)    — MACD line, signal line, histogram
  volume_ratio     — last-candle volume / adv_20d

Lookback minimums (features return None below threshold)
---------------------------------------------------------
  EMA(9):           9 candles
  EMA(21):         21 candles
  RSI(14):         15 candles  (14 delta values → 14 smoothed periods)
  ATR(14):         15 candles  (need previous close for first TR)
  ADX(14):         28 candles  (14 smoothed DM/TR values → 14 DX values)
  MACD(12,26,9):   35 candles  (EMA26 + 9-period signal)
  VWAP:             1 candle
  volume_ratio:    adv_20d must be non-None and > 0

All arithmetic uses Python floats.  No external numeric libraries.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from shared.models.feature_set import FeatureSet

# ── Lookback constants ────────────────────────────────────────────────────────

_EMA9_MIN:   int = 9
_EMA21_MIN:  int = 21
_RSI_MIN:    int = 15    # 14 deltas require 15 candles
_ATR_MIN:    int = 15    # prev_close TR requires 2 candles; 14 smoothed → 15 total
_ADX_MIN:    int = 28    # 14 smoothed DM/TR → 14 DX values → first ADX
_MACD_MIN:   int = 35    # EMA26 seeds at candle 26; 9-period signal seeds at candle 34

_RSI_PERIOD:  int = 14
_ATR_PERIOD:  int = 14
_ADX_PERIOD:  int = 14
_EMA_FAST:    int = 12
_EMA_SLOW:    int = 26
_MACD_SIGNAL: int = 9


class FeatureEngine:
    """
    Stateless technical feature engine.

    Usage::

        engine = FeatureEngine()
        feature_set = engine.compute(candles, adv_20d=1_500_000.0)

    Args:
        candles:  Chronological list of candle objects.  Each object must
                  expose: ``.open``, ``.high``, ``.low``, ``.close``,
                  ``.volume`` (numeric), ``.dt`` (datetime), ``.market`` (str),
                  ``.instrument`` (str), ``.interval`` (str).
        adv_20d:  20-day average daily volume for the instrument, in shares.
                  Pass ``None`` if unavailable — ``volume_ratio`` will be None.
    """

    # No state; all methods are pure functions.

    def compute(
        self,
        candles: list[Any],
        adv_20d: Optional[float] = None,
    ) -> FeatureSet:
        """
        Compute all features for the given candle window.

        Args:
            candles:  Chronological list of candle objects (oldest first).
                      Must all be for the same symbol and interval.
                      At minimum one candle must be present.
            adv_20d:  20-day average daily volume (shares).  Used for
                      ``volume_ratio``.  Pass ``None`` if not available.

        Returns:
            FeatureSet with all features populated (or None where insufficient
            history).

        Raises:
            ValueError: If ``candles`` is empty.
        """
        if not candles:
            raise ValueError("FeatureEngine.compute() requires at least one candle")

        n         = len(candles)
        last      = candles[-1]
        closes    = [c.close for c in candles]
        highs     = [c.high  for c in candles]
        lows      = [c.low   for c in candles]
        volumes   = [c.volume for c in candles]
        now_utc   = datetime.now(timezone.utc)

        # Normalize candle_time to UTC-aware datetime
        candle_time = last.dt
        if candle_time is not None and candle_time.tzinfo is None:
            candle_time = candle_time.replace(tzinfo=timezone.utc)

        # ── Compute each feature ──────────────────────────────────────────

        vwap         = _compute_vwap(candles)                   if n >= 1   else None
        ema_9        = _compute_ema(closes, _EMA9_MIN)          if n >= _EMA9_MIN   else None
        ema_21       = _compute_ema(closes, _EMA21_MIN)         if n >= _EMA21_MIN  else None
        rsi_14       = _compute_rsi(closes, _RSI_PERIOD)        if n >= _RSI_MIN    else None
        atr_14       = _compute_atr(highs, lows, closes, _ATR_PERIOD) if n >= _ATR_MIN else None
        adx_14       = _compute_adx(highs, lows, closes, _ADX_PERIOD) if n >= _ADX_MIN else None

        macd_line: Optional[float] = None
        macd_sig:  Optional[float] = None
        macd_hist: Optional[float] = None
        if n >= _MACD_MIN:
            macd_line, macd_sig, macd_hist = _compute_macd(
                closes, _EMA_FAST, _EMA_SLOW, _MACD_SIGNAL
            )

        vol_ratio: Optional[float] = None
        if adv_20d is not None and adv_20d > 0:
            vol_ratio = float(volumes[-1]) / adv_20d

        # Determine interval in canonical form (convert Kite strings if needed)
        interval = _canonical_interval(last.interval)

        return FeatureSet(
            symbol=last.instrument,
            market=last.market,
            interval=interval,
            candle_time=candle_time,
            candle_count=n,
            computed_at=now_utc,
            rsi_14=rsi_14,
            ema_9=ema_9,
            ema_21=ema_21,
            vwap=vwap,
            atr_14=atr_14,
            adx_14=adx_14,
            macd=macd_line,
            macd_signal=macd_sig,
            macd_hist=macd_hist,
            volume_ratio=vol_ratio,
        )


# ── Pure computation helpers ──────────────────────────────────────────────────

def _canonical_interval(kite_interval: str) -> str:
    """Convert Kite interval strings to canonical short form."""
    _MAP = {
        "minute":    "1m",
        "3minute":   "3m",
        "5minute":   "5m",
        "10minute":  "10m",
        "15minute":  "15m",
        "30minute":  "30m",
        "60minute":  "60m",
        "day":       "1d",
    }
    return _MAP.get(kite_interval, kite_interval)


def _compute_vwap(candles: list[Any]) -> Optional[float]:
    """Rolling VWAP over the provided candle window."""
    cumulative_tp_vol = 0.0
    cumulative_vol    = 0.0
    for c in candles:
        tp = (c.high + c.low + c.close) / 3.0
        vol = float(c.volume)
        cumulative_tp_vol += tp * vol
        cumulative_vol    += vol
    if cumulative_vol == 0.0:
        return None
    return cumulative_tp_vol / cumulative_vol


def _sma(values: list[float], n: int) -> float:
    """Simple moving average of the last n values."""
    return sum(values[-n:]) / n


def _ema_series(values: list[float], period: int) -> list[float]:
    """
    Compute EMA series.  Seed = SMA of first ``period`` values.
    Returns a list of the same length as ``values`` with None-equivalent
    (math.nan) for positions before the EMA seeds.

    The returned list has len == len(values).  The first (period-1) entries
    are math.nan; entry at index (period-1) is the seed SMA; subsequent
    entries are proper EMA values.
    """
    n     = len(values)
    alpha = 2.0 / (period + 1)
    result = [math.nan] * n

    if n < period:
        return result

    # Seed with SMA
    seed_sma = sum(values[:period]) / period
    result[period - 1] = seed_sma

    # Propagate EMA
    for i in range(period, n):
        result[i] = values[i] * alpha + result[i - 1] * (1.0 - alpha)

    return result


def _compute_ema(closes: list[float], period: int) -> Optional[float]:
    """Return the last EMA value for the given period."""
    series = _ema_series(closes, period)
    last   = series[-1]
    return last if not math.isnan(last) else None


def _compute_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """
    Wilder's smoothed RSI.

    Algorithm:
      1. Compute price deltas (gains / losses separated).
      2. Seed avg_gain / avg_loss as SMA of first ``period`` deltas.
      3. Apply Wilder's smoothing: avg_gain = (prev * (period-1) + gain) / period.
      4. RSI = 100 - 100 / (1 + avg_gain / avg_loss).
    """
    n = len(closes)
    if n < period + 1:   # need period deltas → period+1 closes
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    # Seed
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Smooth over remaining deltas
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0
    rs  = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _true_range(high: float, low: float, prev_close: float) -> float:
    """Single-period True Range."""
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def _compute_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> Optional[float]:
    """
    Wilder's Average True Range.

    Requires len(closes) >= period + 1 (for previous close).
    Seed = SMA of first ``period`` true ranges.
    """
    n = len(closes)
    if n < period + 1:
        return None

    trs = [_true_range(highs[i], lows[i], closes[i - 1]) for i in range(1, n)]
    # Seed
    atr = sum(trs[:period]) / period
    # Wilder's smoothing
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr


def _wilder_smooth(values: list[float], period: int) -> list[float]:
    """
    Apply Wilder's smoothing (equivalent to EMA with alpha = 1/period).

    Seed = sum of first ``period`` values (Wilder uses cumulative sum as seed).
    Returns list same length as values, with nan before the seed.
    """
    n      = len(values)
    result = [math.nan] * n
    if n < period:
        return result

    seed = sum(values[:period])
    result[period - 1] = seed

    for i in range(period, n):
        result[i] = result[i - 1] - result[i - 1] / period + values[i]

    return result


def _compute_adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> Optional[float]:
    """
    Wilder's Average Directional Index.

    Algorithm:
      1. Compute +DM, -DM, TR for each bar (needs prev high/low/close).
      2. Wilder-smooth +DM, -DM, TR over ``period`` bars.
      3. +DI = 100 * smoothed_+DM / smoothed_TR
      4. -DI = 100 * smoothed_-DM / smoothed_TR
      5. DX  = 100 * |+DI - -DI| / (+DI + -DI)
      6. ADX = Wilder-smooth DX over ``period`` bars.

    Minimum candles: 2 * period = 28 (for period=14).
    """
    n = len(closes)
    min_candles = 2 * period
    if n < min_candles:
        return None

    # Step 1: raw DM and TR vectors (length = n-1)
    plus_dms  = []
    minus_dms = []
    trs       = []
    for i in range(1, n):
        up   = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm  = up   if up > down and up > 0   else 0.0
        minus_dm = down if down > up and down > 0 else 0.0
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)
        trs.append(_true_range(highs[i], lows[i], closes[i - 1]))

    # Step 2: Wilder-smooth over ``period``
    s_plus   = _wilder_smooth(plus_dms,  period)
    s_minus  = _wilder_smooth(minus_dms, period)
    s_tr     = _wilder_smooth(trs,       period)

    # Step 3-5: compute DX where smoothed values are valid
    dx_list = []
    for i in range(period - 1, len(trs)):
        sp = s_plus[i]
        sm = s_minus[i]
        st = s_tr[i]
        if math.isnan(sp) or math.isnan(sm) or math.isnan(st) or st == 0.0:
            continue
        plus_di  = 100.0 * sp / st
        minus_di = 100.0 * sm / st
        denom    = plus_di + minus_di
        if denom == 0.0:
            dx_list.append(0.0)
        else:
            dx_list.append(100.0 * abs(plus_di - minus_di) / denom)

    if len(dx_list) < period:
        return None

    # Step 6: ADX = Wilder-smooth of DX
    adx_series = _wilder_smooth(dx_list, period)
    # Return the latest valid ADX
    for v in reversed(adx_series):
        if not math.isnan(v):
            return v / period   # Wilder seed is cumulative sum; divide to get average
    return None


def _compute_macd(
    closes: list[float],
    fast:   int = 12,
    slow:   int = 26,
    signal: int = 9,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    MACD line, signal line, and histogram.

    Returns (macd_line, signal_line, histogram) or (None, None, None) if
    insufficient history.
    """
    n = len(closes)
    # Minimum: EMA(slow) seeds at candle `slow`, yielding 1 MACD value;
    # signal EMA(signal) needs `signal` MACD values → slow + signal - 1 candles
    # minimum. Design spec uses slow + signal = 35 (26+9) to be conservative and
    # ensure signal EMA has a full seed period before emitting values.
    if n < slow + signal:
        return None, None, None

    ema_fast_series = _ema_series(closes, fast)
    ema_slow_series = _ema_series(closes, slow)

    # Compute MACD line where both EMAs are valid (from index slow-1 onward)
    macd_values: list[float] = []
    for i in range(slow - 1, n):
        ef = ema_fast_series[i]
        es = ema_slow_series[i]
        if math.isnan(ef) or math.isnan(es):
            continue
        macd_values.append(ef - es)

    if len(macd_values) < signal:
        return None, None, None

    # Signal line = EMA(signal) of MACD line values
    sig_series = _ema_series(macd_values, signal)
    last_macd  = macd_values[-1]
    last_sig   = sig_series[-1]

    if math.isnan(last_sig):
        return None, None, None

    return last_macd, last_sig, last_macd - last_sig
