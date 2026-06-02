# Phase 8 Design Review: Production Hardening + Fault Tolerance

**Version**: 1.0 — APPROVED
**Date**: 2026-05-08
**Status**: ✅ APPROVED — implementation ready
**Author**: QuantEmbrace Architect
**Prerequisites**: Phase 7 complete ✅ (retry infrastructure, preflight check, paper session report, go-live runbook)

---

## 1. Executive Summary

Phase 8 systematically closes eight known failure modes identified during the Phase 7
post-review. Every scenario was observed or derived from the live paper-trading sessions
and represents a path to financial loss, duplicate orders, or unprotected positions.

**No new capabilities are added.** Phase 8 is purely defensive: it hardens the paths that
already exist so that the failure modes identified in the review cannot cause a bad outcome
in production. The eight targets are:

| # | Failure | Consequence Without Fix |
|---|---------|------------------------|
| F1 | Kafka down | Kill switch may not propagate; broker keeps receiving orders |
| F2 | Zerodha slow / 429 | Cancel priority starved; new placements continue during degradation |
| F3 | ACK delayed / network timeout | Blind retry places duplicate order undetected by idempotency gate |
| F4 | Market data freeze / reconnect | Gap-period candles treated as normal; bad signals generated |
| F5 | Restart during market | Stale DynamoDB schema on cold start leads to incorrect position state |
| F6 | Consumer lag / replay | Publish-after-reserve race; retry sink can double-process |
| F7 | Reconciliation fail | Position drift is logged but trading continues on incorrect state |
| F8 | Stop-loss miss | Position unprotected if child SL order rejected after entry fill |

All eight fixes are surgical. No service boundaries move. Kafka topic schema stays at v3.0 /
v4.0. DynamoDB table ownership is unchanged. The risk engine remains the sole gatekeeper.

---

## 2. Current State (Post Phase 7)

| Component | State |
|-----------|-------|
| Retry infrastructure | `signals.enriched.retry` + `.dlq` wired; `KafkaRetryReplayer` drains retry → enriched |
| Preflight check | `scripts/deploy/preflight_check.py` — 9 checks, exit 0 = safe |
| Paper session report | `scripts/monitoring/paper_session_report.py` — 8-threshold go-live gate |
| Go-live runbook | `docs/runbooks/go_live_checklist.md` |
| Kill switch | DynamoDB + SNS + Kafka `risk.kill-switch`; 5 auto-triggers in KillSwitchMonitor |
| Order idempotency | DynamoDB conditional write `attribute_not_exists(order_id)` |
| Reconciliation | End-of-day: logs mismatches to CloudWatch, no hard stop |
| Stop-loss | Child SL order placed after fill confirmation; no fallback on child SL rejection |
| Consumer lag | `EnrichmentWatchdog` monitors aiengine-v1 lag; no watchdog on risk-v1 lag |
| Startup sequence | `set_ready(True)` called after config refresh; no position/broker reconciliation gate |
| Data quality | No `data_quality` field on tick or candle; warm-up suppression absent |
| Rate limiter | Global 10 req/s token bucket; no per-endpoint budgets |
| ACK state machine | `PENDING → PLACED / FAILED`; no `ACK_UNKNOWN` intermediate state |

---

## 3. Scope

### In scope

| # | Component | Description |
|---|-----------|-------------|
| 8-1 | `OrderStatus.ACK_UNKNOWN` + pre-retry tag scan | Prevents blind duplicate on BrokerTimeoutError |
| 8-2 | Per-endpoint Zerodha rate budgets + cancel priority | Priority queuing; placement pause on degradation |
| 8-3 | `data_quality` field + warm-up suppression | Propagates gap flag from tick through to strategy |
| 8-4 | Startup reconciliation gate | All services wait on broker + DynamoDB reconcile before consuming |
| 8-5 | Durable Kafka outbox + broker-halt on Kafka failure | Local buffer; halt new entries when Kafka unavailable |
| 8-6 | Inbox/outbox for signal processing + lag kill switch | Closes publish-after-reserve race; halts on runaway lag |
| 8-7 | Hard reconciliation stop | Position drift triggers halt; clears only via operator script |
| 8-8 | Broker-native SL + immediate flatten fallback | Protects position when child SL is rejected |
| 8-9 | Unit + integration tests for all 8 scenarios | Must be green before Phase 8 is marked complete |
| 8-10 | DynamoDB: `signal-inbox`, `signal-outbox` tables | Inbox/outbox persistence (F6) |
| 8-11 | `scripts/ops/reconcile.py` | Three-way (broker + DynamoDB + Kafka) drift operator tool |

