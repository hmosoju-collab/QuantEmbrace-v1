# QuantEmbrace - Anti-Patterns

> Things we explicitly do not do. Each anti-pattern includes why it is wrong
> and what to do instead. When reviewing code or architecture changes, check
> this list for violations.

---

## 1. Lambda for Continuous Polling or Streaming

### The Anti-Pattern

Using AWS Lambda for workloads that require persistent connections or continuous execution, such as WebSocket market data streaming or continuous strategy evaluation loops.

### Why It Is Wrong

- Lambda has a hard 15-minute execution limit. WebSocket connections for market data must persist for the entire trading session (6+ hours).
- Lambda cold starts introduce unpredictable latency (100ms to several seconds). Trading workloads require consistent, low-latency responses.
- Repeatedly invoking Lambda every few seconds for polling is more expensive than a continuously running Fargate task for workloads that run during market hours.
- Lambda cannot maintain WebSocket connections natively. Workarounds (API Gateway WebSocket + Lambda) add complexity without benefit for server-initiated connections to broker APIs.

### What To Do Instead

Use ECS Fargate for all workloads that:
- Require persistent connections (WebSocket, long-polling).
- Run continuously for more than a few minutes.
- Need predictable, low latency.

Lambda is acceptable for:
- Infrequent scheduled jobs (e.g., nightly report generation).
- Event-driven processing triggered by S3 object creation or SNS notifications.
- One-off operational scripts.

---

## 2. Mixing Strategy, Execution, and Risk Logic

### The Anti-Pattern

Writing code where a single function or module handles signal generation, risk validation, and order submission together.

```python
# WRONG: Everything in one function
async def trade(market_data):
    if market_data.rsi < 30:  # Strategy logic
        if portfolio.exposure < MAX_EXPOSURE:  # Risk logic
            await broker.place_order(  # Execution logic
                symbol=market_data.symbol,
                side="BUY",
                quantity=100,
            )
```

### Why It Is Wrong

- **Untestable**: You cannot test the strategy without mocking the broker. You cannot test risk without generating real signals.
- **Unsafe**: A bug in the strategy code could accidentally modify risk parameters or skip risk checks entirely.
- **Undeployable independently**: Changing a strategy requires redeploying risk and execution code. Changing risk rules requires redeploying strategies.
- **Unauditable**: You cannot clearly trace which component approved or rejected a trade.

### What To Do Instead

Separate into distinct services with clear boundaries:

```python
# Strategy engine: generates signals only
class MomentumStrategy(BaseStrategy):
    async def on_bar(self, bar: Bar) -> Signal | None:
        if bar.rsi < 30:
            return Signal(instrument=bar.symbol, direction="BUY", strength=0.8)
        return None

# Risk engine: validates signals only
class RiskValidator:
    async def validate(self, signal: Signal) -> ValidationResult:
        if self.portfolio_exposure >= self.max_exposure:
            return ValidationResult(approved=False, reason="exposure_limit")
        return ValidationResult(approved=True)

# Execution engine: submits orders only
class OrderManager:
    async def submit(self, order: Order) -> OrderResponse:
        return await self.broker_adapter.place_order(order)
```

---

## 3. Direct Broker Calls from Strategy Engine

### The Anti-Pattern

Strategy code that directly calls broker APIs to place orders, check positions, or fetch account information.

```python
# WRONG: Strategy directly calls broker
class MyStrategy(BaseStrategy):
    def __init__(self, kite_client):
        self.kite = kite_client  # Direct broker reference

    async def on_tick(self, tick):
        if self.should_buy(tick):
            self.kite.place_order(  # Bypasses risk engine entirely
                variety="regular",
                exchange="NSE",
                tradingsymbol=tick.symbol,
                transaction_type="BUY",
                quantity=100,
                order_type="MARKET",
            )
```

### Why It Is Wrong

- **Bypasses risk engine**: The entire risk validation pipeline is circumvented. There is no position limit check, no daily loss check, no kill switch check.
- **Broker coupling**: The strategy is now tied to Zerodha's API. Switching to another broker requires modifying strategy code.
- **No audit trail**: The order was never logged through the standard pipeline, making it invisible to monitoring and compliance.

