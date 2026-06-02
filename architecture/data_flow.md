# QuantEmbrace - Data Flow

_Last updated: 2026-05-27 — Phase 6 enriched signal path (signals.enriched) active; Phase 8 hardening in progress; ADR-019 universe model (PAPER_SAFE_START/PAPER_EXPAND/LIVE_ADVANCED) and daily snapshot refresh loop added to execution_engine; ADR-020 paper readiness fixes (signal age 5s→30s, NAV ₹50L→₹10L, strategy config seeding, duplicate suppression)._

---

## Overview

This document traces every data path in QuantEmbrace from market tick ingestion through order execution and back into monitoring. All inter-service flows are Kafka-based. SQS has been permanently removed — there is no migration window or fallback path.

---

## End-to-End Data Flow Summary (Phase 3)

```
  Zerodha Kite Ticker          Alpaca WebSocket
  (NSE — full mode)            (US trades + quotes)
        │                              │
        │ raw ticks (WebSocket)        │ raw ticks (WebSocket)
        ▼                              ▼
  ┌─────────────────────────────────────────────┐
  │  Data Ingestion Service                      │
  │  ┌──────────────────────────────────────┐   │
  │  │  Normalize → MarketTick (v3.0)       │   │
  │  │  Assign trace_id (uuid4, per tick)   │   │
  │  │  Assign sequence_id (monotonic)      │   │
  │  └──────────────────────────────────────┘   │
  └─────────┬────────────────────┬──────────────┘
            │                    │
            │                    │ OHLCV candles (IntradayCandleStream)
            │                    ▼
            │          DynamoDB: candle-cache          ← Phase 3
            │          (TTL 2h, GSI: candle_open_time)
            │
         ┌──┴────────────────────────────────────┐
         │                                        │
         ▼                                        ▼
  Kafka: ticks.nse / ticks.us             DynamoDB: latest-prices
  (v3.0 TICK, 2 partitions)               (hot cache, TTL 24h)
         │                                        │
         │                                        ▼
         │                                  S3: tick-data/
         │                                  (Parquet, partitioned)
         │
         ├───── consumer group: strategy-v1      ┌──── poll every 500ms ← Phase 3
         │             │                          │
         │       ┌─────┴──────────────────────────┴──────────────────────────────┐
         │       │  Strategy Engine Service (4-loop asyncio.gather)              │
         │       │                                                                │
         │       │  Loop A — Kafka tick path:                                    │
         │       │    KafkaTickConsumer → StrategyRunner(TICK) → dispatch_tick() │
         │       │    └─► MomentumStrategy                                       │
         │       │                                                                │
         │       │  Loop B — DynamoDB candle path (Phase 3):                    │
         │       │    DynamoCandleConsumer.poll_new_candles()                    │
         │       │    → route by candle_interval + symbols                       │
         │       │    → StrategyRunner(CANDLE) → dispatch_bar()                  │
         │       │    └─► ORBStrategy (15min)                                    │
         │       │    └─► Scalp1mStrategy (1min)                                 │
         │       │    └─► VWAPReversionStrategy (5min)                           │
         │       │    └─► IntradayTrend15mStrategy (15min)                       │
         │       │    └─► PreCloseMomentumStrategy (15min)                       │
         │       │                                                                │
         │       │  Loop C — Config refresh (Phase 3):                          │
         │       │    StrategyConfigLoader.refresh_all() every 60s              │
         │       │    reads DynamoDB: strategy-config                            │
         │       │    → StrategyRunner.apply_config() (hot-reload, no restart)  │
         │       │                                                                │
         │       │  Loop D — Kill switch listener:                              │
         │       │    Kafka kill.switch topic                                    │
         │       │                                                                │
         │       │  Each StrategyRunner has its own CircuitBreaker:             │
         │       │    OPEN on 5 consecutive errors OR 10 errors/5min            │
         │       │    → returns None, does not affect other runners              │
         │       │                                                                │
         │       │  Signal stamping (Phase 3):                                   │
         │       │    signal.paper_trade ← strategy-config.paper_trade          │
         │       │    → KafkaSignalPublisher → signals.pending                  │
         │       └────────────────────────────────────────────────┬─────────────┘
         │                                                         │
         │                                              Kafka: signals.pending
         │                                              (SIGNAL_PENDING, 2 parts)
         │                                              (signal_id = sha256 determ.)
         │                                              (expires_at = +30s)
         │                                              (paper_trade = from config)
         │
         └───── consumer group: risk-v1
                                        │
                                  ┌─────┴──────────────────────┐
                                  │  Risk Engine                 │
                                  │  ┌────────────────────────┐ │
                                  │  │ validate(signal):       │ │
                                  │  │  1. kill_switch_check  │ │
                                  │  │  2. position_limit     │ │
                                  │  │  3. exposure_check     │ │
                                  │  │  4. stop_loss_check    │ │
                                  │  │  5. drawdown_check     │ │
                                  │  │  6. instrument_limit   │ │
                                  │  │  7. margin_check       │ │
                                  │  └────────────────────────┘ │
                                  │   APPROVED → signals.approved│
                                  │   REJECTED → ops.audit only  │
                                  └─────┬────────────────────────┘
                                        │
                          Kafka: signals.approved      DynamoDB: risk-state
                          (SIGNAL_APPROVED, 2 parts)   (P&L, kill switch)
                          (paper_trade preserved)
                                  │
                consumer group: execution-v1
                                  │
                           ┌──────┴──────────────────────┐
                           │  Execution Engine             │
                           │  → validate signal not stale  │
                           │  → DynamoDB idempotency check │
                           │  → paper_trade=True  → Alpaca paper endpoint
                           │  → paper_trade=False → live broker
                           │  → poll for fill (300ms)      │
                           │  → KafkaOrderEventsPublisher  │
                           └──────┬──────────────────────┘
                                  │
                       ┌──────────┴──────────┐
                       ▼                      ▼
                  Zerodha API            Alpaca API
                  (NSE live orders)      (US live / paper)
                       │                      │
                       └──────────┬───────────┘
                                  │ fill confirmed
                                  ▼
                          Kafka: orders.events
                          (ORDER_FILLED / ORDER_REJECTED)
                                  │
                          consumed by risk-v1
                          (real-time P&L update)
                                  │
                          DynamoDB: positions + orders
                          S3: trading-logs/ (audit)
```

