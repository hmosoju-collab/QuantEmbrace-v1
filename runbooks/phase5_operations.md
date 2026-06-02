# Phase 5 Operations Runbook

> **What is Phase 5?** The system has passed all pre-go-live checks and is running in paper trading mode with real market data. Phase 5 is the validation phase: the full production pipeline runs on real ticks, real broker data, and real market conditions — but orders are simulated, not sent to a real broker. Capital is at zero risk. The goal is to validate signal quality, risk behaviour, and system stability before committing real capital.

---

## Pre-Paper-Trading Gate

This runbook is the minimum operator surface before paper trading starts. Any failed check keeps live trading disabled. Run `make checklist` to execute all checks at once.

### Required Checks (All Must Pass)

| Check | Pass Condition | How to Verify |
|---|---|---|
| All 5 services healthy | `/health` on ports 8081–8085 returns `{"status": "ok"}` | `make health` |
| Kill switch clear | DynamoDB `kill-switch` table has `active = false` | `make kill-switch-status` |
| Kafka topics exist | All 6 topics present with correct partition count | `make kafka-topics` |
| DynamoDB tables exist | All 10 tables created in the target environment | `make infra-setup` (idempotent) |
| Zerodha token valid | Token in `.env` is < 24h old | `make checklist` → `zerodha_token` row |
| Consumer lag near zero | No consumer group > 1000 messages behind | `make kafka-topics` |
| Candle cache fresh | strategy_engine has `minute`, `5minute`, `15minute` bars | `make logs-strategy \| grep candle_cache` |
| Paper mode confirmed | `EXECUTION_PAPER_MODE=true` in environment | `grep EXECUTION_PAPER_MODE .env` |
| Positions reconciled | DynamoDB positions table matches paper simulator state | `make paper-orders` shows no unexpected open positions |

Failure action: keep strategies in paper mode and do not promote capital until all checks pass.

---

## Dashboards

Use the CloudWatch dashboard `${project}-${environment}-overview` (e.g., `quantembrace-staging-overview`).

### Key Panels

- **Order Lifecycle:** `OrdersSubmitted`, `OrderPlacementErrors`, `OrderRejectionRatePct`, `PaperOrdersSimulated`
- **Kafka Lag by Consumer Group:** `ConsumerLag` for groups: `strategy-v1`, `aiengine-v1`, `risk-v1`, `risk-v1-fallback`, `execution-v1`
- **Data Feed Health:** `DataFeedStalenessSeconds`, `WebSocketGapSeconds`, candle-cache freshness
- **Position Drift:** `PositionDriftQuantity`, `PositionMismatchCount` (always zero in paper mode)
- **Kill Switch:** `KillSwitchActivations` and current DynamoDB state via CLI
- **P&L and NAV:** `DailyPnL`, `CumulativePnL`, NAV snapshots, daily loss threshold tracking
- **Broker Latency:** order placement p50/p99, fill detection latency, Zerodha 429/timeout rates
- **Kafka Retry and DLQ:** retry and DLQ message counts by source topic (`signals.pending.retry`, `signals.pending.dlq`)

For local development, use Grafana (Docker Compose includes it): `http://localhost:3000`

---

## CLI Checklists

Run these from the repository root:

```bash
# Full daily readiness check
python scripts/ops/checklist.py daily

# Market-open specific checks
python scripts/ops/checklist.py market-open

# Post-market checks
python scripts/ops/checklist.py post-market

# Weekly review
python scripts/ops/checklist.py weekly
```

JSON output for automation:

```bash
python scripts/ops/checklist.py market-open --json
```

Or use the Makefile shortcut:

```bash
make checklist
```

---

## Market-Open Gate

Before enabling any live strategy, verify every item in this list:

### Signal Path

- [ ] Kill switch is clear (`make kill-switch-status`)
- [ ] Kafka consumer lag is near zero for all groups (`make kafka-topics`)
- [ ] strategy_engine is generating signals (`make logs-strategy | grep "signal_published"`)
- [ ] ai_engine is processing and enriching signals (`make logs-ai | grep "enriched"`)
- [ ] risk_engine is approving/rejecting signals (`make logs-risk | grep "risk_decision"`)
- [ ] execution_engine is placing paper orders (`make logs-execution | grep "order_placed"`)

