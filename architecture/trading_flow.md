# QuantEmbrace - Trading Flow

## Overview

This document details the complete trading lifecycle in QuantEmbrace, from market
open to market close, across both NSE India and US Equity markets. It covers the
order lifecycle, risk checkpoints, error handling, recovery flows, and the kill switch
mechanism.

---

## Market Hours and Timezone Handling

### Market Schedule

```
+------------------+-------------------+-------------------+---------------------+
| Market           | Trading Hours     | In IST            | In UTC              |
+------------------+-------------------+-------------------+---------------------+
| NSE India        | 09:15 - 15:30 IST| 09:15 - 15:30 IST| 03:45 - 10:00 UTC   |
| US Equities      | 09:30 - 16:00 ET | 19:00 - 01:30 IST*| 13:30 - 20:00 UTC  |
|                  |                   | (next day)        |                     |
+------------------+-------------------+-------------------+---------------------+

* US hours in IST shift by 30 minutes during DST transitions:
  - EDT (Mar-Nov): 19:00 - 01:30 IST
  - EST (Nov-Mar): 20:00 - 02:30 IST
```

### System Startup Timeline (Typical Day)

```
UTC Time    IST Time    Event
--------    --------    -----
03:00       08:30       NSE pre-market: start data-ingestion-nse service
03:00       08:30       Start risk-engine, strategy-engine, execution-engine
03:15       08:45       NSE data ingestion connects to Kite WebSocket
03:30       09:00       Risk engine loads today's config, resets daily counters
03:45       09:15       >>> NSE MARKET OPENS <<<
03:45       09:15       Strategy engine begins processing NSE ticks
...
10:00       15:30       >>> NSE MARKET CLOSES <<<
10:05       15:35       NSE end-of-day: square off intraday positions
10:30       16:00       NSE data ingestion disconnects, service scales to 0
...
13:00       18:30       US pre-market: start data-ingestion-us service
13:15       18:45       US data ingestion connects to Alpaca WebSocket
13:30       19:00       >>> US MARKET OPENS <<<
13:30       19:00       Strategy engine begins processing US ticks
...
20:00       01:30+1     >>> US MARKET CLOSES <<<
20:05       01:35+1     US end-of-day: square off intraday positions
20:30       02:00+1     US data ingestion disconnects, service scales to 0
20:30       02:00+1     Risk engine, strategy engine, execution engine scale to 0
```

### Timezone Rules in Code

```python
# ALL timestamps in the system are stored and processed in UTC.
# Conversion to local time happens ONLY for:
#   1. Display/logging (human readability)
#   2. Market open/close comparisons
#   3. Holiday calendar lookups

import pytz

NSE_TZ = pytz.timezone("Asia/Kolkata")       # UTC+5:30, no DST
US_TZ  = pytz.timezone("America/New_York")   # UTC-5 (EST) or UTC-4 (EDT)

def is_market_open(market: Market) -> bool:
    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    
    if market == Market.NSE:
        now_local = now_utc.astimezone(NSE_TZ)
        market_open = now_local.replace(hour=9, minute=15, second=0)
        market_close = now_local.replace(hour=15, minute=30, second=0)
        return market_open <= now_local <= market_close and is_trading_day_nse(now_local)
    
    elif market == Market.US:
        now_local = now_utc.astimezone(US_TZ)
        market_open = now_local.replace(hour=9, minute=30, second=0)
        market_close = now_local.replace(hour=16, minute=0, second=0)
        return market_open <= now_local <= market_close and is_trading_day_us(now_local)
```

---

## Order Lifecycle

### Complete Flow Diagram

