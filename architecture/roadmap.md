# QuantEmbrace V2 — Phase Roadmap

**Last Updated**: 2026-05-28  
**Current Phase**: 7 — Complete (paper trading infra, ADR-018/019/020 applied)  
**Next Phase**: 8 — Production Hardening + Fault Tolerance (design complete, implementation not started)  

---

## Phase Overview

```
┌─────┬───────────────────────────────────┬──────────────┬────────────────┐
│     │ Phase                             │ Status       │ Key Outcome    │
├─────┼───────────────────────────────────┼──────────────┼────────────────┤
│  0  │ Legacy Fargate Baseline           │ ✅ BASELINE  │ Initial system │
│     │ (Pre-V2 reference)                │              │ documented     │
├─────┼───────────────────────────────────┼──────────────┼────────────────┤
│  1  │ EC2 Backbone Migration            │ ✅ COMPLETE  │ Low-latency    │
│     │                                   │              │ compute        │
├─────┼───────────────────────────────────┼──────────────┼────────────────┤
│  2  │ Kafka Streaming Core              │ ✅ COMPLETE  │ Push messaging,│
│     │                                   │              │ tick replay    │
├─────┼───────────────────────────────────┼──────────────┼────────────────┤
│  3  │ Decouple Strategy & Scale         │ 🔲 NEXT      │ Horizontal     │
│     │ Horizontally                      │              │ strategy scale │
├─────┼───────────────────────────────────┼──────────────┼────────────────┤
│  4  │ Distributed Risk Engine +         │ 🔲 PLANNED   │ Portfolio-level│
│     │ Portfolio Layer                   │              │ risk analytics │
├─────┼───────────────────────────────────┼──────────────┼────────────────┤
│  5  │ Data Platform + Feature Store     │ 🔲 PLANNED   │ Versioned,     │
│     │                                   │              │ real-time      │
│     │                                   │              │ features       │
├─────┼───────────────────────────────────┼──────────────┼────────────────┤
│  6  │ ML + Agentic Layer                │ 🔲 PLANNED   │ ML on live     │
│     │                                   │              │ signal path    │
├─────┼───────────────────────────────────┼──────────────┼────────────────┤
│  7  │ Latency Optimization +            │ ✅ COMPLETE  │ Retry infra,   │
│     │ Execution Enhancements            │              │ go-live runbook│
├─────┼───────────────────────────────────┼──────────────┼────────────────┤
│  8  │ Production Hardening +            │ 🔲 NEXT      │ Design done;   │
│     │ Fault Tolerance                   │              │ 0/15 impl tasks│
└─────┴───────────────────────────────────┴──────────────┴────────────────┘
```

---

## Phase Dependencies

```
Phase 0 (Fargate — Legacy, documented reference)
    │
    └──▶ Phase 1 (EC2)            ← V2 foundation; enables kernel tuning + EBS
              │
              └──▶ Phase 2 (Kafka)          ← requires EC2 for Kafka broker EBS storage
                        │
                        └──▶ Phase 3 (Scale)     ← requires Kafka consumer groups
                                  │
                                  └──▶ Phase 4 (Risk+)   ← requires stable horizontal strategy scale
                                            │
                                            └──▶ Phase 5 (Feature Store)  ← requires stable risk layer
                                                      │
                                                      └──▶ Phase 6 (ML)   ← requires feature store
                                                                │
                                                                └──▶ Phase 7 (Latency)  ← requires all prior
                                                                          │
                                                                          └──▶ Phase 8 (Hardening)  ← closes known failure modes
```

No phase may begin until the previous phase passes its acceptance criteria.

---

## Phase 0 — Legacy Fargate Baseline (Historical Reference)

**Status**: ✅ BASELINE — documented, not a delivery phase  
**Documented**: 2026-04-29  
**Reference file**: `architecture/v1.md` → "Legacy Architecture — Phase 0" section  

This is not a delivery phase. Phase 0 records the pure-ECS Fargate state of the
system *before* V2 work began. It exists so every V2 phase has a clear before/after
comparison. No work was executed in Phase 0 — it is a snapshot.

