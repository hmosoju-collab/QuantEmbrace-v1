# QuantEmbrace - Project File Structure

> Canonical reference for the directory layout of the QuantEmbrace algorithmic trading platform.
> Every new folder or service must be registered here before code is merged.

---

## Root Layout

```
QuantEmbrace/
|-- agents/
|-- commands/
|-- hooks/
|-- rules/
|-- memory/
|-- architecture/
|-- governance/
|-- services/
|   |-- data_ingestion/
|   |-- strategy_engine/
|   |-- execution_engine/
|   |-- risk_engine/
|   |-- ai_engine/
|   |-- shared/
|-- infra/
|-- docs/
|-- tests/
```

---

## Directory Descriptions

### `/agents/` -- AI Agent Configurations

Configuration files for AI-powered agents that assist with development, code review, and operational tasks. Includes prompt templates, tool definitions, and agent behavior rules. Each agent config is a standalone YAML or JSON file describing the agent's scope, permitted tools, and constraints.

### `/commands/` -- CLI Command Definitions

Custom CLI commands used during development and operations. Includes commands for running backtests, deploying services, rotating credentials, triggering kill switches, and other operational workflows. Each command is a self-contained module with argument parsing and execution logic.

### `/hooks/` -- Pre/Post Execution Hooks

Hooks that run automatically before or after specific events such as commits, deployments, order submissions, and strategy activations. Critical hooks include:

- **Pre-commit hooks**: Lint, type-check, and validate no mixed concerns across service boundaries.
- **Pre-deploy hooks**: Run risk validation, confirm terraform plan, verify staging tests passed.
- **Pre-order hooks**: Validate order against risk engine rules before submission.
- **Post-deploy hooks**: Smoke-test the deployed service, verify monitoring is active.

### `/rules/` -- System Rules and Constraints

Declarative rule files that define hard constraints the system must respect at all times. Includes risk limits (max position size, max daily loss, max order rate), operational rules (trading hours, circuit breaker thresholds), and code quality rules (linting configs, type checking configs). Rules are loaded at startup and enforced by the risk engine and CI pipeline.

### `/memory/` -- Project Decisions and Patterns

Living documentation of architectural decisions, reusable patterns, anti-patterns, and open tasks. This is the institutional memory of the project. Updated whenever a significant decision is made or a new pattern is adopted. See the memory docs for details.

### `/architecture/` -- System Design Documents

High-level and detailed architecture documents including:

- System context diagrams (C4 Level 1)
- Container diagrams (C4 Level 2)
- Service interaction diagrams
- Data flow diagrams (market data ingestion through to order execution)
- Infrastructure topology diagrams (AWS VPC, ECS clusters, networking)
- Sequence diagrams for critical flows (order lifecycle, kill switch activation)

All architecture docs must be updated **before** corresponding code changes are made.

### `/governance/` -- Project Governance Rules

This directory. Contains the rules that govern how the project is developed, structured, and maintained. Includes file structure definitions, naming conventions, definition of done, and contribution guidelines.

### `/services/` -- Core Trading Services

The heart of the platform. Each subdirectory is a self-contained microservice deployed as an independent ECS Fargate task. Services communicate through well-defined interfaces (SQS queues, SNS topics, or direct SDK calls where latency requires it).

#### `/services/data_ingestion/` -- Market Data Connectors, Processors, Storage

Responsible for all market data acquisition and persistence:

- **Connectors**: WebSocket clients for Zerodha Kite Ticker (NSE India) and Alpaca real-time data (US Equities). Each connector implements the `MarketDataConnector` abstract interface.
- **Processors**: Tick normalization, OHLCV aggregation, order book depth processing. Transforms raw broker-specific formats into a unified internal schema.
- **Storage**: Writers for S3 (historical tick/OHLCV data in Parquet format) and DynamoDB (latest quotes, instrument metadata). Implements batched S3 writes for tick data efficiency.
- **Replay**: Historical data replay for backtesting, reading from S3 and emitting events in the same format as live connectors.

#### `/services/strategy_engine/` -- Trading Strategies, Signals, Backtesting

Responsible for generating trading signals based on market data:

- **Strategies**: Each strategy is a class implementing the `Strategy` abstract interface with `on_tick()`, `on_bar()`, and `on_event()` methods. Strategies receive market data and emit `Signal` objects.
- **Signals**: Data classes representing trade intentions (instrument, direction, strength, metadata). Signals are not orders -- they are passed to the risk engine for validation before execution.
- **Backtesting**: Framework for running strategies against historical data from S3. Produces standardized reports (Sharpe ratio, max drawdown, win rate, profit factor). Every new strategy must have backtest results before deployment.
- **Feature Store**: Computed features (moving averages, RSI, volatility measures) cached and shared across strategies to avoid redundant computation.

#### `/services/execution_engine/` -- Broker Integration, Order Management

Responsible for translating validated signals into actual broker orders:

- **Broker Adapters**: Abstract `BrokerAdapter` interface with concrete implementations for Zerodha Kite Connect and Alpaca Trading API. The adapter pattern allows swapping brokers without modifying strategy or risk logic.
- **Order Manager**: Tracks order lifecycle (created, submitted, partially filled, filled, cancelled, rejected). Implements idempotent order submission using UUID-based deduplication.
- **Position Tracker**: Real-time position tracking reconciled against broker-reported positions.
- **Fill Processor**: Processes execution reports from brokers, updates positions, triggers post-trade analytics.

