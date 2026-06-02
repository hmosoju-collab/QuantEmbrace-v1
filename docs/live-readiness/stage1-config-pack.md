# Stage-1 One-Share Live Validation — Config Pack

**Date prepared:** 2026-05-30
**Status:** PREPARED — NOT APPLIED
**Live trading:** NOT enabled. No config applied. No deployment.

> This document is a proposed diff only. Every change here requires:
> 1. Explicit operator review
> 2. Completion of all 25 LiveGateChecker gates
> 3. Manual approval recorded in DynamoDB
> 4. Explicit `terraform apply` + ASG instance refresh
>
> `live_trading_enabled` remains `false` until the final approval step.
> ₹1,000,000 capital remains BLOCKED. Stage-1 max capital is ₹50,000.

---

## Stage-1 Rules (Design Basis)

| Rule | Value |
|------|-------|
| Max quantity per order | **1 share** (enforced by ₹2,000 order cap on ₹1,600/share HDFCBANK) |
| Max concurrent positions | **1** |
| Max trades per day | **2** |
| Max capital deployed | **₹50,000** (₹50k; well below ₹10L limit) |
| Max order value | **₹2,000** |
| Max daily loss (abs) | **₹250** (0.5% of ₹50k) |
| Whitelisted strategy | **`nse_vwap_reversion`** |
| Whitelisted symbols | **`HDFCBANK`** (primary) + `ICICIBANK` (secondary; see §3) |
| Live trading gate | Remains `false` until all 25 LiveGateChecker gates pass + manual approval |
| 1M capital | **BLOCKED** — ₹1,000,000 is not a valid Stage-1 capital level |

---

## 1. Proposed Config Diff

### 1.1 `configs/risk_limits_production.yaml`

```yaml
# ── BEFORE (current) ─────────────────────────────────────────────────────────
portfolio_value: 1_000_000      # ₹10 lakh
max_daily_loss_pct: 2.0
max_position_size_pct: 5.0
max_total_exposure_pct: 50.0
max_single_order_value: 5000
max_open_orders: 10
max_concurrent_positions: 8
max_position_per_symbol: 2
go_live_checklist:
  terraform_plan_clean:               false
  instruments_yaml_verified:          false
  five_day_paper_counter_reset:       false
  zerodha_token_refreshed:            false
  kill_switch_confirmed_inactive:     false

# ── AFTER (Stage-1 one-share) ─────────────────────────────────────────────────
portfolio_value: 50_000         # ₹50k — Stage-1 tiny capital
max_daily_loss_pct: 0.5         # ₹250 absolute loss limit (0.5% × ₹50k)
max_position_size_pct: 4.0      # ₹2,000 max per position (4% × ₹50k)
max_total_exposure_pct: 4.0     # One position only; same cap as single position
max_single_order_value: 2000    # ₹2,000 — forces 1 share of HDFCBANK (~₹1,600)
max_open_orders: 1              # One in-flight order at any time
max_concurrent_positions: 1     # One position at a time
max_position_per_symbol: 1      # Never accumulate more than 1 share
go_live_checklist:
  terraform_plan_clean:               true   # Set after successful terraform plan
  instruments_yaml_verified:          true   # Set after reviewing this document
  five_day_paper_counter_reset:       true   # Set after completing 5 paper sessions
  zerodha_token_refreshed:            true   # Set each morning before market open
  kill_switch_confirmed_inactive:     true   # Set after preflight check confirms inactive
```

**Why ₹50,000 and not ₹10,00,000:**
At ₹10L with a ₹5k order cap, a single bad fill is 0.5% of capital — uncomfortable for a first live session but survivable. At ₹50k with a ₹2k cap, a single bad fill is 4% — painful, but the absolute loss in rupees (≤ ₹2,000) is small enough that the operator can absorb it without stress. The low absolute amount allows purely psychological comfort during the first live validation.

---

### 1.2 EC2 `risk_engine.sh` userdata — `RISK_PROFILE`

```bash
# ── BEFORE ───────────────────────────────────────────────────────────────────
RISK_PROFILE=paper

# ── AFTER (Stage-1) ──────────────────────────────────────────────────────────
RISK_PROFILE=tiny-live
# tiny-live profile: max_single_order_value=₹5,000 (overridden by risk_limits_production.yaml),
# max_open_orders=1, max_concurrent_positions=1, allow_leverage=False
```

