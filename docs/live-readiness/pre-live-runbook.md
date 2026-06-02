# Pre-Live Runbook — QuantEmbrace Stage-1 Live Validation

**Generated:** 2026-05-30
**Target:** Stage-1 live validation — one share / minimum quantity, one whitelisted strategy (`tiny-live` profile), manual approval at every gate.
**Capital:** ₹1,000,000 BLOCKED until all gates in §4 pass.
**Authority:** Every promotion gate requires explicit operator sign-off. No automatic promotion.

---

## 1. All Open Blockers (consolidated across Phases A, B, C)

### 1.1 Fixed in this audit session (no further action needed)

| ID | What was fixed | Files changed |
|---|---|---|
| C-001 | `symbol-status-index` GSI added to orders DynamoDB table | `infra/terraform/modules/dynamodb/main.tf` |
| C-002 | `RISK_MAX_SIGNAL_AGE_SECONDS=30`, `RISK_PROFILE=paper`, `UNIVERSE_MODE=PAPER_SAFE_START` added to EC2 env files | `userdata/risk_engine.sh`, `execution_engine.sh`, `strategy_engine.sh` |
| C-003 | Per-symbol asyncio lock in `DailyLossValidator.record_fill()` — cost-basis race closed | `services/risk_engine/validators/loss_validator.py` |
| C-005/6/9 | Misleading docstrings in `MarginValidator`, `SlippageValidator`, `SectorConcentrationValidator` | 3 validator files |

### 1.2 Must fix before `terraform apply`

| ID | Severity | Description | Fix |
|---|---|---|---|
| **INFRA-1** | **CRITICAL** | `prod/main.tf:58` passes `single_nat_gateway = false` to VPC module, which declares `ha_nat`. Terraform plan fails. | Change to `ha_nat = true` in `infra/terraform/environments/prod/main.tf:58` |
| **INFRA-2** | **CRITICAL** | `sessions` DynamoDB table absent — `ZerodhaTokenManager` raises `ResourceNotFoundException` on every token read; data_ingestion and execution_engine fall back to stale env var token, which expires at 07:30 IST daily | Add `aws_dynamodb_table.sessions` to `modules/dynamodb/main.tf` (see §5.1) |
| **INFRA-3** | **HIGH** | `deploy.yml` calls `python scripts/deploy/check_asg_health.py --asg ...` but the file on disk is `check_ecs_health.py` (ECS API, wrong interface) — every CI/CD deploy fails at the health-check step | Rename `check_ecs_health.py` → `check_asg_health.py` and rewrite to use `aws autoscaling describe-auto-scaling-groups` (see §5.2) |

### 1.3 Must fix before first live session (code)

| ID | Severity | Description | Fix |
|---|---|---|---|
| **B-001** | HIGH | Candle signal Kafka publish failure silently dropped in `strategy_engine/_candle_processing_loop()` — no retry, no DLQ. Tick path correctly raises. | Add return-value check after `await self._publish_signal()` in `strategy_engine/service.py:634`; route failure to retry topic same as tick path |
| **B-002** | HIGH | `asyncio.gather(return_exceptions=True)` in strategy_engine — a permanently crashed loop (e.g. Kafka auth revoked) keeps the service alive and healthy-looking while processing nothing | Change to `return_exceptions=False` or add a post-gather exception check |
| **HIGH-001** | MEDIUM | `_signal_locks` dict in `execution_engine/service.py:778` grows unboundedly (`setdefault(signal_id, Lock())`, no cleanup). Not dangerous at Stage-1 volume (1 position, low signal count) but leaks memory in sustained sessions | Add cleanup: `self._signal_locks.pop(signal_id, None)` after the signal lock block exits |
| **HIGH-004** | MEDIUM | MIS square-off task (`execution-mis-square-off`) has no watchdog. A crash at 15:05 IST causes full service restart during the most sensitive trading window, potentially leaving open MIS positions | Wrap `mis_manager.run()` in a `try/except` that logs CRITICAL and fires SNS alert before propagating |

### 1.4 Known, tracked, acceptable for Stage-1

