# QuantEmbrace — Daily Operations Guide

> **Who is this for?** The person responsible for starting and monitoring QuantEmbrace on a live trading day. You don't need to understand the code in detail, but you do need to follow these procedures exactly. Every step here exists because something went wrong before if it was skipped.

---

## Table of Contents

1. [Monday Morning — Full Startup](#monday-morning--full-startup)
2. [Tuesday–Friday — Daily Startup](#tuesdayfriday--daily-startup)
3. [Pre-Market Checklist (08:30–09:15 IST)](#pre-market-checklist-08300915-ist)
4. [Market Open (09:15–15:30 IST)](#market-open-09151530-ist)
5. [Intraday Monitoring](#intraday-monitoring)
6. [Pre-Close Procedures (15:00–15:30 IST)](#pre-close-procedures-15001530-ist)
7. [Post-Market Procedures (After 15:30 IST)](#post-market-procedures-after-1530-ist)
8. [US Market Session (19:00–02:30 IST)](#us-market-session-19000230-ist)
9. [Weekly Tasks (Every Friday)](#weekly-tasks-every-friday)
10. [Emergency Procedures](#emergency-procedures)
11. [Interpreting Alerts](#interpreting-alerts)
12. [Quick Reference — Make Commands](#quick-reference--make-commands)

---

## Monday Morning — Full Startup

Monday requires the most steps because infrastructure may have been idle since Friday close.

### 08:00 IST — Infrastructure Start

```bash
# 1. Start all local dependencies (Docker must be running first)
make infra-up

# 2. Wait for the green confirmation:
#    ✅ LocalStack (S3/DynamoDB) is healthy
#    ✅ Redpanda (Kafka) is healthy
#    (This takes ~30 seconds)
```

**If Docker isn't running:** Open Docker Desktop and wait for it to show "Running" before proceeding.

### 08:15 IST — Zerodha Token Refresh

Zerodha's API token expires every night. You must get a fresh one before market opens.

```bash
make zerodha-login
```

This opens a browser window. Follow the steps:
1. Log in to Zerodha with your credentials
2. Complete 2FA (TOTP/PIN)
3. You'll see a success page — the token is saved automatically
4. Terminal shows: `✅ Zerodha token saved to .env`

**If the browser doesn't open:** Run `python scripts/zerodha_login.py` directly.

### 08:30 IST — Start All Services

```bash
make monday
```

This single command:
1. Checks that `.env` exists with required credentials
2. Verifies Docker is running
3. Starts LocalStack (S3 + DynamoDB simulator) and Redpanda (Kafka)
4. Waits for both to be healthy (polls up to 90 seconds)
5. Creates DynamoDB tables and Kafka topics (idempotent — safe to run again)
6. Starts all 5 services: data_ingestion, strategy_engine, risk_engine, ai_engine, execution_engine
7. Waits 20 seconds for services to initialize
8. Runs a health check on all services
9. Displays the current kill switch status

**Expected output (all green):**
```
✅ data_ingestion  — healthy (port 8081)
✅ strategy_engine — healthy (port 8082)
✅ risk_engine     — healthy (port 8083)
✅ execution_engine— healthy (port 8084)
✅ ai_engine       — healthy (port 8085)
✅ localstack      — healthy (port 4566)

Kill switch: OFF — trading is ACTIVE
```

**If any service shows ❌:** Run `make logs-<service>` (e.g., `make logs-risk`) to see the error. See [Emergency Procedures](#emergency-procedures).

### 08:45 IST — Run Pre-Market Checklist

```bash
make checklist
```

See [Pre-Market Checklist](#pre-market-checklist-08300915-ist) for what each item means.

---

## Tuesday–Friday — Daily Startup

Faster than Monday because infrastructure is already configured.

```bash
# Step 1: Refresh Zerodha token (required every day)
make zerodha-login

# Step 2: Start services
make start

# Step 3: Verify health
make health

# Step 4: Pre-market checklist
make checklist
```

If services were never stopped (i.e., the machine was just left running), skip Step 2 and go straight to Step 3 to verify they're still healthy.

---

## Pre-Market Checklist (08:30–09:15 IST)

Run `make checklist` and verify every item is green. Here is what each check means:

| Check | What It Verifies | Action If Red |
|---|---|---|
| **Zerodha token** | Token in `.env` is less than 24 hours old | Run `make zerodha-login` |
| **Kill switch** | Kill switch is OFF (trading enabled) | Run `make kill-switch-status` to investigate |
| **Service health** | All 5 services respond on `/health` | Check `make logs-<service>` |
| **Kafka topics** | All 6 topics exist and have correct partition counts | Run `make infra-setup` to recreate |
| **DynamoDB tables** | All 10 required tables exist | Run `make infra-setup` |
| **Open orders** | No orders stuck in PENDING state from a prior crash | Investigate via `make paper-orders` |
| **Kafka consumer lag** | No consumer group is falling behind (lag > 1000) | Restart the lagging service |
| **Zerodha connectivity** | Can reach Zerodha API and WebSocket | Check network; re-run zerodha-login |

### Verifying Kill Switch Before Open

The kill switch is the most important check. If it is ON, no trades will execute.

```bash
make kill-switch-status
```

Expected output when clear to trade:
```
Kill switch status: OFF
Trading is ACTIVE — signals will be processed normally
```

If kill switch is ON due to yesterday's drawdown limit, it resets automatically at midnight. If it's still ON in the morning, it means either:
- The system reset hasn't happened yet (wait until 08:00 IST)
- Someone manually activated it — investigate before turning it off

To turn it off manually (only after you understand why it was on):
```bash
make kill-switch-off
# You will be asked to type YES to confirm
```

---

## Market Open (09:15–15:30 IST)

At 09:15 IST, NSE opens and trading begins automatically. You do not need to do anything — the system starts processing ticks and generating signals immediately.

### First 15 Minutes (09:15–09:30 IST) — Watch Closely

The first 15 minutes are the most volatile. The ORB (Opening Range Breakout) strategy uses these minutes to establish reference prices, so signals in this window may be more frequent.

```bash
# Watch signals in real time (new terminal tab)
make watch-signals

# Watch orders in real time (new terminal tab)
make watch-orders
```

**What you should see:**
- `watch-signals`: Lines appearing with `PENDING → ENRICHED → APPROVED` or `PENDING → REJECTED`
- `watch-orders`: Lines appearing with `PLACED → FILLED` within 1–2 seconds

**What is abnormal:**
- Signals staying in `PENDING` for more than 30 seconds → AI engine may be down
- All signals being `REJECTED` → Check risk limits (daily loss may be at limit, or kill switch)
- No signals at all after 09:30 IST → Check `make logs-strategy`

---

## Intraday Monitoring

You don't need to watch screens constantly during the day. Check in every 30–60 minutes.

### The 3-Minute Intraday Check

```bash
# Single command — shows everything important at once
make health
```

Look for:
1. All 5 services still green
2. No `⚠️ HIGH KAFKA LAG` warning
3. Kill switch still OFF

### Checking Current Positions and P&L

```bash
make paper-orders
```

This shows all open and filled paper orders for today. The columns are:
```
signal_id | symbol | direction | qty | status | avg_fill | pnl_est
```

- `pnl_est` is estimated P&L based on current price vs fill price
- `status = FILLED` means the order completed
- `status = PLACED` means the order is still open at the broker (waiting for fill)
- `status = FAILED` requires investigation — run `make logs-execution`

### Kafka Consumer Lag

If a service is processing slowly, signals pile up unread in Kafka topics. Check with:

```bash
make kafka-topics
```

Normal lag values:
- `signals.pending` → risk consumer: lag < 100
- `signals.enriched` → risk consumer: lag < 100
- `signals.approved` → execution consumer: lag < 50
- `ticks.nse` → strategy consumer: lag < 500 (ticks come fast)

If any lag exceeds 1000 and is growing, restart the relevant service:

```bash
make restart-risk      # If risk consumer is lagging
make restart-strategy  # If strategy consumer is lagging
make restart-execution # If execution consumer is lagging
```

---

## Pre-Close Procedures (15:00–15:30 IST)

### 15:00 IST — Pre-Close Warning

The PreCloseMomentum strategy generates signals in the last 30 minutes of NSE trading. This is normal — these are legitimate strategy signals.

### 15:10 IST — Intraday Position Closure

The system automatically starts closing all open MIS (intraday) positions at 15:10 IST, before Zerodha's auto-square-off at 15:15 IST.

**Monitor this actively:**

```bash
make watch-orders
```

You should see `SELL` orders for all open positions, each reaching `FILLED` status.

**If any positions are not closed by 15:13 IST:**

```bash
# Check what's still open
make paper-orders

# If an order is stuck in PLACED, check the execution logs
make logs-execution
```

Zerodha will force-close any remaining MIS positions at 15:15 IST. This is a safety net, not a plan — a forced square-off may fill at a worse price.

### 15:30 IST — Market Close

At 15:30 IST NSE closes. The strategy engine stops generating NSE signals. No action needed.

---

## Post-Market Procedures (After 15:30 IST)

### Mandatory Post-Market Checks

Run these after market close, ideally by 16:30 IST:

```bash
# 1. Verify no orders are stuck in non-terminal state
make paper-orders
# Every order should be FILLED, REJECTED, or CANCELLED
# PENDING or PLACED after 15:30 IST indicates a problem

# 2. Check today's P&L summary
make logs-risk | grep "daily_pnl"
# Look for lines like: daily_pnl=8250 limit=20000 pct_used=41.2

# 3. Check for any errors in the trading path
make logs-execution | grep "ERROR\|FAILED"
make logs-risk | grep "ERROR"
```

### Reconciling With the Broker

After any trading session, reconcile your DynamoDB position records against the actual broker:

In paper mode, reconciliation just verifies the paper simulator's records are consistent — there are no real broker positions to reconcile.

When you go live, add:

```bash
# Check if DynamoDB positions match broker positions
python scripts/ops/reconcile_positions.py
```

### Archiving Logs

Daily logs are automatically shipped to S3 by the services. You can verify with:

```bash
aws s3 ls s3://${S3_BUCKET_LOGS}/execution/$(date +%Y/%m/%d)/
aws s3 ls s3://${S3_BUCKET_LOGS}/risk/$(date +%Y/%m/%d)/
```

### Stopping Services (Optional)

If you want to stop everything after NSE close and before the US session:

```bash
make stop
```

Restart before the US session with `make start`.

---

## US Market Session (19:00–02:30 IST)

US markets open at 09:30 ET = 19:00 IST (EDT) or 20:00 IST (EST, Nov–Mar).

The system handles both sessions automatically — data_ingestion has an Alpaca WebSocket open alongside the Zerodha connection. When Alpaca sends ticks, the strategy engine generates US market signals.

### Before US Open

```bash
# Verify services are running (if stopped after NSE close, restart them)
make health

# Alpaca paper trading uses a separate endpoint — verify it's configured
grep ALPACA_BASE_URL .env
# Should show: ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

### During US Session

Same intraday monitoring as NSE applies. US fills appear in `make watch-orders` alongside NSE fills.

Key difference: **Alpaca does not auto-square-off positions.** Overnight positions from US strategies are intentional. The `IntradayTrend15m` and `PreCloseMomentum` strategies are NSE-only; the US strategies (`ORB`, `Scalp1m`, `VWAPReversionStrategy`) can hold positions across the US session.

### After US Close (02:30 IST)

```bash
# Stop services if you want to conserve resources overnight
make stop
```

---

## Weekly Tasks (Every Friday)

### 1. Review the Week's Signals and Fills

```bash
# Download this week's risk audit log from S3
aws s3 sync s3://${S3_BUCKET_LOGS}/risk/ /tmp/risk_logs/ \
    --exclude "*" --include "$(date +%Y/%m)/*"

# View signal approval rate per strategy
grep "risk_decision=APPROVED\|risk_decision=REJECTED" /tmp/risk_logs/*/*.json | \
    python scripts/ops/signal_report.py
```

### 2. Backtest Any Strategy Parameter Changes

Before adjusting `configs/risk_limits_production.yaml` or strategy parameters:

```bash
python scripts/backtest/run_backtest.py \
    --strategy momentum \
    --config configs/backtest_momentum.yaml \
    --from 2025-01-01 \
    --to $(date +%Y-%m-%d)
```

### 3. Review CloudWatch Cost and Alarms

```bash
aws cloudwatch describe-alarm-history \
    --start-date $(date -d '7 days ago' +%Y-%m-%dT00:00:00) \
    --end-date $(date +%Y-%m-%dT23:59:59)
```

Look for any alarms that fired (ALARM state) during the week and investigate.

### 4. Rotate Zerodha Token Record in AWS Secrets Manager

Zerodha tokens are refreshed daily but the Secrets Manager entry should be audited weekly to verify it has today's token (not an old one from a failed refresh).

---

## Emergency Procedures

### Kill Switch — Immediate Trading Halt

Use this if something is wrong and you need trading to stop immediately:

```bash
make kill-switch-on
# You will be asked to provide a reason (required)
```

This immediately:
1. Activates the kill switch flag in DynamoDB
2. All services check the kill switch before processing signals
3. Signals already approved but not yet executed are cancelled
4. Open positions are held (not automatically closed)

**To re-enable trading:**
```bash
make kill-switch-off
# You will be asked to type YES to confirm
```

**Do not turn the kill switch off until you understand and have resolved the issue that caused you to activate it.**

---

### Service Is Down — One Service Failed

```bash
# Check which service is down
make health

# Read the logs to find the error
make logs-risk          # or logs-strategy, logs-execution, logs-data, logs-ai

# Restart just that service
make restart-risk       # or restart-strategy, restart-execution
```

**During market hours, if the risk engine is down:** Trading automatically halts. Signals from the strategy engine accumulate in Kafka topic `signals.pending` and will be processed when the risk engine restarts (within the signal age limit of 30 seconds — older signals are discarded).

**During market hours, if the execution engine is down:** Approved signals accumulate in `signals.approved`. They replay on restart, but signals older than 30 seconds will be rejected by the age validator — this is correct behavior (stale signals should not be executed).

**During market hours, if the AI engine is down:** The EnrichmentWatchdog detects the lag and the risk engine reads from `signals.pending` directly, using conservative defaults for quality score and market regime. Trading continues safely.

---

### Kafka Is Down — No Messages Flowing

If `make kafka-topics` shows errors or services report "broker not available":

```bash
# Restart Redpanda (local dev)
docker compose restart redpanda

# Wait 15 seconds, then check
sleep 15
make kafka-topics

# Restart all services to re-establish Kafka connections
make restart
```

Services use exponential backoff when Kafka is unavailable. They reconnect automatically when Kafka comes back.

---

### DynamoDB Is Down — State Unavailable

In local development, DynamoDB is LocalStack. If it goes down:

```bash
docker compose restart localstack
sleep 20

# Recreate tables (idempotent — won't overwrite existing data in the LocalStack volume)
make infra-setup
```

**Critical:** If DynamoDB goes down during live trading, services enter a degraded state:
- The execution engine cannot write order state → it halts new orders (safe fail)
- The risk engine cannot read position counts → it halts approvals (safe fail)

This is correct behavior — when the state store is unavailable, we halt rather than trade blindly.

---

### All Open Positions Not Closing Before 15:10 IST

If the strategy engine fails to send exit signals for intraday positions before 15:10 IST:

1. Zerodha will auto-square-off at 15:15 IST — your positions are not at risk of being open overnight
2. Check the execution logs: `make logs-execution | grep "square_off"`
3. For paper trading this is just a simulation discrepancy — no real money at stake

---

### Daily Loss Limit Hit — Kill Switch Auto-Activated

When daily losses exceed `max_daily_loss_pct` (default: 2% of portfolio = ₹20,000 on ₹10 lakh):

1. Kill switch activates automatically
2. All pending/enriched/approved signals are dropped
3. Open positions are held (not force-closed — you hold them into close)

The kill switch resets at midnight for the next trading day.

**To check today's loss:**
```bash
make logs-risk | grep "daily_pnl" | tail -5
```

---

## Interpreting Alerts

### CloudWatch Alerts

| Alert Name | Meaning | Action |
|---|---|---|
| `HighKafkaConsumerLag` | A consumer is more than 1000 messages behind | Restart the lagging service |
| `SignalRejectionRateHigh` | >80% of signals rejected by risk engine | Check risk logs — limit may be reached |
| `BrokerAPIError` | Broker API calls failing | Check Zerodha/Alpaca status pages; check credentials |
| `KillSwitchActivated` | Kill switch turned on (manually or auto) | Investigate the reason before re-enabling |
| `DailyLossLimitApproached` | P&L loss > 80% of daily limit | Review open positions; consider reducing exposure |
| `DataFeedStale` | No ticks received for >60 seconds during market hours | WebSocket may have dropped; restart data_ingestion |
| `ExecutionLatencyHigh` | Order placement p99 > 2 seconds | Broker API may be slow; check for rate limiting |

### What "REJECTED" Signals Mean

A REJECTED signal is not an error — it means the risk engine did its job. Check why:

```bash
make logs-risk | grep "REJECTED" | tail -20
```

Common rejection reasons:
- `signal_age_exceeded` — signal took too long to reach risk engine (AI engine was slow)
- `kill_switch_active` — kill switch is on
- `daily_loss_limit_reached` — you've already lost the maximum for today
- `position_limit_reached` — already have the maximum number of open positions
- `quality_score_too_low` — AI engine rated this signal as low confidence
- `spread_too_wide` — bid-ask spread is too large (illiquid conditions)

---

## Quick Reference — Make Commands

| Command | When to Use |
|---|---|
| `make monday` | Monday morning full startup |
| `make start` | Tue–Fri startup |
| `make stop` | Shutdown all services |
| `make restart` | Restart all services |
| `make restart-strategy` | Restart only strategy_engine |
| `make restart-risk` | Restart only risk_engine |
| `make restart-execution` | Restart only execution_engine |
| `make health` | Check if all services are up |
| `make checklist` | Pre-market readiness check |
| `make zerodha-login` | Daily Zerodha token refresh |
| `make logs` | Stream all service logs |
| `make logs-risk` | Risk engine logs only |
| `make logs-execution` | Execution engine logs only |
| `make watch-signals` | Real-time signal flow |
| `make watch-orders` | Real-time order flow |
| `make paper-orders` | Today's paper order summary |
| `make kafka-topics` | Kafka topic and consumer lag status |
| `make kill-switch-status` | Is trading halted? |
| `make kill-switch-on` | **Emergency: halt all trading immediately** |
| `make kill-switch-off` | Re-enable trading after investigation |
| `make backtest` | Run a strategy backtest |

---

*Last updated: 2026-05-15 | Update this runbook when: new services are added, market hours change, new broker integrations go live, or emergency procedures change.*
