# QuantEmbrace — Zerodha Full-Capacity Rate Limit Design
## Using All 10 req/sec Intelligently

**Version:** 1.1
**Date:** 2026-04-30
**Approved:** 2026-05-01
**Status:** APPROVED — implementation in progress (Gate 1 + Gate 2 active)
**Author:** Hari

---

## The Core Problem with the Current Design

### 1. Semaphore ≠ Rate Limiter

The current code has:
```python
self._nse_semaphore = asyncio.Semaphore(8)
```

This is documented as enforcing the "10 req/sec limit" — but it does not.

A semaphore limits **concurrency**, not **rate**. If 8 concurrent requests each complete in 50ms,
you are making 160 req/sec. If they take 500ms, you make 16 req/sec. Neither is 10 req/sec.

The current design has no true rate limiter. At burst conditions (market open + multiple fills)
it can silently exceed the Zerodha limit, triggering 429 errors and circuit breaker activation.

### 2. O(N) Fill Polling Instead of O(1)

The current fill poller calls:
```python
# For EACH open order — N API calls per 300ms cycle:
await self._zerodha.get_order_status(order.broker_order_id)  # → kite.order_history(id=X)
```

But the Zerodha Kite API has `kite.orders()` — a single call that returns **all orders** at once.

| Approach | Open Orders | API Calls / Cycle | Calls / Second (300ms) |
|----------|-------------|-------------------|------------------------|
| Current (per-order) | 1 | 1 | 3.3 |
| Current (per-order) | 5 | 5 | 16.7 ← **exceeds limit** |
| Current (per-order) | 10 | 10 | 33.3 ← **severe violation** |
| Bulk `kite.orders()` | ANY | 1 | 3.3 max |

The fix: replace per-order polling with a single `kite.orders()` call. This reduces fill
detection to **1 API call per cycle** regardless of how many orders are open.

### 3. Under-utilization at Normal Trading Hours

Current average utilization of the 10 req/sec budget:

| Operation | Frequency | req/sec |
|-----------|-----------|---------|
| Fill polling (per-order, 3 orders avg) | 300ms | ~10 req/s (sometimes violates!) |
| Margin refresh | 5s | 0.2 |
| Order placement | 5-20/day | ~0.001 |
| **Everything else** | NEVER | 0 |

The system simultaneously **over-uses** (fill polling) and **under-uses** (everything else).

With the bulk order fix, fill polling drops to **2 req/s**, freeing 8 req/s for:
- Live bid/ask quotes for all instruments (1 call covers 50+ instruments)
- Real-time position monitoring (1 call covers all positions)
- Intraday candle streaming (for faster strategies)
- Pre-market historical data pre-fetch

---

## Architecture: Token Bucket Rate Limiter with Priority Budget

### The Rate Limiter

Replace `asyncio.Semaphore(8)` with a **token bucket rate limiter** with priority queues.

```
Token Bucket:
  Capacity:    10 tokens  (hard ceiling — Zerodha's published limit)
  Burst cap:   15 tokens  (absorb instantaneous spikes, e.g., MIS square-off)
  Refill rate: 10 tokens/second
  
  If no token available: request waits in priority queue (never dropped)
  If token available:    request executes immediately

Priority tiers (CRITICAL always gets next available token):
  CRITICAL  Emergency cancel, stop-loss protection, kill-switch force-cancel
  HIGH      place_order, bulk_fill_poll (kite.orders()), cancel_order
  MEDIUM    get_positions, get_margins, get_quotes (batch)
  LOW       get_historical (candles), get_instruments, analytics queries
```

### Budget Allocation by Market Phase

