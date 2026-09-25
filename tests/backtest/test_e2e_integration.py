"""End-to-end integration smoke test for the QuantEmbrace backtesting lab (Phase AWS-BT-12).

This is the **cross-layer** test: it wires the real production classes of every
lab layer together and drives one flow through all of them —

    DataCatalog/BarSource → CandleReplayEngine → Backtester (real) →
    StrategyAdapter (real production strategy) → BacktestRunner → metrics_engine →
    ReportWriter (real, local) → ModelDatasetBuilder → GenAI analyst (stubbed LLM)

and then asserts the lab's hard isolation invariants hold when everything is
wired together:

    * the registry/checkpoint live-table guard rejects any live/paper table,
    * NO broker library is imported and NO broker order call exists anywhere in
      the backtesting package,
    * `scalp_1m` stays paper-only end-to-end,
    * the GenAI layer can explain but never recommends promotion,
    * a run is resumable: completed shards are skipped, a spot-interrupted shard
      is retried.

Backtest-only: synthetic candles, in-memory fakes for DynamoDB, a stub LLM, a
local ReportWriter — no AWS, no broker, no network, no model training, no live
or paper trading behaviour is touched.

Run:  python -m pytest tests/backtest/test_e2e_integration.py -q
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.checkpoint_manager import CheckpointManager  # noqa: E402
from backtesting.metrics_engine import compute_metrics  # noqa: E402
from backtesting.model_dataset_builder import (  # noqa: E402
    LABEL_FIELDS,
    DatasetMeta,
    ModelDatasetBuilder,
    SignalRecord,
)
from backtesting.replay_engine import (  # noqa: E402
    Candle,
    CandleReplayEngine,
    DataFrameBarSource,
    ReplayConfig,
)
from backtesting.report_writer import ReportWriter  # noqa: E402
from backtesting.run_registry import RunRegistry, RunSpec, RunStatus  # noqa: E402
from backtesting.runner import BacktestRunner  # noqa: E402
from backtesting.strategy_adapter import get_adapter, list_adapters  # noqa: E402

IST = "Asia/Kolkata"
CODE_VERSION = "e2e-test"
DATA_VERSION = "SYNTHETIC-e2e"


# ── in-memory DynamoDB fakes ──────────────────────────────────────────────────


class FakeRunsTable:
    def __init__(self, name: str = "qe-bt-runs") -> None:
        self.name = name
        self._store: dict[str, dict] = {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None, **kw):  # noqa: N803
        self._store[Item["run_id"]] = dict(Item)
        return {}

    def get_item(self, Key, ConsistentRead=False, **kw):  # noqa: N803
        it = self._store.get(Key["run_id"])
        return {"Item": dict(it)} if it else {}

    def scan(self, **kw):
        return {"Items": [dict(v) for v in self._store.values()]}


class FakeCheckpointsTable:
    def __init__(self, name: str = "qe-bt-checkpoints") -> None:
        self.name = name
        self._store: dict[tuple, dict] = {}

    def put_item(self, Item, **kw):  # noqa: N803
        self._store[(Item["run_id"], Item["partition_id"])] = dict(Item)
        return {}

    def get_item(self, Key, **kw):  # noqa: N803
        it = self._store.get((Key["run_id"], Key["partition_id"]))
        return {"Item": dict(it)} if it else {}

    def query(self, KeyConditionExpression=None, ExpressionAttributeValues=None, **kw):  # noqa: N803
        rid = (ExpressionAttributeValues or {}).get(":rid")
        return {"Items": [dict(v) for (pk, _sk), v in self._store.items() if pk == rid]}


# ── synthetic data ────────────────────────────────────────────────────────────


def _synth_daily(symbol: str, start: str, n: int, seed: int) -> list[Candle]:
    rng = random.Random(seed)
    t = pd.Timestamp(start, tz=IST)
    px = 100.0
    out: list[Candle] = []
    for i in range(n):
        px = max(5.0, px + rng.uniform(-1.0, 1.05))  # mild upward drift
        o = px
        c = max(5.0, px + rng.uniform(-0.5, 0.5))
        h = max(o, c) + abs(rng.uniform(0, 0.6))
        low = min(o, c) - abs(rng.uniform(0, 0.6))
        out.append(Candle(symbol, "NSE", "EQ", "1d", t, o, h, low, c, 100_000 + i))
        t = t + pd.Timedelta(days=1)
    return out


def _synth_intraday(symbol: str, start: str, n_days: int, seed: int, interval: str) -> list[Candle]:
    from datetime import time as dtime

    rng = random.Random(seed)
    step = {"1m": 1, "5m": 5, "15m": 15}[interval]
    out: list[Candle] = []
    day = pd.Timestamp(start, tz=IST)
    px = 100.0
    for _ in range(n_days):
        t = pd.Timestamp(f"{day.date()} 09:15", tz=IST)
        while t.time() <= dtime(15, 30):
            px = max(5.0, px + rng.uniform(-0.4, 0.42))
            o = px
            c = max(5.0, px + rng.uniform(-0.3, 0.3))
            h = max(o, c) + abs(rng.uniform(0, 0.3))
            low = min(o, c) - abs(rng.uniform(0, 0.3))
            out.append(Candle(symbol, "NSE", "EQ", interval, t, o, h, low, c, 50_000))
            t = t + pd.Timedelta(minutes=step)
        day = day + pd.Timedelta(days=1)
    return out


def _make_runner(report_writer):
    registry = RunRegistry(FakeRunsTable())
    checkpoint = CheckpointManager(FakeCheckpointsTable())
    runner = BacktestRunner(
        registry=registry,
        checkpoint=checkpoint,
        report_writer=report_writer,
        cw_emitter=None,
    )
    return runner, registry, checkpoint


def _spec(symbols: list[str], start: str, end: str) -> RunSpec:
    return RunSpec(
        strategy="momentum",
        symbols=symbols,
        timeframe="1d",
        start_date=start,
        end_date=end,
        config_s3_path="s3://quantembrace-backtest-data/configs/e2e.json",
        code_version=CODE_VERSION,
        data_version=DATA_VERSION,
        cost_model_version="indian-v1",
        exit_policy_version="tee@1.0",
        operator="e2e-test",
    )


# ── 1. full pipeline, real strategy, real engine, real runner ─────────────────


def test_full_pipeline_with_real_strategy_adapter(tmp_path):
    """One flow through every layer with the REAL MomentumStrategy adapter."""
    symbols = ["RELIANCE", "INFY"]
    candles: list[Candle] = []
    for i, s in enumerate(symbols):
        candles.extend(_synth_daily(s, "2020-01-01", 60, seed=11 + i))

    writer = ReportWriter(base_dir=str(tmp_path), s3_bucket=None, s3_client=None)
    runner, registry, checkpoint = _make_runner(writer)
    spec = _spec(symbols, "2020-01-01", "2020-03-01")

    source = DataFrameBarSource(candles)
    config = ReplayConfig(timeframes=["1d"], partition_by="symbol", market_hours_filter=False)
    adapter = get_adapter("momentum")

    summary = runner.run(
        spec,
        source=source,
        config=config,
        strategy_factory=lambda: adapter.build_strategy(
            symbols, short_window=5, long_window=20, min_confidence=0.0
        ),
        backtester_kwargs={"slippage_bps": 2.0, "spread_bps": 4.0, "commission_pct": 0.03},
    )

    # Pipeline reached the end with metrics + a report.
    assert summary.status == "COMPLETED"
    assert "gates" in summary.metrics and "net_pnl" in summary.metrics
    assert summary.partitions_processed == 2  # one shard per symbol
    # Registry shows the full lifecycle reached COMPLETED.
    assert registry.get_run(spec.run_id()).status == RunStatus.COMPLETED.value
    # Result path is on the backtest results bucket — never a live/paper bucket.
    assert summary.result_s3_path.startswith("s3://quantembrace-backtest-results/")
    # A real local report was written (no S3).
    assert Path(summary.local_report_dir).exists()


# ── 2. all six adapters smoke-run end-to-end ──────────────────────────────────


def test_all_six_adapters_collect_signals_end_to_end():
    """Every production adapter runs through the engine path without error."""
    names = list_adapters()
    assert set(names) == {"vwap_reversion", "momentum", "orb", "trend_15m", "preclose", "scalp_1m"}

    for name in names:
        adapter = get_adapter(name)
        bars = _synth_intraday("SMOKE", "2020-06-01", 4, seed=7, interval=adapter.replay_interval)
        kwargs = (
            dict(short_window=3, long_window=14, min_confidence=0.0)
            if name == "momentum"
            else {}
        )
        signals = asyncio.run(
            adapter.collect_signals(bars, ["SMOKE"], data_version=DATA_VERSION, **kwargs)
        )
        assert isinstance(signals, list)  # ran cleanly; may be empty for some shapes
        # Every emitted signal is enriched + carries TEE metadata (no lookahead leakage).
        for sig in signals:
            assert sig["strategy_version"] == adapter.strategy_version
            assert sig["data_version"] == DATA_VERSION
            assert sig["metadata"]["backtest"] is True
            assert sig["metadata"]["tee"]["exit_policy_version"] == "tee@1.0"


def test_scalp_is_paper_only_end_to_end():
    """scalp_1m is paper-only; the flag is forced True even if a caller fights it."""
    adapter = get_adapter("scalp_1m")
    assert adapter.paper_only is True

    bars = _synth_intraday("SCALP", "2020-06-01", 3, seed=3, interval="1m")
    # Even passing paper_trade=False, the adapter must force paper_trade=True.
    signals = asyncio.run(
        adapter.collect_signals(bars, ["SCALP"], data_version=DATA_VERSION, paper_trade=False)
    )
    for sig in signals:
        assert sig["paper_trade"] is True
        assert sig["metadata"]["paper_trade"] is True


# ── 3. pipeline outcomes → leakage-free training dataset ──────────────────────


def test_pipeline_outputs_feed_leakage_free_dataset():
    """Signal+outcome records build a leakage-free, chronological, authoritative dataset."""
    idx = pd.date_range("2020-06-01", periods=60, freq="D", tz=IST)
    price = pd.Series([100 + (i % 17) - 8 for i in range(len(idx))], index=idx)

    sigs: list[SignalRecord] = []
    for i in range(40):
        t = idx[i]
        pnl = 70.0 if i % 3 else -55.0
        base = float(price.asof(t))
        sigs.append(SignalRecord(
            signal_id=f"e2e_s{i}", strategy="momentum", symbol="RELIANCE", timestamp=t,
            features={"rsi_14": 45 + (i % 20), "ema_ratio": 1.0 + (i % 4) * 0.01},
            entry_price=base, stop_price=base - 1.0, target_price=base + 2.0,
            exit_price=base + (2.0 if pnl > 0 else -1.0),
            exit_reason="FINAL_TARGET" if pnl > 0 else "STOP_LOSS",
            net_pnl=pnl, mfe_r=2.0, mae_r=-0.6, realized_r=1.5 if pnl > 0 else -1.0,
            hit_tp_before_sl=pnl > 0, trust_level="HIGH",
        ))

    meta = DatasetMeta(
        dataset_id="ds_e2e", data_version=DATA_VERSION, code_version=CODE_VERSION,
        strategy_version="momentum@2.0", exit_policy_version="tee@1.0",
        train_frac=0.7, val_frac=0.15, embargo_minutes=0,
    )
    ds = ModelDatasetBuilder().build_dataset(sigs, {"RELIANCE": price}, meta)

    # Features are point-in-time (prefixed) and disjoint from labels.
    assert all(c.startswith("feat_") for c in ds.feature_columns)
    assert not (set(ds.feature_columns) & set(ds.label_columns))
    assert all(c.replace("feat_", "") not in LABEL_FIELDS for c in ds.feature_columns)
    # Chronological split: train ≤ val ≤ test in time.
    if len(ds.train) and len(ds.val):
        assert ds.train["timestamp"].max() <= ds.val["timestamp"].min()
    if len(ds.val) and len(ds.test):
        assert ds.val["timestamp"].max() <= ds.test["timestamp"].min()
    # HIGH-trust only → authoritative dataset.
    assert ds.manifest["authoritative"] is True
    assert ds.manifest["rows_total"] == 40


# ── 4. GenAI layer explains but never promotes ────────────────────────────────


def test_genai_explains_but_never_recommends_promotion():
    """The analyst produces a cited report and the governance verdict never promotes."""
    from backtesting.genai import AnalysisContext, GenAIAnalyst, StubProvider
    from backtesting.genai.guardrails import evaluate_evidence

    analyst = GenAIAnalyst(StubProvider(response="Net P&L positive; gates evaluated. Advisory only."))
    report = analyst.generate_report(AnalysisContext(
        run_id="bt_e2e",
        summary_text="E2E smoke summary.",
        metrics={"net_pnl": 1234.0, "profit_factor": 1.5},
        sources=[{"id": "run:bt_e2e/metrics.json", "ref": "s3://quantembrace-backtest-results/runs/bt_e2e/metrics.json"}],
    ))
    assert "## Sources" in report.text  # cited
    assert report.model == "stub"

    # Even with textbook-strong evidence, promotion is never recommended.
    verdict = evaluate_evidence({
        "valid_sessions": 9, "oos_gates_pass": True, "expectancy": 0.8,
        "profit_factor": 1.9, "realized_pnl": 5000.0, "reconciliation_mismatches": 0,
    })
    assert verdict["verdict"] == "ADVISORY_OK"
    assert verdict["recommend_promotion"] is False


# ── 5. isolation: live-table guard rejects trading-runtime tables ─────────────


@pytest.mark.parametrize("bad_name", [
    "quantembrace-development-orders",
    "quantembrace-development-positions",
    "orders",
    "risk-state",
    "quantembrace-development-strategy-config",
    "sessions",
])
def test_registry_and_checkpoint_reject_live_tables(bad_name):
    """RunRegistry and CheckpointManager refuse any live/paper trading table."""
    with pytest.raises(ValueError):
        RunRegistry(FakeRunsTable(name=bad_name))
    with pytest.raises(ValueError):
        CheckpointManager(FakeCheckpointsTable(name=bad_name))


def test_registry_accepts_only_backtest_tables():
    """The qe-bt-* tables are accepted; isolation does not block the lab itself."""
    assert RunRegistry(FakeRunsTable(name="qe-bt-runs")) is not None
    assert CheckpointManager(FakeCheckpointsTable(name="qe-bt-checkpoints")) is not None


# ── 6. isolation: no broker code anywhere in the backtesting package ──────────


def test_no_broker_imports_or_order_calls_in_backtesting_package():
    """Scan every .py file in services/backtesting for broker imports / order calls.

    `guardrails.py` legitimately names `place_order` / `submit_order` as regex
    string literals it BLOCKS — so we forbid broker *imports* and *call sites*
    (`(`), never the bare token, which would false-positive on the guardrail list.
    """
    import re

    pkg = _REPO / "services" / "backtesting"
    py_files = sorted(pkg.rglob("*.py"))
    assert py_files, "backtesting package has no python files — wrong path?"

    import_re = re.compile(r"(?m)^\s*(?:import|from)\s+(?:kiteconnect|alpaca|zerodha)")
    call_re = re.compile(r"(?:place_order|submit_order|kite\.|\.connect\()\s*\(")

    offenders: list[str] = []
    for f in py_files:
        src = f.read_text()
        if import_re.search(src) or call_re.search(src):
            offenders.append(str(f.relative_to(_REPO)))
    assert offenders == [], f"broker references found in backtesting package: {offenders}"


# ── 7. resumability: completed shards skipped, failed shard retried ───────────


def test_resume_skips_completed_partitions():
    """A worker resuming a partially-done RUNNING run reprocesses only pending shards."""
    symbols = ["AAA", "BBB", "CCC"]
    candles: list[Candle] = []
    for i, s in enumerate(symbols):
        candles.extend(_synth_daily(s, "2021-01-01", 30, seed=20 + i))

    registry = RunRegistry(FakeRunsTable())
    checkpoint = CheckpointManager(FakeCheckpointsTable())
    source = DataFrameBarSource(candles)
    config = ReplayConfig(timeframes=["1d"], partition_by="symbol", market_hours_filter=False)
    engine = CandleReplayEngine(source, config)
    adapter = get_adapter("momentum")
    spec = _spec(symbols, "2021-01-01", "2021-01-30")
    run_id = spec.run_id()

    def factory():
        return adapter.build_strategy(symbols, short_window=5, long_window=20, min_confidence=0.0)

    plan = engine.plan()
    assert len(plan) == 3  # one shard per symbol

    # Simulate a prior worker that created the run, started it, and finished two of
    # the three shards before being interrupted — the run is still RUNNING.
    registry.create_run(spec)
    checkpoint.init_checkpoint(run_id)
    registry.mark_running(run_id)
    checkpoint.checkpoint_partition(run_id, "AAA|1d", last_processed_timestamp="2021-01-30T00:00:00")
    checkpoint.checkpoint_partition(run_id, "BBB|1d", last_processed_timestamp="2021-01-30T00:00:00")
    assert checkpoint.pending_partitions(run_id, plan) == ["CCC|1d"]

    # Resume: only the single pending shard is reprocessed; run reaches COMPLETED.
    results = engine.run_with_backtester(
        factory, run_id=run_id, registry=registry, checkpoint=checkpoint
    )
    assert set(results.keys()) == {"CCC|1d"}  # AAA + BBB skipped
    assert checkpoint.completed_partitions(run_id) == set(plan)  # now all three DONE
    assert registry.get_run(run_id).status == RunStatus.COMPLETED.value


def test_spot_interrupted_partition_is_retried_on_resume():
    """A shard marked FAILED (spot interruption) returns to the pending work list."""
    checkpoint = CheckpointManager(FakeCheckpointsTable())
    run_id = "bt_spot_demo"
    plan = ["AAA|1d", "BBB|1d", "CCC|1d"]

    # AAA + BBB completed; CCC was interrupted mid-flight (SIGTERM → fail_partition).
    checkpoint.checkpoint_partition(run_id, "AAA|1d", last_processed_timestamp="2021-01-30T00:00:00")
    checkpoint.checkpoint_partition(run_id, "BBB|1d", last_processed_timestamp="2021-01-30T00:00:00")
    checkpoint.fail_partition(run_id, "CCC|1d", "spot_interruption")

    # Completed = the two DONE shards; the failed shard is NOT completed.
    assert checkpoint.completed_partitions(run_id) == {"AAA|1d", "BBB|1d"}
    # Resume work list = only the interrupted shard (retried from scratch).
    assert checkpoint.pending_partitions(run_id, plan) == ["CCC|1d"]
    # Failure was recorded with its reason + an incremented retry count.
    rec = checkpoint.get_checkpoint(run_id)
    assert rec.failed_partitions["CCC|1d"]["reason"] == "spot_interruption"
    assert rec.failed_partitions["CCC|1d"]["retry_count"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