**File:** `infra/terraform/modules/ec2_services/userdata/risk_engine.sh:145`
**Requires:** `terraform apply` + risk_engine ASG instance refresh

---

### 1.3 EC2 `execution_engine.sh` userdata — live gate + watchlist

```bash
# ── BEFORE ───────────────────────────────────────────────────────────────────
UNIVERSE_MODE=PAPER_SAFE_START
# QE_EXECUTION_LIVE_TRADING_ENABLED=true      ← still commented out
STRATEGY_WATCHLIST_NSE=                       ← empty (or full NIFTY 50 list)

# ── AFTER (Stage-1 live — set ONLY after all 25 gates pass + human approval) ──
UNIVERSE_MODE=PAPER_SAFE_START               # unchanged; NIFTY 50 universe is correct
QE_EXECUTION_LIVE_TRADING_ENABLED=true       # UNCOMMENT after final gate sign-off
STRATEGY_WATCHLIST_NSE=HDFCBANK,ICICIBANK    # Restrict LTP polling to whitelisted symbols only
```

**File:** `infra/terraform/modules/ec2_services/userdata/execution_engine.sh`
**`QE_EXECUTION_LIVE_TRADING_ENABLED=true` must NOT be uncommented until all 25 LiveGateChecker gates return PASS.**

---

### 1.4 DynamoDB `strategy-config` table — strategy flip (operator action)

This is a DynamoDB write, not a Terraform change. Run after all other config is applied.

```python
# Command (after all other gates pass):
python scripts/strategy/config.py go-live nse_vwap_reversion --env production

# Which writes to DynamoDB:
#   PK=STRATEGY_CONFIG#nse_vwap_reversion, SK=ENV#production
#   paper_trade=False, enabled=True, max_signals_per_day=2
```

**All other strategies remain `paper_trade=True`:**

```python
# Verify before flipping:
python scripts/strategy/config.py list --env production
# Expected output: all strategies paper=True except nse_vwap_reversion

# After flip:
# nse_vwap_reversion: paper=False, enabled=True, max_signals_per_day=2
# All others         : paper=True
```

---

### 1.5 LiveGate Approval Record (operator writes to DynamoDB)

```python
# Run AFTER all other config changes are applied and verified:
python scripts/ops/approve_live_gate.py \          # [PLANNED — not yet implemented]
  --stage    STAGE_1_ONE_SHARE \
  --capital  50000 \
  --tag      $(git rev-parse --short HEAD) \
  --by       "hari.mosoju@gmail.com" \
  --rollback-confirmed \
  --sns-tested

# Which writes:
#   Table:  quantembrace-prod-risk-state
#   PK:     LIVE_GATE#APPROVAL
#   SK:     CURRENT
#   Fields: approval_token, live_stage, max_capital, approved_by, approved_at,
#           release_tag, rollback_plan_confirmed=True, sns_alert_tested=True
```

**Until this record is written, LiveGateChecker will return BLOCKED (check 3).**

---

## 2. Allowed Strategy

| Field | Value |
|-------|-------|
| Strategy name (DynamoDB key) | `nse_vwap_reversion` |
| Strategy class | `VWAPReversionStrategy` |
| Candle interval | 1-minute |
| Market phase | `NORMAL` only (09:30–14:45 IST) |
| Direction | BUY and SELL (mean-reversion) |
| Default paper flag | `paper_trade=True` (must be flipped to `False` by operator) |
| Why this strategy | Produced the first paper fill on Day 7 (JINDALSAW BUY qty=429 @ ₹233.03). Candle-based, low signal frequency, well-tested. |
| Signal frequency | 1–5 signals per day (candle-triggered, cooldown 5 bars between same-symbol signals) |
| Stage-1 signal cap | `max_signals_per_day=2` |

**No other strategy is permitted to run live at Stage-1.** All other strategies keep `paper_trade=True`:
- `orb_15m` — paper only
- `scalp_1m` — paper only
- `intraday_trend_15m` — paper only
- `preclose_momentum` — paper only
- `momentum_v2` — paper only
- `us_momentum_v1` — paper only (US market, not active during IST)

