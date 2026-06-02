# EC2 Runtime Verification Before Stage-1

**Version:** 1.0  
**Date:** 2026-05-31  
**Owner:** Operator (human sign-off required at every gate)  
**Scope:** Read-only runtime state verification on the actual AWS EC2 trading host

---

## ⚠️ CRITICAL CONSTRAINTS — READ BEFORE PROCEEDING

```
DO NOT enable live trading.
DO NOT change capital limits.
DO NOT deploy any service.
DO NOT place broker orders.
DO NOT mutate DynamoDB.
DO NOT print, log, or paste token values anywhere.
DO NOT run this on the development sandbox (it will return RUNTIME_VERIFICATION_REQUIRED).
```

This runbook performs **read-only verification only**. It proves that the runtime state on the actual EC2 trading host is safe before Phase 8 planning can begin.

---

## 1. Purpose

The development sandbox cannot access production DynamoDB — it has no IAM role and points to a non-running LocalStack. Phase 7.2 correctly returned `RUNTIME_VERIFICATION_REQUIRED` because 7 critical runtime checks were UNKNOWN.

This runbook closes that gap by running `scripts/read_only_live_readiness_runtime_check.py` directly on the EC2 trading host, where:

- The EC2 instance profile provides DynamoDB access via IAM.
- The Zerodha token row exists in the `sessions` DynamoDB table.
- Kill switch, ENTRY_BLOCK, reconciliation flags, and strategy configs are readable from the production `risk-state` and `strategy-config` tables.
- Environment variables (`RISK_PROFILE`, `UNIVERSE_MODE`, etc.) are injected via EC2 userdata.

**A PASS result here means the runtime is in a paper-safe state.** It does not authorise live trading, capital deployment, or Stage-1 execution. Those require additional gates described in §8.

---

## 2. Preconditions

Verify every item before proceeding. Do not skip.

| Precondition | Check |
|---|---|
| SSH access to the EC2 trading host | `ssh -i <key.pem> <user>@<ec2-ip>` succeeds |
| Correct IAM role attached to instance | `aws sts get-caller-identity` returns expected role ARN |
| Python 3.11+ installed | `python3 --version` shows 3.11.x |
| boto3 installed | `python3 -c "import boto3; print('OK')"` succeeds |
| Project repo present at expected path | `/opt/quantembrace` or equivalent |
| Correct branch/tag checked out | `git log --oneline -1` shows expected Phase 7.1 commit |
| All services are running (or at least process env is set) | `systemctl status quantembrace-*` or docker-compose ps |
| Zerodha login script available | `ls scripts/zerodha_login.py` |
| No `QE_EXECUTION_LIVE_TRADING_ENABLED=true` in environment | `echo $QE_EXECUTION_LIVE_TRADING_ENABLED` returns empty or false |
| Current time is a trading day (Zerodha token is daily) | Confirm date is not a market holiday |

---

## 3. Commands — Step by Step

### Step 1 — SSH to the EC2 trading host

```bash
ssh -i ~/.ssh/quantembrace-prod.pem ec2-user@<trading-host-ip-or-hostname>
# OR via SSM Session Manager (no key required if configured):
aws ssm start-session --target <instance-id> --region ap-south-1
```

Confirm you are on the correct host:

```bash
hostname
curl -s http://169.254.169.254/latest/meta-data/instance-id
# Expected: i-0xxxxxxxxxxxx (EC2 instance ID)
```

### Step 2 — Go to project root

```bash
cd /opt/quantembrace
# OR wherever the repo was deployed:
# cd ~/quantembrace
# cd /srv/quantembrace
```

Confirm the correct branch:

```bash
git log --oneline -1
git branch --show-current
# Expected: phase7.1 commit or equivalent release tag
```

### Step 3 — Activate Python environment (if applicable)

```bash
# If using a virtual environment:
source .venv/bin/activate

# If using system Python (common on EC2 AMI):
# No activation needed — skip this step

python3 --version
# Expected: Python 3.11.x
```

