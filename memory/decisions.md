# QuantEmbrace - Architectural Decisions

> Living record of key architectural decisions, their context, and rationale.
> New decisions are appended at the bottom. Existing decisions are never deleted,
> only superseded with a reference to the new decision.

---

## ADR-001: ECS Fargate Over Lambda for Compute [SUPERSEDED by ADR-009 — EC2 ARM64 ASGs]

**Date**: 2026-04-23
**Status**: Accepted

### Context

The platform requires compute for several workloads: real-time market data streaming via WebSocket, continuous strategy evaluation, order management, and risk monitoring. We evaluated AWS Lambda and ECS Fargate as the two primary serverless compute options.

### Decision

Use **AWS ECS Fargate** as the primary compute platform for all services.

### Rationale

1. **WebSocket support**: Market data from both Zerodha Kite Ticker and Alpaca arrives over persistent WebSocket connections. Lambda has a hard 15-minute execution limit and does not support long-lived connections. ECS Fargate tasks run indefinitely, making them the natural fit for WebSocket consumers.

2. **Cost for continuous workloads**: Our core services (data ingestion, strategy engine, risk engine) run continuously during market hours (approximately 6.5 hours for US markets, 6.25 hours for NSE India, with overlap). Lambda pricing is per-invocation and per-millisecond, which becomes expensive for always-on workloads. Fargate's per-second billing for continuously running tasks is substantially cheaper at this duty cycle.

3. **Predictable latency**: Lambda cold starts introduce variable latency (100ms to several seconds depending on runtime and package size). For trading, predictable sub-millisecond response times for risk validation and order submission are essential. Fargate tasks are always warm.

4. **Resource flexibility**: Fargate allows fine-grained CPU and memory allocation per task (up to 4 vCPU, 30 GB memory), sufficient for ML inference workloads in the AI engine without requiring a separate compute tier.

5. **Simpler networking**: Fargate tasks run inside a VPC with ENI-level networking, making it straightforward to configure security groups, access VPC-internal resources, and maintain persistent connections.

### Alternatives Considered

- **AWS Lambda**: Cheaper for sporadic, short-lived workloads. Would work for the AI engine training pipeline (triggered on schedule) but not for core real-time services. Rejected as the primary compute platform.
- **EC2 instances**: Lower cost for predictable, sustained workloads. Rejected because it requires managing instances, patching, scaling, and capacity planning. Fargate removes all server management.
- **EKS (Kubernetes)**: More flexible orchestration. Rejected because the added complexity of managing Kubernetes is not justified for our current service count (5 services). ECS is simpler and sufficient.

### Consequences

- All services are containerized with Docker.
- We accept Fargate's slightly higher per-unit cost compared to EC2 in exchange for zero server management.
- Batch/scheduled workloads (ML model training, report generation) may use Lambda or Fargate Spot for cost optimization.

---

## ADR-002: Dual Broker Architecture -- Zerodha + Alpaca

**Date**: 2026-04-23
**Status**: Accepted

### Context

The platform aims to trade across multiple markets for diversification and to capture opportunities in different time zones and market structures. The initial target markets are NSE India and US Equities.

### Decision

Integrate with **Zerodha Kite Connect** for NSE India access and **Alpaca Trading API** for US Equities access.

### Rationale

1. **Zerodha for NSE India**:
   - Largest retail broker in India with a well-documented API (Kite Connect).
   - WebSocket-based streaming market data via Kite Ticker.
   - Reasonable API pricing (Rs. 2,000/month for Kite Connect).
   - Supports all NSE order types (market, limit, SL, SL-M) and product types (CNC, MIS, NRML).
   - Strong community and library support in Python (`kiteconnect` package).

2. **Alpaca for US Equities**:
   - Commission-free trading with a developer-first API.
   - Free real-time market data (IEX feed) and premium data options.
   - Paper trading environment that mirrors production exactly -- essential for strategy validation.
   - WebSocket streaming for real-time quotes and trade updates.
   - Well-maintained official Python SDK (`alpaca-py`).
   - No minimum account balance for paper trading.

3. **Dual market access**:
   - NSE India (IST: 09:15-15:30) and US markets (ET: 09:30-16:00) have limited overlap, allowing capital efficiency and more trading opportunities.
   - Different market microstructures allow for diverse strategy types (momentum works differently in India vs. US).
   - Currency diversification (INR and USD).

### Alternatives Considered

- **Interactive Brokers**: Supports both Indian and US markets through a single account. Rejected because the API is complex, requires a running TWS/Gateway instance, and the minimum balance requirements are higher. May reconsider later for institutional scaling.
- **Upstox for India**: Viable alternative to Zerodha. Rejected because Zerodha has better API documentation and community support.
- **Tradier for US**: Another commission-free broker with API access. Rejected because Alpaca's paper trading environment and developer experience are superior.

### Consequences

- The execution engine must implement a broker adapter pattern to abstract broker differences.
- Authentication flows differ significantly (Zerodha uses OAuth-like redirect, Alpaca uses API key/secret).
- Order types and parameters differ between brokers and must be normalized.
- Position tracking must handle two different currencies and settlement cycles.
- Risk engine must aggregate risk across both brokers.

---

## ADR-003: Python as Primary Language

**Date**: 2026-04-23
**Status**: Accepted

### Context

We need a primary programming language for all platform services. The language must support quantitative analysis, machine learning, broker API integration, and cloud infrastructure management.

### Decision

Use **Python 3.12+** as the primary language for all services.

### Rationale

1. **Quantitative ecosystem**: Python has the strongest ecosystem for quantitative finance:
   - `pandas` for time-series data manipulation
   - `numpy` for numerical computation
   - `scipy` and `statsmodels` for statistical analysis
   - `ta-lib` and `pandas-ta` for technical indicators
   - `zipline` / `backtrader` patterns for backtesting frameworks

2. **Machine learning**: Python is the dominant language for ML:
   - `scikit-learn` for classical ML models
   - `xgboost` / `lightgbm` for gradient-boosted models
   - `pytorch` / `tensorflow` for deep learning (if needed later)
   - `optuna` for hyperparameter optimization

3. **Broker SDK support**: Both target brokers have official or well-maintained Python SDKs:
   - `kiteconnect` (Zerodha official)
   - `alpaca-py` (Alpaca official)

4. **AWS SDK**: `boto3` is the most mature AWS SDK with excellent async support via `aioboto3`.

5. **Developer velocity**: Python enables rapid prototyping and iteration, critical for a trading platform where strategy ideas need fast feedback loops.

6. **Type safety**: Modern Python (3.12+) with `mypy --strict`, Pydantic models, and dataclasses provides sufficient type safety for a trading platform without the verbosity of statically typed languages.

### Alternatives Considered

- **Rust**: Best performance and memory safety. Rejected because the quant/ML ecosystem is immature, and development velocity would be significantly slower. May introduce Rust later for latency-critical hot paths (risk validation).
- **Go**: Good performance and concurrency model. Rejected because the quant/ML ecosystem is nearly nonexistent.
- **Java/Kotlin**: Strong enterprise ecosystem and JVM performance. Rejected because Python's quant ecosystem is substantially richer and broker SDKs are better maintained.
- **TypeScript/Node.js**: Good async model. Rejected for the same ecosystem reasons as Go.

### Consequences

- Performance-critical paths (risk validation, tick processing) must be profiled and optimized. Python's GIL may require `asyncio` or multiprocessing for CPU-bound work.
- All services use the same language, simplifying shared code, tooling, and developer onboarding.
- CI pipeline includes `mypy`, `ruff`, and `pytest` for all services.

---

## ADR-004: Terraform for Infrastructure as Code

**Date**: 2026-04-23
**Status**: Accepted

### Context

All AWS infrastructure must be defined as code for reproducibility, version control, and auditability. We evaluated several IaC tools.

### Decision

Use **Terraform** (OpenTofu-compatible) for all infrastructure management.

### Rationale

1. **Industry standard**: Terraform is the most widely adopted IaC tool. Large community, extensive documentation, and abundant examples for every AWS service we use.

2. **Cloud-agnostic**: While we currently use AWS exclusively, Terraform's provider model means we are not locked into AWS-specific tooling. If we ever add a non-AWS service (e.g., a third-party monitoring SaaS), Terraform likely has a provider for it.

3. **State management**: Terraform's state file provides a clear mapping between declared resources and actual infrastructure. Remote state in S3 with DynamoDB locking enables team collaboration.

4. **Plan/Apply workflow**: The `terraform plan` step provides a clear preview of changes before they are applied, which is critical for a trading platform where infrastructure misconfiguration could cause financial loss.

5. **Module ecosystem**: Reusable modules from the Terraform Registry reduce boilerplate for common patterns (VPC, ECS, IAM).

### Alternatives Considered

- **AWS CDK**: Allows defining infrastructure in Python, which would match our application language. Rejected because CDK generates CloudFormation under the hood, adding a layer of abstraction that makes debugging harder. Terraform's HCL is purpose-built for infrastructure and is more readable for infra changes.
- **Pulumi**: Similar to CDK but cloud-agnostic. Rejected because community and ecosystem are smaller than Terraform's.
- **CloudFormation**: AWS-native, no external tooling required. Rejected because JSON/YAML templates are verbose and the plan/preview experience is inferior to `terraform plan`.

### Consequences

- Team members must learn HCL syntax (low learning curve).
- Terraform state must be carefully managed (S3 backend with locking).
- Terraform version must be pinned across all environments.

---

## ADR-005: DynamoDB for Operational State

**Date**: 2026-04-23
**Status**: Accepted

### Context

The platform needs low-latency state storage for operational data: current positions, active orders, risk parameters, strategy state, and instrument metadata. This data is accessed frequently, is relatively small per item, and requires consistent read/write latency.

### Decision

Use **Amazon DynamoDB** for all operational state.

### Rationale

1. **Low-latency key-value access**: DynamoDB provides single-digit millisecond reads and writes for key-value lookups, which is essential for risk validation in the order path (every order must be checked against current positions and risk limits).

2. **Pay-per-use pricing**: DynamoDB on-demand mode charges only for actual reads and writes. During non-market hours, costs drop to near zero. This aligns with our usage pattern (high activity during market hours, near-zero activity otherwise).

3. **Serverless**: No capacity planning, no instance management. Scales automatically from zero to peak load.

4. **Conditional writes**: DynamoDB's `ConditionExpression` enables idempotent operations (e.g., `attribute_not_exists(order_id)` for order deduplication) without external locking.

5. **TTL support**: Time-to-live on items enables automatic cleanup of transient data (e.g., expired orders, stale quotes).

6. **Streams**: DynamoDB Streams can trigger downstream processing (e.g., position change triggers risk recalculation) without polling.

### Alternatives Considered

- **PostgreSQL (RDS)**: Full relational database with rich querying. Rejected because our access patterns are almost exclusively key-value lookups, not complex joins. RDS also requires always-on instances with higher baseline cost.
- **Redis (ElastiCache)**: Even lower latency (sub-millisecond). Rejected because it requires managing cluster instances, data persistence configuration is complex, and cost is higher for our data volume. May add Redis later if we need sub-millisecond latency for specific hot paths.
- **Aurora Serverless v2**: Scales to zero and provides SQL. Rejected because scale-to-zero still has a several-second cold start, and our access patterns don't require SQL.

### Consequences

- Data modeling must follow DynamoDB best practices (single-table design or purpose-specific tables with well-defined partition keys).
- Complex queries (e.g., "all orders for a strategy in the last 7 days") require GSIs or must be moved to S3 + Athena for analytics.
- Transactions are supported but limited to 100 items per transaction.