| Service | Compute | Resources |
|---|---|---|
| data-ingestion-nse | ECS Fargate | 0.25 vCPU / 512 MB |
| data-ingestion-us | ECS Fargate | 0.25 vCPU / 512 MB |
| strategy-engine | ECS Fargate | 0.5 vCPU / 1 GB |
| execution-engine | ECS Fargate | 0.25 vCPU / 512 MB |
| risk-engine | ECS Fargate | 0.25 vCPU / 512 MB |
| ai-engine | ECS Fargate Spot | 0.5 vCPU / 1 GB |

**Monthly cost**: ~$130/month  
**Key limitations**: no kernel tuning, 30s cold start, no placement groups, no EBS, no persistent block storage, no horizontal scale with deduplication.  
**Full detail**: see `architecture/v1.md` Phase 0 limitations table and Phase 0 ASCII diagram.

---

## Phase 1 — EC2 Backbone Migration

**Status**: ✅ COMPLETE  
**Completed**: 2026-04-29  
**Duration**: 1 session  

### What was delivered

| Deliverable | File |
|---|---|
| Phase document | `docs/phase1_ec2_migration.md` |
| Updated infra diagram | `architecture/infra_diagram.md` |
| Terraform EC2 module | `infra/terraform/modules/ec2_services/` |
| Architecture decision | `memory/decisions.md` → ADR-009 |
| Legacy baseline snapshot | `architecture/v1.md` → Phase 0 section added |
| V2 foundation design | `architecture/v2-draft.md` → Phase 1 as V2 base; 3-column vision table |
| Phase transition record | `architecture/decisions.md` → Phase Transition Log (Phase 0 → 1) |
| Phase roadmap | `architecture/roadmap.md` → Phase 0 baseline row; Phase 1 marked complete |

### Architecture changes

| Service | Before | After |
|---|---|---|
| data-ingestion-nse | ECS Fargate 0.25vCPU/512MB | EC2 t4g.medium ASG 1/1 |
| data-ingestion-us | ECS Fargate 0.25vCPU/512MB | EC2 t4g.medium ASG 1/1 |
| strategy-engine | ECS Fargate 0.5vCPU/1GB | EC2 c6g.large ASG 1/2 |
| execution-engine | ECS Fargate 0.25vCPU/512MB | EC2 c6g.large ASG 1/1 |
| risk-engine | ECS Fargate 0.25vCPU/512MB | **unchanged** |
| ai-engine | ECS Fargate Spot | **unchanged** |

### Acceptance criteria (met)

- [x] EC2 architecture design: instance types, ASG config, placement groups
- [x] Migration steps from Fargate → EC2 (blue/green per service)
- [x] Networking considerations: SGs, SSM endpoints, service discovery
- [x] Cost comparison: on-demand vs Reserved vs Spot
- [x] Terraform module: `infra/terraform/modules/ec2_services/`
- [x] Updated `architecture/infra_diagram.md`
- [x] ADR-009 recorded in `memory/decisions.md`
- [x] Phase 0 legacy baseline documented in `architecture/v1.md`
- [x] `architecture/v2-draft.md` reframed with Phase 1 as V2 foundation
- [x] `architecture/decisions.md` Phase Transition Log written (Phase 0 → 1)
- [x] `architecture/roadmap.md` Phase 0 row added, Phase 1 marked complete

### Cost impact

| | Before | After |
|---|---|---|
| Monthly compute | ~$130/month | ~$166/month |
| Premium | — | +$36/month |
| What the premium buys | — | 3–10ms lower latency, cluster placement group, warm pools, Phase 2 prerequisite |

---

## Phase 2 — Kafka Streaming Core

**Status**: ✅ COMPLETE — 2026-05-03  
**Prerequisite**: Phase 1 complete ✅  
**Architecture decisions**: `memory/decisions.md` ADR-011  

### What was delivered

All 4 trading services run Kafka-native. SQS is fully removed. Blockers (B1 fill tracking
via BulkOrderPoller 300ms polling, B2 infra) were resolved as part of implementation.

### Objective

Replace SQS **entirely** from all trading paths with Kafka (Amazon MSK, 3-AZ).
Enable deterministic event ordering, bounded replay, real-time fill tracking,
and scoped kill switch propagation. Close the fill-tracking loop for NSE.