### Step 4 — Verify Python and boto3

```bash
python3 --version
python3 -c "import boto3; print('boto3 OK')"
python3 -c "import boto3; c = boto3.client('dynamodb', region_name='ap-south-1'); print('DynamoDB client OK')"
```

If boto3 is missing:
```bash
pip install boto3 --break-system-packages
# Or with venv: pip install boto3
```

### Step 5 — Confirm AWS IAM role

```bash
aws sts get-caller-identity --region ap-south-1
# Expected output (no secrets — only role ARN):
# {
#   "UserId": "AROA...",
#   "Account": "123456789012",
#   "Arn": "arn:aws:sts::123456789012:assumed-role/quantembrace-prod-<service>-role/i-0..."
# }
```

If this fails: the instance role is not attached or AWS credentials are not available. Stop and fix before proceeding.

### Step 6 — Refresh Zerodha token

The Zerodha access token expires daily at ~07:30 IST. Run the daily login to refresh it before the check:

```bash
python3 scripts/zerodha_login.py
# This will prompt for the Kite login URL or OTP.
# The token is stored in DynamoDB (sessions table), NOT printed to stdout.
# Never paste the token value anywhere.
```

If the token was refreshed earlier today and is still valid, this step can be skipped — but the runtime check will verify freshness.

### Step 7 — Verify critical environment variables

```bash
echo "RISK_PROFILE=$RISK_PROFILE"
echo "UNIVERSE_MODE=$UNIVERSE_MODE"
echo "QE_EXECUTION_LIVE_TRADING_ENABLED=${QE_EXECUTION_LIVE_TRADING_ENABLED:-<unset>}"
echo "EXECUTION_PAPER_TRADING=${EXECUTION_PAPER_TRADING:-<unset>}"
echo "STRATEGY_WATCHLIST_NSE=${STRATEGY_WATCHLIST_NSE:0:40}..."   # first 40 chars only
echo "DYNAMODB_TABLE_PREFIX=$DYNAMODB_TABLE_PREFIX"
```

**Required values:**

| Variable | Required Value | Action if wrong |
|---|---|---|
| `RISK_PROFILE` | `paper` | Set in EC2 userdata (`risk_engine.sh`); do not change without approval |
| `UNIVERSE_MODE` | `PAPER_SAFE_START` | Set in `execution_engine.sh`; must be set before services start |
| `QE_EXECUTION_LIVE_TRADING_ENABLED` | absent or `false` | **HARD BLOCK** — must not be `true` |
| `EXECUTION_PAPER_TRADING` | `true` or unset (default True) | Safe if unset |
| `STRATEGY_WATCHLIST_NSE` | comma-separated NSE symbols | Set before strategy_engine start; empty = no candle signals |
| `DYNAMODB_TABLE_PREFIX` | `quantembrace-prod` (or staging) | Confirm matches Terraform provisioned tables |

If `QE_EXECUTION_LIVE_TRADING_ENABLED=true` is found, **stop immediately**. This must not be set during verification.

### Step 8 — Run the read-only runtime verification

```bash
export PYTHONPATH=services
python3 scripts/read_only_live_readiness_runtime_check.py --json \
  | tee runtime_check_$(date +%Y%m%d_%H%M%S).json
echo "Exit code: $?"
```

**This command:**
- Is read-only by construction (`_ReadOnlyDynamo` proxy blocks all write calls)
- Never reads, stores, or prints the Zerodha token value
- Writes the JSON output to a timestamped file for evidence
- Exits 0 (PASS), 1 (FAIL), or 2 (RUNTIME_VERIFICATION_REQUIRED)

Do not redirect stderr — let it appear in the terminal so errors are visible.

---

## 4. Critical Checks That Must PASS

All of the following checks must be PASS (not UNKNOWN, not FAIL) before Phase 8 planning can begin:

| Check | Expected Status | What It Verifies |
|---|---|---|
| `connectivity` | PASS | DynamoDB reachable via IAM role |
| `table_names` | PASS | Tables resolved from AppSettings (not env-prefix fallback) |
| `zerodha_token` | PASS | Token row exists, token present, `expires_at` in the future |
| `strategy_config` | PASS | All enabled strategies have `paper_trade=True` |
| `kill_switch` | PASS | `KILLSWITCH/GLOBAL active=False` |
| `reconciliation` | PASS | `RECONCILIATION#STATE/GLOBAL required=False` or row absent |
| `env:live_trading_enabled` | PASS | `QE_EXECUTION_LIVE_TRADING_ENABLED` absent or false |
| `env:risk_profile` | PASS | `RISK_PROFILE=paper` |
| `env:universe_mode` | PASS | `UNIVERSE_MODE=PAPER_SAFE_START` or `PAPER_EXPAND` |

Acceptable WARN (does not block Phase 8 planning):
- `zerodha_token` WARN if token row exists but `expires_at` is stale → re-run `zerodha_login.py` and retry
- `env:watchlist` WARN if `STRATEGY_WATCHLIST_NSE` is unset → candle strategies won't emit, but not a safety failure
- `env:execution_paper_trading` UNKNOWN if unset → code default is True; acceptable

---

## 5. Required Environment Variables — EC2 Verification

Before running the check, confirm each variable is set as expected on the EC2 instance:

```bash
# Full env check (no secrets printed — these vars have no sensitive values)
printenv | grep -E "^RISK_PROFILE|^UNIVERSE_MODE|^QE_EXECUTION|^EXECUTION_PAPER|^STRATEGY_WATCHLIST|^DYNAMODB|^AWS_REGION|^AWS_DEFAULT_REGION" | sort
```

If `QE_EXECUTION_LIVE_TRADING_ENABLED` appears with value `true` → **STOP. Do not proceed.**

Variables that should NOT be set on the trading host (they would force live routing):
- `QE_EXECUTION_LIVE_TRADING_ENABLED=true`
- `EXECUTION_PAPER_TRADING=false`

These are commented out in `execution_engine.sh` EC2 userdata and must remain commented until Stage-1 operator sign-off.

---

## 6. PASS / FAIL / RUNTIME_VERIFICATION_REQUIRED Interpretation

### PASS — verdict: `RUNTIME_STATE_PAPER_SAFE` (exit code 0)

```json
{
  "verdict": "RUNTIME_STATE_PAPER_SAFE",
  "summary": { "fail": 0, "warn": 0, "unknown": 0 }
}
```

All checks resolved. The runtime is paper-safe. See §8 for what PASS does and does not authorise.

### FAIL — verdict: `RUNTIME_STATE_UNSAFE` (exit code 1)

One or more checks returned FAIL. Common causes and fixes:

| FAIL condition | Cause | Fix |
|---|---|---|
| `strategy_config` FAIL: `paper_trade=False` | A strategy was accidentally promoted to live | Update strategy config in DynamoDB: `scripts/strategy/config.py set --strategy <name> --paper-trade true` |
| `env:live_trading_enabled` FAIL | `QE_EXECUTION_LIVE_TRADING_ENABLED=true` in env | Remove or set to false; restart services |
| `kill_switch` WARN (active) | Kill switch was tripped during a paper session | Investigate reason; clear with `python3 scripts/kill_switch_cli.py deactivate --reason "verified safe after session review"` — operator decision only |
| `reconciliation` WARN | `reconciliation_required=True` flag set | Run `python3 scripts/ops/reconcile.py --environment prod` and resolve mismatches; then `--clear` |

Do not proceed to Phase 8 while any FAIL remains.

### RUNTIME_VERIFICATION_REQUIRED — verdict: `RUNTIME_VERIFICATION_REQUIRED` (exit code 2)

The script could not reach DynamoDB or boto3 is missing. Causes:

| Cause | Fix |
|---|---|
| Running on sandbox (LocalStack endpoint) | SSH to the actual EC2 host |
| boto3 not installed | `pip install boto3` |
| IAM role not attached | Attach the instance profile; or set `AWS_PROFILE` if using a named profile |
| Wrong `AWS_ENDPOINT_URL` | Unset it: `unset AWS_ENDPOINT_URL` (it should be unset on production EC2) |
| Network/security group blocks DynamoDB | Verify VPC endpoint or outbound 443 to DynamoDB |

---

## 7. Evidence to Paste Back

After the verification script runs, capture and share the following. **Never include token values.**

```bash
# 1. Full JSON output (already saved to file by tee above)
cat runtime_check_*.json

# 2. Hostname
hostname

# 3. AWS account/region (no secrets)
aws sts get-caller-identity --region ap-south-1 --query '{Account:Account,Arn:Arn}' --output json

# 4. Timestamp
date -u "+%Y-%m-%dT%H:%M:%SZ"

# 5. Branch/commit
git log --oneline -1
```

Paste all of the above into the Phase 8 gate review. Do not paste:
- Token values (access_token field)
- AWS secret keys
- Kite API secrets
- Any credential material

The JSON output from the script is safe to share — it never reads or prints the token value, only its presence and expiry metadata.

---

## 8. Explicit Decision Rules

| Verdict | Authorises | Does NOT authorise |
|---|---|---|
| `RUNTIME_STATE_PAPER_SAFE` | Phase 8 **planning and config-pack only** | Live trading, capital deployment, Stage-1 execution |
| `RUNTIME_STATE_UNSAFE` | Nothing — fix FAIL conditions first | Everything |
| `RUNTIME_VERIFICATION_REQUIRED` | Nothing — run on correct host | Everything |

**PASS specifically does NOT:**
- Enable live trading (`QE_EXECUTION_LIVE_TRADING_ENABLED` must remain absent/false)
- Allow ₹1,000,000 capital deployment
- Allow `RISK_PROFILE` to be changed from `paper` to `tiny-live`
- Authorise any operator to place broker orders
- Allow `UNIVERSE_MODE=LIVE_ADVANCED`
- Allow any strategy to have `paper_trade=False`

**To proceed from PASS to Stage-1:** the full `docs/live-readiness/pre-live-runbook.md` checklist must be completed, including promotion gates, `evaluate_promotion_gate.py`, and explicit operator sign-off.

---

## 9. Final Warning

```
╔══════════════════════════════════════════════════════════════╗
║  THIS RUNBOOK IS VERIFICATION ONLY.                          ║
║                                                              ║
║  DO NOT run Stage-1.                                         ║
║  DO NOT enable live trading.                                 ║
║  DO NOT deploy any service.                                  ║
║  DO NOT place broker orders.                                 ║
║  DO NOT change capital limits.                               ║
║  DO NOT mutate DynamoDB (the script is read-only by design). ║
║  DO NOT print or share Zerodha token values.                 ║
║                                                              ║
║  A PASS verdict here is a green light for Phase 8 PLANNING   ║
║  only — not for any production action.                       ║
╚══════════════════════════════════════════════════════════════╝
```

After completing this runbook, paste the JSON output and wait for the Phase 8 gate review. Do not take any trading action until Phase 8 is formally approved.

---

## Appendix: Quick Reference

```bash
# Full verification sequence (run on EC2 trading host only)

ssh -i ~/.ssh/quantembrace-prod.pem ec2-user@<host>
cd /opt/quantembrace
source .venv/bin/activate 2>/dev/null || true
python3 -c "import boto3; print('boto3 OK')"
aws sts get-caller-identity --query Arn --output text
python3 scripts/zerodha_login.py           # refresh token if needed
export PYTHONPATH=services
python3 scripts/read_only_live_readiness_runtime_check.py --json \
  | tee runtime_check_$(date +%Y%m%d_%H%M%S).json
echo "Exit: $PIPESTATUS"
hostname; date -u; git log --oneline -1
```

Expected exit code on success: **0** (`RUNTIME_STATE_PAPER_SAFE`)
