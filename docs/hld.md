# QuantEmbrace — High-Level Design (HLD)

> **Document scope:** System-level architecture. Explains WHAT the system does, WHY each major component exists, HOW components interact, and WHAT trade-offs were made. For internal implementation details (class design, message schemas, DynamoDB keys), see [lld.md](lld.md).

---

## Table of Contents

1. [System Purpose and Goals](#1-system-purpose-and-goals)
2. [Non-Functional Requirements](#2-non-functional-requirements)
3. [Six-Layer Architecture](#3-six-layer-architecture)
4. [Component Interaction Overview](#4-component-interaction-overview)
5. [Signal Flow: Tick to Fill](#5-signal-flow-tick-to-fill)
6. [Technology Stack and Rationale](#6-technology-stack-and-rationale)
7. [AWS Deployment Architecture](#7-aws-deployment-architecture)
8. [Data Architecture](#8-data-architecture)
9. [Messaging Architecture](#9-messaging-architecture)
10. [Security Model](#10-security-model)
11. [Resilience and Failure Model](#11-resilience-and-failure-model)
12. [Cost Model](#12-cost-model)
13. [Key Design Decisions](#13-key-design-decisions)
14. [Monitoring and Observability](#14-monitoring-and-observability)

---

## 1. System Purpose and Goals

QuantEmbrace is a **production-grade algorithmic trading platform** that automatically monitors live market data, generates trading signals through multiple strategies, validates each signal through a multi-layer risk engine, and executes approved orders on two broker platforms — without human intervention during trading hours.

### Business Goals

| Goal | How the System Meets It |
|---|---|
| Automate intraday trading across NSE India + US equities | 5 microservices operating continuously during market hours |
| Never lose more than 2% of capital in a day | Risk engine with kill switch, enforced as a hard gate with no bypass |
| Support multiple concurrent strategies | 6 pluggable strategies with independent circuit breakers |
| Run cost-efficiently on AWS | EC2 ARM64 Graviton instances, MSK Serverless, no over-provisioned resources |
| Scale from paper trading to live capital safely | Paper/live flag per strategy, 5-day validation gate before promotion |
| Full audit trail for every decision | `trace_id` from tick to fill, all decisions logged to S3 and CloudWatch |

### What the System Is NOT

- Not a high-frequency trading (HFT) system — targets 1-second to minute-level signals
- Not a strategy research tool — that is handled by the separate backtesting pipeline
- Not a UI-driven platform — operated via CLI commands and CloudWatch dashboards
- Not real-time sub-millisecond — broker API latency (50–200ms) dominates; code latency is not the bottleneck

---

## 2. Non-Functional Requirements

| Category | Requirement | Target |
|---|---|---|
| **Latency** | Tick-to-signal | < 100ms |
| **Latency** | Signal-to-execution (risk + order placement) | < 500ms |
| **Latency** | AI enrichment (P99) | < 15ms |
| **Throughput** | Ticks per second (NSE full mode) | ~500 tps (50 symbols × ~10 tps each) |
| **Throughput** | Signals per day | < 1,000 (typical); system handles 10,000 |
| **Availability** | During market hours | 99.9% (max 4.4 min/day downtime) |
| **Durability** | Order state (DynamoDB) | 99.999999999% (11 nines) |
| **Safety** | Duplicate order prevention | Zero tolerance — idempotency enforced at every layer |
| **Safety** | Daily loss enforcement | Kill switch fires at 2% loss; no bypass path exists |
| **Cost** | Monthly AWS spend (paper trading) | < ₹20,000/month ($250) |
| **Auditability** | Signal traceability | Every tick → fill traceable via single `trace_id` |

---

## 3. Six-Layer Architecture

The system is organized into six layers, each with a single responsibility. The layers are not optional — they form the backbone of the safety architecture. The critical rule is that information flows top-to-bottom; **no layer reaches back up** to a layer above it.

```
┌══════════════════════════════════════════════════════════════════════════════┐
│  LAYER 6: INFRASTRUCTURE                                                     │
│  AWS EC2 ARM64 ASGs | MSK Serverless Kafka | S3 | DynamoDB | CloudWatch     │
│  Terraform IaC | Multi-AZ | Scheduled Scaling | Secrets Manager             │
╞══════════════════════════════════════════════════════════════════════════════╡
│  LAYER 5: AI/ML ENGINE                                                       │
│  Regime Classification (HMM) | Signal Quality Scoring (GBT)                 │
│  Feature Store (DynamoDB) | Model Registry (S3) | Hot-Reload                │
╞══════════════════════════════════════════════════════════════════════════════╡
│  LAYER 4: RISK ENGINE  ◄──── THE CRITICAL GATE (no bypass exists) ─────────│
│  11 Validators in Sequence | Kill Switch | Position Limits | P&L Tracking   │
│  EnrichmentWatchdog Fallback | Audit Log | Daily Loss Enforcement           │
╞══════════════════════════════════════════════════════════════════════════════╡
│  LAYER 3: EXECUTION ENGINE                                                   │
│  Broker Adapters (Zerodha + Alpaca) | Order Lifecycle Management            │
│  Idempotent Submission | Fill Tracking | Paper Simulator                    │
╞══════════════════════════════════════════════════════════════════════════════╡
│  LAYER 2: STRATEGY ENGINE                                                    │
│  6 Pluggable Strategies | Circuit Breakers | Hot-Reload Config               │
│  Tick Path (Kafka) + Candle Path (DynamoDB) | Paper Trade Stamping          │
╞══════════════════════════════════════════════════════════════════════════════╡
│  LAYER 1: DATA INGESTION                                                     │
│  Zerodha Kite Ticker (NSE) | Alpaca WebSocket (US)                          │
│  Tick Normalization | Candle Stream | Feature Engine | S3 Historical Store   │
└══════════════════════════════════════════════════════════════════════════════┘
```

### Layer Responsibilities

**Layer 1 — Data Ingestion:** The only layer that speaks directly to external brokers for market data. Normalizes all incoming ticks into a unified `MarketTick` format so that all downstream layers are broker-agnostic. Also computes and writes OHLCV candles and technical indicator features to DynamoDB.

**Layer 2 — Strategy Engine:** Consumes normalized market data and applies trading logic to generate signals. Strategies are stateless functions operating on price windows. They never know about risk limits, margin, or how orders get placed. They output a structured `Signal` object and nothing more.

**Layer 3 — Execution Engine:** The only layer that speaks to brokers for order placement. It accepts only `ApprovedSignal` objects (signals that have been explicitly approved by the risk engine). It handles broker-specific quirks, retries, fill tracking, and reporting. It never generates signals or checks risk.

**Layer 4 — Risk Engine:** The gatekeeper. Sits physically between strategy and execution. Every signal must pass all 11 validators before reaching execution. If the risk engine is down, trading halts by design. It cannot be bypassed.

**Layer 5 — AI/ML Engine:** Enriches signals with ML predictions before they reach the risk engine. Adds regime classification (what kind of market environment we're in) and a quality score (how likely this signal is to be profitable). If it's slow or down, an `EnrichmentWatchdog` in the risk engine detects this and routes signals directly to risk validation with conservative defaults.

**Layer 6 — Infrastructure:** Terraform-managed AWS resources. Provides the compute (EC2), messaging (Kafka MSK), state (DynamoDB), storage (S3), and monitoring (CloudWatch) that all other layers depend on.

### The Golden Rule

```
Strategy logic NEVER touches execution or risk.
Risk logic NEVER modifies signals or places orders.
Execution logic NEVER generates signals or overrides risk.

Services communicate ONLY through Kafka topics.
No layer imports from another layer's internal code.
```

---

## 4. Component Interaction Overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│  EXTERNAL                                                                  │
│  Zerodha Kite WebSocket ────────┐                                          │
│  Alpaca WebSocket ──────────────┤                                          │
└──────────────────────────────────│─────────────────────────────────────────┘
                                   ▼
                    ┌──────────────────────────────┐
                    │      data_ingestion           │
                    │  Normalize → MarketTick       │
                    │  Build candles & features     │
                    └─────────────┬────────────────┘
                                  │
              ┌───────────────────┼────────────────────────────┐
              ▼                   ▼                            ▼
      Kafka: ticks.nse    Kafka: ticks.us          DynamoDB: candle-cache
      Kafka: ticks.us     (separate streams)       DynamoDB: features
              │
              ▼ (strategy-v1)
  ┌─────────────────────────────────────────────┐
  │           strategy_engine                    │
  │  Tick path: MomentumStrategy                │
  │  Candle path: ORB, Scalp1m, VWAP,           │
  │              IntradayTrend15m,               │
  │              PreCloseMomentum                │
  │  Circuit breaker per strategy               │
  │  Paper trade stamping from DynamoDB config  │
  └───────────────────┬─────────────────────────┘
                      │
              Kafka: signals.pending
                      │
         ┌────────────┴─────────────────┐
         ▼ (aiengine-v1)                │ (risk-v1-fallback, when AI lags)
  ┌──────────────────────┐             │
  │      ai_engine        │             │
  │  FeatureReader        │             │
  │  RegimeClassifier     │             │
  │  (HMM — 4 states)    │             │
  │  SignalQualityScorer  │             │
  │  (GBT — 0.0–1.0)     │             │
  └──────────┬───────────┘             │
             │                          │
     Kafka: signals.enriched            │
             │                          │
             ▼ (risk-v1)                │
  ┌──────────────────────────────────────────────────┐
  │                  risk_engine                      │
  │  EnrichmentWatchdog (monitors AI lag, routes)    │
  │  11 Validators: SignalAge → KillSwitch →         │
  │    Position → Exposure → DailyLoss →             │
  │    Margin → Slippage → Spread →                  │
  │    Sector → Liquidity → Quality                  │
  │  APPROVED → signals.approved                     │
  │  REJECTED → ops.audit only (no retry)            │
  └─────────────────────┬────────────────────────────┘
                        │
                Kafka: signals.approved
                        │
                        ▼ (execution-v1)
  ┌──────────────────────────────────────────────────┐
  │              execution_engine                     │
  │  DynamoDB idempotency check (no duplicate orders)│
  │  Smart routing: NSE → Zerodha, US → Alpaca       │
  │  paper_trade=True (NSE) → PaperSimulator (internal deterministic fill) │
  │  paper_trade=True (US) → Alpaca paper endpoint   │
  │  paper_trade=False → live broker                  │
  │  Fill tracking via BulkOrderPoller (300ms)        │
  └──────────┬──────────────────────────────────────┘
             │
    ┌────────┴────────┐
    ▼                 ▼
Zerodha API      Alpaca API
(NSE orders)     (US orders / paper)
    │                 │
    └────────┬─────────┘
             │
    Kafka: orders.events
             │
             ▼ (risk-v1-order-events)
    risk_engine: real-time P&L update
    DynamoDB: positions + orders updated

    kill.switch topic ───────────────────► ALL services (dedicated listener task)
    ops.audit topic ◄─────────────────── risk_engine + execution_engine
```

---

## 5. Signal Flow: Tick to Fill

The complete lifecycle of a single trade, from raw price arriving to broker order filled:

```
TIME    EVENT
──────────────────────────────────────────────────────────────────
t=0ms   Exchange sends tick: RELIANCE LTP = ₹2,453.50
        ↓
        data_ingestion receives via Zerodha WebSocket
        Normalizes to MarketTick, assigns trace_id (uuid4)
        Publishes to Kafka: ticks.nse (key=RELIANCE)
        ↓
t=5ms   strategy_engine consumes (strategy-v1)
        MomentumStrategy.on_tick() updates price window
        Short MA (10) crosses above Long MA (50) → bullish
        ↓
t=8ms   Signal generated:
        signal_id = sha256(strategy|RELIANCE|BUY|2453.5|...) [:32]
        confidence = 0.75, expires_at = +30s
        paper_trade = True (from DynamoDB strategy-config)
        Publishes to Kafka: signals.pending
        ↓
t=10ms  ai_engine consumes (aiengine-v1)
        FeatureReader reads RSI=62, EMA9>EMA21, ATR=0.018 from DynamoDB
        RegimeClassifier (HMM): regime = "trending", confidence = 0.81
        SignalQualityScorer (GBT): quality_score = 0.82
        EnrichedSignal published to Kafka: signals.enriched
        ↓
t=20ms  risk_engine consumes (risk-v1, reading signals.enriched)
        11 validators run in sequence:
          ✓ SignalAge: 20ms < 30s limit
          ✓ KillSwitch: OFF
          ✓ Position: 3 open < max 8
          ✓ Exposure: ₹49,070 < 5% portfolio limit
          ✓ DailyLoss: P&L = +₹8,200 (no drawdown)
          ✓ Margin: sufficient in Zerodha account
          ✓ Slippage: market not moved more than 0.15%
          ✓ Spread: 8 bps < 50 bps limit
          ✓ Sector: Energy sector within 20% limit
          ✓ Liquidity: order < 1% of ADV
          ✓ Quality: 0.82 > minimum threshold 0.30
        APPROVED. risk_decision_id = "rsk-abc-789"
        Published to Kafka: signals.approved
        ↓
t=35ms  execution_engine consumes (execution-v1)
        DynamoDB conditional check: signal_id not in orders table → proceed
        Routes to Zerodha (NSE instrument)
        paper_trade=True (NSE) → PaperSimulator (internal, deterministic fill)
        Order placed: BUY 20 RELIANCE @ MARKET
        DynamoDB: order_id=ord-123, status=PLACED
        ↓
t=185ms Zerodha (or paper simulator) fills the order
        Fill confirmed: 20 shares @ ₹2,454.00
        DynamoDB: status=FILLED, avg_price=₹2,454.00
        Published to Kafka: orders.events
        ↓
t=190ms risk_engine (risk-v1-order-events) consumes fill
        Position table updated: +20 RELIANCE @ ₹2,454.00
        P&L tracking updated with new position
        ↓
DONE    Total: ~190ms tick-to-fill (paper simulation)
        Trace query: CloudWatch Logs Insights on trace_id shows full journey
```

---

## 6. Technology Stack and Rationale

### Language: Python 3.11+

Chosen because the entire quant/ML ecosystem (numpy, pandas, scikit-learn, PyTorch, kiteconnect SDK, alpaca-trade-api) is Python-first. Our latency target is seconds-to-minutes, not microseconds — the broker API latency (50–200ms) dominates. Python's async/await model (`asyncio`) handles concurrent I/O-bound operations (WebSocket feeds, broker API calls, DynamoDB queries) efficiently.

### Messaging: Kafka MSK Serverless

| Requirement | Why Kafka, not SQS |
|---|---|
| Multiple independent consumers | Kafka consumer groups — risk engine + AI engine both read `signals.pending` independently without competing. SQS would require SNS fan-out + duplicate queues |
| Message replay | A restarted service re-reads from its last committed offset. SQS messages are deleted after one consumer reads them |
| Ordered delivery per symbol | Kafka partitioning by `instrument_id` guarantees ordered processing per symbol — critical for position tracking |
| Tracing across services | `trace_id` flows through Kafka message headers from tick to fill — single query reconstructs the full lifecycle |

**MSK Serverless specifically** because there is no cluster to provision, size, or maintain. Cost scales with actual usage. Zero cost when markets are closed overnight.

### Compute: EC2 ARM64 ASGs

AWS Graviton3 ARM64 instances (c6g/t4g) are 20–40% cheaper than equivalent x86 instances for the same performance profile. All services run as systemd units on dedicated ASGs (one ASG per service), with scheduled scaling to zero outside market hours.

**Not ECS Fargate** because:
- Fargate has a cold start latency of 30–90 seconds — too slow for post-crash recovery
- WebSocket connections (Zerodha Kite Ticker) require persistent processes; Fargate task lifecycle management adds complexity
- EC2 instance profiles give direct IAM access without ECS task role orchestration
- 20–30% cost reduction at our compute profile

**Not Lambda** because Lambda's 15-minute maximum execution time and cold start latency are incompatible with persistent WebSocket connections and continuous data processing.

### State: AWS DynamoDB

DynamoDB is the system's fast-path state store. It holds:
- Live order status (updated on every state change)
- Current open positions (read on every risk check)
- Kill switch state (read with `ConsistentRead=True` on every signal)
- Candle cache (written by data_ingestion, read by strategy_engine at 500ms intervals)
- Feature store (pre-computed indicators for AI enrichment)

On-demand capacity mode is used for all tables — the trading workload is bursty at market open and close, with periods of inactivity overnight.

### Storage: AWS S3

S3 is the system's cold-path store. Historical ticks, OHLCV data, audit logs, and ML model artifacts live here. Never queried in the real-time trading path — only for backtesting, compliance, and model training.

### ML Models: joblib (HMM + GBT)

The regime classifier uses a Hidden Markov Model (HMM) trained offline on historical market regimes. The quality scorer uses a Gradient Boosted Trees (GBT) classifier trained on historical signal outcomes. Both are serialized as joblib artifacts stored in S3, hot-reloaded every 60 seconds by the ai_engine's `ModelRegistry`.

Chosen over deep learning because:
- HMM is interpretable for regime detection — operators can explain why a regime was classified
- GBT handles tabular features (RSI, ATR, MACD, etc.) without feature engineering overhead
- Both are fast at inference: < 5ms per signal

---

## 7. AWS Deployment Architecture

### Region and Availability Zones

Primary region: `ap-south-1` (Mumbai) — chosen for proximity to NSE exchange servers (Zerodha's infrastructure is in Mumbai). US trading latency to Alpaca (Virginia) is acceptable at ~200ms over the public internet.

Two Availability Zones: `ap-south-1a` and `ap-south-1b` for high availability.

### Network Topology

```
┌─────────────────────────────── AWS VPC ──────────────────────────────────┐
│                                                                           │
│  ┌─── Public Subnet (1a) ─────┐   ┌─── Public Subnet (1b) ────────────┐ │
│  │  NAT Gateway                │   │  NAT Gateway (backup)              │ │
│  │  (outbound to broker APIs)  │   │                                    │ │
│  └─────────────────────────────┘   └────────────────────────────────────┘ │
│                                                                           │
│  ┌─── Private Subnet (1a) ─────────────────────────────────────────────┐ │
│  │  EC2: data_ingestion_nse ASG   (t4g.medium)                          │ │
│  │  EC2: data_ingestion_us ASG    (t4g.medium)                          │ │
│  │  EC2: strategy_engine ASG      (c6g.large)                           │ │
│  │  EC2: risk_engine ASG          (c6g.large)                           │ │
│  │  EC2: execution_engine ASG     (c6g.xlarge)                          │ │
│  │  EC2: ai_engine ASG            (c6g.large)                           │ │
│  └─────────────────────────────────────────────────────────────────────┘ │
│                                                                           │
│  ┌─── MSK Serverless (Kafka) ──────────────────────────────────────────┐ │
│  │  VPC connectivity via private subnets                                 │ │
│  │  SASL/OAUTHBEARER IAM auth, port 9098                                │ │
│  └─────────────────────────────────────────────────────────────────────┘ │
│                                                                           │
│  VPC Endpoints (bypass NAT Gateway for hot-path):                        │
│    → S3 Gateway Endpoint                                                  │
│    → DynamoDB Gateway Endpoint                                            │
│    → CloudWatch Logs Interface Endpoint                                   │
└───────────────────────────────────────────────────────────────────────────┘
```

### ASG Sizing and Scheduling

| Service | Instance | Min | Max | Scale to 0 |
|---|---|---|---|---|
| data_ingestion_nse | t4g.medium | 1 | 1 | 15:35 IST (after NSE close) |
| data_ingestion_us | t4g.medium | 1 | 1 | 03:00 IST (after US close) |
| strategy_engine | c6g.large | 1 | 2 | Overnight |
| risk_engine | c6g.large | 1 | 1 | Overnight |
| execution_engine | c6g.xlarge | 1 | 1 | Never (needed for reconciliation) |
| ai_engine | c6g.large | 1 | 2 | Overnight |

**Execution engine never scales to 0** — it needs to be running for emergency order cancellations and post-close position reconciliation even outside market hours.

### Scheduled Scaling Actions

```
Pre-NSE open:   08:30 IST → scale all services to min=1
NSE close:      15:35 IST → scale data_ingestion_nse to min=0
Pre-US open:    18:30 IST → scale data_ingestion_us to min=1
US close:       03:00 IST → scale all non-execution services to min=0
Weekend:        Friday 03:00 IST → all ASGs to 0 except execution_engine
Monday:         08:30 IST → resume full schedule
```

### Deployment Model

Services are deployed as systemd units on EC2 instances. New versions are deployed via ASG **instance refresh** (rolling replacement of instances with the new AMI). Terraform manages the AMI ID, ASG configuration, and IAM roles. Zero-downtime deployments use a minimum healthy percentage of 50%.

---

## 8. Data Architecture

### Hot Path (Real-Time Trading)

```
Kafka Topics (MSK Serverless)
  → in-flight data between services
  → retention: 24h for ticks, 1h for signals, 30min for approved signals

DynamoDB Tables
  → current state: kill switch, positions, orders, candle cache, features
  → read with ConsistentRead=True for safety-critical reads (kill switch, positions)
```

### Cold Path (Historical / Compliance)

```
S3 Buckets
  → quantembrace-tick-data/     Parquet ticks, partitioned by market/symbol/date/hour
  → quantembrace-ohlcv-data/    Parquet OHLCV, partitioned by market/symbol/date
  → quantembrace-trading-logs/  JSON audit logs from risk + execution services
  → quantembrace-model-artifacts/ Trained ML models (joblib), features used in training

Lifecycle Policies
  → Standard → Glacier Instant Retrieval after 90 days (dev/staging) / 365 days (prod)
  → Glacier → Delete after 3 years (compliance retention)
```

### Data Ownership

Each service owns the data it writes. No service reads another service's internal DynamoDB state directly:

| Service | Owns (writes) | Reads |
|---|---|---|
| data_ingestion | ticks.nse, ticks.us, candle-cache, features, S3 ticks | Broker WebSocket feeds |
| strategy_engine | signals.pending, strategy-state | ticks.nse, ticks.us, candle-cache, strategy-config |
| ai_engine | signals.enriched, regime-log, strategy-recommendations | signals.pending, features, S3 models |
| risk_engine | signals.approved, risk-state, ops.audit, S3 audit logs | signals.enriched (or signals.pending), orders.events, risk-state |
| execution_engine | orders, positions, orders.events, ops.audit | signals.approved, DynamoDB orders |

---

## 9. Messaging Architecture

### Topic Map

```
Kafka Topics (MSK Serverless, ap-south-1)
─────────────────────────────────────────────────────────────────────────
ticks.nse         4 partitions  24h retention  key=instrument_id
ticks.us          2 partitions  24h retention  key=instrument_id
signals.pending   3 partitions  1h retention   key=instrument_id
signals.enriched  3 partitions  1h retention   key=symbol
signals.approved  3 partitions  30min retention key=instrument_id
orders.events     4 partitions  7d retention   key=instrument_id
risk.kill-switch  1 partition   30d retention  key=GLOBAL
ops.audit         2 partitions  90d retention  key=trace_id

Each topic also has: <topic>.retry (same config) + <topic>.dlq (same config)
```

### Consumer Groups and What They Read

| Consumer Group | Service | Reads | Purpose |
|---|---|---|---|
| strategy-v1 | strategy_engine | ticks.nse, ticks.us | Run tick strategies on every price update |
| aiengine-v1 | ai_engine | signals.pending | Enrich signals with ML predictions |
| risk-v1 | risk_engine | signals.enriched | Primary risk validation path |
| risk-v1-fallback | risk_engine | signals.pending | Activated when AI engine lags |
| execution-v1 | execution_engine | signals.approved | Execute approved signals |
| execution-v1-kill-switch | execution_engine | risk.kill-switch | Dedicated kill switch listener |
| risk-v1-order-events | risk_engine | orders.events | Real-time position and P&L updates |

### Message Schema Versions

All Kafka messages use a versioned JSON envelope:

- **v3.0** — used by ticks.*, signals.pending, signals.approved. Core fields: `event_id`, `trace_id`, `event_type`, `schema_version`, `source`, `published_time`.
- **v4.0** — used by signals.enriched. Extends v3.0 with enrichment fields: `regime`, `regime_confidence`, `quality_score`, `filtered`, `enrichment_latency_ms`, `model_versions`.

The `trace_id` is set once (at tick origin) and propagated unchanged through every downstream message, enabling end-to-end trace reconstruction.

### EnrichmentWatchdog — AI Fallback Routing

The `EnrichmentWatchdog` (running inside risk_engine) monitors the AI engine's consumer group lag every 500ms:

```
Normal:         lag(aiengine-v1) ≈ 0
                → risk_engine reads signals.enriched (risk-v1)

Degraded:       lag(aiengine-v1) ≥ threshold for 2 consecutive checks
                → risk_engine switches to signals.pending (risk-v1-fallback)
                → quality_score defaults to 0.5, regime defaults to "unknown"
                → all 11 validators still run normally
                → trading continues safely

Recovery:       lag(aiengine-v1) = 0 for 5 consecutive checks
                → risk_engine switches back to signals.enriched (risk-v1)
```

This means **the AI engine is optional for trading continuity**. The system degrades gracefully, never halts.

---

## 10. Security Model

### Authentication to AWS Services

All EC2 instances use **IAM Instance Profiles** (attached roles). No hardcoded credentials anywhere in code or configuration. Each service has a scoped IAM policy granting only the minimum permissions it needs:

- `data_ingestion` — Write to S3, Write to specific DynamoDB tables, Publish to Kafka topics
- `strategy_engine` — Read DynamoDB (candle-cache, strategy-config), Publish to Kafka
- `ai_engine` — Read S3 (model artifacts), Read DynamoDB (features), Publish to Kafka
- `risk_engine` — Read/Write DynamoDB (risk-state, positions), Read Kafka, Publish Kafka
- `execution_engine` — Read/Write DynamoDB (orders, positions), Read Kafka, Call broker APIs

### Kafka Authentication

MSK Serverless uses **SASL/OAUTHBEARER with IAM** on port 9098. The `aws-msk-iam-sasl-signer-python` library generates short-lived (1-hour) tokens using the EC2 instance's IAM role. No static credentials needed for Kafka access.

### Broker Credentials

Zerodha API key, secret, and session token are stored in **AWS Secrets Manager**. Alpaca API key and secret are also in Secrets Manager. Services read credentials at startup and on token refresh — never from environment variables in production.

### Network Isolation

All EC2 instances run in **private subnets** with no inbound internet access. Outbound connections to broker APIs go through a NAT Gateway. DynamoDB and S3 are accessed via **VPC Gateway Endpoints** (no NAT Gateway charges, no public internet path).

### Audit Trail

Every risk decision (approve or reject) is written to `ops.audit` Kafka topic and archived to S3 with a 90-day hot retention and 3-year Glacier archive. Every order is logged in DynamoDB with full state history. Every fill event is logged via `orders.events` Kafka topic. All logs carry the `trace_id` for correlation.

---

## 11. Resilience and Failure Model

### Fail Safe, Not Fail Open

Every failure mode halts or degrades trading — it does not allow untested signals through:

| Component Fails | What Happens |
|---|---|
| Risk engine down | Signals accumulate in `signals.pending`/`signals.enriched`. No order reaches execution. On restart, signals > 30s old are rejected by `SignalAge` validator. |
| AI engine down/slow | `EnrichmentWatchdog` detects lag within 2 checks (≈1s). Switches to fallback path with conservative defaults. Trading continues safely. |
| Execution engine down | Approved signals accumulate in `signals.approved`. On restart, signals > 30s old are rejected as stale by execution engine before placement. |
| Data ingestion down | No new ticks → strategy engine goes idle (no signals). Existing open positions are held at broker. |
| Strategy engine down | No new signals. Existing positions held. Risk and execution engines idle. |
| Kafka unavailable | Services use exponential backoff and retry. If Kafka is down for > 30s, kill switch activates automatically (WebSocket gap alarm triggers). |
| DynamoDB unavailable | Safety-critical operations (kill switch reads, position checks) fail → risk engine halts approvals. Execution engine halts new orders. Safe fail. |
| WebSocket disconnect | Reconnect with exponential backoff (max 5 attempts). If exhausted, kill switch activates and ops alert fires. |

### Kill Switch

The kill switch is the most important safety control. Three activation paths ensure it can always be triggered:

1. **Automatic:** CloudWatch alarm (daily P&L loss > 2%, WebSocket gap > 10s, DLQ depth > 0) → SNS → Lambda → DynamoDB write → Kafka publish
2. **Manual:** `make kill-switch-on` → script writes to DynamoDB + publishes to `risk.kill-switch` topic
3. **Any service:** Any service can produce a `KILL_SWITCH_ACTIVE` event to `risk.kill-switch`

All services consume the `risk.kill-switch` topic via dedicated async tasks (independent from the main processing loop). On receipt, they halt immediately — they do not wait for the current message processing to finish.

### Idempotency (Restart Safety)

Every stateful operation is designed to be safe if retried:

- Signal IDs are deterministic (SHA256 of strategy+symbol+direction+price+time) — same tick replayed = same signal_id = DynamoDB deduplication catches it
- Order placement uses `attribute_not_exists(order_id)` conditional write — duplicate order request = silent no-op
- DynamoDB state transitions use `ConditionExpression` to prevent race conditions
- On startup, every service reconciles its in-memory state with DynamoDB before processing

---

## 12. Cost Model

### Monthly AWS Cost Estimate (Paper Trading Mode)

| Service | Configuration | Estimated Monthly Cost |
|---|---|---|
| EC2 — 6 ASGs (ARM64) | 2 × t4g.medium, 3 × c6g.large, 1 × c6g.xlarge (scaled to 0 overnight) | $55–80 |
| MSK Serverless | ~2h/day market hours × 30 days, low data volume | $15–25 |
| DynamoDB | On-demand, ~10 tables, trading-hours workload | $5–15 |
| S3 | Tick storage, logs, model artifacts (~50GB/month) | $2–5 |
| CloudWatch | Logs, metrics, alarms | $10–15 |
| Secrets Manager | 6 secrets × 30-day rotation | $2–3 |
| NAT Gateway | Outbound traffic to broker APIs | $5–10 |
| **Total** | | **$94–153/month** |

### Cost Optimization Levers

1. **Scheduled scaling to 0** — Services scale down between market sessions. Overnight (15:35–08:30 IST for NSE), most ASGs have min=0.
2. **ARM64 Graviton** — 20–40% cheaper than x86 equivalents with better performance-per-watt.
3. **MSK Serverless** — Zero idle cost. Only charged for actual throughput during market hours.
4. **VPC Endpoints** — S3 and DynamoDB traffic bypasses NAT Gateway (saves ~$8–12/month at scale).
5. **S3 Intelligent-Tiering** — Model artifacts and infrequently accessed backtesting data auto-moves to cheaper storage tiers.

---

## 13. Key Design Decisions

### Decision 1: Kafka over SQS

SQS was used in Phase 1. Replaced with Kafka MSK Serverless in Phase 2. The trigger was the need for:
- Multiple independent consumers reading the same topic (AI engine and risk engine both need `signals.pending`)
- Message replay for crash recovery (SQS deletes after reading)
- Per-symbol ordering guarantees

The trade-off: Kafka requires understanding consumer groups and offset management. SQS is simpler. But the multi-consumer requirement made SQS unworkable without duplicating queues via SNS, which introduced its own complexity and cost.

### Decision 2: EC2 ARM64 ASGs over ECS Fargate

ECS Fargate was the original compute platform. Replaced with EC2 ARM64 ASGs in Phase 1. The trigger was:
- Fargate cold start latency (30–90s) was too slow for crash recovery during market hours
- Persistent WebSocket connections (Zerodha Kite Ticker) fit poorly into Fargate's task lifecycle model
- ARM64 Graviton instances are 20–30% cheaper at our compute profile

Trade-off: EC2 ASGs require managing AMI builds and instance refresh deployments. ECS handles that automatically. Accepted cost: an AMI build pipeline and instance refresh script.

### Decision 3: Deterministic Signal IDs

Signal IDs are SHA256 hashes of `strategy|symbol|direction|price|time` rather than random UUIDs. This ensures that if a strategy generates the same signal twice (e.g., after a restart), the second signal is silently discarded by DynamoDB's `attribute_not_exists(signal_id)` conditional write.

Trade-off: Two different signals at nearly the same time for the same symbol/direction/price would collide if the price truncation is the same. In practice, this doesn't happen at the precision we use (4 decimal places), but it is a known limitation documented in the codebase.

### Decision 4: DynamoDB Candle Cache (not Kafka) for Candle Strategies

Five of the six strategies are candle-based (1min, 5min, 15min OHLCV bars). The candle data could have been published to a Kafka topic. Instead, it's written to a DynamoDB table and polled by strategy_engine every 500ms.

Rationale: Candles arrive at most once per minute per symbol. DynamoDB Scan with a 3-minute lookback window reads ~150 items maximum. This is negligible in cost and latency. The polling approach with an in-memory deduplication set provides at-least-once delivery without the complexity of a Kafka consumer group specifically for candles.

### Decision 5: AI Enrichment as a Non-Blocking Side Path

The AI engine is not in the critical path. If it's slow or down, trading continues via the `EnrichmentWatchdog` fallback. This was a deliberate choice to keep the system's uptime independent of ML model availability.

Trade-off: Fallback signals get conservative defaults (quality_score=0.5, regime=unknown), which means some borderline signals that would normally pass quality scoring may be treated differently. Accepted: it's better to trade with conservative estimates than to halt trading when the AI engine has a transient issue.

---

---

## 14. Monitoring and Observability

### Paper Trading Monitoring Report

The system produces a structured **15-section paper trading monitoring report** via `MonitoringStatusService` + `MonitoringStatusRenderer` (`services/shared/monitoring/`). This is the primary operational view during paper trading sessions.

**Data sources:**

| Source | What it provides |
|---|---|
| DynamoDB `{prefix}-positions` | Open positions, quantities, entry prices, LTP, exit states |
| DynamoDB `{prefix}-risk-state` | Kill switch status |
| `LiveCounters` JSON (`/tmp/qe_live_counters.json`) | All service counters — TEE events, router stats, MIS state, recon results, P&L |

**15 report sections:**

| # | Section | GREEN criteria |
|---|---|---|
| 1 | Overall Status | All gates pass |
| 2 | Service Health | All services UP |
| 3 | Trading Mode & Safety Gates | mode=PAPER, live_trading_enabled=false |
| 4 | Position Summary | 0 unmanaged positions |
| 5 | Open Positions Detail | All have exit policy |
| 6 | TEE Status | tee_running=true, 0 unmanaged_detections |
| 7 | ExitOrderRouter Status | mode=PAPER, 0 failed_routes |
| 8 | MIS Square-Off Status | armed=true, kill_switch not activated |
| 9 | Reconciliation Status | recon_ran=true, 0 criticals |
| 10 | Risk & Daily Cap Status | No caps reached |
| 11 | Strategy Status | All ACTIVE, no CB open |
| 12 | Paper P&L | realized_pnl populated |
| 13 | Alerts & Warnings | None |
| 14 | Action Required | NO |
| 15 | Final Verdict | GREEN |

**LiveCounters wire-up (execution engine):**

```
ExecutionService.__init__()
  └── self._live_counters = LiveCounters()
        ├── ExitOrderRouter(live_counters=...)
        │     → router_paper_exits, realized_pnl, idempotency_*, failed_routes,
        │       live_attempts/blocked, backtest_exits
        ├── TradeExitEngine(live_counters=...)
        │     → tee_running, tee_poll_interval, stop_loss/take_profit/trailing hits,
        │       trailing_activated, unmanaged_detections, tee_latest_events
        ├── MISSquareOffManager(live_counters=...)
        │     → mis_positions_discovered, long/short_discovered,
        │       orders_placed/rejected, positions_flat, at_deadline,
        │       kill_switch_activated
        └── _monitoring_flush_loop() [asyncio task, 60s]
              → atomic write to /tmp/qe_live_counters.json
                (QE_MONITORING_COUNTERS_PATH, QE_MONITORING_FLUSH_INTERVAL)
```

**CLI usage:**

```bash
# With live execution service (reads /tmp/qe_live_counters.json):
python scripts/monitoring/paper_trading_monitor.py \
  --counters /tmp/qe_live_counters.json --watch 60

# Offline / debug (uses static sample stub):
python scripts/monitoring/paper_trading_monitor.py \
  --counters scripts/monitoring/sample_counters.json --watch 60
```

**Local dev seed:**
```bash
AWS_ENDPOINT_URL=http://localhost:4566 DYNAMODB_TABLE_PREFIX=quantembrace-test \
  python scripts/monitoring/seed_local_positions.py
```

---

*Last updated: 2026-05-28 | This document should be updated when: new layers are added, technology stack changes, AWS architecture changes, or key design decisions are revisited. For internal class and message schema details, see [lld.md](lld.md).*