### Out of scope

- Smart Order Router / TWAP / VWAP (Phase 7 latency enhancement — separate concern)
- New Kafka topics or schema version bumps (Phase 8 is schema-stable)
- US equities reconciliation script (Alpaca already supports `list_positions()` — same pattern, lower urgency)
- Automated reconciliation (operator decision required before clearing halt; automation is a Phase 9 candidate)

---

## 4. Failure Mode Designs

### F1 — Kafka Down: Kill Switch Fanout Fails

**Root cause**: When Kafka becomes unavailable, `KafkaTickPublisher._on_delivery_failure()`
attempts to activate the kill switch by writing to the `risk.kill-switch` topic — which is also
down. The DynamoDB fallback write (`_kill_switch_fallback_dynamo()`) executes, but the write
uses a boto3 DynamoDB client that may share the same thread pool under extreme resource
pressure. Additionally, kill-switch consumers that rely solely on the Kafka topic path may
not see the activation until Kafka recovers.

**Three-part mitigation**:

**Part A — Durable local outbox** (`services/shared/kafka/local_outbox.py`):

```python
class LocalOutbox:
    """SQLite-backed outbox for messages when Kafka is unavailable.
    Written synchronously; drained by background asyncio.Task when Kafka recovers.
    Never used for trading signals — only for kill-switch events and audit records.
    """
    async def enqueue(self, topic: str, key: str, value: bytes) -> None: ...
    async def drain(self, producer: KafkaProducer) -> int: ...  # returns drained count
```

SQLite chosen over a file because it supports concurrent readers and atomic writes without
additional locking. The outbox stores at most 1,000 entries (hard cap). On overflow, the
service activates the kill switch and halts new order intake.

**Part B — Broker halt on consecutive Kafka failures**:

`KafkaTickPublisher` already activates kill switch after 3 consecutive delivery failures.
Phase 8 extends this to all publishers: `KafkaSignalPublisher`, `KafkaEnrichedPublisher`,
and `KafkaApprovedPublisher` will call `_halt_new_order_intake()` after 3 failures. This
method sets a service-level `_kafka_healthy = False` flag that the consumer loops check
before processing new messages.

**Part C — Kill-switch fanout CI test**:

New test `tests/integration/test_kill_switch_fanout.py`:
- Starts risk_engine with Kafka mocked to return `KafkaException` for 5 consecutive calls
- Asserts kill switch DynamoDB state is `ACTIVE` within 2 seconds
- Asserts all subsequent `validate_signal()` calls return `KILL_SWITCH_ACTIVE`
- Restores Kafka, asserts outbox drains within 5 seconds

**Files**: `shared/kafka/local_outbox.py`, `data_ingestion/publishers/kafka_tick_publisher.py`,
`shared/kafka/base_publisher.py`, `tests/integration/test_kill_switch_fanout.py`

---

### F2 — Zerodha Slow / 429: Cancel Priority Starved

**Root cause**: The global `ZerodhaRateLimiter` (10 req/s token bucket) does not distinguish
between order placement, cancellation, and polling calls. When Zerodha is degraded, a burst
of `place_order` calls can consume the entire token budget, leaving `cancel_order` calls
queued behind them. In a kill-switch scenario, this is dangerous: the kill switch fires but
`cancel_order` calls starve until `place_order` calls drain the shared queue.

**Three-tier endpoint budget**:

```python
@dataclass
class ZerodhaEndpointBudgets:
    # Total: 10 req/s as per Zerodha limit
    place_order:  float = 3.0   # reduced during DEGRADED state
    cancel_order: float = 4.0   # always preserved; boosted during DEGRADED state
    get_orders:   float = 2.0   # reduced during DEGRADED state
    misc:         float = 1.0   # quote snapshots, instrument list, etc.
```

**Degraded state** (429 rate or circuit breaker OPEN on place_order):
- `place_order` budget → `0.0` (no new placements)
- `cancel_order` budget → `7.0` (takes from place + misc)
- Service-level `_placement_paused = True` flag set; consumer loop stops polling new `signals.approved`

**Consumer pause on placement circuit open**:

```python
# execution_engine/service.py
async def _signal_processing_loop(self) -> None:
    while self._running:
        if self._placement_paused:
            await asyncio.sleep(1.0)   # back off; don't consume new signals
            continue
        signal = await self._kafka_consumer.poll_approved_signal()
        ...
```