| ID | Severity | Why acceptable at Stage-1 |
|---|---|---|
| **ADR-021-P1** | HIGH | `RISK_DATA_FEED_STALE_SECONDS=3600` workaround in `.env` — must be reverted before medium/high volume live. At Stage-1 (1 position, ~5 signals/day) the false-kill-switch risk from 3s staleness is negligible. |
| **ADR-021-P2** | MEDIUM | `risk_limits_production.yaml` not wired — limits come from `RiskLimits.for_profile("tiny-live")` hardcoded defaults (₹5k max order, 1 concurrent position). Those defaults ARE the correct Stage-1 limits. |
| **HIGH-002** | LOW | Strategy engine kill switch uses 1s TTL lazy DynamoDB cache (not `KillSwitchCache` class). Functionally equivalent for this service. |
| **B-003** | LOW | `get_dynamodb_resource()` recreated on each 1s kill-switch cache miss in strategy_engine. Wasteful but not blocking. |
| **B-004** | MEDIUM | `KafkaTickPublisher(dynamodb_table_sessions=risk_state_table)` — wrong table passed. Durable outbox is disabled in non-production (`durable_outbox_enabled=_is_production`), so no contamination in current state. Must verify before prod deploy. |
| **PHASE8-002** | LOW | `endpoint_budgets.py` absent. Rate budget enforcement is degraded; Zerodha 10 req/s limit must be respected manually at Stage-1 volume. |
| **C-004** | LOW | ExposureValidator runs a full DynamoDB scan per signal. Fine at Stage-1 (≤1 position). |
| **MEDIUM-6** | LOW | ECS-namespace CloudWatch alarms breach permanently (wrong namespace — EC2 not ECS). Alert noise; not connected to kill switch. |
| **ALPACA-FIX** | LOW | Alpaca `.value` enum AttributeError in `alpaca_broker.py`. NSE-only Stage-1 is unaffected. |

---

## 2. Infrastructure Fix Sequence

Run in this exact order. Do not skip steps.

### Step 1 — Fix VPC variable mismatch (INFRA-1)

```hcl
# infra/terraform/environments/prod/main.tf:58
# BEFORE:
single_nat_gateway = false  # ← fails — variable does not exist in vpc module

# AFTER:
ha_nat = true               # ← correct variable name; HA NAT gateway per AZ
```

### Step 2 — Add sessions DynamoDB table (INFRA-2)

Add to `infra/terraform/modules/dynamodb/main.tf` after the risk_state table (see §5.1 for full resource block). Also add to `dynamodb/outputs.tf`:
```hcl
output "sessions_table_name" {
  value = aws_dynamodb_table.sessions.name
}
```

### Step 3 — Fix CI/CD health check script (INFRA-3)

Rename and rewrite `scripts/deploy/check_ecs_health.py` → `scripts/deploy/check_asg_health.py` to use ASG describe API (see §5.2 for full script). Update `deploy.yml` to call it correctly (already references `check_asg_health.py` — the rename alone fixes the `deploy.yml` reference).

### Step 4 — Run `terraform plan` on prod

```bash
cd infra/terraform/environments/prod
terraform plan -out=prod.tfplan 2>&1 | tee /tmp/tf-plan-$(date +%Y%m%d).log
```

Expected changes:
- `aws_dynamodb_table.orders`: update (add `symbol-status-index` GSI) — online, no downtime
- `aws_dynamodb_table.sessions`: create — new table
- `module.vpc.aws_nat_gateway.*`: may recreate if `ha_nat` changes NAT configuration — **verify VPC connectivity during apply**

Confirm `go_live_checklist.terraform_plan_clean = true` once plan shows no unexpected destroys.

### Step 5 — Apply Terraform

```bash
terraform apply prod.tfplan
```

GSI creation backfills an existing table — this runs in the background and can take 5-20 minutes. All other changes apply immediately. DynamoDB service is not interrupted during GSI backfill.

### Step 6 — Deploy new EC2 images

Push a code change to `main` to trigger the build + deploy pipeline, or manually promote the current image SHA:

```bash
# Manual promote if code hasn't changed but userdata scripts have:
# (userdata is baked at Terraform apply time, not at image build time)
# Trigger ASG instance refresh to replace instances with updated userdata:
aws autoscaling start-instance-refresh \
  --auto-scaling-group-name quantembrace-prod-risk-engine-asg \
  --preferences '{"MinHealthyPercentage": 0, "InstanceWarmup": 180}'

# Repeat for execution-engine and strategy-engine ASGs
```

Wait for each refresh to complete before starting the next.

---

## 3. Paper Session Verification Sequence

Run this for every paper session before any live session.

### Pre-session (each morning)

