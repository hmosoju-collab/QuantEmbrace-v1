# QuantEmbrace - Hedge-Level Algorithmic Trading System

## Session Start Protocol

Every session must begin with this initialization sequence before any work is done:

```
1. Load CLAUDE.md             → Understand project rules, architecture, conventions
2. Load architecture/*        → Review system_design.md, data_flow.md, infra_diagram.md,
                                 api_contracts.md, trading_flow.md for current system state
3. Load memory/open_tasks.md  → Check pending work, blockers, and priorities
4. Summarize current state    → Output a brief status covering:
                                 • Which services are implemented vs. stubbed
                                 • Any open blockers or risks
                                 • Next priority task from open_tasks.md
5. Proceed                    → Begin work only after steps 1–4 are complete
6. If user asks question     → Explain, Suggest and Implement only after input/Approval from User
```

This protocol ensures continuity across sessions and prevents duplicate work,
missed context, or architectural drift. Never skip this sequence.

---

## Project Overview

QuantEmbrace is a production-grade algorithmic trading platform designed for multi-market execution across Indian (NSE) and US equity markets. The system operates as a set of loosely coupled microservices, each responsible for a single domain concern, deployed on AWS ECS Fargate.

The platform integrates with Zerodha Kite Connect for NSE/BSE Indian markets and Alpaca for US equities, providing unified order management, risk controls, and strategy orchestration across both brokers.

---

## Tech Stack

| Layer          | Technology                                           |
|----------------|------------------------------------------------------|
| Language       | Python 3.11+                                         |
| Broker (India) | Zerodha Kite Connect API                             |
| Broker (US)    | Alpaca Trading API                                   |
| Compute        | AWS ECS Fargate                                      |
| Storage        | AWS S3 (logs, historical data, backtest artifacts)   |
| State          | AWS DynamoDB (order state, positions, session tokens) |
| IaC            | Terraform                                            |
| Messaging      | AWS SQS / SNS                                        |
| Monitoring     | CloudWatch, Prometheus (sidecar), Grafana            |
| CI/CD          | GitHub Actions                                       |
| Testing        | pytest, hypothesis (property-based), locust (load)   |
| Containerization | Docker                                             |

---

## Architecture Overview

The system is organized into six architectural layers. Each layer maps to one or more microservices. Cross-layer communication happens exclusively through well-defined interfaces (SQS queues, HTTP APIs, or shared DynamoDB tables with strict ownership).

### Layer 1: Data Ingestion

Responsible for all market data acquisition, normalization, and storage.

- **Service:** `data_ingestion`
- Connects to Zerodha WebSocket (Kite Ticker) for real-time NSE tick data.
- Connects to Alpaca WebSocket for US equity streaming data.
- Normalizes tick data into a unified internal format.
- Persists raw ticks to S3 (partitioned by date/symbol) for replay and backtesting.
- Publishes normalized ticks to downstream consumers via SQS.

### Layer 2: Strategy Engine

Houses all trading strategies and signal generation logic.

- **Service:** `strategy_engine`
- Consumes normalized market data from the data layer.
- Runs registered strategy modules (momentum, mean-reversion, stat-arb, etc.).
- Emits signal objects (direction, conviction, target price, stop-loss).
- Strategies are stateless functions operating on windowed data. All persistent state lives in DynamoDB.
- **Never** contains execution or risk logic.

### Layer 3: Execution Engine

Translates validated signals into broker orders and manages order lifecycle.

- **Service:** `execution_engine`
- Receives risk-approved signals only (never directly from strategy).
- Manages order placement, modification, and cancellation via broker APIs.
- Implements smart order routing: Zerodha for NSE instruments, Alpaca for US equities.
- Tracks order state transitions in DynamoDB (PENDING -> PLACED -> FILLED / REJECTED / CANCELLED).
- Handles partial fills, slippage tracking, and retry logic.
- Implements circuit breakers for broker API failures.

### Layer 4: Risk Engine