---

## 3. Allowed Symbol List

| Symbol | Name | Sector | Price (approx) | 1-share value | Reason |
|--------|------|--------|----------------|---------------|--------|
| **HDFCBANK** (primary) | HDFC Bank Ltd | Banking | ~₹1,600 | ~₹1,600 | Liquid, VWAP-friendly, consistently in VWAP band at open |
| ICICIBANK (secondary) | ICICI Bank Ltd | Banking | ~₹1,200 | ~₹1,200 | Alternative if HDFCBANK has no signal |

**Why HDFCBANK as primary:**
- ₹1,600/share × 1 share = ₹1,600 — well within ₹2,000 order cap
- One of the most liquid NSE stocks (avg volume >1M shares/day)
- VWAP reversion works well on high-liquidity single names
- Already in paper watchlist from Day 7 sessions

**Why limit to 2 symbols:**
Stage-1 is a validation exercise, not a trading exercise. More symbols increase the probability of a fill, increasing the operational burden and risk of the first live session. Two symbols provides a backup if HDFCBANK has no signal on a given day.

**Config update for `STRATEGY_WATCHLIST_NSE`:**
```bash
STRATEGY_WATCHLIST_NSE=HDFCBANK,ICICIBANK
```

**`instruments.yaml` does not need to change** — the watchlist variable is the runtime filter.
The full instrument universe in the YAML is used for PAPER sessions. Live filtering is
enforced by `STRATEGY_WATCHLIST_NSE` and by the strategy's own symbol whitelist in DynamoDB.

---

## 4. Max Quantity

| Stage | Mechanism | Value |
|-------|-----------|-------|
| Stage-1 | `max_single_order_value=₹2,000` cap | Effectively 1 share of HDFCBANK (~₹1,600) or ICICIBANK (~₹1,200) |
| Enforcement layer 1 | `SignalAgeValidator` → `ExposureValidator` (max_position_size_pct=4%) | Max position = 4% × ₹50k = ₹2,000 |
| Enforcement layer 2 | `PositionValidator` (max_position_per_symbol=1) | Never more than 1 share of any symbol |
| Enforcement layer 3 | `SlippageValidator` → `UniverseOrderValidator` | Final hard gate at execution |

The strategy's signal `quantity` field is set by its internal risk sizing (based on nav and confidence). Even if the strategy sizes for 2 shares, the risk engine validators will cap the notional to ₹2,000, reducing quantity to 1 share.

---

## 5. Max Order Value

```
₹2,000 per order
```

Rationale:
- HDFCBANK ≈ ₹1,600/share → 1 share = ₹1,600 → fits within cap with ₹400 headroom
- ICICIBANK ≈ ₹1,200/share → 1 share = ₹1,200 → fits with ₹800 headroom
- If either stock rises to ₹2,001, the order is rejected by `ExposureValidator` — this is correct behaviour for Stage-1
- Set in `risk_limits_production.yaml` as `max_single_order_value: 2000`

---

## 6. Max Capital

```
₹50,000 total portfolio value (₹50k)
```

| Control | Value | Enforcement |
|---------|-------|-------------|
| `portfolio_value` (risk_limits_production.yaml) | ₹50,000 | RiskLimits NAV denominator |
| `max_capital` in LiveGate approval record | ₹50,000 | LiveGateChecker check 5 |
| `PAPER_SEED_NAV` (paper mode only) | ₹10,00,000 | Irrelevant for live — paper only |
| Stage-1 hard ceiling (LiveGateChecker) | ₹10,00,000 | check 5: blocks if > ₹10L |

**₹1,000,000 is explicitly BLOCKED at Stage-1.** The LiveGateChecker enforces this at check 5:
- `portfolio_value > 1,000,000` → BLOCKED
- `approval_record.max_capital > 1,000,000` → BLOCKED

Setting portfolio_value=₹50,000 gives a comfortable margin below the ₹10L ceiling.

---

## 7. Max Daily Loss

```
₹250 absolute  (0.5% of ₹50,000)
```

