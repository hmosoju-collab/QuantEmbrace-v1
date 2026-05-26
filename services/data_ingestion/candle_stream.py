"""
Intraday Candle Stream — confirmed 1-minute candles via ``kite.historical_data()``.

Streams exchange-validated OHLCV candles for all active instruments at
3 req/sec.  Uses the **separate historical data API budget** (3 req/sec limit
independent of the 10 req/sec order API limit).

Why this is better than tick-aggregated candles:
------------------------------------------------
The strategy engine currently builds 1m/5m candles by aggregating WebSocket
ticks in memory.  This approach has two failure modes:

1. **Reconnect gaps**: When KiteTicker reconnects (typically 2-3 times per
   session), it takes up to 10 seconds to re-subscribe.  Any ticks during
   that window are lost.  The current candle has a hole.  The strategy may
   fire on a partial candle.
2. **Partial first candle**: At strategy startup, the first candle is always
   incomplete (we join mid-minute).  ORB strategy needs confirmed first-15-min
   candles; a partial candle from ticks produces wrong high/low.

``IntradayCandleStream`` fetches confirmed, exchange-validated candles from
Zerodha's historical data endpoint.  These are the same candles shown in
Kite charts — exchange-cleared prices, correct volume.

Round-robin scheduling:
    At 3 req/sec with 50 instruments: each instrument gets a fresh candle
    every 50/3 = ~17 seconds.  Instruments with open positions or active
    signals are prioritised and get updated every ~5 seconds (top-10 priority
    queue rotates 3x faster).

Output:
    Candles are stored in DynamoDB (``quantembrace-candle-cache`` table, TTL 2h).
    Strategy engine reads from this cache instead of tick aggregation.
    Phase 2: publishes to Kafka ``candles.1m`` topic instead of DynamoDB.

Separate rate limit:
    Zerodha's historical data API has a dedicated 3 req/sec limit.
    ``_historical_rate_limiter`` inside ``ZerodhaBrokerClient`` enforces it.
    This stream does NOT consume order API tokens — the 10 req/sec order
    budget is unaffected.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.utils.helpers import utc_iso
from shared.zerodha.market_phase import MarketPhase, MarketPhaseGovernor

if TYPE_CHECKING:
    # Import only used for type annotations — never at runtime.
    # Phase 3 fix (P2): removes the module-level cross-service import that gave
    # data_ingestion a runtime dependency on execution_engine.
    # Full fix (moving ZerodhaBrokerClient to shared/) is Phase 5 scope.
    from data_ingestion.features.feature_engine import FeatureEngine
    from data_ingestion.features.feature_writer import FeatureWriter
    from execution_engine.brokers.zerodha_broker import ZerodhaBrokerClient

logger = get_logger(__name__, service_name="data_ingestion")

# ── Constants ─────────────────────────────────────────────────────────────────

# Historical data API rate: 3 req/sec (enforced by _historical_rate_limiter inside broker client)
_HISTORICAL_API_INTERVAL_SECONDS: float = 1.0 / 3.0  # ~333ms between calls

# How many completed 1m candles to fetch per request
# 5 candles = last 5 minutes of confirmed data; covers any gap since last fetch
_CANDLE_LOOKBACK_MINUTES: int = 5

# Candle TTL in DynamoDB (2h — well beyond intraday use)
_CANDLE_TTL_SECONDS: int = 7_200
_CANDLE_CACHE_BUCKET: str = "ACTIVE"
_CANDLE_TTL_ATTRIBUTE: str = "expires_at"

# Phases where candle streaming runs
_ACTIVE_PHASES: frozenset[MarketPhase] = frozenset({
    MarketPhase.PRE_OPEN,     # pre-market warm-up
    MarketPhase.PRE_AUCTION,
    MarketPhase.MARKET_OPEN,
    MarketPhase.NORMAL,
    MarketPhase.PRE_CLOSE,
    MarketPhase.CLOSING,
})

_MAX_CONSECUTIVE_ERRORS: int = 15

# Number of candles kept per instrument for feature computation.
# MACD(12,26,9) needs 35 candles; keep extra headroom.
_FEATURE_HISTORY_SIZE: int = 40


def _consume_background_task_result(task: asyncio.Task) -> None:
    """Mark background task exceptions as observed."""
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("candle_stream.background_task_error")


class CandleData:
    """A single OHLCV candle."""

    __slots__ = (
        "close", "data_quality", "dt", "high", "instrument",
        "interval", "low", "market", "open", "volume",
    )

    def __init__(
        self,
        market: str,
        instrument: str,
        interval: str,
        dt: datetime,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: int,
        data_quality: str = "NORMAL",
    ) -> None:
        self.market       = market
        self.instrument   = instrument
        self.interval     = interval
        self.dt           = dt
        self.open         = open
        self.high         = high
        self.low          = low
        self.close        = close
        self.volume       = volume
        self.data_quality = data_quality  # "NORMAL" | "WARMING_UP" | "GAP" | "STALE"

    def to_dict(self) -> dict:
        return {
            "market":       self.market,
            "instrument":   self.instrument,
            "interval":     self.interval,
            "datetime":     self.dt.isoformat(),
            "open":         self.open,
            "high":         self.high,
            "low":          self.low,
            "close":        self.close,
            "volume":       self.volume,
            "data_quality": self.data_quality,
        }


class IntradayCandleStream:
    """
    Continuous intraday candle fetcher using the Zerodha historical data API.

    Maintains a round-robin queue of instruments and fetches confirmed 1m
    candles at 3 req/sec.  Priority instruments (open positions, active signals)
    rotate 3x faster than the standard queue.

    Args:
        zerodha:            Connected Zerodha broker client.
        instrument_tokens:  Dict mapping ``"{EXCHANGE}:{SYMBOL}"`` →
                            ``instrument_token`` (Zerodha numeric ID).
                            Obtain from ``kite.instruments("NSE")`` at startup.
        dynamo_client:      boto3 DynamoDB client for candle cache writes.
        candle_table:       DynamoDB table name for candle cache.
        phase_governor:     Optional phase governor.
        interval:           Backward-compatible single candle interval string.
                            Ignored when ``intervals`` is provided.
        intervals:          Candle intervals to fetch in one shared round-robin
                            queue. Phase 5 live strategies require ``minute``,
                            ``5minute``, and ``15minute``.
                            Kite accepts: minute, 3minute, 5minute, 10minute,
                            15minute, 30minute, 60minute, day.
        on_candle:          Optional callback ``(CandleData) → None`` called
                            on each new confirmed candle.  Useful for piping
                            directly to strategy engine without DynamoDB hop.
        feature_engine:     Optional ``FeatureEngine`` instance.  When provided
                            (together with ``feature_writer``), computes features
                            on every confirmed candle and writes them to DynamoDB
                            asynchronously via ``asyncio.create_task`` — zero
                            latency impact on the candle pipeline.
        feature_writer:     Optional ``FeatureWriter`` instance.  Paired with
                            ``feature_engine``; ignored if ``feature_engine`` is
                            None.
        adv_20d_cache:      Optional dict mapping ``"{EXCHANGE}:{SYMBOL}"`` to
                            ADV (20-day average daily volume, shares).  Sourced
                            from the prices table via ``candle_prefetch.py``.
                            Used for ``volume_ratio`` feature.
        settings:           App settings.
    """

    def __init__(
        self,
        zerodha: ZerodhaBrokerClient,
        instrument_tokens: dict[str, int],
        dynamo_client: Any,
        candle_table: str,
        phase_governor: MarketPhaseGovernor | None = None,
        interval: str = "minute",
        intervals: list[str] | None = None,
        on_candle: Callable[[CandleData], None] | None = None,
        feature_engine: FeatureEngine | None = None,
        feature_writer: FeatureWriter | None = None,
        adv_20d_cache: dict[str, float] | None = None,
        settings: AppSettings | None = None,
    ) -> None:
        self._zerodha           = zerodha
        self._instrument_tokens = dict(instrument_tokens)  # symbol → token
        self._dynamo            = dynamo_client
        self._candle_table      = candle_table
        self._interval          = interval
        self._intervals         = self._normalise_intervals(intervals or [interval])
        self._on_candle         = on_candle
        self._feature_engine    = feature_engine
        self._feature_writer    = feature_writer
        self._adv_20d_cache     = adv_20d_cache or {}
        self._settings          = settings or get_settings()
        self._phase             = MarketPhase.POST_CLOSE
        self._running           = False
        self._consecutive_errors = 0

        # Rolling candle history for feature computation — per instrument+interval key
        # {"NSE:RELIANCE|minute": deque of CandleData, max _FEATURE_HISTORY_SIZE}
        self._candle_history: dict[str, deque] = {}

        # Round-robin queues
        # Standard queue: all instrument/interval pairs, round-robin. One shared
        # queue preserves the 3 req/sec historical API budget across intervals.
        self._standard_queue: deque[tuple[str, str]] = deque(
            (instrument, candle_interval)
            for instrument in instrument_tokens.keys()
            for candle_interval in self._intervals
        )
        # Priority queue: instruments with open positions / active signals
        # These are fetched first on each cycle, consuming up to 1/3 of budget
        self._priority_instruments: set[str] = set()
        self._priority_queue: deque[tuple[str, str]] = deque()

        # Last known candle per instrument+interval (avoid duplicate writes)
        # {"NSE:RELIANCE|5minute": last_candle_dt_isoformat}
        self._last_candle_dt: dict[str, str] = {}

        # Post-reconnect warm-up: candles fetched before this timestamp are tagged
        # WARMING_UP. Set by notify_reconnect() when the data feed reconnects.
        # None means NORMAL (no active warm-up window).
        self._warm_until: datetime | None = None

        if phase_governor is not None:
            phase_governor.add_listener(self.on_phase_change)

    # ── Phase awareness ────────────────────────────────────────────────────────

    def notify_reconnect(self, warmup_seconds: int = 30) -> None:
        """
        Called by the WebSocket connector after a successful reconnect.

        Marks all candles fetched within ``warmup_seconds`` as WARMING_UP so
        that downstream strategies suppress signals until the feed is stable.
        The warm-up window is intentionally short: the historical API returns
        exchange-validated candles, so one full candle interval is sufficient.
        """
        self._warm_until = datetime.now(tz=UTC) + timedelta(seconds=warmup_seconds)
        logger.info(
            "candle_stream.reconnect_warmup_started",
            warmup_seconds=warmup_seconds,
            warm_until=self._warm_until.isoformat(),
        )

    def _current_data_quality(self) -> str:
        """Return WARMING_UP if inside the post-reconnect window, else NORMAL."""
        if self._warm_until is not None:
            if datetime.now(tz=UTC) < self._warm_until:
                return "WARMING_UP"
            self._warm_until = None  # window expired; clear it
        return "NORMAL"

    def on_phase_change(self, phase_name: str) -> None:
        """Called by ``MarketPhaseGovernor`` on phase transitions."""
        try:
            self._phase = MarketPhase(phase_name)
            active = self._phase in _ACTIVE_PHASES
            logger.info(
                "candle_stream.phase_changed",
                phase=phase_name,
                streaming=active,
            )
        except ValueError:
            logger.warning("candle_stream.unknown_phase", phase_name=phase_name)

    # ── Priority management ───────────────────────────────────────────────────

    def set_priority_instruments(self, instruments: set[str]) -> None:
        """
        Mark instruments as high-priority for more frequent candle updates.

        Called by the strategy engine when a position is opened or an active
        signal is pending for a given instrument.  Priority instruments rotate
        through the fetch queue 3x faster than standard instruments.

        Args:
            instruments: Set of ``"{EXCHANGE}:{SYMBOL}"`` strings.
        """
        self._priority_instruments = {i for i in instruments if i in self._instrument_tokens}
        self._priority_queue = deque(
            (instrument, candle_interval)
            for instrument in self._priority_instruments
            for candle_interval in self._intervals
        )
        logger.debug(
            "candle_stream.priority_updated",
            count=len(self._priority_instruments),
        )

    @staticmethod
    def _normalise_intervals(intervals: list[str]) -> list[str]:
        """Return supported interval strings in stable de-duplicated order."""
        aliases = {
            "1m": "minute",
            "1min": "minute",
            "5m": "5minute",
            "5min": "5minute",
            "15m": "15minute",
            "15min": "15minute",
        }
        seen: set[str] = set()
        result: list[str] = []
        for raw in intervals:
            interval = aliases.get(str(raw).strip().lower(), str(raw).strip())
            if interval and interval not in seen:
                seen.add(interval)
                result.append(interval)
        return result or ["minute"]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the candle stream loop."""
        self._running = True
        logger.info(
            "candle_stream.started",
            instrument_count=len(self._instrument_tokens),
            intervals=self._intervals,
            candle_table=self._candle_table,
        )
        await self._stream_loop()

    async def stop(self) -> None:
        """Stop the candle stream."""
        self._running = False
        logger.info("candle_stream.stopped")

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _stream_loop(self) -> None:
        """
        Continuous candle fetch loop at 3 req/sec.

        Each iteration:
          1. Skip if phase is not active (e.g. POST_CLOSE).
          2. Pick next instrument/interval pair from priority queue (if non-empty) or
             standard round-robin queue.
          3. Fetch last 5 confirmed candles for that instrument/interval.
          4. Write new candles to DynamoDB + call on_candle callback.
          5. Sleep for 333ms (1/3 sec) to maintain 3 req/sec.
        """
        _loop_count = 0
        while self._running:
            _loop_count += 1
            if _loop_count <= 3 or _loop_count % 300 == 0:
                logger.warning(
                    "candle_stream.loop_heartbeat",
                    loop_count=_loop_count,
                    phase=self._phase.value,
                    running=self._running,
                    queue_size=len(self._standard_queue),
                )
            if self._phase not in _ACTIVE_PHASES:
                await asyncio.sleep(5.0)
                continue

            # Pick next instrument/interval pair
            next_item = self._next_instrument_interval()
            if next_item is None:
                await asyncio.sleep(1.0)
                continue
            instrument, candle_interval = next_item

            cycle_start = asyncio.get_event_loop().time()

            try:
                await self._fetch_candles(instrument, candle_interval)
                self._consecutive_errors = 0
            except Exception:
                self._consecutive_errors += 1
                logger.exception(
                    "candle_stream.fetch_error",
                    instrument=instrument,
                    interval=candle_interval,
                    consecutive_errors=self._consecutive_errors,
                )
                if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    logger.critical(
                        "candle_stream.extended_failure",
                        consecutive_errors=self._consecutive_errors,
                        message=(
                            "Candle stream has been failing — strategies "
                            "may be operating on stale candle data."
                        ),
                    )

            elapsed    = asyncio.get_event_loop().time() - cycle_start
            sleep_time = max(0.0, _HISTORICAL_API_INTERVAL_SECONDS - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    def _next_instrument_interval(self) -> tuple[str, str] | None:
        """
        Return the next instrument/interval pair to fetch.

        Priority queue first (instruments with open positions/active signals).
        Falls back to standard round-robin when priority queue is empty.
        Refills priority queue when exhausted.
        """
        if self._priority_queue:
            item = self._priority_queue.popleft()
            # Refill priority queue when exhausted
            if not self._priority_queue and self._priority_instruments:
                self._priority_queue = deque(
                    (instrument, candle_interval)
                    for instrument in self._priority_instruments
                    for candle_interval in self._intervals
                )
            return item

        if self._standard_queue:
            item = self._standard_queue[0]
            self._standard_queue.rotate(-1)   # move to end
            return item

        return None

    def _next_instrument(self) -> str | None:
        """Backward-compatible helper returning only the instrument component."""
        item = self._next_instrument_interval()
        return item[0] if item is not None else None

    # ── Candle fetch + write ──────────────────────────────────────────────────

    async def _fetch_candles(self, instrument: str, candle_interval: str | None = None) -> None:
        """
        Fetch last N confirmed candles for an instrument/interval and persist.

        Uses ``_historical_rate_limiter`` inside the broker client (3 req/sec).
        Does NOT acquire from ``ZerodhaRateLimiter`` — separate budget.
        """
        interval = candle_interval or self._interval
        token = self._instrument_tokens.get(instrument)
        if token is None:
            logger.warning("candle_stream.unknown_token", instrument=instrument)
            return

        now_utc  = datetime.now(tz=UTC)
        from_dt  = now_utc - timedelta(minutes=_CANDLE_LOOKBACK_MINUTES + 2)
        to_dt    = now_utc

        candles_raw = await self._zerodha.get_historical_candles(
            instrument_token=token,
            from_dt=from_dt,
            to_dt=to_dt,
            interval=interval,
        )

        if not candles_raw:
            logger.warning(
                "candle_stream.empty_response",
                instrument=instrument,
                interval=interval,
                from_dt=from_dt.isoformat(),
                to_dt=to_dt.isoformat(),
            )
            return

        new_candles = 0
        last_key = f"{instrument}|{interval}"
        for raw in candles_raw:
            candle = self._parse_candle(instrument, raw, interval=interval)
            if candle is None:
                continue

            dt_iso = candle.dt.isoformat()
            if self._last_candle_dt.get(last_key) == dt_iso:
                continue   # Already processed this candle

            self._last_candle_dt[last_key] = dt_iso
            new_candles += 1

            # Persist to DynamoDB candle cache
            if self._dynamo is not None:
                await self._write_candle(candle)

            # Callback (e.g. strategy engine in-process pipe)
            if self._on_candle is not None:
                try:
                    self._on_candle(candle)
                except Exception:
                    logger.exception("candle_stream.callback_error", instrument=instrument)

            # [Phase 5] Update rolling history and compute features
            self._update_candle_history(instrument, candle)
            self._maybe_compute_features(instrument, candle.interval)

        if new_candles > 0:
            logger.debug(
                "candle_stream.candles_fetched",
                instrument=instrument,
                new_candles=new_candles,
                interval=interval,
            )

    def _parse_candle(
        self,
        instrument_key: str,
        raw: Any,
        *,
        interval: str | None = None,
    ) -> CandleData | None:
        """
        Parse a raw Kite historical_data candle dict into ``CandleData``.

        Args:
            instrument_key: Full ``"{EXCHANGE}:{SYMBOL}"`` key from
                            ``instrument_tokens`` (e.g. ``"NSE:RELIANCE"``).
            raw:            Raw dict from Kite historical_data API.
        """
        try:
            # Split "NSE:RELIANCE" → exchange="NSE", symbol="RELIANCE".
            # Exchanges NSE and BSE are both routed as market="NSE" (Indian market).
            # US instruments (if any) would carry exchange prefix "US" or similar.
            if ":" in instrument_key:
                exchange, symbol = instrument_key.split(":", 1)
            else:
                exchange, symbol = "NSE", instrument_key
            market = "NSE" if exchange in ("NSE", "BSE", "NFO", "BFO") else "US"

            # Kite returns: {"date": datetime, "open": float, "high": float,
            #                "low": float, "close": float, "volume": int}
            dt = raw.get("date")
            if isinstance(dt, str):
                from datetime import datetime
                dt = datetime.fromisoformat(dt)
            if dt is not None and dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            elif dt is not None:
                dt = dt.astimezone(UTC)

            return CandleData(
                market=market,
                instrument=symbol,
                interval=interval or self._interval,
                dt=dt,
                open=float(raw.get("open", 0)),
                high=float(raw.get("high", 0)),
                low=float(raw.get("low", 0)),
                close=float(raw.get("close", 0)),
                volume=int(raw.get("volume", 0)),
                data_quality=self._current_data_quality(),
            )
        except Exception:
            logger.exception("candle_stream.parse_error", instrument=instrument_key)
            return None

    # ── Phase 5: feature computation ──────────────────────────────────────────

    def _update_candle_history(self, instrument: str, candle: CandleData) -> None:
        """
        Append ``candle`` to the rolling history for ``instrument``.

        Maintains at most ``_FEATURE_HISTORY_SIZE`` entries.  History is keyed
        by the full instrument string plus interval (e.g. ``"NSE:RELIANCE|minute"``).
        """
        key = f"{instrument}|{candle.interval}"
        if key not in self._candle_history:
            self._candle_history[key] = deque(maxlen=_FEATURE_HISTORY_SIZE)
        self._candle_history[key].append(candle)

    def _maybe_compute_features(self, instrument: str, interval: str = "minute") -> None:
        """
        If a FeatureEngine and FeatureWriter are wired, compute features for
        ``instrument`` and schedule an async write via ``asyncio.create_task``.

        Fire-and-forget: the write never blocks the candle pipeline.
        """
        if self._feature_engine is None or self._feature_writer is None:
            return

        # The current feature set is 1-minute oriented (VWAP/MACD/RSI online
        # store). Keep 5m/15m candles available for strategies but do not mix
        # their bars into the minute feature window.
        if interval != "minute":
            return

        history = list(self._candle_history.get(f"{instrument}|{interval}", []))
        if not history:
            return

        # Look up ADV from the candle-prefetch cache (keyed by instrument)
        adv_20d: float | None = self._adv_20d_cache.get(instrument)

        try:
            feature_set = self._feature_engine.compute(history, adv_20d=adv_20d)
        except Exception:
            logger.exception("candle_stream.feature_compute_error", instrument=instrument)
            return

        task = asyncio.create_task(self._safe_feature_write(feature_set))
        task.add_done_callback(_consume_background_task_result)

    async def _safe_feature_write(self, feature_set: Any) -> None:
        """Non-fatal wrapper around feature_writer.write()."""
        try:
            await self._feature_writer.write(feature_set)
        except Exception:
            logger.warning("candle_stream.feature_write_error")

    async def _write_candle(self, candle: CandleData) -> None:
        """
        Write a candle to the DynamoDB candle cache.

        Schema matches exactly what ``DynamoCandleConsumer`` expects (ADR-013 §4):
            PK = "{market}#{instrument}#{interval}#{candle_open_time_iso}"
            cache_bucket, market, instrument, interval, candle_open_time as top-level attributes
            expires_at as the DynamoDB TTL attribute configured by Terraform/LocalStack
            No SK — PK is the full composite key, sufficient for point reads and Scan.

        Previous schema (``PK=CANDLE#{instrument}#{interval}``, ``SK=datetime``,
        no ``market``, no ``candle_open_time`` attribute) was incompatible with the
        consumer's FilterExpression on ``candle_open_time`` and its item parser.
        """
        import time
        try:
            candle_open_time_iso = candle.dt.isoformat()
            pk  = f"{candle.market}#{candle.instrument}#{candle.interval}#{candle_open_time_iso}"
            ttl = int(time.time()) + _CANDLE_TTL_SECONDS

            await asyncio.to_thread(
                self._dynamo.put_item,
                TableName=self._candle_table,
                Item={
                    "PK":               {"S": pk},
                    "cache_bucket":     {"S": _CANDLE_CACHE_BUCKET},
                    "market":           {"S": candle.market},
                    "instrument":       {"S": candle.instrument},
                    "interval":         {"S": candle.interval},
                    "candle_open_time": {"S": candle_open_time_iso},
                    "open":             {"N": str(candle.open)},
                    "high":             {"N": str(candle.high)},
                    "low":              {"N": str(candle.low)},
                    "close":            {"N": str(candle.close)},
                    "volume":           {"N": str(candle.volume)},
                    "data_quality":     {"S": candle.data_quality},
                    "captured_at":      {"S": utc_iso()},
                    _CANDLE_TTL_ATTRIBUTE: {"N": str(ttl)},
                },
            )
        except Exception:
            logger.exception(
                "candle_stream.dynamo_write_error",
                instrument=candle.instrument,
                market=candle.market,
            )
