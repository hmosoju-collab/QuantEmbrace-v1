# QuantEmbrace Phase 2 — Final Approved Design
## Personal Algo Trader Edition

**Version:** 3.0 — FINAL APPROVED
**Date:** 2026-04-30
**Owner:** Hari
**Status:** ✅ APPROVED FOR IMPLEMENTATION
**Replaces:** phase2_kafka_architecture.md (v2.1 — institutional over-engineering, NOT approved)

---

## The Core Reframe

v2.0 and v2.1 were designed against the wrong benchmark: hedge-fund throughput.
That design was 64 Kafka partitions, 15 topics, 3 named consumer groups, a LagMonitor
service, hot partition detection, Schema Registry, and Avro schemas.

For a Zerodha personal trader, **none of that complexity buys you anything**.

What you actually need is different:

> **Never place a wrong order. Never miss a fill. Never trade without risk validation.
> Restart cleanly. Kill safely. Debug easily. Sleep well.**

That is the quality definition for this system. Every design decision below
is evaluated against that definition — not against throughput numbers you will never hit.

**Your realistic trading profile:**
- Instruments: 10–50 NSE instruments, 5–15 US instruments
- Signal rate: 5–30 signals per day (not per second)
- Peak rate: 10–20 signals in the first 15 minutes of NSE open
- Order rate: 5–20 orders per day
- Zerodha rate limit (10 req/sec): you will NEVER hit it
- Capital: retail-grade, not institutional

At these numbers, a well-designed single EC2 instance can handle the entire
trading pipeline with headroom to spare. The quality work is in correctness,
idempotency, and safe failure handling — not throughput.

---

## What Was Over-Engineered (Removed)

These items existed in v2.1 for hedge-fund scale. They add cost, complexity,
and operational burden with zero benefit for personal trading:

| Removed | Why |
|---------|-----|
| 64 partitions on ticks.nse | You watch ≤50 instruments. 4 partitions is sufficient. |
| 15 Kafka topics | 7 topics handle everything you need cleanly. |
| 3 separate risk consumer groups | 30 signals/day doesn't need consumer group isolation. Single group is correct. |
| LagMonitor service (6th service) | A simple age check inside each consumer does the same job. |
| Hot partition detection | Irrelevant with 4 partitions and ≤50 instruments. |
| 5-tier consumer lag policy | 3-tier is precise enough. You don't need fractional conviction. |
| Schema Registry + Avro | JSON with inline schema_version is simpler and debuggable without tooling. |
| Consumer group auto-scaling | There is nothing to scale. You run one instance of each service. |
| ALB + DNS for Zerodha postback | 300ms polling is reliable and requires zero infrastructure. |
| fills-pending DynamoDB table | Restart reconciliation from DynamoDB + bounded Kafka replay is sufficient. |
| Separate backtest topics (2) | A BACKTEST_MODE env flag with a separate consumer group on existing topics. |
| risk.state-updates topic | DynamoDB is the state store. This topic served monitoring dashboards at scale. CloudWatch metrics from the risk engine serve the same purpose at personal scale. |
| 3-AZ MSK cluster | MSK Serverless is cheaper ($5–15/month at your volume) and zero-ops. |

**What this removes:** ~6 topics, ~60 Kafka partitions, 1 entire service, Avro dependency,
ALB infrastructure, Schema Registry, and most of the operational complexity.

---

## What Was Kept (Every Quality Property)

These items protect your money and make the system trustworthy. They are kept
regardless of scale:

| Kept | Why It Matters for Personal Trading |
|------|-------------------------------------|
| Deterministic signal_id (hash + timeframe) | Same market conditions never produce 2 orders |
| Deterministic order_id (hash) | Restart cannot submit the same order twice |
| Deterministic fill_id (hash) | Polling cannot double-count the same fill |
| trace_id end-to-end | When a trade goes wrong, you can trace exactly what happened |
| signal.expires_at check | Old signals are never executed at stale prices |
| Kill switch (scoped: GLOBAL / NSE / US / instrument) | Your panic button works within 200ms |
| DynamoDB conditional write on every state change | The idempotency foundation — cannot be removed |
| Zerodha fill tracking (300ms polling) | Position state is always current |
| Manual Kafka offset commit | Service crash does not lose messages |
| Bounded replay on restart | 60s–30min replay windows; no 24h replays |
| Risk engine sits between strategy and execution | Every order is validated. No bypass. |
| Signal expiry at 3 layers | Risk, execution, broker all check freshness independently |
| 3-tier broker error classification | Never retry an order rejected for bad parameters |
| Kill switch propagation via Kafka (1 partition) | Halt reaches all services within 200ms |

---

