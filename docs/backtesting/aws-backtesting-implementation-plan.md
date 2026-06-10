# QuantEmbrace — AWS Historical Backtesting Lab: Implementation Plan

> **Status: PLANNED — design only.** Each phase below is gated: it produces a report and **stops for human approval** before the next begins. Nothing here is implemented by this document.
> Last updated: 2026-06-06 · Governed by `aws-backtesting-steering.md`.

---

## Phasing principle

Design before build. No phase modifies trading behavior, enables live, or places orders. Each phase maps to a `/aws_bt_*` command and produces a stop-for-approval report.

| Phase | Name | Command | Primary deliverable | Report gate |
|---|---|---|---|---|
| 0 | Discovery & Steering | `/aws_bt_session_start`, `/aws_bt_discovery` | This doc set + repo-state report | **DONE in this setup** |
| 1 | Data Lake | `/aws_bt_data_lake` | Parquet lake + ingestion (bhavcopy backbone — see `daily-bhavcopy-ingestion-design.md`) + quality checks | Lake coverage + quality report |
| 2 | Run Registry | `/aws_bt_run_registry` | `qe-bt-runs` + `qe-bt-checkpoints` (Terraform) + registry client | Registry schema + resume proof |
| 3 | Replay Engine | `/aws_bt_replay_engine` | Parquet loader + shard runner + checkpoint hooks on existing `backtester.py` | Determinism + resume report |
| 4 | Strategy Adapters | `/aws_bt_strategy_adapters` | Adapters for all 6 production strategies | Per-strategy smoke report |
| 5 | Execution Simulator | `/aws_bt_execution_simulator` | Cost/slippage/no-lookahead fill model wired + asserted | Realism + `lookahead==0` report |
| 6 | TEE / MIS | `/aws_bt_tee_mis` | Old-vs-new exit + MIS square-off comparison | TEE comparison report |
| 7 | Metrics & Reports | `/aws_bt_metrics_reports` | Full metrics catalog + `report.md` generator | Metrics validation report |
| 8 | Walk-Forward | `/aws_bt_walk_forward` | IS/OOS fold harness + aggregation | Walk-forward study report |
| 9 | Model Dataset | `/aws_bt_model_dataset` | Leakage-free training-set generator | Dataset spec + stats report |
| 10 | GenAI Layer | `/aws_bt_genai_layer` | On-demand Bedrock analysis/report/RAG | GenAI governance report |
| 11 | Full Run | `/aws_bt_full_run` | End-to-end orchestration of a multi-year batch | Full-run acceptance report |

> The first authoritative **daily-data** run — eligible strategies, snapshot/registry metadata, delivery cost model, walk-forward, promotion boundary, pass/fail — is specified in `daily-data-backtest-protocol.md`.

## Infra delta (introduced across phases 1–10, design only here)

- **S3**: `quantembrace-backtest-data`, `quantembrace-backtest-results` (+ lifecycle: Standard → IA 30d → Glacier IR; results never auto-deleted).
- **DynamoDB**: `qe-bt-runs`, `qe-bt-checkpoints`, `qe-bt-datasets` (on-demand, PITR on registry).
- **EC2**: `backtest-worker` ARM64 ASG (min/desired=0, **spot**), launch template + userdata. Reuses the `modules/ec2_services` *pattern* (AL2023 ARM64 AMI, launch-template/ASG, scheduled scaling) via a **new least-privilege `ec2_backtest_worker` module** — not the live module itself, which carries broker/Kafka/Secrets IAM and is on-demand-only (no spot). Detail: §"Backtest environment — Terraform detail".
- **EventBridge + Step Functions + Bedrock**: GenAI orchestration (on-demand only).
- **CloudWatch**: `QuantEmbrace/Backtest` namespace; **SNS**: `quantembrace-backtest-alerts`.
- **IAM**: least-privilege worker role (S3 lake R/W, registry R/W, CloudWatch, **no** Secrets Manager broker creds, **no** live/paper table access).
- **Terraform**: new `environments/backtest/` + a `backtest` module group; never edits live `prod`/`staging`.

