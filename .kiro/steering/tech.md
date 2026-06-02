# QuantEmbrace — Tech Stack & Build System

## Language & Runtime

- **Python 3.11+** — all services
- **asyncio** — all services are async; long-running loops use `asyncio.gather()`
- **Pydantic v2** — all data models crossing service boundaries; `pydantic-settings` for config
- **Type hints mandatory** on all function signatures and return types
- **mypy `--strict`** mode required; zero type errors before merge

## Key Libraries

| Category | Library | Notes |
|---|---|---|
| Broker — NSE | `kiteconnect>=5.0.0` | Zerodha Kite Connect |
| Broker — US | `alpaca-trade-api>=3.0.0` | Alpaca Markets |
| Messaging | `confluent_kafka` | MSK Serverless, SASL/OAUTHBEARER+IAM on port 9098 |
| MSK Auth | `aws-msk-iam-sasl-signer-python` | Short-lived IAM tokens |
| AWS SDK | `boto3>=1.34.0`, `aiobotocore>=2.12.0` | S3, DynamoDB, Secrets Manager |
| Web (AI engine) | `fastapi>=0.110.0` + `uvicorn` | HTTP inference endpoint only |
| Data | `pandas>=2.2.0`, `numpy>=1.26.0` | Tick processing, feature engineering |
| ML | `scikit-learn>=1.4.0`, `joblib>=1.3.0` | Models loaded from S3 at startup |
| Logging | `structlog>=24.1.0` | Structured JSON, correlation IDs |
| Async HTTP | `aiohttp>=3.9.0` | Async HTTP client |
| WebSocket | `websockets>=12.0` | Market data streaming |
| Testing | `pytest>=8.1.0`, `pytest-asyncio>=0.23.0`, `hypothesis>=6.98.0` | PBT via hypothesis |
| AWS mocking | `moto>=5.0.0` | S3/DynamoDB in tests |
| HTTP mocking | `responses>=0.25.0` | Broker API mocking |
| Linter | `ruff>=0.3.0` | Enforces banned APIs (SQS) via TID251 |
| Formatter | `black>=24.3.0` | Line length 100 |
| Type checker | `mypy>=1.9.0` | `--strict` mode required |
| Monitoring | CloudWatch + Prometheus sidecar + Grafana | |

All dependencies are in `services/requirements.txt`.

## Infrastructure

- **Compute:** AWS EC2 ARM64 (Graviton3) Auto Scaling Groups — `t4g.medium` for data ingestion, `c6g.large` for strategy/risk, `c6g.xlarge` for execution
- **Messaging:** AWS MSK Serverless (Kafka) — sole inter-service transport; SASL/OAUTHBEARER+IAM on port 9098
- **Storage:** DynamoDB (hot state), S3 Parquet (historical/bulk), Secrets Manager (credentials)
- **IaC:** Terraform — all infra in `infra/terraform/`; topics created via `scripts/kafka/create_topics.py` post-apply
- **CI/CD:** GitHub Actions (`.github/workflows/`)
- **Region:** `ap-south-1` (primary)
- **Environments:** `dev`, `staging`, `prod` (separate tfvars in `infra/terraform/environments/`)

## Kafka Topics

| Topic | Partitions | Key | Retention |
|---|---|---|---|
| `ticks.nse` | 2 | symbol | 1h dev / 4h prod |
| `ticks.us` | 2 | symbol | 1h dev / 4h prod |
| `signals.pending` | 2 | signal_id | 10m dev / 30m prod |
| `signals.approved` | 2 | instrument_id | 5m dev / 15m prod |
| `orders.events` | 2 | order_id | 24h |
| `kill.switch` | 1 | — | 24h |
| `ops.audit` | 1 | service | 7d |

All events use **schema_version: "3.0"** (frozen). Envelope fields: `event_id`, `trace_id`, `event_type`, `schema_version`, `source`, `published_time`.

## Common Commands

### Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r services/requirements.txt
```

### Code Quality (all must pass before merging)

```bash
# Format
black services/ tests/ --line-length 100

# Lint (zero errors required; SQS usage is CI-failing)
ruff check services/ tests/

# Type check (strict mode)
mypy services/ --strict
```

### Testing

```bash
# All unit tests
pytest tests/unit/ -v

# Specific service
pytest tests/unit/risk_engine/ -v

# With coverage (≥85% required for trading logic)
pytest tests/unit/ --cov=services --cov-fail-under=85

# Integration tests (requires LocalStack running)
pytest tests/integration/ -v

# Property-based tests only
pytest tests/unit/ -v -k "hypothesis"
```

### Running Services Locally

```bash
# Individual service
python -m services.data_ingestion.main
python -m services.strategy_engine.main
python -m services.risk_engine.main
python -m services.execution_engine.main

# Full stack
docker compose up
docker compose logs -f risk_engine
```

### Backtesting

```bash
python scripts/backtest/run_backtest.py \
    --strategy momentum \
    --symbols RELIANCE,TCS \
    --market NSE \
    --from 2025-01-01 \
    --to 2025-12-31
```

### Strategy Config (operator CLI)

```bash
python scripts/strategy/config.py list
python scripts/strategy/config.py get momentum_v1
python scripts/strategy/config.py paper momentum_v1      # set paper_trade=True
python scripts/strategy/config.py go-live momentum_v1   # promote to live
python scripts/strategy/config.py disable momentum_v1
```

### Zerodha Auth (daily token refresh — run before market open)

```bash
python scripts/zerodha_login.py
```

### Kafka Topics (run after terraform apply)

```bash
python scripts/kafka/create_topics.py
```

### Terraform

```bash
cd infra/terraform
terraform init
terraform plan
terraform apply
```

### Operational Scripts

```bash
# Kill switch (manual halt)
python scripts/kill_switch_cli.py

# Circuit breaker reset
python scripts/strategy/reset_circuit_breaker.py

# Verify candle cache
python scripts/strategy/verify_candle_cache.py

# ECS health check
python scripts/deploy/check_ecs_health.py

# Zerodha utilities
python scripts/zerodha/position_audit.py
python scripts/zerodha/rate_monitor.py
```

## Banned Patterns (CI-enforced via ruff TID251)

These will **fail the build**:

| Pattern | Reason |
|---|---|
| `boto3.client("sqs")` | Use Kafka publishers instead |
| `get_sqs_client()` | Removed; use `KafkaTickPublisher`, `KafkaSignalPublisher`, etc. |
| `botocore.client.SQS` | SQS permanently removed from trading path |
| Direct strategy→execution import | Violates risk gate requirement |
| `PHASE2_KAFKA_ENABLED` | Migration flag — fully deleted |
| `print()` statements | Use `structlog` for all output |
| Hardcoded credentials | All secrets via AWS Secrets Manager + IAM instance roles |

## Code Style Rules

- Docstrings required on all public functions and classes (Google style)
- No cross-service imports — only `services/shared/` is importable across boundaries
- `asyncio` for all I/O-bound operations; sync acceptable for CPU-bound strategy computations
- DynamoDB conditional writes for all state transitions (idempotency)
- Correlation IDs (`trace_id`) must propagate from tick through to order event

## Mandatory Environment Variables

Every trading service raises `RuntimeError` on startup if `KAFKA_BOOTSTRAP_SERVERS` is not set.

| Variable | Required By |
|---|---|
| `KAFKA_BOOTSTRAP_SERVERS` | All 4 trading services |
| `AWS_REGION` | All services |
| `ZERODHA_API_KEY` / `ZERODHA_API_SECRET` | execution_engine |
| `ALPACA_API_KEY` / `ALPACA_API_SECRET` | execution_engine |
| `DYNAMODB_TABLE_PREFIX` | All services |
| `S3_BUCKET_DATA` / `S3_BUCKET_LOGS` | All services |
| `QE_ENVIRONMENT` | All services |