### Data Quality

- [ ] Candle cache contains fresh `minute`, `5minute`, and `15minute` bars
- [ ] Quote cache is fresh enough for spread/circuit checks (< 60s old)
- [ ] NSE tick feed (Zerodha WebSocket) is active
- [ ] US tick feed (Alpaca WebSocket) is active (if US session is in scope)

### State Consistency

- [ ] Broker positions and DynamoDB positions match
- [ ] Retry/DLQ topics are empty or explicitly triaged
- [ ] No orders stuck in `PENDING` state from a prior session

Failure action: keep strategies in paper/shadow mode and do not promote capital.

---

## Post-Market Gate

Before considering the day clean:

- [ ] Every order is in a terminal state (`FILLED`, `REJECTED`, or `CANCELLED`)
- [ ] No orders in `PENDING` or `PLACED` state after market close
- [ ] Partial-fill and cancel cases have correct final exposure recorded
- [ ] NAV and daily P&L are updated from all fills
- [ ] Position drift is zero (DynamoDB matches paper simulator)
- [ ] Any retry/DLQ message has an owner and a written resolution
- [ ] Daily P&L is within expected range (positive or negative, but not extreme)
- [ ] CloudWatch shows no unacknowledged alarms

```bash
# Quick post-market verification
make paper-orders
make logs-risk | grep "daily_pnl" | tail -1
make logs-execution | grep "ERROR" | wc -l   # Should be 0
```

---

## Kafka Architecture Reference

All inter-service communication uses Kafka MSK Serverless (production) or Redpanda (local dev).

### Topic Map

| Topic | Producer | Consumer(s) | Purpose |
|---|---|---|---|
| `ticks.nse` | data_ingestion | strategy_engine (strategy-v1) | Real-time NSE price ticks |
| `ticks.us` | data_ingestion | strategy_engine (strategy-v1) | Real-time US equity ticks |
| `signals.pending` | strategy_engine | ai_engine (aiengine-v1), risk_engine (risk-v1-fallback) | Raw strategy signals awaiting enrichment |
| `signals.enriched` | ai_engine | risk_engine (risk-v1) | Signals enriched with ML predictions |
| `signals.approved` | risk_engine | execution_engine (execution-v1), execution_engine (execution-v1-kill-switch) | Risk-validated signals ready for execution |
| `orders.events` | execution_engine | risk_engine (risk-v1-order-events) | Order fills and state changes for position tracking |

### EnrichmentWatchdog Fallback

When the AI engine lags (defined as: no message on `signals.enriched` for 30 seconds while `signals.pending` has unread messages), the risk_engine's `EnrichmentWatchdog` kicks in:

1. Risk engine switches to reading from `signals.pending` directly (consumer group `risk-v1-fallback`)
2. Signals are processed with a conservative default quality score of `0.30` (the minimum threshold)
3. Market regime defaults to `unknown`
4. All 11 validators still run normally — only the enrichment step is bypassed
5. CloudWatch metric `EnrichmentFallbackActivations` increments
6. When the AI engine catches up, the watchdog automatically switches back

This means **trading continues safely even when the AI engine is down**.

---

## Consumer Groups Reference

| Group ID | Service | Reads From | Purpose |
|---|---|---|---|
| `strategy-v1` | strategy_engine | ticks.nse, ticks.us | Run strategies on each tick |
| `aiengine-v1` | ai_engine | signals.pending | Enrich signals with ML predictions |
| `risk-v1` | risk_engine | signals.enriched | Primary risk validation path |
| `risk-v1-fallback` | risk_engine | signals.pending | Fallback when AI engine lags |
| `execution-v1` | execution_engine | signals.approved | Execute approved signals |
| `execution-v1-kill-switch` | execution_engine | signals.approved | Kill switch monitor (separate consumer for isolation) |
| `risk-v1-order-events` | risk_engine | orders.events | Update position state from fills |

---

## DynamoDB Tables Reference

