"""
FeatureArchiver — EOD S3 parquet archive of intraday feature history.

Triggered by the POST_CLOSE phase transition.  For each configured NSE
symbol/interval combination, queries the features DynamoDB table for all
``CANDLE#...`` SK rows written today and writes a consolidated parquet file
to S3.

S3 layout::

    s3://{data_bucket}/features/{market}/{symbol}/{interval}/{date}.parquet

Parquet schema
--------------
Columns: symbol, market, interval, candle_time, rsi_14, ema_9, ema_21,
         vwap, atr_14, adx_14, macd, macd_signal, macd_hist, volume_ratio,
         candle_count, computed_at.

Failure contract:
    A failed S3 write is logged; the archiver continues to the next
    symbol/interval.  Partial archives are acceptable — the next day's
    run is unaffected.  There is no automatic retry: if the service
    restarts before midnight the run simply won't happen for that day.

Dependencies:
    ``pandas`` and ``pyarrow`` must be installed.

NSE-only (Phase 5):
    US symbols have no candle stream yet.  Pass only NSE symbol/interval
    combinations to the ``symbols`` constructor argument.
"""

from __future__ import annotations

import asyncio
import io
from datetime import datetime, timezone
from typing import Any, Optional

from shared.logging.logger import get_logger
from shared.zerodha.market_phase import MarketPhase

logger = get_logger(__name__, service_name="data_ingestion")

_MARKET = "NSE"   # Phase 5 — NSE only


class FeatureArchiver:
    """
    Background task that archives intraday feature rows to S3 at POST_CLOSE.

    Args:
        dynamo_client:  boto3 DynamoDB client.
        s3_client:      boto3 S3 client.
        features_table: DynamoDB features table name.
        data_bucket:    S3 bucket name for market data.
        symbols:        List of NSE symbol strings (e.g. ``["RELIANCE", "INFY"]``).
        intervals:      List of canonical interval strings (default:
                        ``["1m", "5m", "15m"]``).
    """

    def __init__(
        self,
        dynamo_client:  Any,
        s3_client:      Any,
        features_table: str,
        data_bucket:    str,
        symbols:        list[str],
        intervals:      Optional[list[str]] = None,
    ) -> None:
        self._dynamo    = dynamo_client
        self._s3        = s3_client
        self._table     = features_table
        self._bucket    = data_bucket
        self._symbols   = list(symbols)
        self._intervals = intervals or ["1m", "5m", "15m"]

    def on_phase_change(self, phase_name: str) -> None:
        """
        Called by ``MarketPhaseGovernor`` on phase transitions.

        Schedules ``archive_day()`` as a background task when POST_CLOSE
        is reached.
        """
        try:
            phase = MarketPhase(phase_name)
        except ValueError:
            return
        if phase == MarketPhase.POST_CLOSE:
            asyncio.create_task(self._run_archive())

    async def _run_archive(self) -> None:
        """Run ``archive_day()`` with top-level error capture."""
        try:
            await self.archive_day()
        except Exception:
            logger.exception("feature_archiver.archive_day_error")

    async def archive_day(self, date: Optional[datetime] = None) -> None:
        """
        Archive today's feature rows for all configured symbols/intervals.

        Args:
            date: Reference date (UTC).  Defaults to today.  Pass explicitly
                  for testing or historical back-fill.
        """
        try:
            import pandas as pd  # noqa: F401 — ensure pandas available
        except ImportError:
            logger.warning(
                "feature_archiver.pandas_missing",
                message="pandas/pyarrow not installed; skipping archive",
            )
            return

        ref_date    = date or datetime.now(timezone.utc)
        date_prefix = ref_date.strftime("%Y-%m-%dT")
        date_str    = ref_date.strftime("%Y-%m-%d")

        logger.info(
            "feature_archiver.archive_start",
            date=date_str,
            symbols=len(self._symbols),
            intervals=self._intervals,
        )

        tasks = [
            self._archive_one(symbol, interval, date_prefix, date_str)
            for symbol   in self._symbols
            for interval in self._intervals
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        errors = sum(1 for r in results if isinstance(r, Exception))
        logger.info(
            "feature_archiver.archive_complete",
            date=date_str,
            total=len(tasks),
            errors=errors,
        )

    async def _archive_one(
        self,
        symbol:       str,
        interval:     str,
        date_prefix:  str,
        date_str:     str,
    ) -> None:
        """
        Query DynamoDB for all CANDLE# rows for (symbol, interval) today,
        build a DataFrame, and write parquet to S3.
        """
        pk       = f"FEATURE#{_MARKET}#{symbol}#{interval}"
        sk_start = f"CANDLE#{date_prefix}"

        rows = await asyncio.to_thread(
            self._query_candle_rows, pk, sk_start
        )

        if not rows:
            logger.debug(
                "feature_archiver.no_rows",
                symbol=symbol,
                interval=interval,
                date=date_str,
            )
            return

        df = _rows_to_dataframe(rows)
        s3_key = f"features/{_MARKET}/{symbol}/{interval}/{date_str}.parquet"

        parquet_bytes = await asyncio.to_thread(df.to_parquet, None, index=False)
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self._bucket,
            Key=s3_key,
            Body=parquet_bytes,
        )

        logger.info(
            "feature_archiver.wrote_parquet",
            symbol=symbol,
            interval=interval,
            rows=len(df),
            s3_key=s3_key,
        )

    def _query_candle_rows(self, pk: str, sk_prefix: str) -> list[dict[str, Any]]:
        """
        Synchronous DynamoDB Query for today's CANDLE# rows.

        Uses ``KeyConditionExpression`` with ``begins_with`` on the SK.
        Paginates until all items are retrieved.
        """
        from boto3.dynamodb.conditions import Attr, Key  # type: ignore

        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "TableName":                self._table,
            "KeyConditionExpression":   "PK = :pk AND begins_with(SK, :prefix)",
            "ExpressionAttributeValues": {
                ":pk":     {"S": pk},
                ":prefix": {"S": sk_prefix},
            },
        }

        while True:
            response = self._dynamo.query(**kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key

        return items


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rows_to_dataframe(items: list[dict[str, Any]]) -> "Any":
    """Convert raw DynamoDB items to a pandas DataFrame."""
    import pandas as pd

    records = []
    for item in items:
        def _s(key: str) -> Optional[str]:
            return item.get(key, {}).get("S")

        def _f(key: str) -> Optional[float]:
            raw = item.get(key, {}).get("N")
            return float(raw) if raw is not None else None

        records.append({
            "symbol":        _s("symbol"),
            "market":        _s("market"),
            "interval":      _s("interval"),
            "candle_time":   _s("candle_time"),
            "rsi_14":        _f("rsi_14"),
            "ema_9":         _f("ema_9"),
            "ema_21":        _f("ema_21"),
            "vwap":          _f("vwap"),
            "atr_14":        _f("atr_14"),
            "adx_14":        _f("adx_14"),
            "macd":          _f("macd"),
            "macd_signal":   _f("macd_signal"),
            "macd_hist":     _f("macd_hist"),
            "volume_ratio":  _f("volume_ratio"),
            "candle_count":  int(item.get("candle_count", {}).get("N", "0")),
            "computed_at":   _s("computed_at"),
        })

    df = pd.DataFrame(records)
    # Sort by candle_time ascending for clean time-series parquet
    if "candle_time" in df.columns:
        df = df.sort_values("candle_time").reset_index(drop=True)
    return df
