# QuantEmbrace — AWS-BT-8 Metrics Engine & Report Writer Report

> **Phase 8 — metrics engine + report writer. Implemented + tested.** Backtest-only: pure computation + artifact writing; no broker APIs, no live trading. Results are advisory.
> Generated: 2026-06-06 · Governed by `aws-backtesting-steering.md` · Consumes outputs of AWS-BT-4…7.

---

## 1. What was built

- `services/backtesting/metrics_engine.py` — computes the full `metrics-catalog.md` set from a canonical trades DataFrame, builds per-strategy / per-symbol / per-exit-reason breakdowns, and maps results to the **live-readiness gates**.
- `services/backtesting/report_writer.py` — persists the full artifact set to `reports/backtests/<run_id>/` locally and (optionally) to S3, including a **failure report** path, with `code_version` / `data_version` on every run.

## 2. Metrics coverage

gross P&L · net P&L · cost impact · total slippage · profit factor · win rate · avg winner · avg loser · payoff ratio · expectancy · max drawdown (% / abs) · daily drawdown · monthly P&L · number of trades · turnover · exposure time/% · MIS dependency · avg MFE/MAE (R) · profit-capture ratio · strategy/symbol/exit-reason breakdowns.

## 3. Live-readiness gate mapping (advisory)

| Gate | Rule | Source metric |
|---|---|---|
| Expectancy > 0 | `expectancy_gt_0` | mean net P&L per trade |
| Profit factor > 1.2 | `profit_factor_gt_1_2` | gross profit / gross loss |
| Net P&L > 0 | `net_pnl_gt_0` | sum net P&L |
| **Overall** | all three pass | — |

Every report states: *a passing backtest is necessary but not sufficient for live — promotion still requires ≥5 valid paper sessions + operator sign-off.*

## 4. Output artifacts (`reports/backtests/<run_id>/`)

`config.yaml` (JSON — valid YAML), `summary.md`, `metrics.json`, `trades.parquet`, `equity_curve.parquet`, `strategy_breakdown.csv`, `symbol_breakdown.csv`, `exit_reason_breakdown.csv`, `mfe_mae.parquet`, `rejected_signals.parquet`, `model_labels_preview.parquet`, `logs/run.log`. Same bytes are uploaded to S3 (`s3://<bucket>/runs/<run_id>/...`) when a bucket is configured.

## 5. Test results

`tests/backtest/test_metrics_reports.py` — **7/7 passing** (6 required + a losing-run gate check). Full lab suite **64/64** (12 data + 9 registry + 8 replay + 8 adapters + 9 execution + 11 tee/mis + 7 metrics).

| Test | Verifies |
|---|---|
| `metrics_calculated_correctly` | net=70, PF=2.4, win-rate=66.7%, expectancy=23.33, payoff=1.2, MIS-dep=0.33, gates PASS |
| `losing_run_fails_gates` | negative run → overall gate FAIL |
| `report_files_written_locally` | all 11 artifacts + `logs/run.log` present |
| `s3_write_stubbed` | every file uploaded; returned keys == stub keys |
| `summary_contains_required_metrics` | Net P&L, Profit factor, Win rate, Expectancy, Max drawdown, gate lines |
| `failure_report_generated` | FAILED summary + error reason; metrics.json still written |
| `results_include_code_and_data_version` | metrics.json + config.yaml carry code/data versions |

## 6. Design notes

- Operates on a **canonical trades DataFrame** (from the AWS-BT-7 `TradeOutcome` / execution simulator), keeping the engine decoupled from any single producer.
- `config.yaml` is emitted as JSON (a valid YAML subset) to avoid a hard YAML dependency.
- S3 writes use an injected client or the sanctioned `shared.aws.clients.get_s3_client` — never a raw `boto3.client`.
- Drawdown is computed from the equity curve (built from cumulative net P&L when not supplied); daily drawdown is the worst intraday drawdown across days.

## 7. Files

| Artifact | Path |
|---|---|
| Metrics engine | `services/backtesting/metrics_engine.py` |
| Report writer | `services/backtesting/report_writer.py` |
| Tests (7) | `tests/backtest/test_metrics_reports.py` |

## 8. Recommended next phase

**Phase 9 — Walk-forward validation** (`/aws_bt_walk_forward`): in-sample/out-of-sample folds with OOS aggregation and overfitting indicators, each fold a registered run feeding this metrics engine.

---

*Implemented + tested. No infra deployed, no live trading, no broker APIs called. Stop for approval before the next phase.*