```bash
# 1. Refresh Zerodha token (expires ~07:30 IST daily)
python scripts/zerodha_login.py

# 2. Verify kill switch is inactive
python scripts/kill_switch_cli.py status
# Expected output: ✅ KILL SWITCH — TRADING IS ACTIVE

# 3. Run full preflight check
python scripts/deploy/preflight_check.py --env production
# Must exit 0 — stop if exit 1

# 4. Run read-only runtime verification
PYTHONPATH=services python3 scripts/read_only_live_readiness_runtime_check.py --json
# Must exit 0 (PAPER_SAFE) — stop if exit 1 or 2

# 5. Verify all strategy paper_trade flags
python scripts/ops/reconcile.py --environment prod --status
# Confirm reconciliation_required = CLEAR
```

### Session start (Docker / EC2)

```bash
# Local paper session:
docker-compose down -v
docker-compose up -d localstack redpanda
docker-compose run --rm setup
python scripts/deploy/paper_preflight_check.py   # must exit 0

python scripts/zerodha_login.py
docker-compose up -d
```

### During session

```bash
python scripts/monitoring/paper_trading_monitor.py \
  --counters /tmp/qe_live_counters.json
```

### Session close

```bash
python scripts/monitoring/paper_session_report.py --date $(date +%Y-%m-%d)
```

### Required paper session count before Stage-1 live: **5 sessions**

All five must show:
- At least one signal generated
- At least one fill recorded
- No unmanaged MIS positions at session end
- No kill switch self-fires
- No reconciliation halts

---

## 4. Stage-1 Live Promotion Gate

**All items must be checked by the operator before enabling live trading. No automated promotion.**

### Gate 4.1 — Infrastructure ready

- [ ] `terraform plan` shows no unexpected destroys (`go_live_checklist.terraform_plan_clean = true`)
- [ ] `symbol-status-index` GSI on orders table is ACTIVE (not CREATING) — check AWS console or `aws dynamodb describe-table --table-name quantembrace-prod-orders`
- [ ] `sessions` table exists and is ACTIVE
- [ ] All prod ASGs show ≥1 InService instance with updated userdata (post-refresh)
- [ ] CI/CD pipeline: last `deploy.yml` run completed without errors (health check step passes)

### Gate 4.2 — Token and credentials

- [ ] Zerodha token refreshed today (`python scripts/zerodha_login.py` run successfully this morning)
- [ ] Token written to DynamoDB sessions table (`python scripts/ops/reconcile.py --status` shows no DynamoDB errors)
- [ ] `ZERODHA_ACCESS_TOKEN` in sessions table `expires_at` is today and in the future
- [ ] Kill switch INACTIVE (`python scripts/kill_switch_cli.py status`)

### Gate 4.3 — Risk configuration

- [ ] `RISK_PROFILE=tiny-live` set in `risk_engine.sh` userdata **and** instances refreshed after that change
- [ ] `UNIVERSE_MODE=PAPER_SAFE_START` confirmed in execution_engine env
- [ ] `QE_EXECUTION_LIVE_TRADING_ENABLED` is absent or false on all running instances (runtime check)
- [ ] `risk_limits_production.yaml` reviewed — `max_single_order_value=5000`, `max_concurrent_positions=1` match `tiny-live` profile

### Gate 4.4 — Strategy configuration

- [ ] Exactly ONE strategy has `paper_trade = false` in DynamoDB `strategy-config` (the whitelisted strategy)
- [ ] All other strategies have `paper_trade = true`
- [ ] Whitelisted strategy is confirmed to only trade whitelisted symbols (instruments.yaml review)
- [ ] `go_live_checklist.instruments_yaml_verified = true` set in `risk_limits_production.yaml`

### Gate 4.5 — Paper session evidence

- [ ] `go_live_checklist.five_day_paper_counter_reset = true`
- [ ] 5 completed paper sessions with `paper_session_report.py` outputs reviewed
- [ ] No session had: kill switch self-fire · reconciliation halt · unmanaged positions at close

### Gate 4.6 — Runtime state (run on trading host, day of)

```bash
PYTHONPATH=services python3 scripts/read_only_live_readiness_runtime_check.py --json
# Exit code must be 0 (PAPER_SAFE) — the script cannot yet enforce live-specific checks;
# treat exit 2 (RUNTIME_VERIFICATION_REQUIRED) as FAIL
```

- [ ] Script exits 0
- [ ] All DynamoDB tables reachable
- [ ] Zerodha token fresh
- [ ] Kill switch inactive
- [ ] Reconciliation clear

### Gate 4.7 — Live gate activation (operator sign-off)

