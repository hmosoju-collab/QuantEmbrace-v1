# QuantEmbrace Phase 3 — Decouple Strategy & Scale Horizontally
## Architecture Design Review — PENDING FINAL APPROVAL

**Version**: 1.2 — APPROVED
**Date**: 2026-05-05
**Owner**: Hari
**Status**: ✅ APPROVED — implementation in progress
**Prerequisite**: Phase 2 complete ✅ (2026-05-04)

**Revision history:**
| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-05-04 | Initial draft |
| 1.1 | 2026-05-04 | Rate-limit conflict analysis; candle path changed to DynamoDB polling |
| 1.2 | 2026-05-05 | Hari review: DynamoDB cost corrected, paper_trade contract added, trace_id fixed, overlapping lookback, candle timestamp rule. Q1–Q5 closed. |

**All open questions are now closed.** Document is ready for final approval or change requests.

---

## 1. The Honest Reframe

The roadmap Phase 3 sketch targeted scale this system will never need.

Your actual profile: 10–50 NSE instruments, ≤ 15 US instruments, 5–30 signals per day,
one c6g.large at ~3% average CPU during market hours. Multi-instance Kafka partition
sharding at this load is architectural cosplay.

The real Phase 3 work is:

1. Five candle strategies produce zero signals today (they generate into a queue nobody reads).
2. One bad strategy can grind all strategies by blocking the processing loop.
3. Config changes require a full deploy.
4. The Kafka partition topology needs one-time future-proofing.

**Phase 3 correct definition:**

> Make each strategy an independent failure domain. Wire the candle strategies into
> production via the path that was already designed for them. Give operators
> live config control. Prepare the topology for future horizontal scale without
> building the machinery today.

---

## 2. What Phase 2 Left Incomplete

### The Processing Gap

`StrategyEngineService` runs one processing path:

```
Kafka ticks.nse / ticks.us  →  KafkaTickConsumer  →  MomentumStrategy  →  signals.pending
```

The five candle strategies exist in a disconnected path:

```
IntradayCandleStream  →  DynamoDB candle-cache   (written correctly)
                      →  CandleBarAdapter._signal_queue  (nobody drains this)
```

`CandleBarAdapter` is never instantiated in `StrategyEngineService`. Signals accumulate
and are never published to `signals.pending`. No kill-switch check. No state persistence.
No circuit isolation.

### The Pre-Designed Fix

`candle_stream.py` docstring says explicitly:
> *Candles are stored in DynamoDB `quantembrace-candle-cache` (TTL 2h). Strategy
> engine reads from this cache instead of tick aggregation.*

This was designed from the start. Phase 3 implements the second half: strategy_engine
polls that table and routes confirmed candles to the candle strategies.

---

## 3. Rate Limit Alignment

Phase 3 must not create a second Zerodha session from `strategy_engine`. The full
conflict analysis is in the v1.1 revision notes. Summary of why in-process
`IntradayCandleStream` in strategy_engine was rejected:

- `ZerodhaBrokerClient` contains `place_order()` / `cancel_order()` — giving
  strategy_engine an import path to those methods violates the service boundary rule
- `ZerodhaRateLimiter` is in-process; two processes = two token buckets, no coordination
- `MarketPhaseGovernor` enforces `candle_stream = 0` during MARKET_OPEN / PRE_CLOSE —
  that enforcement only works if the rate limiter and the candle stream share a process
- Candle API calls from strategy_engine would be invisible to `rate_monitor.py`

**Resolution:** `IntradayCandleStream` stays in `data_ingestion` untouched. Strategy_engine
reads confirmed candles from DynamoDB `candle-cache`. Zero Zerodha API calls from
strategy_engine process.

---

## 4. Design Goals

| # | Goal |
|---|------|
| G1 | Wire candle strategies via DynamoDB candle-cache polling — they produce zero signals today |
| G2 | Per-strategy isolation: circuit breaker + independent failure domains |
| G3 | Per-strategy hot-reload config (enable/disable, paper_trade) without restart |
| G4 | paper_trade pipeline: minimal Signal model + Risk + Execution changes for full-pipeline paper trading |
| G5 | Tick topic partition expansion 2 → 4 (one-time topology preparation) |

---

## 5. What Phase 3 Will NOT Build

| Out of Scope | Why Deferred |
|---|---|
| Multi-instance strategy engine | Zero operational need at 30 signals/day |
| Instrument-to-partition DynamoDB registry | Needed only for multi-instance |
| CPU / lag-based ASG scale-out policy | CPU is ~3%; policy would never fire |
| `candles.1m` Kafka topic | Deferred to Phase 5 Data Platform |
| Move `ZerodhaBrokerClient` to `shared/` | Correct long-term fix; Phase 5 scope |
| Schema Registry / Avro | JSON + `schema_version` is sufficient |
| Redis for strategy state | DynamoDB is fast enough for 6 strategies; Phase 4 |

---

## 6. Architecture: Strategy Isolation

### 6.1 The Problem Today

```python
# StrategyEngineService.process_tick() — today
for strategy in self._strategies:
    await strategy.on_tick(symbol, price, volume, timestamp)
    signal_out = await strategy.generate_signal()
```