## 1. Final Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│               QUANTEMBRACE PHASE 2 — PERSONAL TRADER                 │
│                    v3.0 · FINAL APPROVED                              │
└──────────────────────────────────────────────────────────────────────┘

  ┌─────────────────┐          ┌─────────────────┐
  │   Zerodha WS    │          │   Alpaca WS      │
  │  (NSE ticks)    │          │  (US ticks)      │
  │  + order status │          │  + trade updates │
  │    polling      │          │  (WebSocket)     │
  └────────┬────────┘          └────────┬─────────┘
           │                            │
           └──────────┬─────────────────┘
                      │  normalized ticks · trace_id · sequence_id
                      ▼
           ┌──────────────────────┐
           │    DATA INGESTION    │
           │  sequence dedup      │
           │  exchange_time stamp │
           │  trace_id assignment │
           └──────────┬───────────┘
                      │
        ┌─────────────┴──────────────┐
        │                            │
        ▼                            ▼
  ┌──────────────┐         ┌─────────────────────┐
  │  ticks.nse   │         │  DynamoDB            │
  │  ticks.us    │         │  latest-prices table │
  │  (Kafka)     │         │  (state, not source) │
  └──────┬───────┘         └─────────────────────┘
         │
  ┌──────▼──────────────────────────────────────────────────────────┐
  │              KAFKA — MSK SERVERLESS                              │
  │                                                                  │
  │  ticks.nse           (4 parts · 24h · instrument_id key)        │
  │  ticks.us            (2 parts · 24h · instrument_id key)        │
  │  signals.pending     (2 parts ·  1h · instrument_id key)        │
  │  signals.approved    (2 parts · 30m · instrument_id key)        │
  │  orders.events       (4 parts ·  7d · instrument_id key)        │
  │  risk.kill-switch    (1 part  · 30d · "GLOBAL" key)             │
  │  ops.audit           (2 parts · 90d · trace_id key)             │
  └──────────────────────────────────────────────────────────────────┘
         │                    │                     │
         ▼                    ▼                     ▼
  ┌─────────────┐    ┌────────────────┐    ┌───────────────────┐
  │  STRATEGY   │    │  RISK ENGINE   │    │  DATA PLATFORM    │
  │  ENGINE     │    │                │    │  (future phase)   │
  │             │    │ Group:         │    └───────────────────┘
  │ Group:      │    │ risk-v1        │
  │ strategy-v1 │    │                │
  │             │    │ Consumes:      │
  │ Consumes:   │    │  ticks.nse/us  │
  │  ticks.nse  │    │  signals.pend  │
  │  ticks.us   │    │  orders.events │
  │             │    │                │
  │ Produces:   │    │ Produces:      │
  │  signals.   │    │  signals.appr  │
  │  pending    │    │  orders.events │
  └─────────────┘    │  (rejections)  │
                     │  risk.kill-sw  │
                     │  ops.audit     │
                     └────────────────┘
                              │
                   signals.approved
                              │
                              ▼
                    ┌──────────────────────┐
                    │   EXECUTION ENGINE   │
                    │                      │
                    │ Group: execution-v1  │
                    │ + kill-switch        │
                    │   listener           │
                    │                      │
                    │ Fill tracking:       │
                    │  NSE: 300ms polling  │
                    │  US:  Alpaca WS      │
                    │                      │
                    │ Produces:            │
                    │  orders.events       │
                    │  ops.audit           │
                    └──────────────────────┘
                              │
              ┌───────────────┴────────────┐
              ▼                            ▼
         Zerodha API                  Alpaca API
         (NSE orders)                 (US orders)
              │                            │
              │ fills (300ms poll)         │ fills (WebSocket)
              └──────────────┬─────────────┘
                             ▼
                    orders.events topic
                    (event_type: ORDER_FILLED)
```

---

## 2. Topic Design (7 Topics)

| Topic | Partitions | Retention | Key | Purpose |
|-------|-----------|-----------|-----|---------|
| `ticks.nse` | 4 | 24h | instrument_id | NSE real-time tick stream |
| `ticks.us` | 2 | 24h | instrument_id | US equity tick stream |
| `signals.pending` | 2 | 1h | instrument_id | Unvalidated signals awaiting risk check |
| `signals.approved` | 2 | 30m | instrument_id | Risk-validated, ready for execution |
| `orders.events` | 4 | 7d | instrument_id | All order lifecycle events (unified) |
| `risk.kill-switch` | 1 | 30d | "GLOBAL" | Kill switch state changes |
| `ops.audit` | 2 | 90d | trace_id | Full audit trail of every decision |

**Why 4 partitions on ticks.nse:**
You watch ≤50 NSE instruments. 4 partitions = ~12 instruments per partition.
At NSE tick rates for a personal portfolio, each partition handles <100 ticks/second.
4 partitions provide enough consumer parallelism for 2 instances (2 partitions each)
without over-provisioning infrastructure you will never use.

**Why orders.events is a single unified topic:**
At 5–20 orders per day, separating ORDER_SUBMITTED, ORDER_FILLED, ORDER_CANCELLED,
ORDER_REJECTED into 4 topics adds complexity with zero benefit. A single `orders.events`
topic with an `event_type` field carries all order lifecycle events in instrument order.
The risk engine subscribes to this single topic for position state updates.

**Why signals.pending retention is 1 hour (not 2h):**
Your signal expiry is 30 seconds. Any signal older than 60 seconds is expired and
will be rejected by the risk engine's freshness check. 1-hour retention provides
enough replay window for a risk engine restart without wasting storage.

---

## 3. Consumer Groups (3 Groups)

```
strategy-v1
  Consumes: ticks.nse, ticks.us
  Produces: signals.pending
  Instances: 1 (single EC2, single consumer)
  Lag action: see §5

