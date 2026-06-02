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
- Repeatedly invoking Lambda every few seconds for polling is more expensive than a continuously running EC2 ASG process for workloads that run during market hours.
- Lambda cannot maintain WebSocket connections natively. Workarounds (API Gateway WebSocket + Lambda) add complexity without benefit for server-initiated connections to broker APIs.

### What To Do Instead

Use EC2 ARM64 ASGs for all workloads that:
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

---

## 9. Background asyncio Task Without a Watchdog

### The Anti-Pattern

Creating an `asyncio.create_task()` for a critical background loop and never checking whether it has died.

```python
# WRONG: fire-and-forget task creation
async def start(self):
    self._candle_stream_task = asyncio.create_task(
        self._candle_stream.start(),
        name="candle_stream",
    )
    # No watchdog — if this task dies, nobody knows
```

### Why It Is Wrong

- `asyncio.CancelledError` is a `BaseException` (not `Exception`) in Python 3.8+. An `except Exception` handler in the task body does NOT catch it. A stray `.cancel()` call, a timeout cancellation inside `asyncio.wait_for`, or a WebSocket disconnect can kill the task silently.
- Once a task is done (`.done() == True`), it never restarts itself. Critical streams go silent indefinitely.
- `task.exception()` and `task.cancel()` only work on live tasks — there is no automatic alerting when a task exits unexpectedly.

### What To Do Instead

Every critical background `asyncio.Task` must have a companion watchdog:

```python
async def _candle_stream_watchdog(self) -> None:
    while self._running:
        try:
            await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            break
        task = self._candle_stream_task
        if task is None or not task.done():
            continue
        reason = "cancelled" if task.cancelled() else str(task.exception())
        logger.critical("candle_stream_watchdog.task_dead", reason=reason)
        self._candle_stream_task = asyncio.create_task(
            self._candle_stream.start(), name="candle_stream"
        )
        logger.info("candle_stream_watchdog.restarted")
```

