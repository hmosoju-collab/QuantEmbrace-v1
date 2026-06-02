# QuantEmbrace - System Design

_Last updated: 2026-05-30 — ADR-022 live-readiness audit applied: `symbol-status-index` GSI added to orders table (PositionValidator dirty-read fix), `sessions` DynamoDB table provisioned in Terraform (ZerodhaTokenManager), `RISK_MAX_SIGNAL_AGE_SECONDS=30` / `RISK_PROFILE` / `UNIVERSE_MODE` injected into EC2 userdata, `check_asg_health.py` created (replaced stale ECS script), per-symbol asyncio lock in DailyLossValidator. Pre-live runbook at `docs/live-readiness/pre-live-runbook.md`. Prior: ADR-021 staleness monitor split; ADR-020 paper readiness sweep; ADR-019 universe model._

---

## Overview

QuantEmbrace is a production-grade algorithmic trading platform operating across two markets:

- **NSE India** via Zerodha Kite Connect
- **US Equities** via Alpaca Markets API

The system is built in Python, deployed on **AWS EC2** (ARM64 Auto Scaling Groups), and follows a strict 6-layer architecture where every trade must pass through a centralized Risk Layer before reaching any broker.

**Current phase:** Phase 6 (AI/ML Signal Enrichment) is **complete**. The ai_engine service now enriches every signal with a market regime label (HMM, 4 states: trending/ranging/volatile/crash/unknown) and a quality score (GBT, 0.0–1.0) before the risk_engine validates it. The enriched signal flow is `signals.pending → ai_engine (aiengine-v1) → signals.enriched → risk_engine (risk-v1)`. An `EnrichmentWatchdog` monitors aiengine-v1 lag and automatically falls back to the direct `signals.pending → risk_engine` path when ai_engine is lagging. All enrichment uses graceful degradation — signals always flow through even if ML fails. Phase 3 (strategy decoupling), Phase 4 (distributed risk layer), and Phase 5 (feature store) are complete prerequisites.

---

## Architecture Principles

1. **Risk is non-negotiable.** Every order passes through the Risk Layer. No exceptions. No bypasses.
2. **Broker-agnostic execution.** The Execution Layer uses an adapter pattern. Adding a new broker means adding one adapter.
3. **Idempotent everything.** Every order submission, every state write, every recovery action is idempotent.
4. **Cost-conscious infra.** EC2 ARM64 (c6g/t4g) with scheduled scaling. No over-provisioned resources. No Fargate for always-on workloads.
5. **Fail safe, not fail open.** If a component fails, it halts trading — it does not pass through unchecked orders.
6. **Event-driven streaming.** Kafka is the sole backbone for all inter-service communication. SQS is permanently removed — using it is a CI violation (ruff TID251 banned-api).

---

## 6-Layer Architecture

```
+============================================================================+
|                                                                            |
|  LAYER 6: INFRASTRUCTURE LAYER                                            |
|  AWS EC2 ARM64 ASGs | MSK Serverless | S3 | DynamoDB | CloudWatch        |
|  Terraform-managed | Multi-AZ | Scheduled Scaling                         |
|                                                                            |
+============================================================================+
|                                                                            |
|  LAYER 5: AI/ML LAYER                                                     |
|  Feature Pipelines | Model Registry (S3) | Inference Service              |
|  Signal Enrichment | Lightweight, not over-engineered                      |
|                                                                            |
+============================================================================+
|                                                                            |
|  LAYER 4: RISK LAYER  <<<< CRITICAL -- SITS BETWEEN STRATEGY & EXEC >>>> |
|  Position Limits | Stop-Loss Enforcement | Exposure Checks                |
|  Kill Switch | Drawdown Monitoring | Per-Instrument Limits                |
|                                                                            |
+============================================================================+
|                                                                            |
|  LAYER 3: EXECUTION LAYER                                                 |
|  Broker Adapters (Zerodha + Alpaca) | Order Manager                       |
|  Retry Logic | Idempotent Submission | Fill Tracking                      |
|                                                                            |
+============================================================================+
|                                                                            |
|  LAYER 2: STRATEGY LAYER                                                  |
|  Signal Generation | Backtesting Framework | Pluggable Strategies         |
|  Multi-Market Support | Parameter Store                                   |
|                                                                            |
+============================================================================+
|                                                                            |
|  LAYER 1: DATA LAYER                                                      |
|  WebSocket Feeds (Kite Ticker + Alpaca) | S3 Historical Store             |
|  DynamoDB State (Latest Prices, Positions) | Data Normalization            |
|                                                                            |
+============================================================================+
```

---

## Phase 3 Strategy Decoupling Architecture

### Problem Solved

All five candle-based strategies were producing **zero signals** in Phase 2. The strategy engine consumed only Kafka tick events but never dispatched bars to candle strategies. Additionally, a single strategy crash halted all strategies, and config changes required full service restarts.

### Solution Components

#### StrategyRunner — Per-Strategy Failure Domain

Every strategy (tick-based and candle-based) is wrapped in a `StrategyRunner` that provides:

- **Dual-threshold circuit breaker** (ADR-013 §6.3): OPEN on 5 consecutive errors **OR** 10 errors/5 min. Whichever threshold is hit first.
- **Paper trade stamping**: `signal.paper_trade` is overwritten from DynamoDB config before publishing — the strategy never controls this.
- **Daily signal cap**: hard cap per calendar day (UTC), configurable per strategy.
- **Interface routing**: `InterfaceType.TICK` runners receive `dispatch_tick()`; `InterfaceType.CANDLE` runners receive `dispatch_bar()`. Wrong-interface calls return `None` immediately without calling the strategy.

