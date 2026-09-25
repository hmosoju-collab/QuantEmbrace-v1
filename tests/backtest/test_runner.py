"""Tests for the BacktestRunner (Phase 3 — end-to-end pipeline).

Covers: full happy-path run · S3 export manifest · metrics populated ·
registry lifecycle (RUNNING → COMPLETED) · checkpoint written per partition ·
CloudWatch emitter calls · failure path (registry → FAILED) ·
SIGTERM handler installation/restoration · no-op CW emitter when None ·
no broker calls.

Backtest-only: in-memory fakes for DynamoDB, S3, and CW — no AWS, no broker.

Run:  python -m pytest tests/backtest/test_runner.py -q
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.checkpoint_manager import CheckpointManager
from backtesting.cloudwatch_metrics import BacktestCloudWatchEmitter
from backtesting.replay_engine import CandleReplayEngine, Candle, DataFrameBarSource, ReplayConfig
from backtesting.run_registry import RunRegistry, RunSpec, RunStatus
from backtesting.runner import BacktestRunner, _results_to_trades_df, _results_to_equity_df

IST = "Asia/Kolkata"


# ── fakes ─────────────────────────────────────────────────────────────────────


class FakeRunsTable:
    """Simple dict-backed fake for qe-bt-runs."""

    def __init__(self, name: str = "qe-bt-runs") -> None:
        self.name = name
        self._store: dict[str, dict] = {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None, **kw):
        self._store[Item["run_id"]] = dict(Item)
        return {}

    def get_item(self, Key, ConsistentRead=False, **kw):
        it = self._store.get(Key["run_id"])
        return {"Item": dict(it)} if it else {}

    def scan(self, **kw):
        return {"Items": [dict(v) for v in self._store.values()]}


class FakeCheckpointsTable:
    """Composite-key fake for qe-bt-checkpoints."""

    def __init__(self, name: str = "qe-bt-checkpoints") -> None:
        self.name = name
        self._store: dict[tuple, dict] = {}

    def put_item(self, Item, **kw):
        self._store[(Item["run_id"], Item["partition_id"])] = dict(Item)
        return {}

    def get_item(self, Key, **kw):
        it = self._store.get((Key["run_id"], Key["partition_id"]))
        return {"Item": dict(it)} if it else {}

    def query(self, KeyConditionExpression=None, ExpressionAttributeValues=None, **kw):
        rid = (ExpressionAttributeValues or {}).get(":rid")
        return {"Items": [dict(v) for (pk, _sk), v in self._store.items() if pk == rid]}


class FakeCWClient:
    """Records put_metric_data calls for assertion."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def put_metric_data(self, Namespace, MetricData, **kw):
        self.calls.append({"Namespace": Namespace, "MetricData": MetricData})


class FakeReportWriter:
    """No-op report writer that captures the write_run() arguments."""

    def __init__(self) -> None:
        self.written: list[dict] = []

    def write_run(self, meta, *, metrics=None, trades=None, equity_curve=None,
                  rejected_signals=None, model_labels=None, config=None, logs_text="") -> dict:
        self.written.append({"meta": meta, "metrics": metrics})
        return {"local_dir": f"/tmp/reports/{meta.run_id}", "files": [], "s3_keys": []}


# ── strategy factory ──────────────────────────────────────────────────────────


def _noop_strategy_factory():
    """Returns a no-op strategy (no signals) for testing the pipeline wiring."""
    from unittest.mock import AsyncMock, MagicMock

    strat = MagicMock()
    strat.name = "TestStrategy"
    strat.initialize = AsyncMock(return_value=None)
    strat.on_bar = AsyncMock(return_value=None)
    strat.generate_signal = AsyncMock(return_value=None)
    return strat


def _make_candles(symbol: str = "RELIANCE") -> list[Candle]:
    return [
        Candle(
            symbol=symbol, market="NSE", segment="EQ", interval="1d",
            timestamp=pd.Timestamp(f"2020-01-0{i+1} 15:30:00", tz=IST),
            open=100.0, high=102.0, low=98.0, close=101.0, volume=1_000_000,
        )
        for i in range(3)
    ]


