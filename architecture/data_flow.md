# QuantEmbrace - Data Flow

## Overview

This document traces every data path in QuantEmbrace from market tick ingestion through
order execution and back into monitoring. Every flow is designed to be traceable,
recoverable, and auditable.

---

## End-to-End Data Flow Summary

```
+----------+       +----------+
| Zerodha  |       |  Alpaca  |
| Kite WS  |       |    WS    |
+----+-----+       +----+-----+
     |                   |
     | raw ticks         | raw ticks
     v                   v
+----+-------------------+-----+
|     DATA INGESTION           |
|  (normalize + fan out)       |
+-----------+------------------+
            |
            | MarketTick (normalized)
            |
     +------+------+
     |             |
     v             v
+----+----+  +----+--------+
|DynamoDB |  |    S3        |
| latest  |  | tick history |
| prices  |  | (parquet)    |
+---------+  +----+---------+
     |             |
     | real-time   | batch (nightly)
     v             v
+----+----+  +----+---------+
|Strategy |  |   AI/ML      |
| Engine  |<-+ Feature Pipe |
+---------+  +--------------+
     |
     | Signal
     v
+----+----+
|  RISK   |-----> REJECTED (logged + alerted)
| ENGINE  |
+----+----+
     |
     | ApprovedOrder
     v
+----+------+
| EXECUTION |
|  ENGINE   |
+----+------+
     |
     | broker API calls
     |
+----+----+----------+
|         |          |
v         v          v
Zerodha   Alpaca   DynamoDB
 API       API     (orders +
                   positions)
```

---

## Flow 1: Market Data Ingestion

### Zerodha NSE Path

```
Zerodha Kite Ticker (WebSocket)
        |
        | on_ticks() callback
        | raw payload: {instrument_token, ltp, bid, ask, volume, ...}
        v
+-------+--------+
| NSE Ingestion  |
| Service        |
|                |
| 1. Parse raw   |
| 2. Normalize   |
|    to MarketTick|
| 3. Fan out     |
+---+--------+---+
    |        |
    v        v
DynamoDB    S3
```

**Step-by-step:**

1. **WebSocket Connect**: At market open (09:00 IST), the `data-ingestion-nse` service
   establishes a WebSocket connection to Zerodha Kite Ticker.

2. **Subscribe**: Subscribes to configured instruments in `full` mode:
   ```python
   kws.subscribe([instrument_tokens])
   kws.set_mode(kws.MODE_FULL, [instrument_tokens])
   ```

3. **Receive Ticks**: `on_ticks` callback fires with batched tick data (Kite batches
   ticks and delivers them roughly every second).

4. **Normalize**: Each raw tick is converted to a `MarketTick`:
   ```python
   def normalize_zerodha_tick(raw: dict) -> MarketTick:
       return MarketTick(
           market="NSE",
           instrument=f"NSE:{token_to_symbol[raw['instrument_token']]}",
           ltp=Decimal(str(raw['last_price'])),
           bid=Decimal(str(raw['depth']['buy'][0]['price'])),
           ask=Decimal(str(raw['depth']['sell'][0]['price'])),
           volume=raw['volume_traded'],
           timestamp=datetime.utcnow(),
           raw=raw,
       )
   ```

5. **Write to DynamoDB** (hot path):
   ```
   Table: latest-prices
   Key: {market}#{instrument} = "NSE#RELIANCE"
   Attributes: ltp, bid, ask, volume, timestamp
   Conditional write: only if incoming timestamp > stored timestamp
   ```

6. **Buffer and Write to S3** (cold path):
   - Ticks are buffered in memory (list of MarketTick objects)
   - Every 5 minutes, buffer is flushed to S3 as a Parquet file
   - Path: `s3://quantembrace-market-data-history/NSE/{symbol}/{date}/{hour}/ticks_{minute}.parquet`
   - Buffer flush is async -- does not block tick processing

### Alpaca US Path

Identical flow pattern with Alpaca-specific normalization:

