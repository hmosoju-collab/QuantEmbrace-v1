<!--
╔══════════════════════════════════════════════════════════════════════╗
║  ARCHIVED DOCUMENT — DO NOT USE AS IMPLEMENTATION REFERENCE         ║
║                                                                      ║
║  This document describes the SQS → Kafka migration process that      ║
║  has been COMPLETED. The system is now fully Kafka-native.           ║
║                                                                      ║
║  Source of truth for the current architecture: ARCHITECTURE.md       ║
║  Any SQS, PHASE2_KAFKA_ENABLED, or dual-path references below       ║
║  describe a historical state that no longer exists in the code.      ║
╚══════════════════════════════════════════════════════════════════════╝
-->

# QuantEmbrace Phase 2 — Implementation Plan
## Personal Trader Kafka Migration

**Version:** 1.0
**Date:** 2026-04-30
**Author:** Hari
**Status:** PENDING APPROVAL — no code written yet
**Architecture Ref:** `phase2_final_approved.md` v3.0 ✅ APPROVED
**Decision Ref:** `memory/decisions.md` ADR-011

---

## Overview

This document breaks Phase 2 into **10 sequenced implementation tasks** (P2-T01 through P2-T10).
Each task is self-contained, independently reviewable, and individually deployable.
No task is implemented without explicit owner approval.

**B1 Blocker Status:** ✅ COMPLETE — `fill_poller.py` already shipped (2026-04-30).

**Migration direction:** 4 services transition from SQS-based messaging to Kafka.
SQS queues remain in place until all services are migrated and validated.

```
Current State (Phase 1):
  data_ingestion → SQS → strategy_engine → SQS → risk_engine → SQS → execution_engine

Target State (Phase 2):
  data_ingestion → Kafka → strategy_engine → Kafka → risk_engine → Kafka → execution_engine
                     └──────────────────────────────────────────────┘
                           kill-switch propagation via risk.kill-switch
```

---

## Task Dependency Graph

```
P2-T01: Kafka Terraform Module
    └── P2-T02: Shared Kafka Client Library
            ├── P2-T03: Topic Setup Script + Settings
            │       ├── P2-T04: data_ingestion → KafkaTickPublisher
            │       ├── P2-T05: strategy_engine → Kafka consumer + publisher
            │       ├── P2-T06: risk_engine → Kafka consumer + publisher
            │       └── P2-T07: execution_engine → Kafka consumer + orders.events publisher
            │               ├── P2-T08: Kill Switch Kafka Integration (all services)
            │               ├── P2-T09: Consumer Lag + Bounded Replay (all services)
            │               └── P2-T10: Pre-Go-Live Validation Checklist
```

**Critical path:** T01 → T02 → T03 → T04,T05,T06,T07 (parallel) → T08,T09 (parallel) → T10

---

## P2-T01: Kafka MSK Serverless Terraform Module

**Priority:** P0 — blocks everything else
**Owner approval needed before:** writing any Terraform
**Files created:**
```
infra/terraform/modules/kafka/
    main.tf          — MSK Serverless cluster + security groups + VPC config
    iam.tf           — IAM policies for each EC2 service role (produce/consume per topic)
    topics.tf        — aws_msk_serverless_cluster + aws_kafka_topic resources
    variables.tf     — cluster_name, vpc_id, subnet_ids, env, tags
    outputs.tf       — bootstrap_brokers_sasl_iam, cluster_arn, topic names
```

### Design Spec

**Cluster:**
```hcl
resource "aws_msk_serverless_cluster" "quantembrace" {
  cluster_name = "quantembrace-${var.env}"

  vpc_config {
    subnet_ids         = var.private_subnet_ids   # 2 AZs minimum
    security_group_ids = [aws_security_group.kafka.id]
  }

  client_authentication {
    sasl { iam { enabled = true } }
  }
}
```

**Security Group rules:**
```
Inbound:  port 9098 (SASL/IAM) from EC2 service security group only
Outbound: unrestricted (MSK Serverless requires internet egress via NAT for TLS)
```

**IAM Policy — per-service granularity:**
```
data_ingestion IAM:    kafka:Produce on ticks.nse, ticks.us
strategy_engine IAM:   kafka:Consume from ticks.nse, ticks.us
                       kafka:Produce on signals.pending
risk_engine IAM:       kafka:Consume from ticks.nse, ticks.us, signals.pending, orders.events
                       kafka:Produce on signals.approved, orders.events, risk.kill-switch, ops.audit
execution_engine IAM:  kafka:Consume from signals.approved, risk.kill-switch
                       kafka:Produce on orders.events, ops.audit
```

**Outputs required by environments:**
```
bootstrap_brokers_sasl_iam  → injected as KAFKA_BOOTSTRAP_SERVERS env var per service
cluster_arn                 → used by monitoring module for MSK-level CloudWatch alarms
```

**Environment wiring:** Each environment module (`dev/`, `staging/`, `prod/`) calls the
kafka module and passes its outputs to the `ec2_services` module as env vars.

**Topic creation:** MSK Serverless does NOT support `auto.create.topics.enable`. Topics
are created by `P2-T03` (admin script), not by Terraform. This is intentional — topic
configuration (retention, partitions) is not managed by HCL because it changes
independently of infrastructure.

**Acceptance Criteria:**
- [ ] `terraform validate` passes in all three environments
- [ ] `terraform plan` shows only additive changes (no modifications to existing resources)
- [ ] MSK Serverless cluster is reachable from EC2 instances (connectivity test in T03)
- [ ] IAM policies are least-privilege — each service can only produce/consume its own topics
- [ ] `outputs.tf` exports `bootstrap_brokers_sasl_iam` for downstream module consumption

