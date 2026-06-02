# QuantEmbrace — Architectural Decisions

**Scope**: This file records the **why** behind every major architectural choice.  
**Audience**: Engineers joining the project, or anyone asking "why did we do it this way?"  
**Format**: Decision → Context → Choice → Rationale → Trade-offs accepted  
**Full ADR text**: `memory/decisions.md` (ADR-001 through ADR-009)  
**Last Updated**: 2026-05-27  

---

## Phase Transition Log

A chronological record of what changed between phases, why, and what was preserved.
This section grows with each completed phase. For full ADR detail see `memory/decisions.md`.

---

### Phase 0 → Phase 1: Fargate-Only to EC2 Backbone

**Date completed**: 2026-04-29  
**ADR**: ADR-009 (`memory/decisions.md`)  
**Phase doc**: `docs/phase1_ec2_migration.md`  

#### What changed

| Component | Phase 0 (Legacy) | Phase 1 (Current) | Why it changed |
|---|---|---|---|
| data-ingestion-nse | ECS Fargate 0.25vCPU/512MB | EC2 t4g.medium ASG 1/1 | Persistent WebSocket, kernel TCP tuning |
| data-ingestion-us | ECS Fargate 0.25vCPU/512MB | EC2 t4g.medium ASG 1/1 | Same as NSE |
| strategy-engine | ECS Fargate 0.5vCPU/1GB | EC2 c6g.large ASG 1/2 | Sustained CPU, scale-out foundation |
| execution-engine | ECS Fargate 0.25vCPU/512MB | EC2 c6g.large ASG 1/1 | Cluster placement, order latency |
| Instance access | N/A (Fargate managed) | SSM Session Manager | Secure operator access, no bastion |
| Failover time | ~30s (Fargate restart) | < 60s (ASG warm pool) | Market-hours uptime requirement |

#### What was deliberately preserved

| Component | Reason preserved |
|---|---|
| risk-engine on Fargate | Consistency-critical singleton — no latency gain from EC2. Migrates in Phase 4 with distributed risk redesign. |
| ai-engine on Fargate Spot | On-demand batch only (desired=0 when idle). EC2 brings no benefit for sub-hourly workloads. |
| All SQS queues | **Removed in Phase 2.** All queues deleted; Kafka MSK Serverless is the sole transport. |
| All DynamoDB tables | No change to state layer. Redis cache introduced in Phase 4. |
| All S3 buckets | Storage layer unchanged across all phases. |
| Docker images / CI/CD | Same ECR images, same GitHub Actions pipeline. Only compute substrate changed. |
| All Python application code | Zero application-level changes in Phase 1. Transport and compute changes only. |

#### Why these services and not others

The three migrated services are the ones whose output latency directly affects trading outcomes:

- **data-ingestion**: tick-to-SQS latency sets the freshness of all downstream decisions.
  Kernel TCP tuning and persistent process (no Fargate container restart overhead) reduce this.
- **strategy-engine**: tick-to-signal latency. EC2 sustained CPU (no burstable credit ceiling
  on t-series Fargate) ensures consistent signal generation rate.
- **execution-engine**: signal-to-wire latency. Cluster placement group co-locates the
  execution engine with the DynamoDB AZ endpoint, shaving 1–3ms from the risk state read
  on every order (kill switch check + positions scan).

The risk engine migrated to EC2 in Phase 2 alongside the Kafka migration. The AI engine is batch-only.

#### Cost impact

| | Phase 0 | Phase 1 | Delta |
|---|---|---|---|
| Monthly total | ~$130/month | ~$166/month | +$36/month (+28%) |
| Compute only | ~$37.50/month | ~$55.96/month (on-demand) | +$18.46/month |
| Break-even path | — | 1-year Reserved RI for t4g.med instances → ~$35.31/month compute | -$2/month vs Phase 0 |