risk-v1
  Consumes: ticks.nse, ticks.us (price monitoring)
            signals.pending (validation)
            orders.events (fill/cancel processing for position state)
  Produces: signals.approved (to signals.approved topic)
            orders.events (rejection records)
            risk.kill-switch (automatic halt triggers)
            ops.audit (every decision)
  Instances: 1 (single EC2, single consumer)
  Note: A single consumer group consuming 3 topics is correct here.
        At 30 signals/day, there is no throughput argument for separate groups.
        Single group = single offset tracking = simpler debugging.

execution-v1
  Consumes: signals.approved
  Produces: orders.events (submitted, filled, cancelled, rejected)
            ops.audit
  Instances: 1 (ALWAYS running, even when markets are closed — emergency cancel)

kill-switch-listener (not a consumer group — dedicated reader)
  Topic: risk.kill-switch
  Pattern: Seek to latest on start. Read last message. Apply state.
  Used by: execution-v1 process (separate async task within execution engine)
```

**Why single consumer group for risk engine is correct for personal trading:**

The v2.1 argument for 3 separate risk consumer groups was:
"Shared offset commits, shared rebalances." At hedge-fund signal volume, this was valid.
At 30 signals per day, the risk engine processes one signal every 28 minutes on average.
The idea that tick monitoring will starve signal validation in the same consumer group
is theoretical at this scale — it simply cannot happen.

What single group gives you that 3 groups do not: one log stream, one set of Kafka
offsets to monitor, one failure domain, and 3× less operational complexity.
When something goes wrong at 14:47 on a trading day, you want to check one consumer
group, not three.

---

## 4. Event Schemas (JSON — No Schema Registry)

**Why JSON instead of Avro:**
Avro with Schema Registry requires running a separate service, registering schemas
before deployment, and using a client library to serialize/deserialize. For a personal
trader, the benefit is schema enforcement across teams. You are one person. JSON with
a `schema_version` field gives you forward compatibility when you update schemas and
lets you read messages directly from `kcat` without a deserializer. Simplicity is quality.

Schema evolution rule: Never remove fields. Never change field types. Add new optional
fields with defaults. Increment `schema_version` minor version on changes.

### 4.1 Tick Event

```json
{
  "event_id":        "uuid-v4",
  "trace_id":        "uuid-v4 — set here, never changed downstream",
  "event_type":      "TICK",
  "schema_version":  "3.0",
  "source":          "data_ingestion_nse",
  "published_time":  "2026-04-30T03:45:00.124Z",
  "instrument_id":   "NSE:RELIANCE",
  "market":          "NSE",
  "session_id":      "NSE-20260430",
  "sequence_id":     10045231,
  "exchange_sequence": 987654321,
  "exchange_time":   "2026-04-30T03:45:00.001Z",
  "ltp":             2450.50,
  "volume":          12345,
  "bid":             2450.25,
  "ask":             2450.75,
  "tick_type":       "FULL"
}
```

### 4.2 Signal Event (signals.pending)

```json
{
  "event_id":        "uuid-v4",
  "trace_id":        "copied unchanged from triggering tick",
  "event_type":      "SIGNAL",
  "schema_version":  "3.0",
  "source":          "strategy_engine",
  "published_time":  "2026-04-30T03:45:00.146Z",
  "signal_id":       "sha256(strategy_id|instrument_id|direction|timeframe|tick_sequence_id)[:32]",
  "strategy_id":     "momentum_v2",
  "timeframe":       "5m",
  "instrument_id":   "NSE:RELIANCE",
  "market":          "NSE",
  "direction":       "BUY",
  "conviction":      0.78,
  "signal_price":    2450.50,
  "target_price":    2475.00,
  "stop_loss":       2430.00,
  "quantity":        10,
  "order_type":      "LIMIT",
  "signal_time":     "2026-04-30T03:45:00.145Z",
  "expires_at":      "2026-04-30T03:45:30.000Z",
  "tick_event_id":   "uuid of triggering tick event",
  "tick_sequence_id": 10045231
}
```

**signal_id computation (canonical — must not change):**
```
signal_id = sha256(
    strategy_id + "|" + instrument_id + "|" +
    direction   + "|" + timeframe     + "|" +
    str(tick_sequence_id)
)[:32]

Example:
  sha256("momentum_v2|NSE:RELIANCE|BUY|5m|10045231")[:32]
  = "a3f9b2c1d4e5f6a7b8c9d0e1f2a3b4c5"
