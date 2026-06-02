# QuantEmbrace — Project Structure

## Root Layout

```
QuantEmbrace/
├── services/           # Core trading microservices (primary codebase)
├── tests/              # All test code
├── infra/              # Terraform IaC and deployment scripts
├── scripts/            # Operational and developer CLI scripts
├── configs/            # Instrument universe and strategy config files
├── architecture/       # Architecture decision records and design docs
├── docs/               # Supplementary documentation
├── governance/         # Naming conventions, file structure rules, DoD
├── memory/             # Patterns, anti-patterns, decisions, open tasks
├── agents/             # AI agent YAML configs for dev assistance
├── commands/           # Custom CLI command definitions
├── hooks/              # Pre/post execution hook definitions
├── rules/              # Declarative system constraint files
├── pyproject.toml      # Ruff, black, mypy, pytest configuration
├── ARCHITECTURE.md     # Canonical architecture — source of truth
├── CLAUDE.md           # Session start protocol and project rules
└── services/requirements.txt  # All Python dependencies
```

## Services (`/services/`)

Each subdirectory is an independent microservice deployed as its own EC2 ARM64 ASG process. Services communicate **only** through Kafka topics — never by importing from each other. Only `shared` may be imported across services.

```
services/
├── data_ingestion/     # WebSocket feeds (Zerodha + Alpaca), tick publishing, candle streaming
│   ├── connectors/     # Zerodha Kite Ticker + Alpaca WebSocket clients
│   ├── processors/     # Tick normalization, OHLCV aggregation
│   ├── publishers/     # KafkaTickPublisher
│   ├── storage/        # S3 Parquet writer, DynamoDB latest quotes
│   ├── features/       # Feature computation for data layer
│   └── candle_stream.py
├── strategy_engine/    # Signal generation, strategy runners, circuit breakers
│   ├── strategies/     # Strategy implementations (momentum, candle-based, etc.)
│   ├── runners/        # Strategy runner orchestration
│   ├── signals/        # Signal construction and validation
│   ├── consumers/      # KafkaTickConsumer (group: strategy-v1)
│   ├── publishers/     # KafkaSignalPublisher
│   ├── config/         # DynamoDB strategy config loader
│   ├── universe/       # Instrument universe management
│   └── backtesting/    # Backtest framework
├── risk_engine/        # Signal validation, position limits, kill switch, P&L tracking
│   ├── validators/     # Position limits, spread gate, exposure checks
│   ├── limits/         # Risk limit definitions and enforcement
│   ├── killswitch/     # Kill switch logic and consumers
│   ├── consumers/      # KafkaSignalConsumer (risk-v1), KafkaOrderConsumer (risk-orders-v1)
│   ├── publishers/     # KafkaApprovedPublisher
│   ├── analytics/      # Real-time P&L, VaR, drawdown
│   ├── context/        # Risk context builder
│   ├── cache/          # In-memory risk state cache
│   ├── registry/       # Risk decision registry
│   └── api/            # Internal risk API
├── execution_engine/   # Broker adapters (Zerodha + Alpaca), order management, fill reporting
│   ├── brokers/        # ZerodhaBroker + AlpacaBroker adapters
│   ├── orders/         # Order lifecycle management, idempotency
│   ├── consumers/      # KafkaApprovedConsumer (group: execution-v1)
│   ├── publishers/     # KafkaOrderEventsPublisher
│   ├── auth/           # Broker auth handling
│   ├── polling/        # Order status polling
│   ├── retry/          # Retry logic and circuit breakers
│   └── mis_square_off.py  # MIS auto square-off at 15:15 IST
├── ai_engine/          # ML feature pipelines, model registry, inference (FastAPI)
│   ├── features/       # ML feature engineering
│   ├── models/         # Model registry (S3-backed)
│   └── inference/      # FastAPI inference endpoint
└── shared/             # Common models, config, logging, utilities — imported by all services
    ├── models/         # Signal, Order, Position, Tick, RiskContext (Pydantic)
    ├── config/         # settings.py — pydantic-settings config loader
    ├── aws/            # clients.py — S3, DynamoDB, Secrets Manager (NO SQS)
    ├── logging/        # structlog setup with correlation IDs
    ├── metrics/        # CloudWatch metrics publisher
    ├── features/       # feature_reader.py — shared feature store access
    ├── health/         # health_server.py — HTTP health check endpoint
    ├── utils/          # helpers.py — datetime, retry, circuit breaker
    └── zerodha/        # market_phase.py, rate_limiter.py
```

### Service Ownership Rules

| Responsibility | Owner | No Other Service May |
|---|---|---|
| Broker API calls | `execution_engine` | Call broker APIs directly |
| Risk validation | `risk_engine` | Implement their own risk checks |
| Market data acquisition | `data_ingestion` | Fetch data from brokers |
| Signal generation | `strategy_engine` | Generate signals |
| Shared models/utils | `shared` | Duplicate shared code |

## Tests (`/tests/`)