### Key Design Decisions (already made — see ADR-011)

1. **SQS completely removed** from all trading paths (not partially replaced)
2. **15 Kafka topics** with per-instrument partitioning; `risk.kill-switch` uses 1 partition
3. **signal_id = sha256(strategy_id|symbol|direction|timeframe|tick_sequence_id)**
4. **Risk engine runs 3 independent consumer groups** (not threads sharing one group)
5. **Kill switch hierarchy: GLOBAL > MARKET > INSTRUMENT** (most restrictive wins)
6. **Consumer lag: 5-tier policy** with deterministic actions per tier
7. **Bounded replay on restart** (not auto.offset.reset=earliest — avoids massive replay)
8. **Kafka write failure → Global kill switch** (3 consecutive failures → halt)

### Architecture After Phase 2

> **Historical snapshot — Phase 2 state only.** Phase 6 (ML layer) inserted the ai_engine between strategy_engine and risk_engine. The current primary consumer for risk_engine is `signals.enriched` (aiengine-v1 publishes; risk-v1 consumes), not `signals.pending`. See `architecture/system_design.md` for the current signal flow.

```
data-ingestion   ──▶ ticks.nse / ticks.us
strategy-engine  ──▶ signals.pending (group: strategy-v1)
risk-engine      ──▶ signals.approved / ops.audit
execution-engine ──▶ orders.events, ops.audit
kill.switch       ─▶ consumed by all services (kill-switch-listener task)

Risk engine consumes (Phase 2 state — superseded by Phase 6):
  signals.pending   (group: risk-v1)       ← Phase 6 changed this to signals.enriched
  orders.events     (group: risk-v1, separate subscription)

All consumer groups at Phase 2: strategy-v1, risk-v1, execution-v1
```

### What to Build

**Infrastructure (Terraform):**
- Amazon MSK 3-broker cluster, 3-AZ (kafka.m5.xlarge or kafka.m5.large)
- Schema Registry on EC2 (Confluent-compatible, MSK Serverless SchemaRegistry)
- ALB for Zerodha postback webhook (BLOCKER B2 — or defer to Phase 3)
- Kafka UI (Redpanda Console) for operator visibility via SSM port-forward
- `fills-pending` DynamoDB table (TTL 24h) for fill durability during Kafka outage
- LagMonitor service (polls AdminClient every 5s, publishes per-partition lag metrics)

**Service changes:**
- `data-ingestion`: add `KafkaTickPublisher`, set exchange_sequence and trace_id on tick
- `strategy-engine`: Kafka-only consumer (group: strategy-v1) — **complete**
- `risk-engine`: replace SQS consumers with 3 named Kafka consumer groups + kill-switch listener
- `execution-engine`: replace SQS consumer with Kafka consumer (execution-v1)
  + add kill-switch listener + Zerodha fill tracking (postback handler OR polling loop)
  + publish all fill events to orders.events with schema_version 3.0

**Canonical consumer group names (deployed — do not change):**
```
strategy-v1        (consumes ticks.nse + ticks.us)
risk-v1            (consumes signals.enriched primary + orders.events)
                   risk-v1-fallback (signals.pending — EnrichmentWatchdog fallback, Phase 6)
execution-v1       (consumes signals.approved)
aiengine-v1        (consumes signals.pending — Phase 6 addition)
```

### Acceptance Criteria

**Completed (verified 2026-05-03):**
- [x] Zerodha fill tracking implemented (BulkOrderPoller 300ms)
- [x] `orders.events` topic populated with confirmed NSE and US fills
- [x] Risk engine (risk-v1) consuming fills and updating position state

**Infrastructure:**
- [ ] MSK cluster running, 3 AZ, 3 brokers, configs from ADR-011 §8
- [x] All 7 topics created via scripts/kafka/create_topics.py (ticks.nse, ticks.us, signals.pending, signals.approved, orders.events, kill.switch, ops.audit)
- [ ] Schema registry with all v3.0 schemas registered (Phase 3 enhancement)
- [ ] Consumer group names finalized and provisioned