An unhandled exception in any strategy propagates up, triggering a 1-second backoff
in `_kafka_processing_loop()`. Every strategy misses ticks for each failure. Repeated
failures in one strategy grind the whole service.

### 6.2 StrategyRunner: One Class, Two Interface Types (Q1 closed)

A single `StrategyRunner` wraps every strategy with `interface_type = TICK | CANDLE`.
The circuit breaker, enabled flag, metrics, state persistence, paper_trade flag, and
max_signals_per_day cap are identical regardless of interface type. Two separate
runner classes would duplicate all of that safety logic for no benefit.

```
StrategyEngineService
    ├── StrategyRunner(MomentumStrategy,         interface_type=TICK)
    ├── StrategyRunner(ORBStrategy,              interface_type=CANDLE)
    ├── StrategyRunner(Scalp1mStrategy,          interface_type=CANDLE)
    ├── StrategyRunner(VWAPReversionStrategy,    interface_type=CANDLE)
    ├── StrategyRunner(IntradayTrend15mStrategy, interface_type=CANDLE)
    └── StrategyRunner(PreCloseMomentumStrategy, interface_type=CANDLE)
```

### 6.3 Circuit Breaker: Dual-Threshold (Q2 closed)

Default thresholds, configurable per strategy via DynamoDB `strategy-config`:

```
OPEN if:
    consecutive_errors >= 5          (catches persistent failures)
    OR
    errors_in_last_5_minutes >= 10   (catches flaky strategies)
```

Consecutive-only misses a strategy that fails 9 times over 20 minutes but never
5 in a row. The rate threshold catches that pattern.

**State machine:**
```
  ┌──────────────┐  threshold   ┌──────────────┐
  │    CLOSED    │─────────────►│     OPEN     │
  │  (normal)    │              │  (disabled)  │
  └──────────────┘              └──────┬───────┘
         ▲                             │ 5 min
  3 ok   │                             ▼
  ┌──────┴───────┐              ┌──────────────┐
  │  HALF_OPEN   │◄─────────────│  (waiting)   │
  │  (1 test)    │              └──────────────┘
  └──────────────┘
```

**On OPEN:**
- CloudWatch `CircuitBreakerState = 1` emitted
- CloudWatch alarm → SNS → operator alert
- DynamoDB `strategy-config.circuit_breaker_opened_at = now()`
- Exception + stack trace logged to S3 `trading-logs/strategy-errors/`

**Manual reset:**
```bash
python scripts/strategy/reset_circuit_breaker.py --strategy ORBStrategy --env prod
# Sets circuit_breaker_reset=True in DynamoDB; picked up on next 60s config poll
```

---

## 7. Architecture: Candle Strategy Integration

### 7.1 Data Flow (DynamoDB Path)

```
data_ingestion service  (UNCHANGED)
    IntradayCandleStream
        rate-limited: 3 req/sec historical API (_historical_rate_limiter in ZerodhaBrokerClient)
        phase-gated:  candle_stream=0 during MARKET_OPEN and PRE_CLOSE (MarketPhaseGovernor)
        │
        ▼
    DynamoDB: quantembrace-candle-cache
        key:  {market}#{instrument}#{interval}#{candle_open_time_iso}
        TTL:  2 hours

strategy_engine service  (Phase 3 addition)
    _candle_processing_loop()  every 500ms
        │
        ▼
    DynamoCandleConsumer.poll_new_candles()
        overlapping lookback: query items where candle_open_time >= (now - 3 minutes)
        deduplicate by candle key (in-memory set, 5-minute rolling window)
        │
        for each new candle
        ▼
    StrategyRunner.dispatch_bar(bar)
        1. kill_switch_check  (DynamoDB 1s TTL cache)
        2. IST phase check    (skip during MARKET_OPEN / PRE_CLOSE)
        3. circuit_breaker check
        4. strategy.on_bar(bar)
        5. strategy.generate_signal()
        │
        ▼
    KafkaSignalPublisher.publish(signal) → signals.pending
```

### 7.2 Overlapping Lookback (Fix applied)

`updated_at > last_poll_time` can miss items that landed just after the previous
poll cutoff due to DynamoDB eventual consistency. Instead, query the last 3 minutes
on every poll:

```python
lookback_start = now - timedelta(minutes=3)
# Query candle-cache: candle_open_time >= lookback_start
# Deduplicate with in-memory set: {market}#{instrument}#{interval}#{candle_open_time}
# Only dispatch candles NOT already in the dedup set
# Dedup set evicts entries older than 5 minutes (rolling window)
```

This means every candle is seen by at most 6 consecutive polls before falling
out of the 3-minute lookback window, ensuring eventual-consistency stragglers
are always caught.

### 7.3 Candle Signal Timestamp Rule (Fix applied)

`signal.generated_at` MUST be set to the **candle's close time**, not `datetime.utcnow()`
at polling time. Replay-stable `signal_id` depends on this:

```python
# For a 1-minute candle that opened at 09:15:00:
candle_close_time = candle.dt + timedelta(minutes=1)  # 09:16:00

signal = Signal(
    ...
    generated_at = candle_close_time,   # NOT datetime.utcnow()
)
```

**Why this matters for replay stability:** `signal_id` is computed from `generated_at`
(via `_make_deterministic_signal_id` in `kafka_signal_publisher.py`). If polling time
is used, the same candle polled in two different polling cycles produces two different
`signal_id` values — defeating DynamoDB deduplication on restart. Using candle close
time makes `signal_id` identical regardless of when the candle is polled.

For 5m and 15m candles:
```python
interval_minutes = {"1min": 1, "5min": 5, "15min": 15}
candle_close_time = candle.dt + timedelta(minutes=interval_minutes[candle.interval])
```

### 7.4 Candle trace_id (Fix applied)

Deterministic, 32 hex chars, includes market to prevent collisions across NSE/US:

```python
trace_id = sha256(
    f"candle|{market}|{symbol}|{interval}|{candle_open_time.isoformat()}"
).hexdigest()[:32]
```

Same candle always produces the same trace_id across service restarts. Same
candle polled twice in the same DynamoDB lookback window → same trace_id →
dedup gates prevent double-publishing.

### 7.5 Latency Budget

`IntradayCandleStream` round-robins 50 instruments at 3 req/sec. Each instrument
gets a fresh confirmed candle approximately every 17 seconds. DynamoDB polling with
3-minute lookback adds at most 500ms of processing latency.

**Total candle-to-signal latency: ~17–18 seconds.**

All five candle strategies act on closed, exchange-validated bars. None requires
sub-second candle delivery. Acceptable.

### 7.6 Phase Awareness

Strategy_engine does not run `MarketPhaseGovernor`. A lightweight IST clock check
gates candle dispatch:

```python
if _current_ist_phase() in (MarketPhase.MARKET_OPEN, MarketPhase.PRE_CLOSE):
    # data_ingestion pauses candle_stream during these phases anyway;
    # DynamoDB cache has nothing new. Skip polling.
    await asyncio.sleep(2.0)
    continue
```

This is a stateless IST time calculation — no external calls, no dependency on
execution_engine's governor.

---

## 8. Architecture: paper_trade Pipeline (Q4 closed — Option A with downstream contract)

### 8.1 Decision

**Option A: full pipeline with paper_trade flag** — signals flow through risk engine
and execution engine, but execution engine routes to paper endpoint (Alpaca paper URL /
Zerodha log-only) instead of live broker.

This provides the highest-fidelity pre-live validation: real latency, real risk
validation, real order event publishing. No live capital.

Option A requires minimal, well-contained changes to three files. Since risk and
execution engines are "unchanged" by default in Phase 3, these additions are
explicitly scoped in below.

### 8.2 Signal Model Addition

`shared/models/signal.py` — add `paper_trade` field:

```python
@dataclass
class Signal:
    ...
    paper_trade: bool = False    # NEW — if True, route to paper endpoint, never live broker
```

`to_dict()` must include `"paper_trade": self.paper_trade`.
`from_dict()` must read `data.get("paper_trade", False)`.

Schema version remains `3.0` — `paper_trade` is an additive optional field with
a safe default. Existing consumers that don't know about it will parse correctly.

### 8.3 Risk Engine: Propagate Without Modification

`risk_engine/consumers/kafka_signal_consumer.py` — when building the
`SIGNAL_APPROVED` event for `signals.approved`, copy `paper_trade` from the
incoming signal payload unchanged:

```python
approved_event = {
    ...existing fields...,
    "paper_trade": incoming_signal.get("paper_trade", False),   # NEW — passthrough
}
```

No change to any risk validation logic. The risk engine validates paper signals
with identical checks to live signals — this is the point. A paper signal that
would fail risk validation should fail the same way.

### 8.4 Execution Engine: Paper Routing

`execution_engine/consumers/kafka_signal_consumer.py` — after consuming from
`signals.approved`, check the flag:

```python
if signal.paper_trade:
    await self._handle_paper_order(signal)   # NEW
else:
    await self._handle_live_order(signal)    # existing path, unchanged
```

`_handle_paper_order()`:
- Logs the would-be order with full detail (symbol, direction, quantity, price)
- Publishes an `ORDER_FILLED` event to `orders.events` with `paper=true` in metadata
- Records the "fill" in DynamoDB `orders` table with status `PAPER_FILLED`
- Does NOT call Zerodha or Alpaca
- Does NOT consume any ZerodhaRateLimiter tokens
- Updates risk engine P&L state exactly as a real fill would (via `orders.events`)

This gives the risk engine real paper P&L tracking — the drawdown monitor sees
paper fills just like live fills.

### 8.5 Scope Summary for paper_trade

| File | Change size | Risk |
|---|---|---|
| `shared/models/signal.py` | +3 lines | Low — additive, safe default |
| `strategy_engine/publishers/kafka_signal_publisher.py` | +1 line | Low — add field to event dict |
| `risk_engine/consumers/kafka_signal_consumer.py` | +1 line | Low — passthrough only |
| `execution_engine/consumers/kafka_signal_consumer.py` | ~15 lines | Low — branch on flag |
| `execution_engine/service.py` | ~20 lines | Low — new `_handle_paper_order()` method |