#### `/services/risk_engine/` -- Risk Validation, Limits, Kill Switch

The critical safety boundary of the platform. Every signal must pass through risk validation before reaching the execution engine:

- **Pre-trade Validation**: Checks position limits, order size limits, sector exposure, daily loss limits, and order rate limits before any order is submitted.
- **Real-time Monitoring**: Continuously monitors portfolio-level risk metrics (VaR, drawdown, exposure).
- **Kill Switch**: Manual and automatic circuit breaker that halts all trading activity. Automatic triggers include: max daily loss breached, abnormal order rate, broker connectivity loss, or data feed staleness.
- **Risk Parameters**: Configurable per-strategy and portfolio-level risk limits stored in DynamoDB.
- **Audit Log**: Every risk decision (approved or rejected) is logged with full context for post-trade review.

#### `/services/ai_engine/` -- ML Features, Models, Inference

Machine learning components for enhancing trading decisions:

- **Feature Engineering**: Computes ML-specific features from market data (statistical features, sentiment scores, cross-asset correlations).
- **Model Registry**: Versioned model storage in S3. Each model has metadata (training date, performance metrics, feature dependencies).
- **Inference Service**: Lightweight inference endpoint that strategies can query for ML predictions. Designed for low-latency synchronous calls.
- **Training Pipeline**: Offline training jobs triggered manually or on schedule. Runs on ECS tasks with larger resource allocations. Results are evaluated against backtest benchmarks before model promotion.

Note: Keep inference lightweight. Complex training happens offline. The inference path must not add more than a few milliseconds to the signal generation pipeline.

#### `/services/shared/` -- Common Config, Logging, Utilities

Shared code used across all services:

- **Config**: Centralized configuration loading from environment variables, SSM Parameter Store, and Secrets Manager. All config keys follow the `QUANTEMBRACE_{SERVICE}_{KEY}` convention.
- **Logging**: Structured JSON logging with correlation IDs that trace a signal from data ingestion through execution. Uses Python's `structlog` library.
- **Models**: Shared data models (Signal, Order, Position, Tick, Bar) used across service boundaries. Defined as Pydantic models for validation.
- **Utilities**: Common helpers for datetime handling (timezone-aware, market hours), retry logic, circuit breaker implementation, and S3/DynamoDB client wrappers.
- **Constants**: Market hours, exchange codes, instrument type enums, and other system-wide constants.

### `/infra/` -- Terraform Configs, Deployment Scripts

All infrastructure-as-code and deployment automation:

- **Terraform Modules**: Modular Terraform configs for VPC, ECS cluster, task definitions, S3 buckets, DynamoDB tables, SQS queues, IAM roles, CloudWatch alarms, and Secrets Manager entries.
- **Environments**: Separate tfvars files for `dev`, `staging`, and `prod` environments.
- **Scripts**: Deployment scripts, database migration helpers, and operational runbooks.
- **CI/CD**: GitHub Actions workflow definitions for build, test, and deploy pipelines.

### `/docs/` -- Additional Documentation

Supplementary documentation not covered by architecture, governance, or memory docs:

- API documentation for inter-service communication
- Broker API integration guides (Zerodha Kite Connect, Alpaca)
- Runbooks for operational procedures (incident response, kill switch activation, credential rotation)
- Onboarding guide for new developers

### `/tests/` -- Unit, Integration, Backtest Tests

All test code organized by type:

- **`/tests/unit/`**: Unit tests for individual functions and classes. Mirrors the `/services/` directory structure. Must achieve minimum 80% coverage for critical paths (risk engine, execution engine).
- **`/tests/integration/`**: Integration tests that verify service interactions. Includes tests for the full signal-to-order pipeline using mock broker adapters.
- **`/tests/backtest/`**: Backtest test suites that verify strategies produce expected results against known historical datasets. Used as regression tests when strategy logic changes.
- **`/tests/fixtures/`**: Shared test data (sample ticks, order responses, instrument lists).

---

## Structural Rules

### Rule 1: No Duplicate Services

Each responsibility has exactly one home. If a capability exists in one service, it must not be reimplemented in another. Examples:

- All risk validation lives in `/services/risk_engine/`. Strategies must not implement their own risk checks.
- All broker communication lives in `/services/execution_engine/`. No other service may call broker APIs directly.
- All market data acquisition lives in `/services/data_ingestion/`. Strategies receive data; they do not fetch it.

### Rule 2: No Logic Mixing Across Service Boundaries

Each service has a single, well-defined responsibility. Code that crosses concerns must be refactored:

- Strategy code must not contain order submission logic.
- Execution code must not contain signal generation logic.
- Risk code must not contain data ingestion logic.
- AI/ML code must not contain direct broker calls.

If a function touches two services' domains, it belongs in neither -- it belongs in an orchestration layer or must be split.

### Rule 3: Shared Code Goes in `/services/shared/`

Any code used by two or more services must live in `/services/shared/`. This includes data models, configuration loaders, logging setup, and utility functions. Services must not import from each other directly.

### Rule 4: Architecture Docs Before Code

Any change that modifies service boundaries, adds new infrastructure, or changes data flow must update the relevant architecture document **before** the code change is made. PRs that modify architecture without updating docs will be rejected.

### Rule 5: New Files Must Be Registered

Any new top-level directory or service must be added to this document before the PR is merged. This file is the single source of truth for project structure.