The gatekeeper between strategy signals and order execution.

- **Service:** `risk_engine`
- Sits between strategy_engine and execution_engine. Every signal must pass through risk validation before reaching execution.
- Enforces position sizing limits, daily loss limits, and exposure caps.
- Validates margin requirements against available capital.
- Maintains real-time P&L calculations.
- Can halt trading system-wide via a kill switch.
- Logs every risk decision (approve/reject) with full reasoning to S3 for audit.

### Layer 5: AI/ML Engine

Provides model inference and adaptive parameter tuning.

- **Service:** `ai_engine`
- Hosts trained ML models for signal enhancement, regime detection, and volatility forecasting.
- Serves predictions via internal HTTP API.
- Model training happens offline (SageMaker or local). Only inference runs in production.
- Provides feature store integration for consistent feature computation.

### Layer 6: Infrastructure

All Terraform-managed AWS resources, networking, IAM, and deployment configuration.

- ECS Fargate cluster with service discovery.
- VPC with private subnets for compute, public subnets only for ALB.
- S3 buckets with lifecycle policies for cost optimization.
- DynamoDB tables with on-demand capacity for unpredictable workloads, provisioned capacity for predictable ones.
- CloudWatch alarms for latency, error rates, and cost anomalies.

---

## Critical Trading Rules

These rules are non-negotiable. Every contributor must understand and follow them.

### 1. Separation of Concerns

- **Strategy logic** computes signals. It must never place orders or check risk limits.
- **Risk logic** validates signals. It must never modify signals or place orders.
- **Execution logic** places orders. It must never generate signals or override risk decisions.
- If you find yourself importing from another layer's internals, you are violating this rule.

### 2. Risk Engine is Mandatory

- The risk engine sits between strategy and execution. There is no bypass path.
- Every signal flows: `strategy_engine -> risk_engine -> execution_engine`.
- Direct strategy-to-execution communication is a critical defect.

### 3. All Trades Must Pass Risk Validation

- No order reaches a broker without an explicit risk approval record.
- Risk approvals are logged with a unique `risk_decision_id` linked to the order.
- If the risk engine is down, trading halts. This is by design.

### 4. Restart Safety and Idempotency

- Every service must be safe to restart at any time without data loss or duplicate orders.
- Use DynamoDB conditional writes for state transitions to prevent race conditions.
- Order placement must be idempotent: the same signal processed twice must not produce duplicate orders.
- On startup, each service must reconcile its state with the broker and DynamoDB before resuming.

### 5. No Silent Failures

- Every exception in the trading path must be logged, alerted on, and handled.
- Swallowing exceptions in execution or risk code is a critical defect.
- Use structured logging with correlation IDs across all services.

---

## AWS Cost Optimization Rules

### Compute

- **Do not use Lambda for streaming workloads.** Lambda's cold start latency and 15-minute timeout make it unsuitable for persistent WebSocket connections and continuous data processing.
- **Use ECS Fargate** for all long-running services. Right-size task definitions based on actual resource usage.
- Use Fargate Spot for non-critical workloads (backtesting, batch analytics) to reduce costs by up to 70%.

### Storage

- **S3 for all logs and historical data.** Never store time-series history in DynamoDB.
- Use S3 Intelligent-Tiering for backtest data that has unpredictable access patterns.
- Set lifecycle policies: move data older than 90 days to S3 Glacier for compliance retention.
- Use S3 Select or Athena for querying historical data instead of loading full datasets.

### Database

- **DynamoDB for low-latency state only:** order state, positions, session tokens, risk counters.
- Use on-demand capacity mode during development and for unpredictable workloads.
- Switch to provisioned capacity with auto-scaling for production workloads with known patterns.
- Set TTL on ephemeral records (session tokens, temporary locks) to avoid unbounded table growth.

### Networking

- Use VPC endpoints for S3 and DynamoDB to avoid NAT Gateway data transfer charges.
- Keep inter-service communication within the VPC using service discovery (Cloud Map).