Circuit breaker state machine:

```
CLOSED ──── N consecutive errors ──► OPEN
CLOSED ──── N errors/5min ──────────► OPEN
OPEN ─────── 300s elapsed ──────────► HALF_OPEN
HALF_OPEN ── 3 successes ───────────► CLOSED
HALF_OPEN ── 1 failure ─────────────► OPEN  (re-opens immediately)
OPEN ─────── manual DynamoDB reset ─► CLOSED (≤60s via StrategyConfigLoader)
```

#### DynamoCandleConsumer — Candle Data Without Zerodha API Calls

The `DynamoCandleConsumer` polls the `{prefix}-candle-cache` DynamoDB table every 500ms. The candle-cache is written by `data_ingestion/candle_stream.py` (IntradayCandleStream, implemented Phase 2). This decouples candle strategies from the Zerodha API rate limiter.

Key design decisions:
- **3-minute overlapping lookback**: FilterExpression on `candle_open_time >= (now - 3min)` ensures no candle is missed during the 500ms polling interval.
- **In-memory dedup set**: tracks `trace_id` (deterministic hash of market+symbol+interval+timestamp) to prevent the same candle from dispatching twice. Eviction runs every 30s, removing entries older than 5 min.
- **strategy_engine makes ZERO direct Zerodha API calls** for candle data.

#### StrategyConfigLoader — Hot-Reload Without Restarts

Reads `{prefix}-strategy-config` DynamoDB table every 60s. Applies updated `StrategyConfig` to each registered `StrategyRunner` atomically. Supports operator-initiated circuit breaker resets via a `circuit_breaker_reset` DynamoDB flag — the service detects it, resets the circuit, then self-clears the flag.

Config managed via `scripts/strategy/config.py` operator CLI (list, get, set, enable, disable, go-live, paper).

#### 4-Loop asyncio.gather in StrategyEngineService

```python
await asyncio.gather(
    self._kafka_processing_loop(),   # TICK runners — Kafka ticks.nse/ticks.us
    self._candle_processing_loop(),  # CANDLE runners — DynamoDB candle-cache poll
    self._config_refresh_loop(),     # hot-reload config every 60s
    self._kill_switch_loop(),        # Kafka kill.switch topic listener
)
```

Each loop is an independent asyncio.Task. A crash in the candle loop does not affect the tick loop.

#### paper_trade Pipeline (ADR-013 §8)

The `paper_trade` field was added to `Signal` (schema v3.0, safe default `False`). Flow:

```
DynamoDB strategy-config.paper_trade = True
         │
         ▼
StrategyRunner._apply_paper_flag()   ← stamps signal.paper_trade = True
         │
         ▼
signals.pending  →  risk_engine  →  signals.approved
                                          │
                                          ▼
                                 execution_engine
                                 paper_trade=True → routes to Alpaca paper endpoint
                                 paper_trade=False → routes to live broker
```

All strategies start with `paper_trade=True` in DynamoDB. Promotion to live requires 5-day paper validation + operator `go-live` confirmation via the CLI.

#### New DynamoDB Tables (Phase 3)

| Table | Owner | Purpose |
|---|---|---|
| `{prefix}-candle-cache` | data_ingestion (write), strategy_engine (read) | OHLCV candles, TTL 2h, GSI on candle_open_time |
| `{prefix}-strategy-config` | strategy_engine (read/write), ops CLI (write) | Per-strategy runtime config, hot-reload, circuit breaker reset flag |

---

## Phase 2 Kafka Streaming Architecture

All inter-service communication flows through MSK Serverless Kafka topics. Each service has its own consumer group with independent offsets. The kill switch is a separate high-priority topic consumed by a dedicated listener task in every service.

```
                   ticks.nse / ticks.us
 KiteWebSocket ──► KafkaTickPublisher ──────────────────────────────────────────┐
 AlpacaWS     ──►                                                                │
                                                                                 │
                   consumer group: strategy-v1               consumer group: risk-v1
                         ┌───────────────────────────────────────────────┐      │
                         │  Strategy Engine                  Risk Engine  │      │
 ┌───────────────────────│──► KafkaTickConsumer ──► strategy ──► signal │◄─────┘
 │                        │   (generates SIGNAL_PENDING)      │         │
 │  signals.pending       │                                    │         │
 └───────────────────────►│                   KafkaSignalPublisher       │
                          │                         │                     │
                          └─────────────────────────┼─────────────────────┘
                                                     │
                   signals.pending ─────────────────►│ risk-v1 consumer group
                                              ┌──────┴────────────────────┐
                                              │  Risk Engine               │
                                              │  validate(signal)          │
                                              │  → SIGNAL_APPROVED / REJECT│
                                              └──────┬────────────────────┘
                                                     │
                   signals.approved ────────────────►│ execution-v1 consumer group
                                              ┌──────┴────────────────────┐
                                              │  Execution Engine          │
                                              │  place_order(broker)       │
                                              │  → orders.events (fills)   │
                                              └───────────────────────────┘

  kill.switch ────────────────────────────────► kill-switch-listener (ALL services)
  ops.audit   ◄──────────────────────────────── risk_engine + execution_engine
```

### Kafka Topics