---

## 9. Architecture: Strategy Config Hot-Reload

### 9.1 DynamoDB Strategy Config Table

New table: `{prefix}-strategy-config`

| Attribute | Type | Description |
|---|---|---|
| `PK` | S | `STRATEGY#{strategy_name}` |
| `SK` | S | `CONFIG#{env}` |
| `enabled` | BOOL | Whether strategy generates signals |
| `paper_trade` | BOOL | Route to paper endpoint if True |
| `max_signals_per_day` | N | Hard cap per day; 0 = unlimited |
| `instruments_override` | L | Optional override of active instrument list |
| `circuit_breaker_threshold_consecutive` | N | Default: 5 |
| `circuit_breaker_threshold_rate` | N | Default: 10 (per 5 minutes) |
| `circuit_breaker_state` | S | `CLOSED` / `OPEN` / `HALF_OPEN` |
| `circuit_breaker_opened_at` | S | ISO8601 UTC; null if CLOSED |
| `circuit_breaker_reset` | BOOL | Set by operator to trigger manual reset |
| `updated_at` | S | ISO8601 UTC |
| `updated_by` | S | Operator identity |

`_config_refresh_loop()` polls every 60 seconds. Changes take effect without restart.

### 9.2 max_signals_per_day Defaults (Q5 closed)

| Strategy | Default | Rationale |
|---|---|---|
| ORBStrategy | 2 | One BUY + one SELL per symbol per day |
| Scalp1mStrategy | 10 | High-frequency scalp; cap prevents runaway |
| VWAPReversionStrategy | 6 | Mean reversion; multiple entries expected |
| IntradayTrend15mStrategy | 4 | Trend following; few high-quality signals |
| PreCloseMomentumStrategy | 2 | One directional trade per symbol |
| MomentumStrategy | **10** | Capped, not unlimited — raise after observing live behaviour |

All strategies default to `paper_trade=True` at creation. Flip to `False` only
after 5-day paper validation.

### 9.3 Operator CLI

```bash
python scripts/strategy/config.py --strategy ORBStrategy --enabled false --env prod
python scripts/strategy/config.py --strategy Scalp1mStrategy --paper-trade true --env prod
python scripts/strategy/config.py --status --env prod
```

---

## 10. Architecture: Consumer Group Topology

### 10.1 Partition Change: ticks.* 2 → 4

MSK Serverless partition increases are online and non-destructive.
Partition key (`symbol`) is unchanged — existing consumers unaffected.

Today (1 instance): all 4 partitions assigned to single consumer. No behaviour change.

Future (2 instances, if signal rate warrants): Kafka rebalances automatically,
no code changes required:
```
instance 1:  ticks.nse p0,p1 / ticks.us p0,p1
instance 2:  ticks.nse p2,p3 / ticks.us p2,p3
```

No other partition changes. `signals.pending`, `signals.approved`, `orders.events`
stay at 2 partitions.

### 10.2 Consumer Group Names — Frozen

```
strategy-v1    (ticks.nse, ticks.us)
risk-v1        (signals.pending, orders.events, kill.switch)
execution-v1   (signals.approved)
```

Never rename. Changing a consumer group name loses offset history.

### 10.3 Updated Topology After Phase 3

| Consumer Group | Topics | Instances | Partitions |
|---|---|---|---|
| `strategy-v1` | ticks.nse **(4p)**, ticks.us **(4p)** | 1 | all 4 each |
| `risk-v1` | signals.pending (2p), orders.events (2p), kill.switch (1p) | 1 | all |
| `execution-v1` | signals.approved (2p) | 1 | all |

---

## 11. Updated Architecture Diagram