**Correctness:**
- [ ] signal_id is deterministic (same input → same ID across restarts)
- [ ] Duplicate signal_id → deduplicated, not double-approved
- [ ] order_id is deterministic; duplicate order attempt → DynamoDB conditional write rejects
- [ ] fill_id is deterministic; Zerodha double-postback → deduplicated
- [ ] trace_id propagates unchanged from tick → signal → order → fill

**Reliability:**
- [ ] Kill switch hierarchy: Global overrides Market overrides Instrument (unit tested)
- [ ] Kafka write failure (simulated) → Global kill switch fires within 500ms
- [ ] Risk fill processor lag >2000 offsets → Global kill switch fires within 2 minutes
- [ ] Bounded replay: service restarted after 10-minute downtime replays only 5 minutes
- [ ] Hot partition detection: single-instrument burst → PARTITION_HOTSPOT alarm fires

**Performance:**
- [ ] Tick-to-signal P99 latency < 100ms (measured via TICK_TO_SIGNAL_LATENCY_MS metric)
- [ ] Signal-to-risk-decision P99 < 30ms
- [ ] End-to-end P99 (NSE MARKET order) < 1000ms
- [ ] Consumer lag on all groups < 200ms (Tier 1) during normal trading hours
- [ ] Updated `architecture/infra_diagram.md`
- [ ] Updated `architecture/v1.md` to reflect Phase 2 state
- [ ] ADR-010 recorded: Kafka transport decision

---

## Phase 3 — Decouple Strategy & Scale Horizontally

**Status**: 🔲 PLANNED  
**Prerequisite**: Phase 2 complete  
**Estimated duration**: 1–2 sessions  

### Objective

Enable multiple strategy-engine instances to process signals in parallel without
duplication, using Kafka consumer group partitioning for clean work distribution.

### What to build

- Kafka topic re-partitioning: `market.ticks.nse` and `market.ticks.us` repartitioned
  from 4 → 8 partitions (2 partitions per potential strategy-engine instance)
- Strategy-engine consumer group rebalancing logic (currently no custom assignment)
- Instrument-to-partition assignment registry in DynamoDB: `{instrument} → partition`
- Circuit breaker per strategy: isolated failure domain per registered strategy
- ASG CPU-based scale-out for strategy-engine (already wired — activate the policy)
- Kafka consumer-lag-based scale-out (signals.pending lag > threshold triggers additional instances)

### Acceptance criteria

- [ ] 2 strategy-engine instances processing in parallel without duplicate signals
- [ ] Consumer group rebalancing tested: remove/add instance with no signal loss
- [ ] Per-strategy circuit breaker: one strategy failing does not stop others
- [ ] ASG scale-out triggered by CPU > 70% for 5 minutes
- [ ] Scale-in: instances drain gracefully (no in-flight signal loss on termination)
- [ ] Backpressure handling: strategy-engine applies consumer-side throttle when risk-v1 Kafka consumer lag grows

---

## Phase 4 — Distributed Risk Engine + Portfolio Layer

**Status**: 🔲 PLANNED  
**Prerequisite**: Phase 3 complete  
**Estimated duration**: 3–4 sessions (most complex phase)  

### Objective

Evolve the risk engine from 7 basic checks (position limits, drawdown) to a full
portfolio risk management system with VaR, correlation limits, and sector caps.
Move risk engine from Fargate to EC2. Introduce active-active dual-instance setup.

### What to build

**New risk checks**:
- VaR (1-day 95% and 99%) via historical simulation on rolling 252-day window
- Portfolio correlation matrix — reject signals that push correlation above threshold
- Portfolio delta/gamma for F&O positions (NSE)
- Sector concentration limits (NSE GICS classification)
- Liquidity check: reject if order size > X% of 20-day average daily volume

**New infrastructure**:
- `risk-analytics` service — separate background process computing VaR and correlation
  every 60 seconds, writing results to DynamoDB `risk-analytics` table
- ElastiCache Redis (cluster mode) — hot-path cache for kill switch state and positions
  (< 0.5ms reads vs 1–3ms DynamoDB reads)
- risk-engine moved to EC2 c6g.large ASG — active-active dual instance (AZ-a + AZ-b)
- Distributed lock for kill switch state transitions (Redis SETNX pattern)