| Topic                 | Partitions | Retention | Key              | Producer           | Consumer group(s)         |
|----------------------|-----------|-----------|------------------|--------------------|---------------------------|
| `ticks.nse`          | 4          | 24h       | instrument_id    | data_ingestion     | strategy-v1               |
| `ticks.us`           | 2          | 24h       | instrument_id    | data_ingestion     | strategy-v1               |
| `signals.pending`    | 2          | 1h        | instrument_id    | strategy_engine    | aiengine-v1, risk-v1 (fb) |
| `signals.enriched`   | 2          | 1h        | symbol           | ai_engine          | risk-v1 (primary)         |
| `signals.approved`   | 2          | 30min     | instrument_id    | risk_engine        | execution-v1              |
| `orders.events`      | 4          | 7d        | instrument_id    | execution_engine   | risk-v1 (P&L)             |
| `risk.kill-switch`   | 1          | 30d       | GLOBAL           | risk_engine / ops  | all services              |
| `ops.audit`          | 2          | 90d       | trace_id         | risk, execution    | ops tooling               |

**Primary signal path (Phase 6):**
`signals.pending` → ai_engine (aiengine-v1) → `signals.enriched` → risk_engine (risk-v1) → `signals.approved`

**Fallback path (ai_engine lagging):**
`signals.pending` → risk_engine (risk-v1) → `signals.approved`

The `EnrichmentWatchdog` (inside risk_engine) monitors aiengine-v1 consumer-group lag. After 2 consecutive checks with lag ≥ 10, it activates fallback mode. Recovery requires 5 consecutive clear checks.

All primary topics have corresponding `.retry` and `.dlq` topics (same partitions/retention).

### Event Schema Versions

| Schema | Topics                   | Notable Fields                                       |
|--------|--------------------------|------------------------------------------------------|
| v3.0   | ticks.*, signals.pending, signals.approved | trace_id, event_type, schema_version, source |
| v4.0   | signals.enriched         | All v3.0 fields + regime, regime_confidence, quality_score, filtered, enriched_at, enrichment_latency_ms, model_versions |

`trace_id` flows: `TICK → SIGNAL_PENDING → SIGNAL_ENRICHED → SIGNAL_APPROVED → ORDER_FILLED`. One CloudWatch Logs Insights query on trace_id reconstructs the full trade lifecycle including enrichment decisions.

**v3.0 envelope:**

```json
{
  "event_id":       "uuid4 (unique per message)",
  "trace_id":       "uuid4 (set at tick origin, propagated unchanged to fill)",
  "event_type":     "TICK | SIGNAL_PENDING | SIGNAL_APPROVED | ORDER_FILLED | ...",
  "schema_version": "3.0",
  "source":         "data_ingestion | strategy_engine | risk_engine | execution_engine",
  "published_time": "ISO8601 UTC"
}
```

`trace_id` flows: `TICK → SIGNAL_PENDING → SIGNAL_APPROVED → ORDER_FILLED`. One CloudWatch Logs Insights query on trace_id reconstructs the full trade lifecycle.

---

## Layer 1: Data Layer

### Purpose

Ingest, normalize, and store all market data. This layer is the single source of truth for current and historical market state.

### Components

#### 1.1 Real-Time Market Data Ingestion

**Zerodha Kite Ticker (NSE India)**
- WebSocket connection via `kiteconnect` Python SDK
- Subscribes to instruments in `full` mode (OHLC, LTP, depth, OI)
- Reconnection logic with exponential backoff (max 5 retries, then alert)
- Runs on EC2 t4g.medium via `data_ingestion_nse` ASG

**Alpaca WebSocket (US Equities)**
- WebSocket connection via `alpaca-trade-api` Python SDK
- Subscribes to trades and quotes for configured symbols
- Separate ASG: `data_ingestion_us`

**Why two separate services:** Different market hours, different reconnection behaviors, independent failure domains.

#### 1.2 Tick Publishing (Phase 2)

The `KafkaTickPublisher` publishes normalized ticks to `ticks.nse` and `ticks.us`. Each tick event carries a fresh `trace_id` (uuid4) and a monotonically incrementing `sequence_id`. The publisher requires `KAFKA_BOOTSTRAP_SERVERS` to be set — the service raises `RuntimeError`
on startup if it is missing. There is no SQS fallback mode. PHASE2_KAFKA_ENABLED has been deleted.

```
KAFKA_BOOTSTRAP_SERVERS set  → Kafka-only (sole operating mode)
KAFKA_BOOTSTRAP_SERVERS unset → RuntimeError at startup (service will not run)
```

#### 1.3 Data Storage

**DynamoDB — Latest State (Hot Path)**
- `latest-prices` table: keyed by `{market}#{instrument}`, stores LTP, bid/ask, volume, timestamp
- TTL: 24 hours (stale prices auto-expire)
- On-demand capacity mode

**S3 — Historical Data (Cold Path)**
- Partitioned by: `s3://{bucket}/{market}/{instrument}/{date}/{hour}/ticks.parquet`
- Lifecycle: Glacier after 90 days (dev/staging), 365 days (prod), delete after 3 years

#### 1.4 Data Normalization

All incoming data is normalized into a unified `MarketTick` schema before publishing. Downstream consumers never deal with broker-specific formats.

---

## Layer 2: Strategy Layer

### Purpose

Generate trading signals from market data. Strategies are modular, pluggable, and completely unaware of execution mechanics.

### Phase 3 Architecture: Two Input Paths

The strategy engine runs two concurrent data input paths:

**Path A — Tick (Kafka, existing):**

The `KafkaTickConsumer` (consumer group `strategy-v1`) polls `ticks.nse` and `ticks.us`. The consumer is synchronous (`confluent_kafka.Consumer.poll()`) and is called via `asyncio.to_thread()`. Used exclusively by `MomentumStrategy` (InterfaceType.TICK).

**Path B — Candle (DynamoDB, Phase 3):**

The `DynamoCandleConsumer` polls `{prefix}-candle-cache` DynamoDB every 500ms. Candles are written by `data_ingestion/candle_stream.py`. Used by all five candle-based strategies. The strategy engine makes **zero direct Zerodha API calls** for candle data.