---

## ADR-006: S3 for Historical Data

**Date**: 2026-04-23
**Status**: Accepted

### Context

The platform generates and consumes large volumes of historical data: tick data, OHLCV bars, backtest results, ML training datasets, and audit logs. This data is write-heavy, read-occasionally, and must be retained for months or years.

### Decision

Use **Amazon S3** for all historical and bulk data storage.

### Rationale

1. **Cheapest durable storage**: S3 Standard is $0.023/GB/month. S3 Intelligent-Tiering automatically moves infrequently accessed data to cheaper tiers. S3 Glacier is $0.004/GB/month for archival. No other storage option approaches this cost for durable, highly available storage.

2. **Unlimited scale**: S3 has no capacity limits. We can store years of tick data without provisioning or capacity planning.

3. **Columnar format support**: Storing data as Parquet files in S3 enables efficient analytical queries via Athena (pay-per-query SQL) without running any servers.

4. **Lifecycle policies**: Automated rules to transition data between storage classes and expire old data. For example: tick data moves to Infrequent Access after 30 days, to Glacier after 90 days.

5. **Integration**: Native integration with virtually every AWS service (Athena, Glue, EMR, SageMaker, Lambda triggers on object creation).

6. **Durability**: 99.999999999% (11 nines) durability. Data loss is effectively impossible.

### Data Layout

```
s3://quantembrace-{env}-tick-data/
  exchange={NSE|NYSE|NASDAQ}/
    year=2026/
      month=04/
        day=23/
          {instrument}_{timestamp}.parquet

s3://quantembrace-{env}-ohlcv-data/
  exchange={NSE|NYSE|NASDAQ}/
    timeframe={1m|5m|15m|1h|1d}/
      year=2026/
        {instrument}_{year}{month}.parquet

s3://quantembrace-{env}-backtest-results/
  strategy={strategy_name}/
    run_id={uuid}/
      results.json
      trades.parquet
      equity_curve.parquet

s3://quantembrace-{env}-ml-models/
  model_name={name}/
    version={version}/
      model.pkl
      metadata.json
      evaluation.json
```

### Consequences

- Real-time data access must not depend on S3 (use DynamoDB for current state).
- Tick data is written in batches (not one S3 PUT per tick) to manage costs and performance.
- Athena queries are eventual-consistency aware (new partitions may take a moment to appear).

---

## ADR-007: Risk Engine as Separate Service

**Date**: 2026-04-23
**Status**: Accepted

### Context

Risk management is the most critical safety component of an algorithmic trading platform. A risk failure can lead to direct financial loss. The question is whether risk validation should be embedded within the execution engine or run as a separate, independent service.

### Decision

Run the **risk engine as a separate, independent ECS service** with its own task definition, deployment lifecycle, and codebase boundary.

### Rationale

1. **Critical safety boundary**: The risk engine is the last line of defense before real money is at risk. Isolating it ensures that a bug in the strategy engine or execution engine cannot accidentally bypass risk checks. The risk engine has its own deployment, and deploying the execution engine cannot modify risk behavior.

2. **Independent scaling**: Risk validation may need different resource profiles than execution. During high-volatility periods, risk checks may be computationally heavier (VaR calculations, correlation checks) while execution is simple (submit order to broker).

3. **Independent deployment**: Risk parameter changes, new risk rules, and risk engine bug fixes can be deployed without touching the execution engine. This reduces the blast radius of deployments.

4. **Auditability**: A separate service has its own log stream, making it trivial to audit every risk decision (approved or rejected) independently of execution logs.

5. **Kill switch isolation**: The kill switch runs within the risk engine. If the execution engine has a bug causing runaway orders, the risk engine (running in a separate process/container) can independently halt all activity.

6. **Regulatory alignment**: Financial regulations increasingly require demonstrable separation of risk controls from trading logic. A separate service provides clear evidence of this separation.

### Consequences

- Every signal must traverse a network hop (strategy -> risk -> execution) before becoming an order. This adds a few milliseconds of latency, which is acceptable for our trading frequency.
- The risk engine must be highly available. If the risk engine is down, no orders can be submitted (fail-safe behavior -- this is by design).
- Risk engine state (positions, P&L, limits) must be kept in sync with execution engine state. DynamoDB serves as the shared source of truth.

---

## ADR-008: Broker Adapter Pattern

**Date**: 2026-04-23
**Status**: Accepted

### Context

The platform integrates with multiple brokers (Zerodha, Alpaca) and may add more in the future. Each broker has a different API, authentication mechanism, order format, and data model. We need a pattern that allows the rest of the platform to work with brokers without being coupled to any specific broker's implementation.

### Decision

Implement a **broker adapter pattern**: define an abstract `BrokerAdapter` interface and implement concrete adapters for each broker. All broker-specific logic is encapsulated within adapters. No other part of the system references broker-specific APIs or data models.

### Rationale

1. **Swap brokers without touching strategy logic**: If we switch from Zerodha to another Indian broker (e.g., Upstox, Angel One), only the adapter implementation changes. Strategy engine, risk engine, and all other services remain untouched.

2. **Add brokers incrementally**: Adding a new broker (e.g., Interactive Brokers for futures) requires only implementing a new adapter class. No changes to existing code.

3. **Testability**: Mock adapters can be used in testing without any broker connectivity. The paper trading adapter for Alpaca is essentially a test adapter that happens to run against a real (simulated) environment.

4. **Normalized data models**: The adapter translates between broker-specific models (Zerodha's order format vs. Alpaca's order format) and our internal models (`Order`, `Position`, `Fill`). The rest of the system works exclusively with internal models.

### Interface Design

```python
class BrokerAdapter(ABC):
    """Abstract interface for broker integration."""

    @abstractmethod
    async def authenticate(self) -> None: ...

    @abstractmethod
    async def place_order(self, order: Order) -> OrderResponse: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> CancelResponse: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> OrderStatus: ...

    @abstractmethod
    async def subscribe_market_data(
        self, instruments: list[str], callback: Callable[[Tick], None]
    ) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...
```

### Consequences

- Every broker-specific behavior must be encapsulated within the adapter. This sometimes requires creative normalization (e.g., Zerodha's order types map differently to internal order types than Alpaca's).
- New adapters must pass the full adapter integration test suite (a set of tests defined against the abstract interface).
- The adapter pattern adds a layer of indirection, but this is a worthwhile tradeoff for flexibility and testability.

---

## ADR-009: EC2 Auto Scaling Groups for Latency-Critical Services

**Date**: 2026-04-29  
**Status**: Accepted (historical — SQS references below describe Phase 1 state; superseded by ADR-010/ADR-011 for messaging layer)  
**Supersedes**: ADR-001 (partially) — EC2 replaces Fargate for data_ingestion, strategy_engine, execution_engine only  
**Phase**: Phase 1 — EC2 Backbone Migration  

### Context

ADR-001 chose ECS Fargate over EC2 for operational simplicity. This was the correct decision at
system inception. As QuantEmbrace evolves toward hedge-fund-grade architecture, three specific
limitations of Fargate have become architectural constraints:

1. **Fargate network virtualization** introduces 3–15ms of additional jitter on the order placement
   path. EC2 with enhanced networking (ENA) and a kernel-tuned TCP stack eliminates this overhead.
2. **Fargate cannot use cluster placement groups**. The execution engine needs physical co-location
   with the AZ's DynamoDB endpoint for minimum-latency risk state reads (kill switch + position checks
   happen on every order).
3. **Fargate provides no persistent local storage**. Phase 2 (Kafka) requires EC2 instances with EBS
   volumes for Kafka broker storage. Phase 1 EC2 migration is the required prerequisite.

### Decision

Migrate **data-ingestion, strategy-engine, and execution-engine** from ECS Fargate to EC2 Auto
Scaling Groups backed by AWS Graviton3 ARM instances.

The risk engine and AI engine **were initially kept on ECS Fargate** (historical — both have since migrated to EC2 ARM64 ASGs in Phase 2) because:
- Risk engine: zero latency benefit from EC2 (signals arrive via SQS, not time-critical path).
  Fargate provides simpler operations for the consistency-critical singleton.
- AI engine: batch inference only. EC2 provides no benefit for on-demand batch workloads.

### Instance Selection

| Service | Instance | Rationale |
|---|---|---|
| data-ingestion-nse/us | `t4g.medium` | I/O-bound WebSocket workload. Burstable credits handle tick bursts. |
| strategy-engine | `c6g.large` | Sustained CPU for indicator math. No burstable ceiling. |
| execution-engine | `c6g.large` | Predictable CPU + cluster placement group for lowest order latency. |

All instances use **AWS Graviton3 ARM64** (AL2023). 15–40% better price/performance vs. x86 for
Python workloads. Fully supported in ap-south-1.

### Rationale

1. **Kernel-tunable network stack**: EC2 allows tuning `net.ipv4.tcp_nodelay`, receive/send buffer
   sizes, keepalive intervals, and slow-start behavior. These are applied via `/etc/sysctl.d/` at
   instance launch. Fargate provides no equivalent control.

2. **Cluster placement group for execution engine**: Physical co-location within a rack reduces
   intra-AZ network hops. Estimated 1–3ms savings on the DynamoDB read path per order. At 100
   orders/day, this compounds significantly and enables Phase 7 latency work.

3. **Warm pools for fast failover**: ASG warm pools (pre-stopped instances) reduce failover time
   from 2–3 minutes (Fargate cold start) to <60 seconds. Critical during market hours.

4. **Phase 2 prerequisite**: Apache Kafka brokers require persistent EBS storage, which Fargate
   cannot provide. EC2 instances established in Phase 1 will host Kafka brokers in Phase 2.

5. **Scheduled stop/start**: EC2 instances can be stopped (not terminated) outside market hours,
   preserving the warm Docker image cache and reducing startup time for the next session. Fargate
   tasks start from cold on every scheduled start.

### Costs

Phase 1 increases monthly compute cost by approximately $31 (from ~$130 to ~$161/month) on
On-Demand pricing. This premium is justified by the latency and architectural benefits.
With 1-year Reserved Instances for the two t4g.medium instances (purchased after 30-day
validation), cost premium reduces to ~$20/month.

### Operational Changes

- **OS patching**: Automated via AWS SSM Patch Manager weekly instance refresh. No in-place
  patching — patch by replacing instances via ASG.
- **Shell access**: SSM Session Manager (no bastion, no SSH keys). IAM-controlled.
- **Monitoring**: CloudWatch Agent on each instance, identical log group names and structured
  JSON format to Fargate predecessor.
- **Deployment**: Same Docker images from ECR. CI/CD pipeline unchanged. Deployment updates
  the Launch Template, then triggers an ASG instance refresh.

### Consequences

- Operational surface area increases modestly (OS-level concerns for 4 EC2 instances).
- Migration requires a blue/green cutover period per service (documented in `docs/phase1_ec2_migration.md`).
- Risk engine and AI engine remain unaffected — zero changes required to those services.
- All Python application code is unchanged — only compute substrate changes.
- Phase 2 (Kafka) and Phase 7 (latency optimization) are now unblocked.

---

## ADR-010: Kafka-Native Event Model for Phase 2 (SQS Removed)

**Date**: 2026-04-30
**Status**: SUPERSEDED by ADR-011 (v2.1 refinements)
**Document**: `architecture/phase2_kafka_architecture.md` (v2.0)