Mirror the `services/` directory structure under `unit/`.

```
tests/
├── unit/               # Unit tests — no network calls; mirrors services/ structure
│   ├── test_alpaca_broker.py
│   ├── test_data_ingestion_feature_wiring.py
│   ├── test_dynamo_candle_consumer.py
│   ├── test_execution_integration.py
│   ├── test_feature_engine.py
│   ├── test_killswitch.py
│   ├── test_momentum_backtester.py
│   ├── test_risk_analytics_engine.py
│   ├── test_risk_context_builder.py
│   ├── test_spread_gate_validator.py
│   ├── test_strategy_runner.py
│   └── ...
├── integration/        # Full pipeline tests with mock broker adapters (LocalStack)
├── backtest/           # Strategy regression tests against known historical data
└── fixtures/           # Shared test data (sample ticks, order responses)
```

Test file naming: `test_{module_name}.py` mirroring the source path.
Example: `services/risk_engine/validators/position_limits.py` → `tests/unit/risk_engine/validators/test_position_limits.py`

## Infrastructure (`/infra/`)

```
infra/
├── terraform/
│   ├── modules/
│   │   ├── ec2_services/   # ARM64 ASG per service
│   │   ├── kafka/          # MSK Serverless cluster
│   │   ├── dynamodb/       # DynamoDB tables
│   │   ├── s3/             # S3 buckets with lifecycle policies
│   │   ├── vpc/            # VPC, subnets, VPC endpoints
│   │   └── monitoring/     # CloudWatch alarms
│   └── environments/
│       ├── dev/
│       ├── staging/
│       └── prod/
└── deployment/
    ├── Dockerfile
    └── deploy.sh
```

Kafka topics are **not** created by Terraform — run `scripts/kafka/create_topics.py` after `terraform apply`.

## Scripts (`/scripts/`)

```
scripts/
├── backtest/
│   └── run_backtest.py         # Backtest runner
├── strategy/
│   ├── config.py               # Strategy config operator CLI (list/get/set/enable/disable/go-live/paper)
│   ├── reset_circuit_breaker.py
│   └── verify_candle_cache.py
├── kafka/
│   ├── create_topics.py        # Topic creation (run after terraform apply)
│   └── validate_phase2.py
├── deploy/
│   └── check_ecs_health.py
├── zerodha/
│   ├── budget_optimizer.py
│   ├── candle_prefetch.py
│   ├── position_audit.py
│   ├── rate_monitor.py
│   └── stress_test.py
├── zerodha_login.py            # Daily access token refresh (run before market open)
└── kill_switch_cli.py          # Manual kill switch activation
```

## Key Config Files

| File | Purpose |
|---|---|
| `configs/instruments.yaml` | Instrument universe — set `active: true/false` to control what's watched |
| `pyproject.toml` | Ruff lint rules (including SQS ban), black config, mypy, pytest settings |
| `ARCHITECTURE.md` | Canonical signal flow, Kafka topics, forbidden patterns — source of truth |
| `CLAUDE.md` | Session start protocol, critical trading rules, broker integration details |
| `governance/file_structure.md` | Canonical directory layout — register new top-level dirs here before merging |

## Naming Conventions

- **Python files/modules:** `snake_case`
- **Classes:** `PascalCase`; abstract bases prefixed with `Base` or suffixed `ABC`
- **Functions/methods:** `snake_case`; private methods prefixed with `_`
- **Constants:** `UPPER_SNAKE_CASE` at module level
- **Enums:** `PascalCase` class, `UPPER_SNAKE_CASE` members
- **Pydantic models:** `PascalCase` class, `snake_case` fields, all fields typed
- **Test functions:** `test_{scenario_description}` — e.g. `test_validate_order_rejects_when_daily_loss_limit_breached`
- **AWS resources:** `quantembrace-{env}-{purpose}` (hyphens, lowercase)
- **Env vars:** `QUANTEMBRACE_{SERVICE}_{KEY}` (uppercase, underscores)
- **Git branches:** `feature/<ticket>-<short-desc>`, `fix/<ticket>-<short-desc>`, `infra/<short-desc>`
- **Commits:** Conventional Commits — `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`, `style`

## Structural Rules

1. **No cross-service imports.** Services never import from each other. Only `services/shared/` is importable across service boundaries.
2. **No duplicate responsibilities.** Each capability has exactly one home (see Service Ownership table above).
3. **Architecture docs before code.** Any change to service boundaries, data flow, or infrastructure requires updating `/architecture/` docs before the code change.
4. **New top-level directories** must be registered in `governance/file_structure.md` before merging.
5. **No `print()` statements.** All output goes through `structlog`.
6. **No hardcoded credentials.** All secrets via AWS Secrets Manager; accessed through IAM instance roles.
7. **No SQS anywhere.** Banned by `ruff` TID251 — will fail CI.
8. **No ECS Fargate.** Permanently removed — use EC2 ARM64 ASGs.
9. **Trading logic PRs require two reviews.** All other PRs require at least one.