**Risk engine HA design**:
- Both instances consume from Kafka `strategy.signals` consumer group
- Both publish to Kafka `signals.approved` (SQS is permanently removed — not an option)
- Redis provides shared state — both instances read/write same Redis cluster
- Kill switch propagation: Redis pub/sub (< 10ms) + DynamoDB persistence (source of truth)

### Acceptance criteria

- [ ] VaR calculation implemented and backtested against historical data
- [ ] Risk engine on EC2 c6g.large (moved from Fargate)
- [ ] Active-active dual instance validated: kill one instance, other continues without gap
- [ ] Kill switch propagation < 1 second (Redis pub/sub)
- [ ] Redis cache hit rate > 95% for position reads during market hours
- [ ] All new risk checks unit tested with property-based tests (hypothesis)
- [ ] `risk-analytics` service running as background job, metrics visible in CloudWatch

---

## Phase 5 — Data Platform + Feature Store

**Status**: 🔲 PLANNED  
**Prerequisite**: Phase 4 complete  
**Estimated duration**: 2–3 sessions  

### Objective

Replace ad-hoc S3 reads with a versioned feature store.
Enable real-time feature computation from Kafka tick stream.
Provide consistent feature values for live trading and backtesting.

### What to build

- **Online feature store**: DynamoDB table `feature-store-online` — latest feature
  values per instrument, keyed by `{instrument}#{feature_group}`, TTL 24h
- **Offline feature store**: S3 Parquet partitioned by `{date}/{feature_group}/{instrument}`
- **Streaming feature pipeline**: Kafka Streams application consuming `market.ticks.*`,
  computing rolling features and writing to online feature store
- **Feature registry**: DynamoDB table `feature-registry` — maps feature names to
  computation logic, version, and dependencies
- **Backfill job**: one-time S3 historical ticks → offline feature store (for model training)

**Feature groups to implement**:
- `price_features`: MA5, MA10, MA20, MA50, MA200, VWAP, daily OHLC
- `volatility_features`: ATR14, realized_vol_10, realized_vol_20, high_low_range
- `momentum_features`: RSI14, MACD, momentum_5d, momentum_20d
- `volume_features`: volume_ratio (vs 20d avg), OBV, large_trade_flag
- `microstructure_features`: bid_ask_spread, depth_imbalance, tick_direction

### Acceptance criteria

- [ ] Streaming feature pipeline consuming ticks and writing to online store in < 2 seconds
- [ ] Strategy engine reading features from online store (not recomputing locally)
- [ ] Feature values consistent between live trading and backtest replay
- [ ] Feature registry tracks version and computation logic for all feature groups
- [ ] Backfill job completed for 1 year of historical data
- [ ] Feature store latency: online store reads < 5ms (DynamoDB), batch < 100ms (S3)

---

## Phase 6 — ML + Agentic Layer

**Status**: 🔲 PLANNED  
**Prerequisite**: Phase 5 complete  
**Estimated duration**: 3–4 sessions  

### Objective

Put ML models on the live signal path. Every signal is enriched with regime classification
and volatility forecast before reaching the risk engine. Position sizing becomes dynamic.

### What to build

- **Signal enrichment service**: new microservice `signal-enrichment` between strategy-engine
  and risk-engine on the Kafka signal topic
- **Production ML models** (to be trained before Phase 6 begins):
  - Regime classifier (HMM or LSTM) — output: {trending, ranging, volatile, crash}
  - Volatility forecaster (GARCH or ML) — output: predicted next-hour realized volatility
  - Signal quality scorer — output: adjusted confidence score (0.0–1.0)
- **Model serving**: ONNX Runtime in `signal-enrichment` service (no HTTP, in-process)
- **Model registry workflow**: S3 model artifacts → version tag → hot reload in service
- **A/B framework**: shadow mode — new model runs in parallel with old, compare distributions
- **Strategy selector agent** (experimental): LLM-based, reads regime + positions → activates/deactivates strategies
- **Parameter tuner agent** (experimental): monitors Sharpe ratio rolling 20d → suggests parameter adjustments

### Acceptance criteria

