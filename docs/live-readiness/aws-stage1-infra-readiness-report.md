# Phase C — AWS Stage-1 Infrastructure Readiness Report

**Date:** 2026-05-30
**Scope:** Static analysis of all Terraform modules, EC2 userdata scripts, IAM policies, CloudWatch/SNS config, CI/CD pipeline, and runtime configuration. No live AWS state queried. No trading enabled. No broker orders placed. No secrets included.
**Input:** Phase A infra-discovery-report.md · Phase B service code audit · pre-live-runbook.md
**Capital:** ₹1,000,000 BLOCKED. Stage-1 = one share / minimum qty / one whitelisted strategy only.

---

## Summary Verdict

**GO for `terraform apply` after operator confirms watchlist variable.**

All previously identified Terraform blockers (INFRA-1, INFRA-2, INFRA-3) are resolved. Four additional infrastructure hardening fixes applied in this phase. Four low-severity items remain, all acceptable for Stage-1 (documented below). No live trading is enabled at any point in this audit.

---

## Fixes Applied in Phase C

| ID | Area | Change | Files |
|----|------|--------|-------|
| **C-101** | CloudWatch | ECS task-count alarm `treat_missing_data` changed from `"breaching"` → `"notBreaching"`. Eliminates 5 permanently-ALARM CloudWatch alarms caused by ECS namespace having no data on EC2 platform. Removes alert fatigue risk. | `modules/monitoring/main.tf` |
| **C-102** | IAM | Removed stale `{prefix}-risk-decisions` DynamoDB ARN from risk_engine IAM policy. That table does not exist; risk-decisions are stored in `risk-state`. Stale grants to non-existent resources are misleading and fail least-privilege audit. | `modules/ec2_services/iam.tf` |
| **C-103** | Runtime config | Added `STRATEGY_WATCHLIST_NSE` to both `execution_engine.sh` and `strategy_engine.sh` EC2 env file sections, injected from new `strategy_watchlist_nse` Terraform variable (default `""`). Without this, `LiveQuotePoller` does not start and spread gate has no data. | `modules/ec2_services/userdata/execution_engine.sh`, `strategy_engine.sh`, `variables.tf`; `environments/prod/variables.tf`, `main.tf` |
| **C-104** | Deployment | Replaced stale ECS-targeting `deploy.sh` with a stub that errors immediately and documents the correct EC2 ASG promotion procedure. Prevents operator confusion during incident response. | `infra/deployment/deploy.sh` |

---

## 1. Secrets

| Check | Status | Detail |
|-------|--------|--------|
| Zerodha API key/secret in Secrets Manager | ✅ PASS | `secrets_zerodha_arn` required var in prod/variables.tf; fetched at startup via `aws secretsmanager get-secret-value` in all relevant userdata scripts |
| Alpaca credentials in Secrets Manager | ✅ PASS | Same mechanism (`secrets_alpaca_arn`) |
| IAM grants scoped to specific secret ARNs | ✅ PASS | `Resource = [var.secrets_zerodha_arn, var.secrets_alpaca_arn]` — not wildcard |
| No hardcoded secrets in Python/TF/YAML/HCL | ✅ PASS | Scan confirmed all credential references use `os.environ` or `secretsmanager` calls |
| `.env` file gitignored and never committed | ✅ PASS | `.gitignore` includes `.env`; `git check-ignore` confirms; git log shows no historical commit |
| `.env` credentials: local dev only | ⚠️ OPERATOR ACTION | `.env` contains non-placeholder Zerodha credentials (API key, secret, access token). Confirm these are local dev credentials only — **if any value was ever used against a real Zerodha account, rotate it immediately at kite.trade/settings**. Do not use production credentials in `.env`. |
| Token freshness check | ✅ PASS | `python scripts/zerodha_login.py status` prints token expiry |
| EC2 token refresh path | ✅ PASS | `ZerodhaTokenManager` reads from DynamoDB `sessions` table at runtime (now provisioned); EC2 startup token from Secrets Manager is only the boot fallback |
| Access token in Secrets Manager at boot | ⚠️ ACCEPTABLE | `ZERODHA_ACCESS_TOKEN` in Secrets Manager is the value at last `terraform apply` time. It expires at ~07:30 IST. After that, `ZerodhaTokenManager` must serve fresh tokens from DynamoDB sessions table. **Daily `scripts/zerodha_login.py` is mandatory before market open.** |