| Config | Value |
|--------|-------|
| `max_daily_loss_pct` | `0.5%` |
| Portfolio value | ₹50,000 |
| Absolute daily loss limit | ₹250 |
| Kill switch auto-trigger threshold | `kill_switch_daily_loss_pct: 3.0` (₹1,500) |

At Stage-1 volume (1-2 trades/day, 1 share per trade), the worst single-day outcome is:
- 1 HDFCBANK share moves 5% against position: ₹80 loss
- 2 trades, both go wrong: ₹160 loss
- Daily loss cap (₹250) is almost never reached from position P&L alone

The kill switch fires at ₹1,500 loss (3% of ₹50k) — this would require an extraordinary move (>9% single-day) on a single share.

---

## 8. Max Trades

```
max_signals_per_day = 2  (in DynamoDB strategy-config)
max_open_orders     = 1  (in risk_limits_production.yaml)
max_concurrent_positions = 1
```

Sequence for a 2-trade day:
1. Signal 1 → BUY 1 share HDFCBANK → position opens
2. TEE detects stop-loss or take-profit → EXIT (flat)
3. Signal 2 → BUY 1 share HDFCBANK → second position (if within daily cap)
4. Position 2 exits → flat at end of day

`max_signals_per_day=2` at the strategy level prevents runaway signal generation if the VWAP bands are frequently crossed. Even if 10 VWAP band crossings occur, only 2 will generate signals.

`max_open_orders=1` means the risk engine will reject a second signal while the first order is in-flight (PENDING/PLACED state). This prevents concurrent position accumulation.

---

## 9. Manual Approval Variable / Token

### LiveGate Approval DynamoDB Record

```
Table:  quantembrace-prod-risk-state
PK:     LIVE_GATE#APPROVAL
SK:     CURRENT
```

Required fields:

| Field | Stage-1 Value | Purpose |
|-------|--------------|---------|
| `approval_token` | `"stage1-2026-06-02-op1"` | Non-empty token proves intentional write |
| `live_stage` | `"STAGE_1_ONE_SHARE"` | Checked by LiveGateChecker check 4 |
| `max_capital` | `50000` (N) | Checked by LiveGateChecker check 5; must be ≤ ₹10L |
| `approved_by` | `"hari.mosoju@gmail.com"` | Human identity of approver |
| `approved_at` | ISO timestamp at time of approval | Time-stamps the approval |
| `release_tag` | git SHA of deployed image | Links approval to code version |
| `rollback_plan_confirmed` | `true` (BOOL) | Operator confirms rollback procedure reviewed |
| `sns_alert_tested` | `true` (BOOL) | SNS test delivery confirmed |

**Manual write command (after `approve_live_gate.py` is implemented):**
```bash
python scripts/ops/approve_live_gate.py \
  --stage    STAGE_1_ONE_SHARE \
  --capital  50000 \
  --tag      $(git rev-parse --short HEAD) \
  --by       "hari.mosoju@gmail.com" \
  --rollback-confirmed \
  --sns-tested \
  --token    "stage1-$(date +%Y-%m-%d)-op1"
```

**Interim (until approve_live_gate.py is built):**
```bash
aws dynamodb put-item \
  --table-name quantembrace-prod-risk-state \
  --item '{
    "PK":                      {"S": "LIVE_GATE#APPROVAL"},
    "SK":                      {"S": "CURRENT"},
    "approval_token":          {"S": "stage1-2026-06-02-op1"},
    "live_stage":              {"S": "STAGE_1_ONE_SHARE"},
    "max_capital":             {"N": "50000"},
    "approved_by":             {"S": "hari.mosoju@gmail.com"},
    "approved_at":             {"S": "2026-06-02T04:00:00+00:00"},
    "release_tag":             {"S": "REPLACE_WITH_GIT_SHA"},
    "rollback_plan_confirmed": {"BOOL": true},
    "sns_alert_tested":        {"BOOL": true}
  }' \
  --region ap-south-1
```

**Do not write this record until all other Stage-1 config changes are applied.**

---

## 10. Kill Switch Command

### Activate (emergency halt)

