# Stage-1 One-Share Live Validation — Operator Runbook

**Version:** 1.0  
**Created:** 2026-05-30  
**Status:** READY — not yet executed  
**Operator:** hari.mosoju@gmail.com

---

## Critical Safety Constraints (read before anything else)

```
LIVE TRADING IS NOT ENABLED UNTIL STEP 4.7.
₹1,000,000 CAPITAL IS BLOCKED. Stage-1 max capital: ₹50,000.
MAX ONE POSITION AT ANY TIME.
MAX TWO TRADES TOTAL FOR THE SESSION.
ONE WHITELISTED STRATEGY: nse_vwap_reversion
ONE/TWO WHITELISTED SYMBOLS: HDFCBANK (primary), ICICIBANK (backup)
MAX ORDER VALUE: ₹2,000 per order

If anything unexpected happens at any step:
  python scripts/kill_switch_cli.py activate --reason "<what happened>"
  Then investigate. Do not resume until root cause is understood.
```

---

## Stage-1 Parameters Reference

| Parameter | Value |
|-----------|-------|
| Portfolio value | ₹50,000 |
| Max order value | ₹2,000 |
| Max concurrent positions | 1 |
| Max trades per day | 2 |
| Max daily loss | ₹250 (0.5%) |
| Strategy | `nse_vwap_reversion` |
| Symbol (primary) | `HDFCBANK` (~₹1,600/share → 1 share = ~₹1,600 ≤ ₹2,000 cap) |
| Symbol (backup) | `ICICIBANK` (~₹1,200/share) |
| Risk profile | `tiny-live` |
| Universe mode | `PAPER_SAFE_START` |

---

## Table of Contents