The +$36/month premium buys: 3–10ms lower order-path latency, cluster placement group,
warm-pool failover (< 60s), and unblocks Phase 2 (Kafka requires EC2 EBS storage).

#### What Phase 1 unblocks

- **Phase 2 (Kafka)**: MSK Serverless eliminates broker management entirely. No EBS required for
  persistent block storage. EC2 instances established in Phase 1 can host Kafka brokers with
  EBS volumes in Phase 2.
- **Phase 3 (Horizontal Scale)**: EC2 ASG with CPU-based scale-out is already configured
  (strategy-engine Max=2). Kafka consumer groups (Phase 2) provide the partition sharding
  needed to activate this.
- **Phase 7 (Latency)**: Kernel TCP tuning is now active. SR-IOV, CPU pinning, and NUMA-aware
  allocation (Phase 7 optimisations) require EC2 — impossible on Fargate.

---

---

### Phases 2-8 + Paper Trading Readiness Review (2026-05-03 → 2026-05-27)

**ADR references**: ADR-010 through ADR-020 (`memory/decisions.md`)

| ADR | Date | Summary |
|---|---|---|
| ADR-010/011 | 2026-04-30 | Kafka MSK Serverless replaces all SQS queues. 15 topics, 3 independent consumer groups for risk engine, deterministic signal/order IDs, 5-tier lag policy |
| ADR-012 | 2026-05-01 | Zerodha token-bucket rate limiter (10 req/s, 4 priorities), BulkOrderPoller replaces O(N) fill polling, LiveQuotePoller, PositionMonitor |
| ADR-013 | 2026-05-05 | StrategyRunner (TICK/CANDLE), DynamoDB candle-cache poll, `paper_trade` signal field + `_handle_paper_order` branch in execution_engine, 6 strategies all default paper_trade=True |
| ADR-014 | 2026-05-06 | KillSwitchCache (0ms RAM check), RiskContextBuilder (single pre-fetch), SpreadGateValidator, SectorConcentrationValidator, LiquidityValidator, RiskAnalyticsEngine |
| ADR-015 | 2026-05-08 | Phase 8 production hardening: SQLite outbox, per-endpoint Zerodha budgets, ACK_UNKNOWN state, startup reconciliation gate, orphan detector |
| ADR-016 | 2026-05-25 | LiveCounters shared singleton flushed to `/tmp/qe_live_counters.json` every 60s; wired into ExitOrderRouter, TEE, MIS square-off |
| ADR-017 | 2026-05-25 | LtpResolver: DynamoDB prices table → position fill price fallback; freshness metadata exposed in monitoring §5 |
| ADR-018 | 2026-05-25 | ExitOrderRouter lock poisoning fix, live_trading_enabled settings-driven, stale-LTP blocking in LIVE mode, preflight kill-switch query corrected |
| ADR-019 | 2026-05-26 | Three-mode trading universe (PAPER_SAFE_START / PAPER_EXPAND / LIVE_ADVANCED), immutable daily snapshots, hard order validation, daily refresh loop |
| ADR-020 | 2026-05-27 | Paper trading readiness sweep: signal age root cause (Days 1-4 zero trades fixed), 6 execution gaps fixed, 16 new tests, pre-flight script added |

---

## How to Read This Document

Each section answers a single architectural question. Decisions are grouped by concern,
not by chronology. Each decision states what was **rejected** and why, because understanding
the roads not taken is as important as understanding the road taken.

---

## Compute

### Why EC2 for latency-critical services, Fargate for managed services?

**Decision**: data-ingestion, strategy-engine, execution-engine → EC2 ASG.
risk-engine, ai-engine → ECS Fargate.

**The problem with pure Fargate (V0)**:
Fargate's network virtualization introduces 3–15ms of jitter on the order path that cannot
be tuned away. Fargate provides no kernel access, no placement groups, and no persistent
local storage. As the platform matures toward hedge-grade standards, these limitations become
architectural blockers rather than acceptable trade-offs.

