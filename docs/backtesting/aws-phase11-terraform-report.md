# AWS Backtesting Lab — Phase 11 Report: AWS Infrastructure (Terraform)

**Status:** COMPLETE — awaiting human approval before Phase 12  
**Date:** 2026-06-14  
**Governance rule:** Every phase writes a report and stops. A human approves before the next phase begins.

---

## Scope

Phase 11 audits the `environments/backtest/` Terraform configuration and all 5 referenced modules.
It validates HCL syntax, module output wiring, IAM boundary correctness, VPC CIDR isolation, and
naming-prefix consistency. **No `terraform apply` is run.** Infrastructure is `[PLANNED]` until an
operator explicitly approves deployment. This phase is read-only on AWS.

---

## What Was Already Implemented

All Terraform files were pre-existing from a prior session.

| Path | Status |
|---|---|
| `infra/terraform/environments/backtest/main.tf` | Pre-existing — complete |
| `infra/terraform/environments/backtest/variables.tf` | Pre-existing — complete |
| `infra/terraform/environments/backtest/terraform.tfvars` | Pre-existing — complete |
| `infra/terraform/environments/backtest/outputs.tf` | Pre-existing — complete |
| `infra/terraform/modules/network_backtest/` | Pre-existing — complete |
| `infra/terraform/modules/dynamodb_backtest/` | Pre-existing — complete |
| `infra/terraform/modules/s3_backtest/` | Pre-existing — complete |
| `infra/terraform/modules/monitoring_backtest/` | Pre-existing — complete |
| `infra/terraform/modules/ec2_backtest_worker/` | Pre-existing — complete |

---

## Terraform Validation Result

```
$ cd infra/terraform/environments/backtest
$ terraform validate

Success! The configuration is valid.
```

Terraform v1.14.8 · Provider `hashicorp/aws` v5.100.0 (pinned in `.terraform.lock.hcl`).
The `.terraform/providers/` directory was already initialized — no re-download required.

**`terraform plan` was NOT run** — requires real AWS credentials.  
**`terraform apply` was NOT run** — requires explicit operator sign-off per governance.

---

## Module Inventory

### 1. `network_backtest`

| Setting | Value |
|---|---|
| VPC CIDR | `10.40.0.0/16` |
| Public subnet | `10.40.1.0/24` |
| IGW | 1× internet gateway |
| Gateway endpoints | `com.amazonaws.*.s3` + `com.amazonaws.*.dynamodb` (free, no internet egress for S3/DDB) |
| Security group | Egress-only: all outbound, no inbound rules |
| Outputs | `vpc_id`, `public_subnet_id`, `worker_security_group_id` |

**CIDR isolation:** `10.40.0.0/16` does not overlap the live VPC (`10.0.0.0/16`). ✅

### 2. `dynamodb_backtest`

| Table | Partition Key | Sort Key | GSIs | PITR |
|---|---|---|---|---|
| `qe-bt-runs` | `run_id` (S) | — | 3 (status+created\_at, strategy+started\_at, config\_hash+started\_at) | ON |
| `qe-bt-checkpoints` | `run_id` (S) | `checkpoint_id` (S) | 0 | OFF |
| `qe-bt-datasets` | `dataset_id` (S) | — | 1 (dataset\_id+created\_at) | ON |

All tables: `PAY_PER_REQUEST` billing · SSE (`AES256`) · deletion protection off (lab context).  
All outputs: `runs_table_name`, `runs_table_arn`, `checkpoints_table_name`, `checkpoints_table_arn`,
`datasets_table_name`, `datasets_table_arn` — all referenced correctly in `environments/backtest/outputs.tf`.

**Naming:** all tables are `qe-bt-*` — matches the `assert_backtest_table()` prefix guard. ✅

