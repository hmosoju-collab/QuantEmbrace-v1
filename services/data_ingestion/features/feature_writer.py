"""
FeatureWriter — async DynamoDB writer for pre-computed feature sets.

Writes two items per ``FeatureSet``:

    LATEST row  — SK = "LATEST"
                   Enables fast ``get_item`` reads in ``FeatureReader``.
                   TTL = 24 hours (expires before next trading day).

    CANDLE row  — SK = "CANDLE#{candle_time_iso}"
                   Intraday audit trail; read by ``FeatureArchiver`` at
                   POST_CLOSE to build the daily S3 parquet file.
                   TTL = 7 days (gives the archiver recovery room after
                   a crash restart).

Failure contract:
    Any DynamoDB error is logged and suppressed.  Feature write failures
    NEVER propagate to the candle pipeline.  If both writes fail, the
    candle stream continues normally; the symbol will be absent from the
    feature store for that interval.

Threading:
    ``asyncio.to_thread`` is used for the blocking DynamoDB ``put_item``
    calls so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from shared.logging.logger import get_logger
from shared.models.feature_set import FeatureSet

logger = get_logger(__name__, service_name="data_ingestion")

# TTL for LATEST row: 24 hours
_LATEST_TTL_SECONDS: int = 86_400
# TTL for CANDLE rows: 7 days (recovery window for EOD archiver)
_CANDLE_TTL_SECONDS: int = 7 * 86_400


class FeatureWriter:
    """
    Writes FeatureSet objects to the DynamoDB features table.

    Args:
        dynamo_client:  boto3 DynamoDB client (synchronous, wrapped with
                        ``asyncio.to_thread``).
        features_table: DynamoDB table name.  Typically
                        ``settings.aws.dynamodb_table_features``.
    """

    def __init__(self, dynamo_client: Any, features_table: str) -> None:
        self._dynamo  = dynamo_client
        self._table   = features_table

    async def write(self, feature_set: FeatureSet) -> None:
        """
        Write feature_set to DynamoDB — both LATEST and CANDLE rows.

        Failures are caught, logged, and suppressed.  Never raises.
        """
        pk = (
            f"FEATURE#{feature_set.market}"
            f"#{feature_set.symbol}"
            f"#{feature_set.interval}"
        )
        base_item = _build_base_item(feature_set)
        now_epoch = int(time.time())

        # ── LATEST row ────────────────────────────────────────────────────
        latest_item: dict[str, Any] = {
            **base_item,
            "SK":  {"S": "LATEST"},
            "ttl": {"N": str(now_epoch + _LATEST_TTL_SECONDS)},
        }

        # ── CANDLE row ────────────────────────────────────────────────────
        candle_sk   = f"CANDLE#{feature_set.candle_time.isoformat()}"
        candle_item: dict[str, Any] = {
            **base_item,
            "SK":  {"S": candle_sk},
            "ttl": {"N": str(now_epoch + _CANDLE_TTL_SECONDS)},
        }

        await asyncio.gather(
            self._put_item(pk, "LATEST", latest_item),
            self._put_item(pk, candle_sk, candle_item),
        )

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _put_item(self, pk: str, sk: str, item: dict[str, Any]) -> None:
        """
        Execute a single ``put_item``.  Logs and suppresses any exception.
        """
        full_item = {"PK": {"S": pk}, **item}
        try:
            await asyncio.to_thread(
                self._dynamo.put_item,
                TableName=self._table,
                Item=full_item,
            )
        except Exception:
            logger.warning(
                "feature_writer.dynamo_write_failed",
                pk=pk,
                sk=sk,
                table=self._table,
            )


def _build_base_item(fs: FeatureSet) -> dict[str, Any]:
    """
    Build shared DynamoDB attribute dict (without PK, SK, ttl).

    Only non-None float features are included — no ``"N": "None"`` noise.
    """
    item: dict[str, Any] = {
        "symbol":         {"S": fs.symbol},
        "market":         {"S": fs.market},
        "interval":       {"S": fs.interval},
        "candle_time":    {"S": fs.candle_time.isoformat()},
        "candle_count":   {"N": str(fs.candle_count)},
        "computed_at":    {"S": fs.computed_at.isoformat()},
        "schema_version": {"N": str(fs.schema_version)},
    }

    # Optional numeric features — omit if None
    _opt = {
        "rsi_14":       fs.rsi_14,
        "ema_9":        fs.ema_9,
        "ema_21":       fs.ema_21,
        "vwap":         fs.vwap,
        "atr_14":       fs.atr_14,
        "adx_14":       fs.adx_14,
        "macd":         fs.macd,
        "macd_signal":  fs.macd_signal,
        "macd_hist":    fs.macd_hist,
        "volume_ratio": fs.volume_ratio,
    }
    for attr_name, value in _opt.items():
        if value is not None:
            item[attr_name] = {"N": str(value)}

    return item