```
Alpaca WebSocket (wss://stream.data.alpaca.markets)
        |
        | on_trade / on_quote callbacks
        v
+-------+---------+
| US Ingestion    |
| Service         |
|                 |
| 1. Parse raw    |
| 2. Normalize    |
|    to MarketTick|
| 3. Fan out      |
+---+---------+---+
    |         |
    v         v
DynamoDB     S3
```

**Key difference**: Alpaca provides separate trade and quote streams. The service
merges these into a single `MarketTick` using the latest available data for each field.

### Reconnection Logic

Both ingestion services implement the same reconnection strategy:

```
on_disconnect:
  attempt = 0
  while attempt < MAX_RECONNECT_ATTEMPTS (5):
      wait = min(2^attempt, 30) seconds  # exponential backoff, cap at 30s
      sleep(wait)
      try:
          reconnect()
          resubscribe()
          log("reconnected after {attempt} attempts")
          return
      except:
          attempt += 1

  # All attempts exhausted
  trigger_alert("CRITICAL: WebSocket reconnection failed after 5 attempts")
  activate_kill_switch()  # Safety: no data = no trading
```

---

## Flow 2: Signal Generation

```
+------------------+      +------------------+
|    DynamoDB      |      |     AI/ML        |
|  latest-prices   |      |  Enrichment      |
+--------+---------+      +--------+---------+
         |                         |
         | poll / stream           | predictions
         v                         v
+--------+-------------------------+---------+
|              STRATEGY ENGINE               |
|                                            |
|  for each active strategy:                 |
|    1. Receive latest MarketTick            |
|    2. Update internal state (indicators)   |
|    3. Check signal conditions              |
|    4. If triggered: create Signal          |
|    5. Enrich with ML predictions (optional)|
|    6. Emit Signal to Risk Engine           |
|                                            |
+--------------------+-----------------------+
                     |
                     | Signal
                     v
              [Risk Engine]
```

**Data Access Pattern:**

The Strategy Engine reads from DynamoDB `latest-prices` table using a polling loop:

```python
while market_is_open():
    # Batch read all subscribed instruments
    ticks = dynamodb.batch_get_items(
        table="latest-prices",
        keys=[f"{market}#{inst}" for inst in instruments]
    )

    for strategy in active_strategies:
        for tick in ticks:
            signal = strategy.on_tick(tick)
            if signal:
                emit_to_risk_engine(signal)

    await asyncio.sleep(0.5)  # 500ms polling interval
```

**Why polling instead of DynamoDB Streams:**
- Streams add latency (processing delay) and cost
- Polling gives consistent, predictable latency
- 500ms is fast enough for our strategies (not HFT)
- Simpler to implement and debug

---

## Flow 3: Risk Validation

```
Signal arrives from Strategy Engine
        |
        v
+-------+--------+
|   RISK ENGINE   |
|                 |
|   Sequential    |
|   Checks:       |
|                 |
|   1. kill_switch|----> REJECT: "kill switch active"
|   2. pos_limit  |----> REJECT: "max positions reached"
|   3. exposure   |----> REJECT: "exposure limit exceeded"
|   4. stop_loss  |----> REJECT: "no stop-loss provided"
|   5. drawdown   |----> REJECT: "daily drawdown exceeded"
|   6. instrument |----> REJECT: "instrument limit exceeded"
|   7. margin     |----> REJECT: "insufficient margin"
|                 |
|   ALL PASS:     |
|   Signal ->     |
|   ApprovedOrder |
+-------+---------+
        |
        | ApprovedOrder (or RejectedSignal)
        v
+-------+---------+
|                  |
| Write to DynamoDB|
| risk-state table |
| (audit trail)    |
|                  |
+------------------+
```

**Risk Engine Data Dependencies:**

