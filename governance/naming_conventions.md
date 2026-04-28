# QuantEmbrace - Naming Conventions

> Consistent naming across all code, infrastructure, and operational artifacts.
> All contributors must follow these conventions. CI checks enforce where possible.

---

## Python Code

### Files and Modules

- **Convention**: `snake_case`
- **Rule**: All Python files use lowercase with underscores. No hyphens, no camelCase.

```
# Correct
zerodha_connector.py
order_manager.py
risk_validator.py
momentum_strategy.py

# Incorrect
ZerodhaConnector.py
order-manager.py
riskValidator.py
```

### Functions and Methods

- **Convention**: `snake_case`
- **Rule**: Descriptive verb-noun pairs. Private methods prefixed with single underscore.

```python
# Correct
def calculate_position_size(signal: Signal) -> float: ...
def validate_order(order: Order) -> ValidationResult: ...
def _normalize_tick_data(raw_tick: dict) -> Tick: ...

# Incorrect
def calcPosSize(signal): ...
def ValidateOrder(order): ...
def __internal_helper(data): ...  # double underscore reserved for name mangling
```

### Variables

- **Convention**: `snake_case`
- **Rule**: Descriptive names. No single-letter variables except in comprehensions and loop counters.

```python
# Correct
max_position_size = 100_000
current_drawdown = portfolio.calculate_drawdown()
order_ids = [o.id for o in pending_orders]

# Incorrect
mps = 100000
x = portfolio.calculate_drawdown()
```

### Classes

- **Convention**: `PascalCase`
- **Rule**: Noun or noun phrase. Abstract base classes prefixed with `Base` or suffixed with `ABC`/`Interface`.

```python
# Correct
class ZerodhaConnector(BaseBrokerAdapter): ...
class MomentumStrategy(BaseStrategy): ...
class OrderValidationError(Exception): ...
class RiskEngineConfig: ...

# Incorrect
class zerodha_connector: ...
class momentumStrategy: ...
class RISK_ENGINE_CONFIG: ...
```

### Constants

- **Convention**: `UPPER_SNAKE_CASE`
- **Rule**: Module-level constants only. Defined at the top of the file after imports.

```python
# Correct
MAX_DAILY_LOSS_PCT = 0.02
DEFAULT_ORDER_TIMEOUT_SECONDS = 30
NSE_MARKET_OPEN_IST = time(9, 15)
US_MARKET_OPEN_ET = time(9, 30)

# Incorrect
maxDailyLoss = 0.02
default_timeout = 30
```

### Type Aliases and Generics

- **Convention**: `PascalCase`
- **Rule**: Descriptive of the type being aliased.

```python
# Correct
OrderId = str
CorrelationId = UUID
TickStream = AsyncGenerator[Tick, None]
StrategyParams = dict[str, float | int | str]
```

### Enums

- **Convention**: `PascalCase` for class, `UPPER_SNAKE_CASE` for members.

```python
class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderStatus(str, Enum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
```

### Pydantic Models

- **Convention**: `PascalCase` for class, `snake_case` for fields.
- **Rule**: Inherit from `pydantic.BaseModel`. All fields must have type annotations.

```python
class Signal(BaseModel):
    signal_id: UUID
    instrument: str
    direction: OrderSide
    strength: float
    strategy_name: str
    timestamp: datetime
    metadata: dict[str, Any] = {}
```

---

## Terraform / Infrastructure

### Resource Names

- **Convention**: `snake_case` with resource type context
- **Rule**: Terraform resource names describe what they are. Use underscores, not hyphens.

```hcl
# Correct
resource "aws_ecs_service" "strategy_engine" { ... }
resource "aws_sqs_queue" "signal_queue" { ... }
resource "aws_iam_role" "ecs_task_execution_role" { ... }
resource "aws_cloudwatch_metric_alarm" "high_daily_loss" { ... }

# Incorrect
resource "aws_ecs_service" "StrategyEngine" { ... }
resource "aws_sqs_queue" "signal-queue" { ... }
```

### Variable Names

- **Convention**: `snake_case`
- **Rule**: Prefixed with context where ambiguous.

```hcl
variable "environment" { ... }
variable "ecs_task_cpu" { ... }
variable "risk_engine_max_daily_loss_pct" { ... }
```

### Module Names

- **Convention**: `snake_case`
- **Rule**: Named after the infrastructure component they manage.