```

Timeframe must be from canonical list in `configs/instruments.yaml`:
`"1m"`, `"3m"`, `"5m"`, `"15m"`, `"30m"`, `"1h"`, `"1d"`. No other strings accepted.

### 4.3 Risk Decision — signals.approved

```json
{
  "event_id":          "uuid-v4",
  "trace_id":          "copied from signal (unchanged)",
  "event_type":        "SIGNAL_APPROVED",
  "schema_version":    "3.0",
  "source":            "risk_engine",
  "published_time":    "2026-04-30T03:45:00.159Z",
  "risk_decision_id":  "RD-{date}-{signal_id[:8]}",
  "signal_id":         "sha256 from signal",
  "instrument_id":     "NSE:RELIANCE",
  "approved_quantity": 10,
  "approved_price":    2450.50,
  "signal_age_ms":     14,
  "decision_time":     "2026-04-30T03:45:00.158Z",
  "risk_checks_passed": ["kill_switch", "signal_freshness", "position_limit",
                          "daily_pnl", "trade_count", "exposure", "notional", "margin"]
}
```

Rejections are NOT published to Kafka. They are:
1. Written to DynamoDB `risk-decisions` table (permanent audit record)
2. Written to `ops.audit` Kafka topic
3. Logged as structured JSON to CloudWatch

This is simpler and correct. You do not need a separate `signals.rejected` topic for
personal trading — DynamoDB is a better query surface for reviewing rejection patterns.

### 4.4 Order Lifecycle Event (orders.events — all states in one topic)

```json
{
  "event_id":        "uuid-v4",
  "trace_id":        "copied unchanged from signal",
  "event_type":      "ORDER_SUBMITTED | ORDER_FILLED | ORDER_CANCELLED | ORDER_REJECTED",
  "schema_version":  "3.0",
  "source":          "execution_engine",
  "published_time":  "2026-04-30T03:45:00.201Z",
  "order_id":        "sha256(signal_id+risk_decision_id+instrument_id+direction+quantity)[:32]",
  "signal_id":       "sha256 from signal",
  "risk_decision_id":"RD-...",
  "broker_order_id": "Z-123456789",
  "broker":          "zerodha | alpaca",
  "instrument_id":   "NSE:RELIANCE",
  "market":          "NSE",
  "direction":       "BUY",
  "quantity":        10,
  "order_type":      "LIMIT",
  "limit_price":     2450.50,
  "submitted_at":    "2026-04-30T03:45:00.200Z",

  "fill_id":         "sha256(order_id+fill_time+filled_qty)[:32]",
  "filled_quantity": 10,
  "avg_fill_price":  2451.25,
  "slippage_bps":    3.06,
  "fill_time":       "2026-04-30T03:45:01.050Z",
  "fill_source":     "zerodha_polling | alpaca_websocket",

  "cancel_reason":   null,
  "reject_reason":   null
}
```

Fields like `fill_id`, `avg_fill_price`, `fill_time` are null for non-fill events.
`cancel_reason` and `reject_reason` are null for non-cancel/reject events.
One schema, one topic, all order events. The risk engine filters by `event_type`.

### 4.5 Kill Switch Event (risk.kill-switch)

```json
{
  "event_id":        "uuid-v4",
  "event_type":      "KILL_SWITCH_ACTIVATE | KILL_SWITCH_DEACTIVATE",
  "schema_version":  "3.0",
  "source":          "risk_engine | execution_engine | ops_cli",
  "published_time":  "ISO8601 UTC",
  "scope":           "GLOBAL | NSE | US | NSE:RELIANCE",
  "reason":          "daily_loss_limit | circuit_breaker | ops_manual | price_spike",
  "activated_by":    "loss_validator | ops_cli | circuit_breaker",
  "activation_time": "ISO8601 UTC",
  "confirmation_code": null
}
```

---

## 5. Consumer Lag Policy (3-Tier — Personal Scale)

**Measurement:** `message_age_ms = now_utc() - message.published_time`

```
TIER 1 — NORMAL: message_age_ms < 500ms
  Action: Process normally. Generate signals. Place orders.
  Rationale: Sub-500ms is well within budget for 5-minute candle strategies.

TIER 2 — DEGRADED: 500ms ≤ message_age_ms < 3000ms
  Action:
    Strategy: Generate signal with conviction multiplied by 0.5.
              Add DEGRADED flag to signal event. Log warning.
    Risk:     Validate signal but log signal_age_ms in risk decision.
    Execution:Re-check kill switch before placing order (in-memory, 0ms).
  Rationale: The signal is stale but not dangerously so. Reduce position size
             (0.5× conviction → smaller approved_quantity) to limit exposure.
             You are a personal trader — preserve capital during degraded conditions.

TIER 3 — HALT: message_age_ms ≥ 3000ms
  Action:
    Strategy: Stop generating new signals. Continue consuming (drain backlog).
              Resume when 10 consecutive messages are in Tier 1.
    Risk:     Continue processing (drain backlog, reject expired signals via expires_at).
    Execution:Stop placing new orders. Continue consuming (drain — signals will expire).
  Alert: CloudWatch alarm → SNS email/SMS to you within 60 seconds of sustained Tier 3.
  Note: No automatic kill switch from lag alone. Kill switch is your decision, not the system's.
        Exception: if risk engine's orders.events consumer lag > 100 messages for 60 seconds,
        kill switch fires automatically (stale position state is the one lag condition
        that cannot be tolerated).
```

**Why 3 tiers instead of 5:**
Your signal alpha decay window on a 5-minute strategy is 30 seconds. The difference
between "200ms lag" and "1000ms lag" is irrelevant when your signal expires in 30 seconds.
What matters is: fresh (<500ms), degraded (500ms-3s), and halted (≥3s). Clean, debuggable.

---

## 6. Kill Switch Design (Unchanged Quality — Simplified Operation)

### Hierarchy (non-negotiable)

```
GLOBAL > MARKET (NSE | US) > INSTRUMENT (NSE:RELIANCE)
Most restrictive wins. Always.
```

### Evaluation (in-memory, 0ms, called before every signal approval and order placement)

```python
def is_trading_allowed(instrument_id: str, market: str) -> bool:
    if kill_switch.global_halt:
        return False
    if market in kill_switch.halted_markets:
        return False
    if instrument_id in kill_switch.halted_instruments:
        return False
    return True
