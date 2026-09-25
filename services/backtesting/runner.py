"""High-level backtest runner for the QuantEmbrace lab.

Wraps the full pipeline in one call:
    RunRegistry.create_run()
    → CandleReplayEngine.run_with_backtester()   (SIGTERM-safe, checkpoint/resume)
    → metrics_engine.compute_metrics()
    → ReportWriter.write_run()                   (local + S3)
    → RunRegistry.set_result_paths() / mark_completed()
    → BacktestCloudWatchEmitter

Callers never touch the individual pieces — they build a ``RunSpec`` + strategy
factory and call ``BacktestRunner.run()``.

Backtest-only: no broker APIs, no live trading. S3 writes go only to
``quantembrace-backtest-results``. The live-table guard in RunRegistry rejects
any non-backtest table at construction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

from backtesting.cloudwatch_metrics import BacktestCloudWatchEmitter
from backtesting.metrics_engine import RunMeta, compute_metrics
from backtesting.replay_engine import CandleReplayEngine, ReplayConfig
from backtesting.report_writer import ReportWriter
from backtesting.run_registry import RunRecord, RunSpec

logger = logging.getLogger(__name__)

_DEFAULT_RESULTS_BUCKET = "quantembrace-backtest-results"


@dataclass
class RunSummary:
    """Outcome of a completed (or failed) BacktestRunner.run() call."""

    run_id: str
    status: str
    strategy: str
    symbols: list[str]
    start_date: str
    end_date: str
    metrics: dict = field(default_factory=dict)
    result_s3_path: str = ""
    local_report_dir: str = ""
    partitions_processed: int = 0
    total_trades: int = 0
    error: str | None = None


class BacktestRunner:
    """End-to-end backtest runner.

    Parameters:
        registry: A ``RunRegistry`` instance backed by ``qe-bt-runs``.
        checkpoint: A ``CheckpointManager`` instance backed by ``qe-bt-checkpoints``.
        report_writer: Optional ``ReportWriter``; when omitted, one is constructed
            writing to ``reports/backtests/`` locally and to the results bucket.
        cw_emitter: Optional ``BacktestCloudWatchEmitter``. When None, CW metrics
            are skipped (local / test mode).
        results_bucket: S3 bucket for run outputs. Default: backtest results bucket.
        initial_capital: Starting capital forwarded to ``Backtester``.
    """

    def __init__(
        self,
        *,
        registry: Any,
        checkpoint: Any,
        report_writer: ReportWriter | None = None,
        cw_emitter: BacktestCloudWatchEmitter | None = None,
        results_bucket: str = _DEFAULT_RESULTS_BUCKET,
        initial_capital: float = 1_000_000.0,
    ) -> None:
        self._registry = registry
        self._checkpoint = checkpoint
        self._results_bucket = results_bucket
        self._initial_capital = initial_capital
        self._cw = cw_emitter
        self._report_writer = report_writer or ReportWriter(
            base_dir="reports/backtests",
            s3_bucket=results_bucket,
            s3_prefix="runs",
        )

    @classmethod
    def from_aws(
        cls,
        *,
        runs_table: str = "qe-bt-runs",
        checkpoints_table: str = "qe-bt-checkpoints",
        results_bucket: str = _DEFAULT_RESULTS_BUCKET,
        initial_capital: float = 1_000_000.0,
        emit_cw: bool = True,
    ) -> "BacktestRunner":
        """Construct with real AWS resources (no LocalStack)."""
        from backtesting.checkpoint_manager import CheckpointManager
        from backtesting.run_registry import RunRegistry

        registry = RunRegistry.from_aws(runs_table, results_base=f"s3://{results_bucket}")
        checkpoint = CheckpointManager.from_aws(checkpoints_table)
        cw = BacktestCloudWatchEmitter.from_aws() if emit_cw else None
        return cls(
            registry=registry,
            checkpoint=checkpoint,
            cw_emitter=cw,
            results_bucket=results_bucket,
            initial_capital=initial_capital,
        )

    def run(
        self,
        spec: RunSpec,
        *,
        source: Any,
        config: ReplayConfig,
        strategy_factory: Callable,
        backtester_kwargs: dict | None = None,
        dq_gate: Any = None,
    ) -> RunSummary:
        """Execute the full backtest pipeline for ``spec``.

        Args:
            spec: Run specification (strategy, symbols, dates, versions, …).
            source: A ``BarSource`` (e.g. ``ParquetBarSource`` for the S3 lake).
            config: ``ReplayConfig`` controlling date range, timeframes, filters.
            strategy_factory: Callable returning a fresh ``BaseStrategy`` per shard.
            backtester_kwargs: Forwarded to ``Backtester.__init__`` (slippage, costs…).
            dq_gate: Optional ``DataQualityGate``. When provided it is run first;
                a failing gate marks the run FAILED and returns immediately without
                starting the replay engine. Pass ``None`` to skip the gate (local
                tests, already-validated datasets).
        """
        run_id = spec.run_id()
        logger.info("[runner] Starting run %s (strategy=%s)", run_id, spec.strategy)

        rec: RunRecord = self._registry.create_run(spec)
        self._checkpoint.init_checkpoint(run_id)

        # ── Data-quality gate (pre-flight check) ─────────────────────────────
        if dq_gate is not None:
            gate_result = dq_gate.check(spec, timeframes=config.timeframes)
            if not gate_result.passed:
                reason = (
                    f"data_quality_gate_failed: blocked={gate_result.blocked_symbols}"
                )
                logger.error("[runner] Run %s blocked by DQ gate — %s", run_id, reason)
                self._registry.mark_failed(run_id, reason)
                return RunSummary(
                    run_id=run_id,
                    status="FAILED",
                    strategy=spec.strategy,
                    symbols=list(spec.symbols),
                    start_date=str(spec.start_date),
                    end_date=str(spec.end_date),
                    error=reason,
                )
            logger.info("[runner] DQ gate PASS for run %s", run_id)

        engine = CandleReplayEngine(source, config)
        bt_kwargs = dict(backtester_kwargs or {})
        bt_kwargs.setdefault("initial_capital", self._initial_capital)

        try:
            results = engine.run_with_backtester(
                strategy_factory,
                run_id=run_id,
                registry=self._registry,
                checkpoint=self._checkpoint,
                backtester_kwargs=bt_kwargs,
                cw_emitter=self._cw,
            )
        except Exception as exc:
            logger.error("[runner] Run %s FAILED: %s", run_id, exc)
            return RunSummary(
                run_id=run_id,
                status="FAILED",
                strategy=spec.strategy,
                symbols=list(spec.symbols),
                start_date=str(spec.start_date),
                end_date=str(spec.end_date),
                error=str(exc),
            )

        # Aggregate BacktestResult objects into a trades DataFrame for metrics/report.
        trades_df = _results_to_trades_df(results, spec.strategy)
        equity_df = _results_to_equity_df(results, self._initial_capital)

        period_s = _period_seconds(str(spec.start_date), str(spec.end_date))
        metrics = compute_metrics(
            trades_df,
            equity_curve=equity_df if not equity_df.empty else None,
            initial_capital=self._initial_capital,
            period_seconds=period_s,
        )

        meta = RunMeta(
            run_id=run_id,
            strategy=spec.strategy,
            symbols=tuple(spec.symbols),
            start_date=str(spec.start_date),
            end_date=str(spec.end_date),
            code_version=spec.code_version,
            data_version=spec.data_version,
            cost_model_version=spec.cost_model_version,
            exit_policy_version=spec.exit_policy_version,
            status="COMPLETED",
            initial_capital=self._initial_capital,
        )

        report_manifest = self._report_writer.write_run(
            meta,
            metrics=metrics,
            trades=trades_df if not trades_df.empty else None,
            equity_curve=equity_df if not equity_df.empty else None,
            config={
                "strategy": spec.strategy,
                "symbols": list(spec.symbols),
                "timeframe": spec.timeframe,
                "start_date": str(spec.start_date),
                "end_date": str(spec.end_date),
                "config_hash": spec.config_hash(),
            },
        )

        # Update registry record with the result S3 path.
        result_s3_path = f"s3://{self._results_bucket}/runs/{run_id}/"
        self._registry.set_result_paths(run_id, result_s3_path=result_s3_path)

        # CW active-run-count gauge (best-effort after completion).
        if self._cw is not None:
            try:
                running = self._registry.list_runs(status="RUNNING")
                self._cw.active_run_count(len(running))
                self._cw.candles_replayed(sum(
                    len(r.trades) for r in results.values()
                ))
            except Exception:
                pass

        total_trades = sum(len(r.trades) for r in results.values())
        logger.info(
            "[runner] Run %s COMPLETED — %d partitions, %d trades",
            run_id, len(results), total_trades,
        )

        return RunSummary(
            run_id=run_id,
            status="COMPLETED",
            strategy=spec.strategy,
            symbols=list(spec.symbols),
            start_date=str(spec.start_date),
            end_date=str(spec.end_date),
            metrics=metrics,
            result_s3_path=result_s3_path,
            local_report_dir=report_manifest.get("local_dir", ""),
            partitions_processed=len(results),
            total_trades=total_trades,
        )


# ── conversion helpers ────────────────────────────────────────────────────────


def _results_to_trades_df(results: dict[str, Any], strategy_name: str) -> pd.DataFrame:
    """Convert per-partition BacktestResult objects into a flat trades DataFrame."""
    rows: list[dict] = []
    for pid, res in results.items():
        for t in res.trades:
            direction = t.direction.value if hasattr(t.direction, "value") else str(t.direction)
            rows.append({
                "symbol": t.symbol,
                "strategy": strategy_name,
                "direction": direction,
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "quantity": t.quantity,
                # gross = net + commission (backtester deducts commission from pnl)
                "gross_pnl": t.pnl + t.commission,
                "costs": t.commission,
                "slippage": t.slippage,
                "net_pnl": t.pnl,
                "exit_reason": t.exit_reason,
                "mis_dependent": False,
                "mfe_r": 0.0,
                "mae_r": 0.0,
                "r_multiple": 0.0,
                "partition_id": pid,
            })
    if not rows:
        return pd.DataFrame(columns=[
            "symbol", "strategy", "direction", "entry_time", "exit_time",
            "entry_price", "exit_price", "quantity", "gross_pnl", "costs",
            "slippage", "net_pnl", "exit_reason", "mis_dependent",
            "mfe_r", "mae_r", "r_multiple", "partition_id",
        ])
    return pd.DataFrame(rows)


def _results_to_equity_df(results: dict[str, Any], initial_capital: float) -> pd.DataFrame:
    """Merge per-partition equity curves into a single time-ordered curve."""
    points: list[tuple] = []
    for res in results.values():
        for ts, val in res.equity_curve:
            points.append((ts, val))
    if not points:
        return pd.DataFrame(columns=["timestamp", "equity"])
    points.sort(key=lambda x: x[0])
    # Re-base: cumulative NAV starting from initial_capital (first point is the
    # baseline after the first bar, so shift by the initial_capital delta).
    timestamps = [p[0] for p in points]
    values = [p[1] for p in points]
    return pd.DataFrame({"timestamp": timestamps, "equity": values})


def _period_seconds(start_date: str, end_date: str) -> float | None:
    try:
        s = datetime.fromisoformat(start_date)
        e = datetime.fromisoformat(end_date)
        return max(0.0, (e - s).total_seconds())
    except ValueError:
        return None
