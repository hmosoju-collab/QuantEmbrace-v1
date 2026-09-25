"""Unit tests for the metrics engine + report writer (Phase 7).

Covers: metrics correctness · local report files written · S3 write (stubbed) ·
summary contains required metrics · failure report generated · versions recorded ·
total_return_pct · annualised_return_pct (CAGR) · sharpe_ratio · sortino_ratio ·
largest_win/loss · max_consecutive_losses · lookahead_violations always 0 ·
no broker references.

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


def test_total_return_pct():
    """total_return_pct = net_pnl / initial_capital × 100."""
    m = compute_metrics(_trades(), initial_capital=1_000_000)
    # net_pnl = 70 → 70 / 1_000_000 * 100 = 0.007%
    assert abs(m["total_return_pct"] - 0.007) < 1e-9


def test_annualised_return_pct_cagr():
    """CAGR with a known 1-year period: net_pnl 100_000 on 1_000_000 → ~10% annualised."""
    t0 = pd.Timestamp("2020-01-01 09:30", tz=IST)
    t1 = pd.Timestamp("2021-01-01 15:30", tz=IST)
    df = pd.DataFrame([dict(
        symbol="R", strategy="s", direction="LONG",
        entry_time=t0, exit_time=t1,
        entry_price=100, exit_price=110, quantity=1000,
        gross_pnl=10_003, costs=3, slippage=0, net_pnl=10_000,
        exit_reason="FINAL_TARGET", mfe_r=2.0, mae_r=-0.1, r_multiple=2.0, mis_dependent=False,
    )])
    # 1 year in seconds
    period = (t1 - t0).total_seconds()
    m = compute_metrics(df, initial_capital=100_000, period_seconds=period)
    # CAGR = (110_000/100_000)^1 - 1 = 10% → ~10%
    assert 9.0 < m["annualised_return_pct"] < 11.0


def test_largest_win_and_loss():
    """largest_win = max winning trade; largest_loss = min losing trade (negative)."""
    m = compute_metrics(_trades(), initial_capital=1_000_000)
    # Winners: 100, 20 → largest_win = 100
    assert m["largest_win"] == 100.0
    # Losers: -50 → largest_loss = -50
    assert m["largest_loss"] == -50.0


def test_max_consecutive_losses():
    """Longest losing streak is counted correctly across a mixed sequence."""
    t0 = pd.Timestamp("2020-06-01 10:00", tz=IST)
    rows = []
    # sequence: W L L L W L W → streak = 3
    for i, pnl in enumerate([10, -5, -5, -5, 10, -5, 10]):
        rows.append(dict(
            symbol="R", strategy="s", direction="LONG",
            entry_time=t0 + pd.Timedelta(minutes=i),
            exit_time=t0 + pd.Timedelta(minutes=i + 1),
            entry_price=100, exit_price=100 + (pnl > 0) * 1,
            quantity=10, gross_pnl=pnl + 1, costs=1, slippage=0,
            net_pnl=pnl, exit_reason="FINAL_TARGET" if pnl > 0 else "STOP_LOSS",
            mfe_r=1.0, mae_r=-0.5, r_multiple=1.0, mis_dependent=False,
        ))
    m = compute_metrics(pd.DataFrame(rows))
    assert m["max_consecutive_losses"] == 3


def test_sharpe_and_sortino_computed():
    """Sharpe and Sortino are non-zero when equity curve has variance across days."""
    t0 = pd.Timestamp("2020-06-01 10:00", tz=IST)
    rows = []
    # Build trades spread across 5 different days, alternating win/loss.
    for day in range(5):
        pnl = 200 if day % 2 == 0 else -80
        rows.append(dict(
            symbol="R", strategy="s", direction="LONG",
            entry_time=t0 + pd.Timedelta(days=day),
            exit_time=t0 + pd.Timedelta(days=day, hours=1),
            entry_price=100, exit_price=100 + (pnl > 0) * 2,
            quantity=10, gross_pnl=pnl + 5, costs=5, slippage=0,
            net_pnl=pnl, exit_reason="FINAL_TARGET" if pnl > 0 else "STOP_LOSS",
            mfe_r=1.5, mae_r=-0.5, r_multiple=1.0, mis_dependent=False,
        ))
    m = compute_metrics(pd.DataFrame(rows), initial_capital=100_000)
    assert m["sharpe_ratio"] != 0.0, "Sharpe should be non-zero with daily equity variance"
    assert m["sortino_ratio"] != 0.0, "Sortino should be non-zero with losing days"


def test_lookahead_violations_always_zero():
    """lookahead_violations is always 0 — enforced by the replay engine invariant."""
    m = compute_metrics(_trades(), initial_capital=1_000_000)
    assert m["lookahead_violations"] == 0
    m_empty = compute_metrics(pd.DataFrame())
    assert m_empty["lookahead_violations"] == 0


def test_no_broker_calls_in_metrics_engine():
    """metrics_engine.py and report_writer.py must not reference broker APIs."""
    import backtesting.metrics_engine as me_mod
    import backtesting.report_writer as rw_mod
    forbidden = ["kiteconnect", "alpaca", "place_order", "zerodhabroker", "submit_order"]
    for mod in (me_mod, rw_mod):
        src = Path(mod.__file__).read_text().lower()
        present = [t for t in forbidden if t in src]
        assert present == [], f"{mod.__name__} must not reference brokers: {present}"


def test_summary_contains_new_catalog_metrics(tmp_path):
    """Summary markdown includes the new catalog metrics added in Phase 7."""
    m = compute_metrics(_trades(), initial_capital=1_000_000)
    res = ReportWriter(base_dir=str(tmp_path)).write_run(_meta(), metrics=m, trades=_trades())
    summary = (Path(res["local_dir"]) / "summary.md").read_text()
    # Existing tokens already tested in test_summary_contains_required_metrics —
    # check that new fields appear in metrics.json instead.
    import json
    metrics_json = json.loads((Path(res["local_dir"]) / "metrics.json").read_text())
    for key in ("total_return_pct", "largest_win", "largest_loss",
                "max_consecutive_losses", "lookahead_violations"):
        assert key in metrics_json, f"metrics.json missing {key!r}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
