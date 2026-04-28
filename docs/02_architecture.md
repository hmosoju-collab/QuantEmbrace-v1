# QuantEmbrace — Architecture Deep-Dive

> **Prerequisite:** Read [01_introduction.md](01_introduction.md) first if you haven't already.

This document explains the 6-layer architecture of QuantEmbrace. For each layer we cover:
- What it does in plain English
- Why it exists as a separate layer
- What happens when it fails
- The key files and code

---

## Table of Contents

1. [The 6-Layer Model — Overview](#the-6-layer-model--overview)
2. [Layer 1 — Data Ingestion](#layer-1--data-ingestion)
3. [Layer 2 — Strategy Engine](#layer-2--strategy-engine)
4. [Layer 3 — Execution Engine](#layer-3--execution-engine)
5. [Layer 4 — Risk Engine (Critical)](#layer-4--risk-engine-critical)
6. [Layer 5 — AI/ML Engine](#layer-5--aiml-engine)
7. [Layer 6 — Infrastructure](#layer-6--infrastructure)
8. [How Layers Communicate](#how-layers-communicate)
9. [The Golden Rule — No Layer Skipping](#the-golden-rule--no-layer-skipping)
10. [Failure Modes and Resilience](#failure-modes-and-resilience)

---

## The 6-Layer Model — Overview

Think of the system as a pipeline with strict one-way flow:

```
                    RAW MARKET DATA
                         │
                         ▼
          ┌──────────────────────────┐
          │   LAYER 1: DATA          │  ← Collects & stores prices
          │   data_ingestion/        │
          └──────────────┬───────────┘
                         │ normalized ticks (via SQS)
                         ▼
          ┌──────────────────────────┐
          │   LAYER 2: STRATEGY      │  ← Decides "should we trade?"
          │   strategy_engine/       │
          └──────────────┬───────────┘
                         │ signals (via SQS)
                         ▼
          ┌──────────────────────────┐
          │   LAYER 4: RISK    ◄◄◄  │  ← Validates every signal  ◄── CRITICAL GATE
          │   risk_engine/           │      No bypass exists
          └──────────────┬───────────┘
                         │ approved signals only (via SQS)
                         ▼
          ┌──────────────────────────┐
          │   LAYER 3: EXECUTION     │  ← Places orders with brokers
          │   execution_engine/      │
          └──────────────┬───────────┘
                         │ broker API calls (HTTPS)
                    ┌────┴────┐
                    ▼         ▼
               Zerodha     Alpaca
               (NSE)       (US)
                    
          ┌──────────────────────────┐
          │   LAYER 5: AI/ML         │  ← Enriches signals with ML predictions
          │   ai_engine/             │    (feeds into Layer 2)
          └──────────────────────────┘

          ┌──────────────────────────┐
          │   LAYER 6: INFRA         │  ← AWS services, Terraform, monitoring
          │   infra/                 │    (supports ALL layers above)
          └──────────────────────────┘
```

Notice that **Layer 3 (Execution) is numbered 3 but sits after Layer 4 (Risk)** in the actual flow. The numbering is by conceptual importance, not flow order. The flow is: Data → Strategy → **Risk** → Execution.

---

## Layer 1 — Data Ingestion

### What It Does

This layer is the "eyes" of the system. It watches prices in real time and feeds them to everything else.

```
Zerodha WebSocket ──┐
                    ├──► Normalize ──► DynamoDB (latest prices, hot cache)
Alpaca WebSocket ───┘                 S3 (historical ticks, cold storage)
                                      SQS (stream to Strategy Engine)
```

### Two Separate Services

Data ingestion runs as **two independent ECS Fargate tasks**:

| Service | Market | Source | Hours |
|---|---|---|---|
| `data-ingestion-nse` | NSE India | Zerodha Kite Ticker WebSocket | 09:00–16:00 IST |
| `data-ingestion-us` | US Equities | Alpaca WebSocket | 13:00–21:30 IST |

**Why two services instead of one?** Markets trade at completely different times, have different APIs, different reconnection behavior, and different rate limits. If the NSE feed crashes, US trading should continue unaffected. Independent failure domains.

### Data Normalization

Every price tick from either broker gets converted into a single unified format before storage:

```python
# Zerodha sends this (broker-specific):
{
  "instrument_token": 738561,
  "last_price": 2453.50,
  "volume": 1234567,
  "ohlc": {"open": 2440.0, "high": 2460.0, "low": 2435.0, "close": 2453.5}
}

# Alpaca sends this (completely different format):
{
  "T": "t",
  "S": "AAPL",
  "p": 182.15,
  "s": 100,
  "t": "2026-04-24T14:30:00.123456789Z"
}

# After normalization — BOTH become this unified MarketTick:
MarketTick(
    market="NSE",                    # or "US"
    instrument="NSE:RELIANCE",       # or "US:AAPL"
    ltp=Decimal("2453.50"),
    volume=1234567,
    timestamp=datetime(2026, 4, 24, 3, 45, 0, tzinfo=UTC),  # Always UTC
    raw={...}                        # Original payload kept for debugging
)
```

Downstream services (strategy, risk) never see broker-specific formats. They only deal with `MarketTick`.

### Storage

| Store | What goes there | Why |
|---|---|---|
| **DynamoDB** `latest-prices` | Current LTP for each instrument | Sub-millisecond reads, strategy needs latest price fast |
| **S3** `quantembrace-market-data-history` | All historical ticks as Parquet | Cheap long-term storage, read during backtesting and ML training |

Key files:
- `services/data_ingestion/connectors/zerodha_connector.py` — Kite WebSocket handler
- `services/data_ingestion/connectors/alpaca_connector.py` — Alpaca WebSocket handler
- `services/data_ingestion/processors/tick_processor.py` — Normalization logic
- `services/data_ingestion/storage/s3_writer.py` — Writes Parquet to S3
- `services/data_ingestion/storage/dynamo_writer.py` — Updates DynamoDB latest prices

**What happens if this layer fails?** Strategies stop receiving fresh data. Strategy engine sees stale prices and stops generating signals. Trading pauses safely — no stale signals reach execution. CloudWatch alarm fires if WebSocket is disconnected for > 60 seconds.

---

## Layer 2 — Strategy Engine

### What It Does

This layer is the "brain" of the system. It receives market data and decides whether a trading opportunity exists.

```
Market Data (from SQS / DynamoDB)
         │
         ▼
   ┌─────────────────────────────────────┐
   │  Strategy Engine                    │
   │                                     │
   │  ┌─────────────────────────────┐   │
   │  │ MomentumStrategy            │   │
   │  │  - tracks price history     │   │
   │  │  - computes moving averages │   │
   │  │  → BUY signal if crossover  │   │
   │  └─────────────────────────────┘   │
   │                                     │
   │  ┌─────────────────────────────┐   │
   │  │ MeanReversionStrategy       │   │
   │  │  - tracks z-score           │   │
   │  │  → BUY if oversold (z < -2) │   │
   │  └─────────────────────────────┘   │
   │                                     │
   │  (more strategies can be added)    │
   └─────────────────┬───────────────────┘
                     │
                     ▼ Signal object (via SQS)
              ┌──────────────┐
              │ Risk Engine  │  (NOT directly to execution!)
              └──────────────┘
```

### What a Signal Looks Like

A signal is a structured object — not just "buy RELIANCE". It carries everything the system needs:

```python
Signal(
    signal_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",  # UUID, unique forever
    strategy_name="nse_momentum_v1",
    market="NSE",
    instrument="NSE:RELIANCE",
    direction=Direction.BUY,
    quantity=20,
    order_type=OrderType.MARKET,
    limit_price=None,               # Only for LIMIT orders
    stop_price=Decimal("2420.00"),  # Stop-loss level
    confidence=0.75,                # 0.0 = low, 1.0 = high conviction
    metadata={"reason": "golden_cross", "short_ma": 2420, "long_ma": 2400},
    created_at=datetime(2026, 4, 24, 4, 0, 0, tzinfo=UTC)
)
```

### The Strategy Interface

All strategies implement the same interface (a "contract"). This makes them swappable:

```python
class BaseStrategy(ABC):
    def on_tick(self, tick: MarketTick) -> Optional[Signal]:
        """Called for every price tick. Return a Signal or None."""
        
    def on_bar(self, bar: OHLCV) -> Optional[Signal]:
        """Called when a completed bar (1m, 5m, etc.) is formed."""
        
    def get_parameters(self) -> dict:
        """Return current parameters — used for logging and audit."""
```

If you want to add a new strategy, you implement this interface and register it. No other code changes needed.

### What This Layer Must NEVER Do

- ❌ Call broker APIs directly (that's execution's job)
- ❌ Check account balances or margins (that's risk's job)
- ❌ Cancel or modify orders (that's execution's job)
- ❌ Track P&L (that's risk's job)

Key files:
- `services/strategy_engine/strategies/base_strategy.py` — Interface all strategies implement
- `services/strategy_engine/strategies/momentum_strategy.py` — Momentum strategy implementation
- `services/strategy_engine/signals/signal.py` — Signal data model
- `services/strategy_engine/backtesting/backtester.py` — Replay historical data for testing

**What happens if this layer fails?** No new signals are generated. No new orders are placed. Existing positions are unaffected (risk and execution don't depend on strategy being alive).

---

## Layer 3 — Execution Engine

### What It Does

This layer is the "hands" of the system. It translates approved trade decisions into actual broker orders.

```
Approved Signal (from Risk Engine via SQS)
         │
         ▼
   ┌─────────────────────────────────────┐
   │  Execution Engine                   │
   │                                     │
   │  1. Is this a duplicate? → skip     │
   │  2. Which broker?                   │
   │     market=NSE → Zerodha            │
   │     market=US  → Alpaca             │
   │  3. Place order via broker API      │
   │  4. On failure → retry (max 3x)     │
   │  5. Update DynamoDB with status     │
   │  6. Track fills as they come in     │
   └─────────────────────────────────────┘
         │
    ┌────┴────┐
    ▼         ▼
 Zerodha    Alpaca
 KiteAPI    REST API
```

### Idempotency — The Most Important Engineering Detail

What happens if the service crashes right after placing an order but before recording it to DynamoDB? The service restarts, replays the signal from SQS, and tries to place the order again. Without idempotency checks, you'd get a **duplicate order**.

The solution: every order has a UUID (`signal_id`) that's created when the strategy generates the signal. Before placing any order:

```
1. Check DynamoDB: does an order with this signal_id already exist?
   YES and status = FILLED    → skip, already done
   YES and status = PLACED    → skip, already placed, wait for fill
   YES and status = FAILED    → retry with the SAME signal_id
   NO                         → create record with status=PENDING, then place
```

This guarantees: no matter how many times the system restarts, each signal results in exactly one order.

### Smart Order Routing

The execution engine uses an **adapter pattern** — each broker has its own adapter that translates internal order types to broker-specific ones:

```
Internal Order: BUY NSE:RELIANCE 20 shares MARKET
         │
         ▼ market=NSE
ZerodhaAdapter.place_order()
  → kite.place_order(
        variety="regular",
        exchange="NSE",
        tradingsymbol="RELIANCE",
        transaction_type="BUY",
        quantity=20,
        product="MIS",           # Intraday
        order_type="MARKET"
    )
```

```
Internal Order: BUY US:AAPL 10 shares LIMIT $182.10
         │
         ▼ market=US
AlpacaAdapter.place_order()
  → api.submit_order(
        symbol="AAPL",
        qty=10,
        side="buy",
        type="limit",
        time_in_force="day",
        limit_price=182.10
    )
```

### Retry Logic

Transient failures (network timeout, broker server 500 error) are retried with exponential backoff:

```
Attempt 1 → fails (timeout) → wait 1 second
Attempt 2 → fails (500)     → wait 2 seconds
Attempt 3 → fails           → GIVE UP, mark order FAILED, alert via SNS
```

Non-retryable errors (insufficient margin, invalid symbol, auth expired) fail immediately without retry.

Key files:
- `services/execution_engine/service.py` — Main service, order routing, SQS consumer
- `services/execution_engine/brokers/zerodha_broker.py` — Zerodha adapter
- `services/execution_engine/brokers/alpaca_broker.py` — Alpaca adapter
- `services/execution_engine/orders/order_manager.py` — DynamoDB order tracking
- `services/execution_engine/retry/retry_handler.py` — Retry with backoff

**What happens if this layer fails?** Approved signals pile up in the SQS queue (SQS retains messages for up to 14 days). When execution restarts, it processes the queued signals. It reconciles with broker to catch any orders that were placed before the crash.

---

## Layer 4 — Risk Engine (Critical)

> **This is the most important layer. Read this section carefully.**

### What It Does

The Risk Engine is the gatekeeper. It sits between Strategy and Execution and has the authority to reject any signal for any reason. There is no way to bypass it.

```
Strategy says: "BUY RELIANCE 1000 shares" (very large position)

Risk Engine checks:
  Kill switch active?                         → NO, continue
  Max position size (5% portfolio)?           → 1000 × ₹2,453 = ₹24.5 lakh
                                                Portfolio = ₹10 lakh
                                                24.5L / 10L = 245% — WAY OVER LIMIT
  Decision: REJECTED — position_size_exceeded
  
Signal dies here. Execution engine never sees it.
```

### The 7 Risk Checks (Executed in This Order)

```
SIGNAL ARRIVES
     │
     ▼
┌────────────────────────┐
│ 1. Kill Switch Check   │  Is kill switch ON? → REJECT all immediately
└────────────┬───────────┘
             ▼ (only if kill switch is OFF)
┌────────────────────────┐
│ 2. Position Limit      │  Too many open positions? → REJECT
└────────────┬───────────┘
             ▼
┌────────────────────────┐
│ 3. Exposure Check      │  Too much capital at risk total? → REJECT
└────────────┬───────────┘
             ▼
┌────────────────────────┐
│ 4. Stop-Loss Check     │  No stop-loss defined? → ADD DEFAULT STOP-LOSS
└────────────┬───────────┘
             ▼
┌────────────────────────┐
│ 5. Drawdown Check      │  Daily P&L loss too large? → REJECT + ACTIVATE KILL SWITCH
└────────────┬───────────┘
             ▼
┌────────────────────────┐
│ 6. Instrument Limits   │  Too much in one stock? → REJECT
└────────────┬───────────┘
             ▼
┌────────────────────────┐
│ 7. Margin Check        │  Enough margin in broker account? → REJECT if insufficient
└────────────┬───────────┘
             ▼
         APPROVED
     (sent to execution)
```

**If any check fails, the pipeline short-circuits.** Checks 3-7 are skipped after any rejection.

### The Kill Switch

The kill switch is a single boolean in DynamoDB that, when `true`, causes the Risk Engine to reject every single signal immediately, without running any other checks.

It can be triggered three ways:
- **Manually** — an authorized operator calls the kill switch API (e.g. "market is crashing, stop everything")
- **Automatically** — the drawdown check finds that daily loss has exceeded the configured threshold
- **Via CloudWatch alarm** — an infrastructure alert (e.g. broker API error rate too high) triggers it

When the kill switch activates:
1. All in-flight signals are rejected
2. All open orders are cancelled (best-effort)
3. Positions are held (not force-closed — that could cause more loss during a crash)
4. SNS alert is sent to the operator
5. Kill switch stays ON until manually deactivated for the next trading day

### Every Decision is Logged

Every risk decision — approve or reject — is written to S3 as a JSON audit record:

```json
{
    "risk_decision_id": "abc-789-xyz",
    "signal_id": "f47ac10b-...",
    "status": "REJECTED",
    "reason": "daily_loss_limit_exceeded",
    "validator_results": [
        {"validator_name": "KillSwitchValidator", "approved": true},
        {"validator_name": "PositionValidator", "approved": true},
        {"validator_name": "ExposureValidator", "approved": true},
        {"validator_name": "DailyLossValidator", "approved": false, 
         "reason": "daily_loss_3.2%_exceeds_limit_3.0%"}
    ],
    "timestamp": "2026-04-24T06:30:00.000Z"
}
```

This is stored at: `s3://quantembrace-trading-logs/risk-audit/2026-04-24/{risk_decision_id}.json`

Key files:
- `services/risk_engine/service.py` — Main service, validation pipeline
- `services/risk_engine/killswitch/killswitch.py` — Kill switch logic
- `services/risk_engine/validators/position_validator.py` — Position size checks
- `services/risk_engine/validators/exposure_validator.py` — Portfolio exposure checks
- `services/risk_engine/validators/loss_validator.py` — Daily drawdown checks
- `services/risk_engine/limits/risk_limits.py` — Configurable limit thresholds

**What happens if this layer fails?** Trading halts entirely. This is by design. The system is fail-safe, not fail-open. Strategy signals pile up in SQS. When risk engine restarts, it loads its state from DynamoDB and resumes.

---

## Layer 5 — AI/ML Engine

### What It Does

This layer provides machine-learning-based enrichment to improve strategy signal quality. It's intentionally lightweight — models augment human-designed strategies, they don't replace them.

```
Nightly batch (ECS task):
  S3 historical ticks → Feature computation → Feature store (S3)
  Feature store → Model training → Model registry (S3)

During trading (in-process, no network hop):
  StrategyEngine calls ai_engine.predict(features)
  → returns: {"volatility_estimate": 0.023, "market_regime": "trending"}
  Strategy uses this to scale position size or filter signals
```

### Design Choices — Why So Simple?

- **No SageMaker:** Model training happens offline. Only inference runs in production. SageMaker is overkill and expensive for this scale.
- **No separate inference server:** The AI engine runs as a library inside the strategy engine process. No network round-trip for predictions.
- **ONNX/pickle models on S3:** Model artifacts are just files. No model serving infrastructure.
- **Two models only:** Volatility predictor (help size positions) + Regime classifier (trending vs. ranging market). More than this is over-engineering.

Key files:
- `services/ai_engine/features/feature_pipeline.py` — Computes ML features from historical ticks
- `services/ai_engine/models/model_registry.py` — Loads models from S3
- `services/ai_engine/inference/predictor.py` — Runs inference, returns predictions

---

## Layer 6 — Infrastructure

### What It Does

Everything that supports the other five layers: compute, storage, networking, monitoring, secrets, and deployment — all defined in Terraform.

See [06_aws_infrastructure.md](06_aws_infrastructure.md) for the full details.

Brief summary:
- **Compute:** AWS ECS Fargate (5 services, task-scheduled for market hours)
- **Storage:** S3 (historical data + logs) + DynamoDB (state + orders)
- **Networking:** VPC, private subnets, NAT Gateway, VPC endpoints
- **Monitoring:** CloudWatch Logs + Metrics + Alarms → SNS → Email/SMS
- **Secrets:** AWS Secrets Manager (API keys)
- **CI/CD:** GitHub Actions → ECR → ECS rolling deploy

---

## How Layers Communicate

Layers never call each other directly (no function imports across service boundaries). They communicate through AWS messaging:

| From → To | Transport | Why |
|---|---|---|
| Data Ingestion → Strategy | SQS queue | Decoupled, buffered, survives restarts |
| Strategy → Risk | SQS FIFO queue | FIFO preserves signal order per symbol, deduplication built-in |
| Risk → Execution | SQS FIFO queue | Same — order matters, idempotency built-in |
| All layers → Storage | DynamoDB / S3 direct write | Each layer owns its tables |
| AI/ML → Strategy | In-process function call | Same process, no latency |
| Monitoring | CloudWatch | Each service emits structured logs and custom metrics |

The SQS FIFO queue between Risk and Execution is important: if two signals arrive for the same symbol, they're processed in order (first-in, first-out), preventing race conditions.

---

## The Golden Rule — No Layer Skipping

```
✅ CORRECT:   Strategy → Risk → Execution
❌ WRONG:     Strategy ──────────────────► Execution  (bypassing risk)
❌ WRONG:     Strategy → Risk & Execution (mixing concerns)
❌ WRONG:     Execution imports strategy code
❌ WRONG:     Strategy checks broker margins
```

If you ever find code that violates this, it's a critical defect. Open an issue immediately.

The `hooks/pre_trade_risk_validation.yaml` hook enforces this automatically by checking imports and code structure before any commit.

---

## Failure Modes and Resilience

| What Fails | Immediate Effect | Recovery |
|---|---|---|
| Data ingestion (NSE feed drops) | Strategy stops seeing NSE ticks, no new NSE signals | CloudWatch alarm → SNS alert. Service auto-restarts via ECS health check. Reconnects with exponential backoff. |
| Strategy engine crashes | No new signals generated | ECS restarts service. Strategy state restored from DynamoDB. SQS retains unprocessed ticks. |
| Risk engine crashes | All trading halts (SQS signals queue up) | ECS restarts service. Kill switch state loaded from DynamoDB. Processing resumes. |
| Execution engine crashes | Approved signals queue up in SQS | ECS restarts. Reconcile with broker on startup. Process queued signals. |
| Zerodha API down | NSE orders fail | Retry 3x, then mark FAILED and alert. Risk engine informed. US trading continues. |
| DynamoDB throttle | Risk state reads may slow | On-demand capacity auto-scales. VPC endpoint prevents NAT bottleneck. |
| S3 write fails | Audit log delayed | Non-critical for trading continuity. Retried async. Alert if >5 min behind. |
| Kill switch ON | All new orders blocked | Manual deactivation by operator only. By design. |

---

*Last updated: 2026-04-24 | Update this document whenever: a new layer is added, a service is split or merged, or inter-layer communication mechanism changes.*