```
[1] SIGNAL GENERATION
         |
         | Strategy generates Signal
         v
[2] SIGNAL VALIDATION (self-check)
         |
         | Signal.validate() passes
         v
[3] RISK CHECK: Kill Switch
         |
         | Kill switch is OFF
         v
[4] RISK CHECK: Position Limits
         |
         | Under max positions
         v
[5] RISK CHECK: Exposure Limits
         |
         | Under gross/net exposure caps
         v
[6] RISK CHECK: Stop-Loss Present
         |
         | Stop-loss attached (or default applied)
         v
[7] RISK CHECK: Drawdown
         |
         | Daily/weekly drawdown within limits
         v
[8] RISK CHECK: Instrument Limits
         |
         | Under per-instrument caps
         v
[9] RISK CHECK: Margin Availability
         |
         | Sufficient margin with buffer
         v
[10] ORDER APPROVED
         |
         | ApprovedOrder created with RiskApproval audit trail
         v
[11] IDEMPOTENCY CHECK
         |
         | order_id not already in DynamoDB with PLACED/FILLED
         v
[12] WRITE PENDING STATUS
         |
         | DynamoDB orders table: status=PENDING
         v
[13] BROKER API CALL
         |
    +----+----+
    |         |
  SUCCESS   FAILURE
    |         |
    v         v
[14a]      [14b]
UPDATE     RETRY?
PLACED     +----+----+
    |      |         |
    v    YES        NO
[15]       |         |
MONITOR    v         v
FILL     RETRY    MARK FAILED
    |    (goto 13)   |
    |                v
    v             ALERT
+---+---+
|       |
FILL  REJECT
|       |
v       v
[16a]  [16b]
UPDATE UPDATE
FILLED REJECTED
|       |
v       v
[17]   [17]
UPDATE  LOG
POSITION REJECTION
|
v
[18]
RISK ENGINE
PnL UPDATE
```

### Step-by-Step Detail

#### Step 1-2: Signal Generation and Self-Validation

The strategy engine processes market ticks and generates signals:

```python
# Inside strategy engine main loop
async def process_tick(tick: MarketTick):
    for strategy in active_strategies:
        if tick.market in strategy.markets:
            signal = strategy.on_tick(tick)
            if signal is not None:
                errors = signal.validate()
                if errors:
                    log.warning("Signal validation failed",
                                strategy=strategy.name,
                                errors=errors)
                    continue
                
                # Optionally enrich with ML predictions
                if ml_service.is_available():
                    signal.ml_enrichment = ml_service.predict(tick, signal)
                
                await submit_to_risk_engine(signal)
```

#### Steps 3-9: Risk Validation (Sequential, All Must Pass)

```python
class RiskEngine:
    async def validate_signal(self, signal: Signal) -> Union[ApprovedOrder, RejectedSignal]:
        checks = [
            ("kill_switch",       self._check_kill_switch),
            ("position_limit",    self._check_position_limits),
            ("exposure",          self._check_exposure),
            ("stop_loss",         self._check_stop_loss),
            ("drawdown",          self._check_drawdown),
            ("instrument_limit",  self._check_instrument_limits),
            ("margin",            self._check_margin),
        ]
        
        passed_checks = []
        
        for check_name, check_fn in checks:
            result = await check_fn(signal)
            if not result.passed:
                log.info("Signal rejected",
                         signal_id=signal.signal_id,
                         check=check_name,
                         reason=result.reason)
                return RejectedSignal(
                    signal_id=signal.signal_id,
                    strategy_name=signal.strategy_name,
                    market=signal.market,
                    instrument=signal.instrument,
                    direction=signal.direction,
                    quantity=signal.quantity,
                    rejection_reason=result.rejection_reason,
                    rejection_details=result.reason,
                    risk_state_snapshot=await self._get_risk_snapshot(),
                )
            passed_checks.append(check_name)
        
        # ALL checks passed
        return self._create_approved_order(signal, passed_checks)
```

#### Step 10: Order Approval

```python
def _create_approved_order(self, signal: Signal, checks: list[str]) -> ApprovedOrder:
    return ApprovedOrder(
        order_id=str(uuid4()),
        signal_id=signal.signal_id,
        strategy_name=signal.strategy_name,
        market=signal.market,
        instrument=signal.instrument,
        direction=signal.direction,
        quantity=signal.quantity,
        order_type=signal.order_type,
        limit_price=signal.limit_price,
        stop_price=signal.stop_price,
        stop_loss_price=signal.stop_loss_price or self._compute_default_sl(signal),
        product_type=self._determine_product_type(signal),
        risk_approval=RiskApproval(
            approved_at=datetime.utcnow(),
            checks_passed=checks,
            pre_trade_exposure_pct=self._current_exposure_pct(),
            post_trade_exposure_pct=self._projected_exposure_pct(signal),
            available_margin=self._available_margin(signal.market),
            daily_pnl_at_approval=self._daily_pnl(signal.market),
            risk_engine_version=VERSION,
        ),
    )
```