---

## Phase 3: Candle-Cache Data Flow

The DynamoDB candle-cache path is how all five candle-based strategies receive market data without touching the Zerodha API rate limit budget.

```
data_ingestion / IntradayCandleStream
    │  writes every closed candle (1m, 5m, 15m intervals)
    │  PK = "{market}#{instrument}#{interval}#{candle_open_time}"
    │  TTL = 2h (auto-evict stale candles)
    ▼
DynamoDB: {prefix}-candle-cache
    ▲
    │  poll every 500ms
    │  FilterExpression: candle_open_time >= (now - 3min)
    │  in-memory dedup: trace_id → timestamp
    │    (prevents same candle dispatching twice)
    │    (eviction: every 30s, remove entries > 5min old)
strategy_engine / DynamoCandleConsumer
    │
    ▼  route by interval + symbols
StrategyRunner(CANDLE).dispatch_bar(bar)
    │
    ▼
strategy.on_bar(bar) → strategy.generate_signal()
    │
    ▼
signal (paper_trade stamped from strategy-config)
    │
    ▼
KafkaSignalPublisher → signals.pending
```

**Why DynamoDB, not Kafka, for candles:**

Candles at 1m/5m/15m are low-frequency (at most 1 per symbol per interval per minute). DynamoDB Scan with a 3-minute lookback window reads ~150 items maximum (50 symbols × 3 intervals). At 0.5 RCU per Scan with the candle_open_time GSI, the daily cost is negligible. The overlapping lookback + dedup guarantees at-least-once delivery without a Kafka consumer group or offset management.

---

## Phase 3: paper_trade Pipeline

Every signal carries a `paper_trade` boolean (added in Phase 3, schema v3.0, safe default `False`). The flag is controlled exclusively by DynamoDB strategy-config — strategies never set it directly.