**Why not all EC2**:
The risk engine is the consistency-critical singleton. Moving it to EC2 gains nothing in
latency (signals arrive via SQS — async, not time-critical) and introduces OS management
overhead for a service that is already reliable on Fargate. The risk engine migrates to
EC2 in Phase 4, when the distributed risk engine redesign provides a compelling reason
for the operational change.

The ai-engine runs as an on-demand batch job (desired=0 when idle). EC2 Spot is the
correct choice — no idle cost, no instance management, no EC2 capacity reservation needed
for workloads that run for minutes, not hours.

**Why not EKS (Kubernetes)**:
5 services do not justify the operational overhead of managing a Kubernetes control plane,
node groups, pod scheduling, and cluster upgrades. ECS (Fargate and EC2) provides sufficient
orchestration. We revisit when service count exceeds 20 or when we need cross-region container
scheduling. See ADR-001.

**Why Graviton3 (ARM) over x86**:
AL2023 ARM64 on Graviton3 provides 15–40% better price/performance for Python workloads vs.
Intel/AMD equivalents. All broker SDKs, ML libraries, and AWS tooling support ARM. The Python
GIL means we are primarily I/O-bound — Graviton's network performance advantage matters more
than raw CPU throughput for our workloads.

---

### Why ASG warm pools?

**Decision**: All four EC2 ASGs have warm pools configured (pre-stopped instances).

Without warm pools, an ASG replacement takes 2–3 minutes: EC2 boot (60s) + Docker image pull
(30–60s) + service startup (30s). During market hours, a 3-minute outage can mean missing an
entire trading opportunity window. Warm pools keep pre-booted, pre-pulled instances in a
"Stopped" state. When the ASG needs a replacement, the warm instance starts in ~20 seconds
(resume from stopped, service already configured). Total failover: < 60 seconds.

Cost: a stopped EC2 instance costs ~$0.01/hr for the EBS root volume. For four instances,
this is ~$0.30/day — justified given the market-hours protection it provides.

---

## Messaging

### ~~Why SQS for inter-service communication in V1?~~ [DEPRECATED — SQS REMOVED IN PHASE 2]

**Status**: ⚠️ DEPRECATED. SQS has been fully removed. Kafka MSK Serverless replaced all SQS queues in Phase 2. This ADR is preserved for historical context only.

**Original Decision (V1)**: Three SQS FIFO queues for market-data → strategy → risk → execution flow.

SQS was the right choice for V1 because it requires zero operational overhead — no cluster
to manage, no topic partition sizing, no consumer group configuration. For a system in initial
build-out, operational simplicity beats throughput. SQS FIFO provides per-symbol ordering
(MessageGroupId) which is all we need for correct signal sequencing.

**SQS limitations accepted in V1** (addressed in Phase 2):
- Pull-based polling adds ~50ms latency per message
- No consumer group semantics (two consumers racing = duplicate processing)
- No message replay beyond 14-day retention
- No stream processing without separate infrastructure

**Why Kafka in Phase 2, not V1**:
Kafka requires 3+ brokers, ZooKeeper/KRaft management, topic partition planning, and
consumer group rebalancing logic in every consumer. Introducing this in V1 alongside
broker integrations, risk engine implementation, and authentication would have been
scope overload. V1 proves the trading logic is correct. V2 optimizes the transport.

---

### ~~Why keep SQS for the risk → execution path even in Phase 2?~~ [SUPERSEDED — SQS REMOVED IN PHASE 2]

The approved-signals queue carries ~1–10 messages per minute during active trading — a tiny
volume. The execution engine must be a singleton (Max=1 ASG), so there is no horizontal
scaling benefit to Kafka consumer groups on this queue. SQS FIFO provides exactly the
semantics needed (ordered, deduplicated delivery to a single consumer) at zero operational
cost. Converting it to Kafka adds complexity without adding value.

---

## Risk Architecture

### Why is the risk engine a mandatory gate, not an advisory service?

