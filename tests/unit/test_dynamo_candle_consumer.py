"""
Unit tests for DynamoCandleConsumer (Phase 3 — ADR-013).

Coverage:
    poll_new_candles()
        - Returns CandleResult list for items within lookback window
        - Returns empty list when DynamoDB scan raises
        - Dedup prevents same candle from being returned twice
        - Dedup entries evicted after _DEDUP_EVICT_SECONDS
        - Returns candles during MARKET_OPEN/PRE_CLOSE and pauses only POST_CLOSE
        - Items with missing required fields are skipped (parse_error logged)

    _parse_item()
        - Correct parsing of all required fields
        - candle_close_time = candle_open_time + interval_minutes
        - trace_id is deterministic for same inputs
        - Returns None for item with missing 'close' field
        - Handles string candle_open_time correctly
        - Handles timezone-naive candle_open_time (adds UTC)
        - Handles all known interval strings (minute, 5minute, 15minute, etc.)

    CandleResult.to_bar()
        - Returns Bar with correct symbol, market, OHLCV, interval
        - bar.timestamp equals candle_close_time

    _make_candle_trace_id()
        - Same inputs always produce the same 32-char hex string
        - Different market produces different trace_id
        - Different interval produces different trace_id
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
import sys
import time
from unittest.mock import MagicMock

import pytest

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from strategy_engine.consumers.dynamo_candle_consumer import (  # noqa: E402
    CandleResult,
    DynamoCandleConsumer,
    _make_candle_trace_id,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _utc(minutes_ago: int = 1) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)


def _item(
    market: str = "NSE",
    instrument: str = "RELIANCE",
    interval: str = "minute",
    candle_open_time: datetime | None = None,
    open_: float = 2490.0,
    high: float = 2510.0,
    low: float = 2485.0,
    close: float = 2500.0,
    volume: int = 5000,
) -> dict:
    """Build a DynamoDB candle-cache item dict."""
    cot = candle_open_time or _utc(2)
    pk = f"{market}#{instrument}#{interval}#{cot.isoformat()}"
    return {
        "PK":              pk,
        "cache_bucket":    "ACTIVE",
        "market":          market,
        "instrument":      instrument,
        "interval":        interval,
        "candle_open_time": cot.isoformat(),
        "open":            Decimal(str(open_)),
        "high":            Decimal(str(high)),
        "low":             Decimal(str(low)),
        "close":           Decimal(str(close)),
        "volume":          Decimal(str(volume)),
        "updated_at":      datetime.now(UTC).isoformat(),
        "expires_at":      Decimal(str(int(time.time()) + 7200)),
    }


def _make_consumer(
    items: list[dict] | None = None,
    phase_fn=None,
) -> DynamoCandleConsumer:
    """Build a DynamoCandleConsumer with a mocked DynamoDB table."""
    table = MagicMock()
    table.name = "test-candle-cache"
    table.query.return_value = {"Items": items or []}
    table.scan.return_value = {"Items": items or []}
    return DynamoCandleConsumer(
        dynamo_table=table,
        lookback_minutes=3,
        phase_check_fn=phase_fn,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# poll_new_candles
# ═══════════════════════════════════════════════════════════════════════════════

class TestPollNewCandles:

    def test_returns_new_candles(self):
        items = [_item(), _item(instrument="INFY")]
        consumer = _make_consumer(items=items)
        results = consumer.poll_new_candles()
        assert len(results) == 2

    def test_returns_empty_on_scan_error(self):
        table = MagicMock()
        table.name = "test"
        table.query.side_effect = Exception("DynamoDB index unavailable")
        table.scan.side_effect = Exception("DynamoDB unavailable")
        consumer = DynamoCandleConsumer(dynamo_table=table)
        results = consumer.poll_new_candles()
        assert results == []

    def test_dedup_prevents_duplicate_candles(self):
        candle_item = _item()
        consumer = _make_consumer(items=[candle_item])

        first  = consumer.poll_new_candles()
        second = consumer.poll_new_candles()    # same item from scan on second call

        assert len(first)  == 1
        assert len(second) == 0    # dedup blocks it

    def test_dedup_eviction_allows_reprocessing(self):
        candle_item = _item()
        consumer = _make_consumer(items=[candle_item])
        # Override eviction time to near-zero
        from strategy_engine.consumers import dynamo_candle_consumer as m
        original = m._DEDUP_EVICT_SECONDS
        m._DEDUP_EVICT_SECONDS = 0.01

        try:
            consumer.poll_new_candles()     # first dispatch
            # Force eviction by setting last_evict far in the past
            consumer._last_evict = time.monotonic() - 31.0
            time.sleep(0.02)               # wait for eviction age
            consumer._evict_dedup()        # manually trigger eviction
            second = consumer.poll_new_candles()
            # After eviction the candle can be dispatched again
            assert len(second) == 1
        finally:
            m._DEDUP_EVICT_SECONDS = original

    def test_market_open_phase_allows_polling(self):
        from shared.zerodha.market_phase import MarketPhase
        items = [_item()]
        phase_fn = MagicMock(return_value=MarketPhase.MARKET_OPEN)
        consumer = _make_consumer(items=items, phase_fn=phase_fn)
        results = consumer.poll_new_candles()
        assert len(results) == 1

    def test_pre_close_phase_allows_polling(self):
        from shared.zerodha.market_phase import MarketPhase
        items = [_item()]
        phase_fn = MagicMock(return_value=MarketPhase.PRE_CLOSE)
        consumer = _make_consumer(items=items, phase_fn=phase_fn)
        results = consumer.poll_new_candles()
        assert len(results) == 1

    def test_post_close_phase_returns_empty(self):
        from shared.zerodha.market_phase import MarketPhase
        items = [_item()]
        phase_fn = MagicMock(return_value=MarketPhase.POST_CLOSE)
        consumer = _make_consumer(items=items, phase_fn=phase_fn)
        results = consumer.poll_new_candles()
        assert results == []

    def test_non_paused_phase_allows_polling(self):
        from shared.zerodha.market_phase import MarketPhase
        items = [_item()]
        phase_fn = MagicMock(return_value=MarketPhase.NORMAL)
        consumer = _make_consumer(items=items, phase_fn=phase_fn)
        results = consumer.poll_new_candles()
        assert len(results) == 1

    def test_invalid_item_skipped(self):
        bad_item = {"PK": "bad#item", "market": "NSE"}   # missing required fields
        good_item = _item(instrument="RELIANCE")
        consumer = _make_consumer(items=[bad_item, good_item])
        results = consumer.poll_new_candles()
        assert len(results) == 1
        assert results[0].symbol == "RELIANCE"

    def test_multiple_symbols_different_dedup_keys(self):
        items = [
            _item(instrument="RELIANCE"),
            _item(instrument="INFY"),
            _item(instrument="HDFC"),
        ]
        consumer = _make_consumer(items=items)
        results = consumer.poll_new_candles()
        assert len(results) == 3

    def test_paginated_scan_collects_all_items(self):
        page1_item = _item(instrument="RELIANCE")
        page2_item = _item(instrument="INFY")

        table = MagicMock()
        table.name = "test-candle-cache"
        # First query returns LastEvaluatedKey — triggers second page
        table.query.side_effect = [
            {"Items": [page1_item], "LastEvaluatedKey": {"PK": "marker"}},
            {"Items": [page2_item]},
        ]
        consumer = DynamoCandleConsumer(dynamo_table=table)
        results = consumer.poll_new_candles()
        assert len(results) == 2

    def test_query_uses_candle_open_time_index(self):
        consumer = _make_consumer(items=[_item()])
        consumer.poll_new_candles()
        kwargs = consumer._table.query.call_args.kwargs
        assert kwargs["IndexName"] == "candle-open-time-index"
        assert "KeyConditionExpression" in kwargs

    def test_old_ttl_schema_is_skipped(self):
        old_item = _item()
        old_item["TTL"] = old_item.pop("expires_at")
        consumer = _make_consumer(items=[old_item])
        assert consumer.poll_new_candles() == []


# ═══════════════════════════════════════════════════════════════════════════════
# _parse_item
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseItem:

    def _consumer(self) -> DynamoCandleConsumer:
        return _make_consumer()

    def test_parses_all_fields_correctly(self):
        cot = _utc(2)
        raw = _item(
            market="NSE", instrument="RELIANCE", interval="5minute",
            candle_open_time=cot, open_=2490.0, high=2510.0,
            low=2485.0, close=2500.0, volume=12345,
        )
        consumer = self._consumer()
        result = consumer._parse_item(raw)
        assert result is not None
        assert result.market   == "NSE"
        assert result.symbol   == "RELIANCE"
        assert result.interval == "5minute"
        assert abs(result.open  - 2490.0) < 0.01
        assert abs(result.high  - 2510.0) < 0.01
        assert abs(result.low   - 2485.0) < 0.01
        assert abs(result.close - 2500.0) < 0.01
        assert result.volume   == 12345

    def test_candle_close_time_is_open_plus_interval(self):
        cot = _utc(5)
        raw = _item(interval="5minute", candle_open_time=cot)
        consumer = self._consumer()
        result = consumer._parse_item(raw)
        assert result is not None
        expected_close = cot + timedelta(minutes=5)
        assert abs((result.candle_close_time - expected_close).total_seconds()) < 1

    def test_interval_1min_adds_1_minute(self):
        cot = _utc(2)
        raw = _item(interval="minute", candle_open_time=cot)
        consumer = self._consumer()
        result = consumer._parse_item(raw)
        assert result is not None
        assert (result.candle_close_time - result.candle_open_time) == timedelta(minutes=1)

    def test_interval_15min_adds_15_minutes(self):
        cot = _utc(20)
        raw = _item(interval="15minute", candle_open_time=cot)
        consumer = self._consumer()
        result = consumer._parse_item(raw)
        assert result is not None
        assert (result.candle_close_time - result.candle_open_time) == timedelta(minutes=15)

    def test_timezone_naive_candle_time_gets_utc(self):
        naive_dt = datetime(2025, 3, 15, 9, 30, 0)    # no tzinfo
        raw = _item(candle_open_time=None)
        raw["candle_open_time"] = naive_dt.isoformat()
        consumer = self._consumer()
        result = consumer._parse_item(raw)
        assert result is not None
        assert result.candle_open_time.tzinfo is not None

    def test_missing_close_returns_none(self):
        raw = _item()
        del raw["close"]
        consumer = self._consumer()
        result = consumer._parse_item(raw)
        assert result is None

    def test_missing_market_returns_none(self):
        raw = _item()
        del raw["market"]
        consumer = self._consumer()
        result = consumer._parse_item(raw)
        assert result is None

    def test_trace_id_is_32_hex_chars(self):
        raw = _item()
        consumer = self._consumer()
        result = consumer._parse_item(raw)
        assert result is not None
        assert len(result.trace_id) == 32
        int(result.trace_id, 16)   # raises ValueError if not hex


# ═══════════════════════════════════════════════════════════════════════════════
# CandleResult.to_bar()
# ═══════════════════════════════════════════════════════════════════════════════

class TestCandleResultToBar:

    def _build_result(self) -> CandleResult:
        cot = _utc(2)
        return CandleResult(
            market="NSE",
            symbol="RELIANCE",
            interval="5minute",
            candle_open_time=cot,
            candle_close_time=cot + timedelta(minutes=5),
            open=2490.0,
            high=2510.0,
            low=2485.0,
            close=2500.0,
            volume=10000,
            trace_id="a" * 32,
        )

    def test_to_bar_symbol_and_market(self):
        bar = self._build_result().to_bar()
        assert bar.symbol == "RELIANCE"
        assert bar.market == "NSE"

    def test_to_bar_ohlcv(self):
        bar = self._build_result().to_bar()
        assert bar.open  == 2490.0
        assert bar.high  == 2510.0
        assert bar.low   == 2485.0
        assert bar.close == 2500.0
        assert bar.volume == 10000

    def test_to_bar_timestamp_is_close_time(self):
        result = self._build_result()
        bar = result.to_bar()
        assert bar.timestamp == result.candle_close_time

    def test_to_bar_interval_set(self):
        bar = self._build_result().to_bar()
        assert bar.interval == "5minute"


# ═══════════════════════════════════════════════════════════════════════════════
# _make_candle_trace_id
# ═══════════════════════════════════════════════════════════════════════════════

class TestMakeCandleTraceId:

    def _ts(self) -> datetime:
        return datetime(2025, 3, 15, 9, 30, 0, tzinfo=UTC)

    def test_deterministic_same_inputs(self):
        ts = self._ts()
        id1 = _make_candle_trace_id("NSE", "RELIANCE", "minute", ts)
        id2 = _make_candle_trace_id("NSE", "RELIANCE", "minute", ts)
        assert id1 == id2

    def test_length_32_chars(self):
        ts = self._ts()
        trace_id = _make_candle_trace_id("NSE", "RELIANCE", "minute", ts)
        assert len(trace_id) == 32

    def test_is_valid_hex(self):
        ts = self._ts()
        trace_id = _make_candle_trace_id("NSE", "RELIANCE", "minute", ts)
        int(trace_id, 16)   # raises ValueError if not hex

    def test_different_market_different_id(self):
        ts = self._ts()
        nse_id = _make_candle_trace_id("NSE", "RELIANCE", "minute", ts)
        us_id  = _make_candle_trace_id("US",  "RELIANCE", "minute", ts)
        assert nse_id != us_id

    def test_different_interval_different_id(self):
        ts = self._ts()
        id_1m  = _make_candle_trace_id("NSE", "RELIANCE", "minute",   ts)
        id_5m  = _make_candle_trace_id("NSE", "RELIANCE", "5minute",  ts)
        id_15m = _make_candle_trace_id("NSE", "RELIANCE", "15minute", ts)
        assert len({id_1m, id_5m, id_15m}) == 3

    def test_different_symbol_different_id(self):
        ts = self._ts()
        id_rel  = _make_candle_trace_id("NSE", "RELIANCE", "minute", ts)
        id_infy = _make_candle_trace_id("NSE", "INFY",     "minute", ts)
        assert id_rel != id_infy

    def test_dedup_key_matches_trace_input(self):
        cot = datetime(2025, 3, 15, 9, 30, tzinfo=UTC)
        raw = _item(instrument="RELIANCE", interval="minute", candle_open_time=cot)
        consumer = _make_consumer()
        result = consumer._parse_item(raw)
        expected_trace = _make_candle_trace_id("NSE", "RELIANCE", "minute", cot)
        assert result.trace_id == expected_trace


# ═══════════════════════════════════════════════════════════════════════════════
# Writer → Consumer schema round-trip regression test
#
# This class proves that the item written by IntradayCandleStream._write_candle()
# (DynamoDB low-level client wire format) maps to exactly the shape that
# DynamoCandleConsumer._parse_item() expects (DynamoDB resource API format).
#
# The historical regression:
#   Writer used PK="CANDLE#{instrument}#{interval}", SK=datetime_string,
#   with no 'market' and no 'candle_open_time' attributes.
#   Consumer's FilterExpression and item parser expected market, instrument,
#   interval, candle_open_time as top-level string attributes.
#   → Every candle written was silently invisible to the strategy engine.
#
# Design of this test:
#   We don't import candle_stream.py (it requires pydantic at module-level
#   via shared.config.settings). Instead we mirror _write_candle() logic
#   inline via _build_wire_item() so this test always runs without heavy deps.
#   A second tier (_test_via_real_write_candle) is defined and called only
#   when candle_stream is importable (pytest.importorskip equivalent via try).
# ═══════════════════════════════════════════════════════════════════════════════

def _build_wire_item(
    market: str,
    instrument: str,
    interval: str,
    candle_open_time: datetime,
    open_: float = 2490.0,
    high: float = 2510.0,
    low: float = 2485.0,
    close: float = 2500.0,
    volume: int = 5000,
) -> dict:
    """
    Build the DynamoDB wire-format Put Item dict that _write_candle() produces.

    This is the exact schema written by candle_stream._write_candle() after Fix 1.
    Key contract:
      PK = "{market}#{instrument}#{interval}#{candle_open_time_iso}"
      All required fields present as top-level attributes.
      No SK attribute — PK is the full composite key.
    """
    cot_iso = candle_open_time.isoformat()
    pk = f"{market}#{instrument}#{interval}#{cot_iso}"
    return {
        "PK":               {"S": pk},
        "cache_bucket":     {"S": "ACTIVE"},
        "market":           {"S": market},
        "instrument":       {"S": instrument},
        "interval":         {"S": interval},
        "candle_open_time": {"S": cot_iso},
        "open":             {"N": str(open_)},
        "high":             {"N": str(high)},
        "low":              {"N": str(low)},
        "close":            {"N": str(close)},
        "volume":           {"N": str(volume)},
        "captured_at":      {"S": datetime.now(UTC).isoformat()},
        "expires_at":       {"N": "9999999999"},
    }


def _unwire_dynamo_item(wire_item: dict) -> dict:
    """
    Simulate the DynamoDB resource API deserialization of a wire-format item.

    boto3 resource API returns plain Python types from table.scan():
      {"S": "NSE"}       → "NSE"        (str)
      {"N": "2490.0"}    → Decimal("2490.0")
      {"BOOL": True}     → True

    The low-level client's put_item() Item uses this wire format; the
    consumer reads the resource API format. This function bridges the two.
    """
    result = {}
    for key, val in wire_item.items():
        if "S" in val:
            result[key] = val["S"]
        elif "N" in val:
            result[key] = Decimal(val["N"])
        elif "BOOL" in val:
            result[key] = val["BOOL"]
    return result


class TestWriterToConsumerSchemaRoundTrip:
    """
    Regression guard: writer schema → consumer parser round-trip.

    Any change to _write_candle() that breaks consumer parsing will be
    caught here before it reaches production.
    """

    _COT = datetime(2025, 6, 15, 4, 0, 0, tzinfo=UTC)  # UTC 09:30 IST

    def _round_trip(
        self,
        market: str = "NSE",
        instrument: str = "RELIANCE",
        interval: str = "minute",
        open_: float = 2490.0,
        high: float = 2510.0,
        low: float = 2485.0,
        close: float = 2500.0,
        volume: int = 5000,
    ):
        """Build a wire item, unwire it, parse it — return CandleResult."""
        wire = _build_wire_item(
            market=market,
            instrument=instrument,
            interval=interval,
            candle_open_time=self._COT,
            open_=open_, high=high, low=low, close=close, volume=volume,
        )
        resource_item = _unwire_dynamo_item(wire)
        consumer = _make_consumer()
        return consumer._parse_item(resource_item)

    # ── Schema contract tests ─────────────────────────────────────────────────

    def test_round_trip_returns_candle_result(self):
        result = self._round_trip()
        assert result is not None, "Writer item should parse without error"

    def test_round_trip_market_preserved(self):
        result = self._round_trip(market="NSE")
        assert result.market == "NSE"

    def test_round_trip_instrument_preserved(self):
        result = self._round_trip(instrument="RELIANCE")
        assert result.symbol == "RELIANCE"

    def test_round_trip_interval_preserved(self):
        result = self._round_trip(interval="5minute")
        assert result.interval == "5minute"

    def test_round_trip_candle_open_time_preserved(self):
        result = self._round_trip()
        assert result.candle_open_time == self._COT

    def test_round_trip_candle_open_time_is_utc(self):
        result = self._round_trip()
        import datetime as _dt
        assert result.candle_open_time.tzinfo is not None
        assert result.candle_open_time.utcoffset() == _dt.timedelta(0)

    def test_round_trip_close_time_equals_open_plus_interval(self):
        result = self._round_trip(interval="5minute")
        assert result.candle_close_time == self._COT + timedelta(minutes=5)

    def test_round_trip_ohlcv_preserved(self):
        result = self._round_trip(open_=2490.0, high=2510.0, low=2485.0, close=2500.0, volume=5000)
        assert result.open   == pytest.approx(2490.0)
        assert result.high   == pytest.approx(2510.0)
        assert result.low    == pytest.approx(2485.0)
        assert result.close  == pytest.approx(2500.0)
        assert result.volume == 5000

    def test_round_trip_trace_id_deterministic(self):
        r1 = self._round_trip()
        r2 = self._round_trip()
        assert r1.trace_id == r2.trace_id, "Same inputs must produce same trace_id"
        assert len(r1.trace_id) == 32

    def test_round_trip_no_sk_attribute(self):
        """Regression: old schema had SK; new schema has no SK."""
        wire = _build_wire_item("NSE", "RELIANCE", "minute", self._COT)
        assert "SK" not in wire, "Writer must not write SK — PK is the full composite key"

    def test_round_trip_pk_format(self):
        """PK must match consumer's FilterExpression key and candle_open_time GSI."""
        wire = _build_wire_item("NSE", "RELIANCE", "5minute", self._COT)
        expected_pk = f"NSE#RELIANCE#5minute#{self._COT.isoformat()}"
        assert wire["PK"]["S"] == expected_pk

    def test_round_trip_candle_open_time_attribute_present(self):
        """Consumer FilterExpression: candle_open_time >= cutoff — attribute must exist."""
        wire = _build_wire_item("NSE", "RELIANCE", "minute", self._COT)
        assert "candle_open_time" in wire
        assert wire["candle_open_time"]["S"] == self._COT.isoformat()

    def test_round_trip_cache_bucket_attribute_present(self):
        """Consumer GSI query requires cache_bucket as the partition key."""
        wire = _build_wire_item("NSE", "RELIANCE", "minute", self._COT)
        assert wire["cache_bucket"]["S"] == "ACTIVE"

    def test_round_trip_expires_at_attribute_present(self):
        """Terraform/LocalStack TTL attribute is expires_at, not TTL."""
        wire = _build_wire_item("NSE", "RELIANCE", "minute", self._COT)
        assert "expires_at" in wire
        assert "TTL" not in wire

    def test_round_trip_market_attribute_present(self):
        """Consumer _parse_item requires 'market' as a top-level attribute."""
        wire = _build_wire_item("NSE", "RELIANCE", "minute", self._COT)
        assert "market" in wire
        assert wire["market"]["S"] == "NSE"

    # ── Regression: old broken schema is detected ─────────────────────────────

    def test_old_schema_candle_prefix_pk_fails_parse(self):
        """
        The pre-Fix-1 PK format (CANDLE#{instrument}#{interval}) produced items
        that DynamoCandleConsumer._parse_item() rejected because 'market' was
        absent. Verify this is still rejected after the fix.
        """
        broken_item = {
            "PK":         "CANDLE#RELIANCE#minute",
            "SK":         "2025-06-15T04:00:00",
            "instrument": "RELIANCE",
            "interval":   "minute",
            # 'market' and 'candle_open_time' intentionally absent — old schema
            "open":       Decimal("2490.0"),
            "high":       Decimal("2510.0"),
            "low":        Decimal("2485.0"),
            "close":      Decimal("2500.0"),
            "volume":     Decimal("5000"),
        }
        consumer = _make_consumer()
        result = consumer._parse_item(broken_item)
        assert result is None, "Old schema without 'market'/'candle_open_time' must return None"

    def test_us_market_round_trip(self):
        """US instruments follow the same schema — market='US', instrument=ticker."""
        result = self._round_trip(market="US", instrument="AAPL", interval="1min")
        assert result is not None
        assert result.market == "US"
        assert result.symbol == "AAPL"

    def test_multiple_intervals_all_parse(self):
        """All three active intervals (1m, 5m, 15m) must survive the round-trip."""
        for interval, expected_delta in [
            ("minute",   timedelta(minutes=1)),
            ("5minute",  timedelta(minutes=5)),
            ("15minute", timedelta(minutes=15)),
        ]:
            result = self._round_trip(interval=interval)
            assert result is not None, f"interval={interval} must parse"
            assert result.candle_close_time - result.candle_open_time == expected_delta, (
                f"interval={interval}: close - open should be {expected_delta}"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Real _write_candle() → DynamoCandleConsumer._parse_item() integration
# ═══════════════════════════════════════════════════════════════════════════════
#
# This test calls the REAL IntradayCandleStream._write_candle() (not a mirror),
# captures the DynamoDB wire-format Item it would have written, converts it to
# the resource-API format that the consumer receives, and feeds it into
# DynamoCandleConsumer._parse_item(). This gives us true end-to-end coverage of
# the writer→consumer schema contract without requiring AWS or a live DynamoDB.
#
# candle_stream.py depends on pydantic (via shared.config.settings) at module
# level.  The test stubs those imports in sys.modules before importing the
# module so the test runs in environments without pydantic (CI sandbox).
# ═══════════════════════════════════════════════════════════════════════════════

def _stub_candle_stream_imports() -> None:
    """
    Install a stub for shared.config.settings — the only pydantic-dependent
    module that candle_stream.py imports at module level.

    All other shared.* modules (shared.logging.logger, shared.utils.helpers,
    shared.zerodha.market_phase) are pure stdlib and load fine without stubs.
    We must NOT override the real 'shared' package or its sub-packages —
    doing so would break shared.models.signal and other real imports that
    DynamoCandleConsumer's import chain requires.
    """
    import types

    settings_mod = types.ModuleType("shared.config.settings")
    settings_mod.AppSettings = object
    settings_mod.get_settings = lambda: None

    # Ensure parent packages are imported first so the dotted-path
    # sys.modules key resolves correctly against the real package.
    import shared
    import shared.config  # noqa: F401 — real sub-package

    sys.modules.setdefault("shared.config.settings", settings_mod)


def _import_candle_stream():
    """
    Import IntradayCandleStream and CandleData after stubbing dependencies.

    Returns (IntradayCandleStream, CandleData) or raises ImportError so callers
    can skip gracefully if the module is genuinely unavailable.
    """
    _stub_candle_stream_imports()
    # data_ingestion is mounted under services/ — already on sys.path via
    # the path bootstrap at the top of this file.
    from data_ingestion.candle_stream import CandleData, IntradayCandleStream
    return IntradayCandleStream, CandleData


class _CapturingDynamo:
    """Fake low-level DynamoDB client that captures put_item calls synchronously."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def put_item(self, TableName: str, Item: dict) -> None:
        self.calls.append({"TableName": TableName, "Item": Item})


@pytest.mark.asyncio
class TestRealWriteCandleToConsumerRoundTrip:
    """
    True integration: real IntradayCandleStream._write_candle() → real
    DynamoCandleConsumer._parse_item().

    Any silent schema drift between writer and consumer will be caught here.
    The test is automatically skipped if candle_stream cannot be imported
    (e.g., pydantic genuinely absent and the stub approach fails).
    """

    _COT = datetime(2025, 6, 15, 4, 0, 0, tzinfo=UTC)

    @pytest.fixture(autouse=True)
    def _load_module(self):
        try:
            IntradayCandleStream, CandleData = _import_candle_stream()
        except Exception as exc:
            pytest.skip(f"candle_stream not importable: {exc}")
        self._Stream = IntradayCandleStream
        self._CandleData = CandleData

    def _make_stream(self, fake_dynamo: _CapturingDynamo) -> object:
        """Construct a minimal IntradayCandleStream with injected fake dynamo."""
        stream = object.__new__(self._Stream)
        stream._dynamo = fake_dynamo
        stream._candle_table = "test-candle-cache"
        # _write_candle uses: self._dynamo, self._candle_table, asyncio.to_thread
        # — nothing else needs initialisation for this test.
        return stream

    def _make_candle(
        self,
        market: str = "NSE",
        instrument: str = "RELIANCE",
        interval: str = "minute",
        open_: float = 2490.0,
        high: float = 2510.0,
        low: float = 2485.0,
        close: float = 2500.0,
        volume: int = 5000,
    ) -> object:
        return self._CandleData(
            market=market,
            instrument=instrument,
            interval=interval,
            dt=self._COT,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
        )

    async def _write_and_parse(self, candle) -> CandleResult | None:
        """Call real _write_candle(), capture Item, convert, parse."""
        fake_dynamo = _CapturingDynamo()
        stream = self._make_stream(fake_dynamo)
        await stream._write_candle(candle)

        assert len(fake_dynamo.calls) == 1, "_write_candle must call put_item exactly once"
        wire_item = fake_dynamo.calls[0]["Item"]

        # Simulate DynamoDB resource API deserialization (S/N → str/Decimal)
        resource_item = _unwire_dynamo_item(wire_item)

        # DynamoCandleConsumer.__init__ signature: (dynamo_table, lookback_minutes, phase_check_fn)
        consumer = DynamoCandleConsumer(dynamo_table=MagicMock())
        return consumer._parse_item(resource_item)

    # ── Tests ─────────────────────────────────────────────────────────────────

    async def test_real_write_produces_parseable_item(self):
        candle = self._make_candle()
        result = await self._write_and_parse(candle)
        assert result is not None, (
            "Real _write_candle() produced an item that _parse_item() rejected — "
            "writer/consumer schema contract is broken"
        )

    async def test_real_write_market_round_trips(self):
        result = await self._write_and_parse(self._make_candle(market="NSE"))
        assert result.market == "NSE"

    async def test_real_write_instrument_round_trips(self):
        result = await self._write_and_parse(self._make_candle(instrument="RELIANCE"))
        assert result.symbol == "RELIANCE"

    async def test_real_write_interval_round_trips(self):
        result = await self._write_and_parse(self._make_candle(interval="5minute"))
        assert result.interval == "5minute"

    async def test_real_write_candle_open_time_round_trips(self):
        result = await self._write_and_parse(self._make_candle())
        assert result.candle_open_time == self._COT

    async def test_real_write_ohlcv_round_trips(self):
        result = await self._write_and_parse(
            self._make_candle(open_=2490.0, high=2510.0, low=2485.0, close=2500.0, volume=5000)
        )
        assert result.open   == pytest.approx(2490.0)
        assert result.high   == pytest.approx(2510.0)
        assert result.low    == pytest.approx(2485.0)
        assert result.close  == pytest.approx(2500.0)
        assert result.volume == 5000

    async def test_real_write_pk_has_no_candle_prefix(self):
        """Regression: old schema wrote PK=CANDLE#… — new schema must not."""
        fake_dynamo = _CapturingDynamo()
        stream = self._make_stream(fake_dynamo)
        await stream._write_candle(self._make_candle())
        pk = fake_dynamo.calls[0]["Item"]["PK"]["S"]
        assert not pk.startswith("CANDLE#"), (
            f"Writer still using old CANDLE# prefix: {pk!r}"
        )

    async def test_real_write_pk_format(self):
        """PK must be {market}#{instrument}#{interval}#{candle_open_time_iso}."""
        fake_dynamo = _CapturingDynamo()
        stream = self._make_stream(fake_dynamo)
        await stream._write_candle(self._make_candle(
            market="NSE", instrument="RELIANCE", interval="minute"
        ))
        pk = fake_dynamo.calls[0]["Item"]["PK"]["S"]
        expected = f"NSE#RELIANCE#minute#{self._COT.isoformat()}"
        assert pk == expected, f"PK mismatch: expected {expected!r}, got {pk!r}"

    async def test_real_write_no_sk_attribute(self):
        """Writer must not emit SK — PK is the full composite key."""
        fake_dynamo = _CapturingDynamo()
        stream = self._make_stream(fake_dynamo)
        await stream._write_candle(self._make_candle())
        assert "SK" not in fake_dynamo.calls[0]["Item"], (
            "Writer must not write SK attribute"
        )

    async def test_real_write_cache_bucket_for_gsi(self):
        """Writer must populate the GSI partition key used by the strategy poller."""
        fake_dynamo = _CapturingDynamo()
        stream = self._make_stream(fake_dynamo)
        await stream._write_candle(self._make_candle())
        item = fake_dynamo.calls[0]["Item"]
        assert item["cache_bucket"]["S"] == "ACTIVE"

    async def test_real_write_uses_expires_at_ttl_attribute(self):
        """Regression: Terraform TTL is expires_at, not TTL."""
        fake_dynamo = _CapturingDynamo()
        stream = self._make_stream(fake_dynamo)
        await stream._write_candle(self._make_candle())
        item = fake_dynamo.calls[0]["Item"]
        assert "expires_at" in item
        assert "TTL" not in item


@pytest.mark.asyncio
class TestPhase5MultiIntervalCandleStream:
    """Phase 5: data_ingestion must produce every registered candle interval."""

    _COT = datetime(2025, 6, 15, 4, 0, 0, tzinfo=UTC)

    @pytest.fixture(autouse=True)
    def _load_module(self):
        try:
            IntradayCandleStream, _CandleData = _import_candle_stream()
        except Exception as exc:
            pytest.skip(f"candle_stream not importable: {exc}")
        self._Stream = IntradayCandleStream
        self._CandleData = _CandleData

    def _make_stream(self, fake_dynamo: _CapturingDynamo) -> object:
        stream = object.__new__(self._Stream)
        stream._dynamo = fake_dynamo
        stream._candle_table = "test-candle-cache"
        return stream

    def _make_candle(
        self,
        market: str = "NSE",
        instrument: str = "RELIANCE",
        interval: str = "minute",
        open_: float = 2490.0,
        high: float = 2510.0,
        low: float = 2485.0,
        close: float = 2500.0,
        volume: int = 5000,
    ) -> object:
        return self._CandleData(
            market=market,
            instrument=instrument,
            interval=interval,
            dt=self._COT,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
        )

    async def _write_and_parse(self, candle) -> CandleResult | None:
        fake_dynamo = _CapturingDynamo()
        stream = self._make_stream(fake_dynamo)
        await stream._write_candle(candle)
        wire_item = fake_dynamo.calls[0]["Item"]
        resource_item = _unwire_dynamo_item(wire_item)
        consumer = DynamoCandleConsumer(dynamo_table=MagicMock())
        return consumer._parse_item(resource_item)

    async def test_round_robin_contains_minute_5minute_15minute_pairs(self):
        stream = self._Stream(
            zerodha=object(),
            instrument_tokens={"NSE:RELIANCE": 1},
            dynamo_client=None,
            candle_table="test-candle-cache",
            intervals=["minute", "5minute", "15minute"],
            settings=object(),
        )
        assert list(stream._standard_queue) == [
            ("NSE:RELIANCE", "minute"),
            ("NSE:RELIANCE", "5minute"),
            ("NSE:RELIANCE", "15minute"),
        ]

    async def test_fetch_preserves_requested_interval_on_emitted_candle(self):
        class _FakeZerodha:
            def __init__(self, cot):
                self.cot = cot
                self.intervals: list[str] = []

            async def get_historical_candles(self, **kwargs):
                self.intervals.append(kwargs["interval"])
                return [{
                    "date": self.cot,
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100.5,
                    "volume": 1000,
                }]

        emitted = []
        fake = _FakeZerodha(self._COT)
        stream = self._Stream(
            zerodha=fake,
            instrument_tokens={"NSE:RELIANCE": 1},
            dynamo_client=None,
            candle_table="test-candle-cache",
            intervals=["minute", "5minute", "15minute"],
            on_candle=emitted.append,
            settings=object(),
        )

        await stream._fetch_candles("NSE:RELIANCE", "15minute")

        assert fake.intervals == ["15minute"]
        assert len(emitted) == 1
        assert emitted[0].interval == "15minute"

    async def test_real_write_market_attribute_present(self):
        """consumer._parse_item requires 'market' as top-level attribute."""
        fake_dynamo = _CapturingDynamo()
        stream = self._make_stream(fake_dynamo)
        await stream._write_candle(self._make_candle(market="NSE"))
        item = fake_dynamo.calls[0]["Item"]
        assert "market" in item
        assert item["market"]["S"] == "NSE"

    async def test_real_write_candle_open_time_attribute_present(self):
        """consumer FilterExpression on candle_open_time requires this attribute."""
        fake_dynamo = _CapturingDynamo()
        stream = self._make_stream(fake_dynamo)
        await stream._write_candle(self._make_candle())
        item = fake_dynamo.calls[0]["Item"]
        assert "candle_open_time" in item
        assert item["candle_open_time"]["S"] == self._COT.isoformat()

    async def test_real_write_us_market_round_trips(self):
        result = await self._write_and_parse(
            self._make_candle(market="US", instrument="AAPL", interval="1min")
        )
        assert result is not None
        assert result.market == "US"
        assert result.symbol == "AAPL"