---

## ADR-011: Phase 2 Kafka Architecture v2.1 — Pre-Implementation Final Design

**Date**: 2026-04-30
**Status**: Accepted — implementation complete 2026-05-03
**Supersedes**: ADR-010
**Document**: `architecture/phase2_kafka_architecture.md` (v2.1)

### Blockers (must resolve before implementation begins)

**BLOCKER B1**: Zerodha fill tracking not implemented.
  - Required: Zerodha postback webhook (preferred) OR polling fallback (interim)
  - Infrastructure: ALB + public DNS endpoint for postback; OR 300ms polling loop
  - If using polling fallback initially: must be replaced with postback in Phase 3
  - Without this: orders.events topic is unpopulated for NSE; risk engine position state is wrong

**BLOCKER B2**: ALB and DNS infrastructure for Zerodha webhook not yet in Terraform.
  - Terraform work required: ALB listener, target group, DNS record, security group
  - Fallback: polling loop requires no infrastructure changes — can unblock Phase 2

### Changes from ADR-010 (v2.1 delta)

**1. signal_id collision fix — timeframe added to hash (v2.1).**
Old: `hash(strategy_id, symbol, direction, tick_sequence_id)`
New: `hash(strategy_id, symbol, direction, timeframe, tick_sequence_id)`
Reason: Same strategy can run on 1m and 5m timeframes simultaneously. Same
(symbol, direction, tick_sequence_id) would produce identical signal_ids.
The 1m and 5m signals are distinct trades. The v2.0 formula was silently deduplicating them.
Timeframe string must use canonical registry (instruments.yaml) — "1m", never "1min" or "1_minute".

**2. Kill switch hierarchy with evaluation algorithm.**
Scope hierarchy: GLOBAL > MARKET > INSTRUMENT.
Most restrictive scope always wins. A Global halt cannot be overridden by market-level allow.
Evaluation: check global → check market → check instrument. First match halts.
Kill switch state object: `{global_halt, halted_markets: [], halted_instruments: []}`.
All three checks are in-memory (0ms). No I/O in the hot path.

**3. Consumer lag policy: 4-tier → 5-tier with tighter thresholds.**
<200ms: NORMAL (generate signals)
200–1000ms: WARNING (generate with 0.75× conviction, emit metric)
1000–3000ms: STALE_DROP (consume but discard, no signal generation)
3000–5000ms: PARTIAL_HALT (stop generating signals, continue consuming + fills)
≥5000ms: FULL_HALT (stop signals, auto-scale consumer group, ops alert)
Key change: 200ms is NORMAL (v2.0 had 500ms). 1000ms is STALE_DROP (v2.0 had 2000ms).
Recovery: 30 consecutive messages in Tier 1 before re-enabling signal generation.

**4. Risk engine: 3 named consumer groups (thread isolation was insufficient).**
OLD: 3 threads sharing one consumer group.
NEW: 3 independent consumer groups:
  risk-v1    → ticks.nse, ticks.us
  risk-v1 → signals.pending
  risk-v1  → orders.events, orders.events, orders.events
Reason: Shared consumer group → shared offset commits → rebalances affect all 3 functions
simultaneously. Independent groups have independent lag, independent rebalances,
independent failure modes.
risk-v1 lag >2000 offsets for 2min → automatic GLOBAL kill switch.

**5. Replay guardrail: max bounded window (not auto.offset.reset=earliest).**
OLD: auto.offset.reset=earliest → could replay 24h of ticks on restart.
NEW: On startup, if gap since last commit > MAX_REPLAY_WINDOW → seek to (now - window).
Per-group replay windows:
  risk-v1 (ticks.nse/us):      60s   (stale prices are useless)
  risk-v1 (orders.events):      3600s (1h — fills must not be missed)
  risk-v1 (signals.pending):    300s  (5 min)
  strategy-*-v1:            300s
  execution-v1:             1800s (30 min — approved signals are precious)

**6. Hot partition monitoring.**
LagMonitor publishes KAFKA_PARTITION_TRAFFIC_PCT per partition every 30s.
If any partition > 30% of total traffic:
  5-minute sustained → WARNING alert
  15-minute sustained → CRITICAL alert (re-partitioning evaluation)
LagMonitor maintains partition→instrument mapping for last 1 hour.

**7. Kafka write failure → Global kill switch.**
3 consecutive delivery failures on any critical topic → Global kill switch.
Kill switch published via separate high-priority producer (acks=1, max.block.ms=1000).
If Kafka itself is down: fall back to DynamoDB kill switch write + 5s poll.
Trading halts within 10s of total Kafka failure (without this, trading continues blind).
unclean.leader.election=false: writes block rather than elect stale replica.
A halt is recoverable; silent data corruption from stale leader election is not.

**8. orders.events schema fully specified (v2.1).**
fill_id = deterministic hash ("FILL-{market}-{date}-{time_bucket}-{order_id[:8]}")
fill_source field: zerodha_postback | zerodha_polling | alpaca_websocket
is_replay field: true only during operational replay (risk engine skips position updates)
Partial fills: published immediately; FULL fill event closes position.
Idempotency: DynamoDB conditional write on fill_id before Kafka publish.
Fill durability fallback: fills-pending DynamoDB table (TTL 24h) if Kafka write fails.

### Unchanged from ADR-010
- SQS completely removed from all trading paths
- 15 Kafka topics (same as ADR-010)
- Deterministic order_id (sha256 of signal_id + risk_decision_id + ...)
- Signal expiry at 3 layers (risk, execution, broker)
- trace_id propagated end-to-end from tick → fill
- 3-tier retry classification for broker errors
- CooperativeStickyAssignor for zero-downtime rolling deploys

### Decision

Replace SQS entirely with Kafka (Amazon MSK, 3-broker, 3-AZ) as the exclusive
transport for all trading events. The system is not live; full replacement with no
backward compatibility obligation is permitted.

### Core Design Decisions

**1. Events are contracts, not payloads.**
Every event carries a base envelope: `event_id`, `trace_id`, `schema_version`,
`source`, `source_instance`, `ingestion_time`, `published_time`. Schema versioned
with semver; consumers reject unknown major versions and route to dead-letter.

**2. Deterministic IDs (not UUIDs) for signal_id and order_id.**
`signal_id` = deterministic hash of `(strategy_id, symbol, direction, tick_sequence_id)`.
`order_id` = sha256 of `(signal_id + risk_decision_id + instrument_id + direction + quantity)`.
Same input conditions = same ID = idempotency guaranteed across restarts and replays.
Using UUID4 for these IDs is an architectural defect — it breaks deduplication.

**3. Risk engine is an independent Kafka consumer, not a synchronous gatekeeper.**
Risk engine runs 3 independent consumer threads: tick price monitoring, signal validation,
fill processing. It produces to signals.approved and signals.rejected. It never blocks
the signal producer path — decoupled via Kafka topic.

**4. Kill switch propagates via Kafka (risk.kill-switch, 1 partition, 30d retention).**
All services subscribe to risk.kill-switch as a dedicated listener (not a consumer group).
Kill switch propagation SLA: <200ms from DynamoDB write to "no new orders."
Phase 2 introduces scoped kill switch: scope=ALL | NSE | US | instrument_id.

**5. Consumer lag policy is deterministic — not advisory.**
Every lag threshold produces exactly one defined action (drop, degrade, halt, scale).
No ambiguity. No "depends." See Section 3 of phase2_kafka_architecture.md.

**6. Signal expiry is enforced at three layers.**
Layer 1: risk engine (signal.expires_at check before validation).
Layer 2: execution engine (re-check before broker API call).
Layer 3: broker API (Alpaca 422 on stale limit order, Zerodha order rejection).
Default expires_at = signal_time + 30 seconds. Configurable per strategy.

**7. Three-tier retry classification maintained from Phase 1.**
NonRetryableBrokerError (400/403/422): no retry, no circuit breaker trip.
BrokerAPIError (500/503/429): max 4 attempts, exponential backoff (200ms, factor 2, ±20% jitter).
Circuit breaker: 5 failures in 60s → OPEN → scoped kill switch to risk engine.

### Topic Architecture (15 topics)
| Topic | Partitions | Partition Key | Retention |
|-------|-----------|---------------|-----------|
| ticks.nse | 2 | instrument_id | 24h |
| ticks.us | 2 | instrument_id | 24h |
| signals.pending | 2 | instrument_id | 2h |
| signals.approved | 32 | instrument_id | 30m |
| signals.rejected | 8 | instrument_id | 7d |
| orders.events/filled/cancelled/rejected | 32/16/8/8 | instrument_id | 7d |
| risk.state-updates | 16 | instrument_id | 1h |
| risk.kill-switch | 1 | "GLOBAL" | 30d |
| ops.audit | 8 | trace_id | 90d |
| ops.dead-letter | 8 | original_topic | 7d |

---

## ADR-012: Zerodha Full-Capacity Rate Limit Architecture

**Date**: 2026-05-01
**Status**: Accepted — implementation active
**Document**: `architecture/zerodha_rate_capacity_design.md` (v1.1)

### Context

Two critical bugs existed in the Phase 1 Zerodha integration:

1. `asyncio.Semaphore(8)` was documented as "enforcing 10 req/sec" but controls concurrency, not rate. Under burst conditions it silently exceeded the Zerodha limit.
2. `fill_poller.py` called `kite.order_history(order_id=X)` per open order — O(N) API calls per cycle. With 5 open orders at 300ms, this produced 16.7 req/sec, violating the Zerodha limit. With 10 orders: 33.3 req/sec.

At the same time, the system was massively under-utilizing the 10 req/sec budget: no live quotes, no bulk position monitoring, no intraday candle streaming.

### Decision

Replace `asyncio.Semaphore(8)` with a **token bucket rate limiter** (`ZerodhaRateLimiter`) with 4 priority tiers. Replace per-order polling with `kite.orders()` bulk call (`BulkOrderPoller`). Add market-phase-aware budget allocation (`MarketPhaseGovernor`). Add `LiveQuotePoller`, `PositionMonitor`, and `IntradayCandleStream` to use freed capacity intelligently.

### Core Design Choices

1. **Token bucket, not semaphore**: 10 tokens/sec capacity, 15-token burst ceiling, priority queue (CRITICAL > HIGH > MEDIUM > LOW). Never drops requests — CRITICAL-priority calls always preempt.
2. **O(1) bulk fill polling**: `kite.orders()` returns all orders in one call. Rate cost: 2 req/sec fixed regardless of open order count (was up to 33 req/sec).
3. **Adaptive polling interval**: 2000ms idle → 300ms during heavy order activity and PRE_CLOSE phase.
4. **Market phase awareness**: 7 IST phases (PRE_OPEN through POST_CLOSE). Budget table allocates all 10 req/sec differently per phase. RESERVE column guarantees CRITICAL-priority calls never wait.
5. **Separate historical data budget**: `kite.historical_data()` uses a distinct 3 req/sec limit independent of the 10 req/sec order API limit. `IntradayCandleStream` operates on this separate budget.
6. **LiveQuotePoller disabled during MARKET_OPEN and PRE_CLOSE**: Budget reserved for order placement and fill detection during high-activity phases.

### New Capabilities Unlocked

- Live bid/ask spread gate on every order (risk engine rejects wide-spread entries)
- PositionMonitor: ground-truth position state every 1-2s (catches manual orders, auto-square-offs)
- IntradayCandleStream: exchange-validated 1m candles for ORB, Scalp, VWAP strategies
- 5 new signal types: ORB, Scalp 1m, VWAP Reversion, Intraday Trend 15m, Pre-Close Momentum