**Decision**: Every signal must pass through the risk engine and receive an explicit approval
before reaching any broker. There is no bypass path, no override flag, no "emergency" mode
that skips risk.

This is the most important design decision in the system. An algorithmic trading platform
without mandatory risk controls is a liability, not an asset. The specific risks we are
preventing: runaway loss from strategy bugs, runaway orders from execution bugs, margin
calls from position sizing errors, and catastrophic drawdown from market black swans.

The fail-safe design (if risk engine is down, trading halts) is intentional. A brief trading
pause due to a risk engine outage is a nuisance. Trading without risk controls is potentially
ruinous.

**Why a separate service rather than embedded in execution**:
1. **Auditability**: A separate log stream for every risk decision (approved or rejected)
   creates a clean audit trail. Risk decisions are archived to S3 indefinitely.
2. **Independent deployment**: Risk parameter changes, new risk rules, and risk engine bugs
   can be deployed without touching execution engine code.
3. **Blast radius isolation**: A bug in the execution engine cannot accidentally modify
   risk behavior, and vice versa.
4. **Future regulatory compliance**: Demonstrable separation of risk controls from
   trading logic is a standard regulatory requirement for institutional operations.

See ADR-007.

---

### Why DynamoDB for risk state (not Redis in V1)?

DynamoDB provides multi-AZ replication, automatic failover, and conditional writes (used for
kill switch state transitions) out of the box. In V1, the risk engine is a singleton —
there is only one reader/writer of risk state, so the ~1–3ms DynamoDB read latency is
acceptable. Risk state reads happen ~10 times per signal (7 checks + margin + positions).
At ~10 signals/minute, that is ~100 DynamoDB reads/minute — well within on-demand pricing.

Redis is introduced in Phase 4 when the risk engine becomes dual-instance (active-active)
and read latency on the critical path needs to be sub-millisecond. See `v2-draft.md` Phase 4.

---

## Broker Design

### Why two brokers (Zerodha + Alpaca) instead of one?

**Decision**: Zerodha for NSE India, Alpaca for US Equities. Each is the best-in-class
developer API for its market.

NSE India and US Equities operate in non-overlapping market hours (NSE: 09:15–15:30 IST,
US: 19:30–02:00 IST approximately). This allows near-full capital utilization across
both markets on the same capital base. Different market microstructures also enable
diverse strategy types — momentum works differently in NSE's smaller-cap universe
vs. US large-cap Nasdaq names.

**Why not Interactive Brokers (single account for both)**:
IBKR supports both markets through a single account and a single API. It was rejected because:
1. The TWS Gateway is a Java desktop application that must run continuously — a fragile,
   non-cloud-native dependency.
2. IBKR API is callback-based and complex, with poor Python SDK quality.
3. Account minimums are higher and account setup is slower.
4. IBKR's tick data quality for NSE is lower than Zerodha's native feed.

See ADR-002.

---

### Why the broker adapter pattern?

**Decision**: Abstract `BrokerAdapter` protocol with concrete implementations per broker.
No strategy, risk, or AI code references broker-specific types.

The adapter pattern means adding a new broker (e.g., Interactive Brokers for F&O futures,
ICICI Direct for deeper NSE routing) requires implementing one class, not modifying
strategy or risk code. It also enables full testing without broker connectivity — mock
adapters pass the same interface contract as real brokers.

The `BrokerClient` protocol in Python (structural subtyping via `Protocol`) means adapters
don't need to inherit from a base class — they just need to implement the method signatures.
This keeps broker code cleanly separated without forcing deep inheritance hierarchies.

See ADR-008.

---

## Language and Tooling

### Why Python, not Rust or Go?

**Decision**: Python 3.11+ for all services.

Python was chosen for the quantitative ecosystem, not for performance. `pandas`, `numpy`,
`ta-lib`, `scikit-learn`, `xgboost`, and both broker SDKs are mature, well-maintained
Python libraries. There is no equivalent Go or Rust ecosystem for quantitative finance.