#### Steps 11-13: Idempotent Order Placement

```python
class ExecutionEngine:
    async def execute_order(self, order: ApprovedOrder) -> OrderResult:
        # Step 11: Idempotency check
        existing = await self.orders_table.get(order.order_id)
        if existing:
            if existing.status in (OrderStatus.PLACED, OrderStatus.FILLED):
                log.info("Idempotent skip", order_id=order.order_id,
                         status=existing.status)
                return existing
            elif existing.status == OrderStatus.FAILED and existing.is_retryable:
                log.info("Retrying failed order", order_id=order.order_id)
                # Fall through to placement
            else:
                log.info("Order in terminal non-retryable state",
                         order_id=order.order_id, status=existing.status)
                return existing
        
        # Step 12: Write PENDING
        await self.orders_table.put(OrderResult(
            order_id=order.order_id,
            signal_id=order.signal_id,
            market=order.market,
            instrument=order.instrument,
            direction=order.direction,
            requested_quantity=order.quantity,
            status=OrderStatus.PENDING,
        ))
        
        # Step 13: Place with broker
        adapter = self._get_adapter(order.market)
        return await self._place_with_retry(adapter, order)
```

#### Steps 14-16: Broker Interaction and Fill Monitoring

```python
async def _place_with_retry(self, adapter: BrokerAdapter, 
                              order: ApprovedOrder) -> OrderResult:
    max_retries = 3
    backoff = [1, 2, 4]  # seconds
    
    for attempt in range(max_retries):
        try:
            start = time.monotonic()
            result = await adapter.place_order(order)
            latency_ms = int((time.monotonic() - start) * 1000)
            result.execution_latency_ms = latency_ms
            
            if result.status == OrderStatus.PLACED:
                # Step 14a: Success
                await self.orders_table.update(result)
                log.info("Order placed", order_id=order.order_id,
                         broker_order_id=result.broker_order_id,
                         latency_ms=latency_ms)
                
                # Step 15: Start monitoring for fill
                asyncio.create_task(self._monitor_fill(adapter, result))
                return result
            
            elif result.status == OrderStatus.REJECTED:
                # Step 16b: Broker rejected (not retryable)
                await self.orders_table.update(result)
                log.warning("Order rejected by broker",
                            order_id=order.order_id,
                            error=result.error_message)
                return result
        
        except BrokerTimeoutError:
            if attempt < max_retries - 1:
                log.warning("Broker timeout, retrying",
                            order_id=order.order_id,
                            attempt=attempt + 1)
                await asyncio.sleep(backoff[attempt])
            else:
                # Step 14b: All retries exhausted
                result = OrderResult(
                    order_id=order.order_id,
                    signal_id=order.signal_id,
                    market=order.market,
                    instrument=order.instrument,
                    direction=order.direction,
                    requested_quantity=order.quantity,
                    status=OrderStatus.FAILED,
                    error_message="Broker timeout after 3 attempts",
                    is_retryable=False,
                    retry_count=attempt + 1,
                )
                await self.orders_table.update(result)
                await self._alert("Order failed after retries", result)
                return result
        
        except BrokerRateLimitError:
            log.warning("Rate limited by broker, waiting",
                        order_id=order.order_id)
            await asyncio.sleep(2)
            # Retry on next iteration
        
        except (BrokerInsufficientMarginError, BrokerInvalidOrderError) as e:
            # Non-retryable broker errors
            result = OrderResult(
                order_id=order.order_id,
                signal_id=order.signal_id,
                market=order.market,
                instrument=order.instrument,
                direction=order.direction,
                requested_quantity=order.quantity,
                status=OrderStatus.FAILED,
                error_message=str(e),
                is_retryable=False,
            )
            await self.orders_table.update(result)
            return result
```

#### Steps 17-18: Position Update and PnL Tracking