### Implementation Order

Gate 1 (foundation): RT-T01 (rate limiter), RT-T02 (phase governor), RT-T07 (broker client extensions)
Gate 2 (fill fix): RT-T03 (BulkOrderPoller — replaces O(N) fill_poller)
Gate 3 (new feeds): RT-T04–T06, RT-T09, RT-T10 — enable one at a time after 5-day Gate 2 validation
Gate 4 (new signals): RT-T08 — paper trade 5 days before live capital

### Consequences

- `ZerodhaFillPoller` (`fill_poller.py`) deprecated after RT-T03 5-day validation window
- `asyncio.Semaphore(8)` removed from execution engine entirely
- 3 new shared modules: `services/shared/zerodha/rate_limiter.py`, `market_phase.py`
- New CloudWatch namespace: `QuantEmbrace/ZerodhaRateLimit`
- No changes to: auth, kill switch, OrderManager, signal schemas, risk engine validators

### Honest Weaknesses Documented
1. Single risk engine instance serializes validation — Phase 4 must address per-instrument sharding
2. Zerodha fill tracking is polling-based (500ms) — Phase 3 should implement postback URL
3. No end-to-end transactionality — at-least-once + idempotent consumers (acceptable for Phase 2)
4. Backtest shares production MSK cluster — Phase 3 should isolate to MSK serverless

### Non-Negotiable Pre-Conditions Before Implementation (historical context)
- Zerodha fill tracking (Phase 1 defect) must be implemented before Phase 2 go-live
- All 15 topics created via script (auto.create.topics.enable=false)
- Schema registry running; all Avro schemas registered
- Consumer group names finalized (changing post-deploy = offset loss)

### Consequences
- Full SQS removal from core trading path
- trace_id propagation required in all service loggers
- All services must implement kill-switch Kafka listener (dedicated thread, not consumer group)
- LagMonitor service required (new service, polls AdminClient every 5s)

---

## ADR-013: Phase 3 — Strategy Isolation, DynamoDB Candle Integration, paper_trade Pipeline, Topology Preparation

**Date**: 2026-05-05
**Status**: Accepted — implementation in progress
**Document**: `architecture/phase3_design_review.md` (v1.2)
**Phase**: Phase 3 — Decouple Strategy & Scale Horizontally

### Context

Phase 2 left five candle strategies producing zero signals in production: `CandleBarAdapter._signal_queue` is never drained by `StrategyEngineService`. One unhandled exception in any strategy halts signal generation for all strategies. Config changes require a full deploy. Tick topic partition topology was sized for Phase 2 only.

### Decisions

1. **Single `StrategyRunner` class**, `interface_type = TICK | CANDLE`, with dual-threshold circuit breaker. One class because lifecycle concerns (circuit breaker, enabled flag, paper_trade, metrics, state persistence) are identical for both interface types.

2. **Candle strategies consume via DynamoDB `candle-cache` poll** (3-min overlapping lookback, 500ms poll interval). `IntradayCandleStream` stays in `data_ingestion` untouched. `strategy_engine` makes zero Zerodha API calls.

3. **`candle_stream.py` cross-service import fixed** via constructor injection. Module-level `from execution_engine.brokers.zerodha_broker import ZerodhaBrokerClient` removed. Full fix (moving `ZerodhaBrokerClient` to `shared/`) deferred to Phase 5.

4. **`paper_trade` field added to Signal model** (`bool = False`, additive, backward compatible). Full pipeline support: risk engine propagates flag unchanged; execution engine branches to `_handle_paper_order()` which logs, publishes synthetic `ORDER_FILLED` (paper=true) to `orders.events`, records `PAPER_FILLED` in DynamoDB, never calls live broker.

5. **`max_signals_per_day` defaults set**: ORB=2, Scalp1m=10, VWAPReversion=6, IntradayTrend15m=4, PreCloseMomentum=2, MomentumStrategy=10 (capped, not unlimited).

6. **Tick topic partitions 2 → 4** (`ticks.nse`, `ticks.us`). Online, non-destructive. Prepares topology for future horizontal scale without building multi-instance machinery today.

7. **No multi-instance strategy engine in Phase 3.** Signal rate is 5–30/day; multi-instance sharding at this volume is over-engineering. Deferred until signal rate justifies it.

### Why DynamoDB over in-process IntradayCandleStream

In-process `IntradayCandleStream` inside `strategy_engine` would:
- Import `ZerodhaBrokerClient` into strategy_engine (violates service boundary — broker methods exposed to strategy layer)
- Create a second independent `ZerodhaRateLimiter` token bucket with no coordination across processes
- Break `MarketPhaseGovernor` phase enforcement (governor only works if rate limiter + candle stream share a process)
- Create an unmonitored Zerodha API call path invisible to `rate_monitor.py`

DynamoDB polling resolves all four conflicts. Candle-to-signal latency is ~17.5s — acceptable for all five candle strategies which act on confirmed closed bars.

### Candle Signal Correctness Rules

- `signal.generated_at` = `candle.dt + timedelta(minutes=interval_minutes[candle.interval])` (candle close time, NOT polling time)
- `trace_id` = `sha256("candle|{market}|{symbol}|{interval}|{candle_open_time.isoformat()}").hexdigest()[:32]`
- Overlapping 3-min lookback + in-memory dedup set (5-min rolling window) ensures eventual-consistency stragglers are caught
- Phase check: no candle signals during MARKET_OPEN or PRE_CLOSE (data_ingestion pauses candle_stream during these phases anyway)

### Consequences

- All 6 strategies produce signals after Phase 3 (5 candle strategies were producing zero)
- One strategy exception no longer halts all others (per-StrategyRunner failure domain)
- Config changes (enable/disable, paper_trade flip, threshold changes) take effect within 60s via DynamoDB hot-reload
- `paper_trade=True` signals never result in live broker calls
- Monthly cost delta: ~+$5/month (DynamoDB candle-cache reads ~$2, CloudWatch metrics ~$3)
- Paper trading validation (5 trading days) required before flipping any strategy to `paper_trade=False`

---

## ADR-014: Phase 4 — Distributed Risk Engine + Portfolio Layer

**Date**: 2026-05-06
**Status**: Accepted
**Document**: `architecture/phase4_design_review.md` (v1.1)
**Phase**: Phase 4 — Distributed Risk Engine + Portfolio Layer

### Context

Phase 3 completed the strategy layer. The risk engine has validators in place but lacks:
- Per-signal kill switch I/O costs 3–8ms (DynamoDB read every validation)
- `max_sector_exposure_pct` field exists in risk settings but is never enforced
- No liquidity guard — illiquid instruments are not screened at validation time
- No spread gate — wide bid-ask spreads are not blocked at entry
- No portfolio analytics — sector/VaR snapshots not computed or persisted
- ADV data is in the candle cache (written by candle_prefetch.py) but not consumed by risk engine

The Phase 4 roadmap sketch proposed Redis/ElastiCache and active-active dual instances. These are architectural overkill for 5–30 signals/day. The correct scope is filling the actual gaps.

### Decisions

1. **`KillSwitchCache`** — in-memory bool, background asyncio task polls DynamoDB kill-switch record every 1 second. `is_active()` returns RAM value (0ms, no I/O). `activate()` writes DynamoDB + triggers force-cancel via `Priority.CRITICAL` in `ZerodhaRateLimiter`. Eliminates per-signal DynamoDB read.

2. **`RiskContextBuilder.build(signal)`** — single pre-fetch call assembles all validator inputs in 3–4 DynamoDB reads (position record, portfolio state, live quote from LiveQuotePoller output, ADV from candle cache). Replaces 8–12 scattered reads across individual validators.

3. **`RiskContext` dataclass** — immutable snapshot passed to every validator: `signal, confirmed_position, pending_quantity, current_exposure, sector_exposures, adv_20d, live_spread_bps: float | None, portfolio_nav, analytics_snapshot, fetched_at`.

4. **11-step validator pipeline** (in order):
   1. `KillSwitchCache.is_active()` [0ms — RAM]
   2. `SignalAgeValidator` [0ms]
   3. `RiskContextBuilder.build()` [3–8ms — DynamoDB]
   4. `PositionValidator` [0ms]
   5. `ExposureValidator` [0ms]
   6. `LiquidityValidator` [0ms]
   7. `SpreadGateValidator` [0ms]
   8. `SectorConcentrationValidator` [0ms]
   9. `MarginValidator` [cached broker call]
   10. `SlippageValidator` [0ms]
   11. `DailyLossValidator` [0ms]

5. **`SpreadGateValidator`** — reads `context.live_spread_bps` from `LiveQuotePoller` DynamoDB output. Rejects if `live_spread_bps > max_spread_bps` (default 50 bps, per-instrument configurable). Approves with `STALE_SPREAD_DATA` warning if data is >30s old — never blocks trading on stale data.

6. **`SectorConcentrationValidator`** — enforces `max_sector_exposure_pct` using `context.sector_exposures`. Previously this field was set but never read.

7. **`LiquidityValidator`** — rejects signals on instruments where `adv_20d < min_adv_lakhs` (per-instrument configurable). During `MARKET_OPEN` phase when `IntradayCandleStream` is paused, uses daily ADV written by `candle_prefetch.py` to the candle-cache table with `interval="day"`. Approves with `LOW_ADV_DATA` warning if no data exists.

8. **`InstrumentRegistry`** — `instruments.yaml` with per-instrument risk params: `sector`, `max_spread_bps`, `min_adv_lakhs`, `max_position_size`, `market`. Loaded at startup via `registry.py`. Single source of truth for instrument classification.

9. **`RiskAnalyticsEngine`** — background asyncio loop, reads position snapshots + fill history + NAV from DynamoDB, computes sector breakdown, simplified 5-day historical VaR (2% quantile), portfolio P&L. Persists snapshot to DynamoDB `analytics-snapshot` record. Phase-aware interval via `ANALYTICS_INTERVAL_BY_PHASE`:
   - PRE_OPEN/MARKET_OPEN/PRE_CLOSE: 30s
   - NORMAL: 60s
   - POST_CLOSE: 300s
   - OVERNIGHT: disabled

10. **No Redis, no active-active** — DynamoDB on-demand + in-memory cache covers all latency requirements at current signal volume. Revisit at >500 signals/day.

### Zerodha Rate Capacity Alignment (v1.1 additions)

All 4 misalignments with `zerodha_rate_capacity_design.md` resolved:
- `SpreadGateValidator` added (Misalignment 1)
- `RiskAnalyticsEngine` uses `ANALYTICS_INTERVAL_BY_PHASE` (Misalignment 2)
- `LiquidityValidator` degrades to candle_prefetch.py daily ADV during MARKET_OPEN gap (Misalignment 3)
- Force-cancel on kill switch activation uses `Priority.CRITICAL` in `ZerodhaRateLimiter` (Misalignment 4)

### Consequences

- Per-signal validation latency: 3–8ms (down from 11–16ms) — all from the single `RiskContextBuilder` pre-fetch
- Kill switch check: 0ms RAM (down from 3–8ms DynamoDB)
- Sector exposure cap now actually enforced (was configured but never checked)
- Illiquid instrument protection: live
- Wide-spread rejection: live
- Monthly cost delta: ~+$3/month (analytics loop DynamoDB reads)
- `instruments.yaml` must be updated for each new instrument added to trading
- Paper trading validation (5 trading days, Gate 4) is a prerequisite for Phase 4 go-live