```
infra/
  modules/
    ecs_cluster/
    vpc_network/
    dynamodb_tables/
    s3_buckets/
    sqs_queues/
    iam_roles/
    cloudwatch_alarms/
```

---

## AWS Resource Naming

### S3 Buckets

- **Convention**: `quantembrace-{env}-{purpose}`
- **Rule**: Lowercase, hyphens only. Environment is always included. Globally unique.

```
quantembrace-dev-tick-data
quantembrace-dev-ohlcv-data
quantembrace-dev-backtest-results
quantembrace-dev-ml-models
quantembrace-staging-tick-data
quantembrace-prod-tick-data
quantembrace-prod-ohlcv-data
quantembrace-prod-backtest-results
quantembrace-prod-ml-models
quantembrace-prod-audit-logs
```

### DynamoDB Tables

- **Convention**: `quantembrace-{env}-{table_name}`
- **Rule**: Lowercase with hyphens. Table name describes the entity stored.

```
quantembrace-dev-positions
quantembrace-dev-orders
quantembrace-dev-risk-parameters
quantembrace-dev-instrument-metadata
quantembrace-dev-strategy-state
quantembrace-prod-positions
quantembrace-prod-orders
quantembrace-prod-risk-parameters
quantembrace-prod-instrument-metadata
quantembrace-prod-strategy-state
```

### ECS Services

- **Convention**: `quantembrace-{env}-{service_name}`
- **Rule**: Matches the service directory name from the project structure.

```
quantembrace-dev-data-ingestion
quantembrace-dev-strategy-engine
quantembrace-dev-execution-engine
quantembrace-dev-risk-engine
quantembrace-dev-ai-engine
quantembrace-prod-data-ingestion
quantembrace-prod-strategy-engine
quantembrace-prod-execution-engine
quantembrace-prod-risk-engine
quantembrace-prod-ai-engine
```

### ECS Task Definitions

- **Convention**: `quantembrace-{env}-{service_name}-task`

```
quantembrace-prod-data-ingestion-task
quantembrace-prod-strategy-engine-task
quantembrace-prod-execution-engine-task
quantembrace-prod-risk-engine-task
```

### SQS Queues

- **Convention**: `quantembrace-{env}-{purpose}-queue`
- **Rule**: Dead-letter queues append `-dlq`.

```
quantembrace-prod-signals-queue
quantembrace-prod-signals-queue-dlq
quantembrace-prod-orders-queue
quantembrace-prod-orders-queue-dlq
quantembrace-prod-risk-events-queue
```

### SNS Topics

- **Convention**: `quantembrace-{env}-{event_type}-topic`

```
quantembrace-prod-kill-switch-topic
quantembrace-prod-trade-executed-topic
quantembrace-prod-risk-breach-topic
quantembrace-prod-system-alerts-topic
```

### CloudWatch Log Groups

- **Convention**: `/quantembrace/{env}/{service_name}`

```
/quantembrace/prod/data-ingestion
/quantembrace/prod/strategy-engine
/quantembrace/prod/execution-engine
/quantembrace/prod/risk-engine
/quantembrace/prod/ai-engine
```

### IAM Roles

- **Convention**: `quantembrace-{env}-{service_name}-{role_type}`

```
quantembrace-prod-ecs-task-execution-role
quantembrace-prod-data-ingestion-task-role
quantembrace-prod-strategy-engine-task-role
quantembrace-prod-execution-engine-task-role
quantembrace-prod-risk-engine-task-role
```

### Secrets Manager Secrets

- **Convention**: `quantembrace/{env}/{service}/{secret_name}`

```
quantembrace/prod/zerodha/api-key
quantembrace/prod/zerodha/api-secret
quantembrace/prod/zerodha/access-token
quantembrace/prod/alpaca/api-key
quantembrace/prod/alpaca/api-secret
```

---

## Environment Variables

- **Convention**: `QUANTEMBRACE_{SERVICE}_{KEY}`
- **Rule**: All uppercase, underscores only. Service prefix identifies the owning service. Shared variables use `SHARED` as the service prefix.