```python
async def _on_fill(self, result: OrderResult):
    """Called when an order is fully or partially filled."""
    
    # Step 17: Update position
    position_key = f"{result.market.value}#{result.instrument}"
    existing = await self.positions_table.get(position_key)
    
    if existing:
        # Update existing position
        if result.direction == Direction.BUY and existing.side == PositionSide.LONG:
            # Adding to long position
            new_qty = existing.quantity + result.filled_quantity
            new_avg = (
                (existing.avg_entry_price * existing.quantity +
                 result.fill_price * result.filled_quantity) / new_qty
            )
            await self.positions_table.update(position_key, {
                "quantity": new_qty,
                "avg_entry_price": new_avg,
                "order_ids": existing.order_ids + [result.order_id],
                "last_updated": datetime.utcnow().isoformat(),
            })
        elif result.direction == Direction.SELL and existing.side == PositionSide.LONG:
            # Closing/reducing long position
            new_qty = existing.quantity - result.filled_quantity
            realized = (result.fill_price - existing.avg_entry_price) * result.filled_quantity
            
            if new_qty == 0:
                await self.positions_table.delete(position_key)
            else:
                await self.positions_table.update(position_key, {
                    "quantity": new_qty,
                    "realized_pnl": existing.realized_pnl + realized,
                    "last_updated": datetime.utcnow().isoformat(),
                })
            
            # Step 18: Update risk engine PnL
            await self._update_daily_pnl(result.market, realized)
    else:
        # New position
        await self.positions_table.put(Position(
            market=result.market,
            instrument=result.instrument,
            side=PositionSide.LONG if result.direction == Direction.BUY else PositionSide.SHORT,
            quantity=result.filled_quantity,
            avg_entry_price=result.fill_price,
            current_price=result.fill_price,
            unrealized_pnl=Decimal("0"),
            realized_pnl=Decimal("0"),
            stop_loss_price=...,  # from ApprovedOrder
            strategy_name=...,    # from Signal
            opened_at=datetime.utcnow(),
            last_updated=datetime.utcnow(),
            order_ids=[result.order_id],
            notional_value=result.fill_price * result.filled_quantity,
        ))
```

---

## Risk Checkpoints Map

Every stage of the trading flow has risk checkpoints. This map shows where
risk is enforced:

```
STAGE                          RISK CHECKPOINTS
-----                          ----------------

Signal Generation              - Strategy self-validation
                               - Signal.validate() catches malformed signals

Risk Engine Entry              - Kill switch check (FIRST, always)
                               - Position limits
                               - Exposure limits
                               - Stop-loss presence
                               - Drawdown checks
                               - Instrument limits
                               - Margin check

Pre-Execution                  - Idempotency check (prevents duplicate orders)
                               - Order expiry check (reject stale approved orders)

During Execution               - Broker-side margin check
                               - Broker-side position limits
                               - Rate limiting (prevents runaway order placement)

Post-Execution                 - Position reconciliation
                               - PnL update triggers drawdown check
                               - Stop-loss order placement for new positions

Continuous Monitoring          - Unrealized PnL monitoring
                               - Stop-loss trigger monitoring
                               - WebSocket health monitoring
                               - Position mismatch detection

End of Day                     - Intraday position square-off
                               - Daily PnL reconciliation
                               - Position count verification
```

---

## Kill Switch Activation Flow

The kill switch is the nuclear option. When activated, ALL trading stops immediately.

### Trigger Conditions

```
Kill switch can be activated by:

1. AUTOMATIC: Daily drawdown exceeds limit
   - daily_pnl < -(max_daily_drawdown_pct * capital / 100)

2. AUTOMATIC: Weekly drawdown exceeds limit
   - weekly_pnl < -(max_weekly_drawdown_pct * capital / 100)

3. AUTOMATIC: Order failure rate exceeds threshold
   - > 10% of orders failing in a 5-minute window

4. AUTOMATIC: WebSocket connection lost and not recovered
   - Reconnection attempts exhausted (5 attempts)

5. MANUAL: API call or DynamoDB direct update
   - Operator writes kill_switch.active = true to risk-state table
```

### Activation Sequence