### Follow-up: PHASE4-FU-001 Live Quote Persistence

`LiveQuotePoller` now writes live NSE quote snapshots to the prices DynamoDB table
using `PK=QUOTE#{market}#{symbol}`, `SK=LATEST`. `RiskContextBuilder` reads this
record and `SpreadGateValidator` can reject wide-spread NSE entries end to end.

Important broker boundary: the poller is **NSE-only** because it is backed by
Zerodha `kite.quote()`. US symbols must not be passed to `LiveQuotePoller`; US
spread data remains stale-approved until an Alpaca-backed quote writer exists.

---

## ADR-015: Phase 8 — Production Hardening + Fault Tolerance

**Date**: 2026-05-08
**Status**: ✅ Accepted 2026-05-08 — defaults from §12 all confirmed
**Reference**: `architecture/phase8_design_review.md` v1.0

### Context

Eight failure modes were identified during the Phase 7 post-review session. Each represents a
path to financial loss, duplicate orders, or unprotected positions in a live trading environment.
Phase 8 closes all eight before live capital is deployed.

### Key Decisions

**1. SQLite for durable local outbox (not DynamoDB)**

When Kafka is unavailable, DynamoDB may also be degraded (same VPC, same AZ). SQLite is
process-local with zero network dependency. The outbox is a short-lived buffer (minutes, not
hours), so durability beyond the EC2 instance lifetime is not required.

**2. Per-endpoint Zerodha rate budgets (not a separate cancel queue)**

A separate cancel queue would require persistent state management across restarts. The
endpoint budget model integrates with the existing token bucket, requiring only a config
struct and branch logic in the rate limiter. The trade-off is coarser control, but the
actual requirement (cancel cannot be starved during placement degradation) is fully met.

**3. Outbox pattern over Kafka transactions for signal processing**

MSK Serverless does not support exactly-once semantics (EOS) across all regions. The outbox
pattern achieves the equivalent guarantee using at-least-once Kafka + DynamoDB conditional
write deduplication — the same pattern already used for order idempotency throughout the
system. Consistency over novelty.

**4. Operator approval required to clear the reconciliation halt**

The `reconciliation_required` flag in DynamoDB is set automatically but can only be cleared
by a human operator running `scripts/ops/reconcile.py --clear`. Automation could clear the
halt based on the wrong source of truth (e.g., DynamoDB reflects a failed order that the
broker never received). Position drift after a live incident requires human judgment.

**5. Orphan detector alerts; does not auto-flatten**

An operator may have intentionally removed a protective SL (e.g., converting an intraday
position to delivery by removing the stop). Auto-flattening on orphan detection would be
worse than the intended outcome in that case. The alarm is mandatory; the action is human.

**6. `data_quality` field uses `default=DataQuality.NORMAL`**

This ensures zero breaking changes to all existing consumers. Services that don't yet read
the field continue operating normally. The field is purely additive in schema v3.0.

### Consequences

- 2 new DynamoDB tables (`signal-inbox`, `signal-outbox`)
- 3 new CloudWatch alarms (`RiskV1LagHigh`, `OrphanPositionDetected`, `ReconciliationHaltActive`)
- 5 new shared modules (`local_outbox`, `endpoint_budgets`, `reconciliation/gate`, `signal_inbox`, `signal_outbox`)
- 4 new execution_engine components (`orphan_detector`, `outbox_publisher`, `kafka_lag_watchdog`, reconciliation_validator)
- 158+ new tests
- No new Kafka topics, no schema version bump, no service boundary changes

---

## ADR-016: LiveCounters Shared In-Memory Singleton for Monitoring

**Date**: 2026-05-25
**Status**: Accepted

### Context

The paper trading monitoring report showed all UNKNOWN/zero values for service counters because `LiveCounters` was never populated by running services. `MonitoringStatusService` accepted an optional `live_counters` param but no component ever created or shared one.

### Decision

Create a single `LiveCounters()` instance in `ExecutionService.__init__()` and pass it by reference to `ExitOrderRouter`, `TradeExitEngine`, and `MISSquareOffManager`. A new background task `_monitoring_flush_loop()` serialises the instance to JSON every 60s via atomic `os.replace()`.

### Rationale

1. **No locks needed** — All three components run in the same asyncio event loop. Counter increments (`+= 1`) are safe without additional synchronisation; the GIL and single-threaded event loop guarantee no concurrent mutation from these components.
2. **Flush over push** — The monitor reads a file; the service writes a file. No IPC, no sockets, no new Kafka topic. Simple, debuggable, crash-safe.
3. **Atomic writes** — `open(path + ".tmp")` → `os.replace(tmp, final)` ensures the monitor never reads a partial JSON file during a write cycle.
4. **Configurable path** — `QE_MONITORING_COUNTERS_PATH` env var allows CI/test environments to use a different output path without code changes.
5. **Offline fallback** — When the execution service is not running, the monitor accepts any JSON stub via `--counters scripts/monitoring/sample_counters.json`.

### Consequences

- One new asyncio task (`execution-monitoring-flush`) added to `ExecutionService`'s 10-task `asyncio.gather`. Lightweight — one JSON serialisation + file write per 60s.
- `LiveCounters` fields are all zero/False/None by default — a fresh instance shows "not yet run" rather than UNKNOWN, which is accurate when the service just started.
- The flush loop does not crash the service on write failure — logs a warning and continues.
- `paper_trading_monitor.py --counters /tmp/qe_live_counters.json` is the canonical local monitoring command when the execution service is live.

---

## ADR-017: LtpResolver — Shared LTP Lookup with Freshness Metadata

**Date**: 2026-05-25
**Status**: Accepted

### Context

Monitoring was showing entry fill price as LTP because `_parse_position()` read `last_price` from the DynamoDB positions table (set once at fill, never updated). `TradeExitEngine._read_price_from_table()` read the prices table but never validated the `captured_at` freshness field — stale prices from a prior session were used silently.

### Decision

Introduce `LtpResolver` in `services/shared/monitoring/ltp_resolver.py`. Priority chain:
1. DynamoDB prices table (`QUOTE#NSE/{symbol}/LATEST`) — fresh when `captured_at` age ≤ `freshness_seconds` (default 5 s).
2. Position fill price fallback — always `is_stale=True`, `source="position_fill"`.

Returns `LtpResult(price, source, captured_at, age_seconds, is_stale)`. Shared by both `MonitoringStatusService` (via `_enrich_ltp()`) and `TradeExitEngine` (replaces `_read_price_from_table()`).

Monitoring §5 now shows `LTP Source` and `LTP Age` columns. When `LiveQuotePoller` is offline, all positions show `fill` and a data-quality warning is emitted.

### Rationale

Single implementation of "read DynamoDB prices table + check freshness" — eliminates the two divergent stale-LTP bugs that existed before this ADR. The resolver is async and safe to call from both monitoring and TEE without duplication.

### Consequences

- `TEE_LTP_FRESHNESS_SECONDS` env var controls the freshness threshold (default 5 s).
- Monitoring shows `live` / `stale` / `fill` / `—` in the LTP Source column per position.
- When the poller is offline, P&L values are approximate; this is explicitly flagged.
- `_read_price_from_table()` removed from TEE entirely.

---

## ADR-019: Trading Universe Model — Three-Mode Approved-Symbol System

**Date**: 2026-05-26
**Status**: Accepted — implementation active
**Reference**: `configs/universe_modes.yaml`, `services/shared/universe/`

### Context

Paper trading sessions operated without a hard approved-symbol gate. Orders could be placed on any NSE symbol that a strategy generated a signal for, even if that symbol was not in the intended watchlist. There was no mechanism to promote from a safe subset to a broader universe, or to enforce the approved set at the order level.

### Decision

Implement a **three-mode universe model** with immutable daily snapshots and a hard order validation gate.

| Mode | Symbols | Use Case |
|---|---|---|
| `PAPER_SAFE_START` | NIFTY 50 only (50 symbols) | Initial paper trading — well-known, liquid, well-covered |
| `PAPER_EXPAND` | NIFTY 100 + active F&O (≈120 symbols) | After ≥5 clean sessions on PAPER_SAFE_START |
| `LIVE_ADVANCED` | All 3 tiers + screened illiquid (≈200 symbols) | After passing promotion gates in `configs/promotion_gates.yaml` |

### Key Design Choices

1. **Immutable daily snapshot**: Built once at service startup from `configs/universe_modes.yaml`, refreshed at midnight IST by a background loop. After the snapshot is built, it is frozen for the trading day — no mid-session additions.

2. **Hard order validation (`UniverseOrderValidator`)**: Every NSE order is validated against the snapshot. In paper mode, stale or missing snapshots produce warnings but allow orders. In live mode, stale or missing snapshots block all orders.

3. **Daily snapshot refresh loop** (`_universe_snapshot_refresh_loop`): Checks every 60s whether the snapshot date matches today (IST). Rebuilds if stale. `_universe_mode_str` stored as instance var so mode is preserved across rebuilds.

4. **Permissive paper / strict live**: Paper mode never blocks on universe issues — a single bad snapshot cannot prevent a whole paper session. Live mode is fail-closed.

5. **Promotion gates** (`configs/promotion_gates.yaml`): Explicit criteria for moving between modes. `LIVE_ADVANCED` requires ≥5 clean PAPER_EXPAND sessions + all promotion gate metrics passing.

### Consequences

- `UNIVERSE_MODE` env var controls the mode (docker-compose: `PAPER_EXPAND`)
- `configs/universe_modes.yaml` is mounted read-only into execution_engine container
- `configs/promotion_gates.yaml` defines numeric thresholds for mode promotion
- Stale snapshots (day 2+) log a warning per order in paper mode — this is expected and acceptable

---

## ADR-020: Paper Trading Readiness Sweep — Six Execution Quality Fixes

**Date**: 2026-05-27
**Status**: Accepted — all fixes applied
**Session**: Post-days-1-6 comprehensive review

### Context

Five paper trading sessions (Days 1-5) produced poor execution quality:
- Days 1-4: zero paper trades (100% signal rejection)
- Day 5: some trades, poor signal coverage (60 signal ceiling, 5x NAV mismatch)
- Day 6: monitoring-only session, no fills

A systematic review identified six execution gaps.

### Root Cause: Days 1-4 (Zero Trades)

**Candle signal age mismatch**: `strategy_engine` stamps `Signal.generated_at = candle.candle_close_time` (not poll time). A 1-minute candle closing at T is written to DynamoDB at T+5s, polled at T+5.5s, enriched by ai_engine, and arrives at risk_engine at T+7-12s. `RISK_MAX_SIGNAL_AGE_SECONDS` was at code default of 5.0s → 100% rejection.

**Fix**: `RISK_MAX_SIGNAL_AGE_SECONDS: "30"` in docker-compose risk_engine block. Hard ceiling `_ABSOLUTE_MAX_AGE_SECONDS = 30.0` in `SignalAgeValidator` prevents mis-configuration above 30s. Regression test: `tests/unit/test_signal_age_candle.py` (8 tests).

### Six Fixes Applied