This avoids a backlog of approved signals that are all destined to fail and waste Kafka
offsets. The consumer resumes when `_placement_paused` is cleared (placement circuit CLOSED
for 30 consecutive seconds).

**Files**: `shared/zerodha/rate_limiter.py`, `shared/zerodha/endpoint_budgets.py` (new),
`execution_engine/service.py`, `tests/unit/test_zerodha_endpoint_budgets.py`

---

### F3 — ACK Delayed / Network Timeout: Blind Duplicate Placement

**Root cause**: `BrokerTimeoutError` during `place_order()` means the broker *may or may not*
have received the order. The current retry loop transitions the order from `PENDING` directly
to a new `place_order()` call. If the broker received the first call, the retry places a
second, distinct order (different `broker_order_id`). The DynamoDB idempotency gate only
checks `order_id` — it does not scan the broker for an already-placed order.

**`ACK_UNKNOWN` state machine**:

```
PENDING → place_order() → BrokerTimeoutError
       → ACK_UNKNOWN   (written atomically: DynamoDB update + stop retry)
             │
             └──▶  recovery_task: scan broker orders for matching client tag
                        ├── found (tag matches) → update to PLACED; resume monitoring
                        └── not found (no match within scan_window_s) → retry once; then FAILED
```

**Pre-retry tag scan** in `execution_engine/brokers/base_broker.py`:

```python
async def resolve_ack_unknown(self, order: Order) -> OrderStatus:
    """Called before any retry of an ACK_UNKNOWN order.
    Scans recent broker orders for client_tag matching order.order_id[:20].
    Returns PLACED (skip retry) or UNKNOWN (safe to retry once).
    """
```

Zerodha: scans `kite.orders()` (last 24h), matches on `tag` field (first 20 chars of order_id).
Alpaca: queries `client_order_id` directly (exact match — no scan needed).

**Startup sweep**: During `_reconcile_state()` (F5), all `ACK_UNKNOWN` orders are resolved
before any consumer loop starts.

**Files**: `shared/models/order.py` (add `ACK_UNKNOWN`),
`execution_engine/brokers/base_broker.py`, `execution_engine/brokers/zerodha_broker.py`,
`execution_engine/brokers/alpaca_broker.py`,
`tests/unit/test_ack_unknown_resolution.py`

---

### F4 — Market Data Freeze / Reconnect: Gap Flag Lost

**Root cause**: After a Kite WebSocket reconnect, `IntradayCandleStream` resumes writing
candles immediately. The candle built over the gap window has incorrect OHLCV values
(open from before disconnect, close from after). Strategies treat this candle as valid
and may generate signals on corrupted data.

**`data_quality` field propagation** (schema-additive, backward-compatible):

```python
class DataQuality(str, Enum):
    NORMAL      = "NORMAL"        # clean feed, strategy can use
    WARMING_UP  = "WARMING_UP"    # post-reconnect warm-up period
    GAP         = "GAP"           # candle span crosses a disconnect gap
    STALE       = "STALE"         # tick arrived but timestamp is old (>N seconds)
```

Added to `CandleData` (DynamoDB write) and `Bar` (dispatch to StrategyRunner). Default:
`NORMAL`. No existing code paths break if the field is absent (Pydantic `default=DataQuality.NORMAL`).

**Warm-up suppression in StrategyRunner**:

```python
async def dispatch_bar(self, bar: Bar) -> Signal | None:
    if bar.data_quality != DataQuality.NORMAL:
        self._metrics.increment("bars_suppressed_data_quality")
        return None              # no signal generated during warm-up / gap
    ...
```

**Warm-up window** (configurable per interval in DynamoDB `strategy-config`):
- 1m candles: 30s warm-up after reconnect (= 1 bar dropped)
- 5m candles: 5m warm-up (= 1 bar dropped)
- 15m candles: 15m warm-up (= 1 bar dropped)

`IntradayCandleStream` sets `data_quality=WARMING_UP` on all candles during the warm-up
window. After the window, data quality reverts to `NORMAL`.

**Files**: `shared/models/candle.py` (add `DataQuality`, update `CandleData` + `Bar`),
`data_ingestion/candle_stream.py`, `strategy_engine/runners/strategy_runner.py`,
`tests/unit/test_data_quality_suppression.py`

---

### F5 — Restart During Market: Cold-Start Schema Mismatch