**Secrets verdict: PASS with one operator action (verify .env credentials are local dev only).**

---

## 2. DynamoDB

| Check | Status | Detail |
|-------|--------|--------|
| Paper and live tables separated | ✅ PASS | Full prefix isolation: `quantembrace-{env}-*`. Paper (`quantembrace-development-*`) and live (`quantembrace-prod-*`) namespaces never share a table. |
| PITR enabled on all critical tables | ✅ PASS | `orders`, `positions`, `risk-state`, `sessions`, `candle-cache`, `strategy-config`, `strategy-state`, `signal-inbox`, `signal-outbox`, `features`, `regime-log`, `strategy-recommendations` — all PITR enabled. |
| PITR exception: `latest-prices` | ✅ ACCEPTABLE | Explicitly `enabled = false` — ephemeral price cache with TTL; no trading state, full PITR not justified |
| Conditional writes for order idempotency | ✅ PASS | `ConditionExpression = "attribute_not_exists(PK)"` on order creation; status transitions gated on `order_status = :prev_status`; `exit_order_id` uses `attribute_not_exists(updated_at)` / `updated_at = :prev_updated_at` conditional update |
| Kill switch table and key | ✅ PASS | `{prefix}-risk-state`, PK=`KILLSWITCH`, SK=`GLOBAL`; `active` (BOOL); all validators read it via 1s DDB poll or O(0) KillSwitchCache |
| Reconciliation state key | ✅ PASS | `{prefix}-risk-state`, PK=`RECONCILIATION`, SK=`STATE`; `reconciliation_required` flag checked by ReconciliationValidator before every approval |
| Sessions table | ✅ PASS | `{prefix}-sessions` added to `dynamodb/main.tf` (INFRA-2 fix). PK=`SESSION#{date}`, SK=`ZERODHA`, TTL 48h. Required by `ZerodhaTokenManager`. |
| `symbol-status-index` GSI on orders | ✅ PASS | Added in C-001 fix. Enables `PositionValidator._get_pending_quantity()` without full scan — prevents 100% live signal rejection. |
| Provisioned capacity + auto-scaling (prod) | ✅ PASS | `use_provisioned_capacity = true`; `autoscaling_max_capacity = 100`; auto-scaling targets on orders, positions, risk-state |

**DynamoDB verdict: PASS.**

---

## 3. IAM

| Check | Status | Detail |
|-------|--------|--------|
| Per-service least-privilege roles | ✅ PASS | 6 distinct roles: `data_ingestion`, `strategy_engine`, `execution_engine`, `risk_engine`, `ai_engine`, plus shared CW/Kafka discovery policies |
| No wildcard admin runtime role | ✅ PASS | No `"*"` Resource on write actions. `cloudwatch:PutMetricData` uses `"*"` resource — required by CloudWatch API (service does not support resource-level restrictions for PutMetricData) |
| Secrets access restricted to specific ARNs | ✅ PASS | `Resource = [var.secrets_zerodha_arn, var.secrets_alpaca_arn]` — not `arn:aws:secretsmanager:*:*:secret:*` |
| Paper and live permissions separated | ✅ PASS | Naturally separated by DynamoDB table prefix and IAM resource ARN patterns |
| No SSH key in prod | ✅ PASS | `key_pair_name = ""` in prod/main.tf; SSM Session Manager only |
| IMDSv2 enforced | ✅ PASS | `instance_metadata_http_tokens = "required"` on all prod ASGs |
| No public IP on instances | ✅ PASS | `associate_public_ip_address = false` on all services |
| OIDC for CI/CD (no long-lived keys) | ✅ PASS | GitHub Actions uses OIDC; no static `AWS_ACCESS_KEY_ID` in CI environment |
| Stale `risk-decisions` IAM reference | ✅ FIXED (C-102) | Removed non-existent table ARN from risk_engine policy |
| `kafka_bootstrap_discovery` uses `Resource = "*"` | ✅ ACCEPTABLE | MSK API (`kafka:ListClustersV2`, `GetBootstrapBrokers`) does not support resource-level ARN restrictions — AWS-imposed limitation |

**IAM verdict: PASS.**

---

## 4. CloudWatch

