# QuantEmbrace - Architectural Decisions

> Living record of key architectural decisions, their context, and rationale.
> New decisions are appended at the bottom. Existing decisions are never deleted,
> only superseded with a reference to the new decision.

---

## ADR-001: ECS Fargate Over Lambda for Compute

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