---

## P2-T02: Shared Kafka Client Library

**Priority:** P0 — required before any service migration
**Owner approval needed before:** writing any service-layer code
**Files created:**
```
services/shared/kafka/
    __init__.py
    producer.py      — KafkaProducer wrapper (acks=all, idempotent, delivery callback)
    consumer.py      — KafkaConsumer wrapper (manual commit, bounded replay, lag check)
    schemas.py       — Event dataclasses: TickEvent, SignalEvent, RiskDecisionEvent,
                       OrderEvent, KillSwitchEvent, AuditEvent
    kill_switch_listener.py  — Lightweight single-topic reader for risk.kill-switch
    config.py        — KafkaConfig pydantic model loaded from env vars
    exceptions.py    — KafkaWriteError, KafkaConsumerError (for kill-switch triggering)
```

**Also modified:**
```
services/shared/config/settings.py  — add KafkaConfig sub-section to AppSettings
```

### Design Spec

**`KafkaConfig` (added to `AWSConfig` or as top-level sub-config):**
```python
class KafkaConfig(BaseSettings):
    model_config = {"env_prefix": "KAFKA_"}

    bootstrap_servers: str = Field(
        ...,
        description="MSK Serverless SASL/IAM bootstrap endpoint"
    )
    producer_acks: str = Field(default="all")
    enable_idempotence: bool = Field(default=True)
    max_in_flight: int = Field(default=1)
    retries: int = Field(default=5)
    retry_backoff_ms: int = Field(default=200)
    compression_type: str = Field(default="lz4")
    batch_size: int = Field(default=4096)
    linger_ms: int = Field(default=5)
    max_block_ms: int = Field(default=5000)
    delivery_timeout_ms: int = Field(default=30000)

    # Consumer
    enable_auto_commit: bool = Field(default=False)   # NEVER True in production
    auto_offset_reset: str = Field(default="earliest")  # subject to bounded replay
    isolation_level: str = Field(default="read_committed")
    max_poll_interval_ms: int = Field(default=30000)
    session_timeout_ms: int = Field(default=10000)
    heartbeat_interval_ms: int = Field(default=3000)
    fetch_max_wait_ms: int = Field(default=100)
    max_poll_records: int = Field(default=50)

    # Bounded replay windows (seconds) — per approved design §9
    replay_window_ticks_seconds: int = Field(default=300)       # 5 min
    replay_window_signals_seconds: int = Field(default=300)     # 5 min
    replay_window_fills_seconds: int = Field(default=1800)      # 30 min
    replay_window_execution_seconds: int = Field(default=1800)  # 30 min
```

**`QuantEmbraceProducer` design:**
```python
class QuantEmbraceProducer:
    """
    Thread-safe Kafka producer wrapper.

    Key behaviors:
      - acks=all, enable.idempotence=true, max.in.flight=1 (no silent data loss)
      - delivery_callback tracks consecutive failures — after 3 on critical topics,
        triggers kill_switch (caller passes a kill_switch_fn callback)
      - Synchronous flush available for critical messages (signals, orders)
      - topic argument is the full topic name (no prefix logic here)
    """
    def __init__(self, config: KafkaConfig, kill_switch_fn: Callable = None): ...
    async def publish(self, topic: str, key: str, value: dict) -> None: ...
    async def publish_sync(self, topic: str, key: str, value: dict) -> None: ...
    async def close(self) -> None: ...
    def _on_delivery(self, err, msg) -> None:
        # Count consecutive failures on critical topics
        # At 3 failures: call kill_switch_fn("kafka_write_failure")
        ...
```

**`QuantEmbraceConsumer` design:**
```python
class QuantEmbraceConsumer:
    """
    Kafka consumer with manual commit and bounded replay.

    Key behaviors:
      - Manual offset commit after successful processing (enable.auto.commit=false)
      - On startup: check last commit timestamp vs now. If gap > MAX_REPLAY_WINDOW,
        seek to now() - MAX_REPLAY_WINDOW (prevents 24h replays)
      - Lag check on every poll: compute message_age_ms from published_time field.
        Return (message, LagTier) to caller — caller decides what to do.
      - Consumer group name is fixed at construction time (canonical names only)
    """
    def __init__(self, config: KafkaConfig, group_id: str, topics: list[str],
                 replay_window_seconds: int): ...
    async def start(self) -> None:  # applies bounded replay seek
    async def poll(self) -> list[tuple[dict, LagTier]]: ...
    async def commit(self) -> None: ...
    async def close(self) -> None: ...

class LagTier(str, Enum):
    NORMAL   = "NORMAL"    # < 500ms
    DEGRADED = "DEGRADED"  # 500ms – 3000ms
    HALT     = "HALT"      # ≥ 3000ms
```