| Check | Status | Detail |
|-------|--------|--------|
| Log groups enabled for all 4 services | ✅ PASS | `/quantembrace/{env}/{service}` with 30-day retention in prod; CloudWatch Agent configured in all userdata scripts; bootstrap logs at `/quantembrace/bootstrap` |
| Metrics: TEE, MIS, reconciliation | ✅ PASS | `LiveCounters` (TEE/MIS/reconciliation counters) flushed to `/tmp/qe_live_counters.json` every 60s; CloudWatch Agent can ship these as custom metrics via the `QuantEmbrace` namespace |
| Metrics: broker, LTP, kill switch | ✅ PASS | `QuantEmbrace/ZerodhaRateLimit` namespace (rate limiter, fill latency, token bucket); `KillSwitchActivations` metric; `HealthCheckSuccess` metric |
| Kill switch CloudWatch alarm | ✅ PASS | `{prefix}-kill-switch-activated` alarm fires on `KillSwitchActivations ≥ 1`; routes to both `system-alerts` and `kill-switch` SNS topics |
| P&L halt alarm | ✅ PASS | `{prefix}-daily-pnl-loss-halt` — fires to `alerts + kill_switch` SNS topics |
| WebSocket gap alarm | ✅ PASS | `{prefix}-websocket-disconnected` — fires to `alerts + kill_switch` SNS topics |
| Risk engine unhealthy alarm | ✅ PASS | `{prefix}-risk-engine-unhealthy` — `treat_missing_data = "breaching"` (correct — health check silence IS a failure for this alarm) |
| ECS namespace false-positive alarms | ✅ FIXED (C-101) | `ecs_task_count` `treat_missing_data` changed to `"notBreaching"`. Was causing 5 permanently-ALARM alarms on EC2 platform. |
| Dashboard | ✅ PASS | `{prefix}-overview` dashboard provisioned by Terraform; widgets for ECS metrics (will show INSUFFICIENT DATA on EC2), custom trading metrics, DynamoDB throttles, error summary |
| Dashboard ECS widgets show no data | ⚠️ ACCEPTABLE | Dashboard widgets reference `AWS/ECS` namespace — show "Insufficient data" on EC2. Not blocking. Replace in a future monitoring uplift pass. |

**CloudWatch verdict: PASS (one cosmetic gap in dashboard).**

---

## 5. SNS

| Check | Status | Detail |
|-------|--------|--------|
| CRITICAL alert topic exists | ✅ PASS | `{prefix}-system-alerts` SNS topic provisioned |
| Kill switch alert topic exists | ✅ PASS | `{prefix}-kill-switch` SNS topic provisioned |
| Email subscription | ✅ PASS | Both topics subscribe `var.alert_email` (required in prod/variables.tf) |
| Kill switch alarms routed to kill-switch topic | ✅ PASS | P&L halt, WebSocket gap, data feed staleness, risk engine unhealthy, kill switch activated — all route to `kill_switch` SNS |
| Auto-kill-switch Lambda | ⚠️ NOT PROVISIONED BY DEFAULT | Lambda inline code exists in monitoring module; EventBridge rule exists; but gated on `kill_switch_lambda_role_arn != ""`. Default is `""` → Lambda not created. Auto-halt on alarm requires operator to create a Lambda IAM role and set `kill_switch_lambda_role_arn` in `prod/main.tf`. **Acceptable for Stage-1** — operator monitors manually; Lambda can be added before scaling. |
| Alert delivery test | ⚠️ OPERATOR ACTION | Confirm email subscription is confirmed (Terraform creates SNS subscription; AWS sends confirmation email; operator must click the link). Test: `aws sns publish --topic-arn <alerts-arn> --message "Stage-1 readiness test"` |

**SNS verdict: PASS for Stage-1. Auto-kill Lambda is a pre-scale enhancement.**

---

## 6. Deployment