- Always cancel the watchdog BEFORE stopping the managed task in `stop()` (otherwise the watchdog restarts the task after it's intentionally stopped).
- Log CRITICAL on task death so the event is visible in CloudWatch alarms.

---

## 10. Calling `record_data_tick` After Validation Logic

### The Anti-Pattern

Calling `record_data_tick()` (or any health-heartbeat update) inside `validate_signal()` after one or more validators have already run.

```python
# WRONG: record_data_tick after age check
async def validate_signal(self, signal):
    age_result = await self._signal_age_validator.validate(signal)
    if not age_result.approved:
        return reject(age_result)  # <-- record_data_tick never reached
    # ...
    self._kill_switch_monitor.record_data_tick(signal.market)  # Too late!
```

### Why It Is Wrong

- Candle signals arrive 7-12 seconds old. During service startup warmup (5-10 min), candle strategies emit no signals at all. During that window, the staleness clock is frozen.
- If `record_data_tick` is gated behind a passing age check, ANY period of age-rejected signals (including normal warmup) advances the staleness clock toward the kill switch threshold.
- The kill switch fires during service restart warmup, not because the data feed is actually stale, but because no signals passed the age check during the warmup gap.

### What To Do Instead

Call `record_data_tick` as the VERY FIRST statement in `validate_signal()`, before any validation:

```python
async def validate_signal(self, signal):
    # Reset staleness clock immediately — signal arrival confirms feed is live.
    # Do this before age check so warmup/rejected signals still tick the clock.
    self._kill_switch_monitor.record_data_tick(signal.market)

    age_result = await self._signal_age_validator.validate(signal)
    if not age_result.approved:
        return reject(age_result)
    # ...
```

The staleness threshold protects against the data feed going completely silent (no signals at all). It must not fire because signals are arriving but failing age checks.

---

## 11. DynamoDB Key Constants Mismatched Between Setup Script and Service Code

### The Anti-Pattern

Defining DynamoDB PK/SK prefix constants in a service without verifying they match what the setup/seeding script actually writes.

```python
# In strategy_config_loader.py (WRONG):
_PK_PREFIX = "STRATEGY#"      # setup_local_tables.py writes "STRATEGY_CONFIG#"
_SK_PREFIX = "CONFIG#"         # setup_local_tables.py writes "ENV#"
```

### Why It Is Wrong

- Every `get_item` call returns `None` silently. No `ItemNotFoundException` is raised.
- The code falls back to `_DEFAULT_CONFIG` values, which have conservative defaults (e.g., `max_signals_per_day=10`).
- After 10 signals fire, ALL subsequent signals are blocked for the entire trading day — with no error in logs, only `daily_cap_reached cap=10`.
- This is a silent, session-destroying bug that is very hard to diagnose because both the setup script and the service code appear to be working correctly in isolation.

### What To Do Instead

1. Keep PK/SK prefix constants co-located with or cross-referenced to the setup script.
2. After any schema change, run a verification: `aws dynamodb scan --table-name <table> --endpoint-url http://localhost:4566 | jq '.Items[] | {PK, SK}'` and compare against the service's `_PK_PREFIX` / `_SK_PREFIX` constants.
3. Add a startup log that shows the resolved PK/SK for at least the first lookup — makes mismatches immediately visible in logs.
4. Write a startup assertion or integration test that scans the table for known keys and fails loudly if none are found.

---

## 12. Calling Live Broker API from a Sub-System Without a Paper Mode Guard

### The Anti-Pattern

A service component (MIS square-off, fill poller, reconciliation, etc.) calls `self._broker.place_order()` or any live broker API unconditionally, without checking whether the system is in paper trading mode.

```python
# WRONG: MIS square-off with no paper check
async def _place_mis_close_order(self, symbol, side, qty, ...):
    order_id = await self._zerodha.place_order(
        symbol=symbol,
        side=side,
        quantity=qty,
        order_type="MARKET",
        product="MIS",
    )
    return order_id
```

### Why It Is Wrong

- In paper mode, the Zerodha account is not whitelisted for order placement (only for market data). Every call raises `PermissionException: IP not allowed`.
- The sub-system interprets the Zerodha rejection as a trading failure — not a configuration error — and escalates through its error path (MIS triggers kill switch when all close orders fail past the deadline).
- The kill switch activates mid-session or on every container restart after market close, with a misleading root cause in logs (Zerodha IP error, not "paper mode misconfiguration").
- The bug is silent during development (no integration test exercises the real rejection path) and only manifests in live paper sessions.

### What To Do Instead

Every sub-system that places orders must gate on paper mode before calling any live broker API:

```python
async def _place_mis_close_order(self, position, symbol, close_side, close_qty, order_id):
    if self._paper_trading:
        fill_price = position.get("last_price") or position.get("avg_entry_price", 0.0)
        await self._order_manager.apply_fill_to_position(
            symbol=symbol,
            side=close_side,
            filled_quantity=close_qty,
            avg_fill_price=fill_price,
            last_price=fill_price,
            market="NSE",
            order_id=order_id,
            signal_id=f"mis-square-off-{symbol}",
            risk_decision_id="mis-auto-close",
        )
        logger.info("mis_square_off.paper_close_simulated", symbol=symbol)
        return order_id  # simulated

    # Live path: only reached when _paper_trading is False
    return await self._zerodha.place_order(...)
```

Rules:
- All components that accept a `paper_trading` parameter must store it and check it before every broker call.
- The paper path must still produce the correct side-effects (DynamoDB position update, Kafka event) — use `apply_fill_to_position` or equivalent.
- Wire `paper_trading` from settings at construction time, never as a per-call argument (prevents accidental mismatch).

---

## 13. Time-Gated Background Task That Does Not Check Deadline at Startup

### The Anti-Pattern

A background task that runs at a specific time (e.g., MIS square-off at 15:05 IST) computes its wait time with `max(0.0, seconds_until_target)`. When the service restarts after the target time has passed, `max(0.0, ...)` returns `0.0` and the task fires immediately — as if it is right at the scheduled time.

```python
# WRONG: No past-deadline check
async def run(self):
    while True:
        wait_secs = _seconds_until_ist("15:05")  # returns 0.0 when past 15:05
        await asyncio.sleep(wait_secs)            # 0.0 → fires immediately
        await self._execute()                     # finds positions open → escalates
```

### Why It Is Wrong

- The task was designed to run once per trading day. After the deadline (e.g., 15:10 IST), execution is no longer safe (partial fills can't be managed). But the task doesn't know it's too late — it just sees "wait = 0s".
- MIS square-off: fires on restart, fails to close positions via live broker (paper mode), hits deadline, activates kill switch. Kill switch fires on every post-market container restart.
- Any similar pattern (end-of-day reconciliation, post-close archiver) with the same flaw will exhibit the same false-positive escalation behavior.

### What To Do Instead

Check whether the current time is already past the deadline at the top of the scheduling loop. If past deadline, skip today and sleep until tomorrow:

```python
async def run(self):
    while True:
        wait_secs = _seconds_until_ist(_CLOSE_TIME)

        # Guard: if we're already past the deadline, skip this cycle
        if wait_secs == 0.0 and _seconds_until_ist(_DEADLINE_TIME) == 0.0:
            logger.warning(
                "mis_square_off.skipped_past_deadline",
                detail="Service started after deadline — skipping today",
            )
            await asyncio.sleep(86400)  # sleep until tomorrow
            continue

        await asyncio.sleep(wait_secs)
        await self._execute()
```

- Always log `skipped_past_deadline` so the skip is visible in CloudWatch, not silent.
- Use two guard times: the action time (15:05) and the hard deadline (15:10). Only skip if past the deadline — if between action and deadline, allow normal execution.

---

## 14. Instantiating Broker Clients Before DynamoDB Client Is Created

### The Anti-Pattern

Creating a `ZerodhaBrokerClient` (or any broker client that reads credentials from DynamoDB) before the DynamoDB client is wired, or without passing `dynamo_client` to the constructor.

```python
# WRONG: dynamo_client not passed; DynamoDB lookup silently returns None, None
self._zerodha = ZerodhaBrokerClient(settings=self._settings)
await self._zerodha.connect()  # falls back to stale env var ZERODHA_ACCESS_TOKEN

dynamo = get_dynamodb_client()  # too late — broker already connected with stale token
```

### Why It Is Wrong

- `ZerodhaTokenManager._load_token_from_dynamo()` begins with `if self._dynamo is None: return None, None`. Without an injected client, it never touches DynamoDB regardless of what token is stored there.
- `connect()` catches `TokenExpiredError` and silently falls back to the `ZERODHA_ACCESS_TOKEN` env var — which may be an old, expired token from a prior session.
- All downstream broker calls (`get_historical_candles`, `get_batch_quotes`, WebSocket auth) fail with `kiteconnect.exceptions.TokenException: Incorrect api_key or access_token`.
- The silent fallback makes diagnosis non-obvious: logs say "authenticated" but API calls all fail.

### What To Do Instead

Create the DynamoDB client BEFORE instantiating broker clients, and pass it explicitly:

```python
# CORRECT: dynamo client created first, passed to broker constructor
dynamo = get_dynamodb_client()
self._dynamo = dynamo
self._zerodha = ZerodhaBrokerClient(settings=self._settings, dynamo_client=dynamo)
await self._zerodha.connect()  # reads fresh token from DynamoDB
```

- The correct startup log is: `"Zerodha authenticated via DynamoDB token"`.
- The wrong startup log is: `"Zerodha authenticated via ZERODHA_ACCESS_TOKEN env var"` — this is a fallback path for local dev only.

---

## 15. Resolving Daily-Refreshed Credentials From Environment Variables Instead of the Durable Store

### The Anti-Pattern

Constructing a connector or client with a credential read directly from `settings.zerodha.access_token.get_secret_value()` (= `ZERODHA_ACCESS_TOKEN` env var) when a fresher version of that credential is stored in a durable store (DynamoDB) updated by the daily login flow.

```python
# WRONG: env var is stale after zerodha_login.py runs each morning
zerodha = ZerodhaConnector(
    api_key=self._settings.zerodha.api_key.get_secret_value(),
    access_token=self._settings.zerodha.access_token.get_secret_value(),  # stale
)
```

### Why It Is Wrong

- Zerodha tokens expire daily. The daily `zerodha_login.py` run stores a fresh token in DynamoDB but does NOT update the running container's env var.
- Any component initialized from the env var after the login run will use the expired token.
- The failure mode is silent at construction: the connector creates successfully. The 403 only surfaces when the WebSocket upgrade is attempted — at which point the service is already "running" and the health check passes.
- `ticks.nse` Kafka topic stays empty → tick-based strategies receive no input.

### What To Do Instead

Resolve daily-refreshed credentials from the durable store (DynamoDB via `ZerodhaTokenManager`) at service startup, before constructing the component that needs them. Fall back to the env var only if the durable lookup fails:

```python
_token = self._settings.zerodha.access_token.get_secret_value()  # fallback
try:
    from execution_engine.auth.zerodha_auth import ZerodhaTokenManager
    from shared.aws.clients import get_dynamodb_client
    _token = await ZerodhaTokenManager(
        dynamo_client=get_dynamodb_client(), settings=self._settings
    ).get_valid_token()
except Exception:
    logger.warning("DynamoDB token lookup failed — falling back to env var")

zerodha = ZerodhaConnector(api_key=..., access_token=_token)
```

The correct startup log is: `"zerodha_connector.token_loaded_from_dynamodb"`.
The fallback log is: `"zerodha_connector.token_fallback_env_var"` — investigate if this appears.
