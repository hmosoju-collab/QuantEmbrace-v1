# QuantEmbrace — Complete Signal Lifecycle

> **Prerequisite:** Read [02_architecture.md](02_architecture.md) to understand the layer structure before reading this.

This document traces the exact path of a trading signal from a raw price tick to a filled broker order — including all the edge cases, error paths, the AI enrichment step, and the kill switch mechanism.

---

## Table of Contents

1. [Market Hours and Timezone Handling](#market-hours-and-timezone-handling)
2. [System Startup Sequence](#system-startup-sequence)
3. [Phase 1 — Market Data Arrives](#phase-1--market-data-arrives)
4. [Phase 2 — Strategy Generates a Signal](#phase-2--strategy-generates-a-signal)
5. [Phase 3 — AI Engine Enriches the Signal](#phase-3--ai-engine-enriches-the-signal)
6. [Phase 4 — Risk Validates the Signal](#phase-4--risk-validates-the-signal)
7. [Phase 5 — Order Placed with Broker](#phase-5--order-placed-with-broker)
8. [Phase 6 — Order Fill and Tracking](#phase-6--order-fill-and-tracking)
9. [The Kill Switch Scenario](#the-kill-switch-scenario)
10. [Error Paths and Retry Scenarios](#error-paths-and-retry-scenarios)
11. [End of Day — Market Close Sequence](#end-of-day--market-close-sequence)
12. [Order State Machine](#order-state-machine)

---

## Market Hours and Timezone Handling

QuantEmbrace deals with two markets in different timezones. The system stores **all timestamps in UTC internally**. Conversions happen only at display and logging layers.

```
Market         Local Hours        UTC Equivalent       IST Equivalent
─────────────────────────────────────────────────────────────────────
NSE India      09:15–15:30 IST    03:45–10:00 UTC      09:15–15:30 IST
US Equities    09:30–16:00 ET     13:30–20:00 UTC      19:00–01:30 IST*

* US hours shift 30 min between EDT (summer) and EST (winter):
  EDT (Mar–Nov): 09:30 ET = 14:00 UTC = 19:30 IST
  EST (Nov–Mar): 09:30 ET = 14:30 UTC = 20:00 IST
```

**Why UTC everywhere?** Time arithmetic with mixed timezones is a rich source of bugs. Storing everything in UTC and converting only at boundaries is the industry standard and what every cloud service (DynamoDB, CloudWatch, Kafka) uses natively.

---

## System Startup Sequence

Every service follows this protocol before accepting any work. This is what makes the system **restart-safe** — a service can crash at any point and resume exactly where it left off.

```
SERVICE STARTUP
     │
     ▼
Step 1: Load and validate configuration
        ├── Parse environment variables via Pydantic AppSettings
        └── Fail immediately with clear error if any required variable is missing

     ▼
Step 2: Reconcile state with broker (execution_engine only)
        ├── Fetch open orders from Zerodha / Alpaca
        ├── Compare with DynamoDB orders table
        └── Update any orders that changed state while the service was down

     ▼
Step 3: Load persisted state from DynamoDB
        ├── risk_engine: load kill switch state, today's P&L counters, open positions
        ├── strategy_engine: restore indicator state (moving averages, candle accumulators)
        └── execution_engine: restore in-flight order tracking

     ▼
Step 4: Connect to Kafka
        └── Subscribe to topics, seek to last committed offset (at-least-once delivery)

     ▼
Step 5: Start background tasks
        ├── Health server (HTTP on assigned port)
        ├── KillSwitchMonitor (risk_engine)
        ├── EnrichmentWatchdog (risk_engine)
        └── MIS square-off manager (execution_engine)

     ▼
Step 6: Service READY — begin processing Kafka messages
```

**Start-up order matters.** On Monday morning, the correct sequence is:

```
1. data_ingestion    (ticks must flow before strategies run)
2. ai_engine         (must be consuming signals.pending before risk_engine starts)
3. risk_engine       (starts enriched + fallback loops)
4. execution_engine  (must be consuming before strategies can flood the pipeline)
5. strategy_engine   (started last — signals only flow when downstream is ready)
```

`make monday` handles this automatically.

---

## Phase 1 — Market Data Arrives

### Tick arrives from Zerodha

```
[09:16:03.421 IST] Zerodha Kite WebSocket sends:

Raw packet (Zerodha binary/JSON protocol):
{
    "instrument_token": 738561,         ← Zerodha's internal ID for NSE:RELIANCE
    "last_price": 2453.50,
    "volume": 1847320,
    "ohlc": {"open": 2440.00, "high": 2461.00, "low": 2437.50, "close": 2451.00},
    "timestamp": "2026-04-24 09:16:03"  ← IST string — will be converted to UTC
}
```

### Normalisation and fan-out

`data_ingestion/processors/tick_processor.py` converts this to a `MarketTick` and fans out simultaneously to three destinations:

```python
MarketTick(
    market="NSE",
    instrument="NSE:RELIANCE",
    ltp=Decimal("2453.50"),
    bid=Decimal("2453.45"),
    ask=Decimal("2453.55"),
    volume=1847320,
    timestamp=datetime(2026, 4, 24, 3, 46, 3, tzinfo=UTC),  # IST → UTC
)
```

1. **Kafka publish** → `ticks.nse` (key = `"RELIANCE"`)
2. **DynamoDB write** → `latest-prices` table: `NSE#RELIANCE → {ltp: 2453.50, ts: ...}`
3. **S3 buffer** → batched Parquet write to `s3://quantembrace-{env}-data/NSE/RELIANCE/2026-04-24/09/`

All three happen asynchronously and concurrently — the WebSocket callback returns immediately.

---

## Phase 2 — Strategy Generates a Signal

### Strategy engine consumes the tick

```python
# strategy_engine/service.py
# Consumer group: strategy-v1, topic: ticks.nse

tick = MarketTick(market="NSE", instrument="NSE:RELIANCE", ltp=2453.50, ...)

# All 6 strategies are called for every tick on instruments they watch
for strategy in self._registry.strategies_for(tick.instrument):
    signal = await strategy.on_tick(tick)
    if signal:
        await self._signal_publisher.publish(signal)  # → signals.pending
```

### Inside MomentumStrategy.on_tick()

```python
def on_tick(self, tick: MarketTick) -> Optional[Signal]:
    self.prices.append(tick.ltp)

    if len(self.prices) < self.long_window:
        return None  # Not enough data yet — needs 50 ticks to warm up

    short_ma = mean(self.prices[-self.short_window:])   # 10-tick average
    long_ma  = mean(self.prices[-self.long_window:])    # 50-tick average

    # Golden cross: short MA crosses above long MA (bullish signal)
    if short_ma > long_ma and self.prev_short_ma <= self.prev_long_ma:
        return Signal(
            signal_id=str(uuid4()),               # UUID — unique forever
            strategy_name="nse_momentum_v1",
            market="NSE",
            instrument="NSE:RELIANCE",
            direction=Direction.BUY,
            quantity=self._compute_quantity(tick.ltp),  # position sizing
            stop_price=tick.ltp * Decimal("0.98"),       # 2% stop-loss
            confidence=self._compute_confidence(short_ma, long_ma),
            paper_trade=True,                     # paper mode by default
            created_at=utc_now(),
        )
    return None
```

### Signal published to Kafka

```python
# strategy_engine/publishers/kafka_signal_publisher.py
producer.produce(
    topic="signals.pending",
    key="NSE:RELIANCE",      # key = instrument symbol for ordered processing
    value=signal.model_dump_json(),
)
producer.flush()
```

---

## Phase 3 — AI Engine Enriches the Signal

### AI engine consumes from signals.pending

```python
# ai_engine/service.py
# Consumer group: aiengine-v1, topic: signals.pending

signal = Signal.model_validate_json(kafka_message.value())

# Compute features from recent candle data
features = self._feature_pipeline.compute(signal.instrument, signal.market)

# Run ML models
regime          = self._predictor.classify_regime(features)       # "trending" | "ranging" | "volatile"
quality_score   = self._predictor.score_signal_quality(signal, features)  # 0.0–1.0
volatility_est  = self._predictor.estimate_volatility(features)   # annualised fraction
```

### Enriched signal republished

```python
enriched = signal.model_copy(update={
    "quality_score":      quality_score,      # e.g. 0.82
    "market_regime":      regime,             # e.g. "trending"
    "volatility_estimate": volatility_est,    # e.g. 0.018
    "enrichment_version": "v1.2",
    "enriched_at":        utc_now(),
})

# Publish to signals.enriched — key = instrument (same as signals.pending)
producer.produce(topic="signals.enriched", key=signal.instrument, value=enriched.model_dump_json())
```

### Fallback path (if ai_engine is lagging)

The `EnrichmentWatchdog` in the risk engine monitors the Kafka consumer group lag on `aiengine-v1` every 10 seconds. If lag exceeds the threshold, it switches the risk engine to consume directly from `signals.pending`:

```
Normal:   signals.pending → [ai_engine] → signals.enriched → [risk_engine]
Fallback: signals.pending ─────────────────────────────────► [risk_engine]
```

In fallback mode, `quality_score` is absent from the signal. The `QualityScoreValidator` in the risk engine treats absent quality_score as passing (graceful degradation — we trade without ML filtering rather than stop trading).

---

## Phase 4 — Risk Validates the Signal

### Risk engine consumes from signals.enriched (or signals.pending in fallback)

```python
# risk_engine/service.py
# Consumer group: risk-v1, topic: signals.enriched

signal = EnrichedSignal.model_validate_json(kafka_message.value())
decision = await self._validate_signal(signal)

if decision.status == RiskStatus.APPROVED:
    # Attach the risk_decision_id — proof this signal passed risk
    approved_payload = signal.model_copy(update={"risk_decision_id": decision.risk_decision_id})
    await self._approved_publisher.publish(approved_payload)  # → signals.approved
    await self._audit_writer.write(decision)                  # → S3 audit log
else:
    await self._audit_writer.write(decision)                  # Rejected — still log it
    # Message is committed (consumed) — rejected signals are not retried
```

### Validation runs in sequence — short-circuits on first failure

```python
async def _validate_signal(self, signal: Signal) -> RiskDecision:

    # Validators are ordered cheapest-first: cheap checks fail fast before expensive DynamoDB reads

    for validator in self._validators:  # 11 validators in priority order
        result = await validator.validate(signal)
        if not result.approved:
            # Check 5 (DailyLossValidator): auto-trigger kill switch if loss limit breached
            if isinstance(validator, DailyLossValidator):
                await self._kill_switch.activate(reason=result.reason, activated_by="auto:daily_loss")
            return RiskDecision(status=REJECTED, reason=result.reason, ...)

    return RiskDecision(status=APPROVED, ...)
```

### Audit log written to S3

```json
{
  "risk_decision_id": "abc-789-xyz",
  "signal_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "status": "APPROVED",
  "enriched": true,
  "validator_results": [
    {"validator": "SignalAgeValidator",            "approved": true, "reason": "age 0.3s < 30s"},
    {"validator": "KillSwitchValidator",           "approved": true, "reason": "kill switch is OFF"},
    {"validator": "PositionValidator",             "approved": true, "reason": "4 open < 8 max"},
    {"validator": "ExposureValidator",             "approved": true, "reason": "48% < 50% limit"},
    {"validator": "DailyLossValidator",            "approved": true, "reason": "P&L +₹8,200"},
    {"validator": "MarginValidator",               "approved": true, "reason": "sufficient margin"},
    {"validator": "SlippageValidator",             "approved": true, "reason": "slippage 0.02%"},
    {"validator": "SpreadGateValidator",           "approved": true, "reason": "spread 8bps < 50bps"},
    {"validator": "SectorConcentrationValidator",  "approved": true, "reason": "Energy at 15%"},
    {"validator": "LiquidityValidator",            "approved": true, "reason": "0.3% of ADV"},
    {"validator": "QualityScoreValidator",         "approved": true, "reason": "0.82 >= 0.30 threshold"}
  ],
  "timestamp": "2026-04-24T03:46:04.123Z"
}
```

---

## Phase 5 — Order Placed with Broker

### Execution engine receives approved signal

```python
# execution_engine/service.py
# Consumer group: execution-v1, topic: signals.approved

signal = ApprovedSignal.model_validate_json(kafka_message.value())

# HARD CHECK: reject any signal that bypassed the risk engine
if not signal.risk_decision_id:
    raise ValueError(f"Signal {signal.signal_id} has no risk_decision_id — REJECTED")
```

### Idempotency check

```python
existing = await self._order_manager.get_by_signal_id(signal.signal_id)

if existing:
    match existing.status:
        case OrderStatus.FILLED:     return  # Already done — skip
        case OrderStatus.PLACED:     return  # Already placed — wait for fill
        case OrderStatus.CANCELLED:  return  # Already cancelled — skip
        case OrderStatus.FAILED:     pass    # Fall through to retry
# else: first time seeing this signal — continue
```

### Order record written to DynamoDB (BEFORE calling broker)

```python
# Write PENDING status BEFORE calling the broker.
# If we crash between writing and calling, startup reconciliation will resolve it.
await self._order_manager.create(
    order_id=str(uuid4()),
    signal_id=signal.signal_id,
    risk_decision_id=signal.risk_decision_id,
    instrument=signal.instrument,
    side=signal.direction,
    quantity=signal.quantity,
    status=OrderStatus.PENDING,
)
```

### Paper vs live routing

```python
if signal.paper_trade:
    response = await self._paper_simulator.fill(signal)  # Deterministic simulation
else:
    broker = self._zerodha if signal.market == Market.NSE else self._alpaca
    response = await self._retry_handler.execute(broker.place_order, order_request)
```

### Live path — Zerodha example

```python
# execution_engine/brokers/zerodha_broker.py

kite.place_order(
    variety=kite.VARIETY_REGULAR,
    exchange="NSE",
    tradingsymbol="RELIANCE",
    transaction_type=kite.TRANSACTION_TYPE_BUY,
    quantity=20,
    product=kite.PRODUCT_MIS,       # MIS = intraday; CNC = delivery
    order_type=kite.ORDER_TYPE_MARKET,
)
# → {"order_id": "240424000012345"}
```

```python
# DynamoDB: PENDING → PLACED
await self._order_manager.update(
    signal_id=signal.signal_id,
    broker_order_id="240424000012345",
    status=OrderStatus.PLACED,
)
```

---

## Phase 6 — Order Fill and Tracking

### How fills are detected

The execution engine monitors fill status by:
1. Polling Zerodha order status every 2 seconds for active NSE orders
2. Consuming Alpaca WebSocket trade update events in real time for US orders

```python
broker_status = await self._zerodha.get_order_status("240424000012345")

if broker_status.status == "COMPLETE":
    await self._order_manager.update(
        signal_id=signal.signal_id,
        status=OrderStatus.FILLED,
        filled_quantity=20,
        average_price=Decimal("2454.00"),
        filled_at=utc_now(),
    )
    # Publish fill event so risk engine can update position tracking
    await self._fill_publisher.publish(fill_event)  # → orders.events
```

### Final order record in DynamoDB

```json
{
    "order_id": "qe-ord-a1b2c3",
    "signal_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "risk_decision_id": "abc-789-xyz",
    "broker_order_id": "240424000012345",
    "instrument": "NSE:RELIANCE",
    "market": "NSE",
    "side": "BUY",
    "quantity": 20,
    "status": "FILLED",
    "average_price": "2454.00",
    "filled_quantity": 20,
    "paper_trade": false,
    "created_at": "2026-04-24T03:46:04.000Z",
    "placed_at":  "2026-04-24T03:46:04.500Z",
    "filled_at":  "2026-04-24T03:46:05.200Z",
    "ttl": 1753180800
}
```

**Total time from tick to filled order: typically 0.7–2 seconds.** The processing pipeline takes under 100ms. The broker matching engine accounts for the remaining time.

---

## The Kill Switch Scenario

What happens when the kill switch fires automatically during a trading day:

```
[12:30:00 UTC] Daily P&L reaches -3.2% of portfolio (limit is 3.0%)
                        │
                        ▼
DailyLossValidator returns approved=False
                        │
                        ▼
risk_engine._kill_switch.activate(reason="auto: daily_loss 3.2% > 3.0%")
                        │
          ┌─────────────┼────────────────────┐
          ▼             ▼                    ▼
   DynamoDB:       CloudWatch           Kafka: risk.kill-switch
   kill_switch=ON  metric fires         (execution_engine listener picks this up)
   reason saved    SNS → Email/SMS
          │
          ▼
All subsequent risk validation calls:
  → KillSwitchValidator.validate() → is_active=True → REJECT immediately
  → No DynamoDB reads, no other validator runs — fastest possible rejection

signals.enriched consumer: continues, but every signal is rejected
orders.events consumer: continues (fills still tracked)
data_ingestion: continues (ticks still flowing)
strategy_engine: continues generating signals (they'll be rejected at risk)
Open positions: HELD (not force-closed — positions are safe at broker)

[Next trading day]
Operator runs: make kill-switch-off
                        │
                        ▼
DynamoDB: kill_switch=OFF
risk_engine: normal processing resumes for next session
```

---

## Error Paths and Retry Scenarios

### Scenario: Broker API timeout during order placement

```
execution_engine → Zerodha API: place_order()
        timeout after 5 seconds
                │
                ▼
RetryHandler: attempt 1 failed (timeout) — wait 1 second
              attempt 2: place_order() with same signal_id
              ← Zerodha responds with 429 (rate limit exceeded)
              wait 2 seconds
              attempt 3: place_order() with same signal_id
              ← Zerodha returns order_id "240424000012345"

DynamoDB: PENDING → PLACED ✓  (same order, same signal_id)
```

### Scenario: Service crashes after placing order but before DynamoDB write

```
[execution_engine crash between broker confirm and DynamoDB write]
                │
                ▼
EC2 ASG detects unhealthy instance → replaces it
                │
                ▼
New instance starts → startup reconciliation:
  1. Fetch open orders from DynamoDB with status=PENDING
  2. Query Zerodha: "status of order 240424000012345?"
  3. Zerodha says: COMPLETE, filled at 2454.00
  4. Update DynamoDB: PENDING → FILLED ✓
  5. No duplicate order placed (broker already has it)
```

### Scenario: Kafka delivers the same signal twice (at-least-once semantics)

```
risk_engine approves signal → Kafka → execution_engine
Kafka thought execution_engine didn't acknowledge → re-delivers same message

execution_engine receives signal a second time:
  check: get_by_signal_id(signal_id)
  → finds existing order with status=FILLED
  → return early: "already processed"
  → commit the Kafka message

No duplicate order placed ✓ (idempotency works)
```

### Scenario: Signal enrichment fails (ai_engine processing error)

```
ai_engine receives signal from signals.pending
model inference throws exception
                │
                ▼
KafkaFailurePublisher publishes to signals.enriched.retry
ai_engine commits the original message (moves past it)
                │
                ▼
KafkaRetryReplayer picks up from signals.enriched.retry
Re-publishes to signals.enriched after delay
                │
                ▼
After 3 failed retries OR signal has expired:
  → Published to signals.enriched.dlq for manual inspection
  → Never reaches risk_engine
```

---

## End of Day — Market Close Sequence

### NSE Market Close (15:30 IST = 10:00 UTC)

```
[14:40 UTC = 20:10 IST] 30 minutes before NSE pre-close
  → MIS square-off manager starts checking for open intraday positions

[10:00 UTC = 15:30 IST] NSE trading halts
  1. data_ingestion: stops publishing NSE ticks (WebSocket shows close auction)
  2. strategy_engine: stops generating NSE signals (no new ticks)
  3. risk_engine: MIS open positions trigger square-off signals
  4. execution_engine: places MARKET SELL orders for all open MIS positions
     (proactively at 15:10 IST — 5 min before Zerodha's auto square-off at 15:15)
  5. Fills confirmed and DynamoDB updated
  6. End-of-day P&L calculation written to DynamoDB risk-state table
  7. Paper session report generated: scripts/monitoring/paper_session_report.py
```

### US Market Close (16:00 ET = 20:00 UTC)

```
[20:00 UTC = 01:30 IST+1] US market close
  1. data_ingestion: stops publishing US ticks
  2. strategy_engine: stops generating US signals
  3. Swing positions (if any) are held overnight — no auto square-off for US
  4. Intraday positions (if us_intraday_only=true in risk_limits): squared off before close
```

---

## Order State Machine

An order moves through these states exactly once. There are no backward transitions.

```
                     ┌─────────┐
                     │ PENDING │  ← Written to DynamoDB BEFORE calling broker API
                     └────┬────┘
                          │ broker API call succeeds
                          ▼
                     ┌─────────┐
                ┌────│ PLACED  │────┐
                │    └─────────┘    │
                │                   │
       broker   │                   │ broker confirms fill
       rejects  │                   │
                ▼                   ▼
           ┌──────────┐       ┌──────────┐
           │ REJECTED │       │  FILLED  │  ← Terminal: order completed
           └──────────┘       └──────────┘

     ┌─────────┐
     │ PENDING │
     └────┬────┘
          │ all 3 retries exhausted
          ▼
     ┌────────┐
     │ FAILED │  ← Terminal: alert sent, manual review required
     └────────┘

     ┌─────────┐
     │ PLACED  │
     └────┬────┘
          │ operator or risk engine cancels
          ▼
     ┌───────────┐
     │ CANCELLED │  ← Terminal: order cancelled at broker
     └───────────┘

     ┌─────────┐
     │ PLACED  │
     └────┬────┘
          │ partial fill (e.g. 15 of 20 shares)
          ▼
     ┌─────────────────┐
     │ PARTIALLY_FILLED │
     └────┬────────────┘
          │ remaining shares filled
          ▼
     ┌──────────┐
     │  FILLED  │
     └──────────┘
```

All state transitions use **DynamoDB conditional writes** to prevent race conditions:

```python
# This update succeeds ONLY if the current status is PENDING.
# If two concurrent processes both try to transition from PENDING → PLACED,
# exactly one succeeds and the other gets ConditionalCheckFailedException.
dynamodb.update_item(
    ConditionExpression="#s = :pending",
    UpdateExpression="SET #s = :placed, broker_order_id = :bid, placed_at = :ts",
    ExpressionAttributeValues={
        ":pending": "PENDING",
        ":placed":  "PLACED",
        ":bid":     broker_order_id,
        ":ts":      utc_iso(),
    },
)
```

---

*Last updated: 2026-05-15 | Update this document when: AI enrichment pipeline changes, new order types are added, the state machine changes, a new market is integrated, or kill switch propagation behaviour changes.*
