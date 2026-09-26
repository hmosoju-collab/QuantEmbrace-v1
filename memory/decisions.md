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


## ADR-028: Cross-Strategy Position Netting Protection

**Date:** 2026-06-05  
**Status:** RESOLVED — verified by tests + runtime Section 16 gate  
**Trigger:** Session 10 paper trading exposed UNIONBANK SHORT -296 (nse_vwap_reversion) silently reduced to -1 by a competing nse_orb_15m BUY 295 entry signal, followed by exit policy overwrite and TEE stop-out of the residual.

### Context

The execution engine used symbol as the sole position key. When two strategies independently generated opposing signals for the same symbol, the second entry netted against the first, bypassing TEE exit management and overwriting the original exit policy. This is not a P0 in paper mode (paper_trade=True, no real capital) but is a hard blocker for any live trading.

### Root Cause

`apply_fill_to_position` blindly added/subtracted quantity regardless of whether the incoming order was an entry or an exit. `attach_exit_policy` overwrote the active policy unconditionally.

### Decisions

1. **Direction conflict gate** (`check_direction_conflict`): Before accepting any entry signal, read the current position. If an open position exists in the opposite direction, reject the new entry with `DIRECTION_CONFLICT_EXISTING_POSITION`. No order, no fill, no position mutation.

2. **Mode-aware fail behavior**: Paper mode fails open on position-store outage (warn + allow). Live mode fails closed — rejects the entry with `POSITION_STORE_UNAVAILABLE`. This is the same principle as all other live fail-closed rules in CLAUDE.md.

3. **Exit policy protection** (`attach_exit_policy` `entry_signal_id` parameter): If an active policy from a different signal_id already owns the position, block the overwrite and return False. TEE callers (no `entry_signal_id`) bypass the guard.

4. **End-of-session reconciliation** (`check_competing_entry_unwind`, `check_audit_chain`): Post-session gates detect any position reduced by a competing entry (not an explicit EXIT), and any broken signal→order→fill→position audit chain.

5. **Section 16 in monitoring report**: Live counters surface all netting protection metrics in every monitoring report. `gate_pass` is FAIL if any of the must-be-zero counters are non-zero.

### Counter semantics

| Counter | Meaning | Gate |
|---|---|---|
| `netting_conflicts_seen` | Guard detected a conflict | allowed ≥ 0 |
| `netting_conflicts_rejected` | Correctly rejected before order | allowed ≥ 0 |
| `netting_store_outage_live_rejected` | Live fail-closed on store outage | allowed ≥ 0 |
| `netting_rejected_with_order_id` | Rejected signal leaked an order | must = 0 |
| `netting_rejected_with_fill_id` | Rejected signal produced a fill | must = 0 |
| `netting_rejected_with_position_mutation` | Rejected signal mutated position | must = 0 |
| `netting_policy_overwrite_mutations` | Policy overwrite silently succeeded | must = 0 |
| `netting_entry_unwound_recon_failures` | ENTRY_UNWOUND_BY_COMPETING_ENTRY | must = 0 |
| `netting_store_outage_live_allow_through` | Live entry allowed despite outage | must = 0 |
| `netting_audit_chain_breaks` | signal→position chain broken | must = 0 |

### Live promotion gate for this class

RESOLVED — verified by:
- 20 unit tests in `tests/unit/test_cross_strategy_netting.py` (all passing)
- Runtime Section 16 gate in paper_trading_monitor
- `gate_pass` property on `NettingProtectionStatus`

Remaining promotion requirements (not this ADR):
1. 1 clean paper session with Section 16 gate PASS
2. Shadow-live session (broker calls disabled)
3. Limited live (one-symbol whitelist, hard max notional)

### Files Changed

- `services/execution_engine/orders/order_manager.py` — `check_direction_conflict()`, `attach_exit_policy(entry_signal_id, fail_open)`
- `services/execution_engine/service.py` — direction conflict guard (paper + live paths), counter increments
- `services/execution_engine/reconciliation/reconciliation.py` — `ENTRY_UNWOUND_BY_COMPETING_ENTRY` mismatch type, `check_competing_entry_unwind()`, `check_audit_chain()`
- `services/shared/monitoring/monitoring_status.py` — `NettingProtectionStatus`, `LiveCounters` netting fields, `_s16` renderer

---

## ADR-029: MIS Square-Off Task Resilience Fix (HIGH-004 Closed)

**Date**: 2026-06-08
**Status**: Accepted

### Context

Session 12 (first valid quality-gate session) ended with 6 positions unmanaged because the MIS square-off task never fired at 15:05 IST. Investigation showed the `MISSquareOffManager.run()` method used a single `asyncio.sleep(~17000s)` from service startup to the 15:05 fire time. At 15:00:38 IST, the kill switch auto-triggered (Kafka consumer lag after market signal generation dried up). The MIS task was silently cancelled with no log emitted (the `_on_task_done` callback had `if task.cancelled(): return` — completely silent). The same root cause stranded 30 positions in Session 9.

### Decision

1. **Replace the single long sleep with a 60-second polling loop** in `MISSquareOffManager.run()`. Wall clock is re-checked every tick via `_seconds_until_ist(_CLOSE_TIME)`. If the task is cancelled mid-sleep or the event loop stalls, the next 60-second tick self-corrects. This makes MIS robust to any external interference regardless of root cause.

2. **Log CRITICAL on MIS task cancellation** in `service.py` `_on_task_done`. Any future cancellation of the `execution-mis-square-off` task immediately emits a CRITICAL log instead of disappearing silently.

### Consequences

- MIS fires correctly even if the kill switch activates in the same ~15-minute window before 15:05 IST.
- Task cancellation (whatever the cause) is now immediately visible in execution_engine logs.
- All 41 existing MIS unit tests pass unchanged — the polling loop is a drop-in behavioral replacement.

### Files Changed

- `services/execution_engine/mis_square_off.py` — polling loop replaces `asyncio.sleep(wait_secs)` at line 177
- `services/execution_engine/service.py` — `_on_task_done` logs CRITICAL on MIS task cancellation
- `tests/unit/test_cross_strategy_netting.py` — 20 tests (new file)

---

## ADR-030: Week-1 Entry-Economics Rebuild — Universal Viability Gate, ORB v2, VWAP v2, Global Entry Budget, Strategy Retirements

**Date**: 2026-06-10
**Status**: Accepted (operator-approved implementation of the Retail Quant Investment Committee Report, `docs/strategy/retail-quant-investment-committee-report-2026-06-10.md`)

### Context

Sessions 7–15 audit: no paper session was net profitable; the entire daily loss
(₹3.6k–14k) was explained by round-trip costs (~0.20% of notional) exceeding the
strategies' profit targets (median 0.33–0.36%). Session-15 live economics: ORB
WR 22% / PF 0.17, VWAP WR 43% / PF 0.37 — average losers larger than average
winners in both. Three of six strategies were structurally dead, and two latent
bugs hid the problem from monitoring (full root-cause record:
auto-memory `strategy_pnl_root_causes_2026_06_10`).

### Decisions