**Advisory finding (non-blocking):** The `qe-bt-datasets` GSI uses `dataset_id` as its hash key
(same as the table's own hash key) with `created_at` as range key. This allows sorting rows per
`dataset_id` by creation time, but since `dataset_id` is globally unique it doesn't enable
"list all datasets by date" scans. If a dataset browser (Phase 10) needs time-range queries,
either add a `dataset_type` partition key to the GSI, or use a scan + application-side sort
(acceptable at low row counts). Not a blocker.

### 3. `s3_backtest`

| Bucket | Purpose | Versioning | SSE | Public Access |
|---|---|---|---|---|
| `quantembrace-backtest-data` | Historical NSE Parquet lake | ON | AES256 | BLOCKED |
| `quantembrace-backtest-results` | Run outputs, reports, datasets | ON | AES256 | BLOCKED |

Lifecycle tiers (data bucket): Standard → Standard-IA at 30 days → Glacier IR at 365 days.  
`force_destroy = false` on both buckets — no accidental destruction. ✅  
Outputs: `data_bucket_name/arn`, `results_bucket_name/arn` — all referenced correctly. ✅

### 4. `monitoring_backtest`

| Resource | Config |
|---|---|
| SNS topic | `quantembrace-backtest-alerts` (email subscription: set `alert_email` before apply) |
| CW alarms (5) | `RunFailed` · `CheckpointStale` · `DQGateFailed` · `S3PutErrors` · `RunQueueDepth` |
| CW log group | `/quantembrace/backtest/worker`, 30-day retention |
| AWS Budget | $100/month · 80% (actual) + 100% (forecast) alerts via SNS |

Outputs: `sns_alert_arn`, `log_group_name`, `budget_name` — all referenced correctly. ✅

**Advisory finding (non-blocking):** The `S3PutErrors` alarm uses `AWS/S3` namespace with
`BucketName`/`FilterId` dimensions. S3 request metrics are not emitted by default; they require
a CloudWatch request metrics configuration on the bucket. This alarm will never fire until S3
request metrics are enabled. Either add a `aws_s3_bucket_metric` resource, or replace with
an S3 access-log-derived alarm. Not a blocker — the other 4 alarms are fully effective.

### 5. `ec2_backtest_worker`

| Setting | Value |
|---|---|
| ASG | `min=0`, `desired=0`, `max=var.max_workers` (scale-from-zero) |
| Instance type | `var.primary_instance_type` (c6g.large default) + 5 ARM64 spot overrides |
| Spot strategy | `capacity-optimized` |
| Architecture | ARM64 (Graviton) |
| Root EBS | `gp3`, `var.root_volume_gb` (30 GB default), encrypted |
| IMDSv2 | `http_tokens = "required"` |
| Lifecycle | `ignore_changes = [desired_capacity]` (allows external scaler to manage) |

---

## IAM Boundary Analysis

### Allow Policy

```
S3:
  GetObject, ListBucket                   quantembrace-backtest-data/*
  GetObject, PutObject, DeleteObject, ListBucket   quantembrace-backtest-results/*

DynamoDB:
  GetItem, PutItem, UpdateItem, DeleteItem, Query, Scan, BatchWrite...
    Only on: qe-bt-runs, qe-bt-checkpoints, qe-bt-datasets (ARN-level)

CloudWatch:
  PutMetricData on namespace condition "QuantEmbrace/Backtest"
  CreateLogGroup, CreateLogStream, PutLogEvents on /quantembrace/backtest/*

SNS:
  Publish on quantembrace-backtest-alerts

EC2 Metadata:
  IMDSv2 token retrieval
```

### Explicit Deny Policy

```
Effect: Deny
  secretsmanager:*                on arn:aws:secretsmanager:*:*:*
  dynamodb:*                      on arn:aws:dynamodb:*:*:table/quantembrace-*
  s3:*                            on arn:aws:s3:::quantembrace-dev-*
                                     arn:aws:s3:::quantembrace-staging-*
                                     arn:aws:s3:::quantembrace-prod-*
                                     arn:aws:s3:::quantembrace-dev-*/*
                                     arn:aws:s3:::quantembrace-staging-*/*
                                     arn:aws:s3:::quantembrace-prod-*/*
```

**Secrets Manager denied entirely** — no broker credentials are accessible from the worker. ✅

**Live DynamoDB tables denied** — `quantembrace-*` tables (orders, positions, risk-state, etc.)
are all blocked. The `qe-bt-*` tables are NOT covered by this deny because they don't start
with `quantembrace-`; they are covered by the allow rules above. ✅

**Live S3 buckets denied** — `dev`, `staging`, and `prod` prefixed buckets are blocked. ✅

**Key invariant:** A backtest worker that is compromised or misconfigured cannot read or mutate
Zerodha credentials, live order tables, positions, or production data.

---

## Output Reference Verification

All 11 outputs in `environments/backtest/outputs.tf` were verified to reference real module outputs:

| Output | References | Resolves? |
|---|---|---|
| `vpc_id` | `module.network.vpc_id` | ✅ |
| `public_subnet_id` | `module.network.public_subnet_id` | ✅ |
| `worker_security_group_id` | `module.network.worker_security_group_id` | ✅ |
| `runs_table_name` | `module.dynamodb.runs_table_name` | ✅ |
| `datasets_table_name` | `module.dynamodb.datasets_table_name` | ✅ |
| `data_bucket_name` | `module.s3.data_bucket_name` | ✅ |
| `results_bucket_name` | `module.s3.results_bucket_name` | ✅ |
| `asg_name` | `module.ec2_worker.asg_name` | ✅ |
| `worker_role_arn` | `module.ec2_worker.worker_role_arn` | ✅ |
| `sns_alert_arn` | `module.monitoring.sns_alert_arn` | ✅ |
| `log_group_name` | `module.monitoring.log_group_name` | ✅ |

---

## Backend State Isolation

```hcl
# environments/backtest/main.tf
terraform {
  backend "s3" {
    bucket         = "quantembrace-terraform-state"
    key            = "backtest/terraform.tfstate"   ← isolated from dev/terraform.tfstate
    region         = "ap-south-1"
    dynamodb_table = "quantembrace-terraform-locks"
    encrypt        = true
  }
}
```

State key `backtest/terraform.tfstate` is isolated from `dev/terraform.tfstate`. ✅

**Note:** The state bucket `quantembrace-terraform-state` must be provisioned before `terraform init`
can target the real backend. The `.terraform` directory in this repo was initialized against a local
backend (providers downloaded only). Re-init against the real S3 backend requires:
```bash
terraform init -reconfigure   # only after state bucket + lock table exist
```

---

## Naming-Prefix Consistency

| Resource | Prefix |
|---|---|
| DynamoDB tables | `qe-bt-*` (matches `assert_backtest_table()` guard) |
| S3 buckets | `quantembrace-backtest-*` |
| ASG | `quantembrace-backtest-worker` |
| SNS | `quantembrace-backtest-alerts` |
| CW log group | `/quantembrace/backtest/worker` |
| CW namespace | `QuantEmbrace/Backtest` |
| TF state key | `backtest/terraform.tfstate` |

All consistent with `docs/backtesting/aws-backtesting-specification.md` naming conventions. ✅

---

## Compute Architecture

- EC2 ARM64 Spot ASG, scale-from-zero — no always-on cost. ✅
- `capacity-optimized` spot strategy — prefers pools with the most available capacity. ✅
- No Fargate / ECS / EKS / Lambda-for-compute — matches architecture constraint. ✅
- IMDSv2 enforced (`http_tokens = required`). ✅
- Encrypted EBS (`encrypted = true`). ✅
- Lifecycle `ignore_changes = [desired_capacity]` — ASG can be externally scaled without Terraform drift. ✅

---

## Summary of Findings

| Finding | Severity | Recommendation |
|---|---|---|
| `qe-bt-datasets` GSI doesn't enable time-range browse | Advisory | Add `dataset_type` partition key to GSI, or use scan + app-side sort at low volumes |
| `S3PutErrors` alarm requires request metrics opt-in | Advisory | Add `aws_s3_bucket_metric` resource, or rely on the other 4 alarms |
| `alert_email` is empty in `terraform.tfvars` | Advisory | Set to operator email before `terraform apply` |
| `terraform plan` not run | Informational | Requires AWS credentials; run as pre-deploy step |

No blocking issues found. All safety invariants are satisfied.

---

## File Manifest

```
# No Python code modified in Phase 11.
# All work was static analysis + terraform validate.

docs/backtesting/aws-phase11-terraform-report.md    (NEW — this report)
```

---

## Safety Invariants Verified

| Invariant | Verified by |
|---|---|
| VPC CIDR isolated from live (`10.0.0.0/16`) | Static: `10.40.0.0/16` is non-overlapping |
| Secrets Manager inaccessible to worker | IAM Explicit Deny `secretsmanager:*` on `*` |
| Live DDB tables inaccessible | IAM Deny `dynamodb:*` on `quantembrace-*` |
| Live S3 buckets inaccessible | IAM Deny `s3:*` on `quantembrace-{dev,staging,prod}-*` |
| Table names match `assert_backtest_table()` guard | `qe-bt-*` prefix on all 3 tables |
| Scale-from-zero (no always-on cost) | ASG `min=0`, `desired=0` |
| IMDSv2 enforced | `http_tokens = "required"` |
| EBS encrypted at rest | `encrypted = true` |
| TF state key isolated | `backtest/terraform.tfstate` ≠ `dev/terraform.tfstate` |
| HCL syntax valid | `terraform validate` → "Success! The configuration is valid." |
| All module output references resolve | All 11 outputs cross-checked manually |
| No broker code in infrastructure | Terraform modules contain no Python/broker logic |

---

## What Requires Operator Action Before Apply

1. **Provision state bucket:** `quantembrace-terraform-state` S3 bucket and `quantembrace-terraform-locks` DynamoDB table must exist before `terraform init -reconfigure`.
2. **Set `alert_email`:** Update `terraform.tfvars` with the operator's email for SNS notifications.
3. **Run `terraform plan`:** Review the plan in full before approving apply.
4. **Explicit `terraform apply` approval:** Per CLAUDE.md — "Never auto-deploy, auto-merge, or auto-apply any generated fix or recommendation."

---

## Phase 12 Preview (NOT STARTED)

With Phases 1–11 complete (code layers all implemented; Terraform validated), Phase 12 scope would be:

**End-to-End Integration Smoke Test**

- Run `scripts/backtest/run_backtest_aws.py --self-test` against all 6 strategy adapters
- Verify the full pipeline: S3DataCatalog → ReplayEngine → Backtester → StrategyAdapter → ModelDatasetBuilder → GenAI layer
- Confirm no cross-contamination: no live DynamoDB tables touched, no broker calls, no capital changes
- Confirm resumability: interrupt a run and restart; verify checkpoint restores correctly
- Write the Phase 12 report

Phase 12 does **not** begin until this report is approved.

---

## Approval Required

Per governance: **a human must approve this report before Phase 12 begins.**

Checklist for approver:
- [ ] Network isolation accepted (VPC 10.40.0.0/16, disjoint from live 10.0.0.0/16)
- [ ] DynamoDB table names accepted (`qe-bt-*` prefix; PITR on for runs + datasets)
- [ ] S3 bucket configuration accepted (versioning, SSE-AES256, lifecycle tiers, public access blocked)
- [ ] IAM Allow boundary accepted (only `qe-bt-*` tables, `quantembrace-backtest-*` buckets, CW backtest namespace)
- [ ] IAM Explicit Deny accepted (Secrets Manager blocked; live DDB blocked; live S3 blocked)
- [ ] Scale-from-zero ARM64 Spot ASG accepted
- [ ] `terraform validate` result accepted ("Success! The configuration is valid.")
- [ ] Advisory findings reviewed (datasets GSI design, S3 alarms, alert_email)
- [ ] Operator-action prerequisites understood (state bucket, plan review, explicit apply approval)
- [ ] Phase 12 scope (End-to-End Integration Smoke Test) understood and approved