### What To Do Instead

Strategies emit `Signal` objects. Signals flow through the risk engine. Only the execution engine talks to brokers:

```
Strategy --> Signal --> Risk Engine --> Approved Signal --> Execution Engine --> Broker
```

Strategies must never import broker SDKs, broker adapters, or any execution-related code.

---

## 4. Storing Large Datasets in DynamoDB

### The Anti-Pattern

Using DynamoDB to store historical tick data, OHLCV time series, backtest results, or any dataset that grows unboundedly over time.

```python
# WRONG: Writing every tick to DynamoDB
for tick in tick_stream:
    await dynamodb.put_item(
        TableName="quantembrace-prod-ticks",
        Item={
            "instrument": {"S": tick.instrument},
            "timestamp": {"S": tick.timestamp.isoformat()},
            "price": {"N": str(tick.price)},
            "volume": {"N": str(tick.volume)},
        },
    )
```

### Why It Is Wrong

- **Cost**: DynamoDB charges per read/write capacity unit. At 1,000 ticks/second, write costs alone would exceed $1,000/month. The same data in S3 costs pennies.
- **Item size limit**: DynamoDB items are limited to 400 KB. This prevents storing large objects like backtest results or ML model artifacts.
- **Query limitations**: DynamoDB is optimized for key-value lookups, not time-range scans over millions of records. Scanning large datasets is slow and expensive.
- **Not designed for analytics**: You cannot run SQL-like analytical queries over DynamoDB efficiently. S3 + Athena is purpose-built for this.

### What To Do Instead

- **DynamoDB**: Current state only. Positions, active orders, risk parameters, instrument metadata, strategy configuration. Small, frequently accessed, key-value data.
- **S3**: All historical and bulk data. Tick data, OHLCV bars, backtest results, ML models, audit logs. Large, append-mostly, analytically queried data.
- **S3 + Athena**: For analytical queries over historical data. Parquet format with partition keys for efficient scanning.

---

## 5. Over-Engineering the ML Pipeline

### The Anti-Pattern

Building a complex, feature-rich ML infrastructure (feature stores, model serving clusters, A/B testing frameworks, real-time training) before the first profitable strategy is proven.

### Why It Is Wrong

- **Premature optimization**: ML is a tool, not the product. A simple moving average crossover strategy that works is infinitely more valuable than a sophisticated ML pipeline that does not generate alpha.
- **Latency risk**: Complex ML inference in the hot path adds latency. If a model takes 100ms to run, it may be too slow for tick-level strategy decisions.
- **Maintenance burden**: ML pipelines are notoriously fragile. Data drift, model degradation, feature pipeline failures all require ongoing attention that distracts from core trading logic.
- **Debugging difficulty**: When a trade goes wrong, tracing the cause through a deep ML pipeline is much harder than tracing through a rule-based strategy.

### What To Do Instead

1. Start with rule-based strategies (momentum, mean reversion, statistical arbitrage).
2. Add ML features incrementally: a single gradient-boosted model for signal strength scoring.
3. Keep inference lightweight: pre-compute features, use simple models (XGBoost, not deep learning) until justified by data.
4. Train offline, infer online. Training jobs run as separate batch processes, not in the trading hot path.
5. Every ML model must beat a simple baseline before being deployed.

---

## 6. Hardcoding Broker Credentials

### The Anti-Pattern

Embedding API keys, secrets, access tokens, or any credentials directly in source code, configuration files, environment variable defaults, or Docker images.

```python
# WRONG: Credentials in code
ZERODHA_API_KEY = "abc123xyz"
ZERODHA_API_SECRET = "secret456"

# WRONG: Credentials in .env files committed to git
# .env
ALPACA_API_KEY=PKTEST12345
ALPACA_SECRET_KEY=secretabc

# WRONG: Credentials baked into Docker image
ENV ZERODHA_API_KEY=abc123xyz
```

### Why It Is Wrong