**`schemas.py` — every Kafka message type as a frozen dataclass:**
```python
@dataclass(frozen=True)
class TickEvent:
    event_id: str
    trace_id: str
    event_type: str          # "TICK"
    schema_version: str      # "3.0"
    source: str
    published_time: str
    instrument_id: str
    market: str
    session_id: str
    sequence_id: int
    exchange_sequence: int
    exchange_time: str
    ltp: float
    open: float; high: float; low: float; close: float
    volume: int
    bid: float; ask: float
    tick_type: str

@dataclass(frozen=True)
class SignalEvent:
    event_id: str; trace_id: str; event_type: str; schema_version: str
    source: str; published_time: str; signal_id: str; strategy_id: str
    timeframe: str; instrument_id: str; market: str; direction: str
    conviction: float; signal_price: float; target_price: float; stop_loss: float
    quantity: int; order_type: str; signal_time: str; expires_at: str
    tick_event_id: str; tick_sequence_id: int

@dataclass(frozen=True)
class RiskDecisionEvent:
    event_id: str; trace_id: str; event_type: str; schema_version: str
    source: str; published_time: str; risk_decision_id: str; signal_id: str
    instrument_id: str; approved_quantity: int; approved_price: float
    signal_age_ms: int; decision_time: str; risk_checks_passed: list[str]

@dataclass(frozen=True)
class OrderEvent:
    event_id: str; trace_id: str; event_type: str; schema_version: str
    source: str; published_time: str; order_id: str; signal_id: str
    risk_decision_id: str; broker_order_id: str; broker: str
    instrument_id: str; market: str; direction: str; quantity: int
    order_type: str; limit_price: Optional[float]; submitted_at: str
    fill_id: Optional[str]; filled_quantity: Optional[int]
    avg_fill_price: Optional[float]; slippage_bps: Optional[float]
    fill_time: Optional[str]; fill_source: Optional[str]
    cancel_reason: Optional[str]; reject_reason: Optional[str]

@dataclass(frozen=True)
class KillSwitchEvent:
    event_id: str; event_type: str; schema_version: str
    source: str; published_time: str; scope: str; reason: str
    activated_by: str; activation_time: str

@dataclass(frozen=True)
class AuditEvent:
    event_id: str; trace_id: str; event_type: str; schema_version: str
    source: str; published_time: str
    payload: dict  # full event being audited
```

**`KillSwitchListener` design:**
```python
class KillSwitchListener:
    """
    Lightweight consumer that keeps in-memory kill switch state current.

    On start: seek to latest offset (no replay needed — kill switch state
    is re-read from DynamoDB on startup, this listener tracks changes only).
    Runs as a background asyncio task inside execution_engine and strategy_engine.
    On KILL_SWITCH_ACTIVATE: updates in-memory KillSwitch state immediately.
    On KILL_SWITCH_DEACTIVATE: updates in-memory state.
    Propagation latency: 30–100ms (single-partition topic).
    """
    def __init__(self, config: KafkaConfig, kill_switch: KillSwitch): ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

**Acceptance Criteria:**
- [ ] `KafkaConfig` loads from env vars; missing KAFKA_BOOTSTRAP_SERVERS → clear error
- [ ] `QuantEmbraceProducer`: delivery callback counts consecutive failures correctly, triggers kill switch at 3
- [ ] `QuantEmbraceConsumer`: bounded replay seek works — tested with mock timestamps
- [ ] `QuantEmbraceConsumer`: lag tier computed correctly for all 3 tiers
- [ ] All schema dataclasses match `phase2_final_approved.md §4` exactly (field names, types)
- [ ] `KillSwitchListener`: reads last message on connect, applies state immediately
- [ ] Unit tests: `tests/unit/test_kafka_producer.py`, `tests/unit/test_kafka_consumer.py`
- [ ] No production code calls `enable.auto.commit=True` anywhere

---

## P2-T03: Topic Setup Script + Settings Extension

**Priority:** P0 — topics must exist before any producer runs
**Owner approval needed before:** running against real MSK
**Depends on:** T01 (cluster exists), T02 (KafkaConfig exists)
**Files created/modified:**
```
scripts/kafka/create_topics.py      — admin client creates 7 topics with exact config
scripts/kafka/verify_topics.py      — validates topics exist with correct partition counts
scripts/kafka/list_consumer_groups.py — shows lag for all 3 consumer groups
```

### Design Spec

**`create_topics.py` — exact topic specs (must match approved design):**
```python
TOPICS = [
    TopicSpec(name="ticks.nse",        partitions=4, retention_ms=86_400_000),   # 24h
    TopicSpec(name="ticks.us",         partitions=2, retention_ms=86_400_000),   # 24h
    TopicSpec(name="signals.pending",  partitions=2, retention_ms=3_600_000),    # 1h
    TopicSpec(name="signals.approved", partitions=2, retention_ms=1_800_000),    # 30m
    TopicSpec(name="orders.events",    partitions=4, retention_ms=604_800_000),  # 7d
    TopicSpec(name="risk.kill-switch", partitions=1, retention_ms=2_592_000_000),# 30d
    TopicSpec(name="ops.audit",        partitions=2, retention_ms=7_776_000_000),# 90d
]
# Additional config for all topics:
#   unclean.leader.election.enable=false  (never allow data loss via unclean election)
#   min.insync.replicas=1                 (MSK Serverless manages replication)
#   message.timestamp.type=CreateTime     (producer sets timestamp, not broker)
```

**`verify_topics.py` — pre-go-live gate:**
Checks each topic exists, has correct partition count, correct retention, and at least
one active leader partition. Returns exit code 0 on success, 1 on any mismatch.
Used as a deployment preflight check.

**Canonical consumer group names (must be hardcoded, never from env vars):**
```python
# In shared/kafka/config.py — constants, not settings
CONSUMER_GROUP_STRATEGY  = "strategy-v1"
CONSUMER_GROUP_RISK      = "risk-v1"
CONSUMER_GROUP_EXECUTION = "execution-v1"
```

**Why hardcoded:** Consumer group names are part of the Kafka offset commit history.
A renamed group loses all committed offsets and replays from the beginning. These names
must never change after first deployment. Not env vars, not settings — constants.

**Acceptance Criteria:**
- [ ] `create_topics.py` is idempotent (running twice does not error)
- [ ] `verify_topics.py` exits 0 after topics are created with correct config
- [ ] All 7 topics listed with correct partition counts in `kafka-topics.sh --list`
- [ ] `list_consumer_groups.py` shows 0 lag for all groups after a connectivity test

---

## P2-T04: data_ingestion — Kafka Tick Publisher

**Priority:** P1
**Owner approval needed before:** implementation
**Depends on:** T02, T03
**Files created:**
```
services/data_ingestion/publishers/kafka_publisher.py  — KafkaTickPublisher
```
**Files modified:**
```
services/data_ingestion/processors/tick_processor.py   — swap publisher at construction
services/data_ingestion/service.py                     — instantiate KafkaTickPublisher
```
**Files unchanged (Phase 1 SQS remains):**
```
services/data_ingestion/publishers/sqs_publisher.py    — kept but not used (feature flag)
```

### Design Spec

**Migration strategy:** Feature flag `KAFKA_ENABLED=true/false` in environment.
When `false`: existing `SQSTickPublisher` is used (backward compatible, zero regression).
When `true`: `KafkaTickPublisher` is used. This allows a gradual rollout.

**`KafkaTickPublisher` contract:**
```python
class KafkaTickPublisher:
    """
    Publishes normalized ticks to Kafka ticks.nse and ticks.us topics.

    Key behaviors:
      - Assigns trace_id = uuid4() on each tick (this is where trace_id originates)
      - Assigns sequence_id = monotonically increasing integer (per-session, per-market)
      - Deduplication: exchange_sequence LRU cache (2000 entries per instrument)
        → if exchange_sequence seen before, skip publish (WebSocket reconnect dedup)
      - Partition key = instrument_id (ensures same instrument always goes same partition)
      - Topic routing: NSE ticks → ticks.nse, US ticks → ticks.us
      - Message body = TickEvent dataclass serialized to JSON
    """
    async def publish_tick(self, tick: NormalizedTick) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

