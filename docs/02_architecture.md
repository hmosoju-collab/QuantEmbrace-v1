# QuantEmbrace — Architecture Deep-Dive

> **Prerequisite:** Read [01_introduction.md](01_introduction.md) first if you haven't already.

This document explains the 6-layer architecture. For each layer we cover:
- What it does in plain English
- Why it exists as a separate layer
- What happens when it fails
- The key files and code

---

## Table of Contents

1. [The 6-Layer Model — Overview](#the-6-layer-model--overview)
2. [Layer 1 — Data Ingestion](#layer-1--data-ingestion)
3. [Layer 2 — Strategy Engine](#layer-2--strategy-engine)
4. [Layer 4 — Risk Engine (Critical)](#layer-4--risk-engine-critical)
5. [Layer 3 — Execution Engine](#layer-3--execution-engine)
6. [Layer 5 — AI/ML Engine](#layer-5--aiml-engine)
7. [Layer 6 — Infrastructure](#layer-6--infrastructure)
8. [How Layers Communicate — Kafka Topics](#how-layers-communicate--kafka-topics)
9. [The Golden Rule — No Layer Skipping](#the-golden-rule--no-layer-skipping)
10. [Failure Modes and Resilience](#failure-modes-and-resilience)

---

## The 6-Layer Model — Overview

Think of the system as a pipeline with strict one-way flow. Every message travels as an event on a Kafka topic — services never call each other directly.

```
                    RAW MARKET DATA
                    (Zerodha + Alpaca)
                          │
                          ▼
          ┌───────────────────────────┐
          │   LAYER 1: DATA           │  ← Collects and normalises prices
          │   data_ingestion          │
          └───────────────┬───────────┘
                          │  Kafka: ticks.nse, ticks.us
                          ▼
          ┌───────────────────────────┐
          │   LAYER 2: STRATEGY       │  ← Decides "should we trade?"
          │   strategy_engine         │    6 strategies run in parallel
          └───────────────┬───────────┘
                          │  Kafka: signals.pending
                          ▼
          ┌───────────────────────────┐
          │   LAYER 5: AI/ML          │  ← Enriches signals with ML predictions
          │   ai_engine               │    regime, quality_score, volatility
          └───────────────┬───────────┘
                          │  Kafka: signals.enriched
                          ▼
          ┌───────────────────────────┐
          │   LAYER 4: RISK    ◄◄◄   │  ← Validates every signal  ← CRITICAL GATE
          │   risk_engine             │    11 validators, no bypass
          └───────────────┬───────────┘
                          │  Kafka: signals.approved
                          ▼
          ┌───────────────────────────┐
          │   LAYER 3: EXECUTION      │  ← Places orders with brokers
          │   execution_engine        │
          └───────────────┬───────────┘
                          │  Kafka: orders.events
                     ┌────┴────┐
                     ▼         ▼
                Zerodha     Alpaca
                (NSE)        (US)

          ┌───────────────────────────┐
          │   LAYER 6: INFRA          │  ← AWS EC2 ASGs, MSK Kafka, DynamoDB,
          │   infra/terraform/        │    S3, VPC, Monitoring — supports all above
          └───────────────────────────┘
```

**Important:** Layer numbering reflects conceptual importance, not flow order. The actual flow is:
`Data (1) → Strategy (2) → AI (5) → Risk (4) → Execution (3)`

The Risk Engine (4) intentionally sits above Execution (3) in the numbering to emphasise that it is the most critical gate in the system.

---

## Layer 1 — Data Ingestion

### What It Does

This layer is the "eyes" of the system. It connects to broker market data feeds, normalises every incoming tick into a unified internal format, and publishes those ticks to Kafka for downstream consumers.

```
Zerodha Kite WebSocket ──┐
                          ├──► Normalise ──► Kafka: ticks.nse (key = symbol)
Alpaca WebSocket    ───────┘                 Kafka: ticks.us  (key = symbol)
                                             DynamoDB: latest-prices (hot cache)
                                             S3: historical Parquet files
```

### Data Normalisation

Every tick from either broker is converted into one unified `MarketTick` format before anything else sees it. Downstream services never deal with broker-specific payloads:

```python
# Zerodha sends a broker-specific binary/JSON packet:
{"instrument_token": 738561, "last_price": 2453.50, "volume": 1234567, ...}

# Alpaca sends a completely different format:
{"T": "t", "S": "AAPL", "p": 182.15, "s": 100, "t": "2026-04-24T14:30:00Z"}

# After normalisation — BOTH become this:
MarketTick(
    market="NSE",                         # or "US"
    instrument="NSE:RELIANCE",            # or "US:AAPL"
    ltp=Decimal("2453.50"),
    volume=1234567,
    timestamp=datetime(2026, 4, 24, 3, 46, 3, tzinfo=UTC),  # Always UTC
    bid=Decimal("2453.45"),
    ask=Decimal("2453.55"),
)
```

### Storage Layers

| Store | What goes there | Why |
|---|---|---|
| **Kafka** `ticks.nse` / `ticks.us` | Every normalised tick | Real-time stream for strategy_engine |
| **DynamoDB** `latest-prices` | Current LTP per instrument | Sub-millisecond reads for risk and strategy |
| **S3** `quantembrace-data/ticks/` | All historical ticks (Parquet) | Cheap long-term storage; read during backtesting and ML training |

**Key files:**
- [services/data_ingestion/service.py](../services/data_ingestion/service.py) — main event loop, WebSocket management
- [services/data_ingestion/connectors/zerodha_connector.py](../services/data_ingestion/connectors/zerodha_connector.py) — Kite WebSocket handler
- [services/data_ingestion/connectors/alpaca_connector.py](../services/data_ingestion/connectors/alpaca_connector.py) — Alpaca streaming handler
- [services/data_ingestion/processors/tick_processor.py](../services/data_ingestion/processors/tick_processor.py) — normalisation logic

**What happens if this layer fails?** Kafka topic `ticks.nse` or `ticks.us` stops receiving new messages. The strategy engine continues but stops generating signals on stale data (strategies detect the staleness). CloudWatch alarm fires if the last tick on either topic is > 60s old.

---

## Layer 2 — Strategy Engine

### What It Does

This layer is the "brain" of the system. It consumes tick data and runs 6 independent strategies to find trading opportunities.

```
Kafka: ticks.nse, ticks.us
            │
            ▼
   ┌──────────────────────────────────────┐
   │  Strategy Engine                     │
   │                                      │
   │  ┌──────────────────────────────┐   │
   │  │ MomentumStrategy             │   │  ← tick-based, moving average crossover
   │  └──────────────────────────────┘   │
   │  ┌──────────────────────────────┐   │
   │  │ ORB (Opening Range Breakout) │   │  ← candle-based, first 15min range break
   │  └──────────────────────────────┘   │
   │  ┌──────────────────────────────┐   │
   │  │ Scalp1m                      │   │  ← candle-based, 1-minute momentum
   │  └──────────────────────────────┘   │
   │  ┌──────────────────────────────┐   │
   │  │ VWAPReversionStrategy        │   │  ← candle-based, mean reversion to VWAP
   │  └──────────────────────────────┘   │
   │  ┌──────────────────────────────┐   │
   │  │ IntradayTrend15m             │   │  ← candle-based, 15-minute trend following
   │  └──────────────────────────────┘   │
   │  ┌──────────────────────────────┐   │
   │  │ PreCloseMomentum             │   │  ← candle-based, pre-close momentum burst
   │  └──────────────────────────────┘   │
   └──────────────────┬───────────────────┘
                      │
                      ▼  Kafka: signals.pending (key = instrument)
```

### What a Signal Looks Like

A signal carries everything the downstream pipeline needs to evaluate and act on it:

```python
Signal(
    signal_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",  # UUID, unique forever
    strategy_name="nse_momentum_v1",
    market="NSE",
    instrument="NSE:RELIANCE",
    direction=Direction.BUY,
    quantity=20,
    order_type=OrderType.MARKET,
    stop_price=Decimal("2420.00"),  # Stop-loss — REQUIRED on every signal
    confidence=0.75,                # 0.0 = low conviction, 1.0 = high conviction
    paper_trade=True,               # True until operator promotes to live
    metadata={"reason": "golden_cross", "short_ma": 2420, "long_ma": 2400},
    created_at=datetime(2026, 4, 24, 4, 0, 0, tzinfo=UTC)
)
```

### Signal Timing — Important Constraint

Candle-based strategies stamp `Signal.generated_at = candle.candle_close_time`. A 1-minute candle closing at T is written to DynamoDB at T+5s, polled at T+5.5s, enriched by ai_engine, and arrives at the risk engine at T+7–12s. This is why `RISK_MAX_SIGNAL_AGE_SECONDS` must be ≥ 30s. The default of 5s rejects every candle signal — this was the root cause of Days 1-4 zero fills.

### What the Strategy Layer Must NEVER Do

- Call broker APIs (that is execution's job)
- Check account balances or margin (that is risk's job)
- Cancel or modify orders (that is execution's job)
- Track P&L (that is risk's job)

**Key files:**
- [services/strategy_engine/service.py](../services/strategy_engine/service.py) — main service, strategy orchestration
- [services/strategy_engine/strategies/](../services/strategy_engine/strategies/) — all 6 strategy implementations

**What happens if this layer fails?** No new signals are generated. Existing positions are unaffected — risk and execution don't depend on the strategy engine staying alive. Signals that were already in the pipeline continue flowing.

---

## Layer 5 — AI/ML Engine

### What It Does

This layer runs between the strategy engine and the risk engine. It consumes every pending signal, adds machine-learning enrichment metadata, and republishes the enriched signal.

```
Kafka: signals.pending  →  [ai_engine]  →  Kafka: signals.enriched
```

### What Enrichment Adds

```python
# Signal BEFORE enrichment (signals.pending):
Signal(instrument="NSE:RELIANCE", direction=BUY, confidence=0.75, ...)

# Signal AFTER enrichment (signals.enriched):
Signal(
    ...,                                     # all original fields unchanged
    quality_score=0.82,                      # ML confidence: how good is this signal?
    market_regime="trending",                # is market in trend, ranging, or volatile mode?
    volatility_estimate=0.018,               # expected next-hour volatility
    enrichment_version="v1.2",
)
```

### Why a Separate Service?

The AI engine runs as an independent Kafka consumer, not as a library inside the strategy engine. This means:
- Models can be updated without restarting the strategy engine
- Enrichment can be scaled independently
- If the AI engine falls behind, the `EnrichmentWatchdog` in the risk engine detects the lag and activates a **fallback path**: signals flow directly from `signals.pending` → risk engine without enrichment. Trading continues safely, just without ML quality filtering.

### Fallback Path

```
Normal path:
  signals.pending → ai_engine → signals.enriched → risk_engine

Fallback path (EnrichmentWatchdog activates when ai_engine lag > threshold):
  signals.pending → risk_engine (directly, without enrichment metadata)

Recovery:
  EnrichmentWatchdog automatically switches back to normal path when lag clears.
  No manual action needed.
```

**Key files:**
- [services/ai_engine/service.py](../services/ai_engine/service.py) — Kafka consumer + inference loop
- [services/ai_engine/inference/predictor.py](../services/ai_engine/inference/predictor.py) — model inference
- [services/ai_engine/features/feature_pipeline.py](../services/ai_engine/features/feature_pipeline.py) — feature computation

**What happens if this layer fails?** The `EnrichmentWatchdog` in the risk engine detects the Kafka lag within seconds and activates fallback mode. Trading continues on `signals.pending` without enrichment. An SNS alert fires to notify the operator.

---

## Layer 4 — Risk Engine (Critical)

> **This is the most important layer. Read this section carefully.**

### What It Does

The Risk Engine is the mandatory gatekeeper. No signal can become an order without passing through it. There is no bypass path — not in production, not in staging, not in code.

```
Every signal arrives here. 11 validators run in order:

SIGNAL IN
     │
     ▼
1. SignalAgeValidator      — reject if signal > 30s old (stale)
     │
     ▼
2. KillSwitchValidator     — reject all immediately if kill switch is ON
     │
     ▼
3. PositionValidator       — reject if too many open positions (max 8)
     │
     ▼
4. ExposureValidator       — reject if total portfolio exposure too high (max 50%)
     │
     ▼
5. DailyLossValidator      — reject if daily P&L loss exceeds limit (2%); auto-fires kill switch at 3%
     │
     ▼
6. MarginValidator         — reject if insufficient broker margin
     │
     ▼
7. SlippageValidator       — reject if market moved > 0.15% since signal was generated
     │
     ▼
8. SpreadGateValidator     — reject if bid-ask spread > 50 bps (illiquid conditions)
     │
     ▼
9. SectorConcentrationValidator — reject if single sector > 20% of portfolio
     │
     ▼
10. LiquidityValidator     — reject if order > 1% of average daily volume
     │
     ▼
11. QualityScoreValidator  — reject if ai_engine quality_score < threshold (default 0.30)
     │
     ▼
  APPROVED → Kafka: signals.approved
```

If any check fails, the pipeline short-circuits. All remaining validators are skipped.

### The Kill Switch

The kill switch is stored as a record in DynamoDB. When active, the KillSwitchValidator rejects every incoming signal without running any other checks.

**Three ways it activates:**
1. **Automatically** — the `DailyLossValidator` finds daily loss exceeds 3% of portfolio
2. **Manually** — operator runs `make kill-switch-on` or `python scripts/kill_switch_cli.py activate`
3. **Via `KillSwitchMonitor`** — background task that monitors for position drift, margin breach, or abnormal fill rates

**When activated:**
- All in-flight signals are rejected immediately
- All pending orders are cancelled (best-effort)
- Positions are held (not force-closed — force-closing during a crash can make things worse)
- SNS alert sent to operator
- Kill switch stays ON until manually deactivated

### Every Decision Is Logged

Every risk decision — approve or reject — is written to S3 as a JSON audit record:

```json
{
  "risk_decision_id": "abc-789-xyz",
  "signal_id": "f47ac10b-...",
  "status": "APPROVED",
  "validator_results": [
    {"validator": "SignalAgeValidator",   "approved": true,  "reason": "age 0.3s < 30s limit"},
    {"validator": "KillSwitchValidator", "approved": true,  "reason": "kill switch is OFF"},
    {"validator": "PositionValidator",   "approved": true,  "reason": "4 positions < 8 max"},
    ...all 11 validators...
  ],
  "timestamp": "2026-04-24T03:46:04.123Z"
}
```

Path: `s3://quantembrace-{env}-logs/risk-audit/{YYYY-MM-DD}/{risk_decision_id}.json`

**Key files:**
- [services/risk_engine/service.py](../services/risk_engine/service.py) — main service, validation pipeline
- [services/risk_engine/validators/](../services/risk_engine/validators/) — all 11 validators
- [configs/risk_limits_production.yaml](../configs/risk_limits_production.yaml) — limit thresholds

**What happens if this layer fails?** Trading halts entirely. By design. Signals pile up in the Kafka topic. When the risk engine restarts, it loads its state from DynamoDB and resumes processing from where Kafka left off.

---

## Layer 3 — Execution Engine

### What It Does

This layer is the "hands" of the system. It receives approved signals and translates them into real broker orders.

```
Kafka: signals.approved  →  [execution_engine]

  1. Verify signal has risk_decision_id (proof it passed the risk engine)
  2. Idempotency check: has this signal_id already been processed?
  3. Universe check: is this symbol approved for today in the current UNIVERSE_MODE?
  4. Route by market: NSE → Zerodha, US → Alpaca
  5. Place the order with retry (3 attempts, exponential backoff)
  6. Update DynamoDB: PENDING → PLACED → FILLED
  7. Publish fill event to Kafka: orders.events
```

### Trading Universe Enforcement

Every order (paper or live) must pass through the `UniverseOrderValidator` before reaching a broker. The validator checks the symbol against a daily immutable snapshot built at service start (and rebuilt at midnight IST via `_universe_snapshot_refresh_loop`).

Three universe modes (`UNIVERSE_MODE` env var):
- `PAPER_SAFE_START` — NIFTY 50 only (default for paper sessions)
- `PAPER_EXPAND` — NIFTY 100 + F&O eligible stocks
- `LIVE_ADVANCED` — Full NSE universe (live mode only)

Paper mode is permissive on stale snapshots; live mode blocks orders if no valid snapshot exists for today.

### Paper Trading Simulator

When `paper_trade=True` on a signal, the execution engine routes to a built-in simulator instead of the real broker. The simulator:
- Uses a deterministic SHA256 hash of the signal_id to decide fill outcomes (same signal always produces the same outcome — reproducible)
- Applies configurable slippage (default: 5 bps), spread (10 bps), and latency (250ms)
- Writes the same DynamoDB records and Kafka events as a real fill
- Never calls Zerodha or Alpaca

This means paper trading exercises the entire code path, not just the strategy.

### Idempotency — The Critical Detail

What if the service crashes after placing an order but before writing to DynamoDB? On restart, the same signal would be re-processed from Kafka, and a duplicate order would be placed.

The solution: every signal has a UUID (`signal_id`) created at strategy generation time. Before placing any order:

```
1. Does DynamoDB already have an order with this signal_id?
   FILLED    → skip, already done
   PLACED    → skip, already placed, wait for fill event
   FAILED    → retry the placement
   Not found → write PENDING to DynamoDB, THEN place order
```

This guarantees exactly-once order placement regardless of restarts or Kafka redeliveries.

**Key files:**
- [services/execution_engine/service.py](../services/execution_engine/service.py) — main service, order routing
- [services/execution_engine/brokers/](../services/execution_engine/brokers/) — Zerodha and Alpaca adapters
- [services/execution_engine/paper_simulator.py](../services/execution_engine/paper_simulator.py) — deterministic paper fill simulator

**What happens if this layer fails?** Approved signals queue up in Kafka `signals.approved` (Kafka retains messages for 24h). When execution restarts, it reconciles with the broker to catch any orders placed before the crash, then resumes from the Kafka offset.

---

## Layer 6 — Infrastructure

### What It Does

Everything that supports the other five layers: compute, storage, networking, monitoring, secrets, and deployment — all defined in Terraform.

See [06_aws_infrastructure.md](06_aws_infrastructure.md) for the full details.

**Brief summary:**
- **Compute:** AWS EC2 ARM64 Auto Scaling Groups (c6g/t4g) — one ASG per service
- **Messaging:** Kafka MSK Serverless — SASL/OAUTHBEARER IAM auth, port 9098
- **State:** DynamoDB on-demand — orders, positions, risk-state, sessions, kill-switch
- **Data:** S3 — ticks, audit logs, ML model artifacts
- **Networking:** VPC, private subnets, VPC endpoints for S3 and DynamoDB
- **Monitoring:** CloudWatch Logs + Metrics + Alarms → SNS → email/SMS
- **CI/CD:** GitHub Actions → EC2 rolling deploy

---

## How Layers Communicate — Kafka Topics

Layers communicate exclusively through Kafka topics. No direct Python imports across service boundaries.

### Topic Map

| Topic | Producer | Consumer | Key | Purpose |
|---|---|---|---|---|
| `ticks.nse` | data_ingestion | strategy_engine | instrument symbol | NSE real-time tick stream |
| `ticks.us` | data_ingestion | strategy_engine | instrument symbol | US real-time tick stream |
| `signals.pending` | strategy_engine | ai_engine | instrument | Raw signals awaiting enrichment |
| `signals.enriched` | ai_engine | risk_engine | instrument | ML-enriched signals awaiting risk validation |
| `signals.approved` | risk_engine | execution_engine | instrument | Risk-approved signals ready for execution |
| `orders.events` | execution_engine | risk_engine | order_id | Fill events for position tracking |
| `risk.kill-switch` | risk_engine | execution_engine, risk_engine | — | Kill switch activate/clear events |
| `signals.enriched.retry` | risk_engine (on failure) | KafkaRetryReplayer | — | Signals awaiting retry |
| `signals.enriched.dlq` | KafkaRetryReplayer (max retries) | — (manual inspection) | — | Dead letter queue |

### Consumer Groups

| Consumer Group | Consumes | Service |
|---|---|---|
| `strategy-v1` | `ticks.nse`, `ticks.us` | strategy_engine |
| `aiengine-v1` | `signals.pending` | ai_engine |
| `risk-v1` | `signals.enriched` (+ `signals.pending` in fallback) | risk_engine |
| `execution-v1` | `signals.approved` | execution_engine |
| `execution-v1-kill-switch` | `risk.kill-switch` | execution_engine |
| `risk-v1-order-events` | `orders.events` | risk_engine |

Consumer group versioning matters: if a schema-breaking change is deployed, increment the group version (e.g., `risk-v2`) to start reading from the beginning of the topic without affecting other consumers.

### Why Kafka Instead of SQS?

- **Message ordering:** Kafka partitions guarantee that all messages for the same instrument (keyed by symbol) are processed in order by the same consumer thread. SQS FIFO queues offer this too, but only per MessageGroupId, with lower throughput.
- **Replay and audit:** Kafka retains messages for a configurable period (24h+). A failed consumer can replay from any offset without re-inserting messages. SQS messages are deleted after processing.
- **Consumer groups:** Multiple independent consumers can read the same topic with independent offsets. The `EnrichmentWatchdog` uses this to monitor lag on `signals.pending` without interfering with the ai_engine's reading.
- **MSK Serverless pricing:** No idle cluster cost when markets are closed. Perfect for a trading system that only needs Kafka during market hours.

---

## The Golden Rule — No Layer Skipping

```
✅ CORRECT:
   Strategy → AI enrichment → Risk → Execution

❌ CRITICAL DEFECT:
   Strategy ────────────────────────────────► Execution  (bypassing AI and risk)
   Strategy → Risk ─────────────────────────► Execution  (bypassing AI enrichment)
   Execution imports strategy code
   Strategy checks broker margin
   Risk modifies signal content (it can only approve or reject)
```

If you find code that violates this, it is a critical defect. Open a GitHub issue immediately.

The pre-commit hook `hooks/no_duplicate_services.yaml` enforces import boundaries automatically.

---

## Failure Modes and Resilience

| What Fails | Immediate Effect | Recovery |
|---|---|---|
| `data_ingestion` (WebSocket drops) | No new ticks on Kafka. Strategies see stale prices. | CloudWatch alarm fires after 60s. EC2 ASG health check restarts the process. Reconnects with exponential backoff. |
| `strategy_engine` crashes | No new signals on `signals.pending` | ASG auto-replaces instance. Strategy state restored from DynamoDB. Kafka consumer resumes from last committed offset. |
| `ai_engine` lags | `EnrichmentWatchdog` activates fallback path | Trading continues on `signals.pending` → risk_engine directly. Alert fired. ASG auto-replaces unhealthy instance. |
| `risk_engine` crashes | All trading halts. Signals queue on Kafka. | ASG restarts. Kill switch state loaded from DynamoDB. Kafka consumer resumes from last committed offset. |
| `execution_engine` crashes | Approved signals queue on `signals.approved` | ASG restarts. Startup reconciliation checks broker for in-flight orders. Kafka consumer resumes. |
| Zerodha API down | NSE orders rejected. US trading continues. | Retry 3x with backoff. Circuit breaker opens. Alert fired. |
| MSK Kafka unreachable | All services retry with exponential backoff. | Services stay running, reconnect automatically. No manual restart needed. |
| Kill switch activated | All new signals rejected immediately. | Manual deactivation by operator only. `make kill-switch-off` or `python scripts/kill_switch_cli.py deactivate`. |
| DynamoDB throttle | Risk state reads slow. | On-demand capacity auto-scales. VPC endpoint prevents NAT bottleneck. |

---

*Last updated: 2026-05-15 | Update this document when: a new layer is added, a service is split or merged, or Kafka topic names or consumer groups change.*