```
TRIGGER EVENT
     |
     v
[1] Set kill_switch.active = true in DynamoDB
     |
     v
[2] Reject ALL pending signals immediately
     |    (risk engine returns KILL_SWITCH_ACTIVE for everything)
     v
[3] Cancel ALL open orders across all brokers
     |
     +-- Zerodha: kite.cancel_order() for each open order
     +-- Alpaca:  api.cancel_all_orders()
     |
     v
[4] Log all cancelled orders with reason "KILL_SWITCH"
     |
     v
[5] Send CRITICAL alert via SNS
     |
     +-- Email: "KILL SWITCH ACTIVATED: {reason}"
     +-- SMS: "QE KILL SWITCH ON: {reason}" (optional)
     |
     v
[6] Existing positions are NOT automatically closed
     |    (closing positions during a drawdown could worsen losses)
     |    (human must decide whether to close or hold)
     |
     v
[7] System continues running but no new orders can be placed
     |
     v
[8] Kill switch remains active until manually deactivated
     (unless kill_switch_auto_reset is true, in which case
      it resets at the configured time -- NOT recommended)
```

### Deactivation

```python
async def deactivate_kill_switch(self, deactivated_by: str):
    """
    Only call this after human review of the situation.
    """
    current = await self.get_kill_switch_state()
    if not current.active:
        log.warning("Kill switch already inactive")
        return
    
    await self.risk_state_table.update("kill_switch", {
        "active": False,
        "deactivated_at": datetime.utcnow().isoformat(),
        "deactivated_by": deactivated_by,
    })
    
    log.info("Kill switch deactivated", by=deactivated_by)
    await self._alert_info(f"Kill switch deactivated by {deactivated_by}")
```

---

## Restart and Recovery Flow

### Design Principle

**All state lives in DynamoDB.** No critical state is held only in memory. When a service
restarts, it reads its state from DynamoDB and resumes exactly where it left off.

### Service Restart Scenarios

#### Data Ingestion Service Restart

```
Service crashes or is redeployed
     |
     v
ECS detects task stopped
     |
     v
ECS starts new task (within 30-60 seconds)
     |
     v
New task initializes
     |
     v
Connects to broker WebSocket
     |
     v
Resubscribes to instruments
     |
     v
Resumes writing to DynamoDB + S3
     |
     v
GAP: ticks during downtime are lost
     (acceptable: strategies use latest price, not tick-by-tick history)
     (S3 history will have a gap -- logged for awareness)
```

#### Strategy Engine Restart

```
Service crashes or is redeployed
     |
     v
ECS starts new task
     |
     v
Load strategy configurations from S3
     |
     v
Load strategy state from DynamoDB (strategy-state table)
     |   - Indicator values, counters, etc.
     |   - Each strategy serializes its state periodically
     v
Load ML models from S3
     |
     v
Resume tick processing
     |
     v
NOTE: Signals generated before crash that were not yet
      processed by risk engine are lost (acceptable: new
      signals will be generated on next tick)
```

#### Risk Engine Restart

```
Service crashes or is redeployed
     |
     v
ECS starts new task
     |
     v
Load risk config from DynamoDB (risk-config table)
     |
     v
Load risk state from DynamoDB (risk-state table)
     |   - Kill switch state
     |   - Daily PnL
     |   - Weekly PnL
     v
Load current positions from DynamoDB (positions table)
     |
     v
Verify kill switch state
     |   - If it was active before crash, it stays active
     |   - If daily PnL exceeds limit, activate kill switch
     v
Resume signal validation
     |
     v
CRITICAL: Risk engine must NEVER start in a state that allows
          trading when it should be blocked. Default to SAFE
          (reject signals) if state is unclear.
```

#### Execution Engine Restart

```
Service crashes or is redeployed
     |
     v
ECS starts new task
     |
     v
Load pending orders from DynamoDB (orders table, status=PENDING or PLACED)
     |
     v
For each PENDING order:
     |   - Check if it was actually placed with broker (query broker API)
     |   - If placed: update DynamoDB to PLACED, start monitoring
     |   - If not placed: re-evaluate (has it expired? is kill switch on?)
     |     - If valid: re-place (idempotent, same order_id)
     |     - If expired: mark CANCELLED
     v
For each PLACED order:
     |   - Check current status with broker
     |   - If filled: update DynamoDB to FILLED, update position
     |   - If still open: resume monitoring
     |   - If cancelled/rejected: update DynamoDB accordingly
     v
Resume normal operation
     |
     v
CRITICAL: The idempotency mechanism (order_id based) ensures
          that no duplicate orders are placed during recovery.
```