**Tick schema mapping (NormalizedTick → TickEvent):**
```
tick.symbol          → instrument_id (format: "NSE:RELIANCE" or "US:AAPL")
tick.exchange_time   → exchange_time (not datetime.utcnow() — already fixed in Phase 1)
uuid4()              → trace_id      (assigned here, propagated unchanged downstream)
next_sequence_id()   → sequence_id  (monotonic per-session counter)
tick.exchange_seq    → exchange_sequence (for dedup LRU cache check)
```

**Deduplication logic:**
```python
_dedup_cache: dict[str, LRUCache] = {}  # instrument_id → LRU(2000)

def should_publish(instrument_id: str, exchange_sequence: int) -> bool:
    cache = _dedup_cache.setdefault(instrument_id, LRUCache(maxsize=2000))
    if exchange_sequence in cache:
        return False  # Duplicate from WebSocket reconnect
    cache[exchange_sequence] = True
    return True
```

**DynamoDB latest-prices write:** Unchanged from Phase 1 — still writes to
`quantembrace-latest-prices` table for risk engine margin validator. This is NOT
migrated to Kafka (price state belongs in DynamoDB, not as a Kafka event stream).

**Acceptance Criteria:**
- [ ] `KAFKA_ENABLED=false` → `SQSTickPublisher` used → zero regression on existing tests
- [ ] `KAFKA_ENABLED=true` → ticks appear in `ticks.nse` / `ticks.us` topics (verify with `kcat`)
- [ ] trace_id is a valid UUID4, unique per tick
- [ ] exchange_sequence dedup: sending same tick twice → published once (unit test)
- [ ] partition key = instrument_id (verify with `kcat -e -t ticks.nse -f '%k\n'`)
- [ ] Message body validates against `TickEvent` dataclass schema

---

## P2-T05: strategy_engine — Kafka Consumer + Signal Publisher

**Priority:** P1
**Owner approval needed before:** implementation
**Depends on:** T02, T03, T04 (ticks must be in Kafka before strategy can consume them)
**Files modified:**
```
services/strategy_engine/service.py    — replace SQS consumer with Kafka consumer
```
**Files created:**
```
services/strategy_engine/kafka_consumer.py  — strategy Kafka consumer wrapper
```
**Files unchanged:**
```
services/strategy_engine/service.py (SQS path) — kept behind KAFKA_ENABLED flag
```

### Design Spec

**What changes inside strategy_engine:**
```
BEFORE: SQS receive_message → parse tick → run strategies → SQS send_message (signal)
AFTER:  Kafka poll (ticks.nse + ticks.us) → parse TickEvent → run strategies → Kafka publish (signals.pending)
```

**Consumer behavior:**
```
Consumer group:  strategy-v1  (canonical — must match CONSUMER_GROUP_STRATEGY constant)
Topics:          ticks.nse, ticks.us
Replay window:   5 minutes (KAFKA_REPLAY_WINDOW_TICKS_SECONDS=300)
Lag policy:
  NORMAL:   run strategies as-is, publish signal with full conviction
  DEGRADED: run strategies, multiply conviction × 0.5, add "lag_degraded": true to SignalEvent
  HALT:     consume and discard ticks (drain backlog), do not run strategies
            Resume normal when 10 consecutive ticks are NORMAL-tier

Commit: manual, after signal published (or after discarding HALT-tier tick)
```

**Signal publishing:**
```
Topic: signals.pending
Key:   instrument_id  (ensures signals for same instrument are ordered)
Body:  SignalEvent dataclass

signal_id computation (canonical — must use shared/kafka/signal_id.py helper):
  sha256(strategy_id + "|" + instrument_id + "|" + direction + "|" + timeframe + "|" + str(tick_sequence_id))[:32]

expires_at = signal_time + 30 seconds (hardcoded — per approved design)

trace_id: copied unchanged from TickEvent.trace_id (never generate a new one)
```

**Signal expiry enforcement:**
The strategy engine sets `expires_at`. It does NOT check it — that is the risk engine's job.
The strategy engine must not re-check expiry before publishing. This is by design.

