"""AlphaOutcomeLabeler — closes the forecast feedback loop (ADR-031).

Runs in-service every ~5 min (the candle-cache TTL is 2h, so an offline EOD job
can't see morning candles — in-service labeling is the only zero-infra option).
For each matured PENDING forecast it reads the 1-minute candle at maturity
(``decision_ts + horizon``) and records the realized forward return in bps. A
missing candle (gap / TTL race) marks the row UNLABELABLE. Relabeling is
idempotent. Deep/historical labeling is done offline by the research CLI.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from alpha_engine.store._dynamo_json import from_dynamo, ist_trade_date
from alpha_engine.store.forecast_store import ForecastStore
from shared.logging.logger import get_logger
from shared.models.alpha import LABEL_LABELED, LABEL_PENDING, LABEL_UNLABELABLE

logger = get_logger(__name__, service_name="alpha_engine")


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def maturity_open_time(decision_ts: datetime, horizon_minutes: int) -> datetime:
    """The 1m candle open-time at which a forecast matures (floored to the minute)."""
    target = _aware(decision_ts) + timedelta(minutes=horizon_minutes)
    return target.replace(second=0, microsecond=0)


def realized_bps(decision_price: float, maturity_close: float) -> float:
    """RAW realized forward return in bps (NOT direction-signed).

    Stored raw so the research metrics apply the direction sign exactly once
    (``alpha_metrics.signed_realized_bps``); ``hit`` is computed direction-aware
    separately. Storing a pre-signed value would double-count the direction.
    """
    if decision_price <= 0:
        return 0.0
    return (maturity_close - decision_price) / decision_price * 10_000.0


def is_hit(raw_bps: float, direction: str) -> bool:
    """True when the raw move went the forecast's way."""
    signed = raw_bps if str(direction).upper() == "BUY" else -raw_bps
    return signed > 0


class DynamoCandleReader:
    """Reads a 1-minute candle close from the candle-cache by composite PK."""

    def __init__(self, *, table: Any) -> None:
        self._table = table

    def get_minute_close(self, market: str, symbol: str, open_time: datetime) -> float | None:
        pk = f"{market}#{symbol}#minute#{_aware(open_time).isoformat()}"
        resp = self._table.get_item(Key={"PK": pk})
        item = resp.get("Item")
        if not item or "close" not in item:
            return None
        return float(from_dynamo(item["close"]))


class AlphaOutcomeLabeler:
    def __init__(
        self,
        forecast_store: ForecastStore,
        candle_reader: DynamoCandleReader,
        *,
        market: str = "NSE",
    ) -> None:
        self._store = forecast_store
        self._reader = candle_reader
        self._market = market

    def label_matured(self, now: datetime) -> dict[str, int]:
        """Label all matured PENDING forecasts for the current IST trading day.

        Returns a small counts dict (labeled / unlabelable / still_pending).
        """
        now = _aware(now)
        trade_date = ist_trade_date(now)
        rows = self._store.query_day(trade_date, self._market)
        counts = {"labeled": 0, "unlabelable": 0, "pending": 0}

        for row in rows:
            if row.get("label_status") != LABEL_PENDING:
                continue
            decision_ts = datetime.fromisoformat(row["decision_ts"])
            horizon = int(row["horizon_minutes"])
            mature_at = maturity_open_time(decision_ts, horizon)
            if mature_at > now:
                counts["pending"] += 1
                continue

            close = self._reader.get_minute_close(self._market, row["symbol"], mature_at)
            if close is None:
                self._store.update_label(
                    pk=row["PK"], sk=row["SK"], label_status=LABEL_UNLABELABLE,
                    realized_fwd_return_bps=None, hit=None,
                    label_reason=f"no_candle_at_{mature_at.isoformat()}",
                )
                counts["unlabelable"] += 1
                continue

            rbps = realized_bps(float(row["decision_price"]), close)
            self._store.update_label(
                pk=row["PK"], sk=row["SK"], label_status=LABEL_LABELED,
                realized_fwd_return_bps=rbps, hit=is_hit(rbps, row["direction"]),
            )
            counts["labeled"] += 1

        logger.info(
            "alpha_engine.labeler date=%s labeled=%d unlabelable=%d pending=%d",
            trade_date, counts["labeled"], counts["unlabelable"], counts["pending"],
        )
        return counts
