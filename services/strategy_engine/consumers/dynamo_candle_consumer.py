"""
DynamoCandleConsumer — polls DynamoDB candle-cache for new confirmed candles.

Reads from the ``{prefix}-candle-cache`` table written by ``IntradayCandleStream``
in the data_ingestion service. The strategy_engine has NO Zerodha API access —
this DynamoDB poll is the sole candle data path for Phase 3.

Design decisions (ADR-013):
    Overlapping lookback:
        Query items where cache_bucket = ACTIVE and candle_open_time >=
        (now - 3 minutes) on every poll.
        This catches eventual-consistency stragglers that may have landed after
        the previous poll's cutoff. Items are deduplicated by candle key so each
        candle is dispatched at most once per DynamoDB TTL window (2h).

    Dedup set:
        In-memory set of ``{market}#{instrument}#{interval}#{candle_open_time}``
        keys. Evicts entries older than 5 minutes. At 500ms poll interval and
        3-minute lookback, every confirmed candle is seen by up to 6 polls.
        The dedup set prevents all but the first dispatch.

    Phase check:
        Stateless IST clock check. Skips only POST_CLOSE. Candle strategies need
        opening and pre-close bars, and data_ingestion now keeps the candle stream
        active during MARKET_OPEN and PRE_CLOSE.

    candle_close_time:
        Returns candle close time = candle_open_time + interval_minutes, NOT
        poll time. Callers MUST use this as Signal.generated_at to ensure
        deterministic signal_id across restarts (signal_id is a hash of
        generated_at — using poll time would break deduplication on replay).

    trace_id:
        sha256("candle|{market}|{symbol}|{interval}|{candle_open_time}")[:32]
        Deterministic, 32 hex chars, includes market prefix to prevent
        cross-market collisions.

DynamoDB read pattern:
    Query the candle-open-time-index with cache_bucket as the partition key and
    candle_open_time as the range key. This keeps each 500ms poll bounded to the
    active lookback window instead of scanning the full 2-hour TTL cache.

Usage:
    consumer = DynamoCandleConsumer(
        dynamo_client=boto3_dynamodb_resource,
        candle_table="quantembrace-staging-candle-cache",
    )
    async for candle in consumer.poll_new_candles():
        await strategy_runner.dispatch_bar(candle.to_bar())
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import time
from typing import Any

from shared.logging.logger import get_logger
from shared.zerodha.market_phase import MarketPhase
from strategy_engine.strategies.base_strategy import Bar

logger = get_logger(__name__, service_name="strategy_engine")

# ── Constants ─────────────────────────────────────────────────────────────────

# How far back to look on every poll (catches eventual-consistency stragglers).
# Set to 10 to cover observed 4-7min write lag from IntradayCandleStream (174 fetch
# jobs at ~1 req/sec actual throughput = ~4min full cycle). 3min was too narrow —
# 15min candles and most 1m/5m candles arrived after the window had already closed.
_LOOKBACK_MINUTES: int = 10
_CANDLE_OPEN_TIME_INDEX: str = "candle-open-time-index"
_CANDLE_CACHE_BUCKET: str = "ACTIVE"
_CANDLE_TTL_ATTRIBUTE: str = "expires_at"

# Dedup set entry eviction age. Entries older than this are removed.
_DEDUP_EVICT_SECONDS: float = 300.0  # 5 minutes

# DynamoDB candle-cache item TTL = 2h; we need dedup for the 3-min lookback window.
# 300s covers the full lookback with margin.

# Candle interval string → minutes mapping
_INTERVAL_MINUTES: dict[str, int] = {
    "minute":    1,
    "1min":      1,
    "3minute":   3,
    "5minute":   5,
    "5min":      5,
    "10minute":  10,
    "15minute":  15,
    "15min":     15,
    "30minute":  30,
    "60minute":  60,
    "1h":        60,
    "day":       1440,
    "1d":        1440,
}

# Phases during which no fresh intraday strategy candles are expected.
_PAUSED_PHASES: frozenset[MarketPhase] = frozenset({
    MarketPhase.POST_CLOSE,
})


# ── Candle result type ────────────────────────────────────────────────────────

@dataclass
class CandleResult:
    """
    A new confirmed candle from the DynamoDB candle-cache.

    ``trace_id`` is deterministic — same candle always produces the same trace_id
    across service restarts, so duplicate candles seen in the same lookback window
    produce duplicate trace_ids (caller deduplication handles the rest).
    """

    market:            str        # "NSE" or "US"
    symbol:            str        # e.g. "RELIANCE"
    interval:          str        # e.g. "minute", "5minute"
    candle_open_time:  datetime   # UTC — when the candle opened
    candle_close_time: datetime   # UTC — candle_open_time + interval_minutes (NOT poll time)
    open:              float
    high:              float
    low:               float
    close:             float
    volume:            int
    trace_id:          str        # deterministic 32 hex chars
    data_quality:      str = "NORMAL"  # "NORMAL" | "WARMING_UP" | "GAP" | "STALE"

    def to_bar(self) -> Bar:
        """Convert to strategy Bar model for dispatch_bar() calls."""
        from strategy_engine.strategies.base_strategy import DataQuality
        try:
            quality = DataQuality(self.data_quality)
        except ValueError:
            quality = DataQuality.NORMAL
        return Bar(
            symbol=self.symbol,
            market=self.market,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            timestamp=self.candle_close_time,  # strategies receive close-time as bar timestamp
            interval=self.interval,
            data_quality=quality,
        )

    @property
    def dedup_key(self) -> str:
        return f"{self.market}#{self.symbol}#{self.interval}#{self.candle_open_time.isoformat()}"


# ── Consumer ──────────────────────────────────────────────────────────────────

class DynamoCandleConsumer:
    """
    DynamoDB-backed candle consumer for strategy_engine.

    Polls the candle-cache table at 500ms intervals (called from
    _candle_processing_loop in StrategyEngineService). Returns new,
    confirmed candles not already dispatched in this session.

    Args:
        dynamo_client:    boto3 DynamoDB service resource (not low-level client).
                          E.g. boto3.resource("dynamodb").Table(name)
        candle_table:     Full DynamoDB table name string.
        lookback_minutes: How far back to scan on each poll (default: 3).
        phase_check_fn:   Optional callable returning current MarketPhase.
                          Used to skip polls during paused phases.
    """

    def __init__(
        self,
        dynamo_table: Any,  # boto3 DynamoDB Table resource
        lookback_minutes: int = _LOOKBACK_MINUTES,
        startup_lookback_minutes: int = 60,
        phase_check_fn: Any | None = None,  # () -> MarketPhase | None
    ) -> None:
        self._table           = dynamo_table
        self._lookback_min    = lookback_minutes
        self._startup_lb      = startup_lookback_minutes
        self._phase_fn        = phase_check_fn
        self._startup_done:   bool = False
        # dedup_set: {dedup_key: inserted_monotonic_time}
        self._dedup:          dict[str, float] = {}
        self._last_evict:     float = time.monotonic()

    # ── Main polling interface ────────────────────────────────────────────────

    def poll_new_candles(self) -> list[CandleResult]:
        """
        Synchronous: query DynamoDB and return new candles since last lookback.

        Must be called via asyncio.to_thread() in async code (DynamoDB boto3
        calls are blocking).

        Returns:
            List of CandleResult (may be empty). Order is arbitrary.
            Each item is guaranteed to not have been returned in a previous call.
        """
        # Phase check: skip only during phases where no strategy candles should arrive.
        if self._phase_fn is not None:
            phase = self._phase_fn()
            if phase in _PAUSED_PHASES:
                return []

        now_utc = datetime.now(UTC)
        if not self._startup_done:
            lookback = self._startup_lb
            self._startup_done = True
            logger.info(
                "dynamo_candle_consumer.startup_warmup_replay lookback_minutes=%d",
                lookback,
            )
        else:
            lookback = self._lookback_min
        cutoff_dt  = now_utc - timedelta(minutes=lookback)
        cutoff_iso = cutoff_dt.isoformat()

        self._evict_dedup()

        now_epoch = int(now_utc.timestamp())

        try:
            raw_items = self._read_candle_cache(cutoff_iso, now_epoch)
        except Exception:
            logger.exception(
                "dynamo_candle_consumer.read_error table=%s",
                self._table.name if hasattr(self._table, "name") else "?",
            )
            return []

        results: list[CandleResult] = []
        for item in raw_items:
            if not self._item_is_live(item, now_epoch):
                continue
            candle = self._parse_item(item)
            if candle is None:
                continue
            if candle.candle_open_time < cutoff_dt:
                continue
            if candle.dedup_key in self._dedup:
                continue  # already dispatched in recent lookback
            self._dedup[candle.dedup_key] = time.monotonic()
            results.append(candle)

        if results:
            logger.debug("dynamo_candle_consumer.new_candles count=%d lookback_min=%d", len(results), self._lookback_min)

        return results

    # ── DynamoDB query/scan ───────────────────────────────────────────────────

    def _read_candle_cache(self, cutoff_iso: str, now_epoch: int) -> list[dict]:
        """
        Read candle-cache rows from the active lookback window.

        The production path is a Query on ``candle-open-time-index``. A Scan
        fallback remains for local tables or older environments while they are
        being migrated; it is deliberately logged because Scan is not acceptable
        as the steady-state production path.
        """
        try:
            return self._query_candle_cache(cutoff_iso, now_epoch)
        except Exception:
            logger.warning(
                "dynamo_candle_consumer.query_failed_falling_back_to_scan table=%s",
                self._table.name if hasattr(self._table, "name") else "?",
                exc_info=True,
            )
            return self._scan_candle_cache(cutoff_iso, now_epoch)

    def _query_candle_cache(self, cutoff_iso: str, now_epoch: int) -> list[dict]:
        """
        Query candle-cache via the cache_bucket/candle_open_time GSI.
        """
        from boto3.dynamodb.conditions import Attr, Key

        query_kwargs: dict[str, Any] = {
            "IndexName": _CANDLE_OPEN_TIME_INDEX,
            "KeyConditionExpression": (
                Key("cache_bucket").eq(_CANDLE_CACHE_BUCKET)
                & Key("candle_open_time").gte(cutoff_iso)
            ),
            "FilterExpression": Attr(_CANDLE_TTL_ATTRIBUTE).gt(now_epoch),
        }

        response = self._table.query(**query_kwargs)
        items: list[dict] = response.get("Items", [])

        while "LastEvaluatedKey" in response:
            response = self._table.query(
                **query_kwargs,
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))

        return items

    def _scan_candle_cache(self, cutoff_iso: str, now_epoch: int) -> list[dict]:
        """
        Scan candle-cache for items with candle_open_time >= cutoff_iso.

        This is a migration/local fallback only. Production must use
        ``_query_candle_cache`` because a full table scan every 500ms can read
        the whole two-hour cache before filtering.
        """
        try:
            from boto3.dynamodb.conditions import Attr
            filter_expr = (
                Attr("candle_open_time").gte(cutoff_iso)
                & Attr(_CANDLE_TTL_ATTRIBUTE).gt(now_epoch)
            )
            scan_kwargs: dict = {"FilterExpression": filter_expr}
        except ImportError:  # boto3 not installed (unit-test environment)
            scan_kwargs = {}

        response = self._table.scan(**scan_kwargs)
        items: list[dict] = response.get("Items", [])

        # Handle DynamoDB paginated scan (unlikely at this data volume but safe)
        while "LastEvaluatedKey" in response:
            response = self._table.scan(
                **scan_kwargs,
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))

        return items

    def _item_is_live(self, item: dict, now_epoch: int) -> bool:
        """
        Reject rows written by the old broken schema or already past TTL.

        DynamoDB TTL deletion is eventual, so the consumer must not rely on
        physical deletion to avoid stale candle dispatch.
        """
        raw = item.get(_CANDLE_TTL_ATTRIBUTE)
        if isinstance(raw, dict):
            raw = raw.get("N")
        try:
            return raw is not None and int(float(raw)) > now_epoch
        except (TypeError, ValueError):
            return False

    # ── Item parsing ──────────────────────────────────────────────────────────

    def _parse_item(self, item: dict) -> CandleResult | None:
        """
        Parse a raw DynamoDB item into a CandleResult.

        Expected item schema (written by data_ingestion/candle_stream.py):
            PK:               "{market}#{instrument}#{interval}#{candle_open_time}"
            cache_bucket:     "ACTIVE"
            market:           "NSE" | "US"
            instrument:       e.g. "RELIANCE"
            interval:         e.g. "minute", "5minute"
            candle_open_time: ISO-8601 UTC string
            open:             Decimal / float
            high:             Decimal / float
            low:              Decimal / float
            close:            Decimal / float
            volume:           Decimal / int
            captured_at:      ISO-8601 UTC string (when data_ingestion wrote this)
            expires_at:       Unix epoch seconds for DynamoDB TTL

        Returns None and logs a warning if any required field is missing or malformed.
        """
        try:
            market    = str(item["market"])
            symbol    = str(item["instrument"])
            interval  = str(item["interval"])

            raw_open_time = item["candle_open_time"]
            if isinstance(raw_open_time, str):
                candle_open_time = datetime.fromisoformat(raw_open_time)
            else:
                candle_open_time = raw_open_time
            if candle_open_time.tzinfo is None:
                candle_open_time = candle_open_time.replace(tzinfo=UTC)

            interval_min = _INTERVAL_MINUTES.get(interval, 1)
            candle_close_time = candle_open_time + timedelta(minutes=interval_min)

            trace_id = _make_candle_trace_id(market, symbol, interval, candle_open_time)

            return CandleResult(
                market=market,
                symbol=symbol,
                interval=interval,
                candle_open_time=candle_open_time,
                candle_close_time=candle_close_time,
                open=float(item["open"]),
                high=float(item["high"]),
                low=float(item["low"]),
                close=float(item["close"]),
                volume=int(item["volume"]),
                trace_id=trace_id,
                data_quality=str(item.get("data_quality", "NORMAL")),
            )

        except (KeyError, ValueError, TypeError):
            logger.warning("dynamo_candle_consumer.parse_error item_pk=%s", item.get("PK", "?"), exc_info=True)
            return None

    # ── Dedup maintenance ─────────────────────────────────────────────────────

    def _evict_dedup(self) -> None:
        """Remove dedup entries older than _DEDUP_EVICT_SECONDS."""
        now_mono = time.monotonic()
        if (now_mono - self._last_evict) < 30.0:
            return  # only run eviction every 30 seconds
        self._last_evict = now_mono

        cutoff = now_mono - _DEDUP_EVICT_SECONDS
        keys_to_remove = [k for k, t in self._dedup.items() if t < cutoff]
        for k in keys_to_remove:
            del self._dedup[k]

        if keys_to_remove:
            logger.debug("dynamo_candle_consumer.dedup_eviction evicted=%d remaining=%d", len(keys_to_remove), len(self._dedup))


# ── Module-level helpers ──────────────────────────────────────────────────────

def _make_candle_trace_id(
    market: str,
    symbol: str,
    interval: str,
    candle_open_time: datetime,
) -> str:
    """
    Compute a deterministic 32-char candle trace_id.

    Formula (ADR-013 §7.4):
        sha256("candle|{market}|{symbol}|{interval}|{candle_open_time.isoformat()}")[:32]

    Same candle always produces the same trace_id regardless of when it is polled.
    Includes market prefix to prevent NSE vs. US collisions.
    """
    raw = f"candle|{market}|{symbol}|{interval}|{candle_open_time.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