- **Security breach**: Anyone with repository access can see and use the credentials. If the repo is accidentally made public, all trading accounts are compromised.
- **Credential rotation**: Changing a credential requires a code change, PR, build, and deployment. With a secrets manager, rotation is instant.
- **Environment leakage**: The same credentials used in dev are used in prod, or dev credentials accidentally reach production.
- **Audit trail**: No record of who accessed credentials or when.

### What To Do Instead

- Store all credentials in **AWS Secrets Manager** under the path `quantembrace/{env}/{service}/{secret_name}`.
- Load credentials at runtime:

```python
import boto3
import json

def get_secret(secret_name: str) -> dict:
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])

# Usage
zerodha_creds = get_secret("quantembrace/prod/zerodha/api-credentials")
api_key = zerodha_creds["api_key"]
api_secret = zerodha_creds["api_secret"]
```

- Use IAM task roles for ECS tasks to access Secrets Manager (no AWS credentials in the container).
- Add `.env`, `*.pem`, `*.key`, and `credentials*` to `.gitignore`.
- Pre-commit hooks scan for potential secrets using tools like `detect-secrets`.

---

## 7. Deploying Without Risk Validation Hooks

### The Anti-Pattern

Deploying code changes to production without running risk-related validation checks in the CI/CD pipeline.

### Why It Is Wrong

- A code change could inadvertently modify risk parameters, disable safety checks, or alter the signal-to-order pipeline in unsafe ways.
- Without automated validation, the only safety net is human code review, which is fallible.
- A single deployment without risk checks can result in uncontrolled trading and financial loss.

### What To Do Instead

The CI/CD pipeline must include these mandatory gates before production deployment:

1. **Import boundary check**: Verify that strategy code does not import from execution or broker modules. Verify that no service bypasses the risk engine.
2. **Risk parameter validation**: If risk parameters are changed, verify they are within safe bounds (e.g., max daily loss is not set to 100%).
3. **Kill switch test**: Verify the kill switch can be activated and stops all order submission.
4. **Integration test**: Run the signal-to-risk-to-execution pipeline with a mock broker to verify the complete flow works.
5. **Staging deployment**: Deploy to staging and run paper trading for at least one trading session before promoting to production.

These checks are not optional. The pipeline must fail if any check fails, and there must be no override mechanism that a single person can invoke.

---

## 8. Using Synchronous HTTP for Market Data

### The Anti-Pattern

Polling broker REST APIs at regular intervals to get market data instead of using WebSocket streaming.

```python
# WRONG: Polling for market data
while True:
    response = requests.get(f"{BROKER_API}/quotes/{symbol}")
    tick = parse_tick(response.json())
    process_tick(tick)
    time.sleep(0.5)  # Poll every 500ms
```

### Why It Is Wrong

- **Stale data**: With 500ms polling, you miss every price change between polls. In fast-moving markets, this means missing the signal entirely.
- **Rate limits**: Broker REST APIs have rate limits (Zerodha: 3 requests/second, Alpaca: 200 requests/minute). Polling multiple instruments quickly hits these limits.
- **Latency**: Each HTTP request incurs DNS resolution, TLS handshake (if not reused), and round-trip time. WebSocket connections are persistent with near-zero per-message overhead.
- **Cost**: Each REST API call may count toward a usage quota. WebSocket connections typically have flat or no per-message pricing.
- **Resource waste**: Most polls return the same data (no price change since last poll), wasting compute and network resources.

### What To Do Instead

Use WebSocket streaming for all real-time market data:

```python
# CORRECT: WebSocket streaming
async def connect_and_stream(instruments: list[str]):
    async with websocket_connect(BROKER_WS_URL) as ws:
        await ws.send(json.dumps({
            "action": "subscribe",
            "instruments": instruments,
        }))

        async for message in ws:
            tick = parse_tick(message)
            await process_tick(tick)
```

- Zerodha: Use Kite Ticker WebSocket API for NSE real-time data.
- Alpaca: Use the real-time data WebSocket for US equities.
- Both brokers provide WebSocket APIs specifically designed for streaming market data.
- Implement automatic reconnection with exponential backoff for connection drops.
- REST APIs are acceptable for non-real-time operations: fetching historical data, account information, instrument lists, and order status (where WebSocket events are not available).
