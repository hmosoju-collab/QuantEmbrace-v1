"""PerformanceStore — daily research rollups per model@version x horizon.

Table ``{prefix}-alpha-performance``:
    PK = ``PERF#{model_id}#{model_version}``
    SK = ``{trade_date}#H{horizon}``

Rows hold rank-IC, hit-rate, avg net edge, calibration buckets, universe
breakdown, distribution snapshot + drift scores (P4.5), and the health state
(P5.5). One row per model-version, horizon, trading day; written at EOD.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from alpha_engine.store._dynamo_json import from_dynamo, to_dynamo
from shared.logging.logger import get_logger
from shared.utils.helpers import utc_now

logger = get_logger(__name__, service_name="alpha_engine")

_PERF_TTL_DAYS = 180


def perf_pk(model_id: str, model_version: str) -> str:
    return f"PERF#{model_id}#{model_version}"


def perf_sk(trade_date: str, horizon_minutes: int) -> str:
    return f"{trade_date}#H{horizon_minutes}"


class PerformanceStore:
    def __init__(self, *, table: Any) -> None:
        self._table = table

    def put_rollup(
        self,
        *,
        model_id: str,
        model_version: str,
        trade_date: str,
        horizon_minutes: int,
        payload: dict[str, Any],
    ) -> None:
        """Write (overwrite) one daily rollup row. EOD runs once per day."""
        ttl = int((utc_now() + timedelta(days=_PERF_TTL_DAYS)).timestamp())
        item = {
            "PK": perf_pk(model_id, model_version),
            "SK": perf_sk(trade_date, horizon_minutes),
            "model_id": model_id,
            "model_version": model_version,
            "trade_date": trade_date,
            "horizon_minutes": horizon_minutes,
            **payload,
            "ttl": ttl,
        }
        self._table.put_item(Item=to_dynamo(item))
        logger.info(
            "alpha_engine.perf_rollup_written %s@%s %s H%d",
            model_id, model_version, trade_date, horizon_minutes,
        )

    def get_rollup(
        self, *, model_id: str, model_version: str, trade_date: str, horizon_minutes: int
    ) -> dict | None:
        resp = self._table.get_item(
            Key={
                "PK": perf_pk(model_id, model_version),
                "SK": perf_sk(trade_date, horizon_minutes),
            }
        )
        item = resp.get("Item")
        return from_dynamo(item) if item else None

    def history(self, *, model_id: str, model_version: str, horizon_minutes: int) -> list[dict]:
        """All rollups for a model-version + horizon, oldest-first (for rolling stats)."""
        from boto3.dynamodb.conditions import Key

        resp = self._table.query(
            KeyConditionExpression=Key("PK").eq(perf_pk(model_id, model_version))
        )
        rows = [from_dynamo(i) for i in resp.get("Items", [])]
        rows = [r for r in rows if r.get("horizon_minutes") == horizon_minutes]
        return sorted(rows, key=lambda r: r.get("trade_date", ""))