```
DynamoDB: strategy-config
  paper_trade = True   ←── default for all strategies (until 5-day paper validation)
       │
       │ read every 60s by StrategyConfigLoader
       ▼
StrategyRunner._apply_paper_flag(signal)
  signal.paper_trade = True
       │
       ▼
signals.pending  ──►  risk_engine  ──►  signals.approved
                                              │
                                              ▼
                                    execution_engine
                                    if paper_trade=True:
                                        → Alpaca paper API endpoint
                                        → no real broker call
                                    if paper_trade=False:
                                        → live broker (Zerodha / Alpaca live)
```

**Go-live flow for a strategy:**
1. Operator monitors paper signals for 5 trading days — confirms P&L logic is correct
2. Operator runs: `python scripts/strategy/config.py go-live nse_orb_15m --env production`
3. CLI requires typing the strategy name to confirm (prevents accidental promotion)
4. DynamoDB: `strategy-config.paper_trade = False`
5. StrategyConfigLoader picks up the change within 60s — no service restart

---

## Phase 3: Hot-Reload Config Flow

```
DynamoDB: strategy-config
  (operator updates via scripts/strategy/config.py)
       │
       │ polled every 60s
       ▼
StrategyConfigLoader.refresh_all()
  for each registered runner:
    config, reset_cb = _load_config(strategy_name)
    runner.apply_config(config, reset_cb=reset_cb)
    if reset_cb and apply returned True:
        _clear_reset_flag(strategy_name)   ← writes reset=False back to DynamoDB

Configurable per strategy:
  enabled                              → skip dispatch immediately
  paper_trade                          → stamp on outgoing signals
  max_signals_per_day                  → daily hard cap (UTC day)
  circuit_breaker_threshold_consecutive → open after N consecutive errors
  circuit_breaker_threshold_rate        → open after N errors/5min
  circuit_breaker_reset                → operator-set flag for manual reset
```

**Circuit breaker manual reset flow:**
```
Operator sets:  scripts/strategy/reset_circuit_breaker.py nse_orb_15m --env production
                → DynamoDB: circuit_breaker_reset = True

Within 60s:     StrategyConfigLoader detects reset_cb=True
                → CircuitBreaker.reset() → OPEN → CLOSED immediately
                → StrategyConfigLoader._clear_reset_flag() → reset=False in DynamoDB

CloudWatch log: strategy_runner.circuit_breaker_reset_manual strategy=nse_orb_15m
```

---

## trace_id Propagation

Every trade lifecycle is traceable via a single `trace_id` UUID set at tick origin and never modified.

```
KafkaTickPublisher              sets trace_id = uuid4()       [TICK event]
    │
    ▼
strategy_engine                 propagates trace_id unchanged  [SIGNAL_PENDING event]
    │
    ▼
risk_engine                     propagates trace_id unchanged  [SIGNAL_APPROVED event]
    │
    ▼
execution_engine                propagates trace_id unchanged  [ORDER_FILLED event]
```

**CloudWatch Logs Insights query to trace a full trade lifecycle:**

```
fields @timestamp, service, event_type, signal_id, order_id, direction, price_at_signal
| filter trace_id = "your-trace-id-here"
| sort @timestamp asc
```

Returns: TICK → SIGNAL_PENDING → SIGNAL_APPROVED → ORDER_FILLED, with latencies at each hop.

---

## Signal ID Determinism

```
signal_id = sha256(
    strategy_name + "|" +
    symbol        + "|" +
    direction     + "|" +
    f"{price:.4f}" + "|" +
    signal_time.isoformat()
)[:32 hex chars]
```

**Why deterministic:** A restarted strategy engine replaying the same tick produces the same `signal_id`. The risk engine's DynamoDB conditional write (`attribute_not_exists(signal_id)`) silently discards duplicate signal decisions. No duplicate orders are placed.

---

## Kill Switch Data Flow

The kill switch is the highest-priority control plane path. It bypasses the normal message processing order.