**Acceptance Criteria:**
- [ ] `KAFKA_ENABLED=false` → SQS path unchanged, all existing tests pass
- [ ] `KAFKA_ENABLED=true` → signals appear in `signals.pending` (verify with `kcat`)
- [ ] signal_id is deterministic: same inputs → same signal_id (unit test)
- [ ] trace_id in SignalEvent matches trace_id from source TickEvent (never a new UUID)
- [ ] expires_at = signal_time + 30s exactly
- [ ] DEGRADED-tier signal has `conviction *= 0.5` AND `lag_degraded: true`
- [ ] HALT-tier: no signals published (verify with message count check)
- [ ] Bounded replay: if last commit was 10 minutes ago, seek to now()-5min (unit test)

---

## P2-T06: risk_engine — Kafka Consumer + Multi-Topic Publisher

**Priority:** P1
**Owner approval needed before:** implementation
**Depends on:** T02, T03, T05 (signals.pending must exist)
**This is the most complex migration — risk engine reads 3 topics, writes 4**

**Files modified:**
```
services/risk_engine/service.py           — replace SQS with Kafka consumer + publishers
```
**Files created:**
```
services/risk_engine/kafka_consumer.py   — risk Kafka consumer (3-topic subscription)
services/risk_engine/position_tracker.py — tracks position state from orders.events fills
```

### Design Spec

**What changes inside risk_engine:**
```
BEFORE: SQS receive_message (signals) → validate → SQS send_message (approved signals)
AFTER:  Kafka poll (3 topics) → route by event_type → validate signals / track fills
                              → publish to signals.approved / orders.events / risk.kill-switch / ops.audit
```

**Consumer behavior:**
```
Consumer group:  risk-v1  (canonical — must match CONSUMER_GROUP_RISK constant)
Topics:          ticks.nse, ticks.us     → price monitoring (tick freshness check)
                 signals.pending          → signal validation
                 orders.events            → fill/cancel processing for position state

Replay windows:
  ticks.nse / ticks.us:     5 minutes
  signals.pending:          5 minutes (all will be expired — but audit trail matters)
  orders.events (fills):    30 minutes (MUST replay fills to restore position state)

Consumer commit: after each message processed
```

**Message routing by event_type:**
```python
async def _handle_message(self, event: dict, lag_tier: LagTier) -> None:
    event_type = event.get("event_type")

    if event_type == "TICK":
        await self._update_tick_freshness(event)

    elif event_type == "SIGNAL":
        if lag_tier == LagTier.HALT:
            await self._publish_audit(event, "SIGNAL_DISCARDED_LAG_HALT")
            return
        await self._validate_and_route_signal(event, lag_tier)

    elif event_type in ("ORDER_FILLED", "ORDER_CANCELLED", "ORDER_REJECTED"):
        await self._update_position_from_fill(event)
```

**Signal validation path (unchanged from Phase 1, only transport changes):**
```
1. kill_switch.is_trading_allowed(instrument_id, market)  — in-memory, 0ms
2. signal_age_validator.validate(signal)                  — expires_at check
3. position_validator.validate(signal)                    — DynamoDB position check
4. loss_validator.validate(signal)                        — DynamoDB daily P&L check
5. exposure_validator.validate(signal)                    — DynamoDB exposure check
6. margin_validator.validate(signal)                      — DynamoDB margin check
7. slippage_validator.validate(signal)                    — market impact check

On APPROVED: publish to signals.approved + ops.audit
On REJECTED: write to DynamoDB risk-decisions table + ops.audit (NOT to Kafka signals.rejected)
```

**Fill tracking (replaces SQS-based position updates):**
```
On ORDER_FILLED event (from orders.events):
  1. DynamoDB conditional write on fill_id (same idempotency gate as fill_poller)
     → if already processed: skip (fill_poller may have already written this)
  2. Update positions table (apply_fill_to_position equivalent)
  3. Update NAV snapshot

This means the risk engine has its OWN fill idempotency gate, independent of
the fill_poller's gate. Two gates for the same fill are not a problem — both
check attribute_not_exists(fill_id). Only one will win. The other skips cleanly.
```

**Automatic kill switch — NEW in Phase 2:**
```
Trigger 6 (orders.events consumer lag > 100 messages for 60s):
  Check: every 60s, if consumer.lag("orders.events") > 100:
    publish to risk.kill-switch: {scope: "GLOBAL", reason: "orders_events_lag_too_high"}
    call DynamoDB kill switch as fallback

Trigger 7 (Kafka write failure — 3 consecutive on signals.approved):
  Handled by QuantEmbraceProducer delivery callback (implemented in T02)
  After 3 consecutive failures: kill_switch_fn("kafka_write_failure") is called
```

**`risk_decision_id` format:**
```python
risk_decision_id = f"RD-{date_str}-{signal_id[:8]}"
# Example: "RD-20260430-a3f9b2c1"
```

**Acceptance Criteria:**
- [ ] `KAFKA_ENABLED=false` → SQS path unchanged, all existing tests pass
- [ ] `KAFKA_ENABLED=true` → approved signals appear in `signals.approved` (verify with `kcat`)
- [ ] Rejected signals NOT in any Kafka topic — only in DynamoDB + ops.audit
- [ ] Fill events from `orders.events` update position state correctly (integration test)
- [ ] Fill dedup: same fill_id processed twice → idempotent, position updated once
- [ ] trace_id copied unchanged from signal event to risk decision event
- [ ] risk_decision_id matches format: `RD-{date}-{signal_id[:8]}`
- [ ] Kill switch trigger 6: lag > 100 msgs for 60s → kill switch fires (unit test with mock)
- [ ] Kill switch trigger 7: 3 consecutive delivery failures → kill switch fires (unit test)
- [ ] Bounded replay: fills replay up to 30 minutes; ticks/signals replay up to 5 minutes