The system detects market phase from IST time and dynamically adjusts
how aggressively each operation category competes for tokens.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                  ZERODHA 10 req/sec BUDGET BY MARKET PHASE                  │
│                         (requests per second)                               │
├─────────────────────┬───────────┬─────────────┬────────┬───────────┬───────┤
│ Operation           │ PRE_OPEN  │ MKT_OPEN    │ NORMAL │ PRE_CLOSE │ POST  │
│                     │ 08-09 IST │ 09:15-09:30 │        │ 14:45-15:20│      │
├─────────────────────┼───────────┼─────────────┼────────┼───────────┼───────┤
│ place_order         │     0     │      4      │   2    │     3     │   0   │
│ bulk_fill_poll      │     0     │      3      │   2    │   3       │   1   │
│ get_positions       │     0     │      1      │   1    │     2     │   1   │
│ get_margins         │     0     │      1      │   1    │     1     │   0   │
│ get_quotes (batch)  │     0     │      0      │   2    │     0     │   0   │
│ candle_stream       │     3     │      0      │   1    │     0     │   3   │
│ reconcile/audit     │     2     │      0      │   0    │     0     │   2   │
│ RESERVE             │     5     │      1      │   1    │     1     │   3   │
├─────────────────────┼───────────┼─────────────┼────────┼───────────┼───────┤
│ TOTAL               │    10     │     10      │  10    │    10     │  10   │
└─────────────────────┴───────────┴─────────────┴────────┴───────────┴───────┘
```

**Why RESERVE matters:** CRITICAL-priority calls (emergency cancel, kill switch) always preempt.
The reserve budget ensures they never wait, regardless of what other operations are running.

---

## Service Architecture: 4 New Polling Services

### Service 1: `BulkOrderPoller` (replaces `ZerodhaFillPoller`)

**Current:** `kite.order_history(order_id=X)` per order → O(N) calls
**New:** `kite.orders()` once → O(1) call, parses all orders

```
Every 500ms (2 req/s — down from 300ms × N orders):
  1. kite.orders()                    ← 1 API call, returns ALL orders
  2. For each order in response:
       if status == COMPLETE and order.id in TRACKED_ORDERS:
         compute fill_id = sha256(broker_order_id|qty|price)[:16]
         DynamoDB conditional write (idempotency gate)
         → update order status → update positions → publish orders.events
       if status == CANCELLED or REJECTED:
         update order status → publish orders.events
  3. Commit Kafka offset (if Kafka enabled)

Detection latency: average 250ms (midpoint of 500ms cycle)
Rate cost:         2 req/s fixed (was up to 33 req/s with 10 orders!)
```

Why 500ms instead of 100ms? With 2 req/s from a single bulk call, we have 8 req/s remaining
for order placement, quotes, positions, and candles. Going faster (e.g. 100ms = 10 req/s just
for polling) leaves nothing for the rest of the system.

**Adaptive interval:** When ≥3 PLACED orders exist (active trading period), poll every 300ms
(3.3 req/s). During quiet periods (0-1 open orders), poll every 1000ms (1 req/s). This
dynamically shifts budget to where it matters.

```
PLACED orders → poll_interval
    0 open   → 2000ms (0.5 req/s — minimal, just checking)
    1-2 open → 1000ms (1 req/s)
    3-5 open → 500ms  (2 req/s)
    6+ open  → 300ms  (3.3 req/s)
```

### Service 2: `PositionMonitor` (new)

```
Every 1000ms in NORMAL phase (1 req/s):
  kite.positions()                     ← 1 API call, returns ALL positions
  
  For each position:
    Update DynamoDB positions table if quantity differs
    Compute unrealized P&L from LTP
    Write position snapshot to risk-state table
  
  CloudWatch metric: OPEN_POSITION_COUNT, TOTAL_EXPOSURE_INR