**Performance concerns and mitigations**:
- The GIL: all I/O-bound code uses `asyncio`. Strategy computation is CPU-bound but
  runs on a dedicated EC2 instance — the GIL is not a meaningful bottleneck at our
  tick rate (< 1000 ticks/second per service).
- Hot path latency: Python's interpreter overhead is acceptable at ~100ms target latency
  in V1. Phase 7 will evaluate whether specific hot-path components (risk validation,
  order submission) benefit from Rust extensions via PyO3.

See ADR-003.

---

### Why Terraform, not CDK or Pulumi?

**Decision**: Terraform (OpenTofu-compatible) for all infrastructure.

CDK generates CloudFormation, adding an abstraction layer that makes debugging harder —
a Terraform `plan` diff is readable; a CDK synthesize output is often 2000 lines of
CloudFormation JSON. Pulumi has a smaller community and less module ecosystem. Terraform's
`plan`/`apply` workflow is the gold standard for infrastructure changes in a trading system
where misconfiguration has financial consequences.

Remote state in S3 with DynamoDB locking ensures team-safe concurrent operations.

See ADR-004.

---

## Data Storage

### Why DynamoDB, not PostgreSQL or Aurora?

**Decision**: DynamoDB for all operational state (positions, orders, risk state, session tokens).

Our access patterns are almost entirely key-value lookups: get position by instrument,
get order by order_id, get kill switch state. We have zero need for JOIN queries or
complex aggregations on operational data. DynamoDB's on-demand mode charges nothing
during non-market hours — for a system that runs 7 hours/day, 5 days/week, this matters.
RDS Aurora Serverless v2 was considered but its scale-to-zero still has a multi-second
cold start, and its always-on minimum cost is ~$40/month vs DynamoDB's ~$4/month.

**The exception: analytics queries**. Order history analysis, strategy performance reports,
and backtest metadata run against S3 + Athena. DynamoDB is never used for analytical queries.

See ADR-005.

---

### Why S3 + Parquet, not a time-series database?

**Decision**: All historical tick and OHLCV data stored as Parquet files in S3.

Parquet on S3 costs $0.023/GB/month (Standard tier) compared to $0.25+/GB for managed
time-series databases (InfluxDB Cloud, TimescaleDB RDS). For 63 GB/year of tick data,
S3 costs $1.45/month vs $15+/month for a time-series database. Athena provides ad-hoc
SQL queries against S3 Parquet at $5/TB scanned — typically $0.01–0.05 per query.

The trade-off: Athena is not real-time (data must be partitioned and crawled).
For the hot path (live trading), DynamoDB `latest-prices` provides current state.
S3 + Athena is used only for historical analysis and backtesting — both batch workloads
where a few-second query time is perfectly acceptable.

See ADR-006.

---

## Evolution Principles

### How are architectural decisions made?

Every significant architectural change must answer three questions:

1. **Does it keep the risk engine between strategy and execution?**
   If yes, it can be evaluated. If no, it is rejected.

2. **Does it improve at least one of: latency, reliability, scalability, observability?**
   Changes that add operational complexity without improving a measurable outcome are
   deferred until the benefit becomes concrete.

3. **Can it be deployed without breaking the running system?**
   Each phase must be independently deployable with a clear rollback path.
   Changes that require simultaneous deployment of 3+ services are redesigned.

### Why phases, not a big-bang rewrite?

The system processes real money. A big-bang rewrite (build V2 in parallel, switch over)
carries a risk period where two systems exist and neither is fully validated. The phased
approach keeps one validated system always running while the next capability is built
and tested alongside it. Each phase adds one architectural component, validates it for
5+ trading days, then decommissions the predecessor.

The migration philosophy from `v2-draft.md` applies to every phase:
```
Build in parallel → shadow mode → gradual cutover → decommission
10% traffic → 50% → 100% → remove old path
```
