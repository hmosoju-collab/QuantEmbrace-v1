"""
FeatureSet — pre-computed technical feature snapshot for one symbol/interval.

Written by ``FeatureEngine.compute()`` and persisted by ``FeatureWriter`` to
DynamoDB (online store, SK=LATEST + SK=CANDLE#{ts}) and S3 parquet (offline
archive via ``FeatureArchiver`` at POST_CLOSE).

Read by ``FeatureReader`` in strategy_engine, risk_engine, and ai_engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class FeatureSet:
    """
    Immutable snapshot of pre-computed technical features for a single
    symbol/interval at a specific candle close.

    All floating-point features are ``Optional`` — ``None`` means insufficient
    candle history exists to compute that feature at this candle.  Consumers
    **must** handle ``None`` gracefully (treat as unavailable, degrade safely,
    never raise on a ``None`` feature).

    Lookback requirements (features return None below these thresholds):

    +--------------+-------------------+
    | Feature      | Minimum candles   |
    +==============+===================+
    | VWAP         | 1                 |
    | EMA(9)       | 9                 |
    | EMA(21)      | 21                |
    | RSI(14)      | 15                |
    | ATR(14)      | 15                |
    | ADX(14)      | 28                |
    | MACD(12,26,9)| 35                |
    | volume_ratio | adv_20d provided  |
    +--------------+-------------------+

    Schema version: 1 (stored as ``schema_version`` in every DynamoDB item).
    ``FeatureReader`` rejects items with a mismatched schema_version.
    """

    symbol:        str
    market:        str
    interval:      str          # "1m", "5m", "15m" (canonical form, not Kite "minute")
    candle_time:   datetime     # open time of the latest candle (UTC, timezone-aware)
    candle_count:  int          # number of candles used for this computation
    computed_at:   datetime     # UTC timestamp when FeatureEngine.compute() ran

    # ── Technical features ─────────────────────────────────────────────────
    rsi_14:       Optional[float]   # Relative Strength Index (14), 0.0–100.0
    ema_9:        Optional[float]   # Exponential Moving Average (9)
    ema_21:       Optional[float]   # Exponential Moving Average (21)
    vwap:         Optional[float]   # Volume-Weighted Average Price (rolling window)
    atr_14:       Optional[float]   # Average True Range (14), same units as price
    adx_14:       Optional[float]   # Average Directional Index (14), 0.0–100.0
    macd:         Optional[float]   # MACD line: EMA(12) − EMA(26)
    macd_signal:  Optional[float]   # Signal line: EMA(9) of MACD
    macd_hist:    Optional[float]   # Histogram: macd − macd_signal
    volume_ratio: Optional[float]   # candle_volume / adv_20d; None if adv_20d absent

    # ── Schema version ─────────────────────────────────────────────────────
    schema_version: int = 1