After all gates 4.1-4.6 pass:

```bash
# 1. Change risk_engine.sh userdata (Terraform):
#    RISK_PROFILE=paper  →  RISK_PROFILE=tiny-live
#    Then re-apply Terraform and refresh the risk_engine ASG

# 2. Set live trading enabled on execution_engine (Terraform):
#    Uncomment: QE_EXECUTION_LIVE_TRADING_ENABLED=true
#    Then re-apply Terraform and refresh the execution_engine ASG

# 3. Confirm via runtime check that the flag is active:
PYTHONPATH=services python3 scripts/read_only_live_readiness_runtime_check.py --json
```

**After activation, monitor for the first 30 minutes with:**

```bash
python scripts/monitoring/paper_trading_monitor.py \
  --counters /tmp/qe_live_counters.json
```

**Immediate kill switch trigger conditions:**

```bash
python scripts/kill_switch_cli.py activate --reason "<reason>"
```

Trigger immediately if:
- Any order placed for a symbol not in the whitelisted set
- Any order with quantity > 1 share (Stage-1 constraint)
- Any order with value > ₹5,000
- Daily P&L loss > 0.5% (₹5,000 on ₹1M)
- Any reconciliation mismatch between DynamoDB and broker positions
- Any unhandled exception in execution_engine logs

---

## 5. Appendix: Full Resource Blocks for Remaining Fixes

### 5.1 — `sessions` DynamoDB table (add to `dynamodb/main.tf`)

```hcl
# ---------------------------------------------------------------------------
# Sessions Table — Zerodha daily access token store
#   PK: "SESSION#{date}"  (S)   — one row per trading date
#   SK: "ZERODHA"         (S)   — broker discriminator
#
# ZerodhaTokenManager reads/writes this table after daily login
# (scripts/zerodha_login.py). TTL expires old rows after 48 hours so the
# table never accumulates more than 2 rows under normal operation.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "sessions" {
  name         = "${local.table_prefix}-sessions"
  billing_mode = local.billing_mode
  hash_key     = "PK"
  range_key    = "SK"

  read_capacity  = var.use_provisioned_capacity ? local.default_read_capacity : null
  write_capacity = var.use_provisioned_capacity ? local.default_write_capacity : null

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at_epoch"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name  = "${local.table_prefix}-sessions"
    Table = "sessions"
  })
}
```

Add to `dynamodb/outputs.tf`:
```hcl
output "sessions_table_name" {
  description = "DynamoDB table name for broker session tokens"
  value       = aws_dynamodb_table.sessions.name
}
```

### 5.2 — `check_asg_health.py` replacement (rename from `check_ecs_health.py`)

```python
#!/usr/bin/env python3
"""
ASG health checker used in CI/CD post-deploy verification.

Polls Auto Scaling Group descriptions and verifies that each named ASG has
at least --min-healthy InService instances. Exits non-zero on failure.

Usage:
    python scripts/deploy/check_asg_health.py \
        --asg quantembrace-prod-risk-engine-asg \
        --min-healthy 1 \
        --timeout 300

    # Or with --allow-zero-desired (passes if desired=0, used for scaled-down ASGs):
    python scripts/deploy/check_asg_health.py \
        --asg quantembrace-prod-strategy-engine-asg \
        --allow-zero-desired
"""

from __future__ import annotations

import argparse
import sys
import time

try:
    import boto3
except ImportError:
    print("boto3 is required: pip install boto3")
    sys.exit(1)


def _check(
    asg_name: str,
    min_healthy: int,
    timeout: int,
    allow_zero_desired: bool,
    poll_interval: int = 15,
) -> bool:
    client = boto3.client("autoscaling")
    deadline = time.time() + timeout

    print(f"Checking ASG: {asg_name}")
    print(f"  Min healthy : {min_healthy}")
    print(f"  Zero desired: allowed={allow_zero_desired}")
    print(f"  Timeout     : {timeout}s\n")

    while time.time() < deadline:
        resp = client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        groups = resp.get("AutoScalingGroups", [])

        if not groups:
            print(f"  ERROR: ASG {asg_name!r} not found")
            return False

        asg = groups[0]
        desired = asg.get("DesiredCapacity", 0)
        in_service = sum(
            1 for i in asg.get("Instances", [])
            if i.get("LifecycleState") == "InService"
            and i.get("HealthStatus") == "Healthy"
        )

        print(f"  desired={desired}  in_service={in_service}")

        if allow_zero_desired and desired == 0:
            print(f"  ASG has desired=0 — skipping health check (allow_zero_desired=True).")
            return True

        if in_service >= min_healthy:
            print(f"\n  ASG {asg_name} healthy ({in_service} InService).")
            return True

        remaining = int(deadline - time.time())
        if remaining <= 0:
            break
        print(f"  Not yet healthy. Retrying in {poll_interval}s ({remaining}s remaining)...\n")
        time.sleep(poll_interval)

    print(f"\nTIMEOUT: {asg_name} did not reach {min_healthy} healthy instance(s).")
    return False


def _main() -> None:
    p = argparse.ArgumentParser(description="ASG post-deploy health check")
    p.add_argument("--asg", required=True, help="Auto Scaling Group name")
    p.add_argument("--min-healthy", type=int, default=1,
                   help="Minimum InService+Healthy instances required")
    p.add_argument("--timeout", type=int, default=300,
                   help="Max seconds to wait (default: 300)")
    p.add_argument("--poll-interval", type=int, default=15,
                   help="Seconds between polls (default: 15)")
    p.add_argument("--allow-zero-desired", action="store_true",
                   help="Pass immediately if ASG has desired=0 (scaled-down ASG)")
    args = p.parse_args()

    ok = _check(
        asg_name=args.asg,
        min_healthy=args.min_healthy,
        timeout=args.timeout,
        allow_zero_desired=args.allow_zero_desired,
        poll_interval=args.poll_interval,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main()
```