### Idempotency Deep Dive

```
Order Placement Idempotency Guarantee:

1. Every signal gets a unique signal_id (UUID) at creation
2. Every approved order gets a unique order_id (UUID) at approval
3. Before placing with broker:
   a. Check DynamoDB for existing order with this order_id
   b. If found with status PLACED/FILLED -> skip (already done)
   c. If found with status FAILED (retryable) -> retry with same order_id
   d. If not found -> write PENDING, then place

4. Broker-side idempotency:
   - Alpaca: supports client_order_id natively (set to our order_id)
   - Zerodha: uses tag field (limited to 20 chars, so we use first 20 of UUID)
     + additionally check recent orders for matching tag before placing

5. DynamoDB write idempotency:
   - Use conditional writes: PutItem with condition "attribute_not_exists(order_id)"
   - This prevents race conditions if two instances try to write the same order
```

---

## End-of-Day Flow

### NSE End-of-Day (15:30 IST)

```
15:30 IST: NSE market closes
     |
     v
[1] Stop generating signals for NSE
     |
     v
[2] Check for open intraday (MIS/INTRADAY) positions on NSE
     |
     +-- If any exist:
     |   |
     |   v
     |   [3] Square off intraday positions
     |       - Generate SELL signal for each long MIS position
     |       - Generate BUY signal for each short MIS position
     |       - These signals STILL go through risk engine
     |         (risk engine has special handling for EOD square-off:
     |          allows square-off even if kill switch is on)
     |       - Place as MARKET orders
     |
     +-- If none: skip
     |
     v
[4] Reconcile positions with Zerodha
     |   - broker_positions = kite.positions()
     |   - Compare with DynamoDB positions table
     |   - Flag any mismatches
     v
[5] Calculate daily PnL for NSE
     |   - Sum of realized PnL from all closed positions
     |   - Save to DynamoDB risk-state
     v
[6] Write daily summary log
     |   {
     |     "date": "2026-04-23",
     |     "market": "NSE",
     |     "trades_placed": 12,
     |     "trades_filled": 11,
     |     "trades_rejected_risk": 3,
     |     "realized_pnl": 4500.00,
     |     "open_positions_delivery": 2,
     |     "open_positions_intraday_squared_off": 3
     |   }
     v
[7] Scale down NSE data ingestion service to 0 tasks
```

### US End-of-Day (16:00 ET)

Same flow as NSE, with US-specific broker calls (Alpaca).

```
16:00 ET: US market closes
     |
     v
[1] Stop generating signals for US
     |
     v
[2] Square off intraday positions on US (if any)
     |   - api.close_all_positions() for day-trade positions
     |   - Still goes through risk engine
     v
[3] Reconcile with Alpaca
     |   - api.list_positions()
     |   - Compare with DynamoDB
     v
[4] Calculate daily PnL for US
     v
[5] Write daily summary
     v
[6] Scale down US data ingestion and (if no other market active) all engines
```

---

## Circuit Breaker Pattern

The execution engine implements a circuit breaker to prevent cascading failures
when a broker API is having issues.

### Circuit Breaker States

```
        +----------+     failure_count >= threshold     +----------+
        |          | ---------------------------------> |          |
        |  CLOSED  |                                    |   OPEN   |
        | (normal) |                                    | (halted) |
        |          | <---------------------------------  |          |
        +----------+     timer expires, try one order   +----+-----+
              ^                                              |
              |            success                           |
              +------- +------------+ <----------------------+
                       | HALF-OPEN  |   timer expires
                       | (testing)  |
                       +------------+
```

### Implementation