```bash
# Shared
QUANTEMBRACE_SHARED_ENVIRONMENT=prod
QUANTEMBRACE_SHARED_AWS_REGION=ap-south-1
QUANTEMBRACE_SHARED_LOG_LEVEL=INFO

# Data Ingestion
QUANTEMBRACE_DATA_INGESTION_ZERODHA_WS_URL=wss://ws.kite.trade
QUANTEMBRACE_DATA_INGESTION_ALPACA_WS_URL=wss://stream.data.alpaca.markets
QUANTEMBRACE_DATA_INGESTION_TICK_BATCH_SIZE=1000
QUANTEMBRACE_DATA_INGESTION_S3_BUCKET=quantembrace-prod-tick-data

# Strategy Engine
QUANTEMBRACE_STRATEGY_ENGINE_ENABLED_STRATEGIES=momentum_v1,mean_reversion_v2
QUANTEMBRACE_STRATEGY_ENGINE_BACKTEST_S3_BUCKET=quantembrace-prod-backtest-results

# Execution Engine
QUANTEMBRACE_EXECUTION_ENGINE_ORDER_TIMEOUT_SECONDS=30
QUANTEMBRACE_EXECUTION_ENGINE_MAX_RETRY_ATTEMPTS=3

# Risk Engine
QUANTEMBRACE_RISK_ENGINE_MAX_DAILY_LOSS_PCT=0.02
QUANTEMBRACE_RISK_ENGINE_MAX_POSITION_SIZE=100000
QUANTEMBRACE_RISK_ENGINE_KILL_SWITCH_ENABLED=true

# AI Engine
QUANTEMBRACE_AI_ENGINE_MODEL_BUCKET=quantembrace-prod-ml-models
QUANTEMBRACE_AI_ENGINE_INFERENCE_TIMEOUT_MS=50
```

---

## Git Conventions

### Branch Names

- **Convention**: `{type}/{short-description}`
- **Rule**: Lowercase, hyphens for spaces. Type prefix is mandatory.

```
feature/zerodha-websocket-connector
feature/momentum-strategy-v1
feature/kill-switch-implementation
bugfix/order-dedup-race-condition
bugfix/tick-data-timezone-offset
hotfix/risk-engine-null-position
hotfix/alpaca-auth-token-refresh
refactor/broker-adapter-interface
chore/update-terraform-providers
docs/architecture-sequence-diagrams
```

### Commit Messages

- **Convention**: Conventional Commits
- **Rule**: Type prefix is mandatory. Scope is optional but encouraged. Body explains "why", not "what".

```
feat(data-ingestion): add Zerodha Kite WebSocket connector

Implements real-time tick data streaming from NSE via Kite Ticker API.
Includes automatic reconnection with exponential backoff and tick
normalization to the internal Tick schema.

fix(execution-engine): resolve order deduplication race condition

Orders submitted within the same millisecond could bypass UUID-based
dedup check due to DynamoDB eventual consistency. Switched to
conditional PutItem with attribute_not_exists.

refactor(risk-engine): extract position limit validation to dedicated module

The validate_order function exceeded 200 lines. Position limit checks
are now in position_limits.py for better testability.

docs(architecture): add order lifecycle sequence diagram

chore(infra): upgrade Terraform AWS provider to 5.40.0
```

**Allowed commit types**:

| Type | Use Case |
|------|----------|
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `refactor` | Code restructuring without behavior change |
| `docs` | Documentation only |
| `test` | Adding or updating tests |
| `chore` | Build, CI, dependency updates |
| `perf` | Performance improvement |
| `style` | Code formatting (no logic change) |

---

## Test Files

- **Convention**: `test_{module_name}.py`
- **Rule**: Mirror the source file path under `/tests/`.

```
services/risk_engine/validators/position_limits.py
  -> tests/unit/risk_engine/validators/test_position_limits.py

services/execution_engine/adapters/zerodha_adapter.py
  -> tests/unit/execution_engine/adapters/test_zerodha_adapter.py

tests/integration/test_signal_to_order_pipeline.py
tests/backtest/test_momentum_strategy_v1.py
```

---

## Docker and Container Images

- **Convention**: `quantembrace-{service_name}:{version}`
- **Rule**: Version tags follow semver. Latest is tagged for dev only.

```
quantembrace-data-ingestion:1.2.0
quantembrace-strategy-engine:1.2.0
quantembrace-execution-engine:1.2.0
quantembrace-risk-engine:1.2.0
quantembrace-ai-engine:1.2.0
```

### ECR Repositories

- **Convention**: `quantembrace/{service_name}`

```
quantembrace/data-ingestion
quantembrace/strategy-engine
quantembrace/execution-engine
quantembrace/risk-engine
quantembrace/ai-engine
```