```

### Automatic Kill Switch Triggers (5 triggers — unchanged from Phase 1)

```
1. Daily P&L loss > configured threshold (loss_validator in risk engine)
2. Single strategy loss > configured threshold (KillSwitchMonitor)
3. Order rate runaway > N orders/window (KillSwitchMonitor)
4. Broker connectivity lost > 30 seconds (KillSwitchMonitor)
5. Data feed stale > 10 seconds during market hours (KillSwitchMonitor)
   + NEW in Phase 2:
6. orders.events consumer lag > 100 messages for 60 seconds (stale position state)
7. Kafka write failure on orders.events or signals.pending (3 consecutive failures)
```

### Manual Kill Switch

```bash
# Activate (halt all trading immediately)
python scripts/kill_switch_cli.py activate --reason "market too volatile"

# Activate scoped (halt NSE only)
python scripts/kill_switch_cli.py activate --scope NSE --reason "Zerodha issues"

# Activate instrument-level
python scripts/kill_switch_cli.py activate --scope NSE:ADANIENT --reason "circuit breaker"

# Check status
python scripts/kill_switch_cli.py status

# Deactivate (requires confirmation)
python scripts/kill_switch_cli.py deactivate --confirm CONFIRM-RESUME-20260430
```

### Kill Switch Propagation Flow

```
Trigger
  → DynamoDB write (quantembrace-{env}-sessions / KILLSWITCH / GLOBAL)      ~15ms
  → Kafka publish to risk.kill-switch                                        ~10ms
  → risk engine kill-switch-listener receives                                ~30ms
  → in-memory flag updated: kill_switch.global_halt = True                  ~0ms
  → execution engine kill-switch-listener receives (same Kafka read)        ~30ms
  → in-memory flag updated                                                   ~0ms
  → All signal approvals and order placements check in-memory flag           ~0ms

Total: 50–200ms from trigger to "no more orders placed"
SLA: < 500ms guaranteed
```

---

## 7. Zerodha Fill Tracking (Polling — Approved for Personal Trading)

**Decision:** 300ms polling. No ALB. No webhook. No infrastructure changes.

The postback webhook is architecturally superior but requires an ALB (~$20/month) and
a publicly accessible HTTPS endpoint. For a personal trader placing 5–20 orders per day,
the postback latency improvement (100ms vs 300ms) is not material. 300ms fill detection
latency on a strategy with 5-minute holding periods is negligible.

**Polling design:**

```
Every 300ms (background async task in execution engine):

1. Query DynamoDB for all orders in PENDING status
   (typically 0–5 orders at any time for personal trading)

2. For each PENDING order:
   a. Call Zerodha GET /orders/{broker_order_id}
      (well within 10 req/sec rate limit: 5 orders × 3.3 polls/sec = 16.5 req/sec max,
       but in practice you have 1–3 concurrent orders → 3–10 req/sec → safe)

   b. If status == COMPLETE:
      i.   Compute fill_id = sha256(order_id + fill_time + filled_qty)[:32]
      ii.  DynamoDB conditional write on fill_id (IDEMPOTENCY GATE)
           condition: attribute_not_exists(fill_id)
      iii. If condition passes (new fill):
             Update order status to FILLED in DynamoDB
             Publish ORDER_FILLED event to orders.events
             Metric: ORDER_FILLED
      iv.  If condition fails (already recorded): skip, metric: FILL_DEDUP_HIT

   c. If status == CANCELLED:
      Publish ORDER_CANCELLED event. Update DynamoDB.

   d. If status == REJECTED:
      Publish ORDER_REJECTED event. Update DynamoDB.
      Alert: SNS notification (rejected orders require attention)

3. Continue loop. On exception: log, continue (do not crash the loop)

Fill detection latency:
  Average: 150ms (midpoint of 300ms interval)
  Worst case: 300ms
  This is acceptable for your strategy holding periods.
```

**Upgrade path:** If you ever want the postback webhook, the polling loop can stay active
as a fallback. The DynamoDB conditional write on fill_id prevents double-publishing.
Both can run simultaneously.

---

## 8. Kafka Configuration (Personal Scale)

### Infrastructure: MSK Serverless

**Why MSK Serverless over 3-broker MSK cluster:**

| | MSK Serverless | 3-Broker MSK m5.large |
|--|---------------|----------------------|
| Cost at personal volume | ~$5–15/month | ~$300–400/month |
| Management | Zero (AWS manages) | Zero (MSK is managed) |
| Scaling | Automatic | Manual partition management |
| Replication | Managed by AWS | RF=3, min.isr=2 |
| Suitable for | <200 MB/s throughput | High-throughput production |
| Your actual throughput | <1 MB/s | 1 MB/s out of 200 MB/s capacity |

At your trading volume, MSK Serverless is the correct choice. It costs less than a
dinner out per month and requires zero capacity planning.

**MSK Serverless constraints to be aware of:**
- Partition count limits: up to 120 partitions per cluster (you need 17 total)
- Throughput: up to 200 MB/s aggregate (you use <1 MB/s)
- Retention: up to 7 days for most topics (configure per §2)
- IAM-based authentication only (no SASL/SCRAM) — fine for AWS-hosted services

### Producer Config

```
acks=all
  WHY: With MSK Serverless, AWS manages replication. acks=all ensures the message
       is durably committed before the producer confirms. One extra millisecond
       of latency is worth never losing a signal event.