```
                      +----------+
                      | DynamoDB |
                      +----+-----+
                           |
          +----------------+----------------+
          |                |                |
   +------+-----+  +------+-----+  +------+------+
   | positions  |  | risk-state |  | risk-config |
   | table      |  | table      |  | table       |
   |            |  |            |  |             |
   | Current    |  | Daily PnL  |  | Limits      |
   | open       |  | Kill switch|  | Thresholds  |
   | positions  |  | Drawdown   |  | Parameters  |
   +------------+  +------------+  +-------------+
```

**Every rejected signal is logged with full context:**

```json
{
    "event": "signal_rejected",
    "signal_id": "abc-123",
    "strategy": "momentum_breakout",
    "instrument": "NSE:RELIANCE",
    "direction": "BUY",
    "quantity": 100,
    "rejection_reason": "daily_drawdown_exceeded",
    "risk_state": {
        "current_daily_pnl": -31500.00,
        "max_daily_drawdown": -30000.00,
        "kill_switch_activated": true
    }
}
```

---

## Flow 4: Order Execution

```
ApprovedOrder from Risk Engine
        |
        v
+-------+---------+
| EXECUTION ENGINE |
|                  |
| 1. Check idempotency (DynamoDB orders table)
|    - If order_id exists with PLACED/FILLED: skip
|    - If order_id exists with FAILED: retry
|    - If new: proceed
|                  |
| 2. Write PENDING to DynamoDB orders table
|                  |
| 3. Select broker adapter based on market
|    - NSE -> ZerodhaAdapter
|    - US  -> AlpacaAdapter
|                  |
| 4. Place order via adapter
|    - Translate to broker-specific format
|    - Submit API call
|    - Receive broker_order_id
|                  |
| 5. Update DynamoDB orders table
|    - status: PLACED
|    - broker_order_id: <received>
|                  |
| 6. Start fill monitoring
|    - Poll broker for order status
|    - Update on fill/partial/reject
|                  |
+-------+---------+
        |
        | OrderResult
        v
+-------+---------+
|  POST-EXECUTION  |
|                  |
| 1. Update DynamoDB positions table
|    - Add/modify position
|    - Update average price
|                  |
| 2. Notify Risk Engine
|    - New position info
|    - PnL update
|                  |
| 3. Log execution details
|    - Fill price, slippage, latency
|                  |
+------------------+
```

### Zerodha Order Placement Detail

```python
def place_order_zerodha(order: ApprovedOrder) -> OrderResult:
    try:
        broker_order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NSE,
            tradingsymbol=order.instrument.split(":")[1],
            transaction_type=kite.TRANSACTION_TYPE_BUY if order.direction == "BUY"
                           else kite.TRANSACTION_TYPE_SELL,
            quantity=order.quantity,
            product=kite.PRODUCT_MIS,  # or CNC for delivery
            order_type=MAP_ORDER_TYPE[order.order_type],
            price=float(order.limit_price) if order.limit_price else None,
            trigger_price=float(order.stop_price) if order.stop_price else None,
            tag=order.order_id[:20],  # Kite allows 20-char tag for tracking
        )
        return OrderResult(
            order_id=order.order_id,
            broker_order_id=str(broker_order_id),
            status="PLACED",
            placed_at=datetime.utcnow(),
        )
    except KiteException as e:
        return OrderResult(
            order_id=order.order_id,
            status="FAILED",
            error=str(e),
            retryable=e.code in RETRYABLE_ERROR_CODES,
        )
```

### Alpaca Order Placement Detail

```python
def place_order_alpaca(order: ApprovedOrder) -> OrderResult:
    try:
        alpaca_order = api.submit_order(
            symbol=order.instrument.split(":")[1],
            qty=order.quantity,
            side="buy" if order.direction == "BUY" else "sell",
            type=MAP_ORDER_TYPE[order.order_type],
            time_in_force="day",
            limit_price=str(order.limit_price) if order.limit_price else None,
            stop_price=str(order.stop_price) if order.stop_price else None,
            client_order_id=order.order_id,  # Alpaca supports client order IDs natively
        )
        return OrderResult(
            order_id=order.order_id,
            broker_order_id=alpaca_order.id,
            status="PLACED",
            placed_at=datetime.utcnow(),
        )
    except APIError as e:
        return OrderResult(
            order_id=order.order_id,
            status="FAILED",
            error=str(e),
            retryable=e.status_code in [429, 500, 502, 503],
        )
```

