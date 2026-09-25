"""Report writer for the QuantEmbrace backtesting lab.

Persists a run's full artifact set to ``reports/backtests/<run_id>/`` locally and
(optionally) to S3 — config, summary, metrics, trades/equity/breakdowns, MFE/MAE,
rejected signals, model-label preview, and logs. Generates a **failure report**
when a run did not complete. Every artifact records ``code_version`` and
``data_version`` for reproducibility.

Backtest-only: no broker APIs. S3 writes go through an injected client or the
sanctioned `shared.aws.clients.get_s3_client` (never a raw ``boto3.client``).
``config.yaml`` is emitted as JSON (valid YAML) to avoid a hard YAML dependency.
"""

from __future__ import annotations

import io
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

from backtesting.metrics_engine import RunMeta, breakdowns


class ReportWriter:
    def __init__(
        self,
        base_dir: str = "reports/backtests",
        *,
        s3_bucket: str | None = None,
        s3_prefix: str = "runs",
        s3_client: Any = None,
    ) -> None:
        self._base = Path(base_dir)
        self._s3_bucket = s3_bucket
        self._s3_prefix = s3_prefix.strip("/")
        self._s3_client = s3_client

    # ── public ────────────────────────────────────────────────────────────────
    def write_run(
        self,
        meta: RunMeta,
        *,
        metrics: dict | None = None,
        trades: pd.DataFrame | None = None,
        equity_curve: pd.DataFrame | None = None,
        rejected_signals: pd.DataFrame | None = None,
        model_labels: pd.DataFrame | None = None,
        config: dict | None = None,
        logs_text: str = "",
    ) -> dict[str, Any]:
        run_dir = self._base / meta.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        s3_keys: list[str] = []

        def emit(relpath: str, content: bytes) -> None:
            self._write_local(run_dir, relpath, content)
            written.append(relpath)
            key = self._upload(meta.run_id, relpath, content)
            if key:
                s3_keys.append(key)

        trades = _ensure_trades(trades)
        equity_curve = equity_curve if equity_curve is not None else _build_equity(trades, meta.initial_capital)

        # config.yaml (JSON is valid YAML).
        cfg = {**(config or {}), **_meta_dict(meta)}
        emit("config.yaml", json.dumps(cfg, indent=2, default=str).encode())

        # metrics.json — always includes versions for reproducibility.
        metrics_out = dict(metrics or {})
        metrics_out.update({
            "run_id": meta.run_id,
            "status": meta.status,
            "code_version": meta.code_version,
            "data_version": meta.data_version,
            "cost_model_version": meta.cost_model_version,
            "exit_policy_version": meta.exit_policy_version,
        })
        emit("metrics.json", json.dumps(metrics_out, indent=2, default=str).encode())

        # logs/
        emit("logs/run.log", (logs_text or f"run {meta.run_id} status={meta.status}\n").encode())

        if meta.status == "FAILED":
            emit("summary.md", _failure_summary(meta).encode())
            return {"local_dir": str(run_dir), "files": written, "s3_keys": s3_keys}

        # Data artifacts (completed runs).
        emit("trades.parquet", _parquet_bytes(trades))
        emit("equity_curve.parquet", _parquet_bytes(equity_curve))

        bd = breakdowns(trades)
        emit("strategy_breakdown.csv", _csv_bytes(bd["strategy"]))
        emit("symbol_breakdown.csv", _csv_bytes(bd["symbol"]))
        emit("exit_reason_breakdown.csv", _csv_bytes(bd["exit_reason"]))

        emit("mfe_mae.parquet", _parquet_bytes(_mfe_mae(trades)))
        emit("rejected_signals.parquet", _parquet_bytes(_ensure_nonempty(rejected_signals, ["signal_id", "reason"])))
        emit("model_labels_preview.parquet", _parquet_bytes(_ensure_nonempty(model_labels, ["signal_id", "label"]).head(100)))

        emit("summary.md", _summary(meta, metrics_out, bd).encode())

        return {"local_dir": str(run_dir), "files": written, "s3_keys": s3_keys}

    # ── internals ───────────────────────────────────────────────────────────────
    @staticmethod
    def _write_local(run_dir: Path, relpath: str, content: bytes) -> None:
        path = run_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def _upload(self, run_id: str, relpath: str, content: bytes) -> str | None:
        if not self._s3_bucket:
            return None
        client = self._s3_client
        if client is None:
            from shared.aws.clients import get_s3_client

            client = get_s3_client()
        key = f"{self._s3_prefix}/{run_id}/{relpath}"
        client.put_object(Bucket=self._s3_bucket, Key=key, Body=content)
        return key


# ── helpers ──────────────────────────────────────────────────────────────────


def _meta_dict(meta: RunMeta) -> dict:
    d = asdict(meta)
    d["symbols"] = list(meta.symbols)
    return d


def _ensure_trades(trades: pd.DataFrame | None) -> pd.DataFrame:
    cols = ["symbol", "strategy", "direction", "entry_time", "exit_time", "entry_price",
            "exit_price", "quantity", "gross_pnl", "costs", "slippage", "net_pnl",
            "exit_reason", "mfe_r", "mae_r", "r_multiple", "mis_dependent"]
    if trades is None or trades.empty:
        return pd.DataFrame(columns=cols)
    return trades