```
Trigger sources:
  Manual:     ops script → DynamoDB risk-state.kill_switch = ACTIVE
  CloudWatch: alarm → SNS → Lambda → DynamoDB risk-state.kill_switch = ACTIVE
  Any service: produce to Kafka kill.switch topic

  Kafka kill.switch topic (1 partition, replicated to all consumer groups)
       │
       ├──► risk_engine kill-switch-listener task
       │    → reject all new signals immediately
       │    → publish KILL_SWITCH_ACTIVE to ops.audit
       │
       ├──► strategy_engine kill-switch-listener task
       │    → suppress all signal generation immediately
       │
       └──► execution_engine kill-switch-listener task
            → cancel all open broker orders
            → halt all new order placement

DynamoDB risk-state.kill_switch is also polled at every processing loop
iteration as a fallback for services that missed the Kafka event.
```

---

## DynamoDB Read/Write Patterns

| Table              | Writer(s)                          | Reader(s)                                           | Key Pattern                                            |
|-------------------|------------------------------------|-----------------------------------------------------|--------------------------------------------------------|
| `latest-prices`   | data_ingestion                     | strategy_engine (indicator seed)                    | `{MARKET}#{INSTRUMENT}` (PK)                           |
| `orders`          | execution_engine                   | execution_engine (idempotency check)                | `order_id` (PK)                                        |
| `positions`       | execution_engine, risk_engine      | risk_engine (exposure check)                        | `{market}#{instrument}` (PK)                           |
| `risk-state`      | risk_engine, kill-switch Lambda    | risk_engine (every loop iteration)                  | `key` (PK): `kill_switch`, `daily_pnl_{date}`          |
| `strategy-state`  | strategy_engine                    | strategy_engine (startup rehydrate)                 | `{strategy_name}#{symbol}` (PK)                        |
| `candle-cache`    | data_ingestion (IntradayCandleStream) | strategy_engine (DynamoCandleConsumer, 500ms poll) | `{market}#{instrument}#{interval}#{candle_open_time}` (PK); GSI on `candle_open_time` |
| `strategy-config` | ops CLI (scripts/strategy/config.py), StrategyConfigLoader (reset flag clear) | strategy_engine (every 60s) | `STRATEGY#{name}` (PK), `CONFIG#{env}` (SK) |

**DynamoDB consistency rules:**
- Kill switch reads: `ConsistentRead=True` (never stale)
- Position reads before risk decision: `ConsistentRead=True`
- All other reads: eventual consistency (cheaper, acceptable)
- All writes: conditional expressions to prevent race conditions

---

## S3 Write Patterns

| Bucket              | Written by        | Format        | Partitioning                          | Lifecycle         |
|--------------------|-------------------|---------------|---------------------------------------|-------------------|
| `tick-data`         | data_ingestion    | Parquet       | `{market}/{instrument}/{date}/{hour}` | Glacier 90d (dev/staging), 365d (prod) |
| `ohlcv-data`        | data_ingestion    | Parquet       | `{market}/{instrument}/{date}`        | Glacier 90d       |
| `trading-logs`      | risk_engine, execution_engine | JSON (newline-delimited) | `{service}/{date}` | Glacier 90d |
| `model-artifacts`   | offline training  | ONNX / pickle | `models/{name}/{version}/`            | No expiry         |

---

## Monitoring Data Flow

```
All services emit:
  → CloudWatch Logs (structured JSON, service log group)
  → CloudWatch Metrics (custom namespace per service)

Key custom metrics:
  TradingSystem/OrderPlacementLatencyMs   (execution_engine)
  TradingSystem/TickToSignalLatencyMs     (strategy_engine)
  TradingSystem/WebSocketGapSeconds       (data_ingestion)
  TradingSystem/DailyPnL                  (risk_engine)
  ZerodhaRateLimit/TokenBucketLevel       (execution_engine)
  ZerodhaRateLimit/FillDetectionLatencyMs (execution_engine)
  KafkaConsumerLag/{topic}/{group}        (all services)

CloudWatch Alarms:
  WebSocket gap > 10s  → SNS → Lambda → DynamoDB kill_switch = ACTIVE
  Kafka consumer lag spike → SNS → ops alert (signals not being consumed)
  P&L drawdown ≥ halt  → SNS → Lambda → DynamoDB kill_switch = ACTIVE
  Order rejection > 20%→ SNS → ops alert
  Consumer lag spike   → SNS → ops alert
```