- [ ] Regime classifier running on live signal path with < 5ms inference latency
- [ ] Volatility forecast adjusting position sizing (larger positions in low-vol regimes)
- [ ] Signal quality scorer filtering low-confidence signals (below threshold)
- [ ] A/B framework validated: shadow mode shows new model parity before go-live
- [ ] Model hot-reload tested: update model artifact in S3 → live without service restart
- [ ] Strategy selector agent running (read-only in Phase 6 — no auto-activation)
- [ ] All enrichment steps logged with feature values for model debugging

---

## Phase 7 — Latency Optimization + Execution Enhancements

**Status**: 🔲 PLANNED  
**Prerequisite**: Phase 6 complete  
**Estimated duration**: 3–4 sessions  

### Objective

Drive order-to-wire latency from ~123ms (V1) to < 10ms (NSE), < 50ms (US).
Add smart order routing for large-position execution.

### What to build

- **Redis hot path for risk engine** (if not done in Phase 4): sub-millisecond reads
- **Smart Order Router (SOR)**:
  - TWAP: split orders over time window (configurable)
  - VWAP: time child orders to match volume profile
  - Iceberg: show only 20% of order to market, refill as filled
  - Venue router: NSE vs BSE spread comparison per-instrument
- **EC2 network tuning (execution-engine)**:
  - SR-IOV enabled on c6g.large (already supported, needs explicit ENI configuration)
  - CPU pinning: execution-engine process pinned to core 0 (no scheduler migration)
  - NUMA-aware allocation: Python `mmap` and buffer allocation on NUMA node 0 (same as NIC)
- **Kafka priority lanes**: high-priority signals (high-confidence, high-conviction) get
  their own Kafka partition processed first
- **Connection pooling**: persistent HTTP/2 connections to Zerodha and Alpaca (no
  per-request TLS handshake overhead)
- **Latency instrumentation**: OpenTelemetry traces on every signal, measuring each
  hop with microsecond precision — feeds Grafana latency dashboard

### Latency targets

| Hop | Current | Target |
|---|---|---|
| Tick → strategy (Kafka) | ~5ms | ~2ms |
| Strategy → signal enrichment | ~5ms | ~2ms |
| Signal enrichment → risk | ~5ms | ~1ms |
| Risk validation (DynamoDB) | ~15ms | ~1ms (Redis) |
| Risk → execution (Kafka) | ~5ms | ~1ms |
| Execution → broker API | ~88ms | ~3ms (NSE) |
| **Total** | **~123ms** | **~10ms (NSE)** |

### Acceptance criteria

- [ ] Order-to-wire latency p50 < 10ms for NSE orders
- [ ] Order-to-wire latency p99 < 25ms for NSE orders
- [ ] TWAP order type implemented and tested on paper trading
- [ ] VWAP order type implemented and tested on paper trading
- [ ] Latency dashboard (Grafana) showing p50/p95/p99 per hop in real time
- [ ] OpenTelemetry traces covering full signal lifecycle
- [ ] CPU pinning verified via `taskset` output and latency histogram improvement

---

## Completion Checklist Per Phase

Before marking any phase complete, all of the following must be true:

```
□  Acceptance criteria all checked
□  5+ consecutive trading days without ERROR-level logs
□  Previous phase components fully decommissioned (Fargate tasks set to 0, etc.)
□  architecture/v1.md updated to reflect new system state
□  architecture/infra_diagram.md updated
□  ADR recorded in memory/decisions.md
□  memory/open_tasks.md updated (new tasks for next phase added)
□  Terraform changes applied to prod (not just staging)
□  Cost figures updated in infra_diagram.md
```

---

## Open Questions (To Resolve Before Phase 2)

| Question | Options | Decision Needed By |
|---|---|---|
| Kafka: MSK vs self-managed | **Decided: MSK Serverless** (ADR-011) | ✅ Resolved |
| Kafka partition strategy | **Decided: key=symbol for ticks, key=signal_id for signals** | ✅ Resolved |
| Exactly-once vs at-least-once | **Decided: at-least-once + DynamoDB idempotency gate** | ✅ Resolved |
| Zerodha daily auth automation | Browser automation vs OAuth callback server | Tech debt |
| US execution region | ap-south-1 (current, ~200ms to Alpaca) vs us-east-1 sidecar | Phase 7 |