Routing within `_candle_processing_loop()`:

```python
for candle in consumer.poll_new_candles():
    bar = candle.to_bar()
    for runner in self._candle_runners:
        if (bar.interval == runner._strategy.candle_interval
                and bar.symbol in runner._strategy.symbols):
            signal = await runner.dispatch_bar(bar)
```

### Signal Publishing

The `KafkaSignalPublisher` produces `SIGNAL_PENDING` events to `signals.pending`, keyed by `instrument_id`. Signal IDs are **deterministic**:

```
signal_id = sha256(strategy_name|symbol|direction|price_4dp|signal_time_iso)[:32]
```

A restarted strategy engine replaying the same candle/tick produces the same signal_id. The risk engine's DynamoDB conditional write silently discards duplicates.

### Signal Expiry

Signals carry `expires_at = signal_time + 30s`. The risk engine rejects expired signals to prevent stale execution after queue backlogs.

### Pluggable Strategies (Phase 3 — all active)

| Strategy | Interface | Interval | Status |
|---|---|---|---|
| `MomentumStrategy` | TICK | — | Active (paper) |
| `ORBStrategy` | CANDLE | 15min | Active (paper) |
| `Scalp1mStrategy` | CANDLE | 1min | Active (paper) |
| `VWAPReversionStrategy` | CANDLE | 5min | Active (paper) |
| `IntradayTrend15mStrategy` | CANDLE | 15min | Active (paper) |
| `PreCloseMomentumStrategy` | CANDLE | 15min | Active (paper) |

All strategies import `Signal`, `Direction` from `shared.models.signal` (not from any service-local re-export). All start with `paper_trade=True` in DynamoDB strategy-config until 5-day paper validation passes.

### Per-Strategy Failure Isolation

Each strategy is wrapped in a `StrategyRunner`. An exception in one runner:
- Records a failure in that runner's `CircuitBreaker`
- Does **not** affect any other runner
- Opens the circuit after 5 consecutive errors or 10 errors/5 min
- Self-heals after 5 minutes (HALF_OPEN) or manual operator reset via DynamoDB flag

---

## Layer 3: Execution Layer

### Purpose

Translate approved signals into broker-specific API calls. Handle retries, failures, fill tracking, and fill reporting back to the risk engine via `orders.events`.

### Phase 2 Consumption

The `KafkaSignalConsumer` (consumer group `execution-v1`) consumes `signals.approved`. After each fill, the execution engine publishes to `orders.events` (keyed by `order_id`) so the risk engine can update P&L and position state in real time.

### Broker Adapters

**ZerodhaAdapter** — Zerodha Kite Connect
- Zerodha rate limiter: 10 req/sec token bucket (ADR-012)
- `BulkOrderPoller`: polls `kite.orders()` at 300ms intervals (O(1) instead of O(N) per-order)
- MIS auto-square-off: proactive close at 15:05 IST (before broker auto-close at 15:15)
- Daily token re-authentication handled automatically

**AlpacaAdapter** — Alpaca REST + WebSocket
- Paper trading support for staging
- Fractional shares enabled
- Extended hours trading via `extended_hours` flag

### Idempotent Order Submission

DynamoDB conditional write (`attribute_not_exists(order_id)`) prevents duplicate orders on restart. The same `signal_id` → `order_id` mapping ensures a replayed approved signal does not place a second order.

### Kill Switch Listener

A dedicated `asyncio.Task` consumes the `kill.switch` topic (high-watermark: `auto.offset.reset=latest`). On receipt of a `KILL_SWITCH_ACTIVE` event, the execution engine cancels all open orders and halts all new order placement immediately, without waiting for the main processing loop.

---

## Layer 4: Risk Layer (CRITICAL)

### Purpose

The gatekeeper between strategy signals and execution. No signal becomes an order without passing every risk check.

### Signal Consumption (current — Phase 6 enrichment path)

**Primary (normal):** `KafkaSignalConsumer` (consumer group `risk-v1`) consumes `signals.enriched` — signals that have been enriched by ai_engine with `market_regime`, `quality_score`, and `filtered` flag. `RiskDecision.enriched = True` when this path is used.

**Fallback (ai_engine lagging):** `EnrichmentWatchdog` monitors `aiengine-v1` consumer lag. When lag exceeds threshold, risk_engine falls back to consuming `signals.pending` directly (consumer group `risk-v1-fallback`). `RiskDecision.enriched = False`.

Validated signals are published to `signals.approved`. Every decision (approve/reject) is written to `ops.audit` with a `risk_decision_id` linked to `signal_id` and `trace_id`.

> **Phase 2 historical note:** Before Phase 6, risk-v1 consumed `signals.pending` as the sole primary path (no enrichment hop). The Phase 2 diagram section above still shows this legacy flow.

### Architecture Position

```
Strategy Engine                Risk Engine                  Execution Engine
      │                              │                              │
      │── SIGNAL_PENDING ──────────►│                              │
      │        (signals.pending)     │── validate()                 │
      │                              │   kill_switch_check    (1st) │
      │                              │   position_limit_check       │
      │                              │   exposure_check             │
      │                              │   stop_loss_check            │
      │                              │   drawdown_check             │
      │                              │   instrument_limit_check     │
      │                              │   margin_check         (last)│
      │                              │                              │
      │                              │── IF ALL PASS:               │
      │                              │── SIGNAL_APPROVED ──────────►│
      │                              │       (signals.approved)     │
      │                              │                              │
      │                              │── IF ANY FAIL:               │
      │                              │── ops.audit (REJECTED)       │
```