```
┌────────────────────────────────────────────────────────────────────────┐
│  data_ingestion service  (UNCHANGED)                                   │
│  IntradayCandleStream                                                  │
│    3 req/sec historical API  ← _historical_rate_limiter                │
│    phase-gated by MarketPhaseGovernor (candle_stream=0 MARKET_OPEN/    │
│    PRE_CLOSE)                                                           │
│                │                                                        │
│                ▼                                                        │
│  DynamoDB: quantembrace-candle-cache  (TTL 2h)                         │
└────────────────┼───────────────────────────────────────────────────────┘
                 │ DynamoCandleConsumer.poll_new_candles()
                 │ overlapping 3-min lookback, dedup by candle key
┌────────────────▼───────────────────────────────────────────────────────┐
│  StrategyEngineService  (EC2 c6g.large, consumer group: strategy-v1)   │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  asyncio.gather(                                                  │   │
│  │      _kafka_processing_loop(),     ← tick strategies             │   │
│  │      _candle_processing_loop(),    ← candle strategies  NEW      │   │
│  │      _config_refresh_loop(),       ← 60s hot-reload    NEW      │   │
│  │      _nav_refresh_loop(),          ← existing                    │   │
│  │  )                                                                │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  _kafka_processing_loop()                                               │
│      KafkaTickConsumer ← ticks.nse/ticks.us (4 partitions each)        │
│      StrategyRunner(MomentumStrategy, TICK)  circuit=CLOSED            │
│                                                                         │
│  _candle_processing_loop()  every 500ms                                │
│      DynamoCandleConsumer  ← DynamoDB candle-cache                     │
│      IST phase check  (skip MARKET_OPEN / PRE_CLOSE)                   │
│      kill_switch_check                                                  │
│      StrategyRunner(ORBStrategy,             CANDLE)  circuit=CLOSED   │
│      StrategyRunner(Scalp1mStrategy,         CANDLE)  circuit=CLOSED   │
│      StrategyRunner(VWAPReversionStrategy,   CANDLE)  circuit=CLOSED   │
│      StrategyRunner(IntradayTrend15mStrategy,CANDLE)  circuit=CLOSED   │
│      StrategyRunner(PreCloseMomentumStrategy,CANDLE)  circuit=CLOSED   │
│                                                                         │
│  Each StrategyRunner:                                                   │
│      enabled? → paper_trade flag → circuit breaker → dispatch          │
│      → signal.generated_at = candle_close_time                         │
│      → signal.paper_trade = config.paper_trade                         │
│                                                                         │
│  _config_refresh_loop()  every 60s                                      │
│      DynamoDB strategy-config → StrategyRunner.{enabled, paper_trade,  │
│      max_signals_per_day, circuit_breaker_thresholds}                   │
│                                                                         │
│  KafkaSignalPublisher  (shared by both loops)                           │
│      signal.paper_trade propagated in event payload                    │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │
            Kafka: signals.pending (2p, UNCHANGED)
                               │
            Risk Engine (risk-v1): validates normally, passes paper_trade through
                               │
            Kafka: signals.approved (2p, UNCHANGED)
                               │
            Execution Engine (execution-v1):
                paper_trade=True  →  _handle_paper_order() (log + fake fill event)
                paper_trade=False →  _handle_live_order()  (existing, unchanged)
```

---

## 12. Prerequisites Before Phase 3 Can Start

### P1 — Verify candle-cache writes are live in target environment

```bash
python scripts/strategy/verify_candle_cache.py --env staging
# Queries candle-cache, checks items updated in last 30 min
# Reports item count and freshest timestamp per instrument
# Hard fail if no items found — Phase 3 cannot proceed without this
```

### P2 — Fix candle_stream.py import boundary violation

`data_ingestion/candle_stream.py` currently imports:
```python
from execution_engine.brokers.zerodha_broker import ZerodhaBrokerClient
```

**Phase 3 fix:** Change `IntradayCandleStream.__init__()` to accept
`zerodha: ZerodhaBrokerClient` as a constructor argument rather than importing
the class at module level. The calling service passes its own client instance.
Module-level cross-service import removed. No behaviour change for existing callers.
The full fix (moving `ZerodhaBrokerClient` to `shared/`) is Phase 5 scope.

### P3 — Grant strategy_engine IAM read access to candle-cache table

`DynamoCandleConsumer` reads `quantembrace-candle-cache`. Add to
`infra/terraform/modules/ec2_services/iam.tf` for the strategy_engine role:

```hcl
{
  Action   = ["dynamodb:Query", "dynamodb:GetItem"]
  Effect   = "Allow"
  Resource = "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-candle-cache"
}
```

---

## 13. New and Changed Components

### 13.1 New Files

| File | Purpose |
|---|---|
| `services/strategy_engine/runners/strategy_runner.py` | StrategyRunner: interface_type, dual-threshold circuit breaker, dispatch |
| `services/strategy_engine/runners/circuit_breaker.py` | CircuitBreaker state machine (consecutive + rate thresholds) |
| `services/strategy_engine/consumers/dynamo_candle_consumer.py` | DynamoDB candle-cache polling, 3-min overlapping lookback, dedup |
| `services/strategy_engine/config/strategy_config_loader.py` | DynamoDB strategy-config hot-reload every 60s |
| `scripts/strategy/config.py` | Operator CLI: enable/disable strategies, flip paper_trade |
| `scripts/strategy/reset_circuit_breaker.py` | Operator CLI: manual circuit breaker reset |
| `scripts/strategy/verify_candle_cache.py` | Pre-flight check: confirms candle-cache is being written |
| `tests/unit/test_strategy_runner.py` | Circuit breaker states, dual threshold, interface dispatch |
| `tests/unit/test_dynamo_candle_consumer.py` | Overlapping lookback, dedup, phase filter, candle timestamp rule |
| `tests/unit/test_strategy_config_loader.py` | Hot-reload paths, defaults, circuit breaker config |

### 13.2 Modified Files