**Root cause**: When a service restarts mid-session, it reads DynamoDB records written by
the previous service version. If `Position`, `OrderState`, or `RiskContext` models have
gained or changed fields between versions, Pydantic validation errors in `from_dict()`
are caught by the caller's broad `except Exception` and silently default to zero/empty
state. Trading then proceeds with incorrect baseline positions or risk counters.

**Startup reconciliation gate** (common pattern, applied to all four trading services):

```python
class ReconciliationGate:
    """Blocks consumer loops until DynamoDB state is verified against broker.
    Called in every service's start() before any asyncio.gather loop begins.
    """
    async def run(self) -> None:
        await self._load_and_validate_dynamo_state()   # schema check
        await self._reconcile_with_broker()            # broker-side truth
        await self._verify_risk_counters()             # daily P&L, drawdown
        self._gate.set()                               # unblocks consumer loops

    async def _load_and_validate_dynamo_state(self) -> None:
        # Read all active domain objects, log schema version mismatches,
        # apply model migrations (add default for new required fields),
        # fail-halt if migration is impossible.
```

**Gate in consumer loops**: each `_kafka_processing_loop()` does:
```python
await self._reconciliation_gate.wait()   # asyncio.Event; never times out
```

**Schema migration helpers** on `Position`, `OrderState`, `RiskContext`:
- New optional fields get `default=None` + `@field_validator` that logs once if absent
- New required fields get a migration default and a `# MIGRATION: default applied` log line
- Old fields that no longer exist on model are ignored (`model_config = ConfigDict(extra="ignore")`)

**Files**: `shared/reconciliation/gate.py` (new),
`execution_engine/service.py`, `risk_engine/service.py`,
`strategy_engine/service.py`, `data_ingestion/service.py`,
`tests/unit/test_reconciliation_gate.py`

---

### F6 — Consumer Lag / Replay / Duplicate Events: Unsafe Publish-After-Reserve

**Root cause**: The risk engine's signal processing flow is:
1. DynamoDB: write `risk_decision_reservation` (PK=`RISK_DECISION#{signal_id}`)
2. Kafka: commit offset on `signals.enriched`
3. Kafka: publish to `signals.approved`

If step 3 fails after step 1 and step 2 succeed, the signal is approved (reservation held)
but never reaches execution. On retry consumer replay, the signal re-enters processing.
The reservation gate at step 1 sees the existing record and silently skips — correct, but
the approved event is still never published. The signal is permanently lost with no alert.

Additionally, there is no Kafka consumer lag watchdog for `risk-v1` on `signals.enriched`.
A runaway lag on this consumer group means approved signals are accumulating but execution
is not seeing them — the system appears live but orders are not flowing.

**Inbox/outbox for signal processing**:

**Inbox** (`{prefix}-signal-inbox` DynamoDB table):
- Written by `KafkaEnrichedConsumer` *before* processing. Key: `PK=SIGNAL#{signal_id}`.
- `status: RECEIVED | PROCESSING | DONE | FAILED`
- Deduplication: `attribute_not_exists(PK)` conditional write. Second delivery → write
  fails → skip silently (idempotent).