### Kill Switch

Kill switch state is authoritative in DynamoDB (`risk-state` table). The risk engine polls it at every loop iteration AND listens to the `kill.switch` Kafka topic for real-time propagation. Activation sources:
- Manual: API call / ops script writes `kill_switch=ACTIVE` to DynamoDB
- Automatic: CloudWatch alarm (WebSocket gap > 10s, drawdown threshold, DLQ depth > 0) triggers Lambda → DynamoDB write
- Kafka: any service can produce to `kill.switch` topic; all services listen

---

## Layer 5: AI/ML Layer

### Purpose

Provide signal enrichment and predictive features. Intentionally lightweight — augments human-designed strategies rather than replacing them. The enrichment pipeline never blocks trading. All enrichment degrades gracefully to safe defaults if ML components are unavailable.

### Phase 6 Architecture — Real-Time Signal Enrichment

```
signals.pending (v3.0)
        │
        ▼  consumer group: aiengine-v1
┌───────────────────────────────────────────────────────┐
│  ai_engine  (c6g.large EC2 ASG, ap-south-1a)          │
│                                                        │
│  KafkaSignalConsumer ──► FeatureReader (DynamoDB)     │
│                      │   RegimeClassifier (HMM)        │
│                      │   SignalQualityScorer (GBT)     │
│                      │   SignalEnricher                 │
│                      ▼                                  │
│  KafkaEnrichedPublisher ──► signals.enriched (v4.0)   │
└───────────────────────────────────────────────────────┘
        │
        ▼  consumer group: risk-v1
risk_engine: KafkaEnrichedConsumer
```

### Components (Phase 5 + Phase 6)

**FeatureReader** (`shared/features/feature_reader.py`, Phase 5)
- Reads pre-computed features from `{prefix}-features` DynamoDB table (written by data_ingestion FeatureEngine every candle).
- Returns `FeatureSet`: RSI-14, EMA-9/21, VWAP, ATR-14, ADX-14, MACD, volume_ratio.
- Interval-aware staleness: 1m→5min, 5m→11min, 15m→31min. Returns None when stale.
- On None: `RegimeClassifier` and `SignalQualityScorer` degrade to `regime="unknown"`, `quality_score=0.5`.

**ModelRegistry** (`services/ai_engine/model_registry.py`)
- Loads joblib-serialized models from S3 (`s3://quantembrace-model-artifacts/models/{name}/{version}/model.joblib`).
- Hot-reload every 60 seconds — detects new version by comparing S3 object metadata.
- Falls back to stub model (returns `regime="unknown"`, `quality_score=0.5`) when S3 is unavailable.
- Thread-safe swap with read-write lock — zero downtime model updates.

**RegimeClassifier** (`services/ai_engine/models/regime_classifier.py`)
- Wraps an HMM (Hidden Markov Model) trained offline, stored as a joblib artifact.
- Input: FeatureSet (RSI, ATR, ADX, MACD, volume_ratio, EMA ratio).
- Output: `regime ∈ {trending, ranging, volatile, crash, unknown}`, `regime_confidence ∈ [0.0, 1.0]`.
- Graceful degradation: any exception → `regime="unknown"`, `confidence=0.0`.

**SignalQualityScorer** (`services/ai_engine/models/signal_quality_scorer.py`)
- Wraps a GBT (Gradient Boosted Trees) classifier trained offline.
- Input: FeatureSet + signal metadata (direction, confidence, quantity).
- Output: `quality_score ∈ [0.0, 1.0]`. Below configurable threshold → `filtered=True`.
- Graceful degradation: any exception → `quality_score=0.5`, `filtered=False`.

**SignalEnricher** (`services/ai_engine/enricher.py`)
- Orchestrates: FeatureReader → RegimeClassifier → SignalQualityScorer → EnrichedSignal.
- Always returns an `EnrichedSignal` (v4.0). Never raises on partial failure.
- Publishes enrichment latency to CloudWatch: `QuantEmbrace/AIEngine/EnrichmentLatencyMs` (P99 target: < 15ms).

**EnrichmentWatchdog** (`services/risk_engine/consumers/enrichment_watchdog.py`)
- Runs inside risk_engine as an asyncio.Task.
- Monitors aiengine-v1 consumer-group lag on signals.pending every 500ms.
- Lag ≥ threshold for 2 consecutive checks → activates fallback (risk_engine reads signals.pending directly).
- Lag = 0 for 5 consecutive checks → deactivates fallback (risk_engine reads signals.enriched).
- Tunable via DynamoDB: `ENRICHMENT_CONFIG/GLOBAL` item (`lag_threshold`, `window`, `recovery_window`).
- Publishes `QuantEmbrace/RiskEngine/EnrichmentFallbackActive = 1/0` to CloudWatch.

### Enriched Signal Schema (v4.0)

```python
@dataclass(frozen=True)
class EnrichedSignal:
    # Signal fields (all v3.0 fields pass through unchanged)
    signal_id, strategy_name, symbol, market, direction, quantity,
    confidence, price_at_signal, generated_at, expires_at,
    stop_loss, take_profit, paper_trade, strategy_id, product_type,
    trace_id, metadata
    
    # Enrichment fields (new in Phase 6)
    regime: RegimeLabel           # trending|ranging|volatile|crash|unknown
    regime_confidence: float      # HMM posterior, 0.0 = degraded
    quality_score: float          # GBT score, 0.5 = degraded
    filtered: bool                # True if quality_score < threshold
    enriched_at: datetime
    enrichment_latency_ms: float
    model_versions: dict[str, str]
    schema_version: str = "4.0"
```

### Graceful Degradation Contract