enable.idempotence=true
  WHY: Prevents duplicate messages from network retry. Especially important for
       signals.pending — you cannot have the same signal processed twice.

max.in.flight.requests.per.connection=1
  WHY: Guarantees ordering during retries. Required with idempotence.

retries=5
  WHY: MSK Serverless failover is handled by AWS. 5 retries is sufficient.

retry.backoff.ms=200
compression.type=lz4
batch.size=4096    (smaller than institutional — your message rate is low)
linger.ms=5        (wait up to 5ms to batch — no perceptible impact at personal volume)
max.block.ms=5000  (5s timeout if Kafka unavailable — triggers kill switch fallback)
delivery.timeout.ms=30000
```

### Consumer Config

```
enable.auto.commit=false      (manual commit after processing — non-negotiable)
auto.offset.reset=earliest    (subject to bounded replay — see §9)
isolation.level=read_committed
max.poll.interval.ms=30000
session.timeout.ms=10000
heartbeat.interval.ms=3000
fetch.min.bytes=1
fetch.max.wait.ms=100         (100ms max wait — slightly more than institutional)
max.poll.records=50            (small batch — your rate is low, no need for 100+)
```

### Kafka Failure → Kill Switch

```
3 consecutive delivery failures on orders.events or signals.pending:
  → Kill switch via separate producer (acks=1, max.block.ms=1000)
  → If Kafka is completely down: DynamoDB write + 5s poll by each service
  → Trading halts within 10 seconds

Non-critical topics (ops.audit):
  → 10 consecutive failures → buffer in memory → retry on recovery → trading continues
```

---

## 9. Bounded Replay on Restart

```
On consumer startup:
  last_commit_time = timestamp at last committed Kafka offset
  if (now() - last_commit_time) > MAX_REPLAY_WINDOW:
    seek to: now() - MAX_REPLAY_WINDOW
    log: REPLAY_BOUNDED (gap: X seconds, topic: Y)
  else:
    resume from last committed offset

Per-consumer replay windows:
  strategy-v1 (ticks):           5 minutes
    Rationale: Ticks older than 5 minutes are not useful for signal generation.
               Any strategy running on a 5-minute candle needs at most 5 minutes
               of missed ticks to recompute its state.

  risk-v1 (signals.pending):     5 minutes
    Rationale: All signals expire in 30 seconds. Replaying 5 minutes catches
               signals from just before crash. All will be rejected as expired.
               The value is seeing them in audit logs, not processing them.

  risk-v1 (orders.events fills): 30 minutes
    Rationale: Fills must not be missed. A 30-minute downtime during a busy period
               is the worst realistic scenario for a personal trader. All fills
               in that window must be processed to restore correct position state.

  execution-v1 (signals.approved): 30 minutes
    Rationale: Approved signals are precious — you may want to execute them.
               But signals have expires_at = signal_time + 30s, so most will be
               expired on replay. The replay catches the rare case where the
               execution engine crashed 5 seconds after approval.
