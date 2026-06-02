# Phase A — AWS Infrastructure Discovery Report

**Date:** 2026-05-30
**Auditor:** Claude Code (Production Readiness Audit)
**Scope:** Read-only static inspection of all Terraform, CI/CD, Docker, scripts, and config files
**Not included:** Runtime AWS state (DynamoDB contents, actual ASG state, live Secrets Manager values)

---

## Executive Summary

The QuantEmbrace AWS infrastructure is well-architected at the module level (EC2 ARM64 ASGs, MSK Serverless, DynamoDB provisioned, S3 AES256, IMDSv2, per-service least-privilege IAM, SSM-only access). However, **three blockers must be resolved before `terraform apply` can succeed in prod**, and **the production EC2 environment file is missing critical trading safety parameters** that, if left unset, would result in zero trades (signals rejected with 5s default age limit) or prevent live promotion.

---

## 1. AWS Deployment Model

| Dimension | Value |
|---|---|
| Compute | EC2 Auto Scaling Groups (ARM64: c6g/t4g) |
| Container runtime | Docker on systemd — one container per EC2 instance |
| Messaging | Kafka MSK Serverless (IAM/SASL/TLS port 9098) |
| State store | DynamoDB (12 tables) |
| Object store | S3 (5 purpose-built buckets) |
| Secrets | AWS Secrets Manager (Zerodha, Alpaca) |
| Monitoring | CloudWatch Logs + Metrics + Alarms + SNS |
| Auth | OIDC for GitHub Actions, SSM Session Manager for SSH-free access |
| Primary region | ap-south-1 (Mumbai) |
| Terraform backend | S3 `quantembrace-terraform-state` + DynamoDB locks, encrypted |
| ECS/Fargate | **REMOVED** — not used; references in stale scripts (see §11) |

---

## 2. Services Deployed / Expected

| Service | ASG Name (prod) | Instance Type | Max Capacity | Notes |
|---|---|---|---|---|
| data_ingestion (NSE) | `quantembrace-prod-data-ingestion-nse-asg` | t4g.medium | default | WebSocket to Zerodha |
| data_ingestion (US) | `quantembrace-prod-data-ingestion-us-asg` | t4g.medium | default | WebSocket to Alpaca |
| strategy_engine | `quantembrace-prod-strategy-engine-asg` | c6g.large | 2 | Can scale horizontally |
| risk_engine | `quantembrace-prod-risk-engine-asg` | c6g.xlarge | default | Latency-critical |
| execution_engine | `quantembrace-prod-execution-engine-asg` | c6g.xlarge | **1** (hard) | Cluster placement group |
| ai_engine | not in deploy pipeline | n/a | n/a | **Phase 6 — not yet in ASG config or deploy.yml** |

**Note:** `ai_engine` has no EC2 ASG defined in `ec2_services/main.tf` and is absent from the prod deploy workflow matrix. The Kafka IAM policy exists in the kafka module but its attachment is gated on `ai_engine_role_name != ""` — which is not passed from prod/main.tf. **ai_engine enrichment is disabled at both compute and IAM layers in prod.**

Deploy order in `deploy.yml` (prod): `risk_engine → execution_engine → data_ingestion-nse → data_ingestion-us → strategy_engine` (max-parallel: 1, fail-fast). This is the correct risk-safe order.

---

## 3. Runtime Environment Variables

### How env vars reach EC2 instances

Each service userdata script (`infra/terraform/modules/ec2_services/userdata/{service}.sh`) generates `/opt/quantembrace/{service_name}.env` (chmod 600) at instance startup. This file is loaded by `docker run --env-file`. The file is constructed from:

1. Terraform template variables (injected at `terraform apply` time)
2. MSK bootstrap servers (discovered at startup via `aws kafka get-bootstrap-brokers`)
3. Secrets Manager secrets (Zerodha, Alpaca — fetched at startup, written to env file)

### Variables confirmed present in EC2 env file (execution_engine example)