| File | Change |
|---|---|
| `services/shared/models/signal.py` | Add `paper_trade: bool = False`; update `to_dict()` / `from_dict()` |
| `services/strategy_engine/publishers/kafka_signal_publisher.py` | Add `paper_trade` to published event payload |
| `services/strategy_engine/service.py` | Add `_candle_processing_loop()`, `_config_refresh_loop()`; wrap strategies in StrategyRunner; 4-loop asyncio.gather |
| `services/risk_engine/consumers/kafka_signal_consumer.py` | Propagate `paper_trade` flag from incoming signal to approved signal (+1 line) |
| `services/execution_engine/consumers/kafka_signal_consumer.py` | Branch on `paper_trade` flag; call `_handle_paper_order()` or existing live path |
| `services/execution_engine/service.py` | Add `_handle_paper_order()` method (~20 lines) |
| `services/data_ingestion/candle_stream.py` | Constructor injection: accept `zerodha: ZerodhaBrokerClient` as arg (P2) |
| `infra/terraform/modules/ec2_services/iam.tf` | Add candle-cache read to strategy_engine IAM policy (P3) |
| `scripts/kafka/create_topics.py` | Update ticks.nse / ticks.us to 4 partitions |
| `infra/terraform/modules/kafka/variables.tf` | Update default partition counts |
| `architecture/system_design.md` | Phase 3 topology |
| `architecture/data_flow.md` | Add DynamoDB candle path + paper_trade flow |
| `memory/open_tasks.md` | Phase 3 tasks |
| `memory/decisions.md` | ADR-013 |

### 13.3 Unchanged

Kill switch logic, risk validation rules, broker adapters, ZerodhaRateLimiter,
MarketPhaseGovernor, IntradayCandleStream behaviour, all Kafka consumer/publisher
internals, Kafka topic configs (except tick partition counts), all other DynamoDB tables.

---

## 14. New DynamoDB Table: strategy-config

```
Table name:   {prefix}-strategy-config
Billing:      On-demand (reads ~120 queries/min during market hours — negligible)
Key schema:   PK (S) "STRATEGY#{strategy_name}"  /  SK (S) "CONFIG#{env}"

Attributes:
  enabled                              BOOL  default: true
  paper_trade                          BOOL  default: true
  max_signals_per_day                  N     (see Q5 defaults)
  instruments_override                 L     optional
  circuit_breaker_threshold_consecutive N    default: 5
  circuit_breaker_threshold_rate       N     default: 10 (per 5 min)
  circuit_breaker_state                S     CLOSED | OPEN | HALF_OPEN
  circuit_breaker_opened_at            S     ISO8601 UTC
  circuit_breaker_reset                BOOL  operator-set
  updated_at                           S     ISO8601 UTC
  updated_by                           S     operator identity
```

---

## 15. New CloudWatch Metrics and Alarms

**Namespace:** `QuantEmbrace/StrategyEngine`

| Metric | Dimensions | Description |
|---|---|---|
| `CircuitBreakerState` | `StrategyName` | 0=CLOSED, 1=OPEN, 2=HALF_OPEN |
| `ConsecutiveErrors` | `StrategyName` | Reset to 0 on any success |
| `ErrorsInLast5Min` | `StrategyName` | Rolling rate error count |
| `SignalsByStrategy` | `StrategyName`, `Market` | Signals published |
| `PaperSignalsByStrategy` | `StrategyName` | Paper-only signal count |
| `CandleBarsProcessed` | `StrategyName`, `Interval` | Bars dispatched from DynamoDB |
| `CandleCachePollLatencyMs` | — | DynamoDB query latency |
| `ConfigRefreshLatencyMs` | — | Strategy-config read latency |
| `StrategiesDisabled` | — | Count currently disabled |

**New Alarms:**

| Alarm | Condition | Action |
|---|---|---|
| `StrategyCircuitBreakerOpen` | `CircuitBreakerState ≥ 1` (any strategy) | SNS → ops alert |
| `AllStrategiesDisabled` | `StrategiesDisabled` = total count | SNS → kill-switch topic |
| `NoCandleSignals` | `CandleBarsProcessed = 0` for 30 min during NORMAL phase | SNS → ops alert |
| `CandleCacheStale` | `CandleCachePollLatencyMs p99 > 1000ms` | SNS → ops alert |

---

## 16. Migration Plan (Phase 2 → Phase 3)

### Step 0: Prerequisites

```bash
# P1: Verify candle-cache is being written
python scripts/strategy/verify_candle_cache.py --env staging   # must PASS before proceeding

# P3: Apply IAM Terraform change
terraform apply -target=module.ec2_services.aws_iam_role_policy.strategy_engine_policy
```

### Step 1: Partition increase (online, zero-downtime)

```bash
python scripts/kafka/create_topics.py --update-partitions --env staging
python scripts/kafka/validate_phase2.py --test 1 --env staging   # confirm no message loss
python scripts/kafka/create_topics.py --update-partitions --env production
```

Schedule during off-hours. MSK Serverless rebalances are sub-second but avoid
market hours to be safe.

### Step 2: Create strategy-config DynamoDB table

```bash
terraform apply -target=module.dynamodb.aws_dynamodb_table.strategy_config
```

### Step 3: Seed initial config

```bash
python scripts/strategy/config.py --seed-from-yaml --env staging
python scripts/strategy/config.py --status --env staging   # review seeded values
python scripts/strategy/config.py --seed-from-yaml --env production
```

All 6 strategies seeded with `paper_trade=True`, `enabled=True`, defaults from §9.2.

### Step 4: Fix candle_stream.py (P2)

