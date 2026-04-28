# QuantEmbrace — Complete Signal Lifecycle

> **Prerequisite:** Read [02_architecture.md](02_architecture.md) to understand the layer structure before reading this.

This document traces the exact path of a trading signal from the moment a price tick arrives to a filled order in the broker's system — including all the edge cases, error paths, and the kill switch mechanism.

---

## Table of Contents

1. [Market Hours and Timezone Handling](#market-hours-and-timezone-handling)
2. [System Startup Sequence](#system-startup-sequence)
3. [Phase 1 — Market Data Arrives](#phase-1--market-data-arrives)
4. [Phase 2 — Strategy Generates a Signal](#phase-2--strategy-generates-a-signal)
5. [Phase 3 — Risk Validates the Signal](#phase-3--risk-validates-the-signal)
6. [Phase 4 — Order Placed with Broker](#phase-4--order-placed-with-broker)
7. [Phase 5 — Order Fill and Tracking](#phase-5--order-fill-and-tracking)
8. [The Kill Switch Scenario](#the-kill-switch-scenario)
9. [Error Paths and Retry Scenarios](#error-paths-and-retry-scenarios)
10. [End of Day — Market Close Sequence](#end-of-day--market-close-sequence)
11. [Order State Machine](#order-state-machine)

---

## Market Hours and Timezone Handling

QuantEmbrace deals with two markets in different timezones. The system uses **UTC internally for all timestamps**. Conversions to local time happen only at display/logging layers.

```
Market         Local Hours        UTC Equivalent      IST Equivalent
─────────────────────────────────────────────────────────────────────
NSE India      09:15–15:30 IST    03:45–10:00 UTC     09:15–15:30 IST
US Equities    09:30–16:00 ET     13:30–20:00 UTC     19:00–01:30 IST*

* US hours shift 30 min between EDT (summer) and EST (winter) due to DST.
  EDT (Mar–Nov): IST is UTC+5:30, ET is UTC-4  →  09:30 ET = 19:00 IST
  EST (Nov–Mar): IST is UTC+5:30, ET is UTC-5  →  09:30 ET = 20:00 IST
```

**Why UTC everywhere?** Time arithmetic with mixed timezones is error-prone. Storing everything in UTC and converting only at boundaries is the industry standard.

---

## System Startup Sequence

Every service follows this startup protocol before accepting any work:

```
SERVICE STARTUP
     │
     ▼
Step 1: Load configuration
        ├── Read env vars (API keys, AWS region, table names)
        └── Load runtime config from DynamoDB (risk limits, strategy params)

     ▼
Step 2: Reconcile state with broker
        ├── Get list of open orders from broker API
        ├── Compare with DynamoDB orders table
        └── Update any orders that changed state while service was down

     ▼
Step 3: Load persisted state
        ├── Risk engine: load kill switch state, today's P&L counters
        └── Strategy engine: restore strategy indicator states from DynamoDB

     ▼
Step 4: Connect to message queues
        └── Begin polling SQS for incoming messages

     ▼
Step 5: Service is READY
        └── Begin processing
```

This startup sequence is what makes the system **restart-safe**. A service can crash at any point and resume exactly where it left off.

---

## Phase 1 — Market Data Arrives

### Tick arrives from broker

```
[09:16:03 IST]
Zerodha Kite Ticker sends a WebSocket message:

Raw payload:
{
    "instrument_token": 738561,      ← Zerodha's internal ID for RELIANCE
    "mode": "full",
    "last_price": 2453.50,
    "volume": 1847320,
    "buy_quantity": 45000,
    "sell_quantity": 32000,
    "ohlc": {"open": 2440.00, "high": 2461.00, "low": 2437.50, "close": 2451.00},
    "timestamp": "2026-04-24 09:16:03"   ← IST, string format
}
```

### Normalization and storage

`data_ingestion/processors/tick_processor.py` converts this to:

```python
MarketTick(
    market="NSE",
    instrument="NSE:RELIANCE",
    ltp=Decimal("2453.50"),
    bid=Decimal("2453.45"),    # Computed from depth
    ask=Decimal("2453.55"),
    volume=1847320,
    timestamp=datetime(2026, 4, 24, 3, 46, 3, tzinfo=UTC),  # Converted to UTC
    raw={...}                  # Original payload stored for debugging
)
```

Three things happen simultaneously (all async, non-blocking):
1. **DynamoDB write** — Update `latest-prices` table: `NSE#RELIANCE → {ltp: 2453.50, ts: ...}`
2. **S3 write** — Append tick to `s3://quantembrace-market-data-history/NSE/RELIANCE/2026-04-24/09/ticks.parquet`
3. **SQS publish** — Send normalized tick to `quantembrace-market-data` SQS queue for Strategy Engine

---

## Phase 2 — Strategy Generates a Signal

### Strategy Engine receives the tick

```python
# strategy_engine/service.py polls SQS for ticks
tick = MarketTick(market="NSE", instrument="NSE:RELIANCE", ltp=2453.50, ...)

# All registered strategies that watch RELIANCE are called
for strategy in strategies_watching("NSE:RELIANCE"):
    signal = await strategy.on_tick(tick)
    if signal:
        await publish_to_risk_engine(signal)
```

### Inside MomentumStrategy.on_tick()

```python
# services/strategy_engine/strategies/momentum_strategy.py

def on_tick(self, tick: MarketTick) -> Optional[Signal]:
    # Update price history buffer
    self.prices.append(tick.ltp)
    
    # Need at least 50 data points for long MA
    if len(self.prices) < self.long_window:
        return None  # Not enough data yet
    
    short_ma = mean(self.prices[-self.short_window:])  # 10-period average
    long_ma  = mean(self.prices[-self.long_window:])   # 50-period average
    
    # Golden cross: short MA crosses above long MA
    if short_ma > long_ma and self.previous_short_ma <= self.previous_long_ma:
        return Signal(
            signal_id=str(uuid4()),
            strategy_name="nse_momentum_v1",
            market="NSE",
            instrument="NSE:RELIANCE",
            direction=Direction.BUY,
            quantity=self._compute_quantity(tick.ltp),  # Position sizing
            order_type=OrderType.MARKET,
            stop_price=tick.ltp * Decimal("0.98"),      # 2% stop-loss
            confidence=self._compute_confidence(short_ma, long_ma),
            created_at=utc_now()
        )
    return None
```

### Signal is published to Risk Engine via SQS

```python
# Strategy engine sends to risk engine's input queue
sqs.send_message(
    QueueUrl="https://sqs.ap-south-1.amazonaws.com/.../quantembrace-signals.fifo",
    MessageBody=json.dumps(signal.to_dict()),
    MessageGroupId="NSE:RELIANCE",      # FIFO: all RELIANCE signals in order
    MessageDeduplicationId=signal.signal_id  # Dedup: same signal_id = same message
)
```

**The signal_id is the key to idempotency.** This UUID is generated once and travels with the signal through every step. If the same signal is re-delivered by SQS (network retry), the deduplication ID prevents duplicate processing.

---

## Phase 3 — Risk Validates the Signal

### Risk Engine receives and validates

```python
# services/risk_engine/service.py

signal = Signal.from_dict(sqs_message_body)

decision = await self.validate_signal(signal)
```

### Validation runs in sequence

```python
# services/risk_engine/service.py → validate_signal()

# CHECK 1: Kill switch
if await self.kill_switch.is_active():
    return RiskDecision(status=REJECTED, reason="kill_switch_active")

# CHECK 2: Position validator
result = await self.position_validator.validate(signal)
if not result.approved:
    return RiskDecision(status=REJECTED, reason=result.reason)

# CHECK 3: Exposure validator
result = await self.exposure_validator.validate(signal)
if not result.approved:
    return RiskDecision(status=REJECTED, reason=result.reason)

# CHECK 4: Daily loss validator
result = await self.loss_validator.validate(signal)
if not result.approved:
    # Loss limit breached — activate kill switch automatically
    await self.kill_switch.activate(reason=result.reason)
    return RiskDecision(status=REJECTED, reason=result.reason)

# ALL CHECKS PASSED
return RiskDecision(status=APPROVED, reason="all_checks_passed")
```

### Audit log written to S3

Every decision (approve or reject) is immediately written to S3:

```
s3://quantembrace-trading-logs/risk-audit/2026-04-24/abc-789-xyz.json
```

```json
{
    "risk_decision_id": "abc-789-xyz",
    "signal_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "status": "APPROVED",
    "reason": "all_checks_passed",
    "validator_results": [
        {"validator_name": "KillSwitchCheck", "approved": true, "reason": "kill switch is OFF"},
        {"validator_name": "PositionValidator", "approved": true, "reason": "4 positions open, max 10"},
        {"validator_name": "ExposureValidator", "approved": true, "reason": "exposure 48% of limit"},
        {"validator_name": "DailyLossValidator", "approved": true, "reason": "P&L +₹8,200, no drawdown"}
    ],
    "timestamp": "2026-04-24T03:46:04.123Z"
}
```

### Approved signal forwarded to Execution Engine

```python
# Risk engine attaches its decision ID and forwards to execution queue
payload = signal.to_dict()
payload["risk_decision_id"] = "abc-789-xyz"  # Proves signal was risk-approved

sqs.send_message(
    QueueUrl="https://sqs.ap-south-1.amazonaws.com/.../quantembrace-orders.fifo",
    MessageBody=json.dumps(payload),
    MessageGroupId="NSE:RELIANCE",
    MessageDeduplicationId=signal.signal_id  # Same ID = idempotent
)
```

---

## Phase 4 — Order Placed with Broker

### Execution Engine receives approved signal

```python
# services/execution_engine/service.py

# The presence of risk_decision_id PROVES this came through the risk engine
if not signal_data.get("risk_decision_id"):
    raise ValueError("Signal missing risk_decision_id — rejected")

signal_id = signal_data["signal_id"]
risk_decision_id = signal_data["risk_decision_id"]
```

### Idempotency check

```python
# Check if we already processed this signal
existing = await order_manager.get_order_by_signal(signal_id)

if existing:
    if existing.status in (PLACED, FILLED):
        logger.info("Duplicate signal %s — order already %s", signal_id, existing.status)
        return existing  # Already done, safe to skip
    if existing.status == FAILED:
        logger.info("Retrying failed order for signal %s", signal_id)
        # Fall through to retry
```

### Order record created in DynamoDB

```python
# Write PENDING status BEFORE calling broker API
# If we crash between writing and calling the broker, we know to check the status on restart
await order_manager.create_order(
    order_id=str(uuid4()),
    signal_id=signal_id,
    risk_decision_id=risk_decision_id,
    symbol="RELIANCE",
    side=BUY,
    quantity=20,
    status=OrderStatus.PENDING
)
```

### Route to correct broker

```python
# NSE instrument → Zerodha
broker = self.zerodha if market == Market.NSE else self.alpaca

# Place the order (with retry logic)
response = await retry_handler.execute_with_retry(
    func=broker.place_order,
    order=order_request
)
```

### Zerodha API call

```python
# services/execution_engine/brokers/zerodha_broker.py

kite.place_order(
    variety=kite.VARIETY_REGULAR,
    exchange="NSE",
    tradingsymbol="RELIANCE",
    transaction_type=kite.TRANSACTION_TYPE_BUY,
    quantity=20,
    product=kite.PRODUCT_MIS,    # MIS = intraday; CNC = delivery
    order_type=kite.ORDER_TYPE_MARKET
)
# Returns: {"order_id": "240424000012345"}
```

### DynamoDB updated to PLACED

```python
await order_manager.update_order_status(
    order_id=internal_order_id,
    broker_order_id="240424000012345",
    status=OrderStatus.PLACED
)
```

---

## Phase 5 — Order Fill and Tracking

### How fills are detected

Zerodha doesn't push fill notifications via WebSocket (it uses REST polling). The execution engine polls order status at a configurable interval:

```python
# Every 2 seconds for active orders
broker_status = await zerodha.get_order_status("240424000012345")

if broker_status.status == "COMPLETE":
    await order_manager.update_order_status(
        order_id=internal_order_id,
        status=OrderStatus.FILLED,
        filled_quantity=20,
        average_price=Decimal("2454.00")  # Actual fill price
    )
    # Notify risk engine to update position tracking
```

### Final state in DynamoDB `orders` table

```json
{
    "order_id": "qe-ord-a1b2c3",
    "signal_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "risk_decision_id": "abc-789-xyz",
    "symbol": "RELIANCE",
    "market": "NSE",
    "side": "BUY",
    "quantity": 20,
    "status": "FILLED",
    "broker_order_id": "240424000012345",
    "average_price": "2454.00",
    "filled_quantity": 20,
    "created_at": "2026-04-24T03:46:04Z",
    "placed_at": "2026-04-24T03:46:04.500Z",
    "filled_at": "2026-04-24T03:46:05.200Z"
}
```

**Total time from tick to fill: approximately 0.7–2 seconds.** (Mostly broker latency — the system's own processing is under 100ms.)

---

## The Kill Switch Scenario

What happens when the kill switch is activated mid-trading-day:

```
[12:30:00 UTC] Daily P&L reaches -3.2% — loss limit is 3.0%
                        │
                        ▼
DailyLossValidator.validate() returns approved=False
                        │
                        ▼
RiskEngineService._kill_switch.activate(reason="auto: daily_loss_3.2%_>_3.0%")
                        │
           ┌────────────┼────────────────┐
           ▼            ▼                ▼
  DynamoDB:       CloudWatch         SNS Alert
  kill_switch=ON  custom metric      → Email/SMS
  reason=...      kill_switch=1      to operator
           │
           ▼
All subsequent validate_signal() calls:
  → Check kill_switch.is_active() → True → REJECT immediately
  
Pending SQS messages: consumed and rejected without processing
New ticks from data ingestion: still flowing (data layer unaffected)
Open positions: HELD (not force-closed)
                        
[Next trading day]
Operator manually calls: POST /risk/kill-switch/deactivate
                        │
                        ▼
DynamoDB: kill_switch=OFF
Risk engine: normal processing resumes
```

---

## Error Paths and Retry Scenarios

### Scenario: Broker API timeout during order placement

```
Execution engine → Zerodha API: place_order()
       timeout after 5 seconds
              │
              ▼
RetryHandler: attempt 1 failed (timeout)
              wait 1 second
              attempt 2: place_order() — same order_id
              
              ← Zerodha responds with error 429 (rate limit)
              
              wait 2 seconds
              attempt 3: place_order() — same order_id
              
              ← Zerodha responds with order_id "240424000012345"

DynamoDB updated: status = PLACED ✓
```

### Scenario: System crashes after placing order but before updating DynamoDB

```
[Crash between broker confirmation and DynamoDB write]
              │
              ▼
ECS health check fails → service restarts
              │
              ▼
Startup reconciliation:
  1. Get open orders from DynamoDB with status=PENDING
  2. Query Zerodha: "what's the status of order 240424000012345?"
  3. Zerodha says: COMPLETE, filled at 2454.00
  4. Update DynamoDB: status=FILLED ✓
  5. No duplicate order placed (broker already has it)
```

### Scenario: SQS message re-delivered (network glitch)

```
Risk engine approved signal → SQS → Execution engine
SQS thought execution engine didn't acknowledge → re-delivers message

Execution engine receives signal a second time:
  check: get_order_by_signal(signal_id)
  → finds existing order with status=FILLED
  → return early: "already processed"
  → delete SQS message

No duplicate order placed ✓ (idempotency works)
```

---

## End of Day — Market Close Sequence

### NSE Market Close (15:30 IST = 10:00 UTC)

```
[10:00 UTC] NSE market close trigger (scheduled CloudWatch event)
     │
     ▼
1. Kill switch activated with reason "market_close_nse"
2. Any pending signals in SQS queue → rejected by risk engine
3. Check for open MIS (intraday) positions:
      If any MIS positions remain open → place MARKET SELL orders
      (Zerodha auto-squares at 15:15, but system does it proactively at 15:25)
4. Wait for all orders to settle (max 5 minutes)
5. data-ingestion-nse ECS task scaled to 0 (no cost outside market hours)
6. End-of-day P&L calculation written to DynamoDB and S3
7. Kill switch deactivated for next day (at 08:30 IST next trading day)
```

### US Market Close (16:00 ET = 20:00 UTC)

Same sequence, but:
- CNC orders remain open (delivery positions are multi-day, no auto-squareoff)
- data-ingestion-us ECS task scaled to 0
- No P&L squareoff for swing positions

---

## Order State Machine

An order moves through these states exactly once in each direction. No backwards transitions.

```
                         ┌─────────┐
                         │ PENDING │  ← Created in DynamoDB before API call
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
               │ REJECTED │       │  FILLED  │  ← Terminal state
               └──────────┘       └──────────┘

     ┌─────────┐
     │ PENDING │
     └────┬────┘
          │ all 3 retries fail
          ▼
     ┌─────────┐
     │ FAILED  │  ← Terminal state, alert sent
     └─────────┘

     ┌─────────┐
     │ PLACED  │
     └────┬────┘
          │ operator or risk engine cancels
          ▼
     ┌───────────┐
     │ CANCELLED │  ← Terminal state
     └───────────┘
     
     ┌─────────┐
     │ PLACED  │
     └────┬────┘
          │ partially filled (e.g. 15 of 20 shares)
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

All state transitions use **DynamoDB conditional writes**:
```python
# Only update from PENDING → PLACED if current state IS PENDING
# This prevents race conditions if two processes try to update simultaneously
dynamodb.update_item(
    ConditionExpression="status = :pending",
    ExpressionAttributeValues={":pending": "PENDING", ":placed": "PLACED"},
    UpdateExpression="SET status = :placed"
)
```

---

*Last updated: 2026-04-24 | Update this document whenever: new order types are added, the state machine changes, a new market is integrated, or the kill switch behavior changes.*
