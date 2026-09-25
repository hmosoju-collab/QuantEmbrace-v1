# AWS Backtesting Lab — Phase 2 Report: Run Registry & Terraform Scaffolding

**Status:** COMPLETE — awaiting human approval before Phase 3  
**Date:** 2026-06-14  
**Governance rule:** Every phase writes a report and stops. A human approves before the next phase begins.

---

## Scope

Phase 2 delivers:
1. **Run Registry** (`services/backtesting/run_registry.py`) — DynamoDB-backed run lifecycle manager with optimistic concurrency control
2. **Checkpoint Manager** (`services/backtesting/checkpoint_manager.py`) — per-shard checkpoint persistence for resumable runs
3. **Terraform scaffolding** — 5 thin modules + a wired `environments/backtest/` environment, validated but not applied

---

## Deliverables

### Run Registry & Checkpoint Manager (Python)

Both files were already fully implemented and verified in Phase 2 review. Tests pass:

```
tests/backtest/test_run_registry.py — 17 tests in 0.05s — PASSED
```

Key implementation details:
- `RunSpec.config_hash()` — SHA-256[:16] of strategy+symbols+timeframe+dates+versions → deterministic `run_id`; same config cannot produce duplicate entries
- `RunRegistry._update()` — 6-retry optimistic lock via `record_version` + DynamoDB `ConditionalExpression`; `VersionConflictError` on stale write
- `assert_backtest_table()` — rejects any table name not containing `qe-bt-` or `backtest`; hard guard against live/paper table mutation
- `_TERMINAL_STATUSES = {COMPLETED, FAILED, CANCELLED}` — terminal runs cannot be resurrected (idempotent `create_run()` returns existing record)
- Checkpoint composite PK: `run_id` (PK) + `partition_id` (SK) — supports multi-worker parallel shards

### Terraform Modules Created

All 5 modules are under `infra/terraform/modules/`:

| Module | Resources |
|---|---|
| `dynamodb_backtest` | `qe-bt-runs` (GSIs: status, strategy, config-hash), `qe-bt-checkpoints`, `qe-bt-datasets` — all PAY_PER_REQUEST |
| `s3_backtest` | `quantembrace-backtest-data` (lake), `quantembrace-backtest-results` (run outputs) — versioning + SSE-AES256 + lifecycle tiering |
| `network_backtest` | VPC `10.40.0.0/16` (disjoint from live `10.0.0.0/16`), IGW, public subnet, free S3+DDB gateway VPC endpoints, egress-only SG |
| `ec2_backtest_worker` | ARM64 Spot ASG (min=desired=0, capacity-optimized), launch template (AL2023 arm64, IMDSv2, 30GB gp3, encrypted), IAM role + instance profile |
| `monitoring_backtest` | SNS `quantembrace-backtest-alerts`, CloudWatch log group `/quantembrace/backtest/worker` (30d retention), 5 CloudWatch alarms |

### Environment Wiring

`infra/terraform/environments/backtest/` wires all 5 modules with:
- **Isolated backend**: `key = "backtest/terraform.tfstate"` (completely separate from live state)
- **Shared Terraform state bucket** (`quantembrace-terraform-state`) and lock table (`quantembrace-terraform-locks`) — these already exist; the backtest environment uses them with a different key
- **Default provider tags**: `Environment = "backtest"`, `CostCenter = "backtesting-lab"`

### Validation

```
terraform validate → Success! The configuration is valid.
```

No `terraform plan` or `terraform apply` was run (design-only phase, per governance).

---

## Safety Invariants Verified

| Invariant | Implementation |
|---|---|
| Worker cannot access live DDB tables | Explicit `Deny` on `arn:aws:dynamodb:*:*:table/quantembrace-*` |
| Worker cannot access live S3 buckets | Explicit `Deny` on `quantembrace-dev-*`, `-staging-*`, `-prod-*` |
| Worker cannot access Secrets Manager | Explicit `Deny` on `secretsmanager:*` — no broker credentials ever reachable |
| Backtest state isolated from live state | Separate S3 backend key `backtest/terraform.tfstate` |
| VPC CIDR disjoint from live | `10.40.0.0/16` vs live `10.0.0.0/16` — no overlap |
| No Fargate/ECS/Lambda/EKS | ARM64 EC2 Spot ASG only |
| No data path to live tables | `assert_backtest_table()` in `RunRegistry` rejects non-`qe-bt-*` tables at the Python layer |
| No auto-apply | `terraform validate` only; `apply` requires explicit operator action |

---

## CloudWatch Alarms (5)

| Alarm | Trigger |
|---|---|
| `quantembrace-backtest-run-failed` | RunFailed metric ≥ 1 in 1 min |
| `quantembrace-backtest-checkpoint-stale` | CheckpointWritten < 1 in 2h window |
| `quantembrace-backtest-dq-gate-failure` | DQGateFailed metric ≥ 1 in 1 min |
| `quantembrace-backtest-s3-put-errors` | S3 5xxErrors > 5 over 2 × 5min periods |
| `quantembrace-backtest-run-queue-depth` | ActiveRunCount max > 5 over 3 × 5min periods |

All alarms publish to SNS `quantembrace-backtest-alerts`. Email subscription is opt-in (set `alert_email` in `terraform.tfvars` before apply).

---

## File Manifest

```
services/backtesting/run_registry.py          (pre-existing, verified)
services/backtesting/checkpoint_manager.py    (pre-existing, verified)
tests/backtest/test_run_registry.py           (pre-existing, 17 tests PASS)

infra/terraform/modules/dynamodb_backtest/
  main.tf  variables.tf  outputs.tf

infra/terraform/modules/s3_backtest/
  main.tf  variables.tf  outputs.tf

infra/terraform/modules/network_backtest/
  main.tf  variables.tf  outputs.tf

infra/terraform/modules/ec2_backtest_worker/
  iam.tf  main.tf  userdata.sh.tpl  variables.tf  outputs.tf

infra/terraform/modules/monitoring_backtest/
  main.tf  variables.tf  outputs.tf

infra/terraform/environments/backtest/
  main.tf  variables.tf  terraform.tfvars  outputs.tf
```

---

## Cost Estimate (Idle)

| Resource | Monthly (idle) |
|---|---|
| DynamoDB (3 tables, PAY_PER_REQUEST, no reads/writes) | ~$0 |
| S3 (buckets exist, ~0 data) | ~$0 |
| ASG (desired=0, no instances) | $0 |
| VPC, IGW, VPC endpoints | $0 (free tier / gateway endpoints) |
| CloudWatch alarms (5) | ~$0.50 |
| SNS topic | ~$0 |
| **Total idle** | **< $1/month** |

Costs only accrue during active backtest runs (EC2 Spot + S3 data egress).

---

## Phase 3 Preview (NOT STARTED)

Phase 3 = **Replay Engine** — extend `services/strategy_engine/backtesting/backtester.py` with:
- Parquet loader reading from `quantembrace-backtest-data` via S3
- Shard-based tick replay with partition-level checkpoint writes
- Config injected from `qe-bt-runs` registry record (never hardcoded)

Phase 3 does **not** begin until this report is approved.

---

## Approval Required

Per governance: **a human must approve this report before Phase 3 begins.**

Checklist for approver:
- [ ] Safety invariants section reviewed and accepted
- [ ] Terraform module structure looks correct
- [ ] 17 run_registry tests still green (run `pytest tests/backtest/test_run_registry.py`)
- [ ] Phase 3 scope (Replay Engine) understood and approved