Constructor injection change. No behaviour change. Deploy as part of data_ingestion
service (or in the same deploy as strategy_engine — the change is backward compatible).

### Step 5: Deploy signal model changes (shared/models/signal.py)

`paper_trade=False` default is backward compatible — all existing Kafka consumers that
don't know about the field will parse correctly. Deploy before strategy_engine to
avoid any schema mismatch window.

### Step 6: Deploy risk_engine and execution_engine changes (paper_trade contract)

One-line addition to risk_engine (passthrough). ~35-line addition to execution_engine
(`_handle_paper_order()`). These changes are additive and backward compatible — if
deployed before strategy_engine sends paper signals, execution engine's existing
`paper_trade=False` default means no live orders are accidentally affected.

### Step 7: Deploy strategy_engine (rolling, zero-downtime)

New version: StrategyRunner, circuit breaker, DynamoCandleConsumer, config refresh.
Existing Kafka offsets for `strategy-v1` preserved. On startup: reads DynamoDB
strategy-config, registers all 6 StrategyRunners, begins polling DynamoDB candle-cache.

### Step 8: Validate (1 trading day)

Monitor `QuantEmbrace/StrategyEngine` in CloudWatch:
- `CandleBarsProcessed > 0` for all 5 candle strategies during market hours
- `SignalsByStrategy` showing activity across strategies
- `PaperSignalsByStrategy` matching `SignalsByStrategy` (all paper for now)
- No `CircuitBreakerOpen` alarms
- `orders.events` showing `PAPER_FILLED` events for candle strategy signals

### Step 9: Paper trading validation (5 trading days minimum)

All strategies remain `paper_trade=True`. Monitor paper P&L, signal rates, error
counts. Use `scripts/zerodha/rate_monitor.py`. Flip strategies to `paper_trade=False`
one at a time after 5 clean sessions, per existing paper trading gate process.

---

## 17. Acceptance Criteria

### Correctness
- [ ] All 6 strategies publish signals to `signals.pending`
- [ ] Signal schema identical between tick and candle strategies
- [ ] `paper_trade` field present in all published signal events (default False)
- [ ] `signal.generated_at` uses candle close time, not polling time
- [ ] Same candle produces identical `trace_id` and `signal_id` across restarts
- [ ] `paper_trade=True` signals never result in live broker calls
- [ ] `_handle_paper_order()` publishes `ORDER_FILLED` (paper) to `orders.events`
- [ ] Risk engine P&L reflects paper fills correctly
- [ ] Kill switch halts both tick and candle loops within 1s

### Isolation
- [ ] One strategy exception does NOT stop other strategies
- [ ] Circuit opens after 5 consecutive errors (consecutive threshold)
- [ ] Circuit opens after 10 errors in 5 minutes (rate threshold)
- [ ] `CircuitBreakerState = 1` CloudWatch metric fires on OPEN
- [ ] SNS alert fires on OPEN
- [ ] Half-open recovery: 3 successes → CLOSED
- [ ] Manual reset via CLI takes effect within 60s

### Candle Integration
- [ ] `DynamoCandleConsumer` makes zero Zerodha API calls
- [ ] 3-minute overlapping lookback catches eventual-consistency stragglers
- [ ] Dedup set prevents same candle dispatching twice
- [ ] Phase check: no candle signals during MARKET_OPEN or PRE_CLOSE
- [ ] `CandleBarsProcessed > 0` for all 5 strategies during NORMAL phase
- [ ] Candle strategy state saved on shutdown and warm-restored on startup

### paper_trade Contract
- [ ] `Signal.paper_trade` field added with default False; backward compatible
- [ ] Risk engine passes flag unchanged from pending → approved signal
- [ ] Execution engine branches correctly on flag
- [ ] No live broker API calls for paper signals
- [ ] Paper fills visible in `orders.events` with `paper=true` in metadata
- [ ] Paper P&L tracked in risk engine DynamoDB state

### Hot-Reload
- [ ] `enabled=False` stops signal generation within 60s, no restart
- [ ] `paper_trade` flip takes effect within 60s
- [ ] Dual circuit-breaker thresholds configurable per strategy via DynamoDB

### Topology
- [ ] `ticks.nse` and `ticks.us` at 4 partitions in staging and production
- [ ] `strategy-v1` consumer group receives all messages post-rebalance
- [ ] No message loss during partition increase (sequence_id continuity check)
- [ ] Consumer group name `strategy-v1` unchanged

### Rate Limit Compliance
- [ ] `strategy_engine` process makes zero Zerodha API calls
- [ ] `rate_monitor.py` shows no candle-stream activity from strategy_engine
- [ ] Phase budget enforcement unchanged in data_ingestion

### Testing
- [ ] Unit tests for `StrategyRunner`: both interface types, dual-threshold circuit breaker
- [ ] Unit tests for `DynamoCandleConsumer`: overlapping lookback, dedup, phase filter, timestamp rule
- [ ] Unit tests for `StrategyConfigLoader`: hot-reload, defaults, circuit breaker config
- [ ] Unit tests for paper_trade path in execution_engine
- [ ] Integration test: 6 strategies, one crashes repeatedly → circuit opens, others unaffected
- [ ] Coverage ≥ 85% for all new files