Every component in the enrichment pipeline degrades independently. The pipeline never fails-closed:

| Failure | Degraded values |
|---------|----------------|
| FeatureReader → None (stale or DynamoDB error) | `regime="unknown"`, `regime_confidence=0.0`, `quality_score=0.5`, `filtered=False` |
| RegimeClassifier exception | `regime="unknown"`, `regime_confidence=0.0` |
| SignalQualityScorer exception | `quality_score=0.5`, `filtered=False` |
| EnrichmentWatchdog: ai_engine lagging > threshold | Fallback: risk_engine reads signals.pending directly |
| ModelRegistry: S3 unavailable | Stub model returns degraded values |

### New DynamoDB Tables (Phase 6)

| Table | Owner | Purpose |
|---|---|---|
| `{prefix}-regime-log` | ai_engine (write) | HMM regime state per market/symbol per session; TTL 30d; advisory/analytics |
| `{prefix}-strategy-recommendations` | ai_engine (write) | Per-strategy/date recommendations from enrichment; TTL 30d; advisory |

### New CloudWatch Alarms (Phase 6 — `QuantEmbrace/AIEngine` namespace)

| Alarm | Threshold | Action |
|-------|-----------|--------|
| `EnrichmentLatencyHigh` | P99 > 20ms for 5 min | Alert ops |
| `EnrichmentFallbackActive` | ≥ 1 (any check) | Alert ops (fallback is safe; no trading halt) |
| `RegimeClassificationErrors` | Sum > 5 in 5 min | Alert ops |
| `QualityFilterRateHigh` | Average > 50% in 15 min | Alert ops |
| `SignalsEnrichedSilent` | Count = 0 for 15 min | Alert ops |
| `ModelHotReloadErrors` | Sum > 3 in 15 min | Alert ops |

---

## Layer 6: Infrastructure Layer

### Compute: EC2 ARM64 Auto Scaling Groups

All services run on **AWS Graviton3** ARM64 instances via individual ASGs. This replaces the previous ECS Fargate design.

| Service              | Instance Type  | ASG Min | ASG Max | Notes |
|---------------------|---------------|---------|---------|-------|
| data-ingestion-nse  | t4g.medium    | 1       | 1       | WebSocket + feature pipeline |
| data-ingestion-us   | t4g.medium    | 1       | 1       | WebSocket |
| strategy-engine     | c6g.large     | 1       | 2       | CPU-bound signal generation |
| risk-engine         | c6g.xlarge    | 1       | 1       | Gatekeeper; latency-critical |
| execution-engine    | c6g.xlarge    | 1       | 1       | cluster placement; min=1 always |
| ai-engine           | —             | —       | —       | **[NOT YET DEPLOYED IN PROD]** No ASG defined; no IAM Kafka attachment. EnrichmentWatchdog fallback is permanent production path. See ADR-022. |

**ai_engine production status:** The `aiengine-v1` consumer group and Kafka IAM policy resource exist in Terraform, but `ai_engine_role_name` is not passed to the kafka module from `prod/main.tf`. The service has no EC2 ASG and is not in the deploy pipeline. `signals.enriched` carries no traffic in production; all signals flow through the EnrichmentWatchdog fallback path (`signals.pending → risk_engine`). Planned for Phase 7 rollout.

**Scheduled scaling** (via ASG scheduled actions): services scale to 0 outside market hours. Execution engine min_size is always ≥ 1 for emergency order cancellations and post-close reconciliation.

**Warm pools** (prod only): pre-warmed instances for fast ASG replacements.

### Messaging: Kafka MSK Serverless (Phase 2)

MSK Serverless eliminates broker management. Authentication uses SASL/OAUTHBEARER + IAM token refresh (port 9098). Topics are created by `scripts/kafka/create_topics.py` after Terraform apply — not by Terraform itself.

### Storage

**S3 Buckets:**
- `quantembrace-tick-data` — raw tick Parquet
- `quantembrace-ohlcv-data` — resampled OHLCV
- `quantembrace-trading-logs` — audit + execution logs
- `quantembrace-model-artifacts` — ML models + features

**DynamoDB Tables (13 total — all Terraform-provisioned):**
- `orders` — order lifecycle, idempotency keys; 4 GSIs: `signal-index`, `status-index`, `account-index`, `symbol-status-index` (dirty-read race fix — ADR-022)
- `positions` — open positions, real-time P&L
- `latest-prices` — hot cache (TTL 24h)
- `risk-state` — kill switch, drawdown counters, NAV, margin snapshots, reconciliation flag, P&L aggregates
- `sessions` — Zerodha daily access token store; TTL 48h; read/written by `ZerodhaTokenManager` (ADR-022)
- `strategy-state` — per-strategy indicator state
- `candle-cache` — OHLCV candles written by data_ingestion, read by strategy_engine (Phase 3); TTL 2h, GSI on candle_open_time
- `strategy-config` — per-strategy runtime config + enrichment watchdog config (`ENRICHMENT_CONFIG/GLOBAL`); hot-reloaded every 60s (Phase 3/6)
- `features` — pre-computed TA indicators (RSI/EMA/VWAP/ATR/ADX/MACD/vol_ratio); LATEST (TTL 24h) + CANDLE#{ts} (TTL 7d) (Phase 5)
- `regime-log` — HMM regime state per market/symbol per session; TTL 30d; advisory (Phase 6)
- `strategy-recommendations` — per-strategy/date enrichment recommendations; TTL 30d; advisory (Phase 6)
- `signal-inbox` — Phase 8 durable inbox; TTL; PITR
- `signal-outbox` — Phase 8 durable outbox; TTL; PITR; GSI `status-approved-index`