1. **Universal viability gate** — new `services/strategy_engine/strategies/_viability.py`
   (extracted from scalp_1m's hardened filter). Standing Rules R1/R2: reject any
   signal whose stop < 0.40% of entry or whose target < 2.5 × 0.20% round-trip
   cost. Wired into ORB and VWAP `_build_signal`/`_emit`.

2. **ORB v2** (`orb_strategy.py`): stop = WIDER of OR-midpoint / 1.5×ATR(14) on
   internally aggregated 5m bars / 0.45% floor (was: tighter-of with 1m ATR →
   median 0.163% stops, cost > 1R); breakout requires close beyond boundary +
   0.15×OR-range buffer; volume check rejects when average volume unavailable
   (was fail-open); minimum OR range ≥ 0.5% of price; breakout window 09:30–11:30
   (was –14:45); confidence = proximity-to-boundary + volume ratio (old formula
   rewarded chase distance and pinned at 1.0, max-sizing the worst entries);
   strategy budget 6 signals/day. `strategy_version: orb_v2`.

3. **VWAP v2** (`vwap_reversion_strategy.py`): bands 2.5σ (was 2.0σ); entries
   from 10:15 IST on 60 min of VWAP history (was 09:45/30); reward:risk gate —
   distance-to-VWAP ≥ 1.5 × stop (observed RR was ~1.0–1.1, a structural loser
   at 43% WR); stop floor 0.40%; direction-aware cooldown (15 bars) + max 2
   signals per (symbol, direction) per day (stops averaging into trend days —
   SHREECEM fired SELL 16× on 06-10); strategy budget 6 signals/day.
   `strategy_version: vwap_rev_v2`.

4. **Quality-gate name-mismatch fix** (`paper_quality_gate_validator.py`):
   thresholds are keyed `vwap_reversion`/`orb_15m` in paper_optimization.yaml but
   signals carry `nse_`-prefixed names — the exact-match lookup returned 0.0 for
   every live strategy, so the confidence/RR filters were **silently disabled in
   Sessions 12–15**. `_threshold_for()` now strips `nse_`/`us_` prefixes.
   YAML recalibrated: orb_15m min_confidence 0.93 → 0.65 (orb_v2 confidence
   scale), vwap min_rr 1.20 → 1.40 (backstop under the strategy's 1.5 gate),
   `max_trades_per_day` 20 → 15.

5. **Global daily entry budget** (`symbol_trade_count_validator.py` +
   `risk_engine/service.py`): `GLOBAL_DAILY_ENTRY_LIMIT_REACHED` at 15 filled
   entries/day (YAML `risk.max_trades_per_day`, previously declared but never
   enforced). Exits always exempt.

6. **Strategy retirements** (`scripts/setup_local_tables.py` seeding):
   `nse_scalp_1m` (own viability gate rejects 100% of signals — 1m scalping
   cannot clear retail costs), `nse_preclose_momentum` (fire window sits inside
   the 14:45 entry block; ≤13-min hold fails the cost floor), `nse_momentum_v1`
   (TICK-registered but signal logic in on_bar — never fired; merged into
   trend_15m's validation grid) seeded `enabled=false`. Enabled budgets:
   orb 6/day, vwap 6/day, intraday_trend_15m 8/day.

7. **Ops fixes**: (a) `monitoring_status.py` ENTRY_BLOCK read failed on every
   call — `source`/`reason`/`status` are DynamoDB reserved words in the
   ProjectionExpression (now aliased) AND the inline `from shared.risk_state`
   import broke under host-script sys.path (now relative `..risk_state`);
   (b) `paper_trading_monitor.py` read `{prefix}-prices` (nonexistent) instead
   of `{prefix}-latest-prices` — the §5 LTP column silently fell back to fill
   prices for every position on every session. Monitoring now renders GREEN
   with live LTPs/ages against the running stack.

### Consequences

- Expected trade count drops from ~90/day to ≤15/day; cost drag from ₹6–8k/day
  to <₹1.5k/day. Signals must clear the cost floor before emission.
- intraday_trend_15m remains unable to fire until its 15m warm-up replay lands
  (Week-2 item) — the book trades ORB v2 + VWAP v2 only until then.
- Session 16 is the first session with the quality gates actually active —
  Sessions 12–15 were NOT valid quality-gate tests (name-mismatch bug).
- Tests: `tests/unit/test_week1_entry_economics.py` (33 tests) + all touched
  suites pass per-file (scalp 13, runner 31, gate-log 4, netting 20,
  monitoring 126, TEE 72). Pre-existing failures noted, not touched:
  `test_strategy_config_loader.py` (4 stale PK/SK-format assertions) and
  cross-file stub pollution when `test_monitoring_status.py` runs before
  `test_cross_strategy_netting.py` in the same pytest process.

### Files Changed

- `services/strategy_engine/strategies/_viability.py` — new shared gate
- `services/strategy_engine/strategies/orb_strategy.py` — orb_v2
- `services/strategy_engine/strategies/vwap_reversion_strategy.py` — vwap_rev_v2
- `services/risk_engine/validators/paper_quality_gate_validator.py` — prefix-tolerant lookup
- `services/risk_engine/validators/symbol_trade_count_validator.py` — global entry budget
- `services/risk_engine/service.py` — budget wiring + startup log field
- `services/strategy_engine/config/paper_optimization.yaml` — recalibrated thresholds
- `scripts/setup_local_tables.py` — per-strategy budgets + retirements
- `services/shared/monitoring/monitoring_status.py` — ENTRY_BLOCK read fixes
- `scripts/monitoring/paper_trading_monitor.py` — latest-prices table name
- `tests/unit/test_week1_entry_economics.py` — 33 tests (new file)

---

## ADR-031: alpha_engine — Shadow Alpha-Forecasting Layer (Untracked WIP, Secured 2026-06-13)

**Date**: 2026-06-13 (secured in checkpoint commit `011ef9a`; original coding date unknown)
**Status**: Committed but INACTIVE — not wired into live signal path, not validated

### Context

During the 2026-06-13 WIP checkpoint audit, a fully untracked service `services/alpha_engine/`
was discovered on disk — never committed since its creation. It predates Sessions 16 & 17
and was one `git checkout` away from being permanently lost. It was committed in checkpoint
`011ef9a` as-is, without modification.

### What It Is

A shadow/advisory alpha-forecasting service that sits **outside** the live signal path.
It is meant to run as an observer: label trades with realized outcomes, score strategies,
and rank alpha sources. It does NOT generate signals, does NOT place orders, and has no
connection to execution_engine or risk_engine.

**Packages committed** (`services/alpha_engine/`):
- `cost/` — NSE statutory cost model (STT, txn, GST, stamp, SEBI, margin) for alpha net-of-cost computation
- `gates/kill_switch_gate.py` — advisory kill-switch gate (read-only; does not activate)
- `labeling/` — trade labeling (entry/exit tagging, realized outcome computation)
- `models/` — `alpha_model.py`, `registry.py`, `strategy_alpha_adapter.py` — model abstraction and strategy adapter
- `publishers/alpha_shadow_publisher.py` — publishes alpha scores to a shadow Kafka topic (not created yet)
- `ranking/` — alpha ranking and score aggregation
- `research/` — research utilities (replay, correlation, attribution)
- `store/` — alpha score persistence (DynamoDB + S3)
- `universe/` — universe-level alpha scoring
- `service.py` — service entrypoint (not in docker-compose; never started)

**Also committed**:
- `scripts/alpha/alpha_registry.py` — registry of alpha models for replay
- `scripts/alpha/run_alpha_replay.py` — batch replay driver
- `services/shared/models/alpha.py` — shared `AlphaScore` dataclass

**Tests**: 11 test files in `tests/unit/test_alpha_*.py` + `tests/integration/test_alpha_shadow_flow.py`

### Decisions

1. **Not wired into live/paper path** — `alpha_engine` is never started by docker-compose and
   has no Kafka consumer group registered against any production topic. Trading behavior is unchanged.

2. **Not validated** — no paper session has been run with alpha_engine active. Score quality
   and reliability are unknown. Do not use for trading decisions until validated.

3. **No ADR yet for its design** — this ADR records its existence only. A full design ADR
   is needed before any integration work begins.

4. **Governance invariant applies** — consistent with CLAUDE.md: "GenAI can explain.
   GenAI cannot trade." alpha_engine may produce recommendations; it must never change
   trading behavior directly.

5. **Coupling note** — `shared/models/__init__.py` imports `shared.models.alpha`; this means
   all services transitively import the `AlphaScore` dataclass. This is a latent coupling that
   should be cleaned up when alpha_engine is properly designed.

### When To Pick Up

- After ≥5 valid quality-gate paper sessions confirm ORB v2 / VWAP v2 edge
- As a Week-3+ item, with its own design review and ADR

---

## ADR-032: Backtesting Lab Phase 15C — Momentum Walk-Forward Verdict: PAPER_OPTIMIZATION

**Date:** 2026-06-14
**Status:** Accepted — advisory complete, Phase 16 = continue paper sessions
**Reports:** `docs/backtesting/aws-phase15b-walk-forward-report.md`, `docs/backtesting/aws-phase15c-walk-forward-corrected-report.md`

### Context

Phase 15B ran walk-forward validation on `momentum` (SMA crossover) with a 4-combo param grid
(`sw=5/lw=20`, `sw=10/lw=50`, `sw=20/lw=100`, `sw=10/lw=100`) using the `default` preset
(12m IS / 3m OOS / 3m roll). The result was REJECT — but the root cause was a methodology
defect: `lw=100` requires ≥100 bars to warm up, and a 3-month OOS window has only ~60 trading
days. 8 of 15 folds had 0 trades, contaminating the aggregate metrics.

Phase 15C ran two corrected variants to isolate the strategy edge from the methodology defect.

### Decision: `momentum sw=10/lw=50` has confirmed positive OOS edge — verdict PAPER_OPTIMIZATION

**Primary result — Phase 15C-M (medium preset, 24m IS / 6m OOS):**

| Metric | Value | Gate |
|---|---|---|
| Mean OOS expectancy | +₹90/trade | >₹0 → PASS |
| Mean OOS profit factor | 1.520 | >1.2 → PASS |
| Total OOS net P&L | +₹22,039 | >₹0 → PASS |
| IS→OOS degradation | 0.30 | <0.50 → overfit WARNING |
| Win consistency | 0.60 | >0.50 → OK |
| Parameter stability | 1.00 | >0.70 → OK |
| Verdict | **PAPER_OPTIMIZATION** | |

All 5 folds selected `sw=10, lw=50` as the IS-best parameter set (stability = 1.00).
`lw=100` was never selected even in 24-month IS windows — `sw=10/lw=50` is the stable,
dominant parameter.

**Supplementary — Phase 15C-F (lw≤50 filter, default preset, 15 folds):** REJECT
(win consistency 0.40 — 3-month OOS windows too short for regime-stable signal extraction).

### Regime sensitivity (expected for SMA crossover)

Strategy wins in trending/bull regimes: post-COVID bull 2021, H2 2022 recovery, 2023 bull.
Strategy loses in bear/choppy regimes: H1 2022 Russia/Ukraine bear, early 2024 elections.
This is intrinsic to SMA crossover momentum — not a defect to be fixed.

### Consequences

1. **Advisory only.** This result is non-authoritative for promotion. Live trading remains
   BLOCKED. A human approves all production changes.
2. **Phase 16 = continue paper sessions (Option A).** The 5-consecutive valid quality-gate
   paper session gate has not been cleared (Session 17 was not counted — negative expectancy +
   Bug 5/Bug 6 unfixed; Session 18 is the first counting session after those fixes).
3. **`sw=10/lw=50` is confirmed as the canonical momentum parameter for advisory use.**
   If the strategy is ever added to the paper session universe (future decision), these are
   the parameters to start with. No further IS optimization needed before paper validation.
4. **Daily data momentum is regime-sensitive.** If and when the momentum strategy is activated
   in paper sessions, an overriding regime filter (e.g., NIFTY 50 above/below 200-day MA)
   would be a logical first enhancement — consistent with the NiftyRegimeGate pattern
   already deployed for ORB v2 and trend_15m.
5. **Intraday strategies not yet validated.** `vwap_reversion`, `trend_15m`, `orb`, and
   `preclose` cannot be walk-forward validated without intraday (1m/5m/15m) data.
   Phase B (intraday data acquisition) is a future option when prioritized by the operator.
   → **Superseded 2026-06-14 by ADR-033** (Zerodha Kite intraday tooling built).
- Must remain advisory-only: no output may flow into `signals.pending` or `signals.approved`

---

## ADR-033: Backtesting Lab Phase B — Zerodha Kite Intraday Fetch + Backtest Tooling

**Date:** 2026-06-14
**Status:** Accepted — tooling built + self-tested; real fetch is operator-run (pending)
**Supersedes:** ADR-032 §5 ("Phase B is a future option")
**Docs:** `docs/backtesting/zerodha-intraday-fetch-and-backtest.md`
**Tests:** `tests/backtest/test_intraday_fetch_and_backtest.py` (12) — full backtest suite 188 passing

### Context

ADR-032 left the 4 intraday strategies (`orb`, `vwap_reversion`, `trend_15m`, `preclose`)
unvalidated for lack of intraday data. Operator asked to "work with intraday data
(GlobalDataFeeds)" to evaluate them.

**Source decision — NOT GlobalDataFeeds.** No GDF data or credentials exist in the workspace,
and GDFL only retains ~3 months of 1-min history (too shallow for walk-forward — per our own
`intraday-data-procurement-memo.md`). Operator chose the **Zerodha Kite `historical_data` API**:
free with the base Connect sub, exchange-validated candles, ~3 yr of 1-min depth for liquid
names — the procurement memo's sanctioned "limited intraday" tier. `scalp_1m` excluded
(Stage-1 disabled, lowest priority).

### Decision

Built two operator-run scripts that reuse the existing engine end-to-end (no rewrites):

1. `scripts/backtest/fetch_zerodha_intraday.py` — Kite → Parquet lake
   (`interval=1m|5m|15m`, same layout as the daily Bhavcopy backbone), chunked by Kite's
   per-interval request caps (minute=60d, 5m/15m larger), idempotent (skip existing
   symbol/interval/year), manifest records `source=zerodha_kite`, `trust=HIGH`,
   `license=kite-connect-personal-use`. Mirrors `candle_prefetch.py` (direct KiteConnect,
   env creds, 3 req/s) + `download_bhavcopy.py` (Parquet lake). `--self-test` runs offline.

2. `scripts/backtest/run_intraday_backtest.py` — **per-day** backtest of the 4 strategies.
   Reuses `data_loader.load_candles`, `strategy_adapter.get_adapter`, `Backtester`,
   `compute_metrics`/`evaluate_gates`. Writes `aws-phaseB-intraday-backtest-report.md`.
   `--self-test` builds a synthetic lake and runs the pipeline.

3. `services/backtesting/s3_data_catalog.py` — added `zerodha`/`zerodha_kite`/`kite` to
   `_HIGH_TRUST_SOURCES`. The data-lake contract §1 already classifies Zerodha
   `historical_data` as HIGH trust; the code had omitted it (doc/code drift fixed).

### Key design rationale

- **Per-day execution is mandatory for correctness.** The intraday strategies are
  session-based (ORB opening range, VWAP, per-day signal budgets) and depend on
  `reset_daily()` — which the live MarketPhaseGovernor calls at POST_CLOSE but the offline
  `Backtester` never does. So we run **one strategy instance per trading day across all
  symbols**: fresh instance = daily reset; Backtester close-at-last-bar = MIS EOD flatten;
  one instance across the universe preserves each strategy's *global* daily signal budget.
- **Fixes the Session-16 "ORB blind" defect** — historical bars include the full 09:15–09:30
  opening range, so the range always forms (Session 16's live start was 10:19 IST).

### Trust vs depth (recorded separately, deliberately)

Zerodha is **HIGH trust for provenance** (exchange-validated, operator's own authorized feed)
but **LIMITED depth** (~3 yr, liquid names). Trust ≠ coverage. Results are **advisory edge
exploration**, never a 15-yr authoritative backbone. A positive result warrants procuring
deeper licensed vendor data (TrueData / GlobalDataFeeds) before further validation.

### Consequences / limitations (also in every report)

1. **Advisory only.** Cannot promote. Live trading remains BLOCKED. A human approves all
   production changes. The fetcher places **no orders** (test-guarded); the runner touches
   **no broker/Kite** at all (test-guarded).
2. Per-day capital reset → Sharpe/annualised return unreliable; expectancy / profit factor /
   win rate are the valid edge metrics.
3. `trend_15m` warm-up is intra-day only (~25 15m bars/day) → indicative, not conclusive.
4. Exits modeled by the Backtester (per-bar stop/target + EOD flatten), not the live TEE/MIS.
5. ~~Blocked on operator: fetch needs a fresh Kite token~~ — **DONE 2026-06-14**: operator
   logged in, token exchanged in-process (never persisted/printed; Docker/LocalStack was down
   so the DynamoDB path was bypassed), fetched **16.1M bars, 46/47 NIFTY50 symbols × 1m/5m/15m
   × 2022-2024** (TATAMOTORS tradingsymbol unresolved — backfill pending). Lake = 610 MB.

### Backtester multi-symbol EOD bug — FOUND + FIXED during first real run (2026-06-14)

The first intraday backtest produced physically impossible numbers (orb "win rate" 4194%,
net ₹8.5cr; preclose net ₹21.7cr). Root cause was **not** the data (verified clean — no bar
jumps >15%) and **not** the per-trade logic (individual trades were sane). It was a latent bug
in the **shared `Backtester.run()` EOD flatten** (`backtester.py:401`): it collected the last
bar for *every* symbol and broke once it had `len(open_positions)` entries, so an open-position
symbol whose last bar came later fell back to `bars[-1].close` — **a different symbol's price**
(e.g. a ₹107 BEL position marked out at ₹2751). The momentum walk-forward never hit it (single
symbol per backtest); the multi-symbol intraday runner did.

**Fix:** only record last bars for symbols that hold a position; close each at its own last bar
(`backtester.py:400-413`). Single-symbol behavior unchanged. Regression-pinned by
`test_eod_multi_symbol_uses_own_symbol_price` (tests/unit/test_momentum_backtester.py). Also
fixed a cosmetic win%-×100 double-scaling in the runner. Full backtest suite: 192 passing.

### Phase B results (post-fix, advisory) — ALL intraday strategies REJECT or inconclusive

| Strategy | Trades | Win% | Exp ₹ | PF | Net ₹ | Verdict |
|---|---|---|---|---|---|---|
| orb (1m) | 2403 | 33.2 | -94 | 0.45 | -225,949 | REJECT |
| vwap_reversion (1m) | 131 | 22.9 | -174 | 0.25 | -22,791 | REJECT |
| trend_15m (15m) | 0* | — | — | — | 0 | REJECT (0 at default; relaxed loses) |
| preclose (5m) | 10596 | 12.0 | -84 | 0.05 | -886,007 | REJECT |

Report: `docs/backtesting/aws-phaseB-intraday-backtest-report.md`. All three trading strategies
lose to the NSE statutory cost stack — consistent with [[strategy_pnl_root_causes_2026_06_10]]
and [[feedback_india_cost_mandate]].

**trend_15m resolved (warm-start mode added):** the runner now supports opt-in warm-start
(`WARM_START_STRATEGIES={"trend_15m"}`) — ONE persistent strategy instance whose OHLCV buffers
accumulate across days, `reset_daily()` between days (resets daily counters, keeps buffers),
relying on `initialize(None)` being non-destructive. This is the backtest analogue of the live
ADR-031 DynamoDB warm-start. With it, trend_15m's EMAs warm fully (buffer 150 ≫ 52 needed) yet
it STILL fires 0 signals at its production config: ADX≥25 and confidence≥0.65 are **mutually
exclusive on NIFTY50 15m data** (each alone admits ~46–75 signals; together 0). With both filters
off, the raw trend logic trades ~4,857 times (2023–24) but loses (exp −₹76, PF 0.30, net −₹368k).
NIFTY regime gate disabled for the backtest (no NIFTY index intraday in the lake; fails-open in
prod anyway). So trend_15m has **no edge** either way — joins the REJECT family.

**No intraday strategy is eligible for paper prioritisation on this evidence.** scalp_1m not
tested (Stage-1 disabled). Momentum-on-daily (Phase 15C, PAPER_OPTIMIZATION) remains the only
strategy with a positive advisory edge.

---

## ADR-034: Strategy Thesis Redirection — Intraday → Positional Cross-Sectional Factors

**Date:** 2026-06-15
**Status:** Accepted — advisory. Live trading remains BLOCKED. No capital moves on this ADR.
**Memo:** `docs/strategy/strategy-thesis-redirection-2026-06-15.md`
**Evidence:** `docs/backtesting/factor-study-report.md`, `scripts/backtest/run_factor_study.py`

### Context

Phase B (ADR-033) + paper Sessions 16–17 + the cost arithmetic all agree: the platform's
intraday technical strategies (orb/vwap/trend_15m/preclose) have no edge after the NSE cost
stack. The operator chose to **reconsider the whole strategy thesis** rather than grind more
paper sessions toward a gate this strategy set cannot clear.

### Decision

Reorient from **short-horizon intraday technicals on liquid names** (the hardest cell: small
moves × highest cost frequency) to **horizon-appropriate, edge-source-driven, positional/CNC
strategies where expected move ≫ cost.** Edge must clear cost in the *backtest* before any
capital is staged in front of the live gate.

Beachhead = **daily cross-sectional equity factors** (the daily Bhavcopy lake — 2,983 symbols,
2019–2025, survivorship-robust, delivery_pct fully populated — already supports this).
alpha_engine is repurposed as a **search/scoring harness, not a strategy.** Options are
**deferred** (different risk surface, NOT a safety upgrade under capital-protection-first).

### Evidence (factor study, long-only top-20, monthly, full delivery cost stack, 2020–2025)

Every factor clears costs net (cost drag ~3–8% of CAGR) — night-and-day vs intraday. BUT most
raw return is **beta** (EW benchmark itself 16–20% CAGR this bull regime). The one **robust,
regime-stable, risk-adjusted edge is the India-specific `delivery-%` conviction factor**: it
beats the benchmark Sharpe in BOTH sub-periods (1.88 vs 0.96; 1.20 vs 1.09) with lower drawdowns
(−10.5%, −20.3%) and 72% hit rate. The full-period combo (Sharpe 1.43) is flattered by 2020–22
and only matched the benchmark in 2023–25; momentum/reversal are mostly beta.

### Consequences

1. **Advisory only — direction, not readiness.** Cannot promote. Live trading BLOCKED. A human
   approves all production changes. The 5-session paper gate is unaffected.
2. **Carry `delivery-%` (and a delivery-tilted combo) to the next research gate** — walk-forward
   (like Phase 15C) + out-of-regime stress (no sustained bear in sample) + a trend/regime overlay
   — then paper validation as a **positional/CNC** book. The other factors do not earn it.
3. **Drawdowns are equity-sized** (−10% to −35%): positional/overnight risk, the opposite end
   of the axis from intraday. Position sizing + regime overlay matter before capital.
4. **Stop the intraday paper-session grind** (Session 18+) on strategies that cannot clear the
   gate. Daily momentum (Phase 15C) + delivery-% factor are the positive-edge candidates worth
   paper-validating next — a positional model (overnight CNC), distinct from the MIS intraday
   pipeline now in place.
5. Caveats on the evidence: one broad regime, ffill-on-delisting + trade-at-close mild optimism,
   no walk-forward yet. Robust enough to set direction, not to size positions.

### ADR-034 addendum — delivery-% walk-forward + regime overlay (2026-06-15)

Report: `docs/backtesting/delivery-walkforward-report.md`. `scripts/backtest/run_delivery_walkforward.py`.

- **Walk-forward: delivery-% positive in 5/5 calendar years OOS** (2021 +49%, 2022 +2.3%,
  2023 +40%, 2024 +11%, 2025 +5%). Non-parametric → cannot be curve-fit; never-negative across
  five independent years is real robustness. Full-period net CAGR 22.8%, Sharpe 1.41, MaxDD −20%.
- **200d regime overlay HURTS** (−6.9 pts CAGR, Sharpe 1.41→1.13; in 2022 turned +2.3% into
  −8.9%) — trend filters whipsaw on the V-shaped dips that are in sample. **Overlay NOT adopted.**
  Its real target (sustained bear) is **untestable** — lake starts Oct 2019, no 2008/2011/2018
  bear. Better risk mgmt = sizing / portfolio DD limit, not a market overlay.
- **Decision:** carry **delivery-% (no overlay)** to paper validation as a positional/CNC book —
  strongest positional candidate. NOT promotable: major-bear case unvalidated (data limit),
  drawdowns equity-sized, paper validation against the live gate required first. Acquire pre-2019
  daily history if the bear question must be answered before scaling.

---

## ADR-035: Delivery-% Positional Paper Book — Isolated Advisory Harness (Standup)

**Date:** 2026-06-15
**Status:** Accepted — isolated advisory harness built + inaugural basket generated. Live
trading remains BLOCKED. Integration into the real pipeline is a SEPARATE approval-gated step.
**Tool:** `scripts/paper/run_delivery_paper_book.py` · **Builds on:** ADR-034.

### Context

ADR-034 + walk-forward established delivery-% as the one robust positional edge (Sharpe 1.40,
positive 5/5 years OOS). Operator chose to stand it up as a positional/CNC paper book.

### Decision — isolated advisory harness first

A positional/CNC book (monthly rebalance, overnight holds, NO MIS square-off, cross-sectional
basket) is a **different trading model** from the intraday/MIS pipeline. Retrofitting that
pipeline is a big lift that risks the intraday safety machinery. So the first standup is a
**fully isolated, advisory harness**: own JSON state under `backtest-data/paper_book/`
(gitignored), no broker, no Kite, no DynamoDB live/paper tables, no MIS, no Kafka. It simulates
fills against the daily lake at the rebalance close + full delivery cost stack, tracks
NAV/holdings, and is idempotent per rebalance date. Seed NAV ₹10L, top-200 liquid universe,
long-only top-20 equal-weight, 2% cash buffer, 8% per-name cap.

### ETF/fund contamination — found + fixed during standup

The first inaugural basket held `LIQUIDBEES`/`LIQUIDCASE` (cash funds), `GOLDBEES`, `NIFTYBEES`
— NSE ETFs trade in the EQ segment with trivially ~100% delivery %, so the factor ranked them
top. Added `_drop_funds` (ticker-pattern filter; ISIN would be cleaner but the lake's isin is
unpopulated) to the shared harness — factor study, walk-forward, and paper book all inherit it.
**The delivery edge survives the exclusion unchanged (Sharpe 1.40 either way)** — genuine equity
selection, not an ETF artifact. Inaugural clean basket (as of 2025-12-31): 20 high-delivery
defensive-quality large-caps (ITC, HUL, NTPC, POWERGRID, MARICO, HDFCBANK, KOTAKBANK, SUNPHARMA,
TITAN, MARUTI, ULTRACEMCO, APOLLOHOSP, MAXHEALTH, MANKIND, LUPIN, INDHOTEL, HEROMOTOCO, HYUNDAI,
ICICIGI, BHARTIARTL).

### Consequences / open decisions (operator-gated)

1. **Advisory only.** No broker, no live/paper state touched. Live trading BLOCKED. A human
   approves all production changes. This is NOT the intraday 5-session gate — a monthly strategy
   can't be validated in 5 sessions; the backtest is the edge estimate, the harness proves
   plumbing + accrues slow OOS. The book needs its OWN gate (e.g. N months tracking the backtest
   net-of-cost within tolerance) before any real integration.
2. **Data staleness:** the lake ends 2025-12-31; the book was initialised as of that date. To run
   it truly forward, refresh the Bhavcopy lake (re-run `download_bhavcopy.py` for 2026) monthly
   and rebalance at each month-end.
3. **Integration to real paper-broker (deferred, needs approval + design):** placing CNC paper
   orders, overnight risk handling (no MIS), monitoring/reporting wiring, and how the positional
   book coexists with the intraday platform. Do NOT wire into execution_engine without a design.
4. **Risk management:** position sizing / portfolio drawdown limit (NOT a 200d market overlay —
   that hurt; ADR-034 addendum). Drawdowns are equity-sized (−22%).
5. **Bear caveat stands:** no sustained bear in the lake; acquire pre-2019 history before scaling.

---

## ADR-036: QE Phase-Next — Promotion Framework, Delivery PRE-PRODUCTION, Factor Correlation Verdict

**Date:** 2026-06-15 · **Status:** Adopted (advisory governance). Live trading remains BLOCKED.
**Doc:** `docs/strategy/qe-phase-next-cio-operating-doc-2026-06-15.md`.
**Evidence:** `docs/backtesting/factor-correlation-report.md` (new),
`scripts/backtest/run_factor_correlations.py` (new), factor study + delivery walk-forward (ADR-034).

### Context

Following the intraday retirement (ADR-033) and the factor pivot (ADR-034), the platform needed a
*standing promotion framework* (so promotion is mechanical and evidence-weighted, not ad-hoc) and a
resolution of the open diversification question that both the IC report and the operating doc had
flagged as INSUFFICIENT EVIDENCE.

### Decisions

1. **Delivery-% reclassified RESEARCH → PRE-PRODUCTION (entry).** It cleared the backtest +
   walk-forward bar (Sharpe 1.40, +5/5 OOS years) but has zero forward-paper and zero bear-regime
   evidence — far from PRODUCTION. It enters a **12-month** forward paper program (a monthly
   strategy can't be validated faster). Promotion to PRODUCTION requires: 12-mo forward tracking +
   regime (bear) evidence (or explicit operator bear-risk acceptance) + QE Score ≥ 80 + operational
   readiness.

2. **QE Promotion Score adopted** (0–100): OOS persistence 25 · benchmark-relative alpha 20 ·
   operational stability 15 · diversification contribution 15 · drawdown profile 15 · simplicity 10.
   **Hard veto invariants** (auto-KILL, score void): net-negative after costs · target below product
   cost floor · unresolved harness artifact · invalid universe. Bands: PRODUCTION ≥80 (+ gates),
   PRE-PRODUCTION 65–79, WATCH 45–64, KILL <45. The bar is deliberately high — false alpha costs
   capital + trust, and live is blocked so there is no urgency premium.

3. **Factor correlation study completed — combined equity-factor book DEMOTED.** Added a `value`
   factor (price-based reversion **proxy**; a true value factor needs fundamentals the lake lacks —
   flagged, and added without perturbing the published factor study) and ran a diversification study
   across delivery/momentum/lowvol/value. **Finding:** the four long-only equity sleeves are highly
   correlated (delivery/lowvol 0.84, delivery/value 0.75, momentum/value 0.73 — three pairs above the
   0.70 veto); **blending DILUTES** (best blend Sharpe 1.19 < delivery 1.40; diversification ratio
   1.14 < 1.20 target); and in the worst-decile months **lowvol's correlation to delivery rises to
   0.99** (most redundant exactly in drawdowns) while momentum/value decouples to ~0. Within
   long-only NSE equity factors in this single bull regime, diversification is **illusory**.

### Consequences

- **Delivery-% standalone remains the lead candidate** — do not rush a combined factor book.
- **Research prioritization re-ordered** (operating doc §7): equity-factor diversifiers (H1/H2/H3)
  demoted; genuinely *different* return drivers promoted — **H4 (Delivery Spike) build now**
  (lake-ready), **H5 (PEAD) top data-acquisition priority**.
- **Regime-expansion project = TIER-1.** The correlation finding is itself regime-limited (one bull,
  no bear in sample), which strengthens the case for pre-2019 data before any combined-book or
  scaling decision.
- Risk control stays position sizing / portfolio drawdown limit, NOT a market-timing overlay.
- Governance unchanged: advisory only, backtesting recommends but cannot promote, live BLOCKED, a
  human approves all production changes. The delivery book stays isolated; nothing wired into
  execution_engine. Tests: `tests/backtest/test_factor_correlations.py` (10 tests; full backtest
  suite 199 green).

### ADR-036 addendum — H4 delivery-spike event study: REJECTED (2026-06-15)

Built and ran the top lake-ready different-driver hypothesis (`scripts/backtest/run_delivery_spike.py`,
`docs/backtesting/delivery-spike-report.md`, `tests/backtest/test_delivery_spike.py` — 6 tests).
Event = delivery-% z ≥ 2 + volume ≥ 1.5× median + delivery ≥ 50%, top-200 liquid, funds excluded.
**No-lookahead respected:** delivery % is published post-close, so every event enters at **close[t+1]**
(unit-tested). 2,278 events, 2020–2025.

**Result — REJECTED.** Nominal forward returns are positive (T+20 +1.87%) but that is **pure beta**;
the **abnormal returns vs an EW market index are significantly NEGATIVE at every horizon** (t = −2.1
to −3.5; per-trade abnormal-net t = −4.6 to −6.7). The calendar-time portfolio Sharpe 0.95 < market
1.09. So a delivery spike does **not** predict positive drift — if anything post-spike names mildly
**underperform** (anti-predictive / contrarian). Clean negative result; recorded, not carried
forward as a long event signal. **Per policy we do NOT tune-to-fit** — flipping to a short/contrarian
read would be a different hypothesis needing its own economic rationale, not a parameter sweep.

**Consequence:** delivery information appears to live in the *persistent level* (the monthly factor,
which works) rather than in *spikes* (which don't). Next different-driver priority shifts to **H5
(PEAD)** — which needs an earnings-event panel the lake does not yet hold — and the **regime-expansion
project** (Tier-1). The H4 harness (event detection + event study + calendar portfolio + cost model)
is reusable for H5 and other event hypotheses.

### ADR-036 addendum — H5 PEAD event study: REJECTED (2026-06-15)

Built `scripts/backtest/run_pead_study.py` reusing the full H4 harness (EventStudy, event_study,
per_trade_net, calendar_portfolio, _daily_metrics). Two modes: H5a (real earnings calendar) and H5b
(price-implied large-move proxy: |return| > 3σ AND volume > 2× median on top-200 liquid universe).
NSE corporate announcements API returns 404 in automated sessions (bot-shield); BSE fallback also
failed (ISIN column detection bug — fixed, BSE re-run pending). **H5b proxy ran with 1,907 positive
and 1,013 negative surprise events, 2020–2025.** Report: `docs/backtesting/pead-study-report.md`.

**Result — REJECTED (H5b).** Identical anti-predictive signature to H4:

| Horizon | Nominal return | Abnormal return | t-stat |
|---------|---------------|-----------------|--------|
| T+15    | +1.32%        | **−0.45%**      | −2.01  |
| T+21    | +2.03%        | **−0.67%**      | −2.49  |
| T+42    | +3.81%        | **−1.20%**      | −3.02  |
| T+63    | +5.65%        | **−1.92%**      | −3.98  |

Per-trade abnormal-net all negative (t = −1.55 to −3.83 across 5d→42d holds). Calendar portfolio
Sharpe 0.74 vs market Sharpe 1.07. Large-move event stocks **underperform** the market post-event
in NSE — same pattern as H4. Not carried forward.

**Consequence:** Both H4 (delivery spikes) and H5b (price-implied large moves) confirm the same
structural finding: NSE large-cap event stocks revert or underperform in the weeks following a
discrete trigger event. The delivery information edge lives in the **persistent monthly factor
level**, not in event responses. H5a (true PEAD with real earnings calendar) remains open — BSE
API re-run in progress (2026-06-15) — but the H5b result already makes rejection likely; a
positive H5a would be a surprise requiring its own explanation. **Regime-expansion (Tier-1)**
remains the active gating work — extend the lake to 2016–2018. **Data limitation confirmed
2026-06-15:** NSE legacy `cm` bhavcopy (pre-2019) has OHLCV but NO delivery columns; delivery-%
factor stress-testing pre-2019 requires a paid data provider. `download_bhavcopy.py` updated
to support the legacy ZIP URL format; pre-2019 Parquet rows will have `delivery_pct=NULL`.
Run: `python scripts/backtest/download_bhavcopy.py --start 2016-01-01 --end 2018-12-31`
(re-run after the background job `bluyntj91` completes with 0 files using the old URL format).

### ADR-036 addendum — Regime-expansion factor study 2016–2025: COMPLETE (2026-06-15)

Extended factor study (`run_factor_study.py --start 2016-01-01 --end 2025-12-31`) on the full
lake after downloading pre-2019 OHLCV (delivery_pct=NULL for 2016–2018). Report:
`docs/backtesting/factor-study-extended-2016-2025-report.md`. Key findings:

- **Momentum (Sharpe 0.63 > market 0.59)** — genuine pre-2019 bear-regime evidence. Survived
  2016 demonetization and 2018 IL&FS crisis with positive net alpha and lower MaxDD (−38% vs
  −49.3% for market). Confirmed as second verified factor alongside delivery.
- **Combo (Sharpe 0.72)** — beats market even when running 3-factor (no delivery 2016–18).
- **LowVol (Sharpe 0.59 = market)** — risk reducer, not alpha. MaxDD −31%.
- **Reversal** — killed by costs (18.6% cost drag, 0.5% net CAGR). Retired.
- **Delivery (Sharpe 0.38) — ARTIFACT.** Three years of forced-cash (NULL 2016–18) mechanically
  collapses the full-period Sharpe from 1.40 to 0.38. Not a signal-quality finding.
  Clean delivery result remains the 2019–2025 study: Sharpe 1.40, 5/5 OOS years positive.
- Delivery-% IL&FS regime test: STILL OPEN (requires paid data). COVID crash (Feb–Mar 2020)
  IS in the delivery window; delivery returned +5% OOS in 2020.

### ADR-036 addendum — Delivery-% paper book: FIRST monthly forward OOS read (2026-06-19)

Tool: `scripts/paper/replay_delivery_book_forward.py` (new — deterministic monthly walk that
reuses the validated `_rebalance`/cost logic; re-run after each Bhavcopy refresh to advance the
record). Report: `docs/backtesting/delivery-paper-book-forward-report.md`. This is the first real
data in the ADR-036 12-month PRE-PRODUCTION forward program.

- **Fixed contaminated state.** The saved book held a single Dec-2025→Jun-2026 jump + 3 same-day
  re-rebalances (idempotency guard lives in the CLI `main()`, not in `_rebalance`, so an ad-hoc
  driver double-counted). Rebuilt deterministically from inception 2025-12-31.
- **First OOS result — n=5 complete months (+1 partial), NOT validation.** Book cumulative
  **−9.85%** vs equal-weight liquid benchmark **−0.16%** → **−9.7 pts behind the market**.
  Current NAV ₹901,531 (seed ₹10L). Monthly alpha vs benchmark: Jan −3.6, Feb +3.3, Mar +1.0,
  Apr **−8.4**, May −1.9, Jun(part) −1.1.
- **Read (honest, both directions).** Damage concentrates in April: market +15.0% V-recovery
  (after a −11.6% March crash), defensive high-delivery book only +6.6% — the textbook (1−β) drag
  of a low-beta book through a sharp round-trip that ends ~flat. Consistent with the correlation
  finding that delivery ≈ a low-vol/defensive tilt (delivery↔lowvol 0.84, →0.99 in drawdowns).
  So this is "the factor's defensive beta profile showing up live," NOT proof the signal is broken.
  BUT: the backtest's Sharpe-1.40 edge has not appeared in the first OOS quarter-plus, and a
  defensive factor only pays in a defensive-rewarded regime — i.e. exactly the (pre-2019,
  data-blocked) bear case. This is a **weak/negative start** that tempers PRE-PRODUCTION optimism.
- n=5 has ~zero statistical weight — do not over-update either way. Monthly rebalancing also
  slightly *underperformed* buy-and-hold the Dec basket here (₹901k vs ~₹921k): turnover cost
  without return in this one window (n=1, not conclusive).
- **Decisions (unchanged safety; refined direction):** do NOT promote; do NOT kill on 5 months;
  keep accruing monthly (re-run the replay each refresh). The delivery bear test stays data-blocked
  (no pre-2019 delivery%); paid pre-2019 delivery data remains the TIER-1 unblock. Worth considering:
  stand up a PARALLEL momentum paper book (2nd verified factor, bear-robust, decouples from delivery
  in drawdowns) using the same harness, so the forward program tracks both verified factors.
  Live trading remains BLOCKED.

### ADR-033 addendum — cost-model correction + formal retirement register (2026-06-19)

Phase 1 audit (`scripts/backtest/phase1_strategy_audit.py`, by-year + by-regime) found that the
Phase B intraday backtest charged `IndianCostModel.delivery()` (0.222% round-trip) on MIS intraday
strategies; the correct model is `IndianCostModel.intraday()` (0.035% round-trip, ~6× cheaper;
~0.14% incl. 5bps/leg slippage, slippage-dominated). Re-run at the correct MIS cost: per-trade
losses roughly halved (orb −₹94→−₹41, preclose −₹84→−₹37) but **none flipped positive** — the
retirement is robust to the cost model.

**Formal retirement (register: `docs/strategy/strategy-retirement-register-2026-06-19.md`):**
orb / vwap_reversion / intraday_trend_15m / preclose_momentum all **RETIRED**. PF < 1 in EVERY year
(2022/23/24) and EVERY regime (uptrend/downtrend vs 50d SMA) → cause of death = **no persistent
gross edge on liquid NIFTY50 at intraday horizons**, not regime-dependence/decay/leakage. preclose
worst (PF 0.25, 14.5 trades/day, −39.5% DD = frequency×cost). `scalp_1m` = PARKED (never validated,
do not activate). `momentum` (daily) = KEEP (verified factor). Implication for any future intraday
work: must use the **correct MIS cost model** and be **low-frequency, large-move** (event-conditioned).

### Phase 2 C1 — overnight-gap reaction study: real-but-thin, SHELVED (2026-06-19)

Tool `scripts/backtest/run_gap_reaction_study.py`; report `docs/backtesting/gap-reaction-study-report.md`.
Event-conditioned intraday equity (NIFTY50 5m, 2022-2024), 2-stage (parameter-free event study →
a-priori rule), CORRECT MIS costs, 5 & 10 bps/leg slippage, by year + regime.

- **Data-quality catch:** first pass measured gap as Kite-5m-open ÷ bhavcopy-daily-prev_close — the
  two sources adjust splits differently → fake −42% "gaps" on 10,186 post-split days (large_down).
  Fixed: measure gap ENTIRELY within the 5m source (prev_close = prior day's last 5m close) + 25%
  sanity cap. Clean panel symmetric (large_down 127 / large_up 132).
- **Signal IS real (event study):** large gaps CONTINUE (large_up +0.32% t=+2.12; large_down −0.45%
  t=−2.12); mid up-gaps FADE (−0.073% t=−3.21, n=4,232). 
- **But not tradable:** fade is only ~7bps < ~13.5–23.5bps cost wall (real inefficiency, sub-cost).
  Continuation clears cost in aggregate (PF 1.41@5bps / 1.22@10bps, Sharpe 1.73/1.06) BUT n=259/3yr
  (~7/mo), NOT year-consistent (2022 +29 / 2023 −5 LOSES / 2024 +43 bps; carried by 2024), marginal
  at 10bps, stronger leg = shorting.
- **Verdict: SHELVE — do NOT carry C1 to paper.** Best intraday result obtained but building on a
  259-trade, 2024-carried, year-inconsistent effect = fitting one year. Reinforces ADR-034: retail
  intraday equity on liquid names is cost-walled (signal real but sub-cost). No other Phase 2
  candidate built (C2/C3 data-blocked, C4/C5 retread/poor-fit). Positional/factor track remains the
  platform's only positive-edge direction. Live trading BLOCKED.

### ADR-036 addendum — momentum paper book stood up; verified factors DECOUPLE live (2026-06-19)

Generalized the paper-book harness to `--factor {delivery,momentum}` (backward-compatible; delivery
replay reproduces −9.85% exactly = no regression). `run_delivery_paper_book._target_basket` now also
does 12-1 cross-sectional momentum; `replay_delivery_book_forward.py --factor momentum` writes
`momentum_book_state.json` + `momentum-paper-book-forward-report.md`.

Momentum book forward (Dec-2025→Jun-2026, SAME window as delivery): **+5.43% vs bench −0.16% (+5.6pts),
NAV ₹1,054,297.** Monthly alpha Jan −3.1 / Feb +2.5 / Mar −0.7 / Apr **+5.3** / May +3.6.

**KEY FINDING — the two verified factors DECOUPLE live, as the in-sample correlation study predicted:**
over identical months momentum +5.4% vs delivery −9.9% (~15pt spread), driven by opposite April
behavior (market +15% V-recovery: momentum +20.4% caught it / delivery +6.6% lagged). Momentum is
higher-beta/higher-vol (Mar −12.4, Apr +20.4 = momentum-crash risk) and won only because the window
ended on an up-leg; delivery is defensive and cushions selloffs. They are **regime-complementary** —
the diversification value is real and showed up forward. n=5 (NOT validation either way); the
transferable insight is the **DECOUPLING**, not either point estimate.

Implication: neither factor alone; a **delivery+momentum COMBINED book** (two decorrelated drivers —
unlike the diluting all-equity-factor blend in the correlation study) is the next research step, and
needs its own backtest, not just this window. Both books now forward-tracked; advance monthly. Live
trading remains BLOCKED.

### ADR-036 addendum — delivery+momentum COMBINED book: diversification thesis FAILS (2026-06-19)

Tool `scripts/backtest/run_combined_book_study.py` (reuses run_factor_study); report
`docs/backtesting/combined-book-study-report.md`. Tested a 50/50 sleeve (+ inverse-vol) of the two
verified factors over PROPER history (2020-01-01 → 2026-06-12), not the 5-month forward window.

- **The 5-month forward decoupling was a small-sample REGIME ARTIFACT.** Full-history leg
  correlation = **+0.67** (positive, not negative) — the Dec25→Jun26 divergence (delivery −9.9% vs
  momentum +5.4%) was a single Mar-crash/Apr-recovery event; over 6 years the two co-move (both
  long-only equity beta). Clean vindication of "don't trust 5 months."
- **No diversification benefit:** combo 50/50 Sharpe 0.86 (vs delivery 0.85 / momentum 0.75), MaxDD
  −28.9% — WORSE than delivery alone (−26.4%) and ~benchmark (−25.8%). The drawdown prize (the whole
  point) is not delivered. inverse-vol combo ~same (Sharpe 0.87).
- **Both standalone edges have DECAYED:** delivery Sharpe 1.60 (2020-22) → **0.61 (2023-26)**;
  full-period delivery now 0.85 vs the 1.40 reported for 2020-2025 (weak 2026 dragged it down,
  consistent with the −9.85% forward read). Combo strong years (2021 +44%, 2023 +48%) are behind it;
  2024-26 flat-to-negative.
- **None of the long-only equity books convincingly beats the EW benchmark** risk-adjusted over the
  full window (delivery 0.85 / momentum 0.75 / combo 0.86 vs benchmark 0.83). Early-window factor
  edges have largely washed out.

**Decisions:** (1) do NOT build a combined forward book — it doesn't diversify. (2) Genuine
diversification must come from a driver structurally decorrelated from long-only equity beta
(market-neutral/long-short or different asset/structure), NOT another long-only equity factor
(echoes the ADR-036 correlation verdict). (3) delivery remains the best single equity book but its
edge is weakening — the running forward paper books are the truth serum; keep accruing. (4) Broad
lesson reinforced: apparent edges keep dissolving under proper/OOS testing — the high bar before
deploying capital is vindicated. Live trading remains BLOCKED.

### ADR-036 addendum — Forward Factor Gate pre-registered; posture = ACCRUE, DEPLOY NOTHING (2026-06-19)

Operator chose (b): let the forward paper books be the OOS truth-test; deploy no capital until a book
clears a pre-registered gate FORWARD. Gate doc: `docs/live-readiness/forward-factor-validation-gate.md`;
checker `scripts/paper/check_forward_gate.py` (self-tested).

**Forward Factor Gate (FFG), fixed 2026-06-19 — must NOT be relaxed:** ≥12 complete forward months ·
cumulative alpha vs EW liquid benchmark > 0 · monthly-alpha IR ≥ 0.50 · ≥58% positive-alpha months AND
no single month > 50% of cum alpha (anti one-regime-illusion) · forward MaxDD ≤ benchmark. Clearing →
HUMAN REVIEW for a small gated pilot, NEVER auto-deploy. Targets ALPHA not raw return (most return is
beta). Separate track from the intraday 5-session gate. Live trading remains BLOCKED.

**Current (5/12 months):** delivery IN PROGRESS, failing early (cum alpha −8.7%, IR −1.67, 1 bad month
= 77% of alpha); momentum IN PROGRESS, passing 4/6 early but higher risk (MaxDD −15.2% > bench −13.7%).
Both eligible ~Dec-2026. Monthly cadence: refresh Bhavcopy → re-run both `replay_delivery_book_forward.py`
→ `check_forward_gate.py`. This closes the active research arc — nothing more to build; accrue forward.

### Phase 2 C7 (turn-of-month) + C6 (overnight) — both screened (2026-06-19)

Tool `scripts/backtest/run_calendar_overnight_study.py`; report `docs/backtesting/calendar-overnight-study-report.md`.
EW NIFTY50, daily lake 2016-2026, corp-action guard ±20% (daily bhavcopy is unadjusted — splits land
in the overnight segment, the C1 trap).

- **C7 turn-of-month: NO standalone edge.** Mild real concentration (in-window days [last+first-3]
  ~12.8 bps/day vs rest ~5.1 bps/day, ~2.5×) BUT a long-in-window/flat timing strategy returns only
  5.9% (ETF cost)/4.6% (cons.) ann, Sharpe 0.84/0.67 < buy-hold 17.8%/1.11 — sitting in cash 82% of
  days sacrifices more than the concentration is worth. SHELVE (cash-overlay variant = low-risk
  cash-plus, not equity-beating). India SIP flows real but not exploitable as pure in/out timing.
- **C6 overnight: REAL, dramatic, NOT retail-tradable — and it EXPLAINS THE WHOLE PROJECT.** The
  entire NSE large-cap premium accrues OVERNIGHT: overnight +13.1 bps/day, cum **+2333%**, Sharpe
  **3.20**; INTRADAY is structurally NEGATIVE: −5.9 bps/day, cum **−79%**, Sharpe **−1.10** (total
  buy-hold +411%). Harvesting overnight needs a daily round-trip → net +8%/yr @0.10% cost but −20%/yr
  @0.22% (delivery) → cost-dead for retail; and a long-only holder ALREADY captures it (no incremental
  edge). **This is the unifying reason every intraday strategy failed** (Phase B retirements, C1): NSE
  large-cap intraday isn't merely cost-walled — it is a NEGATIVE-DRIFT desert. The positive premium
  lives in holding overnight / positionally = the (forward-tracked) factor track.

**Decision:** neither becomes a strategy. Intraday-equity research is now CLOSED with a structural
explanation (C6). Standing posture unchanged: accrue the two forward factor books vs the pre-registered
Forward Factor Gate, deploy nothing. Live trading BLOCKED.

### Options / Volatility track OPENED — Phase O-1 free VRP screen built (2026-06-19)

Operator asked for paid 3–5 yr historical data + new strategies; chose the **options/vol** track. Key
honesty: buying more EOD *equity* data buys nothing (10 yr free lake already exhausted that ground —
intraday is a negative-drift desert per C6, all factors decayed/correlated). The only structurally
*different* untested NSE return source is the **volatility risk premium** (implied INDIA VIX > realised).

**Data reality:** Kite `historical_data` needs an instrument_token and **expired weekly-option tokens are
purged** → 3–5 yr option *chains* are NOT cheaply available from Kite (needs paid vendor: Algotest export
/ GDFL / TrueData). But the *premium itself* is measurable from FREE underlying inputs (INDIA VIX + NIFTY
spot). ⇒ strict **two-phase, cheapest-first** plan.

**Phase O-1 (FREE) — BUILT + offline-tested 2026-06-19:**
- `scripts/backtest/fetch_zerodha_indices.py` — reuses the EQ fetcher's plumbing; pulls NIFTY50 +
  INDIA VIX daily (segment=INDICES) into the lake. Self-test PASS. (Added `1d` interval to
  `fetch_zerodha_intraday.py::_INTERVALS` — one-line additive.)
- `scripts/backtest/run_vol_premium_study.py` — pre-registered VRP screen. VRP = VIX − NIFTY realised
  vol over next 21 td (forward-realised = ex-post measurement of "did sellers get paid", NOT lookahead).
  Coarse monthly short-straddle-vega ₹ proxy vs an estimated defined-risk condor cost stack; mandatory
  tail report. Self-test PASS both directions (premium-present→PASS, no-premium→SHELVE).
- **Pre-registered O-1 gate (fixed 2026-06-19, do NOT relax):** G1 mean VRP > 1.0 vp · G2a VIX>RV ≥65%
  days · G2b +mean VRP ≥70% years · G3 median gross ≥2× median cost & net>0. PASS ⇒ authorises *buying
  chain data for O-2 only*, NEVER deployment. Spec: `docs/backtesting/options-vol-track-spec.md`.

**Phase O-2 (PAID, only if O-1 PASSES):** buy NIFTY option-chain history; build `IndianCostModel.options()`
(Zerodha ₹20 flat/leg dominant on small size + STT 0.1% sell premium + txn 0.035% + GST + stamp);
backtest **defined-risk only** (credit spreads / condors, never naked) with bid/ask, walk-forward,
per-regime, explicit crash-tail stress. PASS ⇒ human review for a small gated pilot, never auto-deploy.

**Operator prerequisite:** fetch runs LOCALLY with a same-day Kite token (sandbox has no api.kite.trade
egress); confirm Kite Connect historical-data API add-on is active. **No spend yet.** Live BLOCKED.

### Options/Vol O-1 RUN → PASS (2026-06-19)

Ran `run_vol_premium_study.py` on FREE LOW-trust data (Yahoo ^NSEI / ^INDIAVIX, 2020–2025, 1,449 days,
69 monthly cycles). (Kite request_token expired before exchange → lake not populated this run; yfinance
is fine for an O-1 screen by design.) **VERDICT: PASS** (all 4 pre-registered gates):
mean VRP **+2.34 vp** · median +3.06 · VIX>realised **79%** of days · positive-mean VRP in **100%** of
years (2020 +1.66…2025 +2.56). By regime: bear +4.08 / bull +3.28 / chop +1.98 — premium positive in
every classified regime; SMA warm-up (early-COVID spike) −23.34 = the acute vol-spike onset is the
killer. ₹ straddle-vega proxy: median gross ₹9,357 vs cost ₹329 → net ₹8,998/lot/cycle. ⚠️ **FAT LEFT
TAIL**: worst cycle −₹109,627 (11.7× median gross), cum-DD −₹119,604 — classic short-vol steamroller.
Fixed a CSV date-parse bug (`dayfirst=True` NaT'd ~60% of ISO yfinance dates → fake ~100% realised vol →
a spurious first SHELVE that was correctly rejected as garbage before trusting it).

**Meaning:** the NSE index VRP is real, large, consistent → PASS authorises *buying option-chain data for
an O-2 defined-risk backtest ONLY*. NOT deployment. The fat tail makes defined-risk structures (condors/
credit spreads, never naked) + explicit crash-day stress mandatory in O-2. Live BLOCKED; no spend yet —
next is the operator's vendor decision (Algotest export / GDFL / TrueData).

**O-1 CONFIRMED on HIGH-trust Kite data (2026-06-19).** `kite_fetch_with_token.py` (no-DynamoDB one-shot,
built because `zerodha_login.py` burns request_tokens when LocalStack is down) populated the lake
(segment=INDICES, NIFTY50 + INDIAVIX, 1,492 daily bars each, 2020–25). Lake screen: mean VRP +2.48 vp,
VIX>realised 80% days, +VRP 100% years, worst cycle −₹111k (10.6× median), all 4 gates PASS — matches the
Yahoo run within noise. Cross-source agreement → PASS stands on broker-grade data. Next = operator O-2
vendor/spend decision (Algotest export / GDFL / TrueData). Live BLOCKED; defined-risk only; never deploy.

**O-2 HARNESS BUILT + VALIDATED (2026-06-19, zero spend).** `scripts/backtest/run_options_vol_backtest.py`:
`OptionsCostModel` (₹20/leg flat + STT 0.1% sell premium + txn 0.035% + GST + stamp — flat fee dominates
retail size; distinct from equity IndianCostModel) + Black–Scholes (erf, no scipy) + iron-condor build &
bounded expiry payoff + ≤2%-NAV/cycle risk budget (hard tail cap) + pre-registered O-2 gate (expectancy>0,
PF>1.3, ≥60% pos-years, maxDD≤20%, defined-risk cap held). Self-test PASS (BS parity exact; condor bounded
at ±wing; +VRP→PF 1.89; crash→loss capped; zero-VRP→costs turn negative). Synthetic-on-REAL-NIFTY-path
(incl Mar-2020): 32 cycles, PF 1.65, expectancy ₹2,690, maxDD −5.7%, worst cycle −₹17.6k inside ₹20k
budget → cap held through COVID; all 5 gates PASS. **⚠️ The synthetic PASS is BY CONSTRUCTION (IV set =
realised + 3 vp) — it validates the ENGINE + cost stack + tail cap, NOT a real edge.** Real-chain mode is
a documented schema contract (trade_date/expiry/strike/opt_type/spot/price), not yet wired — needs O-2
vendor data (Algotest/GDFL/TrueData). Report: `docs/backtesting/options-vol-backtest-report.md`. Defined-
risk only; PASS on REAL chains ⇒ human review for a small gated pilot; never auto-deploy; live BLOCKED.

**O-2 chain source = NSE F&O Bhavcopy (free EOD), NOT Zerodha (2026-06-19).** Operator asked to use
Zerodha for 3-yr NIFTY option chains; corrected: Kite CANNOT backfill expired option chains (each
contract is a separate instrument; `instruments("NFO")` lists only live contracts; expired weekly/monthly
tokens are purged; no endpoint for historical instrument dumps). The free source that DOES have 3+ yr of
all NIFTY option strikes/expiries is the NSE **F&O (derivatives) bhavcopy** — same archive family as the
equity bhavcopy. EOD-only, which suffices for our held-to-expiry monthly condor (needs entry-day premiums
+ expiry underlying). Operator approved.

BUILT + offline-validated 2026-06-19 (zero spend):
- `scripts/backtest/download_fo_bhavcopy.py` — reuses equity downloader's NSE bot-shield session; handles
  BOTH formats (legacy `fo{DDMMMYYYY}bhav.csv.zip` pre-2024-07 + UDiFF `BhavCopy_NSE_FO_..._F_0000.csv.zip`),
  filters NIFTY index options (OPTIDX / FinInstrmTp=IDO), writes chain lake
  `backtest-data/lake/options/underlying=NIFTY/date=*/part-0.parquet`. Self-test PASS (both formats).
- `run_options_vol_backtest.py --chain` — `_load_fo_chains` + `backtest_real`: monthly condor on real
  strikes/premiums/skew, held to expiry, settled at NIFTY50 lake spot, per-leg slippage haircut (default
  2.5%; EOD close isn't a guaranteed fill). build_condor now snaps to nearest available strike. Self-test
  PASS (real-chain path: 64 cycles, slippage erodes edge ₹2,351→₹1,130, crash cap held).

NEXT (operator runs LOCALLY — sandbox has no NSE egress): `download_fo_bhavcopy.py --start 2022-06-01
--end 2025-06-30` (804 trading days) → then I run `run_options_vol_backtest.py --chain backtest-data/lake/options`
for the first REAL O-2 verdict. EOD PASS ⇒ intraday-vendor re-test (Algotest/GDFL) before any pilot; never
auto-deploy; live BLOCKED.

### O-2 FIRST REAL VERDICT → FAIL (2026-06-20)

Ran `run_options_vol_backtest.py --chain` on the downloaded NSE F&O bhavcopy lake (760 trading days,
2022-06→2025-06, 1.24M NIFTY option rows). **Two harness bugs caught + fixed before trusting any verdict**
(same discipline as the VRP date-parse bug): (1) `groupby(month).max()` pulled NIFTY long-dated/quarterly
expiries → restricted to genuine ~monthly entries (DTE 20–40d); (2) sub-1-lot cycles were silently dropped
(selection bias toward cheap condors) → now take ≥1 lot (disclosed capital-adequacy caveat). Cycle count
8 → 31 (proper monthly).

**Result (31 cycles, 4% OTM shorts / 2% wings / held-to-expiry / 2.5% slip):** net −₹53,953, ann −2.1%,
**PF 0.67, expectancy −₹1,740, pos-years 50%** → **FAIL** (maxDD −8.7% and defined-risk cap held; the
edge criteria fail). By year: 2022 −₹30.6k, 2023 −₹49.0k, 2024 +₹21.9k, 2025 +₹3.8k.

**Decomposition = EDGE problem, not cost problem.** At ZERO slippage with only ₹4,225 costs it STILL
loses gross −₹34,729 (PF 0.75). Win rate 65% (20/31) but credit/max-loss = **0.25** → needs ~81% win
rate to break even; achieves 65%. Losses cluster in directional 2022–23. **ATM VRP (O-1 +2.5 vp) does NOT
convert to a profitable OTM condor** — at the wings, net of put skew + directional risk, the premium isn't
there. Consistent with the whole engagement: screens look great, real implementation dissolves.

**Discipline:** do NOT parameter-fish a 31-cycle sample to force a PASS (overfitting). This FAIL is one
reasonable untuned structure on EOD data over a short-vol-unfriendly sub-period — not a full refutation of
options-vol, but clear evidence the naive harvest fails. Report: `docs/backtesting/options-vol-backtest-report.md`.
Governance: does NOT advance to a pilot. Live BLOCKED. Capital note: ~₹25k defined risk/condor = 5% of a
₹5L account/cycle — structurally capital-heavy for the target account.

### Options-Vol structure sweep → SHELVE the track (2026-06-20)

Pre-declared robustness sweep (`run_options_vol_sweep.py`, report `options-vol-sweep-report.md`): OTM
{1,2,3,4,5}% × wing {1,2}% = 10 monthly held-to-expiry condor configs on the same free EOD F&O bhavcopy.
**0/10 clear the O-2 gate; only 2/10 are even gross-positive at zero slippage (5%/1% +₹7.7k, 5%/2% +₹12.7k
over 3 yr ≈ <0.5%/yr — noise, negative after costs).** Shape: closer-to-ATM catastrophic (1%/1% −₹318k PF
0.27 DD −30% — directional moves blow through shorts); far-OTM collects ~nothing. No sweet spot. The FAIL
is STRUCTURAL — the NSE index VRP is real ATM/frictionless (O-1) but NOT harvestable by any retail-affordable
static defined-risk structure after put skew + directional risk + costs.

**DECISION: SHELVE the options/vol track.** The only untested lever is intraday active management
(stops/rolls/profit-targets) which needs PAID intraday data — hard to justify when every static structure
loses gross. Consistent with the whole engagement: real implementation dissolves the screen-level edge.
Return to STANDING POSTURE: forward factor books accrue monthly vs the pre-registered Forward Factor Gate,
DEPLOY NOTHING. Live BLOCKED. Don't re-propose static index short-vol without a genuinely new angle
(e.g., intraday-managed, or a different underlying/structure) + fresh data. Tooling retained + reusable
(fetch_zerodha_indices, run_vol_premium_study, download_fo_bhavcopy, run_options_vol_backtest, _sweep).

### F1 overnight index-futures premium — SCREEN PASS (with material caveats), 2026-06-20

`run_overnight_futures_study.py` (NIFTY50 spot OHLC proxy, 2020-2025, 1491 days; basis/roll omitted —
F1-full refinement). **Passes all 4 pre-registered gates.** Core finding is REAL: overnight premium
**+11.3 bps/night SURVIVES the futures cost stack** (round-trip ~0.023% vs cash 0.22% — the cash killer
per C6); net Sharpe 1.98; **positive ALL 6 years** (2020 +₹349k … 2025 +₹69k on 1 lot). Most promising
edge in the whole engagement.

**BUT the NAKED leveraged form is account-inappropriate for ₹5L:**
- maxDD **−41.1%** (bottomed 2020-03-20 COVID; cluster of −4%…−9% overnight gaps). My pre-registered gate
  LACKED a max-DD criterion — an honest gate flaw; a −41% DD violates capital-protection regardless.
- Gap tail SCALES with index level, and index tripled: worst 2020 nights hit at index ~8-10k (so −6%…−12%
  NAV, which is why G3 passed), but 2025-04-07 already = −17% NAV in one night, and the SAME −9% COVID gap
  at today's ~26000 = **−35% of NAV in a single night.** G3 passed on a historical accident, not forward safety.

**Verdict:** the overnight premium is real + cost-surviving + 6/6 years — but naked leveraged carry is too
dangerous. Deployable hedge-level form = **long future + protective OTM put (risk-capped overnight carry)**.
Decisive next question: does +11 bps/night survive the cost of overnight downside protection? (priceable
from our NIFTY options EOD lake). NEXT: F1-hedged test (data in hand) + F1-full on REAL futures (extend
download_fo_bhavcopy for FUTIDX + re-run) with a DD-aware gate. Report:
`docs/backtesting/overnight-futures-study-report.md`. Live BLOCKED; never deploy; advisory only.

**F1-full pipeline BUILT (2026-06-20, awaiting operator futures download).** Operator chose F1-full (real
futures) before the hedged variant. Built + self-tested (zero spend): `download_fo_futures.py` (dedicated
index-FUTURES bhavcopy downloader, NIFTY+BANKNIFTY, both NSE formats, reuses options downloader's session;
separate script so the tested options path is untouched; re-fetches daily zips to extract FUTIDX → lake/
futures/underlying=*/), and `run_overnight_futures_study.py --futures` = `study_futures` (near-month
front-contract overnight: each night buy front-month [nearest expiry > t] at close, sell next open — basis
in prices; **stricter DD-aware 5-gate adds G5 maxDD≤25%** = the criterion the screen gate lacked). Both
self-tests PASS (futures path: 103 nights from synthetic lake, gate has G5). NEXT: operator runs LOCALLY
`download_fo_futures.py --start 2022-06-01 --end 2025-06-30` (re-downloads ~760 zips for futures) → then
`run_overnight_futures_study.py --futures` = real-futures F1 verdict. Expect naked to FAIL the DD-aware gate
(screen showed −41% DD) → confirming the hedged variant is the deployable path. Live BLOCKED; advisory.

### F1-full REAL futures → FAIL; F1 DEAD (naked AND hedged) — 2026-06-20

Ran `run_overnight_futures_study.py --futures` on real near-month NIFTY futures (760-day lake 2022-06→
2025-06, 733 nights, basis+roll in actual contract prices). **Overnight only +3.2 bps/night, net Sharpe
0.34, ann +6.1% → FAIL G4** (Sharpe≥1.0 & ann≥12%). G1/G2/G3/G5 pass this period (beats cost, +ve most
years, tail/DD within limits) but the risk-adjusted return is far too low; 2025 already NEGATIVE (−₹24k).

**Decomposition (spot vs futures, SAME 2022-06→2025-06 window):** spot proxy +8.9 bps Sharpe 2.09 → real
futures +3.2 bps Sharpe 0.34. Period effect minor (11.3→8.9 across windows); **the killer is −5.7 bps lost
to basis-decay + the non-tradable NIFTY index "open" (opening-snapshot artifact you can't trade at).** C6's
overnight premium is REAL as an index statistical property but ~2/3 is NOT harvestable on the tradable
instrument. **F1-hedged is also dead:** at +3.2 bps the binding failure is RETURN (G4), not the tail — a
protective put adds cost to fix a non-binding tail, making the failing metric worse. No point building it.

**Methodology lesson (the real prize):** the spot/index proxy overstated the harvestable edge ~3.5×.
Testing on REAL futures BEFORE building the hedge (operator's call) avoided chasing an +11 bps phantom into
a hedged build. **Always validate on the actual tradable instrument, not an index/spot proxy.**

F1 DEAD. Options/futures program continues: Tier-1 remaining = O1 event IV-crush, F2 futures trend. Live
BLOCKED. Tooling retained+reusable (download_fo_futures, run_overnight_futures_study --futures).
Report: `docs/backtesting/overnight-futures-study-report.md`.

### O1 scheduled-event IV-crush → SHELVE (2026-06-20)

`run_event_vol_study.py` (INDIA VIX + NIFTY50, 28 curated events 2020-2025; budgets/election HIGH conf,
RBI best-effort). Mean crush +0.7vp, win 68%, but event short-straddle net ₹2,062 vs **random-day baseline
₹2,009 = edge 1.03×** (the baseline control = the decisive test) → **SHELVE** (fails G1 crush>1.0 & G3
beats-baseline).

**Decomposition (airtight):** Budget (n=6) crush +1.9vp but net −₹8,995 (big moves: 2021 +7.4%, 2022 +2.5%
eat the crush); Election-2024 crush +2.1vp net −₹26,777 (−2.8% move); RBI (n=21) crush only +0.3vp net
+₹6,594 (no real crush — just ordinary VRP on calm days). **Where the crush is real the MOVE is real and
short vol loses; where short vol wins there's no crush.** HIGH-confidence events net −₹11,535 (worst, not a
date artifact). Pre-event IV is fairly-priced compensation for risk that materializes — efficient, not excess.
Same dissolve-on-real-risk pattern as everything else.

O1 DEAD. Options/futures Tier-1 remaining: F2 futures trend (low prior). Report:
`docs/backtesting/event-vol-study-report.md`. Live BLOCKED. Tooling retained+reusable.

### F2 index-futures trend/momentum → SHELVE (drawdown-reduction, not alpha) — 2026-06-20

`run_futures_trend_study.py` (NIFTY50 spot signal, 1 futures lot P&L w/ cost + carry drag, 2020-2025;
pre-declared grid lookback{20,50,100,200}×{long-only,long-short}). Buy&hold 1 lot: ann +12.6%, Sharpe 0.42,
maxDD **−71.8%** (leverage → near-ruin in COVID). Trend long-only lb20-100: ann ~12% (**= buy-hold, no
return added**), Sharpe 0.54-0.59, maxDD ~−25%. Long-short all bad (short side run over by the bull).
Only lb=100 long-only clears the gate (Sharpe>B&H + maxDD≤25% + ann>0) — an **isolated, threshold-marginal
pass** (lb20/50 miss only on −26/−27% DD).

**Honest verdict: SHELVE.** F2's effect is **DRAWDOWN REDUCTION, not alpha** (same as lowvol "risk reducer
not alpha" + the 200d overlay) — it cuts the −72% leveraged buy-hold DD to ~−25% but adds NO return, and the
whole result hinges on dodging ONE crash (2020) = n≈1 independent trend in the window → luck/known-effect, not
a deployable standalone edge for ₹5L. Useful only as a risk overlay IF leveraged NIFTY futures were held
anyway. Report: `docs/backtesting/futures-trend-study-report.md`.

**OPTIONS/FUTURES PROGRAM EXHAUSTED AT TIER-1: F1 dead, O1 shelved, F2 shelved.** Tier-2 (F3 basis, O2
calendars) low prior; Tier-3 (intraday options) data-blocked. Return to STANDING POSTURE: forward factor
books accrue vs the pre-registered gate, deploy nothing. Live BLOCKED. All tooling retained + reusable.

---

## ADR-037: QuantEmbrace v2 Re-Architecture Approved — One Deterministic Engine, Three Clocks

**Date:** 2026-07-05 · **Status:** Adopted (operator-approved). Live trading remains BLOCKED.
**Doc:** `architecture/re-architecture-2026-07.md` (RA-1, all four phases).

### Context

The 2026 research program (capstone `docs/strategy/research-program-consolidation-2026-06-20.md`)
eliminated every tested strategy family; the firm's validated product is research, not order flow.
Meanwhile the Kafka/MSK microservice trading stack (~51k LOC) produced the platform's dominant bug
classes (6 of 19 paper sessions invalidated by config/image drift; 4 kill-switch self-refire bugs;
7–12s self-inflicted signal age worked around by raising a safety threshold) while research ran on
~30 bespoke script harnesses. Four divergent execution semantics exist (live strategies,
backtester.py, lab replay engine, per-study loops) — the same proxy-divergence class that overstated
F1's edge 3.5×.

### Decision

Rebuild QuantEmbrace as a **research factory wrapped around one deterministic trading engine**:
a single-process, event-sourced core (`qe/` package) where backtest, paper, and live are the same
code under three clocks (SimClock/WallClock), all side effects behind two ports (DataFeed, Broker),
every event appended to a replayable journal, and one typed, frozen, content-hashed config per
session stamped into every record.

1. **Kafka/MSK leaves the trading path** (decommissioned at M5 after shadow-validated cutover).
2. **ai_engine leaves the hot path**; models return only via the research factory after
   demonstrated walk-forward uplift. Bedrock advisory layer unchanged.
3. **New portfolio layer**: strategies emit target positions; portfolio nets; risk clamps the
   delta; execution reconciles current→target (retires the ADR-028 netting bug class).
4. **Safety moves from policy to construction**: live broker unconstructible without a
   `LiveGateToken`; session validity = config-hash match; kill switch = one in-process state
   machine + persisted flag.
5. **NS-1 partially overridden**: the live/paper stack IS rebuilt (NS-1 said do-not-rebuild);
   Iceberg/Glue deferred (Parquet + manifests + DuckDB-when-needed until a second writer or
   schema-evolution pain exists). All other NS-1 data-platform substance adopted.
6. **v1 trading stack feature-frozen** as of this ADR — bugfixes only until M5 decommission.

### Governance unchanged

Capital protection > trade count > profit. Backtesting recommends, humans promote. GenAI explains,
GenAI cannot trade. Live remains BLOCKED until the pre-registered gates pass. Forward factor books
continue their monthly cadence throughout the migration (they become the first `qe.engine` user at M2).

### Roadmap (strangler, each milestone a working system)

M0 this ADR · M1 config+journal+data spine (null engine) · M2 SimClock engine at parity with
registered lab runs · M3 full research-factory port · M4 real-time shadow paper sessions (journal
diff vs v1) · M5 cutover + decommission MSK/ai_engine/surplus DynamoDB · M6 live readiness
(UNSCHEDULED — gated on forward factor gate ~Dec-2026 + human review).

---

## ADR-038: v2 Cutover (M4–M5) — Paper Engine Live, v1 Decommission GATED (Not Executed)

**Date:** 2026-07-06 · **Status:** Adopted. Live trading remains BLOCKED.
**Design:** `architecture/re-architecture-2026-07.md`. Builds on ADR-037.
**Runbooks:** `docs/runbooks/qe-operator-runbook.md` (operating surface),
`docs/runbooks/v1-decommission-runbook.md` (gated teardown).

### Context

M4 built the real-time paper engine and proved the three-clocks invariant: paper (WallClock)
reproduces sim (SimClock) NAV to **₹0.00** on the real delivery book and synthetic panels,
because both call one shared `execute_rebalance` step (`qe/engine/core.py`; sim refactored onto
it with M2/M3 parity preserved). Kill-switch v2 is one in-process state machine + one persisted
flag with idempotent activation (retires the v1 four-bug self-refire class). PaperBroker is a
type-isolated SimBroker subclass; the live broker is unconstructible without an M6 gate token.
A live dry-run demonstrated fail-closed for real (24-day-stale lake → staleness trigger → kill →
due rebalance blocked).

### Decisions

1. **Cutover (reversible, done).** `qe` is the primary path for research + positional paper:
   monthly forward-book cadence and paper sessions run via `python -m qe study|paper|kill|report`.
   CLAUDE.md + a one-page operator runbook updated. v1 factor scripts marked SUPERSEDED but
   **retained** as fallback + qe-test parity anchors.

2. **RA-1 "shadow-diff vs v1 stack" reframed.** The v1 paper stack is the Kafka *intraday*
   pipeline trading retired strategies (ADR-033/034); the live experiment (monthly positional
   factor book) was never in it. The meaningful invariance is paper==sim (proven) plus the
   already-proven M2 parity vs `run_delivery_paper_book.py`. No literal v1-Kafka shadow-diff.

3. **TEE/MIS not built.** Intraday is retired; the factor book has no intraday exits to
   simulate. Revisit only if a strategy needing them reaches paper candidacy.

4. **v1 decommission is GATED and NOT executed.** RA-1's M5 tears down MSK/ai_engine/DynamoDB/
   LocalStack/Terraform *after N clean v2 paper sessions*. That gate is **not met** (0 real v2
   paper sessions; lake stale at 2026-06-12). Removing the frozen fallback now would violate the
   strangler invariant and require destructive live-AWS actions. The exact teardown procedure —
   including the non-obvious hazard that `IndianCostModel` + three v1 scripts are qe-test parity
   anchors that must be retained or golden-value-converted before archival — is staged in the
   decommission runbook, blocked on: ≥N clean sessions + fresh lake + human sign-off + git tag.

### Repo hygiene

Removed 12 stale `.pyc` cloud-sync duplicate cache files. User `.docx` documents in the repo
root left untouched (operator's files, not cruft to auto-delete).

### Unchanged

Capital protection > trade count > profit. Backtesting recommends; a human promotes. Live
BLOCKED. Forward factor gate program (~Dec-2026) runs on `qe`, needs no v1 infra.

---

## ADR-039: M6 Live Readiness — Live Blocked BY CONSTRUCTION (evidence gate refuses)

**Date:** 2026-07-06 · **Status:** Adopted. **Live trading remains BLOCKED.**
**Design:** `architecture/re-architecture-2026-07.md` (M6). Builds on ADR-037/038.
**Runbook:** `docs/live-readiness/pre-live-runbook.md` · `governance/live-gate/README.md`.

### Context

Operator "approved M6" = authorization to BUILD the live-readiness machinery, NOT to enable
live trading. Per the platform's own governance (forward factor gate eligible ~Dec-2026, live
BLOCKED until ≥5 valid gate-passing sessions, "a human promotes"), and per the research record
(no strategy has a validated deployable edge), enabling live now would be indefensible. M6
therefore makes "live is blocked" a fact enforced by construction rather than by policy.

### Decisions

1. **`LiveGateToken` + evidence ceremony** (`qe/live_gate.py`). A token — required to construct
   the live broker — is minted ONLY when all 6 pre-registered preconditions pass:
   forward-gate-pass artifact (config-bound), ≥12 complete forward months (re-verified from qe
   study summaries), ≥3 clean qe paper sessions, config-bound operator approval, kill-switch
   clear, lake fresh (≤7d). All fail closed. Thresholds mirror the Forward Factor Gate and must
   never be relaxed. **Today the ceremony REFUSES** (5/6 checks fail).

2. **`LiveBroker` double-gated** (`qe/execution.py`): requires BOTH a token valid for the
   running config AND an explicitly-supplied broker client. A token alone cannot trade; a
   client alone cannot trade. No real broker adapter is wired — connecting one is a separate,
   human-gated step (M6+).

3. **Fail-closed pre-live drills** (`qe/livecheck/drills.py`, `qe drill`): staleness→halt,
   active-kill→no-emission, config-drift→refuse. All PASS now (proof the safety fires).

4. **`qe live` CLI** runs the ceremony and refuses with the check ledger — the hard block is
   demonstrable, not just asserted. Verified: `qe live` → BLOCKED (5/6 fail); `qe drill` → all PASS.

### What was deliberately NOT done

No live trading enabled. No real broker credentials wired. No gate relaxed. No live capital
path exists that a token alone can reach. The architecture is READY; the DECISION stays
evidence-driven and human, gated on the forward factor gate (~Dec-2026) + the pre-live runbook.

### Unchanged

Capital protection > trade count > profit. Backtesting recommends; a human promotes. GenAI
explains; GenAI cannot trade. Live BLOCKED.

---

## ADR-040: qe Paper Due-Logic Fix — Complete-Month Rule + Open Study Horizons (2026-07-08)

**Status:** Implemented · **Trigger:** first real-world qe cadence run (2026-07-08) after the
M5 cutover surfaced three defects the M4/M2 test drives had masked.

### Context

The 2026-07-08 monthly cadence (lake refresh → studies → gate → paper) hit, in order:

1. **Paper `due` bug** — `_is_month_end_row` defined month-end as "last row of its month *in
   the panel*". A live session's `as_of` defaults to the latest lake date, which is *always*
   the last panel row of its month → `due=True` on any fresh-data day. The first real delivery
   paper session bought its full 20-name basket mid-month (2026-07-07 prices). Run daily it
   would have churned daily. M4 parity tests never caught it because they pinned `as_of`
   exactly at historical month-ends.
2. **Study configs pinned at `end_date: 2026-06-30`** — `qe study` truncated the panel at June,
   making June the excluded final month, so the overdue 2026-06-30 rebalance silently did not
   execute. The qe cadence could not advance past June without a config edit every month.
3. **Gate-checker gap** — `check_forward_gate.py` reads the v1 state files
   (`backtest-data/paper_book/{delivery,momentum}_book_state.json`) which only
   `replay_delivery_book_forward.py` writes; `qe study` does not. Under the runbook's qe-only
   cadence the gate would stay frozen forever.

### Decision

1. **Paper follows sim's complete-month rule** (`qe/engine/paper.py::_pending_rebalances`):
   a session executes every completed month-end rebalance the book still owes — at that
   month-end's own row/prices, the identical (date, row) sim's `rebalance_schedule` uses —
   and is otherwise MTM-only. A month is *completed* only when provably over: a later
   calendar month exists in the panel, **or** the wall clock is already past it (covers the
   runbook's `--as-of <month-end>` pin run after the month turns). The panel frontier is never
   assumed to be a month-end. Normal cadence = run any day after the month-end bhavcopy lands;
   missed sessions catch up deterministically, exactly as sim replays them.
2. **Study configs get an open horizon** (`end_date: 2027-12-31`) — sim's complete-month rule
   already refuses to rebalance the final partial month, so an open horizon is safe and stops
   the monthly config-hash churn. One-time hash change: delivery `6165a3bbdfcb…` →
   `3c45c9e17c9f…`, momentum `1de529849cbe…` → `fd77e6f4f6df…`.
3. **Gate-checker port deferred (open task)** — until `check_forward_gate.py` reads qe state,
   the retained v1 replay remains the gate's system-of-record feeder (it ran today: both books
   advanced through the 2026-06-30 rebalance; gate now 6/12 months, both IN PROGRESS).

### Book-state remediation

The mid-month buy-in from the buggy session was reverted: `qe_delivery-book-paper_state.json`
deleted (the session journal is retained as the audit record); both paper books re-seeded via
clean sessions on the fixed engine (status OK, due=False, 0 orders, NAV ₹10,00,000 each).
First real rebalance = July 2026 month-end, executed when August-proving data lands.

### Verification

- 3 new regression tests (mid-month frontier not-due; deferred month-end executes at
  month-end prices == sim NAV; pinned `--as-of` due only once the clock passes the month);
  qe suite 67/67 green including the M2/M4 parity suites.
- Live parity proof: re-run `qe study` (open horizon) now executes the 2026-06-30 rebalance
  and reproduces the v1 replay to the rupee — delivery final MTM 2026-07-07 ₹941,470.38,
  momentum ₹1,042,811.12; cumulative −5.85% / +4.28% vs bench +2.20% on both paths.

### Addendum (same day) — gate checker ported to qe sources

Defect (3) closed: `check_forward_gate.py` now reads the qe study summaries as its primary
source (`--source qe|v1|auto`, default auto). The evaluation function and pre-registered
thresholds are untouched — only the reader changed; a `_state_from_qe_summary` adapter maps
`nav_history` + `final_mtm` + `months[].bench` onto the exact inputs the v1 evaluator consumes
and fails loudly on shape drift. Auto mode cross-checks qe vs v1 whenever both sit at the same
data frontier and reports any numeric mismatch. Verified on real data: both books MATCH
(6/12 months, delivery alpha −9.85%, momentum +5.49% — identical from either source).
Runbook step 2b (v1 replay) downgraded from dependency to optional monthly cross-check.

### Unchanged

Capital protection > trade count > profit. Backtesting recommends; a human promotes.
Paper==sim invariant preserved. Live BLOCKED.

## ADR-041: US Equities Pivot — QuantConnect Alpha Source, QuantEmbrace Execution (2026-07-09)

**Status:** Approved (plan-of-record) · **Implementation:** NOT started — every phase individually
gated · **Plan:** `docs/strategy/us-equities-pivot-plan.md`

### Context

The NSE research program is exhausted (consolidation memo 2026-06-20): every strategy family
died under costed OOS scrutiny at retail-reachable horizons/instruments. The one live experiment
— the NSE forward factor books — continues (6/12 months, gate ~Dec-2026). Operator decision
2026-07-09: shift research focus to **US equities**, sourcing candidate strategies from
**QuantConnect** while QuantEmbrace remains the execution/risk/infra platform. (Prior QC
research from ~2026-06-10 was shelved verbally and left no repo artifact; operator has now
explicitly re-raised it, so the 2026-06-11 "don't re-propose" hold is lifted by the operator.)

### Decision (operator-confirmed 2026-07-09)

1. **Port, don't bridge.** QC/LEAN is a local research bench only. Winning strategy logic is
   ported as `qe/strategy/` implementations (the `Strategy` protocol: PIT panel in, target
   weights out). One deterministic engine (ADR-037 preserved); no LEAN in the live loop; no
   QC-cloud-direct-to-Alpaca.
2. **Positional first.** Daily-to-monthly rebalance strategies only (dual momentum, TAA,
   sector/ETF rotation, factor tilts). No intraday in wave 1 — qe has no bar-level surface and
   intraday is where every NSE edge died.
3. **NSE forward books run unchanged in parallel.** The monthly cadence and the Dec-2026
   Forward Factor Gate are untouched.
4. **$0 budget.** LEAN CLI local, free EOD data (two sources cross-validated, LOW-trust →
   quarantine → curated lake). Universe = ETFs + mega-caps, where survivorship bias in free
   data is structurally minimal. The same curated lake feeds both LEAN and qe (parity feature).
5. **Extend qe, never revive v1 for US.** The frozen v1 stack's complete-but-dormant Alpaca
   path (`alpaca_broker.py`, `alpaca_connector.py`, `ticks.us`) is a porting reference only;
   its frozen-fallback/decommission-gate status is untouched.

### Phases (each writes a report and stops for human approval)

0. ADR + plan-of-record + operator prerequisites (Alpaca account/W-8BEN/LRS, LEAN CLI install).
1. Curated US EOD lake (`market=US, segment=EQ`), ~20 ETFs + 50–100 mega-caps, 2005→,
   two free sources cross-validated (`scripts/backtest/download_us_eod.py`).
2. Candidate screen: 5–8 QC-library positional strategies vs a **pre-registered screen gate**
   (declared before running); LEAN local + project-native pandas cross-check; shortlist 2–3.
3. qe US market support: `USEquityCosts` (SEC fee, FINRA TAF, spread/slippage; commission $0),
   `America/New_York` clock + NYSE calendar, static config-listed US universe, SPY benchmark,
   USD NAV. NSE behavior byte-identical (₹0.00 parity tests stay green).
4. Port shortlist to `qe/strategy/`; qe-vs-LEAN parity within pre-declared tolerance;
   walk-forward OOS; **pre-register a US Forward Gate** (mirror of the NSE gate, vs SPY).
5. US forward paper book(s) via `qe paper`; prove paper==sim to $0.00; fold into the monthly
   operator cadence; accrue calendar-time against the pre-registered gate.
6. (Much later, fully gated) Live path: fix ALPACA-FIX enum bug, Alpaca adapter behind the
   `LiveGateToken` double gate, close the US universe-validation bypass
   (`services/shared/universe/order_validator.py:89-101` currently allows all non-NSE orders
   with a warning — a known live-safety gap), extend the M6 ceremony with US preconditions.

### Invariants (extended to US, not relaxed)

- QC "proven" strategies are **hypotheses, not edges** — QC library algos are educational
  implementations (Alpha Streams itself was discontinued); every candidate must re-earn its
  claim on our curated data with the full US cost stack, OOS. Most should die in validation.
- Backtesting recommends; a human promotes; live BLOCKED by construction (M6 token; US gets
  its own preconditions). Gates are pre-registered before evidence accrues and never relaxed.
- Full US cost stack mandatory (zero commission ≠ zero cost). India-side operator economics
  (LRS, TCS, US dividend withholding) documented, not modeled in-engine.
- Free data = LOW trust → quarantine → cross-source validation → curated lake.

### Unchanged

Capital protection > trade count > profit. NSE cadence + Dec-2026 gate untouched. v1 remains
frozen fallback pending its own decommission gate. Live BLOCKED.

### Addendum (same day) — broker path amended: Robinhood now, Alpaca later

Operator directive 2026-07-09 (second): the near-term brokerage is **Robinhood + its AI
add-ons**; the Alpaca deterministic adapter is deferred. Verified 2026-07 facts: Robinhood
**Agentic Trading** (beta, 2026-05-27) is its only official programmatic equities surface —
third-party AI agents connect via **Robinhood's official MCP server** to a dedicated,
separately-funded agentic account (equities-only beta, invite rollout, in-app notifications +
human approval previews); **Cortex** is the in-app assistant (research aid, not an API);
**no official equities REST API exists** and unofficial wrappers (robin_stocks) are banned at
every phase. Phases 1–5 are broker-independent, so only P0 prerequisites and P6 change:
**P6a** = Robinhood agentic execution (qe recommends → human/supervised-MCP-agent relays into
the capital-capped agentic account, qe kill-switch + symbol whitelist checked before relay —
"recommend-and-approve," not autonomous); **P6b** = Alpaca `LiveBroker` adapter (full
automation, later still, only if 6a proves out and manual relay is the bottleneck).
**Hard P0 prerequisite:** Robinhood requires US residency status (address + citizen/PR/valid
visa; not openable from India) — operator must confirm personal eligibility; if ineligible the
broker path reverts to Alpaca and this addendum is void. Both P6a and P6b sit behind the same
evidence gate (US forward gate PASS + clean paper sessions + human sign-off). Live BLOCKED.

---

## ADR-042: One-Time Rebind of NSE Paper Books After Benign Config-Hash Drift (2026-07-15)

**Status:** Executed (operator-approved) · **Trigger:** 2026-07-15 cadence run — both NSE
`qe paper` sessions failed closed on config-hash drift.

### Context

ADR-041 P4 (2026-07-14) added `assets` and `vol_lookback` to `StrategyConfig`
(`qe/config.py`) with defaults, for the RPLITE port. `RunConfig.canonical_json()` uses
`model_dump(mode="json")`, which serializes defaulted fields — so **every config's hash
moved, including untouched NSE YAMLs** (file mtimes 2026-07-06). The paper engine's
fail-closed drift check (M4 design, ADR-038) then refused to resume both NSE books:
delivery `fc7e5ff58023…` → `7c95f33da911…`, momentum `d2e6e8b3e5c1…` → `d377a933b653…`.

### Evidence the drift was benign (all verified before any override)

1. Rehashing the frozen config recorded in the 2026-07-08 paper journal reproduces the old
   hash exactly (`fc7e5ff58023…`).
2. Today's canonical config minus **exactly** `strategy.assets` + `strategy.vol_lookback`
   reproduces the old hash for all three configs checked (both paper books + delivery study).
3. Engine semantics unchanged: 2026-07-15 `qe study` matched the v1 replays **to the rupee**
   on both books (delivery NAV ₹928,409 / −7.16%; momentum NAV ₹1,062,287 / +6.23%), and
   `check_forward_gate.py` cross-check reported MATCH on both.
4. Both qe paper book states were pre-first-trade anyway (inception 2026-07-07, zero
   holdings, seed cash intact — first owed rebalance is 2026-07-31).

### Decision (operator-approved, Option A)

One-time audited rebind: back up both state files
(`*.bak-pre-rebind-20260715` alongside the originals in `backtest-data/paper_book/`), then
update only the `config_hash` field to the new full hashes, asserting the stored old hash
matched expectations first. No other state fields touched. Both paper sessions then ran
clean (status OK, 0 rebalances due mid-month, 0 risk rejections, 0 kill events,
NAV ₹1,000,000 both books).

### Consequences / open items

- **Session-validity note:** journals before 2026-07-15 carry the pre-P4 hashes; the rebind
  boundary is this ADR. The approved-config lineage is unchanged in substance.
- **Recurrence hazard (P2, open):** every future schema addition with a default will
  re-strand all live books the same way. Durable options: make canonicalization
  schema-evolution-stable (itself re-hashes everything once more), or add an explicit
  journaled `qe book rebind` ceremony. Decide before the next qe schema change lands.
- The US book (ADR-041 P5, not activated) is unaffected — it seeds fresh at activation.
- Tooling sharp edge found same run: `scripts/backtest/download_bhavcopy.py` defaults to
  `--end 2025-12-31`; a bare cadence invocation silently skips 2026 catch-up. Fixed
  operationally today with explicit `--start 2026-07-08 --end 2026-07-14`; consider
  defaulting `--end` to today.

---

## ADR-043: Offline Advisory AI Research Layer (`qe.ai`) — TradingAgents-Inspired, Engine-Isolated (2026-09-25)

**Status:** Approved (operator, 2026-09-25) — Phases 0–5 authorised; 6–10 design-only ·
**Branch:** `feature/hybrid-ai-research` · **Design:** `docs/architecture/hybrid-ai-system.md`

### Context

The operator asked for a hybrid AI + systematic research platform inspired by
TauricResearch/TradingAgents (LLM analyst team → bull/bear debate → trader → risk debate →
portfolio manager). Constraints from the existing canon: RA-1 F-4 cut `ai_engine` from the
hot path (ADR-037); `qe` is the primary engine and v1 is frozen (ADR-038); the governance wall
(backtesting recommends, GenAI explains, humans promote) is non-negotiable; ADR-042 showed
that any new `RunConfig` field moves every config hash and strands the paper books.
Reconnaissance (`docs/architecture/current-state.md`) found: `services/ai_engine` HMM/GBT
are untrained stubs; the lake is prices-only (no news/fundamentals/sentiment); a dormant
Bedrock GenAI layer exists in `services/backtesting/genai/`.

### Decision

1. **New package `qe/ai/`, offline and advisory.** Own entry point `python -m qe.ai`; the
   `qe` CLI and every trading module (`qe.engine`, `qe.execution`, `qe.risk`, `qe.strategy`,
   `qe.killswitch`, `qe.live_gate`, `qe.cli`, `qe.research`) are untouched and never import
   it; `qe.ai` imports only an allowlist (`qe.config`, `qe.journal`, `qe.data.*`,
   `qe.strategy.base`, `qe.universe`, `qe.clock`, `qe.version`). Enforced by AST + fresh-
   interpreter tests.
2. **Structured outputs only.** `ResearchSignal` v1 (`research_signal/1`), per-component
   status; the LLM returns scores + evidence IDs, code attaches evidence and timestamps.
3. **Tools are called by code**, read-only, point-in-time (`Context.at`), no LLM-directed tool
   calling. No-data agents (fundamental/news/sentiment) return UNAVAILABLE with zero LLM calls.
4. **Contamination rule.** An LLM evaluated at a date ≤ its knowledge cutoff (+90d guard) has
   seen the future; such signals are flagged (computed, never claimed) and never carry weight.
   **Historical backtests of AI scores are not evidence**; AI can earn weight only via
   pre-registered forward (post-cutoff) shadow accrual (P6/P10).
5. **Deterministic fusion, default `AI_ADVISORY` with AI weight 0** — the fused decision equals
   the engine's own pick; the AI recommendation is reported next to it, never merged. WEIGHTED
   (cap 0.20) / EXPERIMENTAL (cap 0.50) allowed only in `study` context. Hard risk flags
   always REJECT. AI alone never decides.
6. **Own configs, own hash** (`configs/qe_ai_research.yaml`, `configs/research_fusion.yaml`,
   hashed with `exclude_none`) — `RunConfig` is not modified, so no config-hash drift.
7. **LLM backend:** provider protocol; Bedrock Converse adapter (lazy boto3, IAM, no API key)
   ported from the dormant genai layer's pattern (not imported); deterministic fake LLM for all
   tests; real spend requires `--allow-llm-spend` and is out of scope until P10.
8. **No new dependencies.** No LangGraph/LangChain; no TradingAgents code copied (clean-room).
9. **Writes confined** to `journals/ai/`, `reports/qe-ai/`, `backtest-data/ai_cache/`; never
   `reports/qe/` or `journals/paper-*` (live-gate / forward-gate evidence).

### Consequences

- Phases 0–5 deliver plumbing (safety, governance, traceability) — **not an edge**. With a
  price-only lake the analysts re-describe what the factor model already sees.
- `services/ai_engine` and `services/backtesting/genai` stay as-is (v1 frozen / lab dormant).
- Current-state findings F-1…F-14 are documented only; F-11 (no-gates-passes) and F-12
  (family count not persisted) must be fixed before P6 hypothesis generation.
- CI does not run `tests/qe` (F-13), so boundary tests are enforced locally only until a
  separately-approved CI change.

---

## ADR-034 correction note — regime overlay re-measured point-in-time (F-10, 2026-09-25)

**Status:** Recorded · **Source:** `docs/architecture/current-state.md §10a` F-10, commit `c73fa19`.

The ADR-034 addendum's "200d regime overlay HURTS" evidence used a market proxy ranked on
**total-period** turnover (`regime_series` in `run_delivery_walkforward.py` / `qe/research/wf_v1.py`) —
look-ahead. Re-measured on the real lake with `qe.research.regime.pit_regime_series` (walk-forward
session `delivery-wf-20260925T215825Z-f45157455211`, snapshot `ds-e3d57f81dbab9cb8`, engine and v1
headline numbers reproduced exactly):

- **Delivery book: decision unchanged.** Overlay (PIT) Sharpe 1.20 vs 1.41 without (look-ahead
  version said 1.17); CAGR 16.5% vs 23.7%; MaxDD −14.2% vs −22.2%; 18 cash months. Overlay stays
  NOT adopted for the delivery book.
- **EW benchmark leg: conclusion reverses.** PIT overlay improves the market proxy (Sharpe 0.99 →
  1.16, MaxDD −25.8% → −17.5%); the look-ahead version showed it hurting (0.94). Any general claim
  that a 200d trend filter "hurts" on this data was contaminated. This does not reopen the overlay
  for the delivery book; a market-exposure overlay would need its own pre-registered study.
- The verbatim v1 function is kept unchanged as a parity anchor and is labelled look-ahead in
  every walk-forward report; the PIT rows are the evidence.

---

## ADR-043 addendum — Phase 6 delivered + findings triage (2026-09-25)

**Status:** Implemented (operator-approved "proceed with recommended") · **Branches:**
`fix/findings-triage` (off `dev`, one commit per fix) merged into `feature/hybrid-ai-research`.

- **Findings triage** (`docs/architecture/current-state.md §10a`): fixed F-1 (paper_trade must be a
  JSON boolean at every signal boundary — Phase 0 overstated it: a *missing* flag was already
  refused; the real gap was null/string), F-2 (non-NSE blocked in LIVE), F-10 (point-in-time regime
  overlay beside the leaky v1 one; see ADR-034 correction note), F-11 (ungated study = FAIL),
  F-12 (family count persisted), F-13 (CI `test-qe` job). F-3…F-9/F-14 deferred to v1 decommission,
  accepted as documented paper degrade, tracked, or superseded — each with its reason.
- **iCloud:** 4,764 evicted lake files re-downloaded (`brctl download` per file — the folder form
  does nothing); the real-lake `qe.ai` E2E then ran in 8 s.
- **P6:** `qe.research.lifecycle` (evidence-gated, human-approved, append-only ledger; AI identities
  refused; qe.ai cannot import it) · `qe.ai.hypotheses` (CANDIDATE drafts; code flags settled-family
  re-proposals from `governance/research-eliminated-families.yaml`, untestable data, and the family
  test budget) · `qe.ai.shadow` + `configs/qe_ai_shadow_gate.yaml` (forward gate on incremental IC
  over the factor rank, mirroring the Forward Factor Gate; **committed as DRAFT — no verdict until a
  human signs off**, which must wait for a real model + cutoff, P10).
- **Not done:** P7–P10; the CI job has not run on GitHub (nothing pushed); the shadow gate is unsigned.

---

## ADR-043 addendum 2 — Phases 7–10 (2026-09-26)

**Status:** P7/P8/P9 implemented; P10 built, real run blocked by the AWS account · **Report:** `docs/research/ai-research-p7-p10-report.md`

- **P7** post-trade analyst: code computes every classification; the LLM writes only the lesson; reviews are stamped
  knowable at exit close and reach later prompts only via `lessons_known_at(cutoff)` (look-ahead-safe reflection).
- **P8** dashboard: static, HTML-escaped, CSP `default-src 'none'`, no JavaScript; shows the AI recommendation beside
  the QuantEmbrace decision and computes nothing.
- **P9** external text: qe.ai keeps zero network code — the downloader lives in `scripts/` and stores raw bytes;
  curation is quarantine-first (sanitize → dual-form screen → validate → promote / hold / reject), point-in-time by
  `knowledge_ts`, re-screened on every load; headlines only reach prompts. A test found and closed a hole where tag
  stripping removed injection markup before the screen.
- **P10** backend on the official Anthropic SDK Bedrock Mantle client (Opus 5.5 both tiers, operator's choice),
  optional `requirements-ai.txt`; boto3 banned in qe.ai; `probe`; failures exit 3. **Blocked:** 404 (ap-south-1) /
  403 not-available (us-east-1) for every Claude model on this account; $0 spent. Options recorded, not chosen:
  enable model access, or a first-party API adapter.
- **Unchanged:** engine, v1, required dependencies, all config hashes; shadow gate remains an unsigned DRAFT.
