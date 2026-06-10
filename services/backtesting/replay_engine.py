"""Candle replay engine for the QuantEmbrace backtesting lab.

Replays historical candles **candle-by-candle in strict chronological order** so
strategies see only the current and past — never the future. It extends the
existing engine (`services/strategy_engine/backtesting/backtester.py`) rather than
replacing it: `Candle.to_bar()` yields the engine's `Bar`, and
`run_with_backtester()` feeds ordered bars to the existing `Backtester`.

Capabilities:
    * candle-by-candle replay, multiple symbols, multiple timeframes
    * preserved chronological order + deterministic tie-breaking
    * no-lookahead enforcement (monotonic delivery + optional ``as_of`` cutoff)
    * batching by ``symbol`` / ``symbol_year`` / ``symbol_date`` for AWS scale
    * checkpoint/resume via ``CheckpointManager`` and lifecycle via ``RunRegistry``
    * market-hours filter (09:15–15:30 IST) + optional trading calendar
    * configurable missing-candle policy

Backtest-only: **no broker APIs are imported or called** anywhere in this module.
The engine only reads candle data and hands it to a consumer.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Protocol

import pandas as pd

from backtesting.data_loader import IST

# NSE continuous session (IST).
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

_INTERVAL_MINUTES: dict[str, int] = {"1m": 1, "5m": 5, "15m": 15, "1d": 1440}


class MissingCandlePolicy(str, Enum):
    SKIP = "SKIP"    # ignore gaps, continue
    WARN = "WARN"    # record gaps, continue
    ERROR = "ERROR"  # raise on any intraday gap


class ReplayError(Exception):
    """Raised on ordering violations or (under ERROR policy) missing candles."""


# ── data model ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candle:
    symbol: str
    market: str
    segment: str
    interval: str
    timestamp: pd.Timestamp  # tz-aware (IST) bar close time
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def interval_minutes(self) -> int:
        return _INTERVAL_MINUTES.get(self.interval, 1)

    def to_bar(self) -> Any:
        """Convert to the existing engine's ``Bar`` (lazy import — no hard dep)."""
        from strategy_engine.strategies.base_strategy import Bar

        return Bar(
            symbol=self.symbol,
            market=self.market,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=int(self.volume),
            timestamp=self.timestamp.to_pydatetime(),
            interval=self.interval,
        )


@dataclass(frozen=True)
class Gap:
    symbol: str
    interval: str
    prev_ts: pd.Timestamp
    next_ts: pd.Timestamp
    missing: int


@dataclass
class ReplayConfig:
    symbols: list[str] | None = None
    timeframes: list[str] | None = None
    date_from: date | None = None
    date_to: date | None = None
    market_hours_filter: bool = True
    calendar: set[date] | None = None  # allowed trading dates; None = no calendar check
    missing_candle_policy: MissingCandlePolicy = MissingCandlePolicy.SKIP
    as_of: datetime | pd.Timestamp | None = None  # no candle with ts > as_of is visible
    partition_by: str = "symbol_year"  # symbol | symbol_year | symbol_date


@dataclass
class ReplayResult:
    candles_replayed: int = 0
    partitions_processed: list[str] = field(default_factory=list)
    partitions_skipped: list[str] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    last_timestamp: str | None = None


class CandleConsumer(Protocol):
    def on_candle(self, candle: Candle) -> None: ...


# ── bar sources ─────────────────────────────────────────────────────────────────


class BarSource(Protocol):
    def partitions(self, config: ReplayConfig) -> list[str]: ...
    def load_partition(self, partition_id: str, config: ReplayConfig) -> list[Candle]: ...


def _partition_id(symbol: str, interval: str, key: str | None, partition_by: str) -> str:
    if partition_by == "symbol":
        return f"{symbol}|{interval}"
    return f"{symbol}|{interval}|{key}"


def _parse_partition(pid: str) -> tuple[str, str, str | None]:
    parts = pid.split("|")
    if len(parts) == 2:
        return parts[0], parts[1], None
    return parts[0], parts[1], parts[2]


class DataFrameBarSource:
    """In-memory source backed by a list of ``Candle`` (tests / single-process).

    Accepts a list of candles or a canonical DataFrame (see ``data_loader``).
    """

    def __init__(self, candles: Iterable[Candle]) -> None:
        self._candles = list(candles)

    @classmethod
    def from_dataframe(cls, df: pd.DataFrame) -> DataFrameBarSource:
        out: list[Candle] = []
        for r in df.itertuples():
            out.append(
                Candle(
                    symbol=getattr(r, "symbol"),
                    market=getattr(r, "market", "NSE"),
                    segment=getattr(r, "segment", "EQ"),
                    interval=getattr(r, "interval"),
                    timestamp=pd.Timestamp(getattr(r, "timestamp")),
                    open=float(getattr(r, "open")),
                    high=float(getattr(r, "high")),
                    low=float(getattr(r, "low")),
                    close=float(getattr(r, "close")),
                    volume=float(getattr(r, "volume", 0) or 0),
                )
            )
        return cls(out)

    def _key_for(self, c: Candle, partition_by: str) -> str | None:
        if partition_by == "symbol":
            return None
        if partition_by == "symbol_date":
            return c.timestamp.tz_convert(IST).date().isoformat()
        return str(c.timestamp.tz_convert(IST).year)  # symbol_year

    def partitions(self, config: ReplayConfig) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for c in sorted(self._candles, key=_sort_key):
            if not _passes_scope(c, config):
                continue
            pid = _partition_id(c.symbol, c.interval, self._key_for(c, config.partition_by),
                                config.partition_by)
            if pid not in seen:
                seen.add(pid)
                ids.append(pid)
        return ids

    def load_partition(self, partition_id: str, config: ReplayConfig) -> list[Candle]:
        symbol, interval, key = _parse_partition(partition_id)
        out: list[Candle] = []
        for c in self._candles:
            if c.symbol != symbol or c.interval != interval:
                continue
            if key is not None and self._key_for(c, config.partition_by) != key:
                continue
            out.append(c)
        return out


