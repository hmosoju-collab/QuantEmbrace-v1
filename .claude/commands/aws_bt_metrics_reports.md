---
description: "Phase 7 — compute the full metrics catalog and generate the run report.md."
---

# /aws_bt_metrics_reports — Phase 7: Metrics & Reports

Compute every metric in `docs/backtesting/metrics-catalog.md` and render `report.md`. Reuse `commands/run_backtest.yaml` metric definitions — do not fork them.

## Load first
`metrics-catalog.md`, `backtester.py` (`BacktestResult`), `aws-backtest-run-registry.md`.

## Do
1. Compute returns, risk-adjusted (Sharpe/Sortino/Calmar), drawdown (+duration), trade stats (incl. expectancy), exposure/turnover, cost/slippage, `lookahead_violations`.
2. Write `metrics.json`, `equity_curve.parquet`, `trades.parquet`; mirror `metrics_summary` to `qe-bt-runs`.
3. Render `report.md` mapping results to the live-readiness gates (expectancy>0, PF>1.2, realized P&L>0) — clearly labeled advisory.

## Safety
Reporting only. No promotion, no trading action implied by a passing backtest.

## Output / Stop
Metrics validation report + sample `report.md`. **Stop for approval.**