```bash
# Immediate halt — use if any unexpected order or position occurs
python scripts/kill_switch_cli.py activate \
  --reason "Stage-1 live validation halt — unexpected behaviour"

# Or via CLI with explicit reason:
python scripts/kill_switch_cli.py activate --reason "<specific reason>"
# Prompts: "Are you sure? [yes/no]:" → type 'yes'
```

### Verify status before session

```bash
python scripts/kill_switch_cli.py status
# Expected output:
# ✅ KILL SWITCH — TRADING IS ACTIVE (inactive)
# Kill switch is inactive. Trading is permitted.
```

### Deactivate (after resolution)

```bash
python scripts/kill_switch_cli.py deactivate
# Prompts: type "I confirm trading should resume" exactly
# Only run after root cause of activation is understood and resolved
```

### Automatic triggers (already configured)

Kill switch fires automatically if:
- Daily P&L loss > ₹1,500 (3% of ₹50k) — `daily_pnl_loss_halt` CloudWatch alarm
- WebSocket disconnected > configured gap — `websocket-disconnected` alarm → EventBridge → Lambda (if wired)
- data_ingestion producer heartbeat stale > 60s — `_monitor_producer_heartbeat()` in risk_engine
- Single strategy loss > threshold — `KillSwitchMonitor._monitor_strategy_loss()`
- MIS positions not closed by 15:10 IST — `MISSquareOffManager._await_fills_or_escalate()`

---

## 11. Rollback Config

### When to rollback

- Any unexpected live position opened in a non-whitelisted symbol
- Any order with quantity > 1 share
- Any order with value > ₹2,000
- Any DynamoDB / Kafka error during live session
- Kill switch self-fires without known cause
- Reconciliation flag becomes True

### Rollback sequence

```bash
# Step 1 — Activate kill switch (stops new entries immediately)
python scripts/kill_switch_cli.py activate --reason "Stage-1 rollback initiated"

# Step 2 — Disable live trading in execution_engine env file
# Re-comment out QE_EXECUTION_LIVE_TRADING_ENABLED in execution_engine.sh userdata
# Then re-run terraform apply + ASG instance refresh

# Step 3 — Flip strategy back to paper in DynamoDB
python scripts/strategy/config.py paper nse_vwap_reversion --env production
# Sets paper_trade=True for nse_vwap_reversion

# Step 4 — Change risk profile back
# In risk_engine.sh userdata: RISK_PROFILE=paper
# Re-run terraform apply + risk_engine ASG instance refresh

# Step 5 — Run reconciliation (verify all positions flat)
python scripts/ops/reconcile.py --environment prod --status

# Step 6 — Review session report
python scripts/monitoring/paper_session_report.py --date $(date +%Y-%m-%d)

# Step 7 — Deactivate kill switch (only after Step 5 confirms positions flat)
python scripts/kill_switch_cli.py deactivate
```

### Config restore (rollback to paper mode)

| Config | Paper (rollback target) | Stage-1 Live |
|--------|------------------------|--------------|
| `RISK_PROFILE` | `paper` | `tiny-live` |
| `QE_EXECUTION_LIVE_TRADING_ENABLED` | absent/false | `true` |
| `nse_vwap_reversion.paper_trade` | `True` | `False` |
| `portfolio_value` | `₹10,00,000` (paper NAV) | `₹50,000` |
| `max_single_order_value` | `₹5,000` | `₹2,000` |

---

## 12. Monitoring Command

### During Stage-1 live session

```bash
# Terminal 1 — live counters monitor (refresh every 30s)
python scripts/monitoring/paper_trading_monitor.py \
  --counters /tmp/qe_live_counters.json \
  --watch 30 \
  --trading-mode STAGE_1_LIVE

# Terminal 2 — live execution logs (execution_engine on EC2 via SSM)
aws ssm start-session \
  --target $(aws autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names quantembrace-prod-execution-engine-asg \
    --query 'AutoScalingGroups[0].Instances[0].InstanceId' \
    --output text) \
  --region ap-south-1
# Then in session: sudo journalctl -u quantembrace-execution_engine -f

# Terminal 3 — kill switch status (run manually if suspicious)
python scripts/kill_switch_cli.py status

# Terminal 4 — risk engine logs (via SSM, separate from execution_engine)
# Follow same SSM pattern above for quantembrace-prod-risk-engine-asg
```