| Fix | File | Problem | Change |
|---|---|---|---|
| FIX-A | `execution_engine/service.py` | `submit_order` return not checked — duplicate could double position | `submitted = await ...; if not submitted: return` |
| FIX-B | `execution_engine/service.py` | Universe snapshot never refreshed after midnight IST | Added `_universe_snapshot_refresh_loop` background task (task 12) |
| FIX-C | `docker-compose.yml` (setup block) | `PAPER_SEED_NAV` defaulted to ₹50L; risk_limits uses ₹10L (5x mismatch) | `PAPER_SEED_NAV: "1000000"` |
| FIX-D | `scripts/setup_local_tables.py` | Strategy configs not seeded — `_DEFAULT_CONFIG` caps at 10 signals/day silently | `_seed_strategy_configs()` seeds all 6 strategies with `max_signals_per_day=0` |
| FIX-E | `risk_engine/service.py` | `RiskDecision.to_dict()` omitted `enriched` field — session report showed 0% enrichment | `enriched: bool = False` field + set `decision.enriched = True` in enriched loop |
| FIX-F | `scripts/monitoring/paper_session_report.py` | Enrichment detection checked validator name substring — no validator named "enriched" | Reads `blob.get("enriched", False)` directly |

### Tests Added

- `tests/unit/test_signal_age_candle.py` — 8 tests: candle signal age regression, boundaries, hard ceiling
- `tests/unit/test_paper_readiness_gaps.py` — 8 tests: paper duplicate suppression (FIX-A), stale snapshot paper vs live mode (FIX-B)

### Operational Requirement

Before next session: `docker-compose down -v && docker-compose run --rm setup`

This is required to pick up FIX-C (new PAPER_SEED_NAV) and FIX-D (strategy config seeds). Existing LocalStack data from prior sessions has the old ₹50L NAV and no strategy config rows.

### Consequences

- Paper sessions should now produce fills if market conditions match any of the 6 strategies
- Session report enrichment funnel will show accurate `total_enriched` / `total_degraded` split
- Strategy signal caps no longer silently limit sessions to 60 total signals
- Pre-flight check (`scripts/deploy/paper_preflight_check.py`) added for session startup validation

---

## ADR-018: Live Trading Tightening — Lock Poisoning Fix, Stale-LTP Blocking, Preflight Gates

**Date**: 2026-05-25
**Status**: Accepted

### Context

Pre-live audit revealed four safety gaps:

1. **Lock poisoning**: `ExitOrderRouter.route()` acquired the DynamoDB idempotency lock (`exit_order_id`) before calling `_route_live()`. If `_route_live()` returned `False` (live disabled, no broker, not implemented), the lock stayed permanently set — TEE could never retry, MIS square-off skips positions with `exit_order_id`, kill-switch flattening also blocked.
2. **`live_trading_enabled` hardcoded**: `service.py` passed `live_trading_enabled=False` regardless of settings. The Phase 4 gate was permanently closed with no path to open it without a code change.
3. **Stale LTP in LIVE mode**: `_get_last_price()` warned on stale LTP but still returned the price. In live mode this risks executing a stop-loss at a 30-second-old price.
4. **Preflight `check_kill_switch()` queried wrong table/key**: Table was `{prefix}-kill-switch` (doesn't exist); key was `pk/sk` lowercase (wrong schema). Should be `{prefix}-risk-state` with `PK/SK` uppercase.

### Decision

**A. Pre-gate in `route()`**: Check `_live_enabled` BEFORE `_acquire_exit_lock()`. Returns `False` immediately if live is disabled — lock is never acquired, position remains retriable.

**B. `_route_live()` implemented**: Calls `zerodha.place_order()` wrapped in `asyncio.wait_for(timeout=LIVE_EXIT_BROKER_TIMEOUT_S)`. On timeout: lock NOT released (order disposition unknown — operator must verify). On other exception: `_release_exit_lock()` called so TEE can retry.

**C. `_release_exit_lock()`**: Conditional DynamoDB `REMOVE exit_order_id, exit_trigger` — only succeeds if `exit_order_id` matches the current request, preventing race with a concurrent winner.

**D. `service.py` reads from settings**: `live_trading_enabled=getattr(self._settings.execution, "live_trading_enabled", False)`. Defaults to `False` (safe). Set `QE_EXECUTION_LIVE_TRADING_ENABLED=true` in the environment to unlock Phase 4.

**E. TEE stale-LTP blocking in LIVE mode**: When `router.mode == "live"` and LTP age exceeds `TEE_MAX_STALE_LTP_LIVE_SECONDS` (default 3 s), `_get_last_price()` returns `None` instead of the stale price. TEE skips exit evaluation for that position. In PAPER mode: warns only (no monetary risk).

**F. Preflight fixes**: `check_kill_switch()` corrected to `{prefix}-risk-state` / `PK=KILLSWITCH, SK=GLOBAL`. Two new checks added: `check_live_trading_gate()` (confirms explicit opt-in via env var) and `check_ltp_freshness_for_live()` (enforces `TEE_LTP_FRESHNESS_SECONDS ≤ 2.0` and `TEE_MAX_STALE_LTP_LIVE_SECONDS ≤ 3.0` when live is enabled).

### Rationale

- Lock poisoning was the most critical risk: a single failed live exit would have permanently frozen the position — no stop-loss, no MIS square-off, no kill switch.
- Timeout handling is asymmetric by design: we release the lock on known-failed calls but NOT on timeout, because releasing on timeout could allow two simultaneous exit orders for the same position if the broker received the first one.
- Stale-LTP blocking in LIVE mode is conservative — it is better to delay an exit by one poll interval than to execute at a price that is 10 seconds old during a fast-moving market.

### Consequences

- `LIVE_EXIT_BROKER_TIMEOUT_S` env var controls broker call timeout (default 15 s).
- `TEE_MAX_STALE_LTP_LIVE_SECONDS` env var controls the LIVE-mode stale-LTP block threshold (default 3 s).
- `QE_EXECUTION_LIVE_TRADING_ENABLED` must be explicitly set to `true` to unlock live broker orders.
- Preflight check now correctly reads kill-switch state from the risk-state table.
- Monitoring §6 shows `Stale LTP exit blocks` counter; §7 shows `Live exits placed` (successes).

---

## ADR-021: Staleness Monitor Split — Separate Producer and Consumer Freshness Gates

**Date**: 2026-05-29
**Status**: Accepted (implementation pending)

### Context

During Day 8/9 paper trading session, the `data_staleness_monitor` in risk_engine auto-triggered the kill switch twice. Initial diagnosis incorrectly concluded the Zerodha WebSocket feed was silent. Actual cause: `ticks.nse` had 253,788 messages — the feed was healthy. The real issue was Kafka consumer group rebalancing during a strategy_engine container rebuild froze risk_engine's own consumer briefly, causing `record_data_tick` calls to stop for >300s.

The current design has a single staleness gate: "no `record_data_tick` calls in N seconds → kill switch." This conflates two distinct failure modes:
1. **Producer stale** — data_ingestion stops publishing to `ticks.nse` (real data feed failure)
2. **Consumer lagging** — risk_engine's Kafka consumer falls behind or pauses (internal infrastructure issue)

These have very different remediation paths. Producer stale is a genuine data emergency. Consumer lag is an infrastructure/rebalance event that self-resolves within seconds.

Additionally, `risk_limits_production.yaml` (in `configs/`) is NOT wired to risk_engine. The risk_engine reads limits from hardcoded `RiskLimits.for_profile()` in `services/risk_engine/limits/risk_limits.py`. The YAML is aspirational documentation and is not mounted into the risk_engine container. There is no session-level override mechanism for risk limits.

### Decision

**Phase 1 (post Day 8):** Split the staleness monitor into two independent checks:

```python
# In risk_engine data_staleness_monitor:
producer_freshness_gate:
  metric: last_tick_timestamp on ticks.nse topic (high-watermark query)
  threshold: RISK_TICK_PRODUCER_STALE_SECONDS = 60
  action_on_breach: kill_switch.activate()
  rationale: If nobody is publishing ticks, it is a real data emergency

consumer_freshness_gate:
  metric: risk_engine consumer lag on ticks.nse consumer group
  threshold: RISK_TICK_CONSUMER_LAG_SECONDS = 60
  action_on_breach: log WARNING + metrics; do NOT trigger kill switch
  rationale: Consumer lag is an infra event (rebalance, GC pause); self-resolves
```

Remove `RISK_DATA_FEED_STALE_SECONDS` as a single undifferentiated gate. Replace with the two thresholds above.

**Phase 2 (pre-live):** Wire `risk_limits_production.yaml` into the risk_engine:
- Mount `./configs:/app/configs:ro` in docker-compose for risk_engine
- Add `RiskLimits.from_yaml(path)` loader that overlays the YAML onto `for_profile()` defaults
- Add session-override support via DynamoDB `risk-state` table (allows session-specific tightening without code changes or container rebuilds)

### Workaround Applied (Day 8)

Set `RISK_DATA_FEED_STALE_SECONDS=3600` in `.env` to prevent false kill-switch triggers during paper sessions where Kafka consumer rebalancing is common. This masks the consumer-lag issue. Acceptable for paper trading only. **Must be reverted to 60s before live trading, or replaced with the Phase 1 split.**

Session guardrails enforced via DynamoDB `max_signals_per_day`:
- `nse_vwap_reversion`: cap=8 (6 already today → 2 more allowed)
- `nse_momentum_v1`: cap=2 (0 today → 2 total allowed)
- `nse_scalp_1m`: enabled=false (disabled for remainder of session)

### Consequences

- `RISK_DATA_FEED_STALE_SECONDS=3600` must be reverted before live trading.
- Phase 1 implementation requires changes to `services/risk_engine/` staleness monitor.
- Phase 2 requires docker-compose update (add volumes to risk_engine) and `RiskLimits.from_yaml()` loader.
- Until Phase 2: `risk_limits_production.yaml` is documentation only, not enforced at runtime.
- `tee_stale_ltp_blocks` incremented in `LiveCounters` and flushed to monitoring JSON every 60 s.

---

## ADR-022: Live-Readiness Audit — Infrastructure and Code Fixes

**Date:** 2026-05-30
**Status:** Accepted — fixes applied; 2 Terraform blockers remain for manual edit before `terraform apply`

### Context

A structured four-phase live-readiness audit (Phases A–D) was conducted as a static code and infrastructure review. Phase A covered AWS infrastructure; Phase B covered service code (strategy_engine, ai_engine, data_ingestion); Phase C covered all 12 risk validators; Phase D produced the consolidated pre-live runbook. No runtime AWS state was mutated during the audit.

### Critical Discoveries

**CRITICAL — broke existing sessions silently:**
- `symbol-status-index` GSI was absent from the orders DynamoDB table. `PositionValidator._get_pending_quantity()` queries this GSI on every signal — its absence caused `ValidationException` on every position check, rejecting all live signals and bypassing dirty-read protection on paper signals.
- `sessions` DynamoDB table was absent from Terraform. `ZerodhaTokenManager` raises `ResourceNotFoundException` on every token read, causing data_ingestion and execution_engine to use stale env var tokens that expire at 07:30 IST.

**HIGH — would prevent any EC2-deployed session from trading:**
- `RISK_MAX_SIGNAL_AGE_SECONDS` was not injected into EC2 userdata scripts. Code default is `5s`. Candle signals are 7-12s old at risk_engine; all would be rejected.
- `RISK_PROFILE` was not injected into risk_engine userdata. Default is `tiny-live` (max 1 concurrent position, ₹5k max order), making paper-session testing unrepresentative.
- `UNIVERSE_MODE` was not injected into execution_engine userdata.
- `deploy.yml` references `check_asg_health.py` but only `check_ecs_health.py` (ECS API, wrong interface) existed — every CI/CD deploy failed at the health-check step.

**MEDIUM — race conditions in risk validators:**
- `DailyLossValidator._apply_fill_to_daily_symbol_pnl()` used get → compute → put without locking. Concurrent fills for the same symbol could corrupt the per-symbol cost basis.

**CODE — pre-live fixes still pending:**
- `strategy_engine/_candle_processing_loop()`: candle signal publish failures silently dropped (no retry/DLQ). Tick path correctly raises.
- `strategy_engine/start()`: `asyncio.gather(return_exceptions=True)` silently swallows crashed processing loops. Service appears healthy while generating no signals.

### Decisions

1. **Add `symbol-status-index` GSI** to `infra/terraform/modules/dynamodb/main.tf` orders table. `hash_key=symbol`, `range_key=order_status`, `projection_type=INCLUDE`, `non_key_attributes=["quantity"]`.

2. **Add `sessions` DynamoDB table** to `infra/terraform/modules/dynamodb/main.tf`. TTL 48h; PITR enabled. Required by `ZerodhaTokenManager` in all services.

3. **Inject trading safety env vars** into EC2 userdata scripts:
   - `risk_engine.sh`: `RISK_MAX_SIGNAL_AGE_SECONDS=30`, `RISK_PROFILE=paper`
   - `execution_engine.sh`: `UNIVERSE_MODE=PAPER_SAFE_START`, `QE_EXECUTION_LIVE_TRADING_ENABLED` commented out
   - `strategy_engine.sh`: `RISK_MAX_SIGNAL_AGE_SECONDS=30`

4. **Create `check_asg_health.py`** in `scripts/deploy/` using `autoscaling:describe-auto-scaling-groups` API. Accepts `--asg`, `--min-healthy`, `--allow-zero-desired`. Unblocks every CI/CD deploy.

5. **Per-symbol asyncio lock** in `DailyLossValidator.record_fill()`. `self._symbol_locks.setdefault(symbol, asyncio.Lock())` serializes same-symbol fills without blocking cross-symbol concurrency. Dict is bounded by universe size (~50-200 symbols).

6. **Fix three validator docstrings** (MarginValidator, SlippageValidator, SectorConcentrationValidator) that incorrectly described fail-open behavior on missing live data — actual behavior is fail-closed for live, fail-open for paper.

### Terraform Blockers Still Requiring Manual Edit

The following must be hand-edited before `terraform apply`:
1. `prod/main.tf:58`: `single_nat_gateway = false` → `ha_nat = true` (VPC module declares `ha_nat`, not `single_nat_gateway` — plan fails without this fix)
2. Add `sessions` table resource block (provided in `docs/live-readiness/pre-live-runbook.md §5.1`)

### Code Fixes Still Pending

- `strategy_engine/service.py`: candle signal publish retry (B-001) — MEDIUM, pre-live
- `strategy_engine/service.py`: `asyncio.gather(return_exceptions=True)` → `False` (B-002) — HIGH, pre-live
- `execution_engine/service.py`: `_signal_locks` cleanup (HIGH-001) — MEDIUM, pre-Stage-2

### Consequences

- The `sessions` table Terraform addition requires a `terraform apply` — new table creation, no existing data affected.
- The `symbol-status-index` GSI addition to orders is an online DynamoDB update (5-20 min backfill, no downtime).
- EC2 userdata changes take effect only after an ASG instance refresh (new instances read updated userdata).
- `RiskLimits` still loaded from `for_profile()` hardcoded defaults (ADR-021 Phase 2 gap). `risk_limits_production.yaml` is still documentation only.
- Pre-live runbook: `docs/live-readiness/pre-live-runbook.md`.

---

## ADR-023: Phase 3 Code Safety Fixes — Kill Switch, Memory Leak, Staleness Monitor Split

**Date:** 2026-05-30
**Status:** Accepted — all fixes applied

### Context

Phase 3 code safety audit identified five issues requiring fixes before Stage-1 live validation: a memory leak in execution_engine, a boto3 resource recreation hotspot in strategy_engine, two silent failure modes in the strategy_engine processing loop, and the ADR-021 Phase 1 staleness monitor split that unblocks removal of the `RISK_DATA_FEED_STALE_SECONDS=3600` workaround.

### Decisions and Fixes

**HIGH-001 — `_signal_locks` memory leak (execution_engine/service.py)**

`self._signal_locks.setdefault(signal_id, asyncio.Lock())` accumulated one Lock object per processed signal with no cleanup. Over a long session this grows unboundedly.

Fix: `self._signal_locks.pop(signal_id, None)` inserted before every `return` statement inside the `async with signal_lock:` block (5 locations). The pop happens while the lock is held, preventing a race with any concurrent coroutine waiting on the same lock object.

**B-003 — `get_dynamodb_resource()` in kill switch hot path (strategy_engine/service.py)**

`_is_kill_switch_active()` called `get_dynamodb_resource()` on every 1s cache miss, creating a new boto3 Session + DynamoDB resource each time.

Fix: `self._ks_dynamo_table` cached in `start()` once, reused by `_is_kill_switch_active()`. Falls back to creating a new resource if called before `start()` (defensive `or` expression).

**B-001 — Candle signal publish failure silently dropped (strategy_engine/service.py)**

`_candle_processing_loop()` called `await self._publish_signal()` without checking the return value. Kafka delivery failures were silently swallowed with no metric, no alert, no retry — asymmetric with the tick path which raises.

Fix: return value checked; on failure, logs CRITICAL with signal_id/strategy/symbol and emits `CandleSignalPublishFailed` CloudWatch metric. Loop continues (does not raise) because candle signals are not tied to a retriable Kafka message offset.

**B-002 — `asyncio.gather(return_exceptions=True)` in strategy_engine**

A permanently crashed processing loop (e.g., Kafka auth revoked) was silently swallowed by `return_exceptions=True`. The service appeared healthy while generating no signals.

Fix: keep `return_exceptions=True` so all loops run to completion before the gather returns, but iterate the results and re-raise the first non-CancelledError with a CRITICAL log. This surfaces the crash via service exit → systemd restart while still reporting all crashed loops.

**ADR-021 Phase 1 — Staleness monitor split (risk_engine + data_ingestion)**

The single `_monitor_data_staleness()` (threshold used as `RISK_DATA_FEED_STALE_SECONDS`) conflated two distinct failure modes:
1. data_ingestion WebSocket dead → no ticks published to Kafka
2. risk_engine Kafka consumer lagging → signals exist on Kafka but not yet consumed

The 3600s workaround masked mode 2 (consumer rebalancing) at the cost of also masking mode 1 (dead feed). The split separates them:

- **Monitor 3 (renamed `_monitor_consumer_lag`)**: fires when risk_engine receives no signals from Kafka for `consumer_lag_stale_secs` (default 300s). Tolerates consumer rebalancing. `RISK_DATA_FEED_STALE_SECONDS` env var now controls this parameter (set to 300 or omit; remove the 3600 workaround from `.env`).

- **Monitor 5 (`_monitor_producer_heartbeat`)**: fires when `data_ingestion`'s DynamoDB heartbeat key is absent/stale by `producer_heartbeat_stale_secs` (default 60s). data_ingestion writes `HEARTBEAT#{market}/CURRENT` to the latest-prices table every 10s. Disabled if `dynamo_client=None` (backwards-compatible default).

### Consequences

- Remove `RISK_DATA_FEED_STALE_SECONDS=3600` from `.env` — code default of 300s is safe.
- `KillSwitchMonitor(data_stale_secs=...)` parameter is still accepted (backwards-compatible alias for `consumer_lag_stale_secs`).
- Producer heartbeat monitor requires data_ingestion to have IAM write access to latest-prices table (already granted).
- `_signal_locks` dict is now bounded by concurrent in-flight signals (not total historical signals).
- Strategy engine loop crashes are now visible in CloudWatch Logs and trigger service restart.
- Candle signal publish failures are now visible in CloudWatch (`CandleSignalPublishFailed` metric) and can be alarmed on.

---

## ADR-024: Monitoring Agent Phase 2 — Enrichment-Only Severity Engine

**Date:** 2026-05-31
**Status:** Accepted — implemented, 123 tests passing

### Context

Phase 1 of the monitoring agent delivers a coarse per-component `Status` (ok/degraded/down/unknown) with worst-wins roll-up to `overall_status`. This tells an operator *that* something is wrong, but not *how bad* or *how urgent*. An execution_engine that is unreachable and a non-critical sidecar with elevated restart counts are both `DOWN`, but one requires immediate action and the other does not.

Phase 2 was gated on the monitoring LLD being reviewed (ADR implicit in `monitoring_agent_deployment_and_lld.md`). That gate was passed in the previous session. This ADR records the design decisions made during Phase 2 implementation.

### Decision 1: Severity is additive enrichment — it never changes overall_status

The Phase 2 severity report (`SeverityReport`) is attached to the snapshot as `snapshot.severity` and `snapshot.phase = 2`, but it never replaces `overall_status` and never changes when a Slack alert fires (that remains driven by the existing `Status`-based edge-triggered incident log).

**Why:** The incident log is restart-safe and edge-triggered — it replays the on-disk JSONL on restart to restore prior state so it does not re-alert on the same condition after a restart. This property is load-bearing for operational reliability. Wiring alerting to severity (a richer but also more complex classification) would require re-proving restart-safety for the severity track. The additive design preserves all existing guarantees while adding operator visibility.

**Implication:** The existing `test_snapshot_overall_and_json_roundtrip` test (`snap.phase == 1`) passes unchanged because `collect_once` still builds the snapshot with `phase=1`; `run_once` bumps it to `2` after enrichment. Severity is attached only when the engine runs (i.e., in `run_once`); a directly-constructed snapshot has `severity=None`.

### Decision 2: INFO / WARNING / CRITICAL / BLOCKER ladder with capital-protection ordering

Four levels chosen to match operator intuition at trading-platform stakes:

| Severity | Meaning | Example |
|---|---|---|
| INFO | Healthy or informational | All services OK; feed fresh; no error-log matches |
| WARNING | Non-critical impairment — watch | ai_engine down (non-critical); consumer lag approaching threshold; feed mildly stale |
| CRITICAL | Critical component impaired or blind — page someone | risk_engine degraded; Kafka consumer lag above critical threshold; broker feed very stale; UNKNOWN on critical component |
| BLOCKER | Critical component down — trading must not proceed | execution_engine unreachable; Kafka event bus down; risk_engine table DELETING |

Mapping rule: `severity_for_status(status, critical=True/False)` is the default; detectors may override directly from numeric measurements (lag, restarts, staleness).

### Decision 3: Detectors are pure (no I/O) — Phase 2 stays observe-only

Each detector implements `detect(result: CollectorResult) -> list[Finding]` with no I/O. It reads only the `details` dict the Phase 1 collector already produced (guaranteed secret-free by the collector contract). This is what keeps Phase 2 strictly observe-only: there is nothing to block, no network call, no write.

The `SeverityEngine` wraps every `detect()` call in a try/except — a malfunctioning detector cannot crash a collection cycle.

### Decision 4: Lag threshold uses max_lag (worst single partition), not total_lag

A single partition falling far behind is the canonical "consumer is stuck" signal. Using `max_lag` keeps thresholds stable regardless of partition count (a 50-partition topic with total_lag=50000 split evenly is fine; one partition at 50000 is not). `total_lag` is still surfaced in the finding's `context` for the operator.

### Decision 5: Broker staleness is market-hours-aware; off-hours → no finding

A stale feed off market hours is expected (no ticks published). The `BrokerDetector` returns `[]` when `market_open=False`. During market hours: `newest_age > max_age` → WARNING; `newest_age > 3× max_age` → CRITICAL ("very stale — feed likely down").

### Decision 6: Slack severity line only shows CRITICAL/BLOCKER findings by message

The Slack payload appends a one-line severity summary (overall + counts) and individually lists CRITICAL/BLOCKER findings by `message` only. WARNING findings are counted but not individually listed (keeps alerts actionable, not verbose). Raw collector `details` are never sent (they may contain log lines or metric values).

### File map

| File | Role |
|---|---|
| `services/monitoring_agent/detectors/severity.py` | Severity enum, rank, worst_severity, severity_for_status |
| `services/monitoring_agent/detectors/base.py` | Finding dataclass, Detector ABC |
| `services/monitoring_agent/detectors/engine.py` | 6 dedicated detectors + SeverityEngine + SeverityReport |
| `services/monitoring_agent/detectors/__init__.py` | Re-exports all public API |
| `services/monitoring_agent/snapshot.py` | Added optional `severity` field + phase=1 default |
| `services/monitoring_agent/app.py` | run_once: evaluate → attach → persist → transition (Status-based, unchanged) |
| `services/monitoring_agent/notify/slack.py` | format_slack_payload: optional report → severity line + top findings |
| `tests/unit/test_monitoring_detectors.py` | 57 new tests (57 + 66 existing = 123 total, all passing) |

### Consequences

- `snapshot.to_dict()` now includes a `"severity"` key when Phase 2 has run. Consumers that read the snapshot file get richer data; consumers that only check `overall_status` are unaffected.
- Phase 3 (`actions/`) can now read `SeverityReport.findings_at_or_above(Severity.BLOCKER)` to gate risk-reducing actions without re-classifying anything.
- The Self-Improvement Assistant's planned post-session report can include severity breakdown from the snapshot file with no additional collection.

---

## ADR-025: Monitoring Agent Phase 3 — Safe Actions Layer Design

**Date:** 2026-05-31
**Status:** Accepted — framework implemented, 53 tests passing

### Context

Phase 2 (ADR-024) delivers a severity-classified snapshot but takes no actions.
Phase 3 adds the "classify → act conservatively" layer. The key design tension
is: how do we enable autonomous risk-reducing responses while maintaining strict
guarantees that live trading state cannot be accidentally mutated?

### Decision 1: safe_actions lives in execution_engine, not monitoring_agent

The safe_actions module (`services/execution_engine/safe_actions/`) owns the
implementation. The monitoring agent's `actions/__init__.py` re-exports from it.
This keeps the action logic co-located with the services it acts on, and means
the monitoring agent does not need to duplicate knowledge of DynamoDB table names,
Kafka topic names, or kill switch semantics.

### Decision 2: Fail-closed executor — every path writes an audit record

The executor's contract: any unrecognised action type, policy violation,
precondition failure, or unexpected exception returns an `ExecutionResult` with
`blocked=True` or `error=<type>`. An audit record is written for every call —
including blocked and forbidden ones. The caller can unconditionally log the result.

### Decision 3: Docker restart is explicitly forbidden in Phase 3 (and Phase 4)

Restarting Docker containers is not an approved safe action. Reasons: hides root
cause; operationally invasive; does not directly reduce trading exposure; the
EnrichmentWatchdog already handles ai_engine unavailability. The classifier sets
`suppress_docker_restart=True` on every `ClassifiedObservation`. When a service
is DOWN the classifier proposes `SEND_ALERT` + `GENERATE_RUNBOOK_COMMAND`; the
operator runs the command manually. See `docs/runbooks/manual-service-restart-runbook.md`.

### Decision 4: Three action types fully implemented; all others are framework stubs

SEND_ALERT, GENERATE_RUNBOOK_COMMAND, and READ_RUNTIME_STATE are fully implemented
in Phase 3. All DynamoDB/Kafka write actions are framework-complete stubs that
return `stub_not_implemented=True`. This lets Phase 3 pass end-to-end tests and
produce real audit records without requiring the write path to exist.

### Decision 5: Idempotency is in-memory per session

The executor tracks executed `idempotency_key` values in a dict for the session
lifetime. A duplicate key produces `idempotency_skipped=True` and an audit record
but no re-execution. On restart the store resets (acceptable for Phase 3; Phase 4
may persist via JSONL replay).

### Decision 6: Policy uses TradingMode, not raw env vars

The executor never reads environment variables directly. `SafeActionPolicy.from_env()`
resolves `RISK_PROFILE` + `QE_EXECUTION_LIVE_TRADING_ENABLED` to a `TradingMode` enum
once at construction. This makes policy rules testable without monkeypatching env vars
in most tests.

### Forbidden actions (enumerated, not inferred)

The forbidden-context list (`FORBIDDEN_CONTEXT_LABELS`) explicitly names 17 operations
that must never execute. This is a deny-list, not an allow-list — it is additive and
can only grow. Any operation not in the classifier's code table defaults to `ALERT_ONLY`
(conservative unknown-code handling).

### Consequences

- 53 new tests; 176 total passing (safe_actions + Phase 2 + Phase 1).
- The monitoring agent remains in `notify_only` mode; Phase 3 code is present but not
  wired. Operator must set `ACTION_MODE=safe_actions` to enable Phase 4 execution.
- Phase 4 adds the two write handlers (BLOCK_NEW_ENTRIES, ACTIVATE_KILL_SWITCH). See ADR-026.

---

## ADR-026: Monitoring Agent Phase 4 — Only Two Write Handlers

**Date:** 2026-05-31
**Status:** Accepted — implemented, 217 tests passing

### Decision

Phase 4 implements exactly two DynamoDB write handlers:
`BLOCK_NEW_ENTRIES` and `ACTIVATE_KILL_SWITCH`. All other action types remain
stubs (framework-complete but no write). This was explicitly confirmed by the
operator mid-implementation as the correct scoping.

### Why only two

Capital protection is the first principle. Every additional write handler adds
blast radius. BLOCK_NEW_ENTRIES and ACTIVATE_KILL_SWITCH are the two actions that
meaningfully reduce risk in an emergency:

- **BLOCK_NEW_ENTRIES** prevents new exposure from being created. It is the
  minimum viable response to a data quality issue (stale LTP, missing prices)
  that should not halt current positions.
- **ACTIVATE_KILL_SWITCH** halts the trading pipeline entirely. It is the
  correct response to an unmanaged position or a critical infrastructure failure
  that could cause uncontrolled exposure.

All other actions (paper repairs, runbook generation, reconciliation) are either
advisory (the human acts) or lower priority than the two above.

### DynamoDB key decisions

**BLOCK_NEW_ENTRIES → `ENTRY_BLOCK/GLOBAL` in risk-state table.**
There was no existing global entry-block mechanism. The closest existing pattern
is `max_signals_per_day` in strategy-config (per-strategy), but that mutates
strategy config — a higher-risk write. A dedicated `ENTRY_BLOCK/GLOBAL` key in
the risk-state table is narrower, less likely to accidentally affect strategy
parameters, and consistent with the kill switch pattern (a single global boolean
in the same table). The reading side (strategy_engine honoring the flag) is Phase 5.

**ACTIVATE_KILL_SWITCH → `KILLSWITCH/GLOBAL` using `kill_switch_item()`.**
Must use the canonical schema from `shared/risk_state.py` so the existing
`KillSwitch._load_state()` reader in risk_engine parses the item correctly. Using
a different schema would break the existing infrastructure.

### Exit management is unaffected

Both writes intentionally leave exit management (TEE, MIS, ExitOrderRouter) untouched.
BLOCK_NEW_ENTRIES affects only entry-signal production (ENTRY_BLOCK key, not orders/positions).
ACTIVATE_KILL_SWITCH blocks signal approvals but the execution_engine's exit path
operates on fills and scheduled events, not approved signals. Verified in tests.

### Consequences

- 41 new tests (20 unit + 21 integration); 217 total passing.
- The `shared/risk_state.py` gains `ENTRY_BLOCK_PK`, `ENTRY_BLOCK_SK`,
  `entry_block_key()`, and `entry_block_item()` — safe additions, no existing
  behavior changed.
- `SafeActionExecutor` gains `dynamo_writer` optional param — backward compatible
  (None falls back to stub, preserving all Phase 3 test behavior).
- Pre-existing failures in test_mis_square_off.py (20), test_position_reconciliation,
  and test_trade_exit_engine (3) are unrelated to Phase 4 and unchanged.
- Phase 4 wires the executor into `run_once()` and implements the DynamoDB/Kafka write
  handlers. Highest-priority Phase 4 actions: BLOCK_NEW_ENTRIES and ACTIVATE_KILL_SWITCH.

---

## ADR-027: Monitoring Agent Phase 5 — ENTRY_BLOCK Enforcement + ACTION_MODE Gate

**Date:** 2026-05-31
**Status:** Accepted — implemented, 411 tests passing

### Decision 1: EntryBlockReader mirrors the kill-switch reader pattern exactly

The existing `_is_kill_switch_active()` in strategy_engine uses a TTL-cached
DynamoDB read via `asyncio.to_thread()` with a cached DynamoDB Table object.
`_is_entry_blocked()` follows the same pattern for consistency and minimal diff:
`EntryBlockReader` wraps the low-level boto3 client, cache TTL 5s, constructed in
`start()`. This keeps strategy_engine's two safety-flag patterns identical.

### Decision 2: Check entry-block BEFORE runner dispatch, not in _publish_signal()

The user required "do not increment signals_today if signal was never emitted."
`_signals_today` is incremented inside `runner.dispatch_tick()` / `runner.dispatch_bar()`
via `_enforce_daily_cap()`, before `_publish_signal()` is called. So the check must
happen BEFORE dispatch. For ticks, the existing `suppress_signals` pattern is reused
(call `runner._strategy.on_tick()` for indicator update, skip dispatch). For candles,
dispatch is skipped entirely.

### Decision 3: Fail behavior is mode-aware, resolved from RISK_PROFILE

Paper mode (`RISK_PROFILE=paper`): DynamoDB read failure → warn + allow (fail-open).
Operators in paper mode should not be blocked from trading due to a monitoring
infrastructure failure. Live modes: fail-closed (block entries). This is the same
principle as live fail-closed for missing/stale data (CLAUDE.md §Non-Negotiable Safety Rules).

### Decision 4: ACTION_MODE gate in monitoring_agent is opt-in, default notify_only

`MONITORING_ACTION_MODE` defaults to `notify_only` (same as Phase 1). Operators must
explicitly set `safe_actions` to enable autonomous execution. This preserves the
original Phase 1/2 safety contract: "the agent only observes, classifies, records,
and notifies" unless explicitly told otherwise.

### Decision 5: _run_safe_actions() executes only the first proposed action per finding

Executing all proposed actions per finding in one cycle risks race conditions (e.g.
BLOCK_NEW_ENTRIES + SEND_ALERT both writing in the same 30s cycle). Taking the
highest-priority action and relying on idempotency for deduplication is safer.

### Consequences

- 411 tests total passing (Phase 5 + 4 + 3 + 2 + 1).
- `BLOCK_NEW_ENTRIES` is now fully effective: write → read → suppress.
- `MONITORING_ACTION_MODE=safe_actions` is production-gated; no running session is affected.
- Pre-existing failures (mis_square_off, position_reconciliation, trade_exit_engine) unchanged.