### Networking

- VPC with public and private subnets across 2 AZs (ap-south-1a, ap-south-1b)
- EC2 instances run in private subnets (no inbound internet)
- NAT Gateway for outbound connections (broker APIs)
- VPC endpoints for S3 and DynamoDB (removes NAT Gateway from hot path)
- MSK Serverless VPC connectivity via private subnets

### Secrets

- AWS Secrets Manager: Zerodha credentials (API key, secret, session token) and Alpaca credentials
- Accessed by EC2 instance profiles (IAM role — no hardcoded credentials)
- Session token rotation: automatic re-auth at startup via Zerodha OAuth flow

### Monitoring

- **CloudWatch Logs**: structured JSON, 30-day hot retention (prod), archived to S3
- **CloudWatch Metrics**: `ZerodhaRateLimit`, `TradingSystem`, `KafkaConsumerLag` namespaces
- **Key alarms**: WebSocket gap > 10s (auto-activates kill switch), DLQ depth > 0, daily P&L drawdown, order rejection rate > 20%, Kafka consumer lag
- **Tracing**: `trace_id` propagated from tick to fill — single Logs Insights query reconstructs full trade lifecycle

---

## Component Interaction Summary (Phase 2)

```
  Kite Ticker (NSE)   Alpaca WS (US)
        │                  │
        ▼                  ▼
  ┌─────────────────────────────┐
  │    Data Ingestion           │  (data_ingestion_nse ASG +
  │  KafkaTickPublisher         │   data_ingestion_us ASG)
  └──────────────┬──────────────┘
                 │ ticks.nse / ticks.us
         ┌───────┴────────┐
         ▼                ▼
  ┌──────────────┐  ┌─────────────┐
  │  Strategy    │  │   Risk      │
  │  Engine      │  │   Engine    │
  │ (strategy-v1)│  │  (risk-v1)  │
  └──────┬───────┘  └──────┬──────┘
         │                 │
         │ signals.pending  │ signals.approved
         └────────►─────────►────────────────┐
                                              ▼
                                    ┌─────────────────┐
                                    │  Execution      │
                                    │  Engine         │
                                    │ (execution-v1)  │
                                    └────────┬────────┘
                                             │
                                    ┌────────┴────────┐
                                    ▼                  ▼
                               Zerodha API         Alpaca API
                               (NSE orders)        (US orders)
                                    │                  │
                                    └────────┬─────────┘
                                             │ orders.events
                                             ▼
                                    Risk Engine (P&L update)

  kill.switch topic ──────────────►  All services (kill-switch-listener task)
  ops.audit topic   ◄──────────────  Risk Engine + Execution Engine
```

---

## Service Boundaries

| Service          | Owns                                           | Reads From                                                                                        | Writes To                                                                                         |
|-----------------|------------------------------------------------|---------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| data_ingestion  | Raw + normalized market data, candles, features | Broker WebSocket feeds                                                                            | `ticks.nse`, `ticks.us` (Kafka); `candle-cache`, `features` (DynamoDB); S3                       |
| strategy_engine | Trading signals, circuit breaker state          | `ticks.nse`, `ticks.us` (Kafka, strategy-v1); `candle-cache` (DynamoDB poll); `strategy-config` (DynamoDB hot-reload) | `signals.pending` (Kafka)                     |
| ai_engine       | Signal enrichment, regime state                 | `signals.pending` (Kafka, aiengine-v1); `features` (DynamoDB); S3 (model artifacts)              | `signals.enriched` (Kafka); `regime-log`, `strategy-recommendations` (DynamoDB); CloudWatch       |
| risk_engine     | Risk decisions, position limits, P&L, enrichment fallback | `signals.enriched` (primary) or `signals.pending` (fallback) (risk-v1); `orders.events` (Kafka); `risk-state`, `strategy-config` (DynamoDB) | `signals.approved` (Kafka); `risk.kill-switch`; `ops.audit`; DynamoDB; S3 (audit) |
| execution_engine| Orders, order state, fill reporting             | `signals.approved` (execution-v1)                                                                 | Broker APIs; `orders.events` (Kafka); `ops.audit`; DynamoDB                                       |

---

## Non-Negotiable Trading Rules

1. **Strategy logic** computes signals. It never places orders or checks risk limits.
2. **Risk logic** validates signals. It never modifies signals or places orders.
3. **Execution logic** places orders. It never generates signals or overrides risk decisions.
4. Every signal flows: `strategy_engine → risk_engine → execution_engine`. No bypass.
5. No order reaches a broker without an explicit `risk_decision_id` logged to `ops.audit`.
6. If the risk engine is down, trading halts. This is by design.

---

## Trading Universe Model

_Added: 2026-05-26 (ADR-019)_

### Overview

The universe model defines which symbols are approved for trading on any given date and in which mode. It is the outer gate — before risk validation, before order placement, the system must know if a symbol is even eligible to be traded at all.

### Universe Modes

Three modes exist. Promotion is manual and requires gate criteria to pass.

| Mode | Scope | Purpose |
|------|-------|---------|
| `PAPER_SAFE_START` | NIFTY 50 only | Paper trading — conservative starting point |
| `PAPER_EXPAND` | NIFTY 100 + F&O stocks | Paper trading — broader universe after safe-start gates pass |
| `LIVE_ADVANCED` | NIFTY 200 with strict filters | Live trading — production, after expansion gates pass |

Promotion path: `PAPER_SAFE_START → PAPER_EXPAND → LIVE_ADVANCED`

Promotion is **always manual** — the evaluator reports pass/fail but does not change the mode. The operator sets `UNIVERSE_MODE` in the environment.

### Hard Order Validation Rule

