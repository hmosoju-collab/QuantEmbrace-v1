"""
Shared math helpers for Gate 4 strategies.

All functions are pure (no side effects), operate on plain Python lists/floats,
and are designed for easy unit testing without any broker or infrastructure deps.

Available:
    sma(values, window)              Simple moving average
    ema(values, period)              Exponential moving average (returns last value)
    ema_series(values, period)       EMA as a full list (for crossover detection)
    atr_wilder(highs, lows, closes, period)   ATR via Wilder smoothing
    vwap(highs, lows, closes, volumes)         Intraday VWAP from bar data
    vwap_bands(highs, lows, closes, volumes, n_std)  VWAP ± N standard deviations
    adx(highs, lows, closes, period)           Average Directional Index
    typical_price(high, low, close)            (H+L+C)/3
    rolling_stddev(values, window)             Population standard deviation
"""

from __future__ import annotations

import math
from typing import Optional


# ── Moving averages ────────────────────────────────────────────────────────────

def sma(values: list[float], window: int) -> float:
    """Simple moving average over the last *window* elements."""
    if len(values) < window:
        return float("nan")
    return sum(values[-window:]) / window


def ema(values: list[float], period: int) -> float:
    """
    Exponential moving average — returns the last EMA value only.

    Uses standard EMA smoothing factor k = 2 / (period + 1).
    Seeds from the first *period* bars as a simple average.
    """
    series = ema_series(values, period)
    return series[-1] if series else float("nan")


def ema_series(values: list[float], period: int) -> list[float]:
    """
    Compute full EMA series for crossover detection.

    Returns a list the same length as *values* (NaN-padded for the first period-1
    elements before the SMA seed is available).
    """
    if len(values) < period:
        return [float("nan")] * len(values)

    k       = 2.0 / (period + 1)
    result  = [float("nan")] * (period - 1)
    seed    = sum(values[:period]) / period
    result.append(seed)
    current = seed

    for v in values[period:]:
        current = v * k + current * (1 - k)
        result.append(current)

    return result


# ── True Range + ATR ───────────────────────────────────────────────────────────

def true_range(high: float, low: float, prev_close: float) -> float:
    """Single-bar True Range = max(H-L, |H-prev_C|, |L-prev_C|)."""
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr_wilder(
    highs:   list[float],
    lows:    list[float],
    closes:  list[float],
    period:  int = 14,
) -> float:
    """
    Average True Range using Wilder's EMA smoothing.

    Seeds from the arithmetic mean of the first *period* true ranges,
    then applies Wilder's smoothing: ATR_t = ((period-1)×ATR_{t-1} + TR_t) / period.

    Returns NaN if there are insufficient bars.
    """
    n = len(closes)
    if n < period + 1:
        return float("nan")

    trs: list[float] = []
    for i in range(1, n):
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))

    if len(trs) < period:
        return float("nan")

    # Seed
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = ((period - 1) * atr_val + tr) / period

    return atr_val


# ── VWAP ──────────────────────────────────────────────────────────────────────

def typical_price(high: float, low: float, close: float) -> float:
    """Typical price = (H + L + C) / 3."""
    return (high + low + close) / 3.0


def vwap(
    highs:   list[float],
    lows:    list[float],
    closes:  list[float],
    volumes: list[int],
) -> float:
    """
    Volume-Weighted Average Price from OHLCV bars.

    VWAP = Σ(typical_price × volume) / Σ(volume)

    Uses bars in the provided order — caller is responsible for ensuring
    these are the current intraday bars (reset at session open).
    """
    total_pv = 0.0
    total_v  = 0.0
    for h, l, c, v in zip(highs, lows, closes, volumes):
        tp       = typical_price(h, l, c)
        total_pv += tp * v
        total_v  += v

    return total_pv / total_v if total_v > 0 else float("nan")


def vwap_bands(
    highs:   list[float],
    lows:    list[float],
    closes:  list[float],
    volumes: list[int],
    n_std:   float = 2.0,
) -> tuple[float, float, float]:
    """
    VWAP ± N standard deviations.

    Returns (vwap_value, upper_band, lower_band).
    Standard deviation is computed over the typical-price deviations from VWAP,
    weighted by volume (volume-weighted standard deviation).
    """
    vwap_val = vwap(highs, lows, closes, volumes)
    if math.isnan(vwap_val):
        return float("nan"), float("nan"), float("nan")

    total_v = sum(volumes)
    if total_v == 0:
        return vwap_val, vwap_val, vwap_val

    # Volume-weighted variance
    variance = 0.0
    for h, l, c, v in zip(highs, lows, closes, volumes):
        tp = typical_price(h, l, c)
        variance += v * (tp - vwap_val) ** 2

    std = math.sqrt(variance / total_v)
    return vwap_val, vwap_val + n_std * std, vwap_val - n_std * std


# ── Standard deviation ────────────────────────────────────────────────────────

def rolling_stddev(values: list[float], window: int) -> float:
    """Population standard deviation of the last *window* elements."""
    if len(values) < window:
        return float("nan")
    subset = values[-window:]
    mean   = sum(subset) / window
    var    = sum((x - mean) ** 2 for x in subset) / window
    return math.sqrt(var)


# ── ADX (Average Directional Index) ───────────────────────────────────────────

def adx(
    highs:   list[float],
    lows:    list[float],
    closes:  list[float],
    period:  int = 14,
) -> float:
    """
    Average Directional Index (Wilder smoothing).

    Returns the ADX value (0–100 scale) for the most recent bar.
    ADX > 25 is conventionally considered a trending market.
    Returns NaN if insufficient data.
    """
    n = len(closes)
    if n < period * 2:
        return float("nan")

    plus_dm_list:  list[float] = []
    minus_dm_list: list[float] = []
    tr_list:       list[float] = []

    for i in range(1, n):
        up_move   = highs[i]  - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        plus_dm  = up_move   if (up_move   > down_move and up_move   > 0) else 0.0
        minus_dm = down_move if (down_move > up_move   and down_move > 0) else 0.0
        tr_val   = true_range(highs[i], lows[i], closes[i - 1])

        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
        tr_list.append(tr_val)

    if len(tr_list) < period:
        return float("nan")

    # Wilder-smooth TR, +DM, -DM
    def _wilder_smooth(data: list[float]) -> list[float]:
        smoothed = [sum(data[:period])]
        for v in data[period:]:
            smoothed.append(smoothed[-1] - smoothed[-1] / period + v)
        return smoothed

    tr_smooth     = _wilder_smooth(tr_list)
    plus_smooth   = _wilder_smooth(plus_dm_list)
    minus_smooth  = _wilder_smooth(minus_dm_list)

    # +DI / -DI series
    dx_list: list[float] = []
    for atr_s, p_s, m_s in zip(tr_smooth, plus_smooth, minus_smooth):
        if atr_s == 0:
            dx_list.append(0.0)
            continue
        plus_di  = 100 * p_s / atr_s
        minus_di = 100 * m_s / atr_s
        denom    = plus_di + minus_di
        dx       = 100 * abs(plus_di - minus_di) / denom if denom > 0 else 0.0
        dx_list.append(dx)

    if len(dx_list) < period:
        return float("nan")

    # ADX = Wilder EMA of DX over *period*
    adx_val = sum(dx_list[:period]) / period
    for dx_v in dx_list[period:]:
        adx_val = ((period - 1) * adx_val + dx_v) / period

    return adx_val