---

## 18. Open Questions — ALL CLOSED

| Q | Question | Answer |
|---|---|---|
| Q1 | One StrategyRunner or two? | **One class** with `interface_type = TICK \| CANDLE`. Lifecycle concerns are identical. |
| Q2 | Circuit breaker threshold? | **Dual threshold**: 5 consecutive errors OR 10 errors in 5 minutes. Configurable per strategy. |
| Q3 | ~~Where to instantiate IntradayCandleStream?~~ | **Resolved in v1.1**: stays in data_ingestion. strategy_engine polls DynamoDB. |
| Q4 | paper_trade=True behaviour? | **Option A: full pipeline**, with explicit minimal changes to Risk + Execution. Scoped in §8. |
| Q5 | max_signals_per_day defaults? | See §9.2. MomentumStrategy default = **10** (not unlimited). |

---

## 19. Risks and Tradeoffs

| Risk | Severity | Mitigation |
|---|---|---|
| Candle-cache not populated in staging (P1 fails) | Medium | Hard fail on `verify_candle_cache.py`; block Phase 3 until resolved |
| paper_trade contract changes trigger unforeseen execution engine path | Low | Changes are additive and behind a flag; existing live path unchanged |
| DynamoDB overlapping lookback slightly increases read cost | Low | 120 queries/min × 2 pages per query ≈ 240 RCUs/min; ~$2/month |
| Partition increase triggers strategy-v1 rebalance | Low | Schedule pre-market; MSK rebalances sub-second |
| candle_stream.py constructor change breaks caller | Low | Change is additive; `zerodha` param added with validation in `start()` |
| Dual-threshold circuit breaker more complex to unit test | Low | Both thresholds tested independently then together |

---

## 20. Cost Impact (Corrected)

| Item | Before Phase 3 | After Phase 3 | Delta |
|---|---|---|---|
| MSK Serverless (4 partitions) | ~$5–15/mo | ~$5–15/mo | ~$0 (personal volume) |
| DynamoDB — strategy-config | $0 | ~$0.01/mo (120 reads/min × config table) | +$0.01/mo |
| DynamoDB — candle-cache reads | $0 | ~$2/mo (120 queries/min × 16h/day, overlapping lookback ≈ 2 pages per query) | +$2/mo |
| CloudWatch (9 metrics, 4 alarms) | — | ~$3/mo | +$3/mo |
| EC2 compute | ~$166/mo | ~$166/mo | $0 |
| **Total monthly impact** | | | **+~$5/month** |

*DynamoDB cost note: 120 queries/min × 60 min × 16 trading hours = 115,200 queries/day.
With overlapping 3-minute lookback, each Query returns ≈ 2 pages at most for 50 instruments.
At $0.25/million read request units: ~115,200 × 2 = 230,400 RCUs/day × 30 = 6.9M/month ≈ $1.73/month.
Still well within the "cheap" category.*

---

## 21. ADR-013 (To Be Recorded on Approval)

**Title**: Phase 3 — Strategy Isolation, DynamoDB Candle Integration, paper_trade Pipeline, Topology Preparation

**Status**: Accepted — 2026-05-05

**Decision**:
1. Single `StrategyRunner` class, `interface_type = TICK | CANDLE`, dual-threshold circuit breaker
2. Candle strategies consume via DynamoDB `candle-cache` poll (3-min overlapping lookback, 500ms interval)
3. `IntradayCandleStream` stays in `data_ingestion`; strategy_engine makes zero Zerodha API calls
4. `candle_stream.py` cross-service import fixed via constructor injection
5. `paper_trade` field added to Signal model; full pipeline support via minimal risk + execution additions
6. `max_signals_per_day` defaults set (MomentumStrategy capped at 10)
7. Tick topic partitions 2 → 4 (topology preparation for future horizontal scale)
8. No multi-instance strategy engine in Phase 3 (deferred until signal rate justifies it)

**Why DynamoDB over in-process callback:**
In-process `IntradayCandleStream` in strategy_engine would create a second Zerodha session,
break `MarketPhaseGovernor` phase enforcement, add an unmonitored API call path, and violate
the service boundary (strategy → broker imports). DynamoDB polling resolves all four conflicts
with negligible latency tradeoff (~17.5s candle-to-signal) for strategies that operate on
confirmed minute bars.

---

## 22. Review Checklist

- [x] All five open questions answered (§18)
- [x] DynamoDB read cost corrected (120 queries/min, ~$2/mo)
- [x] paper_trade downstream contract explicitly scoped (§8)
- [x] Candle trace_id: 32 hex chars including market prefix (§7.4)
- [x] Overlapping lookback (3-min window, dedup by candle key) (§7.2)
- [x] Candle signal generated_at = candle close time, not poll time (§7.3)
- [x] **FINAL APPROVAL** — approved 2026-05-05 ✅

---

*Document status: REVISED v1.2 — 2026-05-05*
*All open questions closed. Five required fixes applied. Awaiting final approval.*