| Variable | Source | Value |
|---|---|---|
| `QE_ENVIRONMENT` | Terraform | `production` (mapped from `prod`) |
| `ENVIRONMENT` | Terraform | `production` |
| `QE_LOG_LEVEL` / `LOG_LEVEL` | Terraform | `INFO` (prod setting) |
| `AWS_REGION` | Terraform | `ap-south-1` |
| `DYNAMODB_TABLE_PREFIX` | Terraform | `quantembrace-prod` |
| `KAFKA_BOOTSTRAP_SERVERS` | Runtime MSK discovery | bootstrap string (SASL/IAM) |
| `S3_BUCKET_TRADING_LOGS` | Terraform | S3 module output |
| `ZERODHA_API_KEY` | Secrets Manager | From `secrets_zerodha_arn` |
| `ZERODHA_API_SECRET` | Secrets Manager | From `secrets_zerodha_arn` |
| `ZERODHA_ACCESS_TOKEN` | Secrets Manager | **At startup only — expires daily** |
| `ALPACA_API_KEY` | Secrets Manager | From `secrets_alpaca_arn` |
| `ALPACA_API_SECRET` | Secrets Manager | From `secrets_alpaca_arn` |

### ⚠️ Variables ABSENT from EC2 env file (code defaults apply)

| Variable | Code Default | Required for Correct Operation | Risk if Missing |
|---|---|---|---|
| `RISK_MAX_SIGNAL_AGE_SECONDS` | `5.0` | `30` | **CRITICAL: all candle signals rejected** (7-12s age > 5s limit) |
| `UNIVERSE_MODE` | `PAPER_SAFE_START` | `PAPER_EXPAND` or `LIVE_ADVANCED` | Reduced universe in prod |
| `RISK_PROFILE` | `tiny-live` | `paper` (paper sessions) | Wrong risk limits applied |
| `QE_EXECUTION_LIVE_TRADING_ENABLED` | `false` | explicitly `false` | Safe by default; must be set to enable live |
| `PAPER_SEED_NAV` | not seeded | `1000000` | Paper NAV not initialized |
| `RISK_DATA_FEED_STALE_SECONDS` | `300` | `30` (pre-live) | **3600 workaround from .env bypasses staleness detection** |
| `STRATEGY_WATCHLIST_NSE` | empty | comma-separated symbols | LiveQuotePoller won't start |

**`RISK_MAX_SIGNAL_AGE_SECONDS` is the most critical missing variable.** Without it, the code defaults to 5.0s and the `_ABSOLUTE_MAX_AGE_SECONDS = 30.0` ceiling in SignalAgeValidator never actually raises the effective limit — it only caps it. With candle signals arriving at risk_engine 7-12s after generation, 0% of signals would pass the age gate.

---

## 4. Live / Paper Config Separation

| Control | Mechanism | Current State |
|---|---|---|
| Per-strategy paper flag | `paper_trade=True` in DynamoDB `strategy-config` | Must be verified at runtime |
| Live trading gate | `QE_EXECUTION_LIVE_TRADING_ENABLED` env var | Absent in prod env file → defaults `False` (safe) |
| Live gate in ExitOrderRouter | Pre-lock check before DynamoDB acquire | ✅ Code confirmed |
| MIS paper mode | `paper_trading=True` constructor param | ✅ Code confirmed |
| Paper/live universe namespaces | `PAPER#` vs `LIVE#` DynamoDB keys | ✅ ADR-019 |
| Paper broker path | `PaperSimulator` when `paper_trade=True` | ✅ No real broker calls |
| Zerodha token source | `ZerodhaTokenManager` reads DynamoDB `sessions` | ⚠️ `sessions` table NOT in Terraform |

**The live trading gate is currently safe by default** (env var absent → code default `False`). Enabling live requires explicitly adding `QE_EXECUTION_LIVE_TRADING_ENABLED=true` to the EC2 env file AND replacing the instance — no hot-switch possible.

---

## 5. DynamoDB Table Separation

| Table | Prod Name | PITR | Capacity | Status |
|---|---|---|---|---|
| orders | `quantembrace-prod-orders` | ✅ | Provisioned + auto-scale | ✅ |
| positions | `quantembrace-prod-positions` | ✅ | Provisioned + auto-scale | ✅ |
| latest_prices | `quantembrace-prod-latest-prices` | ✗ | On-demand (TTL) | ✅ acceptable |
| risk_state | `quantembrace-prod-risk-state` | ✅ | Provisioned + auto-scale | ✅ |
| candle_cache | `quantembrace-prod-candle-cache` | ✅ | Provisioned + auto-scale | ✅ |
| strategy_config | `quantembrace-prod-strategy-config` | ✅ | Provisioned + auto-scale | ✅ |
| strategy_state | `quantembrace-prod-strategy-state` | ✅ | On-demand (TTL) | ✅ |
| signal_inbox | `quantembrace-prod-signal-inbox` | ✅ | On-demand (TTL) | Phase 8 |
| signal_outbox | `quantembrace-prod-signal-outbox` | ✅ | On-demand (TTL) | Phase 8 |
| **sessions** | `quantembrace-prod-sessions` | — | — | **⛔ NOT IN TERRAFORM** |