### Monitoring

- Use CloudWatch Logs with retention policies (30 days hot, archive to S3).
- Avoid high-cardinality custom metrics in CloudWatch; use Prometheus with a Fargate sidecar for detailed metrics.

---

## Development Conventions

### Python Standards

- Python 3.11+ required. Use the latest stable release.
- **Type hints are mandatory** on all function signatures and return types.
- Use `pydantic` for all data models (signals, orders, positions, API payloads).
- Use `async/await` for I/O-bound operations (broker API calls, WebSocket handlers, database queries).
- Synchronous code is acceptable for CPU-bound strategy computations.

### Code Style

- Formatter: `black` (line length 100).
- Linter: `ruff` with a strict rule set.
- Import sorting: `isort` (compatible with black).
- All public functions and classes require docstrings (Google style).

### Testing

- Framework: `pytest` with `pytest-asyncio` for async tests.
- Minimum coverage target: 85% for core trading logic (risk, execution, order management).
- Use `hypothesis` for property-based testing of risk calculations and order state machines.
- Use `pytest-mock` and `responses` for mocking broker API calls. Never hit real broker APIs in tests.
- Integration tests run against LocalStack (S3, DynamoDB, SQS).

### Project Structure

```
quantembrace/
  services/
    data_ingestion/
    strategy_engine/
    execution_engine/
    risk_engine/
    ai_engine/
  common/
    models/          # Shared pydantic models
    broker/          # Broker client abstractions
    risk/            # Risk calculation utilities
    storage/         # S3 and DynamoDB client wrappers
    messaging/       # SQS/SNS publisher and consumer helpers
  tests/
    unit/
    integration/
    backtest/
  infra/
    terraform/
      modules/
      environments/
        dev/
        staging/
        prod/
  scripts/
    backtest/
    data_download/
    deploy/
  configs/
```

### Git Workflow

- Branch naming: `feature/<ticket>-<short-desc>`, `fix/<ticket>-<short-desc>`, `infra/<short-desc>`.
- Squash merge to `main`. Keep commit history clean.
- All PRs require at least one review. Trading logic PRs require two reviews.

---

## Broker Integrations

### Zerodha Kite Connect (NSE India)

- **Markets:** NSE (equities, F&O), BSE.
- **API:** REST for orders/positions/holdings, WebSocket (Kite Ticker) for streaming quotes.
- **Auth:** OAuth2-based login token. Tokens expire daily at ~07:30 IST. The system must handle automatic re-authentication.
- **Rate limits:** 10 requests/second for order APIs, 3 requests/second for historical data. Implement client-side rate limiting.
- **Order types supported:** MARKET, LIMIT, SL (stop-loss), SL-M (stop-loss market).
- **Key considerations:**
  - Kite API uses `variety` (regular, amo, iceberg, auction) for order routing.
  - Position tracking uses `product` type (CNC for delivery, MIS for intraday, NRML for F&O).
  - Auto square-off happens at ~15:15 IST for MIS positions. The system must handle this proactively.

### Alpaca (US Equities)

- **Markets:** US equities (NYSE, NASDAQ), OTC.
- **API:** REST for orders/account, WebSocket for streaming quotes and trade updates.
- **Auth:** API key + secret. Keys do not expire but can be regenerated.
- **Rate limits:** 200 requests/minute. Implement client-side rate limiting.
- **Order types supported:** MARKET, LIMIT, STOP, STOP_LIMIT, TRAILING_STOP.
- **Key considerations:**
  - Supports paper trading with separate API endpoint (use for staging).
  - Fractional shares supported. Useful for position sizing.
  - Extended hours trading available via `extended_hours` flag on orders.
  - PDT (Pattern Day Trader) rules apply to accounts under $25k.

### Unified Broker Abstraction

All broker-specific logic is encapsulated behind a `BrokerClient` protocol:

```python
class BrokerClient(Protocol):
    async def place_order(self, order: OrderRequest) -> OrderResponse: ...
    async def cancel_order(self, order_id: str) -> CancelResponse: ...
    async def get_positions(self) -> list[Position]: ...
    async def get_order_status(self, order_id: str) -> OrderStatus: ...
    async def subscribe_quotes(self, symbols: list[str], callback: QuoteCallback) -> None: ...
```

Strategy and risk code must always use this abstraction. Direct broker client usage outside the execution layer is prohibited.

---

## Service Boundaries Summary

| Service           | Owns                              | Reads From                  | Writes To                       |
|-------------------|-----------------------------------|-----------------------------|----------------------------------|
| data_ingestion    | Raw/normalized market data        | Broker WebSocket feeds      | S3 (raw ticks), SQS (normalized)|
| strategy_engine   | Signals                           | SQS (market data), DynamoDB | SQS (signals to risk)           |
| risk_engine       | Risk decisions, position limits   | SQS (signals), DynamoDB     | SQS (approved signals), S3 (audit log), DynamoDB (risk state) |
| execution_engine  | Orders, order state               | SQS (approved signals)      | Broker APIs, DynamoDB (order state), S3 (execution log) |
| ai_engine         | Model predictions                 | S3 (features, model artifacts) | HTTP responses, S3 (prediction logs) |

---

## How to Run Locally

### Prerequisites

- Python 3.11+
- Docker and Docker Compose
- AWS CLI v2 (configured with a dev profile)
- Terraform 1.5+
- Zerodha Kite Connect API credentials (request via https://kite.trade)
- Alpaca API credentials (sign up at https://alpaca.markets)

### Setup

```bash
# Clone the repository
git clone <repo-url> && cd quantembrace

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Copy environment template and fill in credentials
cp .env.example .env
# Edit .env with your broker API keys and AWS credentials

# Start local infrastructure (DynamoDB Local, LocalStack for S3/SQS)
docker-compose up -d localstack dynamodb-local

# Run database migrations / table creation
python scripts/setup_local_tables.py

# Verify setup
pytest tests/unit/ -v
```

### Running Services Locally

```bash
# Run individual services
python -m services.data_ingestion.main
python -m services.strategy_engine.main
python -m services.risk_engine.main
python -m services.execution_engine.main
python -m services.ai_engine.main

# Or run all services via docker-compose
docker-compose up
```

### Running Backtests

```bash
# Download historical data
python scripts/data_download/fetch_historical.py --symbol RELIANCE --from 2024-01-01 --to 2025-12-31

# Run a backtest
python scripts/backtest/run.py --strategy momentum --config configs/backtest_momentum.yaml
```

### Environment Variables

| Variable                  | Description                        | Required |
|---------------------------|------------------------------------|----------|
| `KITE_API_KEY`            | Zerodha Kite Connect API key       | Yes      |
| `KITE_API_SECRET`         | Zerodha Kite Connect API secret    | Yes      |
| `KITE_ACCESS_TOKEN`       | Zerodha session access token       | Runtime  |
| `ALPACA_API_KEY`          | Alpaca API key ID                  | Yes      |
| `ALPACA_API_SECRET`       | Alpaca API secret key              | Yes      |
| `ALPACA_BASE_URL`         | Alpaca API base URL                | Yes      |
| `AWS_REGION`              | Primary AWS region                 | Yes      |
| `AWS_PROFILE`             | AWS CLI profile name               | Dev only |
| `DYNAMODB_TABLE_PREFIX`   | Prefix for DynamoDB table names    | Yes      |
| `S3_BUCKET_DATA`          | S3 bucket for market data          | Yes      |
| `S3_BUCKET_LOGS`          | S3 bucket for audit/execution logs | Yes      |
| `LOG_LEVEL`               | Logging level (DEBUG/INFO/WARNING) | No       |
| `ENVIRONMENT`             | Runtime environment (dev/staging/prod) | Yes  |
