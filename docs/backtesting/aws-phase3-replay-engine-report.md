# AWS Backtesting Lab — Phase 3 Report: Replay Engine Integration

**Status:** COMPLETE — awaiting human approval before Phase 4  
**Date:** 2026-06-14  
**Governance rule:** Every phase writes a report and stops. A human approves before the next phase begins.

---

## Scope

Phase 3 wires together the pre-existing replay + backtester components into a production-grade pipeline:

1. **Spot interruption checkpointing** — SIGTERM handler in `CandleReplayEngine.run_with_backtester()`
2. **CloudWatch metric emission** — `BacktestCloudWatchEmitter` under `QuantEmbrace/Backtest` namespace
3. **High-level `BacktestRunner`** — single entry point that manages the full lifecycle
4. **S3 results export** — `ReportWriter` wired into runner; artifacts go to `quantembrace-backtest-results/runs/{run_id}/`
5. **Budget alarm** — `aws_budgets_budget` in `monitoring_backtest` (80% actual + 100% forecasted)

Phase 3 did **not** rewrite `backtester.py`. All changes are additive — new parameters, new files.

---

## What Was Already Implemented (Pre-Phase-3)

These files existed and were verified complete before Phase 3 began:

| File | Status |
|---|---|
| `services/backtesting/replay_engine.py` | Pre-existing — `CandleReplayEngine`, `ParquetBarSource`, `DataFrameBarSource`, gap detection, market-hours filter, date-range scope |
| `services/backtesting/data_loader.py` | Pre-existing — `load_candles()`, S3/local, CSV/Parquet, IST normalization |
| `services/backtesting/run_registry.py` | Pre-existing — full lifecycle, optimistic locking, live-table guard |
| `services/backtesting/checkpoint_manager.py` | Pre-existing — per-shard DONE/FAILED, resume via `pending_partitions()` |
| `services/backtesting/metrics_engine.py` | Pre-existing — full metrics catalog, live-readiness gates |
| `services/backtesting/report_writer.py` | Pre-existing — local + S3 artifact writer |

---

## Phase 3 Deliverables

### 1. SIGTERM Handler (`services/backtesting/replay_engine.py`)

`run_with_backtester()` gains a `cw_emitter` parameter and installs a SIGTERM handler before the partition loop:

```
SIGTERM received during partition pid →
  checkpoint.fail_partition(run_id, pid, "spot_interruption")   # shard retried on resume
  registry.mark_failed(run_id, "spot_interruption: …")          # run registry updated
  cw_emitter.run_failed()                                        # CW metric emitted
  sys.exit(0)                                                    # clean exit
```

The original `SIGTERM` handler is always restored in the `finally` block regardless of how the function exits (normal completion, exception, or `sys.exit(0)`).

On resume: `pending_partitions()` excludes only DONE shards — a FAILED shard is retried from scratch, consistent with the Parquet daily-shard model.

### 2. CloudWatch Emitter (`services/backtesting/cloudwatch_metrics.py`)

Thin wrapper around `put_metric_data` in the `QuantEmbrace/Backtest` namespace:

| Method | Metric name | When |
|---|---|---|
| `run_completed()` | `RunCompleted` | Per completed run |
| `run_failed()` | `RunFailed` | Per failed/interrupted run |
| `checkpoint_written()` | `CheckpointWritten` | Per completed partition |
| `dq_gate_failed()` | `DQGateFailed` | On data-quality gate rejection |
| `active_run_count(n)` | `ActiveRunCount` | Gauge after completion |
| `candles_replayed(n)` | `CandlesReplayed` | Counter after completion |

All methods degrade gracefully to a `WARNING` log on CW failure — a CW outage never crashes a running backtest. When `cw_client=None`, all emits are no-ops (test / local mode).

Factory added to `shared/aws/clients.py`: `get_cloudwatch_client()`.

### 3. BacktestRunner (`services/backtesting/runner.py`)

Single call orchestrates the entire pipeline:

```
RunRegistry.create_run(spec)               # idempotent
CheckpointManager.init_checkpoint(run_id)
CandleReplayEngine.run_with_backtester()   # SIGTERM-safe, checkpoint/resume, CW metrics
  → per-partition: Backtester.run(bars) → BacktestResult → checkpoint DONE
metrics_engine.compute_metrics(trades_df)  # full catalog + live-readiness gates
ReportWriter.write_run(meta, metrics, …)   # local + S3 export
RunRegistry.set_result_paths(result_s3_path)
```

`BacktestRunner.from_aws()` constructs the runner with real AWS resources. Local/test mode accepts injected fakes.

`RunSummary` returned includes: `run_id`, `status`, `metrics`, `result_s3_path`, `partitions_processed`, `total_trades`, `error`.

### 4. S3 Results Export

`ReportWriter` writes per-run artifacts to:

```
quantembrace-backtest-results/runs/{run_id}/
  config.yaml
  metrics.json
  summary.md
  trades.parquet
  equity_curve.parquet
  strategy_breakdown.csv
  symbol_breakdown.csv
  exit_reason_breakdown.csv
  mfe_mae.parquet
  model_labels_preview.parquet
  logs/run.log
```

Every artifact includes `code_version`, `data_version`, `cost_model_version`, `exit_policy_version` for reproducibility.

### 5. Budget Alarm (Terraform)

Added to `infra/terraform/modules/monitoring_backtest/main.tf`:

```hcl
resource "aws_budgets_budget" "backtest_monthly" {
  name         = "quantembrace-backtest-monthly"
  budget_type  = "COST"
  limit_amount = "100"   # USD, configurable via monthly_budget_usd variable
  time_unit    = "MONTHLY"
  cost_filter { name = "TagKeyValue"; values = ["user:CostCenter$backtesting-lab"] }

  notification { threshold = 80;  notification_type = "ACTUAL" }     # 80% actual → SNS
  notification { threshold = 100; notification_type = "FORECASTED" } # 100% forecast → SNS
}
```

---

## PITR and Encryption Status

These were required by the approver. Status from Terraform:

| Resource | PITR | Encryption |
|---|---|---|
| `qe-bt-runs` DynamoDB table | ✅ `point_in_time_recovery { enabled = true }` | ✅ AWS default (SSE-KMS managed) |
| `qe-bt-datasets` DynamoDB table | ✅ `point_in_time_recovery { enabled = true }` | ✅ AWS default |
| `qe-bt-checkpoints` DynamoDB table | N/A (ephemeral — PITR off by design; checkpoints are transient) | ✅ AWS default |
| `quantembrace-backtest-data` S3 bucket | N/A (object-level; use S3 versioning for lake recovery) | ✅ `SSE-AES256` |
| `quantembrace-backtest-results` S3 bucket | N/A | ✅ `SSE-AES256` |

S3 lifecycle rules:
- `lake/` prefix: Standard → IA at 30d → Glacier IR at 365d
- `runs/` prefix: Standard → IA at 30d (run results never auto-deleted)

---

## Tests

New: `tests/backtest/test_runner.py` — 12 tests

| Test | Covers |
|---|---|
| `test_happy_path_run_lifecycle` | CREATED → RUNNING → COMPLETED in registry |
| `test_checkpoint_written_per_partition` | Per-partition DONE entry in checkpoint table |
| `test_metrics_populated_in_summary` | `RunSummary.metrics` includes gate keys |
| `test_report_writer_called_on_success` | `ReportWriter.write_run()` called once |
| `test_cloudwatch_emitted_on_success` | `CheckpointWritten` + `RunCompleted` emitted |
| `test_cloudwatch_noop_when_no_client` | No CW client → run still completes |
| `test_failure_path_marks_registry_failed` | Strategy crash → FAILED in registry |
| `test_idempotent_run_create` | Same spec → same `run_id` |
| `test_sigterm_handler_installed_and_restored` | SIGTERM handler installed during run; original restored after |
| `test_results_to_trades_df_empty` | No trades → empty DataFrame with correct columns |
| `test_results_to_equity_df_empty` | No equity curve → empty DataFrame |
| `test_no_broker_calls_in_runner` | `runner.py` contains no broker references |

Full suite result: **109 passed, 0 failed** (`tests/backtest/` — all phases).

---

## File Manifest

```
# Python (new/modified)
services/shared/aws/clients.py           (added get_cloudwatch_client())
services/backtesting/cloudwatch_metrics.py    (NEW)
services/backtesting/replay_engine.py    (SIGTERM handler + cw_emitter param in run_with_backtester)
services/backtesting/runner.py           (NEW — BacktestRunner, RunSummary)
tests/backtest/test_runner.py            (NEW — 12 tests)

# Terraform (modified)
infra/terraform/modules/monitoring_backtest/main.tf         (budget alarm)
infra/terraform/modules/monitoring_backtest/variables.tf    (monthly_budget_usd)
infra/terraform/modules/monitoring_backtest/outputs.tf      (budget_name output)
infra/terraform/environments/backtest/main.tf               (wire monthly_budget_usd)
infra/terraform/environments/backtest/variables.tf          (monthly_budget_usd)
infra/terraform/environments/backtest/terraform.tfvars      (monthly_budget_usd = 100)
```

---

## Safety Invariants Verified

| Invariant | How verified |
|---|---|
| No broker API references in runner | `test_no_broker_calls_in_runner` assertion + `test_no_broker_calls_possible` (existing) |
| Registry only touches `qe-bt-*` tables | `assert_backtest_table()` guard (existing, tests existing) |
| SIGTERM handler always restored | `test_sigterm_handler_installed_and_restored` |
| CW outage doesn't crash run | `try/except` in `_emit()`; `test_cloudwatch_noop_when_no_client` |
| Worker IAM denies live S3/DDB/SM | Terraform explicit Deny policies (Phase 2, unchanged) |
| `terraform validate` passes | ✅ Confirmed |
| `terraform apply` NOT run | ✅ Design-only; apply requires explicit operator action |

---

## Phase 4 Preview (NOT STARTED)

Phase 4 = **Data Quality Gate** — pre-run snapshot validator that:
- Reads from `quantembrace-backtest-data` lake
- Checks symbol coverage, date completeness, price sanity, no-lookahead flag
- Emits `DQGateFailed` CW metric on rejection
- Blocks `BacktestRunner.run()` before any shard starts

Phase 4 does **not** begin until this report is approved.

---

## Approval Required

Per governance: **a human must approve this report before Phase 4 begins.**

Checklist for approver:
- [ ] SIGTERM handler design reviewed and accepted
- [ ] CW metric names match the 5 CloudWatch alarms in Phase 2 Terraform
- [ ] `BacktestRunner` lifecycle accepted (create → run → report → set_result_paths)
- [ ] 109 tests still passing (`python -m pytest tests/backtest/ -q`)
- [ ] Budget alarm thresholds (80% actual, 100% forecasted, $100 default) accepted
- [ ] Phase 4 scope (Data Quality Gate) understood and approved