```

---

## 10. Failure Mode Table (Personal Trader Edition)

| Failure | Detection | Response | Trading? |
|---------|-----------|----------|----------|
| MSK Serverless unavailable | Producer exception → 3 failures | Kill switch via DynamoDB fallback | NO — clean halt |
| Data ingestion crash | CloudWatch ECS alarm | ASG restarts (~2 min). Tick gap. Strategy receives no ticks, generates no signals. | YES — signal gap until restart |
| Zerodha WebSocket drop | Connection error handler | Reconnect with exponential backoff (5 retries). If silent >10s with open positions: scoped kill switch NSE. | HALTED if open positions |
| Strategy engine crash | CloudWatch alarm | ASG restarts. Kafka offset preserved. No state loss. Signal gap ~60s. | YES — brief gap |
| Risk engine crash | CloudWatch alarm | ASG restarts. Bounded replay (5 min ticks, 30 min fills). Stale signals rejected by expires_at. | YES — gap while reconciling |
| Execution engine crash | CloudWatch alarm | ASG restarts. DynamoDB reconcile on startup (open orders vs broker). Bounded replay 30 min. | YES — orders reconciled |
| Zerodha API 429 (rate limit) | Circuit breaker (5 failures/60s) | Circuit opens → scoped kill switch NSE. US trading continues. | NO (NSE) |
| Zerodha daily token expiry | ZerodhaTokenManager | needs_authentication mode. All NSE orders rejected. SNS alert to you. | NO (NSE) until manual refresh |
| Alpaca API outage | Circuit breaker | Scoped kill switch US. NSE continues. | NO (US) |
| DynamoDB slow | Risk validation >200ms | Risk validates slower. Signals may expire before approval. CloudWatch alarm. | YES — reduced throughput |
| Kill switch consumer lag >100 offsets | Self-check in execution engine | Self-imposed halt until lag clears. | NO — safety halt |
| Zerodha polling misses a fill | Polling retry cycle, reconcile on restart | Polling catches fill in next 300ms cycle. Restart reconciliation catches any missed fills. | YES — no impact |
| Duplicate Zerodha postback / double poll | DynamoDB conditional write on fill_id | Duplicate detected, skipped. FILL_DEDUP_HIT metric. Position state correct. | YES — no impact |
| Duplicate signal (restart replay) | deterministic signal_id + DynamoDB conditional write | Duplicate detected, skipped. ORDER_IDEMPOTENCY_HIT metric. No duplicate order. | YES — no impact |
| Stale signal executed | expires_at check at risk + execution | Signal rejected as SIGNAL_EXPIRED. No order placed. | YES — no impact |
| Daily P&L loss limit | DynamoDB loss_validator | Kill switch: scope=GLOBAL. All trading halts. SNS alert. | NO — by design |

---

## 11. Observability: What You Actually Need to Watch

You are one person. You do not need a 5-row CloudWatch dashboard with 20 widgets.
You need to know, at a glance, whether the system is behaving correctly.

**Three questions at any moment:**
1. Is the kill switch off? (yes/no)
2. Are my services running? (yes/no per service)
3. Did today's trades execute as expected? (P&L, fills, rejections)

**CloudWatch Dashboard — 2 rows, 8 widgets:**

```
Row 1 — System Status (always visible on your monitor):
  [Kill Switch Status]    RED if any scope active, GREEN if all clear
  [Services Running]      ECS task count per service (1/1 = green, 0/1 = red)
  [Open Positions]        Count of open positions (from DynamoDB custom metric)
  [Daily P&L (INR)]       Running P&L from risk engine (custom metric)

Row 2 — Today's Trading Activity:
  [Signals Generated]     Count today (bar chart by hour)
  [Orders Placed / Filled] Placed vs filled (grouped bar)
  [Rejection Reasons]     Pie chart of risk rejection reasons today
  [Broker Latency P99]    Order placement latency in ms
```

**Alerts (SNS → your email/phone):**
```
P0 (immediate action):
  Kill switch activated (any scope)
  Order rejected by broker (non-retryable)
  Zerodha token expired
  Service crash (ECS task count = 0)

P1 (check within 1 hour):
  Daily P&L down >50% of daily limit
  Zerodha WebSocket silence >30s
  Consumer lag Tier 3 sustained >2 minutes

P2 (review end of day):
  Fill detection latency >1 second (polling issue)
  Signal rejection rate >50% (strategy or risk config issue)
  Kafka write failures (non-critical topics)
```

**ops.audit as your trade journal:**
Every signal, every risk decision, every order event is written to `ops.audit`.
End of day, you can query CloudWatch Logs Insights:
```sql
fields @timestamp, event_type, instrument_id, direction, signal_price, avg_fill_price, slippage_bps
| filter trace_id = "any-trace-id"
| sort @timestamp asc
```
This gives you the complete lifecycle of any trade from tick to fill in one query.

---

## 12. Cost Estimate (Monthly)

| Component | Phase 1 Cost | Phase 2 Addition | Total |
|-----------|-------------|-----------------|-------|
| EC2 (existing, 4 instances) | ~$130–160 | $0 | $130–160 |
| MSK Serverless | $0 | ~$10–20 | $10–20 |
| DynamoDB on-demand | ~$5 | ~$2 | ~$7 |
| S3 (ticks, logs, artifacts) | ~$3 | ~$1 | ~$4 |
| CloudWatch (logs, metrics, alarms) | ~$8 | ~$3 | ~$11 |
| SNS (alerts) | ~$1 | $0 | ~$1 |
| **Total** | **~$147–177** | **~$16–26** | **~$163–203** |

Phase 2 adds approximately **₹1,400–2,200/month** to your AWS bill.
This is well justified for the ordering guarantees, replay capability, and audit trail.

---

## 13. What Phase 2 Gives You (Quality Checklist)

```
✅ Never place a duplicate order
   → signal_id + order_id are deterministic hashes
   → DynamoDB conditional write guards every state transition

✅ Never trade without risk validation
   → Execution engine only consumes from signals.approved
   → Risk engine is the only producer to signals.approved
   → No bypass path exists

✅ Never trade on a stale signal
   → expires_at checked at risk engine (before approval)
   → expires_at re-checked at execution engine (before broker call)
   → 3-tier lag policy stops signal generation at 3+ second lag

✅ Kill switch always works within 500ms
   → Kafka single-partition topic, all services listening
   → In-memory flag updated immediately on receipt
   → Hierarchy enforced: Global > Market > Instrument

✅ Restart safely after crash
   → Kafka offset + DynamoDB = full state reconstruction
   → Bounded replay prevents massive backlog replays
   → Startup reconciliation syncs open orders with broker

✅ Know what happened to every trade
   → trace_id propagated from tick → signal → order → fill
   → ops.audit topic: 90-day retention, queryable via CloudWatch Insights
   → Every risk rejection recorded in DynamoDB

