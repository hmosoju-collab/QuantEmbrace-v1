"""ForecastStore — persists every alpha forecast (published or suppressed).

Table ``{prefix}-alpha-forecasts``:
    PK = ``DATE#{trade_date}#{market}``   (one partition per NSE trading day)
    SK = ``{decision_ts_iso}#{forecast_id}``

All forecasts are stored regardless of the publication floor — the edge-band and
drift research needs the suppressed ones too. Writes are idempotent (a replayed
bar yields the same forecast_id, hence the same SK). The labeler (P4) later
updates label fields in place.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from alpha_engine.store._dynamo_json import from_dynamo, ist_trade_date, to_dynamo
from shared.logging.logger import get_logger
from shared.models.alpha import LABEL_PENDING, AlphaOpportunity
from shared.utils.helpers import utc_now

logger = get_logger(__name__, service_name="alpha_engine")

_FORECAST_TTL_DAYS = 30


def forecast_pk(trade_date: str, market: str) -> str:
    return f"DATE#{trade_date}#{market}"


def forecast_sk(decision_ts_iso: str, forecast_id: str) -> str:
    return f"{decision_ts_iso}#{forecast_id}"


class ForecastStore:
    def __init__(self, *, table: Any) -> None:
        self._table = table

    def put_opportunity(self, opportunity: AlphaOpportunity) -> bool:
        """Idempotently persist one ranked opportunity. Returns False if it existed."""
        f = opportunity.forecast
        decision_ts_iso = f.to_dict()["decision_ts"]
        trade_date = ist_trade_date(f.decision_ts)
        pk = forecast_pk(trade_date, f.market)
        sk = forecast_sk(decision_ts_iso, f.forecast_id)
        ttl = int((utc_now() + timedelta(days=_FORECAST_TTL_DAYS)).timestamp())

        item: dict[str, Any] = {
            "PK": pk,
            "SK": sk,
            "trade_date": trade_date,
            **f.to_dict(),
            # ranking context
            "rank": opportunity.rank,
            "score": opportunity.score,
            "cycle_id": opportunity.cycle_id,
            "conflict_group_id": opportunity.conflict_group_id,
            "published": opportunity.published,
            "suppressed_by": opportunity.suppressed_by,
            # label fields (written later by the labeler)
            "label_status": LABEL_PENDING,
            "realized_fwd_return_bps": None,
            "hit": None,
            "labeled_at": None,
            "ttl": ttl,
        }

        try:
            self._table.put_item(
                Item=to_dynamo(item),
                ConditionExpression="attribute_not_exists(SK)",
            )
            return True
        except Exception as exc:
            if _is_conditional_check(exc):
                logger.debug("alpha_engine.forecast_already_stored %s", f.forecast_id)
                return False
            logger.exception("alpha_engine.forecast_put_failed %s", f.forecast_id)
            raise

    def get(self, trade_date: str, market: str, decision_ts_iso: str, forecast_id: str) -> dict | None:
        resp = self._table.get_item(
            Key={
                "PK": forecast_pk(trade_date, market),
                "SK": forecast_sk(decision_ts_iso, forecast_id),
            }
        )
        item = resp.get("Item")
        return from_dynamo(item) if item else None

    def query_day(self, trade_date: str, market: str) -> list[dict]:
        """Return all forecasts for one IST trading day + market."""
        from boto3.dynamodb.conditions import Key  # local import; boto3 only in runtime

        items: list[dict] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(forecast_pk(trade_date, market))
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return [from_dynamo(i) for i in items]

    def update_label(
        self,
        *,
        pk: str,
        sk: str,
        label_status: str,
        realized_fwd_return_bps: float | None,
        hit: bool | None,
        label_reason: str = "",
    ) -> None:
        """Write the outcome label onto a forecast row (idempotent overwrite)."""
        self._table.update_item(
            Key={"PK": pk, "SK": sk},
            UpdateExpression=(
                "SET label_status = :s, realized_fwd_return_bps = :r, "
                "hit = :h, label_reason = :reason, labeled_at = :t"
            ),
            ExpressionAttributeValues=to_dynamo(
                {
                    ":s": label_status,
                    ":r": realized_fwd_return_bps,
                    ":h": hit,
                    ":reason": label_reason,
                    ":t": utc_now().isoformat(),
                }
            ),
        )


def _is_conditional_check(exc: Exception) -> bool:
    """True when ``exc`` is a DynamoDB ConditionalCheckFailedException."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    return type(exc).__name__ == "ConditionalCheckFailedException"