---

## P2-T07: execution_engine — Kafka Consumer + orders.events Publisher

**Priority:** P1
**Owner approval needed before:** implementation
**Depends on:** T02, T03, T06 (signals.approved must exist)
**Files modified:**
```
services/execution_engine/service.py                 — replace SQS with Kafka consumer
services/execution_engine/polling/fill_poller.py     — add Kafka publish in _handle_fill()
```
**Files created:**
```
services/execution_engine/kafka_consumer.py          — execution Kafka consumer
```

### Design Spec

**What changes inside execution_engine:**
```
BEFORE: SQS receive_message (signals.approved) → execute_approved_signal() → (no event publish)
AFTER:  Kafka poll (signals.approved) → execute_approved_signal() → publish to orders.events
                                      + kill_switch_listener reads risk.kill-switch
```

**Consumer behavior:**
```
Consumer group:  execution-v1  (canonical — must match CONSUMER_GROUP_EXECUTION constant)
Topic:           signals.approved
Replay window:   30 minutes (signals may still be within expires_at window)

Lag policy:
  NORMAL:   execute signal normally
  DEGRADED: re-check kill switch in-memory before placing order (extra safety)
  HALT:     consume and drain — all signals will expire (their expires_at < now)
            No orders placed. Log all drained signals to ops.audit.

Commit: after broker call completes (success or terminal failure)
```

**Signal expiry check (3rd layer after risk engine):**
```python
async def _handle_approved_signal(self, event: RiskDecisionEvent) -> None:
    # Layer 3 expiry check (risk engine is layer 2, strategy engine is layer 1 via expires_at)
    if datetime.fromisoformat(event.expires_at) < datetime.now(timezone.utc):
        logger.info("execution.signal_expired_before_execution", signal_id=event.signal_id)
        await self._publish_order_event("ORDER_REJECTED", reject_reason="SIGNAL_EXPIRED")
        return

    # Proceed to execute
    await self.execute_approved_signal(...)
```

**orders.events publishing (fill_poller.py — Phase 2 TODO wired):**
After the DynamoDB idempotency gate passes (fill is new), publish to `orders.events`:
```python
await self._producer.publish(
    topic="orders.events",
    key=order.symbol,          # instrument routing key
    value=asdict(OrderEvent(
        event_id=str(uuid4()),
        trace_id=order.trace_id or "",
        event_type="ORDER_FILLED",
        schema_version="3.0",
        source="execution_engine",
        published_time=utc_iso(),
        order_id=order.order_id,
        ...
        fill_id=fill_id,
        filled_quantity=filled_quantity,
        avg_fill_price=avg_fill_price,
        fill_source="zerodha_polling",
        ...
    ))
)
```

**ORDER_SUBMITTED event (on broker call success):**
Published immediately after `broker.place_order()` returns successfully:
```python
# In execute_approved_signal(), after record_order():
await self._producer.publish_sync(
    topic="orders.events",
    key=order_request.symbol,
    value=asdict(OrderEvent(event_type="ORDER_SUBMITTED", ...))
)
```

**Kill switch listener:** Runs as a 5th coroutine in `asyncio.gather` alongside
fill_poller, margin_refresh, signal consumer, and MIS square-off.

**Acceptance Criteria:**
- [ ] `KAFKA_ENABLED=false` → SQS path unchanged, all existing tests pass
- [ ] `KAFKA_ENABLED=true` → ORDER_SUBMITTED appears in `orders.events` after each order (verify with `kcat`)
- [ ] `KAFKA_ENABLED=true` → ORDER_FILLED appears after fill detection (verify with `kcat`)
- [ ] Signal expiry check prevents order placement for expired signals (unit test)
- [ ] trace_id copied unchanged from RiskDecisionEvent through to OrderEvent
- [ ] Kill switch listener: KILL_SWITCH_ACTIVATE received → in-memory flag set → next order rejected
- [ ] Bounded replay: signals older than 30 minutes are NOT executed (expires_at gates them)
- [ ] Commit happens after broker call, not before

---

## P2-T08: Kill Switch Kafka Integration (All Services)

**Priority:** P0 (safety-critical, must be done before go-live)
**Owner approval needed before:** implementation
**Depends on:** T02, T03, T04–T07 (all services must use Kafka before kill-switch wiring)
**Files modified:**
```
services/risk_engine/killswitch/killswitch.py    — add Kafka publish to kill switch activation
services/execution_engine/service.py             — add KillSwitchListener as coroutine
services/strategy_engine/service.py              — add KillSwitchListener as coroutine
```

### Design Spec

**Kill switch activation flow (risk engine side):**
```python
async def activate(self, scope: str, reason: str, activated_by: str) -> None:
    # Step 1: DynamoDB write (Phase 1 path — keep)
    await self._write_to_dynamo(scope, reason, activated_by)

    # Step 2: Kafka publish to risk.kill-switch (NEW in Phase 2)
    # Use a separate producer with acks=1 (not acks=all) and max.block.ms=1000
    # so a Kafka failure does not prevent the kill switch from activating
    await self._kill_switch_producer.publish_sync(
        topic="risk.kill-switch",
        key="GLOBAL",
        value=asdict(KillSwitchEvent(
            event_id=str(uuid4()),
            event_type="KILL_SWITCH_ACTIVATE",
            scope=scope,
            reason=reason,
            activated_by=activated_by,
            activation_time=utc_iso(),
            ...
        ))
    )
    # If Kafka publish fails: log ERROR but do NOT prevent activation.
    # DynamoDB write is the authoritative record.
```