```

**Why this matters:** Currently positions are only updated on fills. If the fill poller
misses a fill (e.g., broker-side partial), the position state is wrong until the next
startup reconciliation. `PositionMonitor` provides ground-truth position state every 1 second.

It also catches:
- Zerodha-side auto-square-offs (MIS positions Zerodha closes without our action)
- Manual orders placed outside the system (you placed an order from phone)
- Partial fills not tracked by the poller

### Service 3: `LiveQuotePoller` (new)

```
Every 2000ms in NORMAL phase (0.5 req/s):
  kite.quote(["NSE:RELIANCE", "NSE:INFY", ..., all 50 instruments])  ← 1 API call
  
  Returns for each instrument: LTP, bid, ask, depth, volume, circuit_limits

  Outputs:
    → Strategy engine: spread width check (don't enter if spread > 0.5%)
    → Risk engine: real-time slippage estimate before approval
    → Execution engine: re-price limit orders if LTP moved after signal
    → CloudWatch metric: QUOTE_SPREAD_BPS per instrument
```

**Why `kite.quote()` is powerful:** A single API call returns data for up to 500 instruments.
At 0.5 req/s, you have live bid/ask for your entire watchlist. This enables:

1. **Pre-order spread check**: Risk engine rejects signals when spread > configured threshold
2. **Dynamic limit price**: If LTP moved 0.5% since signal, adjust limit price before placing
3. **Circuit breaker detection**: If circuit_limits shows instrument is circuit-locked, skip

### Service 4: `Intraday CandleStream` (new)

Uses the **separate 3 req/sec historical data API** (does not count toward order API limit):

```
Every 333ms (3 req/s — uses the SEPARATE historical data budget):
  Round-robin through active instruments:
    kite.historical_data(
      instrument_token=next_instrument,
      from_date=now - 5m,
      to_date=now,
      interval="minute"
    )
  
  Returns last 5 completed 1-minute candles for that instrument.
  
  At 3 req/s over 50 instruments: each instrument updated every ~17 seconds.
  During PRE_OPEN: 3 req/s × 60 min = 180 candle fetches = 3.6 candles/instrument × 50 instruments.
```

**Why this matters:** Strategy engine currently relies on WebSocket tick aggregation to build
candles. WebSocket ticks can have gaps (reconnect). `CandleStream` provides confirmed,
exchange-validated candle data as a primary data source and gap-fill fallback.

---

## New Signal Universe (Enabled by Full Rate Capacity)

### Current Signal Profile
- Strategy: 5m/15m momentum crossover
- Signals/day: 5-30
- Holding period: 5-60 minutes
- Instruments: 25 active in instruments.yaml

### Expanded Signal Universe

With live quotes, position monitoring, and intraday candles:

**1. Opening Range Breakout (ORB)**
```
Window:    09:15 – 09:30 IST (first 15 minutes)
Logic:     Record high/low of first 15-min candle per instrument
           On breakout above high → BUY signal
           On breakout below low → SELL signal
Data:      CandleStream provides confirmed 1m candles for range computation
Signals:   2-8 per day across watchlist (high conviction, range is tight at open)
Requires:  CandleStream + faster fill detection (≤500ms) for tight entry
```

**2. Scalp Momentum (1m)**
```
Window:    09:30 – 11:00 IST (high-volume morning session)
Logic:     1m EMA(9)/EMA(21) crossover + volume confirmation
           Only trade when bid-ask spread < 0.3% (LiveQuotePoller check)
Data:      CandleStream (1m candles) + LiveQuotePoller (spread gate)
Signals:   5-15 per day across watchlist
Hold time: 3-10 minutes
Requires:  Live quote spread check to avoid wide-spread entries
```

**3. VWAP Reversion**
```
Window:    09:30 – 14:00 IST
Logic:     Price diverges >1.5% from session VWAP → fade trade (reversion)
           Built from tick data (WebSocket) + confirmed by 5m candle
Data:      VWAP computed from WebSocket ticks (no API cost)
           PositionMonitor confirms position not already on
Signals:   3-8 per day per instrument
Requires:  PositionMonitor (to avoid double-entering a reversion trade)
```

**4. Intraday Trend Following (15m)**
```
Logic:     Existing momentum strategy (unchanged)
Data:      CandleStream (15m interval) instead of tick-aggregated candles
Improvement: CandleStream uses exchange-validated candles → no gaps, no reconnect noise
```

**5. Pre-Close Momentum (14:45 – 15:15)**
```
Window:    Last 30 minutes before MIS auto-square-off
Logic:     Strong directional move in last 30 minutes → short-term momentum
           ONLY if existing position in same direction (pyramid)
Gate:      PositionMonitor must confirm direction first
Risk:      Tight stop, small size — auto-square-off is hard at 15:15
```

### Signal Routing by Data Dependency

```
                        WebSocket ticks (free, no rate limit)
                              │
                    ┌─────────┴──────────┐
                    ▼                    ▼
              Tick-based             Candle-based
              signals                signals
               │                      │
               │ VWAP Reversion        │ ORB, Scalp, VWAP
               │ (built from ticks)   │ (built from CandleStream)
               │                      │
               └──────────────────────┘
                              │
                     signals.pending (Kafka)
                              │
                         risk_engine
                         + LiveQuotePoller
                           ↓ spread gate
                     signals.approved
                              │
                      execution_engine
                         + BulkOrderPoller
                           ↓ fill detection ≤500ms
                      orders.events
```

---

## Scripts Design

### Script 1: `scripts/zerodha/rate_monitor.py`

Real-time terminal dashboard showing rate limit utilization.

```
┌─────────────────────────────────────────────────────────────┐
│            ZERODHA RATE MONITOR — 14:23:45 IST              │
│                   Market Phase: NORMAL                       │
├─────────────────────────────────────────────────────────────┤
│ Budget Used:  ████████░░  7.8 / 10.0 req/sec               │
│                                                             │
│ By Category:                                                │
│   place_order        ██░░░░░░░░  2.1 req/s  (budget: 2)    │
│   bulk_fill_poll     ██░░░░░░░░  1.5 req/s  (budget: 2)    │
│   get_positions      █░░░░░░░░░  1.0 req/s  (budget: 1)    │
│   get_margins        █░░░░░░░░░  0.9 req/s  (budget: 1)    │
│   get_quotes         ██░░░░░░░░  1.8 req/s  (budget: 2)    │
│   candle_stream      █░░░░░░░░░  0.5 req/s  (budget: 1)    │
│                                                             │
│ 429 Errors Today:    0                                      │
│ Peak This Session:   9.2 req/s (09:17:33 IST)              │
│ Available Tokens:    2.2 (burst pool: 4.1)                  │
├─────────────────────────────────────────────────────────────┤
│ Open Orders (tracked): 2  │  Fill polls today: 847          │
│ Avg fill detect time:  380ms  │  Fastest: 112ms             │
└─────────────────────────────────────────────────────────────┘
```

**Inputs:** Reads metrics from CloudWatch `QuantEmbrace/ZerodhaRateLimit` namespace
**Outputs:** Terminal display, optional CSV export
**Usage:** `python scripts/zerodha/rate_monitor.py --interval 1` (refresh every 1s)

---

### Script 2: `scripts/zerodha/candle_prefetch.py`

Pre-market historical data download (runs 08:00–09:00 IST at 3 req/s).

```
Fetches per instrument (at 3 req/s historical API limit):
  - 90 days of daily candles         → strategy engine backtesting + indicator seeds
  - 60 days of 1-hour candles        → intraday trend context
  - Today's 1-min candles (so far)   → CandleStream warm-up

Run schedule: 08:00 IST daily (before market open)
Rate: 3 req/s × 60 minutes × 60 seconds = 10,800 candle requests
      50 instruments × 3 intervals = 150 fetch tasks → completes in ~50 seconds

Output: S3 bucket s3://quantembrace-market-data/candles/{date}/{symbol}/
Format: Parquet, partitioned by date and symbol
```

**Usage:**
```bash
python scripts/zerodha/candle_prefetch.py --date today --instruments all
python scripts/zerodha/candle_prefetch.py --date 2026-04-30 --instruments NSE:RELIANCE,NSE:INFY
```

**Why this matters:** Strategy engine starts with warm candle history instead of waiting for
live candles to accumulate. Opening range breakout strategy needs 15m of pre-computed history
before 09:15 AM — this script provides it.

---

### Script 3: `scripts/zerodha/position_audit.py`

Compares DynamoDB position state with Zerodha broker state.

```
Calls (3 API calls total):
  1. kite.positions()        → live positions from broker
  2. kite.holdings()         → long-term holdings (CNC positions)
  3. kite.orders()           → today's order history

Compares with:
  DynamoDB quantembrace-positions table
  DynamoDB quantembrace-orders table

Reports:
  MATCH:    DynamoDB matches broker — system state is clean
  DRIFT:    DynamoDB has position, broker does not (or vice versa) → ACTION REQUIRED
  ORPHAN:   Order in broker not in DynamoDB (manual order?) → WARN
```

**Usage:**
```bash
python scripts/zerodha/position_audit.py              # full audit
python scripts/zerodha/position_audit.py --fix        # auto-sync DynamoDB to broker state
python scripts/zerodha/position_audit.py --symbol RELIANCE  # single instrument
```

**Run:** Every morning before market open AND after any service restart.

---

### Script 4: `scripts/zerodha/budget_optimizer.py`

Analyzes 7 days of `ZerodhaAPICallsPerSecond` CloudWatch metrics and outputs
an optimized budget allocation for the current trading style.

```
Reads: CloudWatch metric data for past 7 trading days
Analyzes:
  - Which operations actually consumed budget and when
  - Where 429 errors occurred (if any)
  - Idle budget windows (reserved but unused)
  - Peak concurrent operations

Outputs:
  Current allocation vs actual usage:
    place_order:   budget 2.0 req/s, actual 0.8 req/s → over-allocated
    bulk_fill_poll: budget 2.0 req/s, actual 1.8 req/s → correct
    get_quotes:    budget 2.0 req/s, actual 0.1 req/s → severely over-allocated

  Suggested reallocation:
    place_order    2.0 → 1.0 (save 1 req/s)
    get_quotes     2.0 → 0.5 (save 1.5 req/s)
    candle_stream  1.0 → 3.0 (use freed 2.5 req/s for more candle data)
```

**Usage:**
```bash
python scripts/zerodha/budget_optimizer.py --days 7
python scripts/zerodha/budget_optimizer.py --apply   # writes optimized config to settings
```

---

### Script 5: `scripts/zerodha/stress_test.py`

Tests rate limiter behavior under simulated burst conditions (uses paper trading mode).

```
Test cases:
  1. Normal load:          8 concurrent operations, verify no 429
  2. Burst (market open):  20 requests in 1 second, verify priority queue works
  3. CRITICAL preemption:  Fill CRITICAL task queue with LOW tasks, fire CRITICAL → must execute immediately
  4. Budget phase switch:  Simulate 09:15 IST market open → verify budget reallocation
  5. 429 recovery:         Mock 429 response → verify circuit breaker behavior

Output: pass/fail per test case, timing data
```

**Usage:** `python scripts/zerodha/stress_test.py --env staging`

---

## Infrastructure Changes

### 1. New Python Module: `services/shared/zerodha/rate_limiter.py`

Replaces `asyncio.Semaphore(8)` in execution engine.

**Interface:**
```python
class ZerodhaRateLimiter:
    """
    Token bucket rate limiter for Zerodha 10 req/sec API limit.
    4 priority tiers, market-phase-aware budget allocation.
    """
    def __init__(self, capacity: int = 10, burst: int = 15): ...

    async def acquire(self, priority: Priority = Priority.HIGH) -> None:
        """Block until a token is available. Never drops requests."""
        ...

    def set_market_phase(self, phase: MarketPhase) -> None:
        """Called by MarketPhaseGovernor when phase changes."""
        ...

    # Metrics
    def get_utilization(self) -> dict[str, float]: ...
    def get_token_count(self) -> float: ...

class Priority(IntEnum):
    CRITICAL = 0    # emergency cancel, kill switch force-cancel
    HIGH     = 1    # place_order, bulk_fill_poll, cancel_order
    MEDIUM   = 2    # positions, margins, quotes
    LOW      = 3    # historical data, analytics

class MarketPhase(str, Enum):
    PRE_OPEN      = "PRE_OPEN"       # 08:00 – 09:00 IST
    PRE_AUCTION   = "PRE_AUCTION"    # 09:00 – 09:15 IST
    MARKET_OPEN   = "MARKET_OPEN"    # 09:15 – 09:30 IST (first 15 min burst)
    NORMAL        = "NORMAL"         # 09:30 – 14:45 IST
    PRE_CLOSE     = "PRE_CLOSE"      # 14:45 – 15:20 IST (MIS square-off)
    CLOSING       = "CLOSING"        # 15:20 – 15:30 IST
    POST_CLOSE    = "POST_CLOSE"     # 15:30+ IST
```

### 2. New Python Module: `services/shared/zerodha/market_phase.py`

```python
class MarketPhaseGovernor:
    """
    Watches IST clock, broadcasts MarketPhase transitions.
    All polling intervals and budget allocations react to phase changes.
    """
    PHASE_SCHEDULE = {
        time(8, 0):  MarketPhase.PRE_OPEN,
        time(9, 0):  MarketPhase.PRE_AUCTION,
        time(9, 15): MarketPhase.MARKET_OPEN,
        time(9, 30): MarketPhase.NORMAL,
        time(14, 45): MarketPhase.PRE_CLOSE,
        time(15, 20): MarketPhase.CLOSING,
        time(15, 30): MarketPhase.POST_CLOSE,
    }
    async def run(self) -> None: ...  # publishes phase changes to internal event bus
    def current_phase(self) -> MarketPhase: ...
```

### 3. New Python Module: `services/execution_engine/polling/bulk_order_poller.py`

Replaces `fill_poller.py`'s per-order loop with a single `kite.orders()` call.

**Key change vs current fill_poller.py:**
```python
# CURRENT (O(N) calls — problematic):
for order in open_orders:
    status = await zerodha.get_order_status(order.broker_order_id)

# NEW (O(1) call — correct):
all_orders = await zerodha.get_all_orders()   # kite.orders() — 1 API call
for broker_order in all_orders:
    if broker_order.order_id in self._tracked_ids:
        await self._process_status_change(broker_order)
```

**Adaptive polling interval** (responds to number of open orders and market phase):
```python
def _get_poll_interval(self, open_order_count: int, phase: MarketPhase) -> float:
    if phase == MarketPhase.PRE_CLOSE:
        return 0.3  # 300ms — aggressive during MIS window
    if open_order_count == 0:
        return 2.0  # 2s — no open orders, minimal polling
    if open_order_count <= 2:
        return 1.0  # 1s — light activity
    if open_order_count <= 5:
        return 0.5  # 500ms — normal activity
    return 0.3      # 300ms — heavy activity
```

### 4. New Python Module: `services/execution_engine/polling/position_monitor.py`

```python
class PositionMonitor:
    """
    Ground-truth position state via kite.positions() every 1 second.
    Catches fills missed by BulkOrderPoller, manual orders, broker auto-square-offs.
    """
    POLL_INTERVAL = {
        MarketPhase.MARKET_OPEN: 1.0,   # 1s — aggressive
        MarketPhase.NORMAL:      2.0,   # 2s — balanced
        MarketPhase.PRE_CLOSE:   1.0,   # 1s — watch for auto-square-offs
        MarketPhase.POST_CLOSE:  10.0,  # 10s — end of day check only
    }
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

### 5. New Python Module: `services/execution_engine/polling/live_quote_poller.py`

```python
class LiveQuotePoller:
    """
    Batch quote fetch via kite.quote([all_instruments]) every 2 seconds.
    Single API call covers all 50+ instruments in the watchlist.

    Outputs:
      → spread_validator: bid-ask spread per instrument (reject wide-spread orders)
      → execution_engine: re-price limit orders on LTP drift
      → risk_engine: real-time notional value calculation
    """
    POLL_INTERVAL = {
        MarketPhase.NORMAL:     2.0,   # 0.5 req/s
        MarketPhase.MARKET_OPEN: None, # disabled — budget reserved for orders/fills
        MarketPhase.PRE_CLOSE:  None,  # disabled — budget reserved for MIS
    }
```

### 6. New Python Module: `services/data_ingestion/candle_stream.py`

Uses the **separate 3 req/sec historical data API** (does not share budget with order APIs).

```python
class IntradayCandleStream:
    """
    Streams 1-minute candles from kite.historical_data() at 3 req/sec.
    Round-robins through instruments so each instrument gets a fresh
    candle approximately every 17 seconds (50 instruments / 3 req/s).

    Publishes confirmed candles to Kafka `candles.1m` topic (Phase 2).
    Until Kafka: writes directly to DynamoDB candle cache.
    """
    HISTORICAL_API_RATE = 3  # req/sec (SEPARATE from 10 req/sec order limit)

    def _build_priority_queue(self) -> list[str]:
        """
        Prioritize instruments: ones with open positions and active signals get
        candles more frequently than quiet instruments.
        """
```

### 7. Zerodha Broker Client: New Methods Required

Add to `ZerodhaBrokerClient`:

```python
async def get_all_orders(self) -> list[dict]:
    """kite.orders() — returns ALL today's orders in one API call."""
    ...

async def get_batch_quotes(self, instruments: list[str]) -> dict[str, QuoteData]:
    """kite.quote([...]) — batch quote for multiple instruments, one API call."""
    ...

async def get_historical_candles(
    self, instrument_token: int, from_dt: datetime, to_dt: datetime, interval: str
) -> list[CandleData]:
    """kite.historical_data() — uses separate 3 req/s limit."""
    ...
```

### 8. CloudWatch Metrics: Rate Limit Observability

New custom metrics namespace: `QuantEmbrace/ZerodhaRateLimit`

```
ZerodhaAPICallsPerSecond        dimension: Category (place_order, fill_poll, etc.)
ZerodhaTokenBucketLevel         current token count
ZerodhaRateLimitErrors          count of 429 responses
ZerodhaFillDetectionLatencyMs   time from order placed to fill detected
ZerodhaOpenOrderCount           orders currently in PLACED state
ZerodhaQuoteSpreadBps           dimension: InstrumentId (spread monitoring)
```

**CloudWatch Alarms:**
```
ALARM: ZerodhaRateLimitErrors > 0 for 60s
  Action: SNS P0 alert — 429s mean the rate limiter is misconfigured

ALARM: ZerodhaTokenBucketLevel < 1.0 for 30s
  Action: SNS P1 alert — running at capacity, risk of degradation

ALARM: ZerodhaFillDetectionLatencyMs P95 > 1000ms for 120s
  Action: SNS P1 alert — fill detection degraded
```

### 9. Settings Extension

```python
# services/shared/config/settings.py — new ZerodhaRateLimitConfig:

class ZerodhaRateLimitConfig(BaseSettings):
    model_config = {"env_prefix": "ZERODHA_RATELIMIT_"}

    capacity_per_second: int = Field(default=10)          # Zerodha's hard limit
    burst_capacity: int = Field(default=15)               # token burst ceiling
    fill_poll_max_interval_ms: int = Field(default=2000)  # slowest poll when idle
    fill_poll_min_interval_ms: int = Field(default=300)   # fastest poll (MIS window)
    quote_poll_interval_ms: int = Field(default=2000)     # live quote refresh
    position_poll_interval_ms: int = Field(default=2000)  # position monitor refresh
    margin_poll_interval_ms: int = Field(default=1000)    # margin monitor (was 5000)
    candle_stream_enabled: bool = Field(default=True)
    live_quote_enabled: bool = Field(default=True)
    position_monitor_enabled: bool = Field(default=True)
```

---

## Implementation Task Breakdown

| Task | Name | Description | Priority | Deps | New Files |
|------|------|-------------|----------|------|-----------|
| RT-T01 | Token Bucket Rate Limiter | Replace Semaphore(8) with ZerodhaRateLimiter | P0 | — | `shared/zerodha/rate_limiter.py` |
| RT-T02 | Market Phase Governor | IST-aware phase detection + budget switching | P0 | T01 | `shared/zerodha/market_phase.py` |
| RT-T03 | Bulk Order Poller | Replace per-order polling with kite.orders() | P0 | T01,T02 | `polling/bulk_order_poller.py` |
| RT-T04 | Position Monitor | kite.positions() every 1-2s, ground-truth state | P1 | T01,T02 | `polling/position_monitor.py` |
| RT-T05 | Live Quote Poller | kite.quote(batch) every 2s, spread gate | P1 | T01,T02 | `polling/live_quote_poller.py` |
| RT-T06 | Intraday Candle Stream | kite.historical_data() at 3 req/s (separate limit) | P1 | T02 | `data_ingestion/candle_stream.py` |
| RT-T07 | Broker Client Extension | Add get_all_orders, get_batch_quotes, get_historical_candles | P0 | — | `brokers/zerodha_broker.py` (modified) |
| RT-T08 | New Signal Types | ORB, Scalp 1m, Spread-gated signals | P2 | T05,T06 | `strategy_engine/strategies/` |
| RT-T09 | CloudWatch Metrics | ZerodhaRateLimit namespace + alarms + dashboard | P1 | T01–T05 | `infra/terraform/modules/monitoring/` |
| RT-T10 | Scripts Suite | rate_monitor, candle_prefetch, position_audit, budget_optimizer, stress_test | P1 | T01–T06 | `scripts/zerodha/` |

---

## Approval Gates

```
GATE 1 — Core Rate Limiter (approve before any code):
  Approve RT-T01 (rate limiter design) + RT-T02 (phase governor)
  These are the foundation. Everything else depends on them.

GATE 2 — Fill Detection (approve before replacing current fill_poller):
  Approve RT-T03 (bulk order poller)
  Current fill_poller.py must be kept running in parallel until RT-T03 is validated.
  Cutover: when RT-T03 has processed 5 consecutive trading days without missed fills.

GATE 3 — New Data Feeds (approve before enabling in production):
  Approve RT-T04 (position monitor) + RT-T05 (live quote) + RT-T06 (candle stream)
  Enable one at a time. Verify rate budget isn't exceeded before enabling next.

GATE 4 — New Signals (approve last — after all data feeds stable):
  Approve RT-T08 (new signal types)
  Paper trade for 5 days before live capital.
```

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Token bucket underestimates Zerodha burst behavior | Low | HIGH | Burst capacity set to 15 (50% above limit). 429 alarm fires before circuit breaker. |
| BulkOrderPoller misses a fill (if kite.orders() has delay) | Low | MEDIUM | DynamoDB conditional write idempotency gate. Position audit script catches it. |
| CandleStream historical API hits 3 req/s limit | Low | LOW | Separate rate limiter for historical API. Does not share budget with order APIs. |
| LiveQuotePoller freezes during market open burst | Design | LOW | Disabled in MARKET_OPEN phase (see budget table row). Budget reserved for orders. |
| PositionMonitor detects external manual trade | Expected | LOW | WARN log + CloudWatch metric, not kill switch. Operator reviews end of day. |
| MarketPhase clock drift (IST timezone handling) | Medium | MEDIUM | Phase governor uses `pytz.timezone("Asia/Kolkata")`. Unit test covers DST boundary. |

---

## What Does NOT Change

| Component | Reason |
|-----------|--------|
| `ZerodhaTokenManager` (auth) | Unchanged — daily token lifecycle is correct |
| Kill switch logic | Unchanged — emergency cancel is CRITICAL priority, always preempts |
| `OrderManager` (DynamoDB) | Unchanged — state management is correct |
| DynamoDB idempotency gates | Unchanged — fill dedup gates remain |
| MIS square-off manager | Gets higher-priority budget in PRE_CLOSE phase |
| Signal schemas | Unchanged — new signal types use same schema, different strategy_id |
| Risk engine validators | Gets live spread from LiveQuotePoller as new input to slippage_validator |

---

## Implementation Status

| Task | File | Status |
|------|------|--------|
| RT-T01 | `services/shared/zerodha/rate_limiter.py` | ✅ DONE 2026-05-01 |
| RT-T02 | `services/shared/zerodha/market_phase.py` | ✅ DONE 2026-05-01 |
| RT-T03 | `services/execution_engine/polling/bulk_order_poller.py` | ✅ DONE 2026-05-01 |
| RT-T04 | `services/execution_engine/polling/position_monitor.py` | ⏳ PENDING |
| RT-T05 | `services/execution_engine/polling/live_quote_poller.py` | ⏳ PENDING |
| RT-T06 | `services/data_ingestion/candle_stream.py` | ⏳ PENDING |
| RT-T07 | `services/execution_engine/brokers/zerodha_broker.py` | ✅ DONE 2026-05-01 |
| RT-T08 | `services/strategy_engine/strategies/` (ORB, Scalp, VWAP) | ⏳ PENDING GATE 4 |
| RT-T09 | `infra/terraform/modules/monitoring/main.tf` | ⏳ PENDING |
| RT-T10 | `scripts/zerodha/` (5 scripts) | ⏳ PENDING |

*Gate 1 (RT-T01, RT-T02, RT-T07) + Gate 2 (RT-T03): ACTIVE*
*Gate 3 (RT-T04, RT-T05, RT-T06, RT-T09, RT-T10): awaiting Gate 2 validation (5 trading days)*
*Gate 4 (RT-T08): awaiting Gate 3 stability*