| Table | Owner | Key Fields | Purpose |
|---|---|---|---|
| `orders` | execution_engine | `order_id` (PK), `signal_id` (GSI) | Order state and lifecycle |
| `positions` | risk_engine | `instrument` (PK), `market` (SK) | Current open positions |
| `risk-state` | risk_engine | `date` (PK), `metric` (SK) | Daily P&L, loss counters, margin snapshots |
| `sessions` | data_ingestion | `broker` (PK) | Zerodha/Alpaca session tokens |
| `strategy-config` | strategy_engine | `strategy_name` (PK) | Runtime-configurable strategy parameters |
| `kill-switch` | risk_engine | `id = "global"` | Kill switch state (active/inactive + reason) |
| `candle-cache` | strategy_engine | `symbol` (PK), `timeframe#timestamp` (SK) | Recent OHLCV candles for candle-based strategies |
| `instrument-registry` | data_ingestion | `symbol` (PK), `market` (SK) | Active instrument watchlist |
| `signal-inbox` | risk_engine | `signal_id` (PK) | Deduplication: has this signal been processed? |
| `signal-outbox` | risk_engine | `signal_id` (PK) | Signals awaiting execution confirmation |

---

## Health Endpoint Reference

Each service exposes a `/health` endpoint for readiness checks:

| Service | Port | Endpoint | Returns |
|---|---|---|---|
| data_ingestion | 8081 | `GET /health` | `{"status": "ok", "zerodha_connected": true, "alpaca_connected": true}` |
| strategy_engine | 8082 | `GET /health` | `{"status": "ok", "strategies_active": 6, "kafka_lag": 12}` |
| risk_engine | 8083 | `GET /health` | `{"status": "ok", "kill_switch": false, "daily_pnl": 0.0}` |
| execution_engine | 8084 | `GET /health` | `{"status": "ok", "paper_mode": true, "orders_today": 0}` |
| ai_engine | 8085 | `GET /health` | `{"status": "ok", "model_loaded": true, "queue_lag": 0}` |

---

## Incident Response Flowchart

```
Alert fires / something looks wrong
          │
          ▼
   make health
   ────────────────────────────────
   All green?  YES → Normal; investigate the specific alert
               NO  ↓
          │
          ▼
   Which service is red?
   ────────────────────────────────
   risk_engine DOWN  → Trading auto-halted (safe). Run make restart-risk
   ai_engine DOWN    → EnrichmentWatchdog activates (safe). Run make restart
   exec_engine DOWN  → Signals queue in signals.approved (safe). Run make restart-execution
   data_ingestion DOWN → No new ticks (strategies go idle). Run make restart
          │
          ▼
   make logs-<service>   ← Read the ERROR lines
          │
          ▼
   Credential issue?     → Rotate credentials in .env, restart
   Kafka disconnected?   → make kafka-topics; docker compose restart redpanda
   DynamoDB unavailable? → docker compose restart localstack; make infra-setup
   Unknown error?        → Activate kill switch, investigate, resolve, then make kill-switch-off
```

---

## Phase 5 Exit Criteria

To exit Phase 5 (paper trading) and enter Phase 6 (live trading with real capital), ALL of the following must be satisfied over a minimum 10-day observation window:

- [ ] **Signal approval rate:** > 30% of generated signals approved by risk engine (validates strategy quality)
- [ ] **Order fill simulation accuracy:** Paper fills consistently within 0.15% of signal price (validates execution simulation)
- [ ] **System uptime:** > 99% during market hours over the 10-day window
- [ ] **Zero critical bugs:** No `CRITICAL` or `ERROR` log entries in execution or risk path
- [ ] **Kill switch zero activations:** No automatic kill switch triggers (indicates P&L within limits)
- [ ] **Kafka consumer lag:** Never exceeds 1000 for more than 60 seconds
- [ ] **Positive Sharpe in paper mode:** Cumulative paper P&L Sharpe ratio > 0.5 over the observation window
- [ ] **Position reconciliation:** Zero mismatches in post-market reconciliation checks
- [ ] **Go-live checklist complete:** All items in `runbooks/go_live_checklist.md` have been verified

For daily operations during live trading, see [daily_operations.md](daily_operations.md).

---

*Last updated: 2026-05-15 | Update this runbook when: infrastructure changes (new services, new Kafka topics, new DynamoDB tables), Phase 5 exit criteria change, or incident response procedures are revised.*