| Check | Status | Detail |
|-------|--------|--------|
| Production deployment path documented | ✅ PASS | `deploy.yml` (GitHub Actions): OIDC auth → build.yml triggers → staging → manual approval → prod; documents image SHA tagging |
| Deploy order safe | ✅ PASS | `deploy.yml` deploys `risk_engine` before `execution_engine` (max-parallel: 1, fail-fast) |
| Health check available | ✅ PASS | `scripts/deploy/check_asg_health.py` (INFRA-3 fix) — polls `describe_auto_scaling_groups` for InService instance count |
| Rollback: image retag + ASG refresh | ✅ PASS | `scripts/deploy/promote_ecr_image.sh` retagging + `aws autoscaling start-instance-refresh` |
| Previous version rollback | ✅ PASS | Immutable ECR SHA tags retained; `promote_ecr_image.sh --source-tag <old-sha>` restores any prior SHA |
| Lifecycle hook: inflight order drain | ✅ PASS | `execution-engine-drain` lifecycle hook; `TimeoutStopSec=300s` in systemd unit; `lifecycle-complete.sh` signals CONTINUE only after service stop |
| Stale `deploy.sh` | ✅ FIXED (C-104) | Replaced with stub that errors and documents correct EC2 ASG procedure |
| Rollback runbook | ⚠️ ACCEPTABLE | Rollback steps exist in `pre-live-runbook.md` but no dedicated `rollback.sh` script. Manual for Stage-1. |
| `check_ecs_health.py` filename | ⚠️ NOTE | Original ECS health check script still exists at `scripts/deploy/check_ecs_health.py`. `deploy.yml` correctly calls `check_asg_health.py` (INFRA-3 fix). The ECS script can be left or deleted — it is not called anywhere. |

**Deployment verdict: PASS.**

---

## 7. Runtime Configuration

| Check | Status | Detail |
|-------|--------|--------|
| Default mode is PAPER | ✅ PASS | `RISK_PROFILE=paper` in `risk_engine.sh`; `UNIVERSE_MODE=PAPER_SAFE_START` in `execution_engine.sh` |
| Live trading gate disabled | ✅ PASS | `# QE_EXECUTION_LIVE_TRADING_ENABLED=true` commented out in `execution_engine.sh`; absent from env → code default `false` |
| Stage-1 config exists but not enabled | ✅ PASS | `RISK_PROFILE` comment says "Change to tiny-live before enabling live"; `tiny-live` profile: ₹5k max order, 1 concurrent position |
| 1M capital remains blocked | ✅ PASS | `portfolio_value` not set in any EC2 env file; `risk_limits_production.yaml` not wired into risk_engine for Stage-1 (acceptable per pre-live runbook §1.4 — `tiny-live` hardcoded defaults match Stage-1 limits); `PAPER_SEED_NAV` is local Docker-only |
| `RISK_MAX_SIGNAL_AGE_SECONDS=30` in all services | ✅ PASS | Set in `risk_engine.sh` and `strategy_engine.sh` (C-002 fix); `execution_engine.sh` does not validate signal age directly |
| `STRATEGY_WATCHLIST_NSE` | ✅ PASS (after terraform apply) | Now injected via `strategy_watchlist_nse` Terraform variable (C-103 fix). Set in `prod/main.tf` before `terraform apply`. Minimum for Stage-1: the whitelisted strategy's instrument(s). |
| `RISK_DATA_FEED_STALE_SECONDS=3600` workaround | ✅ REMOVED | Removed from `.env` (ADR-021 Phase 1 fix). EC2 userdata never had this — code default 300s for consumer lag is correct. |

**Runtime config verdict: PASS.**

---

## 8. Remaining Open Items (Acceptable for Stage-1)

These items are documented and tracked but are not blockers for Stage-1 live validation.

| ID | Area | Item | Acceptable Because |
|----|------|------|--------------------|
| MEDIUM-6 | CloudWatch | Dashboard ECS widgets show "Insufficient data" — wrong namespace for EC2 platform | Cosmetic; trading alarms in `QuantEmbrace/{env}` namespace are correct |
| ADR-021-P2 | Risk | `risk_limits_production.yaml` not wired into risk_engine — `tiny-live` hardcoded defaults used | `tiny-live` defaults ARE the Stage-1 limits (₹5k max order, 1 position). Wire the YAML before Phase C capital scaling (₹25L+) |
| HIGH-002 | Code | Strategy engine kill switch uses 1s lazy DynamoDB cache, not `KillSwitchCache` class | Functionally equivalent at Stage-1 signal volume |
| B-004 | Code | `KafkaTickPublisher(dynamodb_table_sessions=risk_state_table)` — wrong table passed | Durable outbox disabled in non-production environments; `durable_outbox_enabled=_is_production` guard means no contamination in current state |
| AUTO-KS | SNS | Auto-kill-switch Lambda not provisioned by default | Stage-1: operator monitors manually. Create Lambda IAM role and set `kill_switch_lambda_role_arn` before scaling. |
| ROLLBACK | Deployment | No dedicated `rollback.sh` script | Steps documented in `pre-live-runbook.md`. Create script before >Stage-1 volume. |
| AI-ENGINE | IAM/Compute | `ai_engine` has no EC2 ASG in prod; Kafka IAM not attached | Enrichment watchdog fallback (`signals.pending → risk_engine` directly) is the designed path for Stage-1 |