### Pre-session LiveGateChecker

```bash
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
    print(f'  [{c.status:4}] {c.name}')
import sys; sys.exit(0 if result.status == 'APPROVED' else 1)
" && echo "GATE: APPROVED — proceed to live" || echo "GATE: NOT APPROVED — do not enable live"
```

### Session close report

```bash
python scripts/monitoring/paper_session_report.py \
  --date $(date +%Y-%m-%d) \
  --mode live
# Review: fills, P&L, fill prices vs signal prices, slippage, any anomalies
```

### CloudWatch dashboard

AWS Console → CloudWatch → Dashboards → `quantembrace-prod-overview`
Watch sections:
- Kill switch activations (must stay 0)
- Order rejection rate (should be low; high rate = risk limits firing correctly or watchlist mismatch)
- Daily P&L (absolute; should be tiny positive or tiny negative)
- Zerodha fill detection latency (should be < 1s P95)

---

## Stage-1 Promotion Sequence (Full Order)

```
DO NOT EXECUTE ANYTHING — this is the planned sequence only.

Phase 1 — Infrastructure (already done)
  [✅] INFRA-1: ha_nat = true in prod/main.tf
  [✅] INFRA-2: sessions table added to dynamodb/main.tf
  [✅] INFRA-3: check_asg_health.py created
  [ ] terraform apply -target=module.dynamodb  (create sessions table + GSI backfill)
  [ ] ASG instance refresh for all services

Phase 2 — Config changes (this document)
  [ ] Update risk_limits_production.yaml (portfolio_value=50000, limits above)
  [ ] Update risk_engine.sh:  RISK_PROFILE=tiny-live
  [ ] Update execution_engine.sh: STRATEGY_WATCHLIST_NSE=HDFCBANK,ICICIBANK
  [ ] terraform apply (picks up userdata changes; triggers new launch template version)
  [ ] ASG instance refresh (risk_engine, then execution_engine)

Phase 3 — Paper session gate (5 sessions with new config, risk_profile still=paper)
  [ ] Run 5 paper sessions using tiny-live risk limits (paper_trade=True everywhere)
  [ ] Each session: python scripts/monitoring/paper_session_report.py
  [ ] Gate criteria: ≥1 fill/session, no unmanaged positions, no kill switch self-fires

Phase 4 — Strategy flip + approval
  [ ] python scripts/strategy/config.py go-live nse_vwap_reversion --env production
  [ ] Write LiveGate approval record (aws dynamodb put-item, see §9)
  [ ] Run LiveGateChecker — must return APPROVED

Phase 5 — Live gate activation (live_trading_enabled=true)
  [ ] Change execution_engine.sh:  # uncomment QE_EXECUTION_LIVE_TRADING_ENABLED=true
  [ ] terraform apply + ASG instance refresh (execution_engine only)
  [ ] Run LiveGateChecker again — confirm APPROVED with live gate enabled
  [ ] Monitor first 30 minutes with paper_trading_monitor.py --watch 30

Phase 6 — Stage-1 result
  [ ] Review session report at close
  [ ] Confirm: 1 fill, correct symbol, correct qty (1 share), P&L within ±₹200
  [ ] Record result in memory/decisions.md as Stage-1 outcome
```

---

## What Is NOT Changed

The following remain unchanged at Stage-1:

| Item | Current Value | Reason |
|------|--------------|--------|
| `UNIVERSE_MODE` | `PAPER_SAFE_START` | NIFTY 50 universe is correct for Stage-1 |
| `RISK_MAX_SIGNAL_AGE_SECONDS` | `30` | Already correct; do not reduce |
| All non-whitelisted strategies | `paper_trade=True` | Must remain paper throughout Stage-1 |
| Paper isolation code paths | unchanged | `paper_trade=True` strategies use PaperSimulator only |
| Kill switch thresholds | unchanged | Conservative thresholds are already appropriate |
| DynamoDB PITR | unchanged | Required; do not disable |
| `infra/deployment/deploy.sh` | stub (error) | Replaced by GitHub Actions deploy.yml |

---

*Config pack prepared 2026-05-30. No config has been applied. No deployment has occurred. No live trading has been enabled.*