- TTL: 2h (signals expire quickly; inbox is not an audit log — that's `ops.audit`).

**Outbox** (`{prefix}-signal-outbox` DynamoDB table):
- Written by risk engine after approval decision, *before* committing Kafka offset.
- Key: `PK=APPROVED#{signal_id}`, Value: serialized approved signal envelope.
- Background `OutboxPublisher` task: polls outbox every 100ms, publishes pending items
  to `signals.approved`, then deletes the outbox record.
- Kafka offset committed only after outbox write succeeds.
- TTL: 2h.

This makes the sequence:
1. Read signal from Kafka
2. Write to inbox (idempotent — skip if exists)
3. Process signal (risk validation)
4. Write approved signal to outbox (atomic with risk decision)
5. Commit Kafka offset
6. `OutboxPublisher` drains outbox → `signals.approved`

Steps 4 and 5 being separate means: if step 5 fails, the consumer will re-deliver the signal.
Step 2 catches the re-delivery and skips it. The outbox record written in step 4 still
exists and will be published by the background task. No signal loss; no double processing.

**Lag-triggered kill switch** (`risk_engine/watchdogs/kafka_lag_watchdog.py`):

```python
class KafkaLagWatchdog:
    """Monitors risk-v1 consumer group lag on signals.enriched.
    Halts trading if lag stays above threshold for sustained_seconds.
    Distinct from EnrichmentWatchdog (which handles aiengine-v1 on signals.pending).
    """
    halt_lag_threshold: int = 500     # offsets
    halt_sustained_seconds: int = 60  # must stay above threshold for this long
```

Activates the kill switch if lag on `risk-v1` / `signals.enriched` exceeds threshold for
the sustained window. Clears automatically when lag returns to < 10 for 5 checks.

**Files**: `risk_engine/processing/signal_inbox.py` (new),
`risk_engine/processing/signal_outbox.py` (new),
`risk_engine/processing/outbox_publisher.py` (new),
`risk_engine/watchdogs/kafka_lag_watchdog.py` (new),
`risk_engine/service.py`,
`infra/terraform/modules/dynamodb/signal_processing.tf` (new),
`tests/unit/test_signal_inbox_outbox.py`

---

### F7 — Reconciliation Fail: Drift Is Not a Hard Stop

**Root cause**: End-of-day position reconciliation in `execution_engine/service.py`
calls `_reconcile_positions()` which compares broker positions to DynamoDB, logs any
drift, and continues. A position mismatch means subsequent signals execute against
a wrong position baseline (e.g., risk engine thinks exposure is 0 when the broker
holds a carry position). This is a silent P&L risk.

**Hard reconciliation gate** (`risk_engine/validators/reconciliation_validator.py`):

```python
class ReconciliationValidator:
    """Reads reconciliation_required flag from DynamoDB risk-state.
    If True: rejects every non-closeout signal with RECONCILIATION_HALT reason.
    Cleared only by operator running scripts/ops/reconcile.py --clear.
    """
```

**Three-way reconciliation** (`scripts/ops/reconcile.py`):

```
Source 1: broker.get_positions()             — live truth from exchange
Source 2: DynamoDB positions table           — system truth
Source 3: orders.events Kafka topic (last Nh) — event-log truth

Drift report:
  ┌──────────────┬────────────┬──────────────┬──────────────┬─────────┐
  │ instrument   │ broker_qty │ dynamo_qty   │ events_qty   │ action  │
  ├──────────────┼────────────┼──────────────┼──────────────┼─────────┤
  │ NSE:RELIANCE │     100    │      100     │     100      │ OK      │
  │ NSE:INFY     │      50    │       75     │      75      │ DRIFT   │
  │ US:TSLA      │       0    │      10      │      10      │ DRIFT   │
  └──────────────┴────────────┴──────────────┴──────────────┴─────────┘

Operator choices per DRIFT row:
  [b] Trust broker (update DynamoDB to match broker)
  [d] Trust DynamoDB (place corrective order with broker)
  [s] Skip (leave drift; operator monitors manually)
  [h] Halt all trading for this instrument until next reconcile
```

**`reconciliation_required` flag** in DynamoDB `risk-state` table:
- Set by `execution_engine._reconcile_positions()` when drift > `max_drift_pct` (default 5%)
- Read by `ReconciliationValidator` every signal validation
- Cleared only by `scripts/ops/reconcile.py --clear` (requires explicit confirmation)
- Persists across service restarts (DynamoDB is source of truth)

**Files**: `risk_engine/validators/reconciliation_validator.py` (new),
`execution_engine/reconciliation/position_reconciler.py`,
`scripts/ops/reconcile.py` (new),
`tests/unit/test_reconciliation_validator.py`

---

### F8 — Stop-Loss Miss: Child SL Rejected After Entry Fill

**Root cause**: After `execution_engine` confirms a fill and calls `apply_fill_to_position()`,
it attempts to place a child SL order via `_place_protective_stop()`. This call can be
rejected by Zerodha for several reasons:
- Instrument entered a circuit breaker / upper/lower circuit band
- Margin requirements changed between entry and SL placement
- Rate limit already consumed by concurrent order placements
- SL price outside allowed tick-size range (slippage caused rounding error)

When the child SL fails, the position is unprotected and only manual intervention saves it.

**Three-tier protective order strategy**:

**Tier 1 — Broker-native**: For Zerodha NSE equity orders, attempt a Cover Order (CO)
or a Bracket Order (BO) where the broker holds the SL at exchange level. If CO/BO is
rejected or not available for the instrument, fall through to Tier 2.

**Tier 2 — Regular SL order with retry**: Place a regular `SL-M` order with up to 3
retries (each with 200ms backoff). On each failure, log `SL_PLACEMENT_ATTEMPT_FAILED`
at WARNING level. After 3 failures, proceed to Tier 3.

**Tier 3 — Immediate flatten**: If Tier 2 exhausted:
1. Log `SL_PLACEMENT_FAILED_FLATTENING_POSITION` at CRITICAL level
2. Publish to `ops.audit` with `event_type=PROTECTIVE_FLATTEN`
3. Place a `MARKET` order for the full position quantity in the opposing direction
4. Mark position as `FLATTENING` in DynamoDB (prevents new signals on this instrument)
5. Alert via SNS to `kill_switch` topic (P0 alert)

**Orphan position detector** (`execution_engine/monitors/orphan_detector.py`):

Background task running every 60s during market hours:
- Reads all `ACTIVE` positions from DynamoDB `positions` table
- For each, checks DynamoDB `orders` table for an `ACTIVE` SL order with `order_type=SL/SL-M`
- If no active SL found and position age > `orphan_grace_period_s` (default 30s): logs
  `ORPHAN_POSITION` at CRITICAL, publishes to `ops.audit`, triggers SNS P0 alert
- Does NOT auto-flatten orphans (operator may have intentionally cancelled the SL). The
  alert is mandatory; the action is human.

**SL priority in rate limiter**: SL orders are submitted via the `cancel_order` endpoint
budget lane (same high-priority lane) to avoid being starved by concurrent signal processing.

**Files**: `execution_engine/brokers/zerodha_broker.py` (Tier 1 CO/BO support),
`execution_engine/brokers/base_broker.py` (place_protective_stop with 3-tier logic),
`execution_engine/monitors/orphan_detector.py` (new),
`shared/models/order.py` (add `FLATTENING` status),
`tests/unit/test_protective_stop.py`,
`tests/unit/test_orphan_detector.py`

---

## 5. Task List

| Task | File(s) | Priority | Status |
|------|---------|----------|--------|
| PHASE8-001 | `shared/models/order.py` + broker resolution — `ACK_UNKNOWN` state + pre-retry tag scan | P0 | 🔲 |
| PHASE8-002 | `shared/zerodha/endpoint_budgets.py` — per-endpoint rate budgets + cancel priority + placement pause | P0 | 🔲 |
| PHASE8-003 | `shared/models/candle.py` + `data_ingestion/candle_stream.py` + `strategy_engine/runners/strategy_runner.py` — `data_quality` field + warm-up suppression | P1 | 🔲 |
| PHASE8-004 | `shared/reconciliation/gate.py` — startup reconciliation gate wired into all 4 service start() methods | P0 | 🔲 |
| PHASE8-005 | `shared/kafka/local_outbox.py` — SQLite durable outbox + `_halt_new_order_intake()` in base_publisher + CI kill-switch fanout test | P0 | 🔲 |
| PHASE8-006 | `risk_engine/processing/signal_inbox.py` + `signal_outbox.py` + `outbox_publisher.py` + `watchdogs/kafka_lag_watchdog.py` — inbox/outbox + lag kill switch | P0 | 🔲 |
| PHASE8-007 | `risk_engine/validators/reconciliation_validator.py` + `scripts/ops/reconcile.py` — hard reconciliation stop + three-way drift tool | P1 | 🔲 |
| PHASE8-008 | `execution_engine/brokers/base_broker.py` 3-tier SL + `execution_engine/monitors/orphan_detector.py` — broker-native SL + flatten + orphan detection | P0 | 🔲 |
| PHASE8-009 | `infra/terraform/modules/dynamodb/signal_processing.tf` — `signal-inbox` + `signal-outbox` tables | P1 | 🔲 |
| PHASE8-010 | `tests/unit/test_phase8_hardening.py` + `tests/integration/test_kill_switch_fanout.py` — all 8 failure scenarios covered | P1 | 🔲 |

**P0 tasks** must be complete before any live capital is deployed. **P1 tasks** must be
complete before the second live trading week.

---

## 6. New DynamoDB Tables

| Table | PK | SK | Owner | TTL | Purpose |
|-------|----|----|-------|-----|---------|
| `{prefix}-signal-inbox` | `SIGNAL#{signal_id}` | — | risk_engine | 2h | Dedup incoming enriched signals before processing |
| `{prefix}-signal-outbox` | `APPROVED#{signal_id}` | — | risk_engine | 2h | Durable approved signal buffer before Kafka publish |

Both tables: on-demand capacity, no PITR (2h TTL means data is not compliance-critical here
— `ops.audit` Kafka topic is the audit record). AES256 encryption.

---

## 7. Schema Changes Summary

All changes are additive / backward-compatible. No schema_version bump required.

| Model | Change | Default |
|-------|--------|---------|
| `OrderStatus` enum | Add `ACK_UNKNOWN`, `FLATTENING` values | — |
| `CandleData` | Add `data_quality: DataQuality = DataQuality.NORMAL` | `NORMAL` |
| `Bar` | Add `data_quality: DataQuality = DataQuality.NORMAL` | `NORMAL` |
| `Position` | Add `model_config = ConfigDict(extra="ignore")` | — |
| `OrderState` | Add `model_config = ConfigDict(extra="ignore")` | — |

No Kafka event envelope changes. Consumers that don't know about the new enum values
will use the existing `default=NORMAL` and continue functioning correctly.

---

## 8. Acceptance Criteria

### F1 — Kafka down / kill-switch fanout
- [ ] Simulated Kafka failure (mock producer raises `KafkaException` 5× consecutive) → kill switch DynamoDB state = `ACTIVE` within 2s
- [ ] All subsequent `validate_signal()` calls return `KILL_SWITCH_ACTIVE` without Kafka
- [ ] Local outbox drains to zero within 10s when Kafka is restored
- [ ] CI test `test_kill_switch_fanout.py` passes in < 30s

### F2 — Zerodha slow / 429 / cancel priority
- [ ] When `place_order` circuit OPEN: consumer loop stops polling `signals.approved` within 1 polling cycle
- [ ] `cancel_order` still executes at full budget while `place_order` circuit is OPEN
- [ ] Per-endpoint budget totals ≤ 10 req/s at all times (unit tested with simulated bursts)
- [ ] Consumer resumes within 5s of `place_order` circuit returning to CLOSED

### F3 — ACK_UNKNOWN / duplicate prevention
- [ ] `BrokerTimeoutError` on `place_order()` → DynamoDB order status = `ACK_UNKNOWN` (not `FAILED` or `PENDING`)
- [ ] Pre-retry tag scan: broker order found with matching tag → status = `PLACED`, no second API call
- [ ] Pre-retry tag scan: no broker order found → retry once (not blindly, not infinitely)
- [ ] Service restart with `ACK_UNKNOWN` orders → all resolved before first consumer loop tick

### F4 — Data quality / warm-up suppression
- [ ] Candle written during warm-up window has `data_quality=WARMING_UP` in DynamoDB
- [ ] `StrategyRunner.dispatch_bar()` with `WARMING_UP` bar → returns `None`, increments `bars_suppressed_data_quality` metric
- [ ] First candle after warm-up window has `data_quality=NORMAL`
- [ ] Strategy generates signals normally after warm-up (no permanent suppression)

### F5 — Startup reconciliation gate
- [ ] Service start with `ACK_UNKNOWN` orders in DynamoDB → reconciliation gate blocks consumer loop until resolved
- [ ] Service start with position field missing from DynamoDB record → warning logged, default applied, gate passes
- [ ] Service start with irreconcilable schema error → gate halts service with clear error; no consumer loop starts

### F6 — Inbox/outbox / lag kill switch
- [ ] Duplicate signal delivery (same `signal_id` twice) → second delivery silently skipped via inbox gate
- [ ] Outbox record present → approved signal eventually published even if Kafka publish initially fails
- [ ] Kafka offset committed only after outbox write succeeds (not before)
- [ ] `risk-v1` lag on `signals.enriched` > 500 for > 60s → kill switch activates within 2s of threshold

### F7 — Hard reconciliation stop
- [ ] Position drift > `max_drift_pct` at end-of-day → `reconciliation_required = True` in DynamoDB
- [ ] `ReconciliationValidator` rejects all non-closeout signals when `reconciliation_required = True`
- [ ] `scripts/ops/reconcile.py --env prod` prints correct three-way drift table
- [ ] `scripts/ops/reconcile.py --clear` requires explicit confirmation string; clears flag; signal processing resumes

### F8 — Stop-loss / immediate flatten / orphan detection
- [ ] CO/BO placement attempted first for eligible NSE instruments; falls back to SL-M on rejection
- [ ] SL-M fails 3 times → MARKET flatten placed within 500ms of third failure
- [ ] CRITICAL log + SNS alert published on flatten trigger
- [ ] `OrphanDetector` detects position with no active SL after `orphan_grace_period_s` → CRITICAL log + SNS alert
- [ ] Orphan detector does NOT auto-flatten (operator decision required)

---

## 9. Test Requirements

All tests must be unit tests (no real broker calls, no real Kafka, no real DynamoDB).
Use the `unittest.mock` / `pytest-mock` patterns established in Phases 3–7.

| Test file | Covers | Target count |
|-----------|--------|-------------|
| `test_phase8_hardening.py` | F1 local outbox, F2 endpoint budgets, F3 ACK_UNKNOWN, F4 warm-up suppression, F5 gate, F7 reconciliation validator, F8 orphan detector | ~60 tests |
| `test_signal_inbox_outbox.py` | F6 inbox dedup, outbox publish, offset commit ordering | ~25 tests |
| `test_ack_unknown_resolution.py` | F3 tag scan (Zerodha + Alpaca), retry count, startup sweep | ~20 tests |
| `test_data_quality_suppression.py` | F4 field propagation, warm-up window math, NORMAL after warm-up | ~18 tests |
| `test_reconciliation_gate.py` | F5 gate blocking, schema migration, irreconcilable halt | ~15 tests |
| `test_protective_stop.py` | F8 3-tier SL, flatten trigger, position FLATTENING state | ~20 tests |
| `tests/integration/test_kill_switch_fanout.py` | F1 end-to-end Kafka failure → kill switch → recovery | 1 integration test (~10 assertions) |

Minimum: **158 new tests**. All must pass before Phase 8 is marked complete.

---

## 10. Infrastructure Changes

### Terraform

- `infra/terraform/modules/dynamodb/signal_processing.tf` (new): `signal-inbox` + `signal-outbox` tables, TTL enabled, IAM policies for risk_engine read/write
- `infra/terraform/modules/monitoring/main.tf` additions:
  - `RiskV1LagHigh` alarm: `KafkaConsumerLag/risk-v1/signals.enriched` > 500 for 3 consecutive 1m periods → SNS `alerts` topic
  - `OrphanPositionDetected` alarm: `QuantEmbrace/ExecutionEngine/OrphanPositionsCount` > 0 → SNS `kill_switch` topic
  - `ReconciliationHaltActive` alarm: `QuantEmbrace/RiskEngine/ReconciliationHaltActive` = 1 → SNS `alerts` topic

No new EC2 ASGs. No new Kafka topics. No schema registry changes.

---

## 11. Architecture Decisions

See `memory/decisions.md` → **ADR-015** (Phase 8 Production Hardening).

Key decisions recorded in ADR-015:
1. **SQLite for local outbox** (not DynamoDB): DynamoDB may itself be degraded when Kafka fails. SQLite is local, synchronous, and has zero network dependency.
2. **Per-endpoint budgets, not a separate "cancel queue"**: A separate queue would require state management across service restarts. The endpoint budget model integrates with the existing token bucket without new persistent state.
3. **Outbox model, not Kafka transactions**: MSK Serverless does not support exactly-once semantics (EOS) with `enable.idempotence=true` for the producer in all regions. The outbox pattern achieves the same guarantee at-least-once + DynamoDB dedup without Kafka transaction broker requirements.
4. **Operator approval required to clear reconciliation halt**: Automation could clear a halt based on incorrect assumptions about which source of truth to trust. Position drift after a broker incident requires human judgment.
5. **Orphan detector alerts, does not auto-flatten**: An operator may have intentionally removed a SL (manual risk management). Auto-flatten on orphan detection would be worse than the alternative in that case.

---

## 12. Pre-Approval Questions for Hari

Before implementation begins, confirm the following design choices:

| # | Question | Default assumption |
|---|----------|--------------------|
| Q1 | SQLite for local outbox: acceptable on the EC2 instance (ephemeral `/tmp` acceptable, or should we use the data volume)? | Ephemeral `/tmp` — outbox is for in-flight events only; Kafka recovery is expected within minutes |
| Q2 | `max_drift_pct` threshold for hard reconciliation stop: 5%? | 5% of position value — operator can tune via DynamoDB |
| Q3 | `orphan_grace_period_s` = 30s: is this long enough to avoid false positives on slow SL placement? | 30s covers normal SL retry chain (3 attempts × 200ms + processing); increase to 60s if 429s are frequent |
| Q4 | For F8 Tier 1: attempt Cover Order for *all* NSE equity instruments, or only for a configured list? | All instruments; exclude via `instruments.yaml` `co_ineligible: true` flag |
| Q5 | Inbox/outbox TTL of 2h: too short for overnight carry positions (US market)? | 2h is for signal processing durability only; carry position state lives in `positions` table (no TTL) |