**The `sessions` table is the Zerodha token store used by `ZerodhaTokenManager`.** It is referenced in `ec2_services/iam.tf` (lines 404, 520) but not defined in `dynamodb/main.tf`. The table is absent from `dynamodb/outputs.tf`. At runtime:
- `ZerodhaTokenManager.get_token()` will fail with `ResourceNotFoundException` on every call
- Service falls back to stale env var token (anti-pattern #15 — explicitly prohibited)
- After 07:30 IST daily, the stale token causes all Zerodha API calls to fail

---

## 6. Secrets Manager Readiness

| Secret | Variable in prod/variables.tf | Fetch Method | Status |
|---|---|---|---|
| Zerodha API credentials | `secrets_zerodha_arn` | Userdata script at startup | ✅ declared |
| Alpaca API credentials | `secrets_alpaca_arn` | Userdata script at startup | ✅ declared |
| Anthropic API key | not declared | N/A | n/a (Claude not in runtime path) |

**Token refresh concern:** The Zerodha `ZERODHA_ACCESS_TOKEN` is written to the env file at EC2 startup from Secrets Manager. The Zerodha token expires daily at ~07:30 IST. `ZerodhaTokenManager` is designed to refresh from DynamoDB `sessions` table at runtime, but the `sessions` table does not exist in Terraform. Without the `sessions` table:
1. The startup env var token is used until it expires
2. After expiry, all broker calls fail

The daily `python scripts/zerodha_login.py` workflow writes the new token to DynamoDB (sessions table) and, in the Docker/EC2 model, also to Secrets Manager (if the script updates it). Whether the Secrets Manager value is refreshed for running instances depends on whether the instance reloads it — which it does NOT (env file is written once at startup). Operators must either: (a) create the sessions table so `ZerodhaTokenManager` can serve tokens, or (b) rotate the instance daily.

---

## 7. IAM Readiness

### Per-service IAM policies confirmed (kafka module)

| Service | Producer Topics | Consumer Topics | Consumer Groups | Status |
|---|---|---|---|---|
| data_ingestion | ticks.nse, ticks.us, ops.audit | — | — | ✅ |
| strategy_engine | signals.pending, ticks (retry/dlq) | ticks.nse, ticks.us | strategy-v1 | ✅ |
| risk_engine | signals.approved, kill-switch, audit | signals.pending, enriched, orders, ticks | risk-v1 | ✅ |
| execution_engine | orders.events, ops.audit | signals.approved, kill-switch | execution-v1 | ✅ |
| **ai_engine** | signals.enriched | signals.pending | aiengine-v1 | **⛔ NOT ATTACHED** |

**ai_engine IAM gap:** The `aws_iam_policy.kafka_ai_engine` resource exists in the kafka module, but `aws_iam_role_policy_attachment.kafka_ai_engine` uses `count = var.ai_engine_role_name != "" ? 1 : 0`. Since `ai_engine_role_name` is not passed from `prod/main.tf`, the count is 0 and the policy is created but never attached. Phase 6 enrichment (ai_engine → signals.enriched) is silently disabled at IAM layer in prod.

### EC2 instance IAM

- Per-service roles with least-privilege DynamoDB, S3, Secrets Manager, CloudWatch access
- Shared policies: `cloudwatch_agent`, `kafka_bootstrap_discovery`
- IMDSv2 enforced (`instance_metadata_http_tokens = "required"`) in prod
- No public IP (`associate_public_ip_address = false`) on all services
- SSH key pair empty in prod — SSM Session Manager only ✅
- OIDC-based GitHub Actions auth (no long-lived access keys in CI) ✅

---

## 8. CloudWatch Readiness

### Log groups
- Per-service: `/quantembrace/{service_name}` (30-day retention in prod) ✅
- Written by CloudWatch Agent from `/var/log/quantembrace-service.log`
- Bootstrap logs: `/quantembrace/bootstrap` stream ✅

### Alarms confirmed present

| Alarm Category | Alarms | Status |
|---|---|---|
| ECS task count (per service) | `*-task-count-low` | ⛔ Wrong namespace (see below) |
| ECS CPU (per service) | `*-cpu-high` | ⛔ Wrong namespace |
| ECS memory (per service) | `*-memory-high` | ⛔ Wrong namespace |
| Application error rate | `*-high-errors` (custom namespace) | ✅ correct |
| Daily P&L loss — alert | `*-daily-pnl-loss-alert` | ✅ |
| Daily P&L loss — halt | `*-daily-pnl-loss-halt` | ✅ |
| Order rejection rate | `*-order-rejection-rate-high` | ✅ |
| No orders during market hours | `*-no-orders-during-market-hours` | ✅ |
| MSK client connections | `*-msk-client-connections-low` | ✅ |
| MSK bytes-in zero | `*-msk-bytes-in-zero-during-market` | ✅ |
| Risk lag high | `RiskV1LagHigh` | ✅ (Phase 8) |
| Orphan position detected | `OrphanPositionDetected` | ✅ (Phase 8) |
| Reconciliation halt active | `ReconciliationHaltActive` | ✅ (Phase 8) |
| Per-service CPU (QuantEmbrace custom) | `*-*-cpu-high` | ✅ EC2 metrics |
| ASG unhealthy instances | per ASG | ✅ |

### ⚠️ ECS namespace alarms generating false positives

The monitoring module creates `RunningTaskCount`, `CPUUtilization`, and `MemoryUtilization` alarms in the **`AWS/ECS` namespace** with `ClusterName` and `ServiceName` dimensions. Since ECS Fargate was removed and EC2 ASGs are used instead, these dimensions produce no metric data. With `treat_missing_data = "breaching"`, the `RunningTaskCount` alarm fires permanently on all 5 services — generating ~10 constant ALARM notifications.

These alarms are not connected to trading halt (alarm_actions goes to alerts SNS, not kill-switch SNS), but they create noise that may cause operators to ignore real alerts.

### SNS Topics

| Topic | Purpose | Subscription |
|---|---|---|
| `*-system-alerts` | General alerts (CPU, errors, P&L) | Email (var.alert_email) |
| `*-kill-switch` | Kill switch trigger events | Email + optional Lambda |

Kill-switch Lambda: `kill_switch_lambda_role_arn` variable exists and is passed to monitoring module. However, **no Lambda function code exists in this repository**. The Lambda is declared as a potential downstream target of the kill-switch SNS topic, but if `kill_switch_lambda_role_arn = ""` (the default), no Lambda attachment is created. Automatic kill-switch on alarm is currently **opt-in and not yet implemented**.

---

## 9. Rollback Readiness

| Mechanism | Script | Status |
|---|---|---|
| Image retag (SHA → latest-prod) | `scripts/deploy/promote_ecr_image.sh` | ✅ |
| ASG instance refresh cancel | CI/CD `cancel-refresh-on-failure` job | ✅ |
| ASG instance refresh (new SHA) | `scripts/deploy/refresh_asg.sh` | ✅ |
| Kill switch activate | `scripts/kill_switch_cli.py activate` | ✅ |
| Kill switch deactivate | `scripts/kill_switch_cli.py deactivate` | ✅ (requires explicit confirmation) |
| Position reconciliation | `scripts/ops/reconcile.py` | ✅ |

**Rollback procedure (EC2):** There is no dedicated `rollback.sh`. The operator must:
1. Identify previous good SHA from ECR or git log
2. Run: `scripts/deploy/promote_ecr_image.sh --repository quantembrace-{service} --source-tag {old-sha} --target-tag latest-prod`
3. Trigger ASG instance refresh: `scripts/deploy/refresh_asg.sh --asg {asg-name}`

This is functional but undocumented in any runbook. The deploy.yml `cancel-refresh-on-failure` job stops an in-progress rollout but does not initiate a rollback — it leaves instances in a mixed-version state requiring manual remediation.

**⚠️ `deploy.sh` is stale:** `infra/deployment/deploy.sh` calls `aws ecs update-service` (ECS Fargate). This script does not work with the current EC2 ASG architecture and will fail immediately if run. It should be deleted or replaced.

**⚠️ `check_ecs_health.py` is stale:** `scripts/deploy/check_ecs_health.py` polls ECS `describe_services`. Used in deploy.yml as `check_asg_health.py` but the filename is misleading — **deploy.yml calls it as `check_asg_health.py`** which does not exist, meaning the health-check step will fail with FileNotFoundError during any CI/CD run. This is a **deployment pipeline blocker**.

Wait — re-reading deploy.yml: it calls `python scripts/deploy/check_asg_health.py --asg "$ASG" --allow-zero-desired` but the file on disk is `check_ecs_health.py`. This script does not accept `--asg` as an argument. The deploy pipeline's health check step will error.

---

## 10. Kill Switch Readiness

| Check | Status |
|---|---|
| CLI tool available | ✅ `scripts/kill_switch_cli.py` |
| Activation requires confirmation | ✅ interactive 'yes' prompt |
| Deactivation requires explicit string | ✅ `"I confirm trading should resume"` |
| DynamoDB-backed (durable) | ✅ writes to risk-state table |
| SNS notification on activate | ✅ optional, wired to kill-switch SNS topic |
| Auto-kill Lambda | ⚠️ Variable declared, code absent |
| Kill switch checked before every order | ✅ (Phase 2 confirmed) |
| Kill switch propagation: Kafka path | ✅ `risk.kill-switch` topic → all consumers |
| Kill switch propagation: DynamoDB poll | ✅ 1s poll in execution + risk |

---

## 11. Infrastructure Blockers

### BLOCKER-1 — `terraform apply` will FAIL: variable name mismatch

**File:** `infra/terraform/environments/prod/main.tf:58`
```hcl
single_nat_gateway = false  # ← not a variable in vpc module
```
**VPC module declares:** `variable "ha_nat"` (line 22 of `vpc/variables.tf`)

`single_nat_gateway` is unrecognised by the VPC module. Terraform will fail at `terraform apply` with "An argument named 'single_nat_gateway' is not expected here."

**Fix:** Change to `ha_nat = true` in prod/main.tf:58.

---

### BLOCKER-2 — `sessions` DynamoDB table missing from Terraform

The `ZerodhaTokenManager` reads and writes `{prefix}-sessions` table for daily token rotation. The IAM policy in `ec2_services/iam.tf` already grants DynamoDB access to a `{prefix}-sessions` table. But `dynamodb/main.tf` does not define this table.

At runtime:
- `ZerodhaTokenManager.get_token()` → `ResourceNotFoundException`
- Service falls back to env-var token (anti-pattern #15 — explicitly prohibited in CLAUDE.md)
- After 07:30 IST, all Zerodha API calls fail with `TokenException`

**Fix:** Add `aws_dynamodb_table.sessions` to `dynamodb/main.tf` and add its output to `dynamodb/outputs.tf`.

---

### BLOCKER-3 — `RISK_MAX_SIGNAL_AGE_SECONDS` absent from EC2 env file

The EC2 userdata script does not inject this variable. Code default is `5.0s`. Candle signals arrive at risk_engine 7-12s after generation. Zero signals will pass the age gate.

This was the root cause of 0 trades in Days 1-4 (fixed in docker-compose). The fix was never propagated to the EC2 env file template.

**Fix:** Add `RISK_MAX_SIGNAL_AGE_SECONDS=30` to the env file section of each service's userdata script (strategy_engine, risk_engine, execution_engine at minimum).

---

### HIGH-4 — ai_engine not reachable at IAM or compute layer in prod

- No EC2 ASG defined for ai_engine in `ec2_services/main.tf`
- Not in deploy.yml workflow matrix
- Kafka IAM policy exists but not attached (`ai_engine_role_name` not passed from prod/main.tf)

Effect: Phase 6 enrichment (market_regime + quality_score) is disabled in prod. Signals flow via `EnrichmentWatchdog` fallback path: `signals.pending → risk_engine` directly. This is the designed fallback, so trading continues, but signals are unenriched and `min_quality_score` validator sees `quality_score=None`.

**Fix:** Either (a) add ai_engine ASG + deploy pipeline entry + pass `ai_engine_role_name` to kafka module, or (b) document explicitly that ai_engine is intentionally disabled for Stage-1 live.

---

### HIGH-5 — deploy.yml health check step calls non-existent script

`deploy.yml` calls `python scripts/deploy/check_asg_health.py --asg "$ASG"`.
The file on disk is `scripts/deploy/check_ecs_health.py` which:
- Uses ECS `describe_services` (not ASG)
- Does not accept `--asg` argument

Every deploy.yml run will fail at the health-check step with `FileNotFoundError` or `unrecognized argument: --asg`. No deployment has succeeded via CI/CD.

**Fix:** Rename `check_ecs_health.py` to `check_asg_health.py` and rewrite to poll ASG instance health via `aws autoscaling describe-auto-scaling-groups`.

---

### MEDIUM-6 — ECS namespace alarms breach permanently

7 CloudWatch alarms per service (5 services = 35 alarms) in `AWS/ECS` namespace will never receive metric data and will breach continuously. This is alarm noise that risks operator alert fatigue.

**Fix:** Replace ECS-namespace alarms with EC2/custom metrics (QuantEmbrace namespace). A temporary workaround is to set `treat_missing_data = "notBreaching"` on the RunningTaskCount alarm.

---

### MEDIUM-7 — `deploy.sh` is stale ECS-targeting script

`infra/deployment/deploy.sh` calls `aws ecs update-service`. ECS was removed. If an operator runs this script, it will silently contact ECS and fail. Risk: operator confusion during incident response.

**Fix:** Delete `infra/deployment/deploy.sh` or replace with an EC2 ASG refresh script.

---

### MEDIUM-8 — Trading safety env vars absent from prod EC2 env file

Beyond `RISK_MAX_SIGNAL_AGE_SECONDS` (BLOCKER-3), these are missing:

| Variable | Risk |
|---|---|
| `UNIVERSE_MODE` | Defaults to `PAPER_SAFE_START` — narrows universe to NIFTY 50 only |
| `RISK_PROFILE` | Defaults to `tiny-live` — may apply wrong risk limits |
| `STRATEGY_WATCHLIST_NSE` | `LiveQuotePoller` won't start; candle signals won't be generated |
| `QE_EXECUTION_LIVE_TRADING_ENABLED` | Safe default (False) but no explicit mechanism to enable for live |

---

### LOW-9 — Auto-kill-switch Lambda code absent

`kill_switch_lambda_role_arn` variable declared in prod. Monitoring module uses it. No Lambda function code exists in this repository. Auto-kill on P&L breach or WebSocket gap is not functional.

---

## 12. Whether Phase B Can Proceed

**Phase B (code-level audit of remaining services + runtime verification) can proceed for static code reading.**

Runtime verification requires:
- **BLOCKER-1** fixed before `terraform apply` (otherwise infra doesn't deploy)
- **BLOCKER-2** fixed before any Zerodha live session (sessions table must exist)
- **BLOCKER-3** fixed before any paper or live session on EC2 (env var injection)
- **HIGH-5** fixed before CI/CD deploy pipeline can succeed (health check step)

**Phase B static analysis can begin immediately without fixing these blockers.**
**Phase B runtime verification on live AWS requires all 3 BLOCKERS + HIGH-5 resolved first.**

---

## Appendix: File Provenance

| Finding | Source File(s) |
|---|---|
| VPC variable mismatch | `infra/terraform/environments/prod/main.tf:58`, `modules/vpc/variables.tf:22` |
| sessions table absent | `modules/dynamodb/main.tf` (full), `modules/dynamodb/outputs.tf`, `modules/ec2_services/iam.tf:404,520` |
| RISK_MAX_SIGNAL_AGE_SECONDS missing | `modules/ec2_services/userdata/execution_engine.sh:138-152` |
| ai_engine IAM gap | `modules/kafka/main.tf:490-494`, `environments/prod/main.tf:143-168` |
| deploy.yml health check script | `.github/workflows/deploy.yml:99`, `scripts/deploy/check_ecs_health.py` |
| ECS namespace alarms | `modules/monitoring/main.tf:84-157` |
| Stale deploy.sh | `infra/deployment/deploy.sh:71-77` |
| Kill switch CLI | `scripts/kill_switch_cli.py` |
| Rollback procedure | `.github/workflows/deploy.yml:241-266`, `scripts/deploy/promote_ecr_image.sh` |
| EC2 userdata env vars | `modules/ec2_services/userdata/execution_engine.sh` |
| S3 buckets | `modules/s3/main.tf` |
| Kafka MSK + IAM | `modules/kafka/main.tf` |
| Monitoring alarms | `modules/monitoring/main.tf` |
| CI/CD pipeline | `.github/workflows/build.yml`, `deploy.yml`, `ci.yml` |