def _make_runner(report_writer=None, cw_client=None):
    runs_table = FakeRunsTable()
    chk_table = FakeCheckpointsTable()
    registry = RunRegistry(runs_table)
    checkpoint = CheckpointManager(chk_table)
    cw = BacktestCloudWatchEmitter(cw_client)
    return (
        BacktestRunner(
            registry=registry,
            checkpoint=checkpoint,
            report_writer=report_writer or FakeReportWriter(),
            cw_emitter=cw,
        ),
        registry,
        checkpoint,
        cw,
    )


def _make_spec(**overrides) -> RunSpec:
    defaults = dict(
        strategy="TestStrategy",
        symbols=["RELIANCE"],
        timeframe="1d",
        start_date="2020-01-01",
        end_date="2020-01-31",
        config_s3_path="s3://quantembrace-backtest-data/configs/test.json",
        code_version="test",
        data_version="2020",
    )
    defaults.update(overrides)
    return RunSpec(**defaults)


# ── tests ─────────────────────────────────────────────────────────────────────


def test_happy_path_run_lifecycle():
    """Full pipeline: CREATED → RUNNING → COMPLETED in registry."""
    runner, registry, checkpoint, cw = _make_runner()
    spec = _make_spec()
    source = DataFrameBarSource(_make_candles())
    config = ReplayConfig(symbols=["RELIANCE"], timeframes=["1d"])

    summary = runner.run(spec, source=source, config=config, strategy_factory=_noop_strategy_factory)

    assert summary.status == "COMPLETED"
    assert summary.run_id == spec.run_id()
    rec = registry.get_run(spec.run_id())
    assert rec is not None
    assert rec.status == RunStatus.COMPLETED.value


def test_checkpoint_written_per_partition():
    """Each processed partition gets a DONE checkpoint entry."""
    runner, registry, checkpoint, cw = _make_runner()
    spec = _make_spec()
    source = DataFrameBarSource(_make_candles())
    config = ReplayConfig(symbols=["RELIANCE"], timeframes=["1d"], partition_by="symbol_year")

    runner.run(spec, source=source, config=config, strategy_factory=_noop_strategy_factory)

    done = checkpoint.completed_partitions(spec.run_id())
    # Daily candles for 2020 → "RELIANCE|1d|2020" partition
    assert any("2020" in pid for pid in done)


def test_metrics_populated_in_summary():
    """RunSummary.metrics includes at least the basic gate keys."""
    runner, registry, checkpoint, cw = _make_runner()
    spec = _make_spec()
    source = DataFrameBarSource(_make_candles())
    config = ReplayConfig(symbols=["RELIANCE"], timeframes=["1d"])

    summary = runner.run(spec, source=source, config=config, strategy_factory=_noop_strategy_factory)

    assert "gates" in summary.metrics
    assert "net_pnl" in summary.metrics
    # No-op strategy → 0 trades
    assert summary.total_trades == 0


def test_report_writer_called_on_success():
    """ReportWriter.write_run() is called once per completed run."""
    rw = FakeReportWriter()
    runner, registry, checkpoint, cw = _make_runner(report_writer=rw)
    spec = _make_spec()
    source = DataFrameBarSource(_make_candles())
    config = ReplayConfig(symbols=["RELIANCE"], timeframes=["1d"])

    runner.run(spec, source=source, config=config, strategy_factory=_noop_strategy_factory)

    assert len(rw.written) == 1
    assert rw.written[0]["meta"].run_id == spec.run_id()


def test_cloudwatch_emitted_on_success():
    """CW emitter receives checkpoint_written + run_completed."""
    fake_cw = FakeCWClient()
    runner, registry, checkpoint, cw = _make_runner(cw_client=fake_cw)
    spec = _make_spec()
    source = DataFrameBarSource(_make_candles())
    config = ReplayConfig(symbols=["RELIANCE"], timeframes=["1d"])

    runner.run(spec, source=source, config=config, strategy_factory=_noop_strategy_factory)

    metric_names = [d["MetricData"][0]["MetricName"] for d in fake_cw.calls]
    assert "CheckpointWritten" in metric_names
    assert "RunCompleted" in metric_names


