"""Unit tests for the metrics engine + report writer (Phase AWS-BT-8).

Covers: metrics correctness · local report files written · S3 write (stubbed) ·
summary contains required metrics · failure report generated · results include
code_version and data_version.

Backtest-only: temp dirs, in-memory fake S3 — no AWS, no broker.

Run:  python -m pytest tests/backtest/test_metrics_reports.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.metrics_engine import RunMeta, compute_metrics  # noqa: E402
from backtesting.report_writer import ReportWriter  # noqa: E402

IST = "Asia/Kolkata"


def _trades() -> pd.DataFrame:
    t0 = pd.Timestamp("2020-06-01 10:00", tz=IST)
    return pd.DataFrame([
        dict(symbol="R", strategy="momentum", direction="LONG", entry_time=t0,
             exit_time=t0 + pd.Timedelta(minutes=30), entry_price=100, exit_price=102, quantity=10,
             gross_pnl=103, costs=3, slippage=1, net_pnl=100, exit_reason="FINAL_TARGET",
             mfe_r=2.5, mae_r=-0.2, r_multiple=2.0, mis_dependent=False),
        dict(symbol="T", strategy="vwap_reversion", direction="LONG", entry_time=t0,
             exit_time=t0 + pd.Timedelta(minutes=20), entry_price=50, exit_price=49.5, quantity=10,
             gross_pnl=-47, costs=3, slippage=1, net_pnl=-50, exit_reason="STOP_LOSS",
             mfe_r=0.5, mae_r=-1.0, r_multiple=-1.0, mis_dependent=False),
        dict(symbol="R", strategy="momentum", direction="LONG", entry_time=t0,
             exit_time=t0 + pd.Timedelta(minutes=300), entry_price=100, exit_price=102, quantity=10,
             gross_pnl=22, costs=2, slippage=1, net_pnl=20, exit_reason="MIS_CLOSE",
             mfe_r=0.8, mae_r=-0.3, r_multiple=0.4, mis_dependent=True),
    ])


def _meta(**kw) -> RunMeta:
    base = dict(run_id="bt_demo123", strategy="momentum", symbols=("R", "T"),
                start_date="2020-06-01", end_date="2020-06-01", code_version="abc123",
                data_version="snap-2020", cost_model_version="v1", exit_policy_version="tee@1.0")
    base.update(kw)
    return RunMeta(**base)


class FakeS3:
    def __init__(self):
        self.keys: list[str] = []

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        self.keys.append(Key)
        return {}


# ── metrics ──────────────────────────────────────────────────────────────────


def test_metrics_calculated_correctly():
    m = compute_metrics(_trades(), initial_capital=1_000_000)
    assert m["number_of_trades"] == 3
    assert m["net_pnl"] == 70.0
    assert m["gross_pnl"] == 78.0
    assert m["cost_impact"] == 8.0
    assert round(m["win_rate"], 1) == 66.7
    assert m["avg_winner"] == 60.0 and m["avg_loser"] == -50.0
    assert round(m["payoff_ratio"], 3) == 1.2
    assert round(m["profit_factor"], 3) == 2.4   # 120 / 50
    assert round(m["expectancy"], 3) == 23.333
    assert round(m["mis_dependency"], 3) == 0.333
    g = m["gates"]
    assert g["expectancy_gt_0"] and g["profit_factor_gt_1_2"] and g["net_pnl_gt_0"]
    assert g["overall_pass"] is True


def test_losing_run_fails_gates():
    t0 = pd.Timestamp("2020-06-01 10:00", tz=IST)
    losing = pd.DataFrame([
        dict(symbol="R", strategy="m", direction="LONG", entry_time=t0, exit_time=t0,
             entry_price=100, exit_price=99, quantity=10, gross_pnl=-8, costs=2, slippage=1,
             net_pnl=-10, exit_reason="STOP_LOSS", mfe_r=0.2, mae_r=-1.0, r_multiple=-1.0,
             mis_dependent=False),
    ])
    g = compute_metrics(losing)["gates"]
    assert g["overall_pass"] is False and g["net_pnl_gt_0"] is False


# ── report writer ────────────────────────────────────────────────────────────


def test_report_files_written_locally(tmp_path):
    m = compute_metrics(_trades())
    res = ReportWriter(base_dir=str(tmp_path)).write_run(_meta(), metrics=m, trades=_trades())
    run_dir = Path(res["local_dir"])
    expected = [
        "config.yaml", "summary.md", "metrics.json", "trades.parquet", "equity_curve.parquet",
        "strategy_breakdown.csv", "symbol_breakdown.csv", "exit_reason_breakdown.csv",
        "mfe_mae.parquet", "rejected_signals.parquet", "model_labels_preview.parquet",
        "logs/run.log",
    ]
    for rel in expected:
        assert (run_dir / rel).exists(), f"missing {rel}"


def test_s3_write_stubbed(tmp_path):
    m = compute_metrics(_trades())
    s3 = FakeS3()
    res = ReportWriter(base_dir=str(tmp_path), s3_bucket="quantembrace-backtest-results",
                       s3_client=s3).write_run(_meta(), metrics=m, trades=_trades())
    assert res["s3_keys"], "no S3 keys returned"
    assert set(res["s3_keys"]) == set(s3.keys)
    assert "runs/bt_demo123/metrics.json" in s3.keys
    assert "runs/bt_demo123/summary.md" in s3.keys
    assert any(k.endswith("trades.parquet") for k in s3.keys)


def test_summary_contains_required_metrics(tmp_path):
    m = compute_metrics(_trades())
    res = ReportWriter(base_dir=str(tmp_path)).write_run(_meta(), metrics=m, trades=_trades())
    summary = (Path(res["local_dir"]) / "summary.md").read_text()
    for token in ["Net P&L", "Profit factor", "Win rate", "Expectancy", "Max drawdown",
                  "Expectancy > 0", "Profit factor > 1.2", "Net P&L > 0", "Overall:"]:
        assert token in summary, f"summary missing {token!r}"


def test_failure_report_generated(tmp_path):
    meta = _meta(run_id="bt_fail", status="FAILED", error_reason="worker OOM on B#2018")
    res = ReportWriter(base_dir=str(tmp_path)).write_run(meta)  # no metrics/trades
    summary = (Path(res["local_dir"]) / "summary.md").read_text()
    assert "FAILED" in summary
    assert "worker OOM on B#2018" in summary
    assert (Path(res["local_dir"]) / "metrics.json").exists()  # still records status + versions


def test_results_include_code_and_data_version(tmp_path):
    m = compute_metrics(_trades())
    res = ReportWriter(base_dir=str(tmp_path)).write_run(_meta(), metrics=m, trades=_trades())
    run_dir = Path(res["local_dir"])
    metrics_json = json.loads((run_dir / "metrics.json").read_text())
    assert metrics_json["code_version"] == "abc123"
    assert metrics_json["data_version"] == "snap-2020"
    config = json.loads((run_dir / "config.yaml").read_text())  # JSON is valid YAML
    assert config["code_version"] == "abc123" and config["data_version"] == "snap-2020"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