**KillSwitchListener in execution_engine and strategy_engine:**
```python
# execution_engine/service.py start() — add 5th coroutine:
self._kill_switch_listener = KillSwitchListener(
    config=kafka_config,
    kill_switch=self._kill_switch,   # in-memory KillSwitch object
)
await asyncio.gather(
    self._consume_approved_signals(),
    self._margin_refresh_loop(),
    mis_manager.run(),
    self._fill_poller.start(),
    self._kill_switch_listener.start(),  # NEW
)
```

**Propagation timing requirement:**
```
Trigger → DynamoDB write (~15ms) → Kafka publish (~10ms) →
  → execution_engine listener receives (~30ms) → in-memory flag set (~0ms)
Total: < 200ms
SLA: < 500ms guaranteed
```

**Scoped kill switch evaluation (in-memory, unchanged from Phase 1):**
```python
def is_trading_allowed(instrument_id: str, market: str) -> bool:
    if self.global_halt:                          return False
    if market in self.halted_markets:             return False
    if instrument_id in self.halted_instruments:  return False
    return True
```

**Note:** DEACTIVATION also publishes to `risk.kill-switch`. All listeners receive
KILL_SWITCH_DEACTIVATE and clear their in-memory halts accordingly.

**Acceptance Criteria:**
- [ ] Activating kill switch via CLI → message appears in `risk.kill-switch` topic (verify with `kcat`)
- [ ] execution_engine receives activation → new signals are rejected within 200ms
- [ ] strategy_engine receives activation → new signals are NOT published within 200ms
- [ ] Kill switch survives Kafka failure: DynamoDB write succeeds even if Kafka is down
- [ ] Deactivation propagates correctly (services resume trading)
- [ ] Scoped kill switch (NSE only): US signals still execute while NSE is halted
- [ ] End-to-end test: activate global → attempt NSE signal → rejected; attempt US signal → rejected

---

## P2-T09: Consumer Lag Policy + Bounded Replay (All Services)

**Priority:** P0 (correctness, must be done before go-live)
**Owner approval needed before:** implementation
**Depends on:** T04–T08 (all service Kafka integrations complete)
**Files modified:**
```
services/shared/kafka/consumer.py    — bounded replay logic (in T02, finalized here)
services/strategy_engine/service.py  — 3-tier lag policy actions
services/risk_engine/service.py      — 3-tier lag policy actions
services/execution_engine/service.py — 3-tier lag policy actions
```

### Design Spec

**Bounded replay — per service (implemented in `QuantEmbraceConsumer.start()`):**
```python
async def start(self) -> None:
    assigned_partitions = self._consumer.assignment()
    for partition in assigned_partitions:
        committed = self._consumer.committed(partition)
        if committed is None:
            continue  # first start, no commit history
        last_commit_epoch_ms = committed.offset_timestamp_ms()  # confluent-kafka API
        gap_seconds = (time.time() * 1000 - last_commit_epoch_ms) / 1000
        if gap_seconds > self.replay_window_seconds:
            seek_to = int(time.time() * 1000) - (self.replay_window_seconds * 1000)
            self._consumer.seek_to_timestamp(partition, seek_to)
            logger.warning(
                "consumer.bounded_replay",
                topic=partition.topic,
                gap_seconds=gap_seconds,
                replay_window_seconds=self.replay_window_seconds,
            )
```

**3-tier lag policy — per service actions:**

Strategy engine (`strategy-v1` consuming `ticks.nse` / `ticks.us`):
```python
if lag_tier == LagTier.NORMAL:
    signals = run_strategies(tick)
    await publish_signals(signals, conviction_multiplier=1.0)

elif lag_tier == LagTier.DEGRADED:
    signals = run_strategies(tick)
    await publish_signals(signals, conviction_multiplier=0.5, lag_degraded=True)

elif lag_tier == LagTier.HALT:
    # consume and discard — drain the backlog
    await self._kafka_consumer.commit()
    self._halt_consecutive += 1
    # Resume check: 10 consecutive NORMAL messages after entering HALT
```

Risk engine (`risk-v1`):
```python
if lag_tier == LagTier.HALT and event_type == "SIGNAL":
    await self._publish_audit(event, "SIGNAL_DISCARDED_LAG_HALT")
    await self._kafka_consumer.commit()
    return
# All other event types (TICK, ORDER_*) processed regardless of lag tier
```

Execution engine (`execution-v1`):
```python
if lag_tier == LagTier.DEGRADED:
    self._kill_switch.is_trading_allowed()  # extra check (already in hot path, no-op)
elif lag_tier == LagTier.HALT:
    # All signals drain — they will be rejected by expires_at check anyway
    await self._publish_order_event("ORDER_REJECTED", reject_reason="CONSUMER_LAG_HALT")
    await self._kafka_consumer.commit()
    return
```

**CloudWatch alarm for sustained HALT:**
```
Alarm: KAFKA_CONSUMER_LAG_TIER3
  MetricFilter: filter for "lag_tier=HALT" in CloudWatch logs
  Period: 60 seconds
  Threshold: count > 10 messages in 60s
  Action: SNS → email/phone (P1 alert — check within 1 hour)
```

**Acceptance Criteria:**
- [ ] Bounded replay: consumer that was down 10 minutes only replays last 5 minutes of ticks (unit test with mock)
- [ ] Bounded replay: execution engine that was down 45 minutes replays last 30 minutes of signals (unit test)
- [ ] DEGRADED-tier: strategy engine signals have `conviction *= 0.5` and `lag_degraded: true`
- [ ] HALT-tier: no new orders placed (verify with order count check in staging)
- [ ] Lag tier computed from `published_time` field in message body (not Kafka broker lag)
- [ ] Resume from HALT: 10 consecutive NORMAL-tier messages → strategy resumes signals