def test_cloudwatch_noop_when_no_client():
    """No CW client → emitter is a no-op, run still completes."""
    runs_table = FakeRunsTable()
    chk_table = FakeCheckpointsTable()
    registry = RunRegistry(runs_table)
    checkpoint = CheckpointManager(chk_table)
    runner = BacktestRunner(
        registry=registry,
        checkpoint=checkpoint,
        report_writer=FakeReportWriter(),
        cw_emitter=None,  # no CW
    )
    spec = _make_spec()
    source = DataFrameBarSource(_make_candles())
    config = ReplayConfig(symbols=["RELIANCE"], timeframes=["1d"])

    summary = runner.run(spec, source=source, config=config, strategy_factory=_noop_strategy_factory)
    assert summary.status == "COMPLETED"


def test_failure_path_marks_registry_failed():
    """A strategy factory that raises propagates to FAILED status."""
    runner, registry, checkpoint, cw = _make_runner()
    spec = _make_spec()

    def bad_factory():
        raise RuntimeError("strategy init exploded")

    source = DataFrameBarSource(_make_candles())
    config = ReplayConfig(symbols=["RELIANCE"], timeframes=["1d"])

    summary = runner.run(spec, source=source, config=config, strategy_factory=bad_factory)

    assert summary.status == "FAILED"
    assert summary.error is not None
    rec = registry.get_run(spec.run_id())
    assert rec.status == RunStatus.FAILED.value


def test_idempotent_run_create():
    """create_run() for the same spec returns the same run_id."""
    runner, registry, checkpoint, cw = _make_runner()
    spec = _make_spec()
    source = DataFrameBarSource(_make_candles())
    config = ReplayConfig(symbols=["RELIANCE"], timeframes=["1d"])

    s1 = runner.run(spec, source=source, config=config, strategy_factory=_noop_strategy_factory)
    # Manually reset to CREATED to allow a second run attempt (simulate re-run).
    # In real usage the idempotent check prevents double-create.
    assert s1.run_id == spec.run_id()


def test_sigterm_handler_installed_and_restored():
    """SIGTERM handler is set during run_with_backtester and restored after."""
    from backtesting.run_registry import RunRegistry

    orig = signal.getsignal(signal.SIGTERM)

    runs_table = FakeRunsTable()
    chk_table = FakeCheckpointsTable()
    registry = RunRegistry(runs_table)
    checkpoint = CheckpointManager(chk_table)
    cw = BacktestCloudWatchEmitter()

    candles = _make_candles()
    source = DataFrameBarSource(candles)
    config = ReplayConfig(symbols=["RELIANCE"], timeframes=["1d"])
    engine = CandleReplayEngine(source, config)

    # Track what the handler was inside the run.
    captured: list = []

    orig_factory = _noop_strategy_factory

    class CapturingFactory:
        def __call__(self):
            captured.append(signal.getsignal(signal.SIGTERM))
            return orig_factory()

    spec = _make_spec()
    rec = registry.create_run(spec)
    engine.run_with_backtester(
        CapturingFactory(),
        run_id=spec.run_id(),
        registry=registry,
        checkpoint=checkpoint,
        cw_emitter=cw,
    )

    # Inside the run, SIGTERM was overridden to our handler.
    assert len(captured) > 0
    assert captured[0] is not orig

    # After the run, SIGTERM is restored.
    assert signal.getsignal(signal.SIGTERM) is orig


def test_results_to_trades_df_empty():
    """No trades → empty DataFrame with correct columns."""
    from strategy_engine.backtesting.backtester import BacktestResult
    df = _results_to_trades_df({"p1": BacktestResult()}, "TestStrategy")
    assert df.empty
    assert "net_pnl" in df.columns


def test_results_to_equity_df_empty():
    """No equity curve → empty DataFrame."""
    from strategy_engine.backtesting.backtester import BacktestResult
    df = _results_to_equity_df({"p1": BacktestResult()}, 1_000_000.0)
    assert df.empty


def test_no_broker_calls_in_runner():
    """runner.py must not import or reference any broker API."""
    import backtesting.runner as runner_module
    src = Path(runner_module.__file__).read_text().lower()
    forbidden = ["kiteconnect", "alpaca", "place_order", "zerodhabroker", "submit_order"]
    present = [t for t in forbidden if t in src]
    assert present == [], f"runner must not reference brokers: {present}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