## Dependencies

Phase 1 → 2 → 3 are sequential. Phases 4–7 depend on 3. Phase 8 depends on 5–7. Phase 9 depends on 5 + lake. Phase 10 depends on 7 (consumes outputs). Phase 11 depends on all.

## Done-definition per phase

Code merged with tests (in that phase, not this one); Terraform `plan` clean (no live diff); docs updated under `docs/backtesting/`; registry/artifacts verified; report written; **explicit human approval** before proceeding. Mirrors `governance/done_definition.md`.

## Guardrails carried through every phase

`prefer_refactor_over_rewrite` · `no_duplicate_services` · `terraform_safety` (no live diff) · `no_hardcoded_secrets` · `cost_optimization` · `restart_safety` · `no_silent_failures`. The `safety-review-agent` reviews each phase before its report.

---

## Backtest environment — Terraform detail

> Expands the Infra-delta bullets into a build-ready plan. **Design only — no `.tf` written, nothing applied.** Build is gated behind Phase reports + human approval.

### Module reuse (review finding)

Shared modules hardcode **live** naming/billing/IAM, so instantiating them with `environment="backtest"` yields wrong names (e.g. `quantembrace-backtest-backtest-results`) and pulls in live IAM. Reuse is therefore at the *pattern/convention* level via thin backtest-only modules; **shared modules stay untouched** — which is what guarantees zero live/staging/prod diff.

| Module | Reuse | Why not as-is |
|---|---|---|
| Remote state (S3 + lock table) | **Direct** | new key `backtest/terraform.tfstate` — the isolation boundary |
| `vpc` | Pattern | reuse the free S3 + DynamoDB **gateway endpoints**; it always builds ≥1 NAT (`count = ha_nat?2:1`, no zero) + paid interface endpoints |
| `ec2_services` | Pattern | reuse AL2023 ARM64 AMI + launch-template/ASG + scheduled scaling; it carries broker/Kafka/Secrets IAM and is **on-demand only** (no spot) |
| `dynamodb` | Convention | reuse `billing_mode = PAY_PER_REQUEST` + `enable_pitr`; prefix is `${project}-${env}` + the live table set |
| `s3` | Convention | reuse versioning + SSE + public-access-block + lifecycle; `${project}-${env}-${purpose}` doubles "backtest" |
| `monitoring` | Pattern | reuse SNS + alarm + log-group; it ships 29 live alarms + a Lambda + kill-switch wiring |
| `kafka` | None | offline lab — no MSK |

### `environments/backtest/` resources

- **Backend:** same `quantembrace-terraform-state` bucket, key `backtest/terraform.tfstate`, lock table `quantembrace-terraform-locks`; provider default-tag `Environment=backtest`.
- **New thin modules:** `s3_backtest` (the 2 canonical buckets), `dynamodb_backtest` (the 3 `qe-bt-` tables, on-demand, PITR on `qe-bt-runs`), `ec2_backtest_worker` (ARM64 **spot** ASG min/desired=0 + least-privilege IAM + explicit live-`Deny`), `monitoring_backtest` (SNS `quantembrace-backtest-alerts` + ~5 alarms in `QuantEmbrace/Backtest` + log group, **no Lambda**), and a minimal network.
- **Network (key cost lever):** separate VPC/CIDR (e.g. `10.40.0.0/16`, distinct from live `10.0.0.0/16`), one public subnet, IGW, **S3 + DynamoDB gateway endpoints (free); no NAT, no interface endpoints.** The worker reaches CloudWatch/SNS via public egress only while running.

### Safety checks — no live impact

1. Separate state key → Terraform cannot read or mutate live state.
2. Shared modules untouched → live `plan` is byte-identical (zero diff).
3. `terraform plan` in `backtest/` shows "N to add, **0 to change, 0 to destroy**" and zero live ARNs.
4. Disjoint name-spaces: `qe-bt-*` / `quantembrace-backtest-*` vs live `quantembrace-{dev,staging,prod}-*`.
5. Worker IAM allows only S3 `*-backtest-data` (read) / `*-backtest-results` (write), DynamoDB `qe-bt-*`, CloudWatch, SNS publish — with **explicit `Deny`** on live buckets/tables and Secrets Manager (no broker creds).
6. Separate VPC, egress-only SG, no peering; no Kafka/broker/live flags.
7. Enforced by `hooks/terraform_safety.yaml` + `safety-review-agent`.

