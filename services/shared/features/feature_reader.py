"""
FeatureReader — shared DynamoDB client for reading the latest FeatureSet.

Importable by strategy_engine, risk_engine, and ai_engine.  All callers
receive the same ``None``-on-degradation contract so consuming code is
uniform: ``if features is None: # degrade gracefully``.

Staleness threshold (interval-aware)
-------------------------------------
A feature is considered stale and returns ``None`` if:

    age > max(5 minutes,  2 * interval_minutes + 1 minute)

Thresholds by interval:
    1m  →  max(5, 2*1+1)  =  5 min
    5m  →  max(5, 2*5+1)  = 11 min
   15m  →  max(5, 2*15+1) = 31 min

Rationale: A 1m feature is valid for 5 minutes because at 1m cadence the
feature should be ≤2 min stale under normal operation.  A 15m feature is
valid for 31 minutes because the next candle only arrives 15 minutes later.

Schema version guard
---------------------
Items with ``schema_version != 1`` are treated as None.  This prevents
silently consuming mis-parsed data after a schema change.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from shared.logging.logger import get_logger
from shared.models.feature_set import FeatureSet

logger = get_logger(__name__, service_name="shared")

_SCHEMA_VERSION: int = 1


class FeatureReader:
    """
    Read-only DynamoDB client for the features table.

    Args:
        dynamo_client:  boto3 DynamoDB client (synchronous, wrapped with
                        ``asyncio.to_thread``).
        features_table: DynamoDB table name.  Typically
                        ``settings.aws.dynamodb_table_features``.
    """

    def __init__(self, dynamo_client: Any, features_table: str) -> None:
        self._dynamo = dynamo_client
        self._table  = features_table

    async def get_latest(
        self,
        market:   str,
        symbol:   str,
        interval: str,
    ) -> Optional[FeatureSet]:
        """
        Return the latest FeatureSet for (market, symbol, interval).

        Returns ``None`` if:
          - Item not found in DynamoDB.
          - Item is stale for the given interval.
          - ``schema_version`` does not match the current version.
          - Any DynamoDB error occurs.
          - ``computed_at`` is missing or unparseable.

        Args:
            market:   Market identifier, e.g. ``"NSE"`` or ``"US"``.
            symbol:   Instrument symbol, e.g. ``"RELIANCE"``.
            interval: Canonical interval string, e.g. ``"1m"``, ``"5m"``,
                      ``"15m"``.

        Returns:
            ``FeatureSet`` or ``None``.
        """
        pk = f"FEATURE#{market}#{symbol}#{interval}"
        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._table,
                Key={"PK": {"S": pk}, "SK": {"S": "LATEST"}},
                ConsistentRead=False,
            )
        except Exception:
            logger.warning(
                "feature_reader.dynamo_error",
                market=market,
                symbol=symbol,
                interval=interval,
            )
            return None

        item = response.get("Item")
        if not item:
            return None

        return _parse_item(item, interval)


# ── Item parsing ──────────────────────────────────────────────────────────────

def _parse_item(item: dict[str, Any], interval: str) -> Optional[FeatureSet]:
    """
    Parse a raw DynamoDB item (resource API format) into a ``FeatureSet``.

    Returns ``None`` if the item is stale, has the wrong schema version, or
    is structurally malformed.
    """
    try:
        # Schema version check
        sv = int(item.get("schema_version", {}).get("N", "0"))
        if sv != _SCHEMA_VERSION:
            logger.warning(
                "feature_reader.schema_version_mismatch",
                got=sv,
                expected=_SCHEMA_VERSION,
            )
            return None

        computed_at_str = item.get("computed_at", {}).get("S")
        if not computed_at_str:
            return None
        computed_at = datetime.fromisoformat(computed_at_str)
        if computed_at.tzinfo is None:
            computed_at = computed_at.replace(tzinfo=timezone.utc)

        # Staleness check
        age_seconds = (datetime.now(timezone.utc) - computed_at).total_seconds()
        threshold   = _staleness_threshold_seconds(interval)
        if age_seconds > threshold:
            return None

        candle_time_str = item.get("candle_time", {}).get("S", "")
        candle_time     = datetime.fromisoformat(candle_time_str)
        if candle_time.tzinfo is None:
            candle_time = candle_time.replace(tzinfo=timezone.utc)

        def _float(key: str) -> Optional[float]:
            raw = item.get(key, {}).get("N")
            return float(raw) if raw is not None else None

        return FeatureSet(
            symbol=item.get("symbol", {}).get("S", ""),
            market=item.get("market", {}).get("S", ""),
            interval=item.get("interval", {}).get("S", interval),
            candle_time=candle_time,
            candle_count=int(item.get("candle_count", {}).get("N", "0")),
            computed_at=computed_at,
            rsi_14=_float("rsi_14"),
            ema_9=_float("ema_9"),
            ema_21=_float("ema_21"),
            vwap=_float("vwap"),
            atr_14=_float("atr_14"),
            adx_14=_float("adx_14"),
            macd=_float("macd"),
            macd_signal=_float("macd_signal"),
            macd_hist=_float("macd_hist"),
            volume_ratio=_float("volume_ratio"),
            schema_version=sv,
        )
    except Exception:
        logger.warning("feature_reader.parse_error", item_keys=list(item.keys()))
        return None


def _staleness_threshold_seconds(interval: str) -> float:
    """
    Return the staleness threshold in seconds for the given interval.

    Formula: max(5 minutes,  2 * interval_minutes + 1 minute)

    Canonical interval examples:
        "1m"  →  max(300, 2*1*60+60)   = 300s  (5 min)
        "5m"  →  max(300, 2*5*60+60)   = 660s  (11 min)
        "15m" →  max(300, 2*15*60+60)  = 1860s (31 min)
    """
    interval_minutes = _parse_interval_minutes(interval)
    formula_seconds  = (2 * interval_minutes + 1) * 60
    return max(300.0, float(formula_seconds))


def _parse_interval_minutes(interval: str) -> int:
    """
    Parse canonical interval string to minutes.

    Handles: "1m", "3m", "5m", "10m", "15m", "30m", "60m", "1d".
    Unknown intervals default to 1 minute (most conservative).
    """
    _MAP = {
        "1m": 1, "3m": 3, "5m": 5, "10m": 10,
        "15m": 15, "30m": 30, "60m": 60, "1d": 1440,
        # Kite canonical strings (defensive)
        "minute": 1, "3minute": 3, "5minute": 5, "15minute": 15,
        "30minute": 30, "60minute": 60, "day": 1440,
    }
    return _MAP.get(interval, 1)