---

## Flow 5: Position Tracking

```
+------------------+     +------------------+
| Execution Engine |     |  Broker API      |
| (fill received)  |     | (position sync)  |
+--------+---------+     +--------+---------+
         |                        |
         | on each fill           | periodic reconciliation
         v                        v
+--------+------------------------+---------+
|              POSITION MANAGER              |
|                                            |
|  1. Update position in DynamoDB            |
|     - Instrument, qty, avg_price, side     |
|     - Unrealized PnL (using latest price)  |
|     - Realized PnL (on closes)             |
|                                            |
|  2. Reconcile with broker positions        |
|     - Every 5 minutes during market hours  |
|     - Flag discrepancies for manual review |
|                                            |
|  3. Feed risk engine                       |
|     - Total exposure update                |
|     - Per-instrument exposure update       |
|     - PnL update                           |
|                                            |
+--------------------+-----------------------+
                     |
                     v
              +------+------+
              |  DynamoDB   |
              |  positions  |
              |  table      |
              +------+------+
                     |
                     | Risk Engine reads
                     v
              +------+------+
              | Risk Engine |
              | (monitors   |
              |  exposure)  |
              +-------------+
```

### DynamoDB Positions Table Schema

```
Table: positions
Partition Key: market#instrument (e.g., "NSE#RELIANCE")

Attributes:
  market:           String   "NSE"
  instrument:       String   "RELIANCE"
  direction:        String   "LONG" or "SHORT"
  quantity:         Number   100
  avg_entry_price:  Number   2450.50
  current_price:    Number   2465.00
  unrealized_pnl:   Number   1450.00
  realized_pnl:     Number   0.00
  stop_loss_price:  Number   2401.49
  opened_at:        String   "2026-04-23T10:15:00Z"
  last_updated:     String   "2026-04-23T11:30:00Z"
  strategy:         String   "momentum_breakout"
  order_ids:        List     ["abc-123", "def-456"]
```

### Reconciliation Process

```
Every 5 minutes:

  broker_positions = adapter.get_positions()
  db_positions = dynamodb.scan("positions", market=market)

  for each instrument:
      broker_qty = broker_positions.get(instrument, 0)
      db_qty = db_positions.get(instrument, 0)

      if broker_qty != db_qty:
          log.error("POSITION MISMATCH", instrument=instrument,
                    broker=broker_qty, db=db_qty)
          alert("Position mismatch detected for {instrument}")
          # Do NOT auto-correct. Flag for manual review.
          # Auto-correction could mask bugs and compound errors.
```

---

## Flow 6: AI/ML Pipeline

```
+---------------------+
|  S3                  |
|  market-data-history |
|  (Parquet files)     |
+---------+-----------+
          |
          | read historical data
          v
+---------+-----------+
|  FEATURE PIPELINE   |  (runs nightly as batch ECS task)
|                     |
|  1. Load N days of  |
|     tick data       |
|  2. Resample to     |
|     bars (1m, 5m)   |
|  3. Compute features|
|     - SMA, EMA      |
|     - RSI, MACD     |
|     - Volatility    |
|     - Volume profile|
|     - Correlation   |
|  4. Store feature   |
|     dataset in S3   |
+---------+-----------+
          |
          | feature datasets
          v
+---------+-----------+
|  MODEL TRAINING     |  (runs weekly or on-demand)
|                     |
|  1. Load features   |
|  2. Train models    |
|     - XGBoost for   |
|       volatility    |
|     - Random Forest |
|       for regime    |
|  3. Evaluate on     |
|     holdout set     |
|  4. If improved:    |
|     save to S3      |
|     model registry  |
+---------+-----------+
          |
          | model artifacts (ONNX/pickle)
          v
+---------+-----------+
|  S3 MODEL REGISTRY  |
|                     |
|  models/            |
|    vol_predictor/   |
|      v1.0.0/        |
|    regime_clf/      |
|      v1.0.0/        |
+---------+-----------+
          |
          | loaded at startup
          v
+---------+-----------+
|  STRATEGY ENGINE    |
|  (inference)        |
|                     |
|  On each tick:      |
|  1. Extract live    |
|     features        |
|  2. Run model       |
|     inference       |
|  3. Enrich signal   |
|     with prediction |
|     (e.g., vol adj  |
|      position size) |
+---------------------+
```