def _ensure_nonempty(df: pd.DataFrame | None, cols: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    return df


def _build_equity(trades: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    if trades.empty or "net_pnl" not in trades:
        return pd.DataFrame({"timestamp": [pd.Timestamp.now("UTC")], "equity": [initial_capital]})
    ordered = trades.sort_values("exit_time") if "exit_time" in trades else trades
    return pd.DataFrame({
        "timestamp": pd.to_datetime(ordered["exit_time"]) if "exit_time" in ordered else range(len(ordered)),
        "equity": initial_capital + ordered["net_pnl"].astype(float).cumsum().values,
    })


def _mfe_mae(trades: pd.DataFrame) -> pd.DataFrame:
    keep = [c for c in ("symbol", "strategy", "mfe_r", "mae_r", "net_pnl") if c in trades.columns]
    if not keep:
        return pd.DataFrame(columns=["symbol", "mfe_r", "mae_r", "net_pnl"])
    return trades[keep].copy()


def _parquet_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def _csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode()


def _summary(meta: RunMeta, m: dict, bd: dict[str, pd.DataFrame]) -> str:
    g = m.get("gates", {})
    lines = [
        f"# Backtest Report — {meta.run_id}",
        "",
        f"_Status: **{meta.status}** · backtest-only · advisory._",
        "",
        f"- Strategy: `{meta.strategy}` · Symbols: {list(meta.symbols)}",
        f"- Period: {meta.start_date} → {meta.end_date}",
        f"- Versions: code=`{meta.code_version}` · data=`{meta.data_version}` · "
        f"cost_model=`{meta.cost_model_version}` · exit_policy=`{meta.exit_policy_version}`",
        "",
        "## Headline metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Net P&L | {m.get('net_pnl', 0):,.2f} |",
        f"| Gross P&L | {m.get('gross_pnl', 0):,.2f} |",
        f"| Cost impact | {m.get('cost_impact', 0):,.2f} |",
        f"| Total slippage | {m.get('total_slippage', 0):,.2f} |",
        f"| Number of trades | {m.get('number_of_trades', 0)} |",
        f"| Win rate | {m.get('win_rate', 0):.1f}% |",
        f"| Expectancy | {m.get('expectancy', 0):,.4f} |",
        f"| Profit factor | {m.get('profit_factor', 0):.3f} |",
        f"| Payoff ratio | {m.get('payoff_ratio', 0):.3f} |",
        f"| Avg winner | {m.get('avg_winner', 0):,.2f} |",
        f"| Avg loser | {m.get('avg_loser', 0):,.2f} |",
        f"| Max drawdown | {m.get('max_drawdown_pct', 0):.2f}% |",
        f"| Daily drawdown | {m.get('daily_drawdown_pct', 0):.2f}% |",
        f"| Turnover | {m.get('turnover', 0):,.2f} |",
        f"| Exposure | {m.get('exposure_pct', 0):.1f}% |",
        f"| MIS dependency | {m.get('mis_dependency', 0):.2%} |",
        f"| Avg MFE (R) | {m.get('avg_mfe_r', 0):.3f} |",
        f"| Avg MAE (R) | {m.get('avg_mae_r', 0):.3f} |",
        f"| Profit capture ratio | {m.get('profit_capture_ratio', 0):.3f} |",
        "",
        "## Live-readiness gates (advisory)",
        "",
        f"- Expectancy > 0: **{'PASS' if g.get('expectancy_gt_0') else 'FAIL'}** ({m.get('expectancy', 0):,.4f})",
        f"- Profit factor > 1.2: **{'PASS' if g.get('profit_factor_gt_1_2') else 'FAIL'}** ({m.get('profit_factor', 0):.3f})",
        f"- Net P&L > 0: **{'PASS' if g.get('net_pnl_gt_0') else 'FAIL'}** ({m.get('net_pnl', 0):,.2f})",
        f"- **Overall: {'PASS' if g.get('overall_pass') else 'FAIL'}**",
        "",
        "> A passing backtest is necessary but **not sufficient** for live — promotion still "
        "requires ≥5 valid paper sessions + operator sign-off.",
        "",
        "## Monthly P&L",
        "",
    ]
    monthly = m.get("monthly_pnl", {})
    if monthly:
        lines += ["| Month | Net P&L |", "|---|---:|"]
        lines += [f"| {k} | {v:,.2f} |" for k, v in sorted(monthly.items())]
    else:
        lines.append("_none_")
    lines += ["", "## Exit-reason breakdown", "", _df_table(bd["exit_reason"])]
    lines += ["", "## Strategy breakdown", "", _df_table(bd["strategy"])]
    lines += ["", "## Symbol breakdown", "", _df_table(bd["symbol"])]
    return "\n".join(lines) + "\n"


def _failure_summary(meta: RunMeta) -> str:
    return (
        f"# Backtest Report — {meta.run_id}\n\n"
        f"_Status: **FAILED** · backtest-only._\n\n"
        f"- Strategy: `{meta.strategy}` · Symbols: {list(meta.symbols)}\n"
        f"- Period: {meta.start_date} → {meta.end_date}\n"
        f"- Versions: code=`{meta.code_version}` · data=`{meta.data_version}`\n\n"
        f"## Error\n\n```\n{meta.error_reason or 'unknown error'}\n```\n\n"
        "The run did not complete; no metrics were produced. Inspect `logs/run.log` "
        "and re-run with the recorded `config.yaml`.\n"
    )


def _df_table(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "_none_"
    cols = list(df.columns)
    out = ["| " + " | ".join(str(c) for c in cols) + " |",
           "|" + "|".join("---" for _ in cols) + "|"]
    for _, row in df.iterrows():
        out.append("| " + " | ".join(_fmt(row[c]) for c in cols) + " |")
    return "\n".join(out)


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)