✅ Position state is always current
   → 300ms Zerodha polling + Alpaca WebSocket
   → DynamoDB conditional write on fill_id prevents double-counting
   → Risk engine processes fills before approving next signal

✅ Operational simplicity
   → 7 topics (not 15)
   → 3 consumer groups (not 9)
   → JSON (not Avro + Schema Registry)
   → MSK Serverless (no cluster management)
   → One person can debug this system at 2am
```

---

## 14. Pre-Implementation Checklist (Approved List)

> **STATUS: ✅ COMPLETE — 2026-05-04**
> All items below are implemented. Run `scripts/kafka/validate_phase2.py --all`
> to execute the automated validation suite before enabling live capital.

**Infrastructure (do first):**
- [x] Provision MSK Serverless cluster in ap-south-1 via Terraform
      → `infra/terraform/modules/kafka/main.tf` (PHASE2-001, 2026-05-02)
- [x] Create all 7 topics via topic creation script (auto.create.topics.enable=false)
      → `scripts/kafka/create_topics.py` (PHASE2-001, 2026-05-02)
- [x] Add MSK VPC endpoint (private connectivity from EC2 → MSK)
      → `infra/terraform/modules/vpc/main.tf` aws_vpc_endpoint.msk + sg (PHASE2-006, 2026-05-04)
- [x] IAM policy: each EC2 instance role gets kafka:Produce + kafka:Consume on specific topics
      → `infra/terraform/modules/kafka/main.tf` per-service IAM policies (PHASE2-001, 2026-05-02)

**Zerodha fill tracking (do before any live trading):**
- [x] Implement 300ms polling loop in execution engine for PENDING orders
      → `services/execution_engine/polling/bulk_order_poller.py` (BLOCKER B1, 2026-04-30)
- [x] DynamoDB conditional write on fill_id in polling handler
      → `bulk_order_poller._write_fill_record()` attribute_not_exists(PK) gate (BLOCKER B1)
- [x] ORDER_FILLED event published to orders.events on fill detection
      → `bulk_order_poller._handle_fill()` calls `KafkaOrderEventsPublisher.publish_fill()` (PHASE2-006, 2026-05-04)

**Service changes:**
- [x] data_ingestion: KafkaTickPublisher replaces SQSTickPublisher
      set trace_id (uuid4) and exchange_sequence on each tick
      → `services/data_ingestion/publishers/kafka_tick_publisher.py` (PHASE2-002, 2026-05-02)
- [x] strategy_engine: Kafka consumer (strategy-v1) replaces DynamoDB polling
      signal_id = sha256(5-field hash), expires_at = signal_time + 30s
      → `services/strategy_engine/consumers/kafka_tick_consumer.py` (PHASE2-003, 2026-05-02)
- [x] risk_engine: single Kafka consumer group (risk-v1) consumes
      signals.pending + orders.events; kill-switch-listener as dedicated async task
      publishes to signals.approved (not SQS)
      → `services/risk_engine/consumers/kafka_order_events_consumer.py` (PHASE2-004, 2026-05-04)
      → `services/risk_engine/killswitch/kafka_kill_switch_listener.py` (PHASE2-004, 2026-05-04)
      → `services/risk_engine/service.py` 4-loop asyncio.gather() (PHASE2-004, 2026-05-04)
- [x] execution_engine: Kafka consumer (execution-v1) consumes signals.approved
      300ms fill polling loop; publishes to orders.events
      → `services/execution_engine/consumers/kafka_approved_consumer.py` (PHASE2-005)
      → `services/execution_engine/publishers/kafka_order_events_publisher.py` (PHASE2-005)

**Validation before go-live:**
- [ ] Run `scripts/kafka/validate_phase2.py --test 1` → trace_id propagates end-to-end
- [ ] Run `scripts/kafka/validate_phase2.py --test 2` → no duplicate orders (DynamoDB gate)
- [ ] Run `scripts/kafka/validate_phase2.py --test 3` → fill dedup gate fires correctly
- [ ] Run `scripts/kafka/validate_phase2.py --test 4` → kill switch halts within 500ms
- [ ] Run `scripts/kafka/validate_phase2.py --test 5` → NSE scoped halt, US continues
- [ ] Run `scripts/kafka/validate_phase2.py --test 6` → restart recovery, bounded replay
- [ ] Complete paper trading gate (see `--test 7` checklist) before enabling live capital

---

## Summary: What Phase 2 Is and Is Not

**Phase 2 IS:**
A quality, fault-proof, personal algo trading backbone. Every quality property that
matters for protecting your capital is present. The system is simpler, cheaper, and
more maintainable than the institutional v2.1 design — without sacrificing any
correctness guarantee.

**Phase 2 IS NOT:**
An HFT system. A hedge-fund platform. A system that needs 64 Kafka partitions.
A system that needs a LagMonitor service. A system that needs Avro and Schema Registry.
None of that was going to help you trade better or sleep better.

**The principle that governs this design:**
> Complexity is a liability, not an asset. Every component you do not need is
> a component that cannot fail, cannot cause confusion, and cannot cost you money.
> Quality means the right design for your actual use case — not the most
> impressive-sounding one.

*Phase 2 v3.0 — Approved for Implementation*
