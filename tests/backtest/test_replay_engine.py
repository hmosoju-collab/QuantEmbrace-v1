"""Unit tests for the candle replay engine (Phase AWS-BT-4).

Covers: future candle not visible · multi-symbol chronological replay · timeframe
alignment · missing-candle policy · checkpoint resume · deterministic output ·
market-hours filter · no broker calls possible.

Backtest-only: in-memory candle source, fake DynamoDB table — no AWS, no broker.

Run:  python -m pytest tests/backtest/test_replay_engine.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.checkpoint_manager import CheckpointManager  # noqa: E402
import backtesting.replay_engine as replay_engine_module  # noqa: E402
from backtesting.replay_engine import (  # noqa: E402
    Candle,
    CandleReplayEngine,
    DataFrameBarSource,
    MissingCandlePolicy,
    ReplayConfig,
    ReplayError,
)

IST = "Asia/Kolkata"


# ── helpers ──────────────────────────────────────────────────────────────────


def C(symbol: str, ts: str, *, interval: str = "1m", price: float = 100.0) -> Candle:
    return Candle(
        symbol=symbol,
        market="NSE",
        segment="EQ",
        interval=interval,
        timestamp=pd.Timestamp(ts, tz=IST),
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=1000,
    )


class Recorder:
    def __init__(self) -> None:
        self.candles: list[Candle] = []
        self._max_ts: pd.Timestamp | None = None

    def on_candle(self, candle: Candle) -> None:
        # No-lookahead: never delivered a candle out of chronological order.
        if self._max_ts is not None:
            assert candle.timestamp >= self._max_ts, "future candle delivered early"
        self._max_ts = candle.timestamp
        self.candles.append(candle)

    @property
    def timestamps(self) -> list[pd.Timestamp]:
        return [c.timestamp for c in self.candles]


class FakeTable:
    """Composite-key (run_id, partition_id) checkpoint fake supporting query."""

    def __init__(self, name: str = "qe-bt-checkpoints") -> None:
        self.name = name
        self._store: dict[tuple, dict] = {}

    def put_item(self, Item, **kw):  # noqa: N803
        self._store[(Item["run_id"], Item["partition_id"])] = dict(Item)
        return {}

    def get_item(self, Key, **kw):  # noqa: N803
        it = self._store.get((Key["run_id"], Key["partition_id"]))
        return {"Item": dict(it)} if it is not None else {}

    def query(self, KeyConditionExpression=None, ExpressionAttributeValues=None, **kw):  # noqa: N803
        rid = (ExpressionAttributeValues or {}).get(":rid")
        return {"Items": [dict(v) for (pk, _sk), v in self._store.items() if pk == rid]}

    def scan(self, **kw):
        return {"Items": [dict(v) for v in self._store.values()]}


# ── tests ──────────────────────────────────────────────────────────────────────


def test_future_candle_not_visible():
    candles = [C("RELIANCE", f"2020-06-01 10:0{i}:00") for i in range(3)]  # 10:00,10:01,10:02
    cfg = ReplayConfig(as_of=pd.Timestamp("2020-06-01 10:01:00", tz=IST))
    eng = CandleReplayEngine(DataFrameBarSource(candles), cfg)
    out = list(eng.replay_stream())
    seen = [c.timestamp for c in out]
    assert pd.Timestamp("2020-06-01 10:02:00", tz=IST) not in seen  # future hidden
    assert max(seen) == pd.Timestamp("2020-06-01 10:01:00", tz=IST)
    # Streaming delivery is strictly past-only (Recorder asserts monotonicity).
    rec = Recorder()
    CandleReplayEngine(DataFrameBarSource(candles), cfg).replay(rec)
    assert len(rec.candles) == 2


def test_multi_symbol_chronological_replay():
    candles = [
        C("RELIANCE", "2020-06-01 10:00:00"),
        C("RELIANCE", "2020-06-01 10:02:00"),
        C("TCS", "2020-06-01 10:01:00"),
        C("TCS", "2020-06-01 10:03:00"),
    ]
    eng = CandleReplayEngine(DataFrameBarSource(candles), ReplayConfig())
    out = list(eng.replay_stream())
    ts = [c.timestamp for c in out]
    assert ts == sorted(ts)  # globally chronological
    assert [c.symbol for c in out] == ["RELIANCE", "TCS", "RELIANCE", "TCS"]


def test_timeframe_alignment():
    candles = [C("RELIANCE", f"2020-06-01 09:1{m}:00") for m in range(6, 10)]  # 1m 09:16-09:19
    candles.append(C("RELIANCE", "2020-06-01 09:20:00", interval="1m"))
    candles.append(C("RELIANCE", "2020-06-01 09:20:00", interval="5m"))
    eng = CandleReplayEngine(DataFrameBarSource(candles), ReplayConfig(timeframes=["1m", "5m"]))
    out = list(eng.replay_stream())
    ts = [c.timestamp for c in out]
    assert ts == sorted(ts)
    # At the shared 09:20 close, the finer 1m candle precedes the 5m candle.
    last_two = out[-2:]
    assert last_two[0].interval == "1m" and last_two[1].interval == "5m"
    assert last_two[0].timestamp == last_two[1].timestamp


def test_missing_candle_handled_per_config():
    candles = [C("RELIANCE", "2020-06-01 10:00:00"), C("RELIANCE", "2020-06-01 10:05:00")]
    # ERROR policy → raises.
    eng_err = CandleReplayEngine(
        DataFrameBarSource(candles),
        ReplayConfig(missing_candle_policy=MissingCandlePolicy.ERROR),
    )
    with pytest.raises(ReplayError):
        list(eng_err.replay_stream())
    # SKIP policy → continues, gap recorded (4 missing 1m candles).
    eng_skip = CandleReplayEngine(
        DataFrameBarSource(candles),
        ReplayConfig(missing_candle_policy=MissingCandlePolicy.SKIP),
    )
    rec = Recorder()
    result = eng_skip.replay(rec)
    assert len(rec.candles) == 2
    assert result.gaps and result.gaps[0].missing == 4


def test_checkpoint_resume_continues_correctly():
    candles = [
        C("RELIANCE", "2020-06-01 10:00:00"),
        C("RELIANCE", "2021-06-01 10:00:00"),
    ]
    cfg = ReplayConfig(partition_by="symbol_year")
    cm = CheckpointManager(FakeTable("qe-bt-checkpoints"))
    run_id = "bt_resume"
    cm.init_checkpoint(run_id)
    cm.checkpoint_partition(run_id, "RELIANCE|1m|2020", last_processed_timestamp="2020-06-01T10:00:00+05:30")

    rec = Recorder()
    result = CandleReplayEngine(DataFrameBarSource(candles), cfg).replay(
        rec, run_id=run_id, checkpoint=cm
    )
    # Only the pending 2021 partition runs; 2020 is skipped.
    assert result.partitions_processed == ["RELIANCE|1m|2021"]
    assert "RELIANCE|1m|2020" in result.partitions_skipped
    assert [c.timestamp.year for c in rec.candles] == [2021]
    assert cm.completed_partitions(run_id) == {"RELIANCE|1m|2020", "RELIANCE|1m|2021"}


def test_deterministic_output_for_same_input():
    candles = [
        C("TCS", "2020-06-01 10:01:00", interval="5m"),
        C("RELIANCE", "2020-06-01 10:00:00"),
        C("RELIANCE", "2020-06-01 10:01:00"),
        C("TCS", "2020-06-01 10:00:00"),
    ]
    eng1 = CandleReplayEngine(DataFrameBarSource(candles), ReplayConfig(timeframes=["1m", "5m"]))
    eng2 = CandleReplayEngine(DataFrameBarSource(list(candles)), ReplayConfig(timeframes=["1m", "5m"]))
    sig1 = [(c.symbol, c.interval, c.timestamp) for c in eng1.replay_stream()]
    sig2 = [(c.symbol, c.interval, c.timestamp) for c in eng2.replay_stream()]
    assert sig1 == sig2


def test_market_hours_filter_works():
    candles = [
        C("RELIANCE", "2020-06-01 08:00:00"),  # pre-open
        C("RELIANCE", "2020-06-01 10:00:00"),  # in session
        C("RELIANCE", "2020-06-01 16:00:00"),  # post-close
    ]
    on = CandleReplayEngine(DataFrameBarSource(candles), ReplayConfig(market_hours_filter=True))
    out_on = list(on.replay_stream())
    assert [c.timestamp.hour for c in out_on] == [10]
    off = CandleReplayEngine(DataFrameBarSource(candles), ReplayConfig(market_hours_filter=False))
    out_off = list(off.replay_stream())
    assert sorted(c.timestamp.hour for c in out_off) == [8, 10, 16]


def test_no_broker_calls_possible():
    src = Path(replay_engine_module.__file__).read_text().lower()
    forbidden = [
        "kiteconnect",
        "kite_connect",
        "alpaca",
        "place_order",
        "zerodhabroker",
        "broker_client",
        "import boto3",
        "submit_order",
    ]
    present = [tok for tok in forbidden if tok in src]
    assert present == [], f"replay engine must not reference brokers: {present}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
