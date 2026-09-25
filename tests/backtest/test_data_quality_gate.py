"""Unit tests for DataQualityGate (Phase 4).

Covers: clean data passes gate · ERROR check blocks run · quarantined source
blocks run · warn-only does not block · CW DQGateFailed emitted on failure ·
load error treated as blocking ERROR · gate integrated into BacktestRunner ·
gate skip (dq_gate=None) does not block · report markdown generated · S3
report upload attempted · no broker calls.

Backtest-only: in-memory fakes for S3, DynamoDB, CW — no real AWS, no broker.

Run:  python -m pytest tests/backtest/test_data_quality_gate.py -q
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.data_quality import QualityIssue, QualityResult, Severity
from backtesting.data_quality_gate import DataQualityGate, GateResult
from backtesting.run_registry import RunSpec
from backtesting.s3_data_catalog import DataCatalog, TrustLevel

IST = "Asia/Kolkata"

# ── helpers / fakes ──────────────────────────────────────────────────────────


def _make_spec(**overrides) -> RunSpec:
    defaults = dict(
        strategy="TestStrategy",
        symbols=["RELIANCE"],
        timeframe="1d",
        start_date="2020-01-01",
        end_date="2020-12-31",
        config_s3_path="s3://quantembrace-backtest-data/configs/test.json",
    )
    defaults.update(overrides)
    return RunSpec(**defaults)


def _daily_parquet_bytes(symbol: str = "RELIANCE", n_rows: int = 5) -> bytes:
    """Build minimal valid Parquet bytes for the gate to load."""
    rows = []
    for i in range(n_rows):
        ts = pd.Timestamp(f"2020-01-{i+2:02d} 15:30:00", tz=IST)
        rows.append({
            "timestamp": ts,
            "symbol": symbol,
            "isin": "INE123",
            "market": "NSE",
            "segment": "EQ",
            "interval": "1d",
            "open": 100.0,
            "high": 102.0,
            "low": 98.0,
            "close": 101.0,
            "volume": 1_000_000,
            "source": "bhavcopy",
            "trust_level": "HIGH",
        })
    buf = io.BytesIO()
    pd.DataFrame(rows).to_parquet(buf, index=False)
    return buf.getvalue()


def _bad_parquet_bytes(symbol: str = "RELIANCE") -> bytes:
    """Parquet with invalid OHLC (high < low) → ERROR on check."""
    buf = io.BytesIO()
    pd.DataFrame([{
        "timestamp": pd.Timestamp("2020-01-02 15:30:00", tz=IST),
        "symbol": symbol,
        "isin": "INE123",
        "market": "NSE",
        "segment": "EQ",
        "interval": "1d",
        "open": 100.0,
        "high": 90.0,   # high < low → invalid OHLC
        "low": 95.0,
        "close": 101.0,
        "volume": 1_000_000,
        "source": "bhavcopy",
        "trust_level": "HIGH",
    }]).to_parquet(buf, index=False)
    return buf.getvalue()


class FakeS3Client:
    """Minimal S3 client that serves specific keys from an in-memory dict.

    Keys in ``objects`` are bare S3 keys (no bucket prefix), matching how
    boto3 returns them in ``list_objects_v2`` and accepts them in ``get_object``.
    """

    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects
        self.uploaded: list[dict] = []

    def list_objects_v2(self, Bucket, Prefix="", **kw):
        keys = [k for k in self._objects if k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def get_object(self, Bucket, Key):
        body = self._objects.get(Key)
        if body is None:
            raise KeyError(f"No such key: {Key}")

        class _Body:
            def __init__(self, b):
                self._b = b
            def read(self):
                return self._b

        return {"Body": _Body(body)}

    def put_object(self, Bucket, Key, Body, **kw):
        self.uploaded.append({"Bucket": Bucket, "Key": Key})
        return {}


class RaisingS3Client:
    """S3 client that raises on list_objects_v2 to simulate S3 outage / access error."""

    def __init__(self):
        self.uploaded: list[dict] = []

    def list_objects_v2(self, Bucket, Prefix="", **kw):
        raise RuntimeError(f"S3 connection refused for bucket={Bucket!r}, prefix={Prefix!r}")

    def get_object(self, Bucket, Key):
        raise RuntimeError(f"S3 connection refused for key={Key!r}")

    def put_object(self, Bucket, Key, Body, **kw):
        self.uploaded.append({"Bucket": Bucket, "Key": Key})
        return {}


class FakeCWClient:
    def __init__(self):
        self.calls: list[str] = []

    def put_metric_data(self, Namespace, MetricData, **kw):
        self.calls.extend(d["MetricName"] for d in MetricData)


def _lake_key(symbol: str, interval: str = "1d", year: int = 2020) -> str:
    """Bare S3 key (no bucket prefix) matching DataCatalog.lake_partition() + a file."""
    return (
        f"lake/ohlcv/market=NSE/segment=EQ/"
        f"symbol={symbol}/interval={interval}/year={year}/part-0.parquet"
    )


def _make_gate(s3_objects: dict[str, bytes], cw_client=None, bucket: str | None = None):
    """Gate backed by FakeS3Client; keys in s3_objects are bare keys (no bucket prefix)."""
    fake_s3 = FakeS3Client(s3_objects)
    from backtesting.cloudwatch_metrics import BacktestCloudWatchEmitter
    cw = BacktestCloudWatchEmitter(cw_client)
    gate = DataQualityGate(
        catalog=DataCatalog(data_base="s3://quantembrace-backtest-data"),
        cw_emitter=cw,
        s3_client=fake_s3,
        results_bucket=bucket,
    )
    return gate, fake_s3


def _make_gate_raising(cw_client=None, bucket: str | None = None):
    """Gate backed by RaisingS3Client — simulates S3 connection failures."""
    raising_s3 = RaisingS3Client()
    from backtesting.cloudwatch_metrics import BacktestCloudWatchEmitter
    cw = BacktestCloudWatchEmitter(cw_client)
    gate = DataQualityGate(
        catalog=DataCatalog(data_base="s3://quantembrace-backtest-data"),
        cw_emitter=cw,
        s3_client=raising_s3,
        results_bucket=bucket,
    )
    return gate, raising_s3


# ── gate unit tests ──────────────────────────────────────────────────────────


def test_clean_data_passes_gate():
    """Valid HIGH-trust daily data → gate PASS."""
    gate, _ = _make_gate({_lake_key("RELIANCE"): _daily_parquet_bytes()})
    result = gate.check(_make_spec(), timeframes=["1d"])
    assert result.passed is True
    assert result.blocked_symbols == []


def test_invalid_ohlc_blocks_gate():
    """Invalid OHLC (high < low) → ERROR → gate FAIL."""
    gate, _ = _make_gate({_lake_key("RELIANCE"): _bad_parquet_bytes()})
    result = gate.check(_make_spec(), timeframes=["1d"])
    assert result.passed is False
    assert len(result.blocked_symbols) == 1
    assert "RELIANCE|1d" in result.blocked_symbols[0]


def test_load_error_blocks_gate():
    """S3 raises on list_objects_v2 → load_error fabricated → gate FAIL (blocking)."""
    gate, _ = _make_gate_raising()
    result = gate.check(_make_spec(), timeframes=["1d"])
    assert result.passed is False
    assert len(result.blocked_symbols) == 1
    qr = next(iter(result.symbol_results.values()))
    assert qr.has("load_error")


def test_multi_symbol_one_fail_blocks_gate():
    """Two symbols; one passes, one has empty data → gate FAIL."""
    gate, _ = _make_gate({
        _lake_key("RELIANCE"): _daily_parquet_bytes("RELIANCE"),
        # TCS key absent → empty_dataset error (still blocks)
    })
    spec = _make_spec(symbols=["RELIANCE", "TCS"])
    result = gate.check(spec, timeframes=["1d"])
    assert result.passed is False
    assert any("TCS" in b for b in result.blocked_symbols)
    assert any("RELIANCE" in k for k in result.symbol_results)


def test_all_symbols_pass_gate():
    """Two symbols, both clean → gate PASS."""
    gate, _ = _make_gate({
        _lake_key("RELIANCE"): _daily_parquet_bytes("RELIANCE"),
        _lake_key("TCS"): _daily_parquet_bytes("TCS"),
    })
    spec = _make_spec(symbols=["RELIANCE", "TCS"])
    result = gate.check(spec, timeframes=["1d"])
    assert result.passed is True
    assert result.blocked_symbols == []


def test_cw_dq_gate_failed_emitted_on_failure():
    """DQGateFailed CW metric is emitted when gate fails."""
    fake_cw = FakeCWClient()
    gate, _ = _make_gate_raising(cw_client=fake_cw)
    result = gate.check(_make_spec(), timeframes=["1d"])
    assert result.passed is False
    assert "DQGateFailed" in fake_cw.calls


def test_cw_not_emitted_on_pass():
    """No DQGateFailed when gate passes (CW should only fire on failures)."""
    fake_cw = FakeCWClient()
    gate, _ = _make_gate(
        {_lake_key("RELIANCE"): _daily_parquet_bytes()},
        cw_client=fake_cw,
    )
    result = gate.check(_make_spec(), timeframes=["1d"])
    assert result.passed is True
    assert "DQGateFailed" not in fake_cw.calls


def test_report_markdown_generated():
    """GateResult.markdown_report is populated regardless of pass/fail."""
    gate, _ = _make_gate_raising()
    result = gate.check(_make_spec(), timeframes=["1d"])
    assert len(result.markdown_report) > 0
    assert "Data Quality Gate" in result.markdown_report


def test_report_uploaded_to_s3_on_failure():
    """When results_bucket is set, gate uploads the markdown report to S3."""
    fake_cw = FakeCWClient()
    gate, raising_s3 = _make_gate_raising(cw_client=fake_cw, bucket="quantembrace-backtest-results")
    result = gate.check(_make_spec(), timeframes=["1d"])
    assert result.passed is False
    assert len(raising_s3.uploaded) == 1
    assert raising_s3.uploaded[0]["Key"].startswith("data-quality/")
    assert result.report_s3_path is not None


def test_report_uploaded_on_pass_too():
    """Report is written to S3 even when gate passes."""
    gate, fake_s3 = _make_gate(
        {_lake_key("RELIANCE"): _daily_parquet_bytes()},
        bucket="quantembrace-backtest-results",
    )
    result = gate.check(_make_spec(), timeframes=["1d"])
    assert result.passed is True
    assert len(fake_s3.uploaded) == 1


def test_gate_result_summary_line():
    """GateResult.summary_line() returns a human-readable string."""
    gate, _ = _make_gate({})
    result = gate.check(_make_spec(), timeframes=["1d"])
    line = result.summary_line()
    assert "FAIL" in line or "PASS" in line


# ── integration with BacktestRunner ─────────────────────────────────────────


def test_runner_blocked_by_dq_gate():
    """BacktestRunner.run() returns FAILED status when DQ gate fails."""
    import sys
    from unittest.mock import MagicMock, AsyncMock

    from backtesting.checkpoint_manager import CheckpointManager
    from backtesting.run_registry import RunRegistry
    from backtesting.runner import BacktestRunner, RunSummary
    from backtesting.replay_engine import DataFrameBarSource, ReplayConfig

    # Faithful fake DynamoDB tables (matching test_runner.py fakes)
    class _RunsTable:
        def __init__(self):
            self.name = "qe-bt-runs"
            self._store: dict = {}
        def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None, **kw):
            self._store[Item["run_id"]] = dict(Item)
        def get_item(self, Key, ConsistentRead=False, **kw):
            it = self._store.get(Key["run_id"])
            return {"Item": dict(it)} if it else {}
        def scan(self, **kw):
            return {"Items": list(self._store.values())}

    class _ChkTable:
        def __init__(self):
            self.name = "qe-bt-checkpoints"
            self._store: dict = {}
        def put_item(self, Item, **kw):
            self._store[(Item["run_id"], Item["partition_id"])] = dict(Item)
        def get_item(self, Key, **kw):
            it = self._store.get((Key["run_id"], Key["partition_id"]))
            return {"Item": dict(it)} if it else {}
        def query(self, KeyConditionExpression=None, ExpressionAttributeValues=None, **kw):
            rid = (ExpressionAttributeValues or {}).get(":rid")
            return {"Items": [dict(v) for (pk, _), v in self._store.items() if pk == rid]}

    registry = RunRegistry(_RunsTable())
    checkpoint = CheckpointManager(_ChkTable())

    # Gate that always fails (no S3 data)
    failing_gate, _ = _make_gate({})

    runner = BacktestRunner(
        registry=registry,
        checkpoint=checkpoint,
        report_writer=MagicMock(),
    )

    spec = _make_spec()
    source = DataFrameBarSource([])
    config = ReplayConfig(symbols=["RELIANCE"], timeframes=["1d"])

    summary = runner.run(
        spec,
        source=source,
        config=config,
        strategy_factory=lambda: MagicMock(),
        dq_gate=failing_gate,
    )

    assert summary.status == "FAILED"
    assert "data_quality_gate_failed" in (summary.error or "")
    # Registry should mark it FAILED
    rec = registry.get_run(spec.run_id())
    assert rec is not None
    assert rec.status == "FAILED"


def test_runner_not_blocked_when_gate_none():
    """BacktestRunner.run() proceeds normally when dq_gate=None."""
    from unittest.mock import MagicMock, AsyncMock

    from backtesting.checkpoint_manager import CheckpointManager
    from backtesting.run_registry import RunRegistry
    from backtesting.runner import BacktestRunner
    from backtesting.replay_engine import Candle, DataFrameBarSource, ReplayConfig

    # Use the same faithful fake tables as test_runner.py
    class _RunsTable:
        def __init__(self):
            self.name = "qe-bt-runs"
            self._store: dict = {}
        def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None, **kw):
            self._store[Item["run_id"]] = dict(Item)
        def get_item(self, Key, ConsistentRead=False, **kw):
            it = self._store.get(Key["run_id"])
            return {"Item": dict(it)} if it else {}
        def scan(self, **kw):
            return {"Items": list(self._store.values())}

    class _ChkTable:
        def __init__(self):
            self.name = "qe-bt-checkpoints"
            self._store: dict = {}
        def put_item(self, Item, **kw):
            self._store[(Item["run_id"], Item["partition_id"])] = dict(Item)
        def get_item(self, Key, **kw):
            it = self._store.get((Key["run_id"], Key["partition_id"]))
            return {"Item": dict(it)} if it else {}
        def query(self, KeyConditionExpression=None, ExpressionAttributeValues=None, **kw):
            rid = (ExpressionAttributeValues or {}).get(":rid")
            return {"Items": [dict(v) for (pk, _), v in self._store.items() if pk == rid]}

    registry = RunRegistry(_RunsTable())
    checkpoint = CheckpointManager(_ChkTable())

    rw = MagicMock()
    rw.write_run.return_value = {"local_dir": "/tmp/x", "files": [], "s3_keys": []}

    def noop_factory():
        s = MagicMock()
        s.name = "noop"
        s.initialize = AsyncMock()
        s.on_bar = AsyncMock()
        s.generate_signal = AsyncMock(return_value=None)
        return s

    candle = Candle(
        symbol="RELIANCE", market="NSE", segment="EQ", interval="1d",
        timestamp=pd.Timestamp("2020-01-02 15:30:00", tz=IST),
        open=100.0, high=102.0, low=98.0, close=101.0, volume=1_000_000,
    )
    source = DataFrameBarSource([candle])
    config = ReplayConfig(symbols=["RELIANCE"], timeframes=["1d"])

    runner = BacktestRunner(
        registry=registry,
        checkpoint=checkpoint,
        report_writer=rw,
    )

    summary = runner.run(
        _make_spec(),
        source=source,
        config=config,
        strategy_factory=noop_factory,
        dq_gate=None,
    )
    assert summary.status == "COMPLETED"


def test_no_broker_calls_in_gate():
    """data_quality_gate.py must not reference any broker API."""
    import backtesting.data_quality_gate as mod
    src = Path(mod.__file__).read_text().lower()
    forbidden = ["kiteconnect", "alpaca", "place_order", "zerodhabroker", "submit_order"]
    present = [t for t in forbidden if t in src]
    assert present == [], f"gate must not reference brokers: {present}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