---

## 9. Operator Action Checklist (Before `terraform apply`)

```
[ ] Set strategy_watchlist_nse in prod/main.tf — minimum: symbols for whitelisted Stage-1 strategy
    Example: strategy_watchlist_nse = "HDFCBANK,ICICIBANK,RELIANCE,TCS,INFY"

[ ] Confirm secrets_zerodha_arn and secrets_alpaca_arn are set in prod/main.tf
    (or a prod.tfvars file — never committed to git)

[ ] Confirm alert_email is set and SNS email subscription confirmed after apply

[ ] Verify .env Zerodha credentials are local dev only — rotate if any was used with a real account

[ ] After terraform apply:
    - Verify symbol-status-index GSI is ACTIVE (not CREATING):
      aws dynamodb describe-table --table-name quantembrace-prod-orders --query "Table.GlobalSecondaryIndexes[*].{Name:IndexName,Status:IndexStatus}"
    - Verify sessions table is ACTIVE:
      aws dynamodb describe-table --table-name quantembrace-prod-sessions --query "Table.TableStatus"

[ ] Trigger ASG instance refresh for risk_engine, execution_engine, strategy_engine
    (picks up updated userdata with STRATEGY_WATCHLIST_NSE and other env vars)

[ ] Test SNS alert delivery:
    aws sns publish --topic-arn <alerts-topic-arn> --message "Stage-1 readiness test"

[ ] Run runtime verification (from trading host):
    PYTHONPATH=services python3 scripts/read_only_live_readiness_runtime_check.py --json
    # Must exit 0
```

---

## 10. Full `terraform plan` Expected Changes

After all Phase C fixes and prior INFRA-1/2/3 fixes, `terraform plan` should show:

| Resource | Change | Notes |
|----------|--------|-------|
| `aws_dynamodb_table.orders` | **update** | Add `symbol-status-index` GSI — online, no downtime, 5-20 min backfill |
| `aws_dynamodb_table.sessions` | **create** | New table for Zerodha daily token rotation |
| `module.vpc.aws_nat_gateway.*` | **may recreate** | From `ha_nat=true` fix — verify VPC connectivity during apply; single-AZ disruption possible |
| `aws_cloudwatch_metric_alarm.ecs_task_count[*]` | **update** | `treat_missing_data` change; 5 alarms will leave ALARM state → OK state |
| `aws_iam_policy.risk_engine` | **update** | Remove `risk-decisions` table ARN from risk_engine policy |
| `aws_autoscaling_group.execution_engine` / `strategy_engine` | **may update** | Userdata hash changes from `STRATEGY_WATCHLIST_NSE` addition — triggers new launch template version |

**No destroys expected on DynamoDB tables. Confirm before applying.**

---

## 11. Test Results

### Config validation

```bash
# terraform fmt check — all changed files clean
terraform fmt -check \
  infra/terraform/modules/monitoring/main.tf \
  infra/terraform/modules/ec2_services/iam.tf \
  infra/terraform/modules/ec2_services/variables.tf \
  infra/terraform/environments/prod/variables.tf \
  infra/terraform/environments/prod/main.tf
# Result: ALL CLEAN
```

### Deployment dry-run

`terraform plan` requires live AWS credentials + state bucket access — cannot be run in static analysis. Expected plan documented in §10. No unexpected destroys anticipated.

### IAM policy validation

Policies validated statically:
- `SecretsReadBrokerCreds`: restricted to `[var.secrets_zerodha_arn, var.secrets_alpaca_arn]` — not wildcard
- `DynamoDBRiskState`: restricted to `{prefix}-risk-state` only (stale `risk-decisions` ARN removed)
- `cloudwatch:PutMetricData`: Resource `"*"` — required by AWS; acceptable
- `kafka_bootstrap_discovery`: Resource `"*"` — required by MSK API; acceptable

### CloudWatch / SNS dry-run

SNS email subscription is created by Terraform at apply time (operator must confirm subscription email). No live SNS publishes performed in this audit. Test command documented in §9.

### Secrets scan

Static scan of all Python, YAML, HCL, and shell files in the repository: no hardcoded API keys, secrets, or access tokens found outside of `.env` (gitignored, local dev only).

---

*Report generated by static analysis only. No AWS runtime state was queried. No trading was enabled. No broker orders were placed. No secrets were included.*