class ParquetBarSource:
    """Lake-backed source: reads partitions from the curated Parquet lake / S3.

    Thin wrapper over ``data_loader.load_candles`` + ``s3_data_catalog``. Used in
    production; tests use ``DataFrameBarSource``.
    """

    def __init__(self, base_path: str, *, source_name: str = "bhavcopy", s3_client: Any = None) -> None:
        self._base = base_path.rstrip("/")
        self._source_name = source_name
        self._s3_client = s3_client

    def partitions(self, config: ReplayConfig) -> list[str]:
        symbols = config.symbols or []
        timeframes = config.timeframes or ["1d"]
        years: list[int] = []
        if config.date_from and config.date_to:
            years = list(range(config.date_from.year, config.date_to.year + 1))
        ids: list[str] = []
        for sym in symbols:
            for iv in timeframes:
                if config.partition_by == "symbol" or not years:
                    ids.append(_partition_id(sym, iv, None, "symbol"))
                else:
                    for y in years:
                        ids.append(_partition_id(sym, iv, str(y), "symbol_year"))
        return ids

    def load_partition(self, partition_id: str, config: ReplayConfig) -> list[Candle]:
        from backtesting.data_loader import load_candles

        symbol, interval, key = _parse_partition(partition_id)
        path = f"{self._base}/market=NSE/segment=EQ/symbol={symbol}/interval={interval}/"
        if key is not None:
            path += f"year={key}/"
        res = load_candles(
            path,
            symbol=symbol,
            interval=interval,
            date_from=config.date_from,
            date_to=config.date_to,
            source_name=self._source_name,
            s3_client=self._s3_client,
        )
        return DataFrameBarSource.from_dataframe(res.df)._candles


# ── ordering / filtering helpers ─────────────────────────────────────────────────


def _sort_key(c: Candle) -> tuple:
    # Deterministic: time, then finer interval first, then symbol.
    return (c.timestamp.value, c.interval_minutes, c.symbol)


def _passes_scope(c: Candle, config: ReplayConfig) -> bool:
    if config.symbols and c.symbol not in config.symbols:
        return False
    if config.timeframes and c.interval not in config.timeframes:
        return False
    ts_ist = c.timestamp.tz_convert(IST) if c.timestamp.tz is not None else c.timestamp
    d = ts_ist.date()
    if config.date_from and d < config.date_from:
        return False
    if config.date_to and d > config.date_to:
        return False
    if config.calendar is not None and d not in config.calendar:
        return False
    if config.as_of is not None and c.timestamp > pd.Timestamp(config.as_of):
        return False  # no-lookahead: future candle not visible
    if config.market_hours_filter and c.interval != "1d":
        tod = ts_ist.time()
        if tod < MARKET_OPEN or tod > MARKET_CLOSE:
            return False
    return True


def _detect_gaps(sorted_candles: list[Candle]) -> list[Gap]:
    gaps: list[Gap] = []
    by_stream: dict[tuple[str, str], list[Candle]] = {}
    for c in sorted_candles:
        by_stream.setdefault((c.symbol, c.interval), []).append(c)
    for (sym, iv), group in by_stream.items():
        if iv == "1d":
            continue  # daily gaps are a calendar concern, not intraday
        delta = pd.Timedelta(minutes=_INTERVAL_MINUTES.get(iv, 1))
        for prev, cur in zip(group, group[1:]):
            same_day = prev.timestamp.tz_convert(IST).date() == cur.timestamp.tz_convert(IST).date()
            if same_day and (cur.timestamp - prev.timestamp) > delta:
                missing = int((cur.timestamp - prev.timestamp) / delta) - 1
                gaps.append(Gap(sym, iv, prev.timestamp, cur.timestamp, missing))
    return gaps


def _prepare(candles: list[Candle], config: ReplayConfig) -> tuple[list[Candle], list[Gap]]:
    scoped = [c for c in candles if _passes_scope(c, config)]
    ordered = sorted(scoped, key=_sort_key)
    gaps = _detect_gaps(ordered)
    if gaps and config.missing_candle_policy is MissingCandlePolicy.ERROR:
        g = gaps[0]
        raise ReplayError(
            f"Missing {g.missing} {g.interval} candle(s) for {g.symbol} between "
            f"{g.prev_ts} and {g.next_ts} (missing_candle_policy=ERROR)."
        )
    return ordered, gaps