---

## 6. Quick-Reference: Environment Variables by Service

| Variable | risk_engine | execution_engine | strategy_engine | Required value |
|---|:---:|:---:|:---:|---|
| `KAFKA_BOOTSTRAP_SERVERS` | ✅ | ✅ | ✅ | MSK bootstrap string (auto-discovered) |
| `DYNAMODB_TABLE_PREFIX` | ✅ | ✅ | ✅ | `quantembrace-prod` |
| `RISK_MAX_SIGNAL_AGE_SECONDS` | ✅ | — | ✅ | `30` (hardcoded in userdata) |
| `RISK_PROFILE` | ✅ | — | — | `paper` (paper sessions) / `tiny-live` (Stage-1 live) |
| `UNIVERSE_MODE` | — | ✅ | — | `PAPER_SAFE_START` → `LIVE_ADVANCED` when ready |
| `QE_EXECUTION_LIVE_TRADING_ENABLED` | — | commented out | — | absent/false (paper) / `true` (live — operator sign-off only) |
| `ZERODHA_API_KEY` | — | ✅ | — | From Secrets Manager |
| `ZERODHA_ACCESS_TOKEN` | — | ✅ | — | From Secrets Manager at startup; runtime refresh via DynamoDB sessions table |

---

## 7. Rollback Procedure

### Immediate halt (any time)

```bash
python scripts/kill_switch_cli.py activate \
  --reason "Stage-1 live rollback: <reason>"
```

This halts all new signal approvals within 1 second (Kafka + DynamoDB dual path). Existing open positions are not automatically closed — they must be managed via the MIS square-off (auto at 15:05 IST) or manual broker intervention.

### Service rollback

```bash
# 1. Identify previous good image SHA from ECR or git log
PREV_SHA="<previous-sha>"

# 2. Retag the previous image as latest-prod for each affected service
for SVC in risk_engine execution_engine strategy_engine; do
  scripts/deploy/promote_ecr_image.sh \
    --repository "quantembrace-${SVC}" \
    --source-tag "${PREV_SHA}" \
    --target-tag "latest-prod"
done

# 3. Trigger ASG instance refresh for each service (sequentially)
for ASG in \
  quantembrace-prod-risk-engine-asg \
  quantembrace-prod-execution-engine-asg \
  quantembrace-prod-strategy-engine-asg; do
  scripts/deploy/refresh_asg.sh --asg "$ASG" --min-healthy 0 --instance-warmup 180
done
```

### After rollback

```bash
# Verify position state matches broker
python scripts/ops/reconcile.py --environment prod

# If drift detected:
python scripts/ops/reconcile.py --environment prod --set-required \
  --reason "post_rollback_drift_check"
# (blocks new signal intake until cleared)

# After manually verifying positions:
python scripts/ops/reconcile.py --environment prod --clear
```

---

*This runbook should be reviewed and updated after each Stage-1 session. Record session outcomes in `docs/live-readiness/` alongside this file.*