---

## P2-T10: Pre-Go-Live Validation Checklist

**Priority:** P0 — must pass 100% before any live trading
**Owner approval needed before:** any real Zerodha/Alpaca API calls with real capital
**Depends on:** T01–T09 ALL complete
**This is a verification task, not an implementation task**

### Checklist (10 items — must all pass)

```
INFRA
[ ] 1. All 7 Kafka topics exist with correct partition counts and retention
       verify_topics.py exits 0 in staging environment

[ ] 2. All 3 consumer groups visible in staging
       list_consumer_groups.py shows strategy-v1, risk-v1, execution-v1

[ ] 3. IAM policies verified — each service can ONLY access its approved topics
       Test: data_ingestion trying to write to signals.approved → AccessDenied

CORRECTNESS
[ ] 4. End-to-end tick → signal → approval → order → fill trace
       Single instrument, paper trading, verify trace_id is identical across all 5 events

[ ] 5. Kill switch propagates to all services within 500ms
       Activate via CLI → measure time until execution_engine rejects next signal

[ ] 6. Signal deduplication: same signal_id processed twice → one order, not two
       Replay same signals.pending message → DynamoDB gate blocks second order

[ ] 7. Fill deduplication: same fill detected by polling twice → position updated once
       Call _handle_fill() with same fill_id twice → second call returns immediately (gate)

RECOVERY
[ ] 8. Execution engine restart: PLACED orders reconciled from broker on startup
       Kill execution_engine mid-trade → restart → verify order state is correct

[ ] 9. Risk engine restart: fill state restored from orders.events replay
       Kill risk_engine → restart → verify position state matches DynamoDB after 30min replay

[ ] 10. Kill switch survives Kafka outage
        Stop MSK Serverless → activate kill switch via CLI
        → DynamoDB write succeeds → services detect via DynamoDB poll → trading halts
```

**Gate:** All 10 items must pass in staging before production deployment.
**How to run:** `python scripts/kafka/validate_pre_golive.py --env staging`
(This script runs items 1–3 automatically; items 4–10 are manual with guided prompts.)

---

## Summary Table

| Task | What | Priority | Depends On | Files Changed |
|------|------|----------|------------|---------------|
| P2-T01 | Kafka MSK Serverless Terraform | P0 | — | `infra/terraform/modules/kafka/` |
| P2-T02 | Shared Kafka client library | P0 | T01 | `services/shared/kafka/` |
| P2-T03 | Topic setup script + settings | P0 | T01,T02 | `scripts/kafka/`, `settings.py` |
| P2-T04 | data_ingestion → KafkaTickPublisher | P1 | T02,T03 | `publishers/kafka_publisher.py` |
| P2-T05 | strategy_engine → Kafka | P1 | T02,T03,T04 | `strategy_engine/service.py` |
| P2-T06 | risk_engine → Kafka (3 topics) | P1 | T02,T03,T05 | `risk_engine/service.py` |
| P2-T07 | execution_engine → Kafka | P1 | T02,T03,T06 | `execution_engine/service.py`, `fill_poller.py` |
| P2-T08 | Kill switch Kafka integration | P0 | T04–T07 | `killswitch.py`, service files |
| P2-T09 | Consumer lag + bounded replay | P0 | T04–T08 | `shared/kafka/consumer.py`, service files |
| P2-T10 | Pre-go-live validation | P0 | T01–T09 | `scripts/kafka/validate_pre_golive.py` |

**Total new/modified Python files:** ~15
**New Terraform resources:** 1 MSK Serverless cluster, ~10 IAM policies, 7 topics
**Estimated implementation order:** T01 → T02 → T03 → (T04, T05, T06, T07 in parallel if desired) → T08 → T09 → T10

---

## What Does NOT Change in Phase 2

These are explicitly out of scope — do not modify them:

| Component | Reason |
|-----------|--------|
| `execution_engine/polling/fill_poller.py` | Already implemented (BLOCKER B1 done) |
| `execution_engine/orders/order_manager.py` | Core logic unchanged; only transport changes |
| `risk_engine/validators/*` | All 6 validators unchanged; only transport changes |
| `risk_engine/killswitch/killswitch.py` | Extended (Kafka publish added) but not rewritten |
| `execution_engine/auth/zerodha_auth.py` | Unchanged |
| `execution_engine/brokers/*` | Unchanged |
| `infra/terraform/modules/dynamodb/` | No new tables needed (fills table added in B1 settings) |
| `infra/terraform/modules/monitoring/` | CloudWatch alarms extended in T09; not rewritten |
| All SQS Terraform resources | Kept in place until full Kafka migration validated |
| `.github/workflows/*` | CI/CD pipeline unchanged |

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| MSK Serverless unavailable during live trading | Low | HIGH | DynamoDB fallback in T08 (kill switch via DynamoDB if Kafka down) |
| Consumer group name changed after deploy | Low | HIGH | Names are constants, not env vars (T03 design) |
| Bounded replay replays fills twice | Low | MEDIUM | Idempotency gate on fill_id (T02+T07) |
| KAFKA_ENABLED=true deployed before topics exist | Medium | HIGH | T10 preflight check: verify_topics.py must exit 0 |
| trace_id not propagated correctly | Medium | LOW | Unit tests in T04–T07 verify trace_id chain |
| signal_id collision between timeframes | Low | HIGH | timeframe in signal_id formula — already in approved design §4.2 |

---

*This document is a design artifact. No code will be written until each task is explicitly approved.*
*Approval sequence: T01 → T02 → T03 → (T04–T07 group) → (T08–T09 group) → T10*