# ── engine ──────────────────────────────────────────────────────────────────────


class CandleReplayEngine:
    """Replays candles in chronological order with no-lookahead, sharding,
    checkpoint/resume, and registry lifecycle wiring."""

    def __init__(self, source: BarSource, config: ReplayConfig | None = None) -> None:
        self._source = source
        self._config = config or ReplayConfig()

    def plan(self) -> list[str]:
        """Ordered list of partitions (shards) to replay."""
        return self._source.partitions(self._config)

    def replay_stream(self) -> Iterator[Candle]:
        """Yield every in-scope candle once, globally chronological, no-lookahead.

        Single-process portfolio view (all symbols/timeframes time-aligned). A
        consumer iterating this sees exactly one candle at a time, past-only.
        """
        all_candles: list[Candle] = []
        for pid in self.plan():
            all_candles.extend(self._source.load_partition(pid, self._config))
        ordered, _gaps = _prepare(all_candles, self._config)
        yield from self._iter_monotonic(ordered)

    def replay(
        self,
        consumer: CandleConsumer,
        *,
        run_id: str | None = None,
        registry: Any = None,
        checkpoint: Any = None,
    ) -> ReplayResult:
        """Drive ``consumer`` partition-by-partition, checkpointing each shard.

        With ``checkpoint`` + ``run_id``, completed partitions are skipped (resume).
        With ``registry`` + ``run_id``, the run is marked RUNNING → COMPLETED, or
        FAILED on error.
        """
        if registry is not None and run_id:
            registry.mark_running(run_id)
        result = ReplayResult()
        try:
            plan = self.plan()
            if checkpoint is not None and run_id:
                pending = checkpoint.pending_partitions(run_id, plan)
                result.partitions_skipped = [p for p in plan if p not in pending]
            else:
                pending = plan
            for pid in pending:
                ordered, gaps = _prepare(self._source.load_partition(pid, self._config), self._config)
                result.gaps.extend(gaps)
                last_ts: pd.Timestamp | None = None
                for candle in self._iter_monotonic(ordered):
                    consumer.on_candle(candle)
                    last_ts = candle.timestamp
                    result.candles_replayed += 1
                if checkpoint is not None and run_id and last_ts is not None:
                    checkpoint.checkpoint_partition(
                        run_id, pid, last_processed_timestamp=last_ts.isoformat()
                    )
                result.partitions_processed.append(pid)
                result.last_timestamp = last_ts.isoformat() if last_ts is not None else result.last_timestamp
            if registry is not None and run_id:
                registry.mark_completed(run_id)
            return result
        except Exception as exc:
            if registry is not None and run_id:
                registry.mark_failed(run_id, str(exc))
            raise

    def run_with_backtester(
        self,
        strategy_factory: Any,
        *,
        run_id: str | None = None,
        registry: Any = None,
        checkpoint: Any = None,
        backtester_kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reuse the existing ``Backtester`` per partition over ordered bars.

        ``strategy_factory`` returns a fresh strategy per partition so per-symbol
        state never bleeds across shards. ``backtester_kwargs`` are forwarded to the
        ``Backtester`` (e.g. ``slippage_bps``, ``commission_pct``) so cost/slippage
        models flow end-to-end. Returns ``{partition_id: BacktestResult}``.
        """
        import asyncio

        from strategy_engine.backtesting.backtester import Backtester

        bt_kwargs = backtester_kwargs or {}
        if registry is not None and run_id:
            registry.mark_running(run_id)
        results: dict[str, Any] = {}
        try:
            plan = self.plan()
            pending = (
                checkpoint.pending_partitions(run_id, plan)
                if (checkpoint is not None and run_id)
                else plan
            )
            for pid in pending:
                ordered, _gaps = _prepare(self._source.load_partition(pid, self._config), self._config)
                bars = [c.to_bar() for c in self._iter_monotonic(ordered)]
                res = asyncio.run(Backtester(strategy=strategy_factory(), **bt_kwargs).run(bars))
                results[pid] = res
                if checkpoint is not None and run_id and bars:
                    checkpoint.checkpoint_partition(
                        run_id,
                        pid,
                        last_processed_timestamp=bars[-1].timestamp.isoformat(),
                        partial_metrics={"trades": res.total_trades},
                    )
            if registry is not None and run_id:
                registry.mark_completed(run_id)
            return results
        except Exception as exc:
            if registry is not None and run_id:
                registry.mark_failed(run_id, str(exc))
            raise

    @staticmethod
    def _iter_monotonic(ordered: list[Candle]) -> Iterator[Candle]:
        """Yield candles asserting non-decreasing timestamps (defensive no-lookahead)."""
        prev: pd.Timestamp | None = None
        for c in ordered:
            if prev is not None and c.timestamp < prev:
                raise ReplayError(
                    f"Chronological order violation: {c.timestamp} after {prev}."
                )
            prev = c.timestamp
            yield c