```python
class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5,
                 recovery_timeout: int = 60,
                 half_open_max_calls: int = 1):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout  # seconds
        self.half_open_max_calls = half_open_max_calls
        
        self.state = "CLOSED"
        self.failure_count = 0
        self.last_failure_time = None
        self.half_open_calls = 0
    
    def can_execute(self) -> bool:
        if self.state == "CLOSED":
            return True
        
        if self.state == "OPEN":
            # Check if recovery timeout has elapsed
            if (datetime.utcnow() - self.last_failure_time).seconds >= self.recovery_timeout:
                self.state = "HALF_OPEN"
                self.half_open_calls = 0
                log.info("Circuit breaker: OPEN -> HALF_OPEN")
                return True
            return False
        
        if self.state == "HALF_OPEN":
            return self.half_open_calls < self.half_open_max_calls
        
        return False
    
    def record_success(self):
        if self.state == "HALF_OPEN":
            self.state = "CLOSED"
            self.failure_count = 0
            log.info("Circuit breaker: HALF_OPEN -> CLOSED")
        elif self.state == "CLOSED":
            self.failure_count = 0
    
    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = datetime.utcnow()
        
        if self.state == "HALF_OPEN":
            self.state = "OPEN"
            log.warning("Circuit breaker: HALF_OPEN -> OPEN (test call failed)")
        elif self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            log.warning("Circuit breaker: CLOSED -> OPEN",
                        failures=self.failure_count)
            # Alert
```

### Per-Broker Circuit Breakers

```python
# Each broker adapter has its own circuit breaker
circuit_breakers = {
    Market.NSE: CircuitBreaker(
        failure_threshold=5,
        recovery_timeout=60,    # 1 minute
        half_open_max_calls=1,
    ),
    Market.US: CircuitBreaker(
        failure_threshold=5,
        recovery_timeout=60,
        half_open_max_calls=1,
    ),
}

async def execute_order(order: ApprovedOrder) -> OrderResult:
    cb = circuit_breakers[order.market]
    
    if not cb.can_execute():
        log.warning("Circuit breaker OPEN, rejecting order",
                    market=order.market, order_id=order.order_id)
        return OrderResult(
            order_id=order.order_id,
            status=OrderStatus.FAILED,
            error_message=f"Circuit breaker OPEN for {order.market.value}",
            is_retryable=True,  # Can retry when circuit closes
        )
    
    try:
        result = await adapter.place_order(order)
        if result.status in (OrderStatus.PLACED, OrderStatus.FILLED):
            cb.record_success()
        elif result.status == OrderStatus.FAILED and result.is_retryable:
            cb.record_failure()
        return result
    except Exception as e:
        cb.record_failure()
        raise
```

---

## Error Recovery Matrix

| Error Scenario                       | Automatic Recovery                  | Manual Action Required    |
|--------------------------------------|--------------------------------------|---------------------------|
| WebSocket disconnect (< 5 retries)   | Reconnect with backoff              | None                      |
| WebSocket disconnect (exhausted)     | Activate kill switch                | Investigate, deactivate   |
| Broker API timeout (single order)    | Retry up to 3 times                | None                      |
| Broker API down (circuit breaker)    | Stop sending orders, test recovery  | Monitor, may need broker  |
| ECS task crash                       | ECS auto-restarts, state in DynamoDB| None (unless repeated)    |
| DynamoDB throttling                  | On-demand auto-scales               | None                      |
| Daily drawdown exceeded              | Kill switch activates               | Review, deactivate next day|
| Position mismatch                    | Alert sent, no auto-correct         | Manual reconciliation     |
| Stale market data (> 5s old)         | Alert sent                          | Investigate data feed     |
| Strategy exception                   | Log error, skip tick, continue      | Review logs, fix strategy |
| ML model load failure                | Fall back to non-ML signals         | Redeploy model to S3     |
| Invalid broker credentials           | All orders fail, alert sent         | Rotate credentials        |

---

## Trading Day Checklist (Automated)

The system performs these checks automatically at each market open:

```
PRE-MARKET CHECKS (30 min before open):
  [x] ECS services are running (all 5 tasks healthy)
  [x] DynamoDB tables accessible (read/write test)
  [x] S3 buckets accessible (list/write test)
  [x] Broker credentials valid (test API call)
  [x] WebSocket connection established
  [x] Kill switch state loaded (is it off?)
  [x] Risk config loaded (are limits reasonable?)
  [x] Strategies loaded and configured
  [x] ML models loaded (or fallback available)
  [x] Daily PnL counters reset (if new day)
  [x] Previous day's positions reconciled

IF ANY CHECK FAILS:
  -> Do NOT start trading
  -> Send alert
  -> Kill switch remains/becomes active
  -> Human must investigate and resolve
```