- [T-1 Day — Preparation](#t-1-day--preparation)
- [Pre-Market — Day Of (07:00–09:14 IST)](#pre-market--day-of-070009-14-ist)
- [Live Gate Activation (09:14–09:15 IST)](#live-gate-activation-091409-15-ist)
- [Live Validation Session (09:15–15:00 IST)](#live-validation-session-091515-00-ist)
- [No-Go Criteria](#no-go-criteria)
- [Session Close (15:00–15:30 IST)](#session-close-150015-30-ist)
- [Post-Session (15:30+ IST)](#post-session-1530-ist)
- [Emergency Procedures](#emergency-procedures)

---

## T-1 Day — Preparation

_Run the evening before the planned Stage-1 session. Allow 2–3 hours._

### T-1.1 Verify runtime check passes

```bash
# Run read-only runtime state verification
# This must exit 0 before anything else is touched.
PYTHONPATH=services python3 scripts/read_only_live_readiness_runtime_check.py --json

# Expected exit code: 0 (PAPER_SAFE)
# If exit 1 or 2: resolve all failures before proceeding
```

Record result: `T-1.1 runtime check → EXIT CODE: ___  STATUS: ___`

**No-go if:** non-zero exit code, any DynamoDB tables unreachable, kill switch active.

---

### T-1.2 Verify AWS infrastructure ready

```bash
# Check all 5 production ASGs have ≥1 InService instance
for ASG in \
  quantembrace-prod-risk-engine-asg \
  quantembrace-prod-execution-engine-asg \
  quantembrace-prod-data-ingestion-nse-asg \
  quantembrace-prod-strategy-engine-asg; do
  python scripts/deploy/check_asg_health.py \
    --asg "$ASG" \
    --min-healthy 1 \
    --timeout 30 \
    --region ap-south-1 && echo "  ✅ $ASG" || echo "  ❌ $ASG"
done

# Confirm DynamoDB tables are active (spot-check three critical ones)
aws dynamodb describe-table \
  --table-name quantembrace-prod-orders \
  --region ap-south-1 \
  --query "Table.{Status:TableStatus,GSIs:GlobalSecondaryIndexes[*].{Name:IndexName,Status:IndexStatus}}"

aws dynamodb describe-table \
  --table-name quantembrace-prod-sessions \
  --region ap-south-1 \
  --query "Table.TableStatus"

aws dynamodb describe-table \
  --table-name quantembrace-prod-risk-state \
  --region ap-south-1 \
  --query "Table.TableStatus"
```

Record result: `T-1.2 ASGs: ___ / ___ healthy  sessions table: ___  orders table: ___`

**No-go if:** any ASG has 0 InService instances; `sessions` or `orders` table not ACTIVE; `symbol-status-index` GSI not ACTIVE.

---

### T-1.3 Verify Secrets Manager

```bash
# Verify Zerodha API credentials exist and are readable (do NOT print values)
aws secretsmanager describe-secret \
  --secret-id <ZERODHA_SECRET_ARN> \
  --region ap-south-1 \
  --query "{Name:Name,LastChanged:LastChangedDate,Status:DeletedDate}"

# Verify Alpaca API credentials exist (even if not used in Stage-1)
aws secretsmanager describe-secret \
  --secret-id <ALPACA_SECRET_ARN> \
  --region ap-south-1 \
  --query "{Name:Name,Status:DeletedDate}"
```

Replace `<ZERODHA_SECRET_ARN>` with the actual ARN from `prod/terraform.tfstate` or AWS console.

Record result: `T-1.3 Zerodha secret last changed: ___  Alpaca secret: ___`

**No-go if:** any secret not found, or `DeletedDate` is set (pending deletion).

---

### T-1.4 Verify broker token freshness path

```bash
# Check if today's session record exists in DynamoDB
# (actual live session not needed today — just confirm the table works)
TODAY=$(date +%Y-%m-%d)
aws dynamodb get-item \
  --table-name quantembrace-prod-sessions \
  --region ap-south-1 \
  --key "{\"PK\":{\"S\":\"SESSION#$TODAY\"},\"SK\":{\"S\":\"ZERODHA\"}}" \
  --query "Item.{token_present: access_token.S}" \
  2>/dev/null || echo "No session for today (expected if T-1)"
```

Confirm `scripts/zerodha_login.py` is working:

```bash
python scripts/zerodha_login.py status
# Expected: shows current token state or prompts for login
```

Record result: `T-1.4 zerodha_login.py status: ___`

**Action for tomorrow morning:** Run `python scripts/zerodha_login.py` before 08:30 IST to generate today's token. Token expires at ~07:30 IST daily.

---

### T-1.5 Verify LTP feed is publishing

```bash
# Check if LiveQuotePoller is writing to the latest-prices table
# Look for HDFCBANK quote row
aws dynamodb get-item \
  --table-name quantembrace-prod-latest-prices \
  --region ap-south-1 \
  --key "{\"PK\":{\"S\":\"QUOTE#NSE#HDFCBANK\"},\"SK\":{\"S\":\"LATEST\"}}" \
  --query "Item.{captured_at: captured_at_utc.S, bid: best_bid_price.S, ask: best_ask_price.S}"

# If running during market hours, captured_at should be < 120s ago
# If running T-1 evening (market closed), row may be absent or stale — acceptable
```

Record result: `T-1.5 LTP last updated: ___  (acceptable if T-1 evening)`

---

### T-1.6 Run Stage-1 critical tests

```bash
# Run the LiveGateChecker test suite and Phase 8 tests
python -m pytest tests/unit/test_live_gate_checker.py tests/unit/test_phase8_hardening.py \
  -v --tb=short 2>&1 | tail -20

# Must show: 101 passed (or >= 101 passed, 0 failed)
```

Record result: `T-1.6 tests: ___ passed / ___ failed`

**No-go if:** any test in `test_live_gate_checker.py` fails.

---

### T-1.7 Run deployment dry-run

```bash
# Confirm terraform is valid (no AWS changes — structural only)
cd infra/terraform/environments/prod
terraform validate 2>&1 | tail -5
# Expected: "Success! The configuration is valid" (1 warning about s3 filter is OK)

terraform fmt -check -recursive ../../ && echo "FMT_OK"
```

Record result: `T-1.7 terraform validate: ___  fmt: ___`

**No-go if:** `terraform validate` returns errors (not warnings).

---

### T-1.8 Test SNS alert delivery

```bash
# Get the alerts topic ARN from Terraform outputs or AWS console
ALERTS_ARN=$(aws sns list-topics --region ap-south-1 \
  --query "Topics[?contains(TopicArn,'quantembrace-prod-system-alerts')].TopicArn" \
  --output text)

KS_ARN=$(aws sns list-topics --region ap-south-1 \
  --query "Topics[?contains(TopicArn,'quantembrace-prod-kill-switch')].TopicArn" \
  --output text)

# Test alert delivery
aws sns publish \
  --region ap-south-1 \
  --topic-arn "$ALERTS_ARN" \
  --subject "[QUANTEMBRACE TEST] Stage-1 T-1 alert test" \
  --message "Stage-1 pre-validation alert test from T-1 runbook. No action required."

echo "Check email inbox for: [QUANTEMBRACE TEST] Stage-1 T-1 alert test"
```

Record result: `T-1.8 email received: YES/NO  at: ___`

**No-go if:** email not received within 5 minutes. Check SNS subscription confirmation (may need to click email confirmation link if first-time setup).

---

### T-1.9 Test kill switch (safe test only)

```bash
# Check current kill switch status (read-only — no activation)
python scripts/kill_switch_cli.py status

# Expected output:
# ✅ KILL SWITCH — TRADING IS ACTIVE (inactive)
# Kill switch is INACTIVE. Trading is permitted.
```

**Do NOT activate the kill switch as a test on T-1 day.** Activation requires deactivation which requires `"I confirm trading should resume"` confirmation. Only test status read on T-1.

Record result: `T-1.9 kill switch status: ___`

**No-go if:** kill switch is already ACTIVE (means a prior incident was not resolved).

---

### T-1.10 Verify dashboards

```bash
# Open CloudWatch dashboard in browser
echo "CloudWatch dashboard URL:"
echo "https://ap-south-1.console.aws.amazon.com/cloudwatch/home?region=ap-south-1#dashboards:name=quantembrace-prod-overview"
```

Manually confirm:
- [ ] Dashboard loads without error
- [ ] "Kill Switch Activations" widget shows 0
- [ ] "Daily P&L" widget is present
- [ ] "Zerodha Fill Detection Latency" widget is present
- [ ] No P0 alarms currently in ALARM state (apart from ECS namespace alarms, which are silenced)

Record result: `T-1.10 dashboard accessible: YES/NO  P0 alarms active: YES/NO`

**No-go if:** any trading P0 alarm (not ECS namespace) is in ALARM state.

---

### T-1.11 Verify rollback readiness

```bash
# Confirm rollback scripts exist and are executable
ls -la scripts/deploy/promote_ecr_image.sh
ls -la scripts/kill_switch_cli.py
ls -la scripts/strategy/config.py
ls -la scripts/ops/reconcile.py

# Confirm current prod image SHA is tagged and retrievable
aws ecr list-images \
  --repository-name quantembrace-execution_engine \
  --region ap-south-1 \
  --query "imageIds[?imageTag=='latest-prod'].imageTag" \
  --output text 2>/dev/null && echo "Prod image tag confirmed" || echo "WARN: no latest-prod tag"

# Record the current SHA for rollback reference
CURRENT_SHA=$(aws ecr list-images \
  --repository-name quantembrace-execution_engine \
  --region ap-south-1 \
  --query "imageIds[?imageTag=='latest-prod'].imageDigest" \
  --output text 2>/dev/null | cut -c-12)
echo "Current execution_engine image digest prefix: $CURRENT_SHA"
```

Record result: `T-1.11 rollback SHA prefix: ___  scripts present: YES/NO`

---

### T-1 Day Checklist

```
[ ] T-1.1  Runtime check exits 0
[ ] T-1.2  All 5 ASGs healthy, sessions + orders tables ACTIVE
[ ] T-1.3  Secrets Manager: Zerodha and Alpaca secrets exist
[ ] T-1.4  zerodha_login.py status works; morning token plan confirmed
[ ] T-1.5  LTP path works (market closed — acceptable if stale)
[ ] T-1.6  Test suite: 0 failures in test_live_gate_checker.py
[ ] T-1.7  terraform validate passes
[ ] T-1.8  SNS test email received
[ ] T-1.9  Kill switch INACTIVE
[ ] T-1.10 Dashboard accessible, no P0 alerts
[ ] T-1.11 Rollback SHA recorded: _______________
```

**If any T-1 item is NOT checked: do not proceed to live day. Resolve and re-verify.**

---

## Pre-Market — Day Of (07:00–09:14 IST)

_All steps must complete before 09:14 IST. Allow 60–75 minutes._

### PM.1 Refresh Zerodha broker token (07:00–07:30 IST)

**Critical window: token expires at ~07:30 IST. Start this step at 07:00 IST.**

```bash
# Step 1: Open the Zerodha login URL
python scripts/zerodha_login.py
# → Prints login URL like: https://kite.trade/connect/login?api_key=...&v=3
# → Open this URL in browser → Zerodha login page

# Step 2: Log in with Zerodha credentials
# → After login, Zerodha redirects to a URL containing request_token=...
# → Copy the request_token value

# Step 3: Enter the request_token when prompted
# → Script exchanges it for an access_token
# → Stores token in DynamoDB sessions table (quantembrace-prod-sessions)
# → Prints: "Token stored successfully. Expires at: YYYY-MM-DD 02:00:00 UTC"

# Verify the token was stored
TODAY=$(date +%Y-%m-%d)
aws dynamodb get-item \
  --table-name quantembrace-prod-sessions \
  --region ap-south-1 \
  --key "{\"PK\":{\"S\":\"SESSION#$TODAY\"},\"SK\":{\"S\":\"ZERODHA\"}}" \
  --query "Item.{token_present: access_token.S, expires: expires_at.S}" \
  2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print('TOKEN:', 'PRESENT' if d.get('token_present') else 'MISSING', '| EXPIRES:', d.get('expires','?'))"
```

Record result: `PM.1 token stored: YES/NO  expires: ___`

**No-go if:** token not stored or DynamoDB write failed.

---

### PM.2 Verify broker positions flat (07:30–08:00 IST)

```bash
# Check Zerodha has zero open positions from prior day
# (Use position_audit script — read-only)
python scripts/zerodha/position_audit.py 2>/dev/null || \
  echo "position_audit.py not available — check Zerodha console manually"

# Check DynamoDB live positions table
aws dynamodb scan \
  --table-name quantembrace-prod-positions \
  --region ap-south-1 \
  --filter-expression "attribute_exists(quantity) AND quantity <> :zero" \
  --expression-attribute-values "{\":zero\":{\"N\":\"0\"}}" \
  --select "COUNT" \
  --query "Count"
# Expected: 0
```

Record result: `PM.2 DynamoDB positions: ___ open  Zerodha console: ___ open`

**No-go if:** any open positions in DynamoDB or Zerodha. Run reconciliation to clear:
```bash
python scripts/ops/reconcile.py --environment prod --status
```

---

### PM.3 Verify kill switch OFF

```bash
python scripts/kill_switch_cli.py status

# Expected output must contain:
#   Kill switch is INACTIVE. Trading is permitted.
```

Record result: `PM.3 kill switch: INACTIVE / ACTIVE`

**No-go if:** ACTIVE. Do NOT deactivate without understanding why it was triggered. Investigate first.

---

### PM.4 Verify reconciliation clean

```bash
python scripts/ops/reconcile.py --environment prod --status

# Expected output:
#   reconciliation_required = CLEAR (False)
#   No halt flag set.
```

Record result: `PM.4 reconciliation: CLEAR / REQUIRED`

**No-go if:** `reconciliation_required = True`. Do NOT override. Investigate position discrepancy and fix.

---

### PM.5 Verify LTP feed is fresh (check at 09:00 IST, after market opens at 09:15)

_Perform this check at 09:05 IST or later — LiveQuotePoller starts publishing after market open._

```bash
# Check HDFCBANK LTP age
aws dynamodb get-item \
  --table-name quantembrace-prod-latest-prices \
  --region ap-south-1 \
  --key "{\"PK\":{\"S\":\"QUOTE#NSE#HDFCBANK\"},\"SK\":{\"S\":\"LATEST\"}}" \
  --query "Item.{updated: captured_at_utc.S, bid: best_bid_price.S, ask: best_ask_price.S}"

# Calculate age (must be < 120 seconds)
UPDATED=$(aws dynamodb get-item \
  --table-name quantembrace-prod-latest-prices \
  --region ap-south-1 \
  --key "{\"PK\":{\"S\":\"QUOTE#NSE#HDFCBANK\"},\"SK\":{\"S\":\"LATEST\"}}" \
  --query "Item.captured_at_utc.S" \
  --output text 2>/dev/null)
echo "LTP updated at (UTC): $UPDATED"
python3 -c "
from datetime import datetime,timezone
ts = datetime.fromisoformat('$UPDATED')
age = (datetime.now(timezone.utc) - ts).total_seconds()
print(f'LTP age: {age:.0f}s  (PASS if < 120s, FAIL if >= 120s)')
" 2>/dev/null || echo "Could not parse timestamp"
```

Record result: `PM.5 HDFCBANK LTP age: ___s  Status: PASS/FAIL`

**No-go if:** LTP age >= 120 seconds after 09:05 IST. This means LiveQuotePoller is not running or HDFCBANK is not in watchlist.

---

### PM.6 Verify TradeExitEngine (TEE) active

```bash
# Check TEE heartbeat in latest-prices table
aws dynamodb get-item \
  --table-name quantembrace-prod-latest-prices \
  --region ap-south-1 \
  --key "{\"PK\":{\"S\":\"HEARTBEAT#TEE\"},\"SK\":{\"S\":\"CURRENT\"}}" \
  --query "Item.updated_at.S"

# If no heartbeat key: check execution_engine logs via SSM
EXEC_INSTANCE=$(aws autoscaling describe-auto-scaling-groups \
  --region ap-south-1 \
  --auto-scaling-group-names quantembrace-prod-execution-engine-asg \
  --query "AutoScalingGroups[0].Instances[0].InstanceId" \
  --output text 2>/dev/null)
echo "execution_engine instance: $EXEC_INSTANCE"
echo "To check TEE logs: aws ssm start-session --target $EXEC_INSTANCE --region ap-south-1"
echo "Then in session: sudo journalctl -u quantembrace-execution_engine --no-pager -n 50 | grep tee"
```

Record result: `PM.6 TEE heartbeat: ___  TEE log confirms active: YES/NO`

**No-go if:** TEE heartbeat stale (> 120s) AND execution_engine logs show `tee.stopped` or `tee.not_started`.

---

### PM.7 Verify MIS Square-Off Manager armed

```bash
# Check execution_engine heartbeat
aws dynamodb get-item \
  --table-name quantembrace-prod-latest-prices \
  --region ap-south-1 \
  --key "{\"PK\":{\"S\":\"HEARTBEAT#EXECUTION_ENGINE\"},\"SK\":{\"S\":\"CURRENT\"}}" \
  --query "Item.updated_at.S"

# MIS armed means execution_engine is running and mis_square_off.scheduled is in logs
# Connect via SSM and check:
# sudo journalctl -u quantembrace-execution_engine --no-pager -n 100 | grep mis_square_off
```

Record result: `PM.7 execution_engine heartbeat: ___  MIS log: mis_square_off.scheduled seen: YES/NO`

**No-go if:** execution_engine not running, or `mis_square_off.scheduled` never appears in logs.

---

### PM.8 Verify allowed symbols configured

```bash
# Check STRATEGY_WATCHLIST_NSE is set on running execution_engine instance
EXEC_INSTANCE=$(aws autoscaling describe-auto-scaling-groups \
  --region ap-south-1 \
  --auto-scaling-group-names quantembrace-prod-execution-engine-asg \
  --query "AutoScalingGroups[0].Instances[0].InstanceId" \
  --output text)

# Via SSM session (connect first, then run):
# grep STRATEGY_WATCHLIST_NSE /opt/quantembrace/execution-engine.env
# Expected: STRATEGY_WATCHLIST_NSE=HDFCBANK,ICICIBANK

echo "Connect to instance: aws ssm start-session --target $EXEC_INSTANCE --region ap-south-1"
echo "Then: grep STRATEGY_WATCHLIST_NSE /opt/quantembrace/execution-engine.env"
```

Record result: `PM.8 STRATEGY_WATCHLIST_NSE value: ___`

**No-go if:** variable is empty or contains symbols outside `HDFCBANK,ICICIBANK`.

---

### PM.9 Verify allowed strategies (all paper=True except nse_vwap_reversion)

```bash
python scripts/strategy/config.py list --env production

# Expected output:
#   nse_vwap_reversion   paper=False   enabled=True   max_signals/day=2
#   orb_15m              paper=True    enabled=True
#   scalp_1m             paper=True    enabled=True
#   intraday_trend_15m   paper=True    enabled=True
#   preclose_momentum    paper=True    enabled=True
#   momentum_v2          paper=True    enabled=True
#   us_momentum_v1       paper=True    enabled=False  (US market, not active)
```

Record result: `PM.9 live strategy count: ___  name: ___  paper=False: YES/NO`

**No-go if:** more than ONE strategy has `paper=False`, or `nse_vwap_reversion` is not the one.

---

### PM.10 Verify Stage-1 limits

```bash
# Check risk_limits_production.yaml matches Stage-1 values
python3 -c "
import yaml
with open('configs/risk_limits_production.yaml') as f:
    cfg = yaml.safe_load(f)

checks = {
    'portfolio_value':         (cfg.get('portfolio_value'), 50_000, '<='),
    'max_single_order_value':  (cfg.get('max_single_order_value'), 2_000, '<='),
    'max_concurrent_positions':(cfg.get('max_concurrent_positions'), 1, '<='),
    'max_open_orders':         (cfg.get('max_open_orders'), 1, '<='),
    'max_daily_loss_pct':      (cfg.get('max_daily_loss_pct'), 1.0, '<='),
}
all_ok = True
for field, (val, limit, op) in checks.items():
    ok = val is not None and val <= limit
    status = '✅' if ok else '❌'
    print(f'  {status} {field} = {val} (limit {op} {limit})')
    if not ok:
        all_ok = False
print()
print('STAGE-1 LIMITS:', 'PASS' if all_ok else 'FAIL — DO NOT PROCEED')
"
```

Record result: `PM.10 Stage-1 limits: PASS / FAIL`

**No-go if:** any field exceeds its Stage-1 limit. Apply the Stage-1 config changes from `docs/live-readiness/stage1-config-pack.md §1.1`.

---

### PM.11 Verify live gate is BLOCKED until manual approval

```bash
# Run LiveGateChecker (should return BLOCKED — live gate not yet activated)
PYTHONPATH=services python3 -c "
import asyncio, boto3
from shared.live_gate_checker import LiveGateChecker
from shared.config.settings import get_settings

checker = LiveGateChecker(
    settings=get_settings(),
    dynamo_client=boto3.client('dynamodb', region_name='ap-south-1'),
    risk_state_table='quantembrace-prod-risk-state',
    orders_table='quantembrace-prod-orders',
    strategy_config_table='quantembrace-prod-strategy-config',
    sessions_table='quantembrace-prod-sessions',
    prices_table='quantembrace-prod-latest-prices',
    cw_client=boto3.client('cloudwatch', region_name='ap-south-1'),
)
result = asyncio.run(checker.check_all())
print(result.status, '--', result.summary)
for c in result.checks:
    symbol = '✅' if c.status == 'PASS' else ('⚠️' if c.status == 'WARN' else '❌')
    print(f'  {symbol} [{c.status:4}] {c.name}')
print()
print('EXPECTED STATUS: BLOCKED (live_trading_enabled=false, no approval record yet)')
import sys; sys.exit(0 if result.status != 'APPROVED' else 1)  # BLOCKED or DEGRADED expected here
"
```

Record result: `PM.11 LiveGateChecker status: ___  (expected: BLOCKED or DEGRADED)`

**Stop here if:** LiveGateChecker returns APPROVED at this stage — that means `QE_EXECUTION_LIVE_TRADING_ENABLED=true` is already set, which is incorrect for pre-activation.

---

### Pre-Market Checklist

```
Time started: ___:___ IST

[ ] PM.1   Zerodha token refreshed and stored in DynamoDB
[ ] PM.2   DynamoDB positions: 0 open  |  Zerodha console: 0 positions
[ ] PM.3   Kill switch: INACTIVE
[ ] PM.4   Reconciliation: CLEAR
[ ] PM.5   HDFCBANK LTP age: ___s  (must be < 120s by 09:10)
[ ] PM.6   TEE active (heartbeat or log confirmed)
[ ] PM.7   MIS armed (execution_engine running, scheduled log present)
[ ] PM.8   STRATEGY_WATCHLIST_NSE = HDFCBANK,ICICIBANK
[ ] PM.9   Only nse_vwap_reversion has paper=False
[ ] PM.10  Stage-1 limits: PASS (portfolio≤₹50k, order≤₹2k, positions≤1)
[ ] PM.11  LiveGateChecker returns BLOCKED (correct — not yet activated)

Time completed: ___:___ IST
```

**Proceed to live gate activation ONLY if all 11 items are checked.**  
**Abort if any item is not checked by 09:10 IST.**

---

## Live Gate Activation (09:14–09:15 IST)

_This window is intentionally tight. Activation happens at 09:14 IST to catch the opening candle._
_If activation takes longer than expected, abort and wait for 09:30 IST (after initial volatility)._

### 4.1 Final LiveGateChecker run

```bash
# Full gate check — must return APPROVED before activation
PYTHONPATH=services python3 -c "
import asyncio, boto3
from shared.live_gate_checker import LiveGateChecker
from shared.config.settings import get_settings

checker = LiveGateChecker(
    settings=get_settings(),
    dynamo_client=boto3.client('dynamodb', region_name='ap-south-1'),
    risk_state_table='quantembrace-prod-risk-state',
    orders_table='quantembrace-prod-orders',
    strategy_config_table='quantembrace-prod-strategy-config',
    sessions_table='quantembrace-prod-sessions',
    prices_table='quantembrace-prod-latest-prices',
    cw_client=boto3.client('cloudwatch', region_name='ap-south-1'),
)
result = asyncio.run(checker.check_all())
print(result.status, '--', result.summary)
for c in result.checks:
    symbol = '✅' if c.status == 'PASS' else ('⚠️' if c.status == 'WARN' else '❌')
    print(f'  {symbol} [{c.status:4}] {c.name}')
import sys; sys.exit(0 if result.status == 'APPROVED' else 1)
" && echo "→ GATE: APPROVED. Proceed to 4.2." \
  || echo "→ GATE: NOT APPROVED. Abort live activation."
```

**Abort immediately if:** LiveGateChecker returns BLOCKED or DEGRADED.

---

### 4.2 Write manual approval record to DynamoDB

```bash
# This is the point of no return for approval. Type carefully.
RELEASE_TAG=$(git rev-parse --short HEAD 2>/dev/null || echo "manual")
TODAY_ISO=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)

aws dynamodb put-item \
  --region ap-south-1 \
  --table-name quantembrace-prod-risk-state \
  --item "{
    \"PK\":                      {\"S\": \"LIVE_GATE#APPROVAL\"},
    \"SK\":                      {\"S\": \"CURRENT\"},
    \"approval_token\":          {\"S\": \"stage1-$(date +%Y-%m-%d)-op1\"},
    \"live_stage\":              {\"S\": \"STAGE_1_ONE_SHARE\"},
    \"max_capital\":             {\"N\": \"50000\"},
    \"approved_by\":             {\"S\": \"hari.mosoju@gmail.com\"},
    \"approved_at\":             {\"S\": \"$TODAY_ISO\"},
    \"release_tag\":             {\"S\": \"$RELEASE_TAG\"},
    \"rollback_plan_confirmed\": {\"BOOL\": true},
    \"sns_alert_tested\":        {\"BOOL\": true}
  }"

echo "Approval record written. Release: $RELEASE_TAG  At: $TODAY_ISO"
```

Record: `4.2 approval written at: ___  release: ___`

---

### 4.3 Enable live trading (Terraform + ASG refresh)

```bash
# Step 1: Edit execution_engine.sh userdata (uncomment live gate)
# In infra/terraform/modules/ec2_services/userdata/execution_engine.sh
# Change:  # QE_EXECUTION_LIVE_TRADING_ENABLED=true
# To:      QE_EXECUTION_LIVE_TRADING_ENABLED=true

# Step 2: Apply Terraform (execution_engine only)
cd infra/terraform/environments/prod
terraform apply \
  -target=module.ec2_services.aws_launch_template.execution_engine \
  -auto-approve 2>&1 | tail -5

# Step 3: Trigger ASG instance refresh
aws autoscaling start-instance-refresh \
  --region ap-south-1 \
  --auto-scaling-group-name quantembrace-prod-execution-engine-asg \
  --preferences '{"MinHealthyPercentage": 0, "InstanceWarmup": 60}'

# Wait for refresh to complete (~2-3 minutes)
aws autoscaling describe-instance-refreshes \
  --region ap-south-1 \
  --auto-scaling-group-name quantembrace-prod-execution-engine-asg \
  --query "InstanceRefreshes[0].{Status:Status,PercentageComplete:PercentageComplete}"
```

Record: `4.3 terraform apply: OK/FAIL  instance refresh: STARTED`

---

### 4.4 Change risk profile to tiny-live (risk_engine)

```bash
# Edit risk_engine.sh userdata
# Change: RISK_PROFILE=paper
# To:     RISK_PROFILE=tiny-live

cd infra/terraform/environments/prod
terraform apply \
  -target=module.ec2_services.aws_launch_template.risk_engine \
  -auto-approve 2>&1 | tail -5

aws autoscaling start-instance-refresh \
  --region ap-south-1 \
  --auto-scaling-group-name quantembrace-prod-risk-engine-asg \
  --preferences '{"MinHealthyPercentage": 0, "InstanceWarmup": 60}'
```

Record: `4.4 risk profile change applied: YES/NO`

---

### 4.5 Wait for instances to stabilise (2–3 minutes)

```bash
# Monitor both ASG refreshes
watch -n 10 'aws autoscaling describe-instance-refreshes \
  --region ap-south-1 \
  --auto-scaling-group-name quantembrace-prod-execution-engine-asg \
  --query "InstanceRefreshes[0].{Status:Status,Pct:PercentageComplete}" && \
aws autoscaling describe-instance-refreshes \
  --region ap-south-1 \
  --auto-scaling-group-name quantembrace-prod-risk-engine-asg \
  --query "InstanceRefreshes[0].{Status:Status,Pct:PercentageComplete}"'
```

Wait until both show `Status: Successful`.

---

### 4.6 Confirm live gate is now APPROVED

```bash
# Run LiveGateChecker again — must return APPROVED
PYTHONPATH=services python3 -c "
import asyncio, boto3
from shared.live_gate_checker import LiveGateChecker
from shared.config.settings import get_settings

checker = LiveGateChecker(
    settings=get_settings(),
    dynamo_client=boto3.client('dynamodb', region_name='ap-south-1'),
    risk_state_table='quantembrace-prod-risk-state',
    orders_table='quantembrace-prod-orders',
    strategy_config_table='quantembrace-prod-strategy-config',
    sessions_table='quantembrace-prod-sessions',
    prices_table='quantembrace-prod-latest-prices',
    cw_client=boto3.client('cloudwatch', region_name='ap-south-1'),
)
result = asyncio.run(checker.check_all())
print(result.status, '--', result.summary)
for c in result.checks:
    symbol = '✅' if c.status == 'PASS' else ('⚠️' if c.status == 'WARN' else '❌')
    print(f'  {symbol} [{c.status:4}] {c.name}')
import sys; sys.exit(0 if result.status == 'APPROVED' else 1)
" && echo "→ LIVE GATE: APPROVED. Begin monitoring." \
  || echo "→ LIVE GATE: NOT APPROVED. Abort."
```

Record: `4.6 LiveGateChecker post-activation: APPROVED / BLOCKED`

**Abort and rollback if:** still BLOCKED or DEGRADED after instance refresh completes.

---

### 4.7 Open monitoring terminals

Open three terminal windows simultaneously:

**Terminal A — Live counters (primary monitor)**
```bash
python scripts/monitoring/paper_trading_monitor.py \
  --counters /tmp/qe_live_counters.json \
  --watch 30 \
  --trading-mode STAGE_1_LIVE
```

**Terminal B — execution_engine logs (live orders)**
```bash
EXEC_INSTANCE=$(aws autoscaling describe-auto-scaling-groups \
  --region ap-south-1 \
  --auto-scaling-group-names quantembrace-prod-execution-engine-asg \
  --query "AutoScalingGroups[0].Instances[0].InstanceId" \
  --output text)
aws ssm start-session --target "$EXEC_INSTANCE" --region ap-south-1
# In session:
# sudo journalctl -u quantembrace-execution_engine -f --no-pager
```

**Terminal C — risk_engine logs (signal approvals/rejections)**
```bash
RISK_INSTANCE=$(aws autoscaling describe-auto-scaling-groups \
  --region ap-south-1 \
  --auto-scaling-group-names quantembrace-prod-risk-engine-asg \
  --query "AutoScalingGroups[0].Instances[0].InstanceId" \
  --output text)
aws ssm start-session --target "$RISK_INSTANCE" --region ap-south-1
# In session:
# sudo journalctl -u quantembrace-risk_engine -f --no-pager
```

---

## Live Validation Session (09:15–15:00 IST)

### 5.1 Monitor order placement

**Watch Terminal A for these expected events (in order):**

```
1. Signal generated:
   Log pattern: "strategy_engine.signal_published strategy=nse_vwap_reversion symbol=HDFCBANK"
   → Expected within first 30–60 minutes of NORMAL phase (09:30 IST)

2. Signal approved by risk_engine:
   Log pattern: "risk_engine.signal_approved signal_id=... strategy=nse_vwap_reversion"
   → Appears within seconds of signal generation

3. Order placed by execution_engine:
   Log pattern: "execution_engine.order_placed broker_order_id=... symbol=HDFCBANK qty=1"
   → Appears within 1-2 seconds of approval

4. Order filled:
   Log pattern: "execution_engine.fill_recorded order_id=... fill_price=... qty=1"
```

**If no signal after 60 minutes of NORMAL phase:**
- Check risk_engine logs for `signal_age_rejected` — means LTP is stale
- Check strategy_engine logs for `candle_stream.candles_fetched` — confirms candle data arriving
- Check DynamoDB candle-cache for recent entries

---

### 5.2 Verify position appears in broker AND DynamoDB

**After first fill, verify within 30 seconds:**

```bash
# Check DynamoDB position
aws dynamodb get-item \
  --table-name quantembrace-prod-positions \
  --region ap-south-1 \
  --key "{\"PK\":{\"S\":\"POSITION#HDFCBANK\"},\"SK\":{\"S\":\"CURRENT\"}}" \
  --query "Item.{qty:quantity.N, side:direction.S, price:avg_entry_price.N}"

# Check broker position via Zerodha console or:
python scripts/zerodha/position_audit.py 2>/dev/null

# Both should show:
#   HDFCBANK   qty=1   direction=LONG (or SHORT if sold)
#   avg_entry_price ≈ current market price
```

Record: `5.2 DynamoDB qty: ___  Broker qty: ___  Direction: ___  Price: ___`

**Abort if:** DynamoDB and broker positions disagree. Activate kill switch and run reconciliation.

---

### 5.3 Verify exit policy attached (TEE managing the position)

```bash
# Check that TradeExitEngine is monitoring the open position
# Look for the position in TEE's monitored set:
aws dynamodb get-item \
  --table-name quantembrace-prod-positions \
  --region ap-south-1 \
  --key "{\"PK\":{\"S\":\"POSITION#HDFCBANK\"},\"SK\":{\"S\":\"CURRENT\"}}" \
  --query "Item.{exit_order_id: exit_order_id.S, stop_loss: stop_loss_price.N, take_profit: take_profit_price.N}"

# Expected: stop_loss_price and take_profit_price are set (non-null)
# If exit_order_id is set: TEE has already placed an exit order — this is correct
```

Record: `5.3 Stop loss: ___  Take profit: ___  Exit order active: YES/NO`

**Action if stop_loss and take_profit are both null:** The position is unmanaged. Monitor closely. If price moves >2% against position: manually activate kill switch and close manually via Zerodha console.

---

### 5.4 Continuous monitoring checkpoints (every 15 minutes)

At each 15-minute mark during the session, verify:

```
Time: ___:___ IST

[ ] Kill switch: INACTIVE  (python scripts/kill_switch_cli.py status)
[ ] Position count: 0 or 1  (aws dynamodb scan ... --select COUNT)
[ ] TEE counters in Terminal A: stop_loss_exits or take_profit_exits incrementing
[ ] No CRITICAL logs in Terminal B or C
[ ] Zerodha 429 rate limit alarm: NOT in ALARM state
[ ] Daily P&L: between -₹250 and +₹₹₹₹  (within loss limit)
```

---

### 5.5 Verify TEE and MIS are running throughout session

```bash
# TEE running check — heartbeat should be < 120s old
aws dynamodb get-item \
  --table-name quantembrace-prod-latest-prices \
  --region ap-south-1 \
  --key "{\"PK\":{\"S\":\"HEARTBEAT#TEE\"},\"SK\":{\"S\":\"CURRENT\"}}" \
  --query "Item.updated_at.S"

# MIS armed check — verify current time vs scheduled fire time
# MIS fires at 15:05 IST; at 14:55 IST, check logs for:
# "mis_square_off.scheduled close_time_ist=15:05 seconds_until_close=600"
```

---

### 5.6 Square off early if position mismatch

**If broker and DynamoDB positions disagree at any point:**

```bash
# Step 1: Activate kill switch immediately
python scripts/kill_switch_cli.py activate \
  --reason "Position mismatch between broker and DynamoDB — Stage-1 abort"

# Step 2: Manually close position in Zerodha console
# (Do NOT use automated scripts until mismatch is understood)

# Step 3: Run reconciliation to understand the discrepancy
python scripts/ops/reconcile.py --environment prod --status

# Step 4: Do NOT deactivate kill switch today. End session.
# Resume on next trading day after investigating root cause.
```

---

## No-Go Criteria

**Abort and activate kill switch immediately if any of the following occur:**

### At any point during the session

| Condition | Action |
|-----------|--------|
| LTP age > 120s for HDFCBANK | Kill switch + investigate LiveQuotePoller |
| Broker API returning errors (404, 429 sustained) | Kill switch + check Zerodha status page |
| DynamoDB and broker position quantities disagree | Kill switch + reconcile + close manually |
| Any alert fires on `quantembrace-prod-kill-switch` SNS topic | Kill switch ALREADY activated by auto-trigger — investigate |
| TEE stops responding (no logs for >5 min) | Kill switch + restart execution_engine |
| MIS not firing at 15:05 IST | Kill switch at 15:06 IST + manually close via Zerodha |
| LiveGateChecker BLOCKED mid-session | Kill switch + investigate which check failed |
| Any CRITICAL log in execution_engine or risk_engine | Evaluate severity; kill switch if position-affecting |
| Unknown runtime state (process crash, OOM) | Kill switch + restart service + verify flat before resuming |

### Pre-activation no-go (abort live activation)

| Condition | Abort Action |
|-----------|-------------|
| Stale LTP (> 120s) at 09:10 IST | Skip today; investigate LiveQuotePoller |
| Broker API unstable (errors in logs) | Skip today; check Zerodha status |
| Reconciliation mismatch (required=True) | Never go live with dirty reconciliation |
| Alerts not working (SNS test failed) | Fix SNS subscription before proceeding |
| TEE inactive or not found | Fix TEE before going live |
| MIS not armed (no scheduled log) | Fix MIS before going live |
| LiveGateChecker not APPROVED | All 25 gates must pass |
| Unknown runtime state | Resolve all unknowns before going live |
| Any P0 alarm currently in ALARM state | Resolve alarm before going live |

**Kill switch command (use immediately, ask questions later):**
```bash
python scripts/kill_switch_cli.py activate --reason "<reason — be specific>"
```

---

## Session Close (15:00–15:30 IST)

### 6.1 No new entries after 15:00 IST

At 15:00 IST, confirm no new signals are being generated (the no-new-entry cutoff check in LiveGateChecker blocks this automatically). Any open position must be closed by 15:05 IST by MIS.

```bash
# Verify strategy is not generating new signals after 15:00
# Check execution_engine logs for any order placements after 15:00:
# sudo journalctl -u quantembrace-execution_engine --since "15:00" --no-pager | grep "order_placed"
# Expected: empty output (no new orders after 15:00)
```

---

### 6.2 Confirm MIS square-off fires at 15:05 IST

```bash
# Watch execution_engine logs at 15:05 IST:
# Expected sequence:
#   mis_square_off.starting close_time_ist=15:05
#   mis_square_off.positions_found position_count=1 symbols=['HDFCBANK']
#   mis_square_off.close_order_placed symbol=HDFCBANK
#   mis_square_off.position_closed symbol=HDFCBANK
#   mis_square_off.all_positions_closed
```

Record: `6.2 MIS fired: YES/NO  Position closed: YES/NO  at: ___:___ IST`

**If MIS does not fire by 15:06 IST:**
```bash
# Activate kill switch
python scripts/kill_switch_cli.py activate --reason "MIS did not fire — manual close required"
# Close position manually in Zerodha console
# Do not reactivate trading today
```

---

### 6.3 Confirm flat positions

```bash
# Verify DynamoDB positions table is empty (or qty=0 for HDFCBANK)
aws dynamodb scan \
  --table-name quantembrace-prod-positions \
  --region ap-south-1 \
  --filter-expression "attribute_exists(quantity) AND quantity <> :zero" \
  --expression-attribute-values "{\":zero\":{\"N\":\"0\"}}" \
  --select "COUNT" \
  --query "Count"
# Expected: 0

# Verify Zerodha console shows 0 open positions
# (or quantity=0 on any MIS positions)
```

Record: `6.3 DynamoDB positions at close: ___  Broker positions: ___`

---

### 6.4 Disable live trading immediately after session

```bash
# Step 1: Edit execution_engine.sh userdata — comment out live gate
# Change: QE_EXECUTION_LIVE_TRADING_ENABLED=true
# To:     # QE_EXECUTION_LIVE_TRADING_ENABLED=true

# Step 2: Apply terraform and refresh
cd infra/terraform/environments/prod
terraform apply \
  -target=module.ec2_services.aws_launch_template.execution_engine \
  -auto-approve

aws autoscaling start-instance-refresh \
  --region ap-south-1 \
  --auto-scaling-group-name quantembrace-prod-execution-engine-asg \
  --preferences '{"MinHealthyPercentage": 0, "InstanceWarmup": 60}'

# Step 3: Flip nse_vwap_reversion back to paper
python scripts/strategy/config.py paper nse_vwap_reversion --env production

# Step 4: Confirm live trading disabled
python scripts/kill_switch_cli.py status
PYTHONPATH=services python3 -c "
import asyncio, boto3; from shared.live_gate_checker import LiveGateChecker
from shared.config.settings import get_settings
checker = LiveGateChecker(get_settings(), boto3.client('dynamodb', region_name='ap-south-1'),
    'quantembrace-prod-risk-state','quantembrace-prod-orders','quantembrace-prod-strategy-config',
    'quantembrace-prod-sessions','quantembrace-prod-latest-prices')
result = asyncio.run(checker.check_all())
print('LIVE GATE POST-CLOSE:', result.status, '(expected: BLOCKED)')
"
```

Record: `6.4 live_trading_enabled disabled: YES  LiveGateChecker post-close: ___`

---

## Post-Session (15:30+ IST)

### 7.1 Confirm P&L

```bash
# Export and review the session report
python scripts/monitoring/paper_session_report.py \
  --date $(date +%Y-%m-%d) \
  --mode live

# Key metrics to record:
#   Fills: ___
#   Total trades: ___ (should be ≤ 2)
#   P&L (realized): ___
#   Fill price vs signal price: ___bps slippage
#   Any rejected signals: ___
#   Kill switch events: ___ (should be 0)
#   TEE exits (stop/take-profit): ___
#   MIS exits: ___
```

Record: `7.1 fills: ___  P&L: ___  slippage: ___bps  kill switch events: ___`

---

### 7.2 Export report and archive

```bash
# Save the session report to a timestamped file
python scripts/monitoring/paper_session_report.py \
  --date $(date +%Y-%m-%d) \
  --mode live \
  > docs/live-readiness/session-reports/stage1-$(date +%Y-%m-%d).txt

echo "Session report saved to: docs/live-readiness/session-reports/stage1-$(date +%Y-%m-%d).txt"
```

---

### 7.3 Run post-session reconciliation

```bash
python scripts/ops/reconcile.py --environment prod --status

# Expected:
#   reconciliation_required = CLEAR
#   No mismatches detected.
```

Record: `7.3 post-session reconciliation: CLEAR / MISMATCH`

**If mismatch detected:** Do NOT re-enable live trading. Investigate the discrepancy and file an incident report before next session.

---

### 7.4 Update Stage-1 paper counter

```bash
# Record session in decisions.md as ADR
echo "Stage-1 session $(date +%Y-%m-%d): fills=___, P&L=___, slippage=___bps, outcome=___ " \
  >> memory/decisions.md

# Update go_live_checklist if session was successful
# In configs/risk_limits_production.yaml: increment five_day_paper_counter_reset tracking
```

---

### 7.5 Verify kill switch was NOT activated during session

```bash
# Confirm kill switch is INACTIVE (should have been throughout)
python scripts/kill_switch_cli.py status

# Review CloudWatch for any automatic kill switch triggers
aws cloudwatch get-metric-statistics \
  --region ap-south-1 \
  --namespace "QuantEmbrace/Trading" \
  --metric-name KillSwitchActivations \
  --start-time $(date -u -v-1d +%Y-%m-%dT00:00:00Z 2>/dev/null || date -u -d 'yesterday' +%Y-%m-%dT00:00:00Z) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 86400 \
  --statistics Sum \
  --query "Datapoints[*].Sum"
# Expected: [0] or empty
```

Record: `7.5 kill switch activations today: ___  (expected: 0)`

---

### Post-Session Checklist

```
[ ] 6.2  MIS fired at 15:05 and all positions closed
[ ] 6.3  DynamoDB positions: 0  |  Broker positions: 0
[ ] 6.4  live_trading_enabled disabled  |  nse_vwap_reversion paper=True
[ ] 6.4  LiveGateChecker post-close: BLOCKED (correct)
[ ] 7.1  Session report reviewed — fills: ___, P&L: ___
[ ] 7.2  Session report archived
[ ] 7.3  Reconciliation: CLEAR
[ ] 7.4  Stage-1 session recorded in decisions.md
[ ] 7.5  Kill switch activations: 0
```

**Stage-1 session is complete when all boxes are checked.**

---

## Emergency Procedures

### E.1 Unexpected position opened in wrong symbol

```bash
# Activate kill switch IMMEDIATELY
python scripts/kill_switch_cli.py activate \
  --reason "Unexpected position in non-whitelisted symbol during Stage-1"

# Manually close the position in Zerodha console
# Do NOT use automated scripts — manually confirm the close

# Run reconciliation after manual close
python scripts/ops/reconcile.py --environment prod --status

# Do NOT re-enable live trading today. Investigate UNIVERSE_MODE filter.
```

---

### E.2 Order quantity > 1 share placed

```bash
# Activate kill switch
python scripts/kill_switch_cli.py activate \
  --reason "Order quantity > 1 share placed during Stage-1 one-share validation"

# Review execution_engine logs for the order:
# sudo journalctl -u quantembrace-execution_engine --no-pager | grep "order_placed" | tail -20
# Note: broker_order_id, quantity, and fill_price

# Escalate to root cause analysis before re-enabling
# Check risk_limits_production.yaml max_single_order_value was correctly set
```

---

### E.3 Kill switch self-fires

```bash
# Do NOT immediately deactivate. First understand why.

# Check which auto-trigger fired:
python scripts/kill_switch_cli.py status
# Look for: activated_by field

# If activated_by = "daily_loss_validator":
#   → Daily loss limit exceeded. Check P&L. Accept and do not deactivate today.

# If activated_by = "mis-square-off-manager":
#   → MIS failed to close positions. Check broker state. Close manually if needed.

# If activated_by = "cloudwatch-auto-kill-switch-lambda":
#   → A P0 alarm fired. Check which alarm. Fix root cause.

# Only deactivate after root cause is confirmed resolved:
python scripts/kill_switch_cli.py deactivate
# (prompts for: "I confirm trading should resume")
```

---

### E.4 Service crash during live session

```bash
# Activate kill switch if position is open
python scripts/kill_switch_cli.py activate \
  --reason "execution_engine crash with open position — manual intervention required"

# Restart the service via ASG instance refresh
aws autoscaling start-instance-refresh \
  --region ap-south-1 \
  --auto-scaling-group-name quantembrace-prod-execution-engine-asg \
  --preferences '{"MinHealthyPercentage": 0, "InstanceWarmup": 60}'

# After instance restarts: verify reconciliation runs on startup
# execution_engine logs: "reconciliation.completed mismatches=0"
# Then deactivate kill switch only if reconciliation shows no mismatches
python scripts/ops/reconcile.py --environment prod --status
```

---

### E.5 Full rollback (abort Stage-1 permanently for the day)

```bash
# Step 1: Kill switch
python scripts/kill_switch_cli.py activate --reason "Stage-1 full rollback"

# Step 2: Disable live trading
# Edit execution_engine.sh: comment out QE_EXECUTION_LIVE_TRADING_ENABLED=true
cd infra/terraform/environments/prod
terraform apply -target=module.ec2_services.aws_launch_template.execution_engine -auto-approve
aws autoscaling start-instance-refresh \
  --region ap-south-1 \
  --auto-scaling-group-name quantembrace-prod-execution-engine-asg \
  --preferences '{"MinHealthyPercentage": 0, "InstanceWarmup": 60}'

# Step 3: Risk profile back to paper
# Edit risk_engine.sh: RISK_PROFILE=paper
terraform apply -target=module.ec2_services.aws_launch_template.risk_engine -auto-approve
aws autoscaling start-instance-refresh \
  --region ap-south-1 \
  --auto-scaling-group-name quantembrace-prod-risk-engine-asg \
  --preferences '{"MinHealthyPercentage": 0, "InstanceWarmup": 60}'

# Step 4: Strategy back to paper
python scripts/strategy/config.py paper nse_vwap_reversion --env production

# Step 5: Rollback image if code was the issue
scripts/deploy/promote_ecr_image.sh \
  --repository quantembrace-execution_engine \
  --source-tag <PREVIOUS_SHA> \
  --target-tag latest-prod

# Step 6: Reconcile and confirm flat
python scripts/ops/reconcile.py --environment prod --status

# Step 7: Deactivate kill switch ONLY after flat confirmed
python scripts/kill_switch_cli.py deactivate
```

---

## Quick Reference Card

```
KILL SWITCH ON:   python scripts/kill_switch_cli.py activate --reason "..."
KILL SWITCH OFF:  python scripts/kill_switch_cli.py deactivate   (confirmation required)
KILL SWITCH CHECK:python scripts/kill_switch_cli.py status

LIVE GATE CHECK:  PYTHONPATH=services python3 -c "import asyncio,boto3;from shared.live_gate_checker import LiveGateChecker;from shared.config.settings import get_settings;c=LiveGateChecker(get_settings(),boto3.client('dynamodb',region_name='ap-south-1'),'quantembrace-prod-risk-state','quantembrace-prod-orders','quantembrace-prod-strategy-config','quantembrace-prod-sessions','quantembrace-prod-latest-prices');r=asyncio.run(c.check_all());print(r.status)"

RECONCILE:        python scripts/ops/reconcile.py --environment prod --status
STRATEGY LIST:    python scripts/strategy/config.py list --env production
STRATEGY PAPER:   python scripts/strategy/config.py paper nse_vwap_reversion --env production
SESSION REPORT:   python scripts/monitoring/paper_session_report.py --date $(date +%Y-%m-%d)
MONITOR:          python scripts/monitoring/paper_trading_monitor.py --counters /tmp/qe_live_counters.json --watch 30

ASG HEALTH:       python scripts/deploy/check_asg_health.py --asg quantembrace-prod-execution-engine-asg --min-healthy 1
DASHBOARD:        https://ap-south-1.console.aws.amazon.com/cloudwatch/home?region=ap-south-1#dashboards:name=quantembrace-prod-overview
```

---

*Runbook created 2026-05-30. No live trading enabled. No deployment executed. No broker orders placed.*  
*This document is read-only until the operator explicitly begins T-1 preparation.*