> **No paper or live order may be placed unless the symbol exists in the approved universe snapshot for that trading date and mode.**

This is enforced at `execute_approved_signal()` in the execution engine, before any broker call, after the kill-switch check. Violations raise `ValueError` and the order is never placed.

- **Paper mode with no snapshot**: allowed with warning (non-fatal, does not block paper trading).
- **Live mode with no snapshot**: all orders BLOCKED until snapshot is built.
- **Symbol not in snapshot**: rejected with reason string that includes mode, date, and checksum.

### Paper / Live Isolation

Paper and live snapshots are stored in separate DynamoDB namespaces (`PAPER#*` vs `LIVE#*`). A live broker can never read a paper snapshot, and vice versa. Paper trading decisions never influence live trading decisions.

### Universe Snapshot

A `UniverseSnapshot` is:
- **Immutable**: `approved_symbols` is a `frozenset`; `decisions` is a `tuple`.
- **Per-date**: valid for exactly one NSE trading date.
- **Auditable**: every symbol has a `UniverseDecision` with inclusion/exclusion reason codes.
- **Checksummed**: SHA-256 of the sorted approved symbol list, first 16 hex chars.
- **Never overwritten**: once stored for `(mode, date)`, subsequent saves are no-ops.

### Filter Chain

Applied in order when building a snapshot. A symbol must pass ALL enabled filters:

1. **ExclusionListFilter** — fast reject: delisted, suspended, SME, ETF, REIT/InvIT, emergency
2. **IndexMembershipFilter** — must be in a configured index (NIFTY_50, NIFTY_100, FNO, NIFTY_200)
3. **EquityTypeFilter** — must be NSE series EQ or BE (not SME, MF, rights warrants)
4. **SurveillanceFilter** — ASM/GSM listed stocks excluded (SEBI surveillance)
5. **LiquidityFilter** — ADV ₹, ADV volume, free-float MCap, bid-ask spread, active days, delivery %
6. **RiskFilter** — penny stock, abnormal volatility, circuit frequency, low float
7. **CorporateActionFilter** — exclusion window around splits, bonuses, mergers

Live mode uses **stricter thresholds** than paper on every filter dimension (higher ADV floor, tighter spread cap, lower volatility cap, etc.).

### Refresh Policy

| Trigger | Action |
|---------|--------|
| Daily (market open) | Rebuild snapshot for the trading date |
| Weekly | Review exclusion lists for stale entries |
| Monthly | Validate YAML index membership against NSE public data |
| Emergency | Operator adds symbol to `exclusion_lists.yaml` emergency list; next snapshot includes exclusion |
| NSE rebalance | Update `universe_modes.yaml` index_symbols lists and rebuild |

Snapshots are stored in DynamoDB with 30-day TTL. Full audit JSON is written to S3 `universe/snapshots/`.

### Promotion Gates

Promotion gate criteria are defined in `configs/promotion_gates.yaml`. Each gate covers:

- **Data quality**: tick pass rate, candle formation rate, data gaps
- **Signal stability**: flip rate, confidence average
- **Order execution**: rejection rate, latency, duplicate rate
- **Slippage**: average and outlier bps
- **Risk controls**: validator pass rate, kill switch false fires, drawdown
- **Portfolio**: position sizing accuracy, P&L reconciliation
- **Monitoring**: dashboards live, alerts configured, MIS tested

Run `python scripts/universe/evaluate_promotion_gate.py --gate PAPER_SAFE_START_TO_EXPAND` to check current status.

### Configuration Files

| File | Purpose |
|------|---------|
| `configs/universe_modes.yaml` | Mode definitions, NIFTY index symbol lists |
| `configs/liquidity_filters.yaml` | ADV, volume, spread thresholds per mode |
| `configs/risk_filters.yaml` | Penny stock, ASM/GSM, volatility, float policy |
| `configs/promotion_gates.yaml` | Pass/fail criteria for mode promotion |
| `configs/exclusion_lists.yaml` | Known exclusions: delisted, SME, ETF, emergency |

### Key Code Locations

| Component | File |
|-----------|------|
| Universe mode enum | `services/shared/universe/modes.py` |
| All data models | `services/shared/universe/models.py` |
| Data source interfaces | `services/shared/universe/data_sources.py` |
| Filter chain | `services/shared/universe/filters.py` |
| Snapshot builder | `services/shared/universe/builder.py` |
| Snapshot store | `services/shared/universe/snapshot_store.py` |
| Order validator | `services/shared/universe/order_validator.py` |
| Promotion gates | `services/shared/universe/promotion.py` |
| InstrumentLoader (snapshot filter) | `services/strategy_engine/universe/instrument_loader.py` |
| Execution engine integration | `services/execution_engine/service.py` (`execute_approved_signal`) |

### Failure Handling

| Scenario | Behavior |
|----------|----------|
| NSE API unavailable | YAML fallback used; snapshot built from curated lists |
| F&O list unavailable | YAML FNO list used; logged as FALLBACK |
| Surveillance list unavailable | WARN policy: symbols included; EXCLUDE policy: all blocked |
| Snapshot generation fails entirely | `UniverseSnapshotError` raised; live mode rejects all orders |
| Approved symbol count below `min_symbols_to_trade` | `failure_mode=PARTIAL`, CRITICAL alert raised |
| Live universe empty | All orders blocked; kill switch candidate |

### Environment Variable

```
UNIVERSE_MODE=PAPER_SAFE_START   # default; also PAPER_EXPAND or LIVE_ADVANCED
```

This is read at `execution_engine` startup to build the initial validator.
7. Every service must be safe to restart at any time without data loss or duplicate orders.
