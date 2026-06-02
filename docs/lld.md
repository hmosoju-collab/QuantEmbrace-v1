# QuantEmbrace — Low-Level Design (LLD)

> **Document scope:** Internal implementation details. Explains HOW each component is implemented — class hierarchies, method signatures, message schemas, database schemas, state machines, concurrency models, and configuration. For system-level architecture and technology choices, see [hld.md](hld.md).

---

## Table of Contents

1. [Service Internal Architecture](#1-service-internal-architecture)
   - 1.1 [data_ingestion](#11-data_ingestion)
   - 1.2 [strategy_engine](#12-strategy_engine)
   - 1.3 [ai_engine](#13-ai_engine)
   - 1.4 [risk_engine](#14-risk_engine)
   - 1.5 [execution_engine](#15-execution_engine)
2. [Kafka Message Schemas](#2-kafka-message-schemas)
3. [DynamoDB Table Schemas](#3-dynamodb-table-schemas)
4. [State Machines](#4-state-machines)
5. [Idempotency and Deduplication](#5-idempotency-and-deduplication)
6. [Shared Components](#6-shared-components)
7. [Configuration Management](#7-configuration-management)
8. [Error Handling and Retry Policy](#8-error-handling-and-retry-policy)
9. [Internal API Contracts (Python Types)](#9-internal-api-contracts-python-types)
10. [Health Check Specification](#10-health-check-specification)

---

## 1. Service Internal Architecture

### 1.1 data_ingestion

**File:** `services/data_ingestion/`

**Responsibility:** Receive raw broker WebSocket events, normalize to unified format, and publish to Kafka + DynamoDB.

#### Class Hierarchy

```
DataIngestionService (main entry point, asyncio-based)
  │
  ├── KiteTickerAdapter         (Zerodha WebSocket handler)
  │     └── on_ticks()          → normalizes to MarketTick[]
  │
  ├── AlpacaStreamAdapter       (Alpaca WebSocket handler)
  │     └── on_trade()          → normalizes to MarketTick
  │     └── on_quote()          → normalizes to MarketTick
  │
  ├── KafkaTickPublisher        (publishes to ticks.nse, ticks.us)
  │     └── publish(tick)       → JSON-encode v3.0 envelope → Kafka
  │
  ├── DynamoTickWriter          (writes to latest-prices table)
  │     └── write(tick)         → conditional put with TTL = +24h
  │
  ├── IntradayCandleStream      (builds OHLCV candles from ticks)
  │     └── on_tick()           → updates open candle per symbol per interval
  │     └── flush_closed()      → writes closed candle to DynamoDB candle-cache
  │
  ├── FeatureEngine             (computes technical indicators per candle close)
  │     └── compute(candle)     → RSI-14, EMA-9/21, VWAP, ATR-14, ADX-14, MACD, vol_ratio
  │     └── write(features)     → DynamoDB features table (LATEST + CANDLE#{ts} rows)
  │
  └── ZerodhaTokenManager       (handles daily OAuth token refresh)
        └── ensure_valid_token() → checks expiry, re-auths if needed
```

#### Concurrency Model

Two independent `asyncio` tasks run concurrently:
1. `_nse_feed_task()` — runs Zerodha WebSocket loop
2. `_us_feed_task()` — runs Alpaca WebSocket loop

Both tasks are supervised by a `asyncio.TaskGroup`. If one crashes, the other continues. The crashed task is restarted by the outer restart loop with exponential backoff.

#### Tick Normalization

Zerodha sends instrument tokens (integers). The service maintains an in-memory mapping `token → symbol` loaded from the instrument registry at startup. Every raw tick is mapped to a `MarketTick` dataclass before publishing.

```python
# Zerodha raw tick → MarketTick
raw = {"instrument_token": 738561, "last_price": 2453.5, ...}
symbol = self._token_map[738561]  # → "RELIANCE"
tick = MarketTick(
    market=Market.NSE,
    instrument=symbol,
    ltp=Decimal(str(raw["last_price"])),
    ...
    timestamp=utc_now(),
)
```

#### Candle Construction

Candles are built in-memory per symbol per interval (1min, 5min, 15min). A candle closes when its time boundary elapses:

```
1min candle for RELIANCE:
  opens at 09:15:00 → closes at 09:16:00
  PK: "NSE#RELIANCE#1minute#2026-05-15T09:15:00Z"
  TTL: 2 hours from close time
  Written to DynamoDB candle-cache on close
```

---

### 1.2 strategy_engine

**File:** `services/strategy_engine/`

**Responsibility:** Consume market data, run trading strategies, and publish trading signals.

#### Class Hierarchy

```
StrategyEngineService
  │
  ├── KafkaTickConsumer          (strategy-v1 group, ticks.nse + ticks.us)
  ├── DynamoCandleConsumer       (polls candle-cache every 500ms)
  ├── StrategyConfigLoader       (hot-reloads strategy-config every 60s)
  ├── KafkaSignalPublisher       (publishes to signals.pending)
  │
  └── StrategyRunner[]           (one per registered strategy)
        ├── CircuitBreaker       (per-strategy failure isolation)
        ├── dispatch_tick(tick)  → calls strategy.on_tick() → generate_signal()
        ├── dispatch_bar(bar)    → calls strategy.on_bar() → generate_signal()
        └── _apply_paper_flag()  → overwrites signal.paper_trade from DynamoDB config

Strategies (all inherit BaseStrategy):
  ├── MomentumStrategy          (InterfaceType.TICK)
  ├── ORBStrategy               (InterfaceType.CANDLE, 15min)
  ├── Scalp1mStrategy           (InterfaceType.CANDLE, 1min)
  ├── VWAPReversionStrategy     (InterfaceType.CANDLE, 5min)
  ├── IntradayTrend15mStrategy  (InterfaceType.CANDLE, 15min)
  └── PreCloseMomentumStrategy  (InterfaceType.CANDLE, 15min, NSE only)
```

#### The 4-Loop asyncio.gather

The service's `run()` method runs four concurrent loops that never block each other:

```python
await asyncio.gather(
    self._kafka_processing_loop(),   # TICK runners — polls Kafka via asyncio.to_thread
    self._candle_processing_loop(),  # CANDLE runners — polls DynamoDB every 500ms
    self._config_refresh_loop(),     # hot-reload strategy-config every 60s
    self._kill_switch_loop(),        # Kafka risk.kill-switch topic listener
)
```

Each loop is an independent `asyncio.Task`. A crash in the candle loop (e.g., a DynamoDB timeout) does not affect the tick loop (which processes MomentumStrategy signals). This is the key concurrency design choice.

#### CircuitBreaker State Machine

Every `StrategyRunner` wraps its strategy in a `CircuitBreaker`:

```
CLOSED (normal operation)
  │
  ├── 5 consecutive errors ────────────────────────────────────► OPEN
  ├── 10 errors in 5 minutes ───────────────────────────────────► OPEN
  │
OPEN (strategy disabled, returns None for all signals)
  │
  ├── 300 seconds elapsed ──────────────────────────────────────► HALF_OPEN
  ├── operator sets circuit_breaker_reset=True in DynamoDB ────► CLOSED (immediate)
  │
HALF_OPEN (testing mode — processing resumes)
  │
  ├── 3 consecutive successes ────────────────────────────────── CLOSED
  └── 1 failure ─────────────────────────────────────────────── OPEN (re-opens)
```

Circuit breaker state is **in-memory only** — it is not persisted to DynamoDB. A service restart resets all circuit breakers to CLOSED.

#### Signal ID Determinism

```python
def _compute_signal_id(
    strategy_name: str,
    symbol: str,
    direction: str,
    price: Decimal,
    signal_time: datetime,
) -> str:
    raw = f"{strategy_name}|{symbol}|{direction}|{price:.4f}|{signal_time.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
```

The same strategy generating the same signal at the same time always produces the same `signal_id`. This is the foundation of idempotent order submission.

#### Signal Expiry

```python
signal.expires_at = signal.generated_at + timedelta(seconds=30)
```

The risk engine rejects signals where `utc_now() > signal.expires_at`. This prevents stale signals (queued during a Kafka backlog) from executing when conditions have changed.

#### DynamoCandleConsumer Deduplication

The candle consumer uses an in-memory set to prevent dispatching the same candle twice:

```python
class DynamoCandleConsumer:
    _seen_trace_ids: dict[str, datetime]  # trace_id → seen_at

    def poll_new_candles(self) -> list[CandleBar]:
        # FilterExpression: candle_open_time >= (now - 3 minutes)
        # 3-minute window ensures no candle is missed during 500ms polling interval
        results = self._dynamo.query(...)
        new_candles = []
        for item in results:
            if item["trace_id"] not in self._seen_trace_ids:
                self._seen_trace_ids[item["trace_id"]] = utc_now()
                new_candles.append(CandleBar.from_dynamo(item))

        # Evict entries older than 5 minutes every 30 seconds
        self._maybe_evict()
        return new_candles
```

---

### 1.3 ai_engine

**File:** `services/ai_engine/`

**Responsibility:** Enrich pending signals with regime classification and quality scoring, then publish enriched signals.

#### Class Hierarchy

```
AIEngineService
  │
  ├── KafkaSignalConsumer         (aiengine-v1 group, signals.pending)
  ├── KafkaEnrichedPublisher      (publishes to signals.enriched)
  │
  └── SignalEnricher              (orchestrates enrichment pipeline)
        ├── FeatureReader         (reads pre-computed features from DynamoDB)
        │     └── read(symbol, interval) → FeatureSet | None
        │         staleness check: 1min→5min, 5min→11min, 15min→31min
        │
        ├── ModelRegistry         (loads/hot-reloads models from S3)
        │     ├── load(name)      → joblib.load(s3_path)
        │     ├── hot_reload()    → checks S3 ETag every 60s, swaps if changed
        │     └── _lock           → threading.RLock (thread-safe model swap)
        │
        ├── RegimeClassifier      (HMM wrapper)
        │     └── classify(features) → RegimeLabel, confidence
        │         degrade on exception: ("unknown", 0.0)
        │
        └── SignalQualityScorer   (GBT wrapper)
              └── score(features, signal) → quality_score ∈ [0.0, 1.0], filtered
                  degrade on exception: (0.5, False)
```

#### Enrichment Pipeline

```python
async def enrich(self, signal: Signal) -> EnrichedSignal:
    start_time = time.monotonic()

    # Step 1: Read features (may return None if stale)
    features = await self._feature_reader.read(signal.symbol, signal.interval)

    # Step 2: Classify regime (degrades to "unknown" if features=None or exception)
    regime, regime_confidence = self._regime_classifier.classify(features)

    # Step 3: Score signal quality (degrades to 0.5 if features=None or exception)
    quality_score, filtered = self._quality_scorer.score(features, signal)

    latency_ms = (time.monotonic() - start_time) * 1000

    return EnrichedSignal(
        **signal.__dict__,          # All v3.0 fields pass through unchanged
        regime=regime,
        regime_confidence=regime_confidence,
        quality_score=quality_score,
        filtered=filtered,
        enriched_at=utc_now(),
        enrichment_latency_ms=latency_ms,
        model_versions=self._model_registry.current_versions(),
        schema_version="4.0",
    )
```

The enrichment pipeline **never raises**. Every component has a try/except that returns degraded defaults instead of propagating exceptions.

#### Model Hot-Reload

```python
class ModelRegistry:
    def _hot_reload_loop(self):
        while True:
            await asyncio.sleep(60)
            for name, current_version in self._current_versions.items():
                s3_etag = self._s3.head_object(...)["ETag"]
                if s3_etag != current_version:
                    new_model = joblib.load(download_from_s3(...))
                    with self._lock:
                        self._models[name] = new_model
                        self._current_versions[name] = s3_etag
```

Model swap is atomic (protected by `threading.RLock`). A request being processed during a reload sees either the old model or the new model entirely — never a partially-swapped state.

---

### 1.4 risk_engine

**File:** `services/risk_engine/`

**Responsibility:** Validate signals against all risk rules and publish approved signals. The critical gatekeeper.

#### Class Hierarchy

```
RiskEngineService
  │
  ├── KafkaEnrichedConsumer        (risk-v1, signals.enriched — primary path)
  ├── KafkaPendingConsumer         (risk-v1-fallback, signals.pending — fallback path)
  ├── KafkaApprovedPublisher       (publishes to signals.approved)
  ├── KafkaOrderEventsConsumer     (risk-v1-order-events, orders.events)
  ├── KafkaAuditPublisher          (publishes to ops.audit)
  │
  ├── EnrichmentWatchdog           (monitors AI lag, switches consumer path)
  │
  ├── RiskValidationPipeline       (runs 11 validators in sequence)
  │     ├── SignalAgeValidator     (1st: cheapest check — reject stale immediately)
  │     ├── KillSwitchValidator    (2nd: most critical — check before any state read)
  │     ├── PositionLimitValidator (is max_positions exceeded?)
  │     ├── ExposureValidator      (would this trade breach exposure cap?)
  │     ├── DailyLossValidator     (is daily P&L beyond the loss limit?)
  │     ├── MarginValidator        (is there sufficient margin at the broker?)
  │     ├── SlippageValidator      (has price moved too much since signal?)
  │     ├── SpreadGateValidator    (is bid-ask spread acceptable?)
  │     ├── SectorLimitValidator   (would this breach sector concentration limit?)
  │     ├── LiquidityValidator     (is order size within ADV limits?)
  │     └── QualityScoreValidator  (does quality_score meet minimum threshold?)
  │
  ├── PositionManager              (reads/writes positions table)
  ├── PnLTracker                   (maintains daily P&L from orders.events)
  └── KillSwitchManager            (reads/writes kill switch state)
```

#### The 11 Validators — Sequence and Logic

Validators run in a fixed sequence. The first failure short-circuits (subsequent validators are not run):

| # | Validator | Logic | Failure Reason |
|---|---|---|---|
| 1 | SignalAge | `utc_now() <= signal.expires_at` | `signal_age_exceeded` |
| 2 | KillSwitch | `kill_switch.active == False` (ConsistentRead) | `kill_switch_active` |
| 3 | Position | `open_positions < max_positions[market]` | `max_positions_reached` |
| 4 | Exposure | `current_exposure + new_trade_value <= max_exposure_pct * portfolio_value` | `exposure_limit_exceeded` |
| 5 | DailyLoss | `daily_pnl > -max_daily_loss_pct * portfolio_value` | `daily_loss_limit_reached` |
| 6 | Margin | `available_margin > order_value * (1 + margin_buffer_pct)` | `insufficient_margin` |
| 7 | Slippage | `abs(current_price - signal.price) / signal.price <= max_slippage_pct` | `slippage_exceeded` |
| 8 | SpreadGate | `(ask - bid) / mid_price <= max_spread_bps / 10000` | `spread_too_wide` |
| 9 | Sector | `sector_exposure[signal.sector] + new_trade <= max_sector_pct * portfolio_value` | `sector_limit_exceeded` |
| 10 | Liquidity | `order_quantity <= adv_pct * avg_daily_volume` | `liquidity_insufficient` |
| 11 | Quality | `signal.quality_score >= min_quality_score` | `quality_score_too_low` |

**Ordering rationale:**
- `SignalAge` first — cheapest check, eliminates stale signals without any DynamoDB reads
- `KillSwitch` second — highest priority safety control, uses `ConsistentRead=True`
- `Quality` last — only paid for with the most expensive read (features) after all simpler checks pass

#### EnrichmentWatchdog

```python
class EnrichmentWatchdog:
    LAG_THRESHOLD = 10      # messages
    ACTIVATE_WINDOW = 2     # consecutive checks at threshold
    RECOVER_WINDOW = 5      # consecutive clear checks

    _consecutive_lag_hits: int = 0
    _consecutive_clear_hits: int = 0
    _fallback_active: bool = False

    async def _check_loop(self):
        while True:
            await asyncio.sleep(0.5)
            lag = await self._get_consumer_lag("aiengine-v1", "signals.pending")

            if lag >= self.LAG_THRESHOLD:
                self._consecutive_lag_hits += 1
                self._consecutive_clear_hits = 0
                if self._consecutive_lag_hits >= self.ACTIVATE_WINDOW and not self._fallback_active:
                    self._activate_fallback()
            else:
                self._consecutive_clear_hits += 1
                self._consecutive_lag_hits = 0
                if self._consecutive_clear_hits >= self.RECOVER_WINDOW and self._fallback_active:
                    self._deactivate_fallback()

    def _activate_fallback(self):
        self._fallback_active = True
        # Switch risk_engine to read from signals.pending (risk-v1-fallback)
        # Publish CloudWatch metric: EnrichmentFallbackActive = 1
        logger.warning("enrichment_fallback_activated")

    def _deactivate_fallback(self):
        self._fallback_active = False
        # Switch risk_engine back to signals.enriched (risk-v1)
        # Publish CloudWatch metric: EnrichmentFallbackActive = 0
        logger.info("enrichment_fallback_deactivated")
```

#### Risk Decision Audit

Every validation decision (approve or reject) is recorded to `ops.audit`:

```python
@dataclass(frozen=True)
class RiskDecision:
    risk_decision_id: str           # uuid4, unique per decision
    signal_id: str                  # Links to originating signal
    trace_id: str                   # Links to originating tick
    decision: Literal["APPROVED", "REJECTED"]
    rejection_reason: Optional[str]  # e.g., "daily_loss_limit_reached"
    validators_passed: list[str]
    validator_failed: Optional[str]
    risk_state_snapshot: dict       # P&L, positions, exposure at decision time
    decided_at: datetime
    enrichment_used: bool           # Was enriched signal or fallback pending signal?
```

---

### 1.5 execution_engine

**File:** `services/execution_engine/`

**Responsibility:** Place approved signals as broker orders. Track fills. Report back to risk engine.

#### Class Hierarchy

```
ExecutionEngineService
  │
  ├── KafkaApprovedConsumer        (execution-v1, signals.approved)
  ├── KafkaKillSwitchConsumer      (execution-v1-kill-switch, risk.kill-switch)
  ├── KafkaOrderEventsPublisher    (publishes to orders.events)
  ├── KafkaAuditPublisher          (publishes to ops.audit)
  │
  ├── OrderManager                 (DynamoDB order state management)
  │     ├── create(signal)         → conditional write (attribute_not_exists)
  │     ├── update_status(id, st)  → conditional write (ConditionExpression)
  │     └── get(signal_id)         → check for existing order
  │
  ├── UniverseOrderValidator       (hard gate — symbol must be in current universe snapshot)
  │     └── validate(signal)       → raises if symbol not in UNIVERSE_MODE snapshot for today
  │
  ├── SmartRouter                  (maps signal.market to broker adapter)
  │     └── route(signal)          → ZerodhaAdapter | AlpacaAdapter | PaperSimulator
  │
  ├── ZerodhaAdapter               (Kite Connect API wrapper)
  │     ├── place_order()          → kite.place_order()
  │     ├── BulkOrderPoller        → polls kite.orders() every 300ms (O(1) fill detection)
  │     └── RateLimiter            → token bucket, 10 req/sec
  │
  ├── AlpacaAdapter                (Alpaca REST + WebSocket — US equities only)
  │     ├── place_order()          → alpaca.submit_order()
  │     └── PaperEndpoint          → uses paper-api.alpaca.markets when paper_trade=True (US only)
  │
  └── PaperSimulator               (NSE paper trading — no Zerodha paper endpoint exists)
        └── simulate_fill()        → deterministic SHA256-based fill price
```

#### Idempotent Order Submission

```python
async def process_approved_signal(self, signal: ApprovedSignal) -> None:
    # Step 1: Check for existing order with this signal_id
    existing = await self._order_manager.get_by_signal_id(signal.signal_id)
    if existing:
        logger.info("order_already_exists", signal_id=signal.signal_id,
                    existing_status=existing.status)
        return  # Silent no-op — same signal processed twice

    # Step 2: Validate signal has not expired
    if utc_now() > signal.expires_at:
        logger.warning("signal_expired_at_execution", signal_id=signal.signal_id)
        return

    # Step 3: Create order record with conditional write
    order = await self._order_manager.create(
        signal_id=signal.signal_id,
        status=OrderStatus.PENDING,
    )
    # DynamoDB: attribute_not_exists(signal_id) — if another instance raced here,
    # the condition fails and raises ConditionalCheckFailedException → caught, return

    # Step 4: Route and place order
    broker = self._router.route(signal)
    broker_response = await broker.place_order(order_request)

    # Step 5: Update status
    await self._order_manager.update_status(
        order.order_id, OrderStatus.PLACED,
        broker_order_id=broker_response.order_id,
    )
```

#### Paper Simulator

When `signal.paper_trade = True` for NSE signals (Zerodha does not offer a paper trading endpoint), the `PaperSimulator` provides a deterministic fill:

```python
class PaperSimulator:
    def simulate_fill(self, signal: ApprovedSignal, current_ltp: Decimal) -> Fill:
        # Deterministic slippage based on signal_id hash
        # Same signal always gets same simulated fill → reproducible backtesting
        seed = int(hashlib.sha256(signal.signal_id.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        slippage_bps = rng.uniform(-5, 15)  # -0.05% to +0.15% (buy typically pays more)
        fill_price = current_ltp * (1 + Decimal(str(slippage_bps / 10000)))
        return Fill(
            fill_id=str(uuid4()),
            order_id=signal.order_id,
            quantity=signal.quantity,
            price=fill_price.quantize(Decimal("0.01")),
            timestamp=utc_now(),
        )
```

#### BulkOrderPoller (Zerodha Fill Detection)

Instead of polling each order's status individually (O(N) API calls), the `BulkOrderPoller` calls `kite.orders()` once every 300ms to get all open orders in one call (O(1)):

```python
class BulkOrderPoller:
    async def _poll_loop(self):
        while True:
            await asyncio.sleep(0.3)
            all_broker_orders = await asyncio.to_thread(self._kite.orders)
            broker_map = {o["order_id"]: o for o in all_broker_orders}

            for our_order in await self._order_manager.get_open_orders():
                broker_order = broker_map.get(our_order.broker_order_id)
                if broker_order and broker_order["status"] == "COMPLETE":
                    await self._handle_fill(our_order, broker_order)
```

#### Kill Switch Listener (Dedicated Task)

The kill switch listener runs in a completely separate `asyncio.Task`, independent from the main order processing loop. This ensures it responds immediately even if the main loop is busy processing a batch of orders:

```python
async def _kill_switch_listener_task(self):
    # auto.offset.reset=latest — only process future events, not replays
    consumer = create_consumer(group_id="execution-v1-kill-switch",
                               auto_offset_reset="latest")
    consumer.subscribe(["risk.kill-switch"])
    while True:
        msg = await asyncio.to_thread(consumer.poll, timeout=1.0)
        if msg and msg.value()["event_type"] == "KILL_SWITCH_ACTIVE":
            await self._handle_kill_switch()

async def _handle_kill_switch(self):
    self._trading_halted = True  # checked before every order placement
    open_orders = await self._order_manager.get_open_orders()
    for order in open_orders:
        broker = self._router.route_by_market(order.market)
        await broker.cancel_order(order.broker_order_id)
    logger.critical("kill_switch_executed", orders_cancelled=len(open_orders))
```

---

#### Monitoring Wire-Up

`ExecutionService` owns a single `LiveCounters` dataclass instance shared by reference with all three exit-path components. No locks are needed — all components run inside the single asyncio event loop.

**Wired components:**

| Component | File | Counters written |
|---|---|---|
| `TradeExitEngine` | `monitors/trade_exit_engine.py` | `tee_running`, `tee_poll_interval`, `tee_stop_loss_hits`, `tee_take_profit_hits`, `tee_trailing_activated`, `tee_unmanaged_detections`, `tee_latest_events` |
| `ExitOrderRouter` | `exit/exit_order_router.py` | `router_paper_exits`, `router_idempotency_skips`, `router_idempotency_successes`, `router_failed_routes`, `router_live_attempts`, `router_live_blocked`, `router_backtest_exits`, `realized_pnl` |
| `MISSquareOffManager` | `mis_square_off.py` | `mis_armed`, `mis_positions_discovered`, `mis_long_count`, `mis_short_count`, `mis_orders_placed`, `mis_orders_rejected`, `mis_positions_flat`, `mis_at_deadline`, `mis_kill_switch_activated` |

**Startup reconciliation (also wired):**

After `_run_startup_position_reconciliation()` completes, `ExecutionService` writes `ReconciliationReport` results into `LiveCounters`:

```python
self._live_counters.recon_ran = True
self._live_counters.recon_mode = "paper"          # or "live"
self._live_counters.recon_mismatches = report.total_mismatches
self._live_counters.recon_repairs   = report.repairs_completed
self._live_counters.recon_criticals = report.critical_alerts
```

**`_monitoring_flush_loop()` background task:**

Serialises `LiveCounters` to JSON every 60 s using an atomic write so the offline CLI never reads a partially-written file:

```python
async def _monitoring_flush_loop(self) -> None:
    output_path   = os.environ.get("QE_MONITORING_COUNTERS_PATH", "/tmp/qe_live_counters.json")
    flush_interval = float(os.environ.get("QE_MONITORING_FLUSH_INTERVAL", "60"))
    while self._running:
        payload = dataclasses.asdict(self._live_counters)
        tmp_path = output_path + ".tmp"
        with open(tmp_path, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(tmp_path, output_path)   # atomic on POSIX
        await asyncio.sleep(flush_interval)
```

**Env vars:**

| Variable | Default | Purpose |
|---|---|---|
| `QE_MONITORING_COUNTERS_PATH` | `/tmp/qe_live_counters.json` | Output path for `LiveCounters` JSON |
| `QE_MONITORING_FLUSH_INTERVAL` | `60` | Flush interval in seconds |

**Offline CLI:**

```bash
# Live counters (execution service running)
python scripts/monitoring/paper_trading_monitor.py \
  --counters /tmp/qe_live_counters.json --watch 60

# Offline with sample stub (no services required)
python scripts/monitoring/paper_trading_monitor.py \
  --counters scripts/monitoring/sample_counters.json
```

---

## 2. Kafka Message Schemas

### v3.0 Envelope (ticks.*, signals.pending, signals.approved)

```json
{
  "event_id":       "uuid4 — unique per message (not per trade)",
  "trace_id":       "uuid4 — set at tick origin, never modified downstream",
  "event_type":     "TICK | SIGNAL_PENDING | SIGNAL_APPROVED | ORDER_PLACED | ORDER_FILLED | KILL_SWITCH_ACTIVE",
  "schema_version": "3.0",
  "source":         "data_ingestion | strategy_engine | risk_engine | execution_engine",
  "published_time": "ISO8601 UTC"
}
```

### Tick Event (ticks.nse, ticks.us)

```json
{
  "event_id":       "f3a1b2c4-...",
  "trace_id":       "a1b2c3d4-...",
  "event_type":     "TICK",
  "schema_version": "3.0",
  "source":         "data_ingestion",
  "published_time": "2026-05-15T03:45:00.123Z",

  "market":         "NSE",
  "instrument":     "RELIANCE",
  "ltp":            2453.50,
  "bid":            2453.00,
  "ask":            2454.00,
  "bid_qty":        500,
  "ask_qty":        300,
  "volume":         1234567,
  "open":           2450.00,
  "high":           2465.00,
  "low":            2448.00,
  "close":          2448.00,
  "change_pct":     0.23,
  "exchange_ts":    "2026-05-15T03:44:59.980Z",
  "sequence_id":    12345678
}
```

### SIGNAL_PENDING Event (signals.pending)

```json
{
  "event_id":       "b2c3d4e5-...",
  "trace_id":       "a1b2c3d4-...",
  "event_type":     "SIGNAL_PENDING",
  "schema_version": "3.0",
  "source":         "strategy_engine",
  "published_time": "2026-05-15T03:45:00.230Z",

  "signal_id":      "a3f2e1d0b9c8...",
  "strategy_name":  "nse_momentum_v1",
  "strategy_id":    "STR-001",
  "market":         "NSE",
  "instrument":     "RELIANCE",
  "direction":      "BUY",
  "quantity":       20,
  "order_type":     "MARKET",
  "limit_price":    null,
  "stop_price":     null,
  "stop_loss":      2405.43,
  "take_profit":    2502.57,
  "confidence":     0.75,
  "price_at_signal": 2453.50,
  "generated_at":   "2026-05-15T03:45:00.200Z",
  "expires_at":     "2026-05-15T03:45:30.200Z",
  "paper_trade":    true,
  "product_type":   "MIS",
  "metadata": {
    "short_ma": 2420.50,
    "long_ma":  2400.00,
    "crossover": "golden"
  }
}
```

### SIGNAL_ENRICHED Event (signals.enriched) — v4.0

```json
{
  "event_id":       "c3d4e5f6-...",
  "trace_id":       "a1b2c3d4-...",
  "event_type":     "SIGNAL_ENRICHED",
  "schema_version": "4.0",
  "source":         "ai_engine",
  "published_time": "2026-05-15T03:45:00.245Z",

  "signal_id":      "a3f2e1d0b9c8...",
  "strategy_name":  "nse_momentum_v1",
  "market":         "NSE",
  "instrument":     "RELIANCE",
  "direction":      "BUY",
  "quantity":       20,
  "confidence":     0.75,
  "price_at_signal": 2453.50,
  "generated_at":   "2026-05-15T03:45:00.200Z",
  "expires_at":     "2026-05-15T03:45:30.200Z",
  "paper_trade":    true,
  "product_type":   "MIS",

  "regime":                "trending",
  "regime_confidence":     0.81,
  "quality_score":         0.82,
  "filtered":              false,
  "enriched_at":           "2026-05-15T03:45:00.240Z",
  "enrichment_latency_ms": 10.2,
  "model_versions": {
    "regime_classifier":   "hmm_v2.1.0",
    "quality_scorer":      "gbt_v1.4.0"
  }
}
```

### SIGNAL_APPROVED Event (signals.approved)

```json
{
  "event_id":       "d4e5f6a7-...",
  "trace_id":       "a1b2c3d4-...",
  "event_type":     "SIGNAL_APPROVED",
  "schema_version": "3.0",
  "source":         "risk_engine",
  "published_time": "2026-05-15T03:45:00.260Z",

  "signal_id":       "a3f2e1d0b9c8...",
  "risk_decision_id": "rsk-abc-789",
  "strategy_name":   "nse_momentum_v1",
  "market":          "NSE",
  "instrument":      "RELIANCE",
  "direction":       "BUY",
  "quantity":        20,
  "order_type":      "MARKET",
  "stop_loss":       2405.43,
  "take_profit":     2502.57,
  "price_at_signal": 2453.50,
  "paper_trade":     true,
  "product_type":    "MIS",
  "time_in_force":   "DAY",
  "expires_at":      "2026-05-15T03:45:30.200Z",
  "validators_passed": [
    "signal_age", "kill_switch", "position", "exposure",
    "daily_loss", "margin", "slippage", "spread",
    "sector", "liquidity", "quality"
  ],
  "risk_state_snapshot": {
    "open_positions": 3,
    "gross_exposure_pct": 31.2,
    "daily_pnl": 8200.00,
    "available_margin": 850000.00
  }
}
```

### ORDER_FILLED Event (orders.events)

```json
{
  "event_id":       "e5f6a7b8-...",
  "trace_id":       "a1b2c3d4-...",
  "event_type":     "ORDER_FILLED",
  "schema_version": "3.0",
  "source":         "execution_engine",
  "published_time": "2026-05-15T03:45:00.490Z",

  "order_id":         "ord-xyz-123",
  "signal_id":        "a3f2e1d0b9c8...",
  "risk_decision_id": "rsk-abc-789",
  "broker_order_id":  "230515000123456",
  "market":           "NSE",
  "instrument":       "RELIANCE",
  "direction":        "BUY",
  "requested_qty":    20,
  "filled_qty":       20,
  "avg_fill_price":   2454.00,
  "slippage":         0.50,
  "paper_trade":      true,
  "execution_latency_ms": 260,
  "placed_at":   "2026-05-15T03:45:00.310Z",
  "filled_at":   "2026-05-15T03:45:00.480Z"
}
```

---

## 3. DynamoDB Table Schemas

### orders

Owned by `execution_engine`. Idempotency key + order lifecycle.

```
PK: order_id        (String)
SK: —
GSI: signal_id-index → PK: signal_id

Attributes:
  order_id            String   PK — uuid4
  signal_id           String   GSI key — sha256 hash from strategy
  risk_decision_id    String   Links to ops.audit entry
  broker_order_id     String   Zerodha/Alpaca order ID
  market              String   "NSE" | "US"
  instrument          String   "RELIANCE"
  direction           String   "BUY" | "SELL"
  quantity            Number
  filled_quantity     Number
  avg_fill_price      Number
  status              String   "PENDING" | "PLACED" | "FILLED" | "REJECTED" | "CANCELLED" | "FAILED"
  paper_trade         Boolean
  placed_at           String   ISO8601 UTC
  filled_at           String   ISO8601 UTC
  updated_at          String   ISO8601 UTC
  trace_id            String   For end-to-end tracing
```

### positions

Owned by `execution_engine` (writes on fill), read by `risk_engine` (exposure checks).

```
PK: market_instrument   String   "NSE#RELIANCE"
SK: —

Attributes:
  market              String   "NSE" | "US"
  instrument          String   "RELIANCE"
  side                String   "LONG" | "SHORT" | "FLAT"
  quantity            Number   Absolute quantity
  avg_entry_price     Number
  current_price       Number   Updated on every fill event
  unrealized_pnl      Number
  realized_pnl        Number
  stop_loss_price     Number
  take_profit_price   Number
  strategy_name       String
  opened_at           String   ISO8601 UTC
  last_updated        String   ISO8601 UTC
  order_ids           List     [String]
```

### risk-state

Owned by `risk_engine`. Kill switch + daily counters.

```
PK: key     String

Items:
  key="kill_switch"
    active          Boolean
    activated_at    String
    activated_by    String   "system:drawdown" | "manual:ops"
    reason          String

  key="daily_pnl_{YYYY-MM-DD}"
    date            String   "2026-05-15"
    realized_pnl    Number   INR or USD
    unrealized_pnl  Number
    total_pnl       Number
    updated_at      String

  key="ENRICHMENT_CONFIG/GLOBAL"
    lag_threshold         Number   default: 10
    activate_window       Number   default: 2
    recover_window        Number   default: 5
```

### candle-cache

Owned by `data_ingestion` (write), read by `strategy_engine` (500ms poll).

```
PK: candle_key    String   "{market}#{instrument}#{interval}#{candle_open_time_iso}"
SK: —
GSI: candle_open_time_index → PK: candle_open_time

Attributes:
  candle_key          String   PK
  market              String   "NSE"
  instrument          String   "RELIANCE"
  interval            String   "1minute" | "5minute" | "15minute"
  candle_open_time    String   ISO8601 UTC — GSI key
  open                Number
  high                Number
  low                 Number
  close               Number
  volume              Number
  trace_id            String   Deterministic hash for deduplication
  written_at          String   ISO8601 UTC
  expires_at          Number   Unix timestamp — TTL = 2 hours after candle_open_time
```

### features

Owned by `data_ingestion` (FeatureEngine writes). Read by `ai_engine` (FeatureReader).

```
PK: symbol_interval   String   "RELIANCE#15minute"
SK: row_type          String   "LATEST" | "CANDLE#{candle_open_time_iso}"

Attributes:
  symbol              String
  interval            String
  rsi_14              Number
  ema_9               Number
  ema_21              Number
  vwap                Number
  atr_14              Number
  adx_14              Number
  macd                Number
  macd_signal         Number
  volume_ratio        Number   current_volume / avg_volume_20d
  computed_at         String   ISO8601 UTC
  expires_at          Number   TTL: LATEST=+24h, CANDLE#{ts}=+7d
```

### strategy-config

Owned by ops CLI (write), read by `strategy_engine` (hot-reload every 60s).

```
PK: strategy_key   String   "STRATEGY#{strategy_name}"
SK: env_key        String   "CONFIG#{environment}"   e.g., "CONFIG#production"

Attributes:
  strategy_name                          String
  enabled                                Boolean
  paper_trade                            Boolean   default: True
  max_signals_per_day                    Number
  circuit_breaker_threshold_consecutive  Number   default: 5
  circuit_breaker_threshold_rate         Number   default: 10
  circuit_breaker_reset                  Boolean  operator-set flag for manual reset
  updated_at                             String
```

### kill-switch

Owned by `risk_engine`. Kill switch state (also tracked in risk-state).

```
PK: id    String   Always "global"

Attributes:
  active           Boolean
  activated_at     String
  activated_by     String
  reason           String
  deactivated_at   String
```

### sessions

Owned by `data_ingestion`. Broker session tokens.

```
PK: broker    String   "zerodha" | "alpaca"

Attributes:
  access_token      String   (encrypted at rest — KMS)
  expires_at        String   ISO8601 UTC
  refreshed_at      String   ISO8601 UTC
  expires_unix      Number   TTL
```

### regime-log

Owned by `ai_engine`. Audit/analytics only — not read in trading path.

```
PK: market_symbol   String   "NSE#RELIANCE"
SK: session_date    String   "2026-05-15"

Attributes:
  regime            String
  regime_confidence Number
  classified_at     String   ISO8601 UTC
  expires_at        Number   TTL: +30 days
```

### strategy-recommendations

Owned by `self_improvement_assistant` (PLANNED — Phase 8). Read by ops CLI and strategy config tooling. Not read in the live trading path.

```
PK: session_date    String   "2026-05-15"
SK: strategy_name   String   "nse_momentum_v1"

Attributes:
  recommendation    String   "KEEP" | "TUNE" | "PAUSE"
  rationale         String   Justification text
  suggested_params  Map      e.g., { "stop_loss_pct": 1.5 }
  session_metrics   Map      fills, win_rate, avg_pnl, max_drawdown
  generated_at      String   ISO8601 UTC
  expires_at        Number   TTL: +90 days
```

---

## 4. State Machines

### Order State Machine

```
                     ┌───────────────────────────────────────────────┐
                     │                                               │
                ┌────┴────┐                                         │
  signal arrives │ PENDING │ ← created by execution_engine           │
                └────┬────┘   (DynamoDB conditional write)          │
                     │                                               │
        broker accepts order                                         │
                     ▼                                               │
                ┌─────────┐                                         │
                │  PLACED  │ ← broker_order_id recorded              │
                └────┬────┘                                         │
                     │                                               │
         ┌───────────┼─────────────┐                                │
         ▼           ▼             ▼                                 │
    ┌────────┐  ┌─────────┐  ┌──────────────┐                      │
    │ FILLED │  │REJECTED │  │ PARTIALLY     │                      │
    └────────┘  └─────────┘  │ FILLED        │                      │
    (terminal)  (terminal)   └──────┬────────┘                      │
                                    │ remaining qty filled           │
                                    ▼                                │
                              ┌────────┐                             │
                              │ FILLED │                             │
                              └────────┘                             │
                              (terminal)                             │
                                                                     │
                    ┌────────────────────────────────┐              │
                    │ CANCELLED  (terminal)           │◄─────────────┘
                    └────────────────────────────────┘
                    (kill switch or broker cancel)

    FAILED (terminal): DynamoDB write error, broker unreachable after retries
```

### Kill Switch State Machine

```
         ┌─────────────────────────────────────────────────────────┐
         │                                                         │
         ▼                                                         │
   ┌──────────┐   daily P&L > limit   ┌──────────┐               │
   │   OFF    │ ─────────────────────► │    ON    │               │
   │(trading  │                        │(trading  │               │
   │ active)  │ ◄───────────────────── │ halted)  │               │
   └──────────┘   manual deactivation  └──────────┘               │
                  (make kill-switch-off)                           │
         │                                                         │
         │    WebSocket gap > 10s ──────────────────────────────► ON
         │    DLQ depth > 0 ────────────────────────────────────► ON
         │    any service produces to risk.kill-switch ─────────► ON
         │                                                         │
         └─────────────────────────────────────────────────────────┘
                (kill switch state is read with ConsistentRead=True)
```

### Circuit Breaker State Machine (per StrategyRunner)

```
   ┌────────┐  5 consecutive errors OR 10 errors/5min   ┌──────┐
   │ CLOSED │ ──────────────────────────────────────────►│ OPEN │
   │(normal)│                                            │(down)│
   └────────┘                                            └──┬───┘
       ▲                                                    │
       │  3 consecutive successes                           │ 300s elapsed
       │                                                    ▼
       │                                              ┌──────────┐
       └──────────────────────────────────────────────│HALF_OPEN │
                                                      │(testing) │
                                                      └──────────┘
                                                           │
                                              1 failure ───┘─────► OPEN (re-opens)

   Manual reset: operator sets circuit_breaker_reset=True in DynamoDB
                 → StrategyConfigLoader detects in 60s → CLOSED immediately
```

---

## 5. Idempotency and Deduplication

### Layer 1: Signal ID Determinism (Strategy Engine)

The same tick replayed → same `signal_id`. Downstream deduplication catches it.

```python
signal_id = sha256(f"{strategy_name}|{symbol}|{direction}|{price:.4f}|{ts.isoformat()}")[:32]
```

### Layer 2: Risk Engine Signal Inbox (DynamoDB)

Before processing any signal, the risk engine checks if it has already made a decision:

```python
# DynamoDB conditional write: only write if signal_id does not exist
response = dynamo.put_item(
    TableName="signal-inbox",
    Item={"signal_id": signal_id, "decided_at": utc_now_iso()},
    ConditionExpression="attribute_not_exists(signal_id)",
)
# ConditionalCheckFailedException → this signal was already decided → skip
```

### Layer 3: Execution Engine Order Creation (DynamoDB)

```python
dynamo.put_item(
    TableName="orders",
    Item={"signal_id": signal.signal_id, "order_id": order_id, "status": "PENDING", ...},
    ConditionExpression="attribute_not_exists(signal_id)",
)
# Raises ConditionalCheckFailedException if already created → silent no-op
```

### Layer 4: DynamoDB State Transitions (Conditional Writes)

All order status updates use `ConditionExpression` to prevent race conditions:

```python
dynamo.update_item(
    TableName="orders",
    Key={"order_id": order_id},
    UpdateExpression="SET #s = :placed, broker_order_id = :bid",
    ConditionExpression="#s = :pending",  # Only update if currently PENDING
    ExpressionAttributeNames={"#s": "status"},
    ExpressionAttributeValues={":placed": "PLACED", ":pending": "PENDING", ":bid": broker_id},
)
# Raises ConditionalCheckFailedException if status ≠ PENDING → another instance already updated
```

---

## 6. Shared Components

### `services/shared/kafka/config.py`

Central Kafka configuration used by all five services. Abstracts the PLAINTEXT (local Redpanda) vs SASL_SSL (MSK production) difference:

```python
def get_kafka_auth_config(region: str = "ap-south-1") -> dict:
    """
    Returns Kafka authentication config for the current environment.

    Local dev (KAFKA_USE_IAM=false): PLAINTEXT, no auth (Redpanda on port 19092).
    Production (KAFKA_USE_IAM=true): SASL_SSL with AWS MSK IAM OAUTHBEARER tokens.

    Args:
        region: AWS region for MSK token generation.

    Returns:
        dict of confluent_kafka producer/consumer config entries.
    """
    use_iam = os.getenv("KAFKA_USE_IAM", "false").lower() == "true"
    if not use_iam:
        return {"security.protocol": "PLAINTEXT"}
    
    token, expiry_ms = generate_auth_token(region)
    return {
        "security.protocol": "SASL_SSL",
        "sasl.mechanisms": "OAUTHBEARER",
        "sasl.oauthbearer.config": f"awsMskIam",
        "oauth_cb": lambda config: (token, expiry_ms),
    }
```

### `services/shared/models/signal.py`

Canonical `Signal` and `EnrichedSignal` Pydantic models. All services import from here — never from a service-local re-export.

### `services/shared/utils/helpers.py`

```python
def utc_now() -> datetime:
    """Always returns UTC-aware datetime. Never use datetime.now() in trading code."""
    return datetime.now(tz=timezone.utc)
```

### `services/shared/logging/logger.py`

Structured JSON logger. Adds `service`, `environment`, and `correlation_id` to every log entry automatically. All log entries are queryable in CloudWatch Logs Insights.

---

## 7. Configuration Management

### Environment Variables (loaded via AppSettings Pydantic model)

| Variable | Required By | Example |
|---|---|---|
| `KAFKA_BOOTSTRAP_SERVERS` | All 5 services | `b-1.qe.xxx.kafka.ap-south-1.amazonaws.com:9098` |
| `KAFKA_USE_IAM` | All 5 services | `true` (prod) / `false` (local dev) |
| `DYNAMODB_TABLE_PREFIX` | All 5 services | `quantembrace-prod` |
| `S3_BUCKET_DATA` | data_ingestion, ai_engine | `quantembrace-tick-data` |
| `S3_BUCKET_LOGS` | risk_engine, execution_engine | `quantembrace-trading-logs` |
| `S3_BUCKET_MODELS` | ai_engine | `quantembrace-model-artifacts` |
| `AWS_REGION` | All services | `ap-south-1` |
| `QE_ENVIRONMENT` | All services | `development` / `staging` / `production` |
| `ZERODHA_API_KEY` | execution_engine, data_ingestion | (from Secrets Manager) |
| `ALPACA_BASE_URL` | execution_engine | `https://paper-api.alpaca.markets` |
| `QE_EXECUTION_LIVE_TRADING_ENABLED` | execution_engine | `false` (absent = false) — hard gate; `true` required to place real broker orders |
| `UNIVERSE_MODE` | execution_engine | `PAPER_SAFE_START` / `PAPER_EXPAND` / `LIVE_ADVANCED` |
| `LOG_LEVEL` | All services | `DEBUG` / `INFO` / `WARNING` |

### Runtime Configuration (DynamoDB Hot-Reload)

Strategy parameters are stored in DynamoDB `strategy-config` table and reloaded every 60 seconds by `StrategyConfigLoader`. No service restart is needed to:

- Enable/disable a strategy
- Promote a strategy from paper → live (`paper_trade = False`)
- Adjust circuit breaker thresholds
- Manually reset a circuit breaker
- Change daily signal cap

### Risk Limits (File-Based Config)

Risk limits that rarely change are stored in `configs/risk_limits_production.yaml`:

```yaml
portfolio:
  portfolio_value: 1000000  # ₹10 lakh
  max_positions: 8
  max_daily_loss_pct: 2.0   # Kill switch fires at 2% = ₹20,000 loss
  kill_switch_auto_reset: false

validators:
  signal_age_seconds: 30
  max_slippage_pct: 0.15
  max_spread_bps: 50
  min_quality_score: 0.30
  max_sector_concentration_pct: 20.0
  max_adv_pct: 1.0
  margin_buffer_pct: 20.0
```

---

## 8. Error Handling and Retry Policy

### Standard Error Classification

| Error Code | Severity | Retryable | Default Action |
|---|---|---|---|
| `WS_DISCONNECTED` | CRITICAL | Yes | Exponential backoff, max 5 retries → kill switch |
| `WS_RECONNECT_EXHAUSTED` | CRITICAL | No | Kill switch + ops alert |
| `TICK_PARSE_ERROR` | WARNING | No | Log and skip |
| `DYNAMO_WRITE_FAILED` | ERROR | Yes | Exponential backoff, max 3 retries → alert |
| `KAFKA_PUBLISH_FAILED` | ERROR | Yes | Exponential backoff, max 5 retries → DLQ |
| `SIGNAL_EXPIRED` | INFO | No | Log and discard |
| `RISK_REJECTED` | INFO | No | Log to ops.audit, discard |
| `KILL_SWITCH_ACTIVE` | CRITICAL | No | Halt processing |
| `BROKER_TIMEOUT` | ERROR | Yes | Exponential backoff, max 3 retries |
| `BROKER_RATE_LIMITED` | WARNING | Yes | Fixed 2s delay, max 5 retries |
| `BROKER_REJECTED` | ERROR | No | Mark order REJECTED, alert |
| `ORDER_IDEMPOTENCY_HIT` | INFO | No | Silent skip |
| `POSITION_MISMATCH` | CRITICAL | No | Kill switch + ops alert |
| `MODEL_LOAD_FAILED` | WARNING | Yes | Use stub model, retry in 60s |
| `ENRICHMENT_FAILED` | WARNING | No | Use degraded defaults, continue |

### Retry Configuration

```python
RETRY_POLICIES = {
    "WS_DISCONNECTED": RetryPolicy(
        max_attempts=5,
        backoff="exponential",
        base_delay_seconds=1,
        max_delay_seconds=30,
        on_exhausted="activate_kill_switch",
    ),
    "DYNAMO_WRITE_FAILED": RetryPolicy(
        max_attempts=3,
        backoff="exponential",
        base_delay_seconds=0.5,
        max_delay_seconds=5,
        on_exhausted="log_and_alert",
    ),
    "BROKER_TIMEOUT": RetryPolicy(
        max_attempts=3,
        backoff="exponential",
        base_delay_seconds=1,
        max_delay_seconds=10,
        on_exhausted="mark_order_failed_and_alert",
    ),
    "KAFKA_PUBLISH_FAILED": RetryPolicy(
        max_attempts=5,
        backoff="exponential",
        base_delay_seconds=0.2,
        max_delay_seconds=10,
        on_exhausted="publish_to_dlq",
    ),
}
```

### Dead Letter Queue (DLQ) Pattern

Every primary Kafka topic has a corresponding `.dlq` topic:

```
signals.pending      → signals.pending.dlq      (max 5 retries)
signals.enriched     → signals.enriched.dlq     (max 5 retries)
signals.approved     → signals.approved.dlq     (max 3 retries)
orders.events        → orders.events.dlq        (max 3 retries)
```

A message reaches the DLQ after `max_retries` unsuccessful processing attempts. DLQ depth > 0 triggers a CloudWatch alarm and ops alert. DLQ messages are retained for 90 days for investigation.

---

## 9. Internal API Contracts (Python Types)

### MarketTick

```python
@dataclass(frozen=True)
class MarketTick:
    market: Market
    instrument: str
    ltp: Decimal
    bid: Decimal
    ask: Decimal
    bid_qty: int
    ask_qty: int
    volume: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    change_pct: Decimal
    timestamp: datetime         # UTC
    exchange_timestamp: datetime
    trace_id: str               # uuid4, set at creation
    sequence_id: int            # monotonically incrementing per service instance

    @property
    def spread(self) -> Decimal: return self.ask - self.bid
    @property
    def mid_price(self) -> Decimal: return (self.bid + self.ask) / 2
    @property
    def market_instrument(self) -> str: return f"{self.market.value}#{self.instrument}"
```

### Signal

```python
@dataclass
class Signal:
    signal_id: str              # sha256 deterministic hash
    strategy_name: str
    strategy_id: str
    market: Market
    instrument: str
    direction: Direction
    quantity: int
    order_type: OrderType
    limit_price: Optional[Decimal]
    stop_price: Optional[Decimal]
    stop_loss: Optional[Decimal]
    take_profit: Optional[Decimal]
    confidence: float           # 0.0–1.0
    price_at_signal: Decimal
    generated_at: datetime
    expires_at: datetime        # generated_at + 30s
    paper_trade: bool           # stamped from DynamoDB strategy-config
    product_type: str           # "MIS" | "CNC" | "NRML"
    metadata: dict
    trace_id: str               # propagated from originating tick
```

### EnrichedSignal (extends Signal)

```python
@dataclass(frozen=True)
class EnrichedSignal(Signal):
    regime: RegimeLabel         # "trending"|"ranging"|"volatile"|"crash"|"unknown"
    regime_confidence: float    # HMM posterior probability
    quality_score: float        # GBT score, 0.0–1.0
    filtered: bool              # True if quality_score < threshold
    enriched_at: datetime
    enrichment_latency_ms: float
    model_versions: dict[str, str]
    schema_version: str = "4.0"
```

### ApprovedSignal (output of risk engine)

```python
@dataclass
class ApprovedSignal:
    # All Signal fields (pass through unchanged)
    signal_id: str
    risk_decision_id: str       # uuid4, audit trail key
    validators_passed: list[str]
    risk_state_snapshot: dict
    approved_at: datetime
    # Inherited from Signal:
    market, instrument, direction, quantity, order_type,
    stop_loss, paper_trade, product_type, expires_at, trace_id
```

### OrderRequest (input to broker adapters)

```python
@dataclass
class OrderRequest:
    order_id: str               # uuid4, generated by execution_engine
    signal_id: str
    risk_decision_id: str
    market: Market
    instrument: str
    direction: Direction
    quantity: int
    order_type: OrderType
    limit_price: Optional[Decimal]
    stop_price: Optional[Decimal]
    stop_loss_price: Decimal    # REQUIRED — risk engine enforces this
    product_type: str
    time_in_force: str          # "DAY" | "IOC" | "GTC"
    paper_trade: bool
```

### BrokerClient Protocol

```python
class BrokerClient(Protocol):
    """Structural interface for all broker adapters."""
    async def place_order(self, order: OrderRequest) -> OrderResponse: ...
    async def cancel_order(self, order_id: str) -> CancelResponse: ...
    async def get_positions(self) -> list[Position]: ...
    async def get_order_status(self, order_id: str) -> OrderStatus: ...
    async def subscribe_quotes(self, symbols: list[str], callback: QuoteCallback) -> None: ...
```

---

## 10. Health Check Specification

All services expose `GET /health` on their designated port.

### Response Schema

```python
@dataclass
class HealthResponse:
    status: Literal["ok", "degraded", "unhealthy"]
    service: str
    version: str
    uptime_seconds: float
    checks: dict[str, bool | str]
    timestamp: str              # ISO8601 UTC
```

### Service Health Endpoints

| Service | Port | Key Checks |
|---|---|---|
| data_ingestion | 8081 | `zerodha_ws_connected`, `alpaca_ws_connected`, `kafka_producer_ready`, `last_tick_age_seconds` |
| strategy_engine | 8082 | `kafka_consumer_ready`, `strategies_active`, `kafka_consumer_lag`, `circuit_breakers` |
| risk_engine | 8083 | `kill_switch_state`, `dynamodb_readable`, `kafka_consumers_ready`, `enrichment_fallback_active`, `daily_pnl` |
| execution_engine | 8084 | `paper_mode`, `zerodha_connected`, `alpaca_connected`, `open_orders_count`, `kafka_consumer_ready` |
| ai_engine | 8085 | `models_loaded`, `feature_reader_ready`, `kafka_consumer_ready`, `last_enrichment_latency_ms` |

### Example (risk_engine)

```json
{
  "status": "ok",
  "service": "risk_engine",
  "version": "1.6.0",
  "uptime_seconds": 14523.5,
  "checks": {
    "kill_switch_state": "OFF",
    "dynamodb_readable": true,
    "dynamodb_writable": true,
    "kafka_enriched_consumer": true,
    "kafka_fallback_consumer": true,
    "kafka_order_events_consumer": true,
    "enrichment_fallback_active": false,
    "daily_pnl_inr": 8200.00,
    "daily_loss_pct_used": 0.0
  },
  "timestamp": "2026-05-15T06:30:00Z"
}
```

### Degraded vs Unhealthy

- `ok` — all checks pass, system operating normally
- `degraded` — some non-critical checks failing (e.g., enrichment fallback active, last tick > 30s ago) — service continues operating
- `unhealthy` — critical subsystem unavailable (e.g., DynamoDB unreachable, Kafka disconnected) — ASG health check fails, instance is replaced

---

*Last updated: 2026-05-28 | This document should be updated when: new service components are added, Kafka message schemas change (schema version bumped), DynamoDB table schemas change, state machines are modified, or new shared components are introduced. For system-level architecture and design decisions, see [hld.md](hld.md).*