### AI/ML Output Integration

The AI/ML layer does NOT generate signals directly. It enriches strategy signals:

```python
@dataclass
class MLEnrichment:
    predicted_volatility: float     # Next-hour predicted volatility
    regime: str                     # "trending", "ranging", "volatile"
    regime_confidence: float        # 0.0 - 1.0
    suggested_position_scale: float # 0.5 - 1.5 multiplier

# Strategy uses enrichment to adjust signal:
signal.quantity = int(base_quantity * enrichment.suggested_position_scale)
signal.metadata["ml_regime"] = enrichment.regime
signal.metadata["ml_volatility"] = enrichment.predicted_volatility
```

---

## Flow 7: Monitoring and Alerting

```
All Services
     |
     | structured JSON logs
     v
+----+----------+
|  CloudWatch   |
|  Logs         |
+----+----------+
     |
     | metric filters
     v
+----+----------+
|  CloudWatch   |
|  Metrics      |
|               |
|  Custom:      |
|  - trade_count|
|  - pnl_daily  |
|  - latency_ms |
|  - error_count|
|  - ws_status  |
+----+----------+
     |
     | threshold alarms
     v
+----+----------+
|  CloudWatch   |
|  Alarms       |
|               |
|  CRITICAL:    |
|  - ws_disconn |    +----------+
|    > 60s      +--->|   SNS    |---> Email + SMS
|  - error_rate |    +----------+
|    > 10%      |
|  - drawdown   |
|    > threshold|
|  - ecs_crash  |
+---------------+
```

### Key Metrics Tracked

| Metric                    | Source           | Alarm Threshold           | Action            |
|---------------------------|------------------|---------------------------|--------------------|
| ws_connected              | Data Ingestion   | 0 for > 60s              | Alert + kill switch|
| tick_lag_seconds           | Data Ingestion   | > 5s                     | Alert              |
| signals_generated_count    | Strategy Engine  | 0 for > 30min (mkt open) | Alert              |
| signals_rejected_count     | Risk Engine      | > 20 in 5min             | Alert              |
| orders_placed_count        | Execution Engine | Informational             | Dashboard          |
| order_failure_rate         | Execution Engine | > 10% in 5min            | Alert + kill switch|
| daily_pnl                  | Risk Engine      | < -drawdown_limit        | Kill switch        |
| execution_latency_ms       | Execution Engine | p99 > 2000ms             | Alert              |
| position_mismatch_count    | Position Manager | > 0                      | Alert              |

---

## Data Retention Policy

| Data Type          | Storage     | Retention    | Format   | Access Pattern         |
|--------------------|-------------|--------------|----------|------------------------|
| Live prices        | DynamoDB    | 24h TTL      | JSON     | Real-time reads        |
| Tick history       | S3 Standard | 90 days      | Parquet  | Backtesting, ML        |
| Tick history (old) | S3 Glacier  | 3 years      | Parquet  | Rare analysis          |
| Orders             | DynamoDB    | 1 year       | JSON     | Audit, debugging       |
| Positions          | DynamoDB    | Current only | JSON     | Real-time monitoring   |
| Logs               | CloudWatch  | 30 days      | JSON     | Debugging              |
| Logs (archived)    | S3          | 1 year       | JSON.gz  | Compliance, audit      |
| Model artifacts    | S3          | All versions | ONNX/pkl | Inference, rollback    |
| Feature datasets   | S3          | 90 days      | Parquet  | Model training         |