### Estimated cost (ap-south-1, approximate)

| State | Drivers | Est. |
|---|---|---|
| **Idle** (ASG desired=0) | S3 ~$1–2 · DynamoDB+PITR <$1 · ~5 CloudWatch alarms ~$0.50 · SNS ~free · gateway endpoints free · **NAT avoided** | **≈ $3–5 / mo** |
| **Active** (~150 worker-hrs) | `c6g.large`/`c6g.xlarge` **spot** ARM ~$0.02–0.06/hr · S3/DDB/CloudWatch requests | **≈ $10–25 / mo** |
| **Marginal** | one full 10–15-yr daily run | **≈ $1–3 / run** |

Biggest lever is avoiding NAT (~$32/mo idle) via a public-subnet worker + free gateway endpoints; second is worker spot-hours. `c6g.large` on-demand ≈ $0.068/hr, `t4g.medium` ≈ $0.0336/hr; spot ≈ 60–70% off (region ±5%).

### Build steps (gated — do NOT apply)

1. **Prereqs:** confirm state bucket + lock table exist; pick CIDR; set `alert_email`; confirm AL2023 ARM64 AMI in ap-south-1.
2. **Scaffold** `environments/backtest/{main,variables,terraform.tfvars,outputs}.tf` (backend key `backtest/terraform.tfstate`).
3. **Author thin modules** (`s3_backtest`, `dynamodb_backtest`, `ec2_backtest_worker`, `monitoring_backtest`, minimal network); **leave shared modules unchanged**.
4. **Wire** modules; outputs = bucket/table names, ASG name, SNS ARN.
5. **`init` (new state) → `validate` → `plan`** → confirm 0-change / 0-destroy + no live ARNs; **write the Phase report and STOP for approval**.
6. **Apply later (gated):** smoke-test scale 0→1→0; verify least-privilege with a denied-action probe; return desired=0.

Maps to Phase 2 (`qe-bt-*` tables) and the infra introduced across Phases 1–10. Constraints honored: no Fargate/ECS/EKS, EC2 ARM64 ASG only, S3 + DynamoDB + CloudWatch/SNS minimum, no Lambda-for-compute, zero live diff, no apply.

### Spot safety (workers are interruption-tolerant)

The `backtest-worker` ASG runs **EC2 Spot** (ARM64). Safe because the lab is
offline/advisory and the registry + checkpoint hardening makes a reclaim lose ≤1
partition: per-shard `qe-bt-checkpoints` (`PK=run_id, SK=partition_id`, idempotent,
resume via `Query`) + an optimistic-`record_version` registry with a terminal-state
guard. **These hardenings are the Spot prerequisite — already implemented.**

- **Config:** Mixed Instances Policy over several ARM64 types (c6g/c7g/m6g) and all
  AZs, **capacity-optimized**; **Capacity Rebalance** on; on-demand fallback for
  *capacity* (not cost); `min = desired = 0`. Worker traps `SIGTERM` / the IMDSv2
  interruption signal → checkpoint + clean exit within the 2-minute window; EBS
  `delete_on_termination = true`.
- **Cost:** ~60–70% off on-demand → a full daily run ~$0.40–1 (vs ~$1–3); idle unchanged.
- **Unsafe for:** hard-deadline runs (use on-demand for those), non-idempotent writes,
  single-type/single-AZ pools, or anything touching live/paper (never on Spot).
- **Gate before a large fleet:** ship the interruption handler + tests — interrupt-vs-clean
  **golden equivalence** (resumed run == uninterrupted run), mid-partition kill, shard
  re-completion no-op, and output overwrite-not-append. (Per-shard no-clobber + registry
  optimistic-lock / terminal-guard tests already exist.) Small-scale Spot is safe now.
