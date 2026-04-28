# QuantEmbrace — Services Reference

> This document is a per-service reference. For how services interact, read [02_architecture.md](02_architecture.md). For the step-by-step trade flow, read [03_signal_lifecycle.md](03_signal_lifecycle.md).

Each section covers:
- What the service does and why it exists
- Key files (what to look at when debugging)
- Configuration
- How it connects to other services
- Common failure modes and how to diagnose them

---

## Table of Contents

1. [data_ingestion — Market Data Collection](#1-data_ingestion--market-data-collection)
2. [strategy_engine — Signal Generation](#2-strategy_engine--signal-generation)
3. [risk_engine — Risk Validation](#3-risk_engine--risk-validation)
4. [execution_engine — Order Placement](#4-execution_engine--order-placement)
5. [ai_engine — ML Predictions](#5-ai_engine--ml-predictions)
6. [shared — Shared Libraries](#6-shared--shared-libraries)
7. [Service Dependency Map](#service-dependency-map)
8. [Environment Variables Reference](#environment-variables-reference)

---

## 1. data_ingestion — Market Data Collection

### Purpose

Connects to broker WebSocket feeds, normalizes all incoming tick data to a unified format, and stores it in DynamoDB (latest price, hot cache) and S3 (historical archive, cold storage). Also publishes normalized ticks to SQS for downstream services.

This is the entry point of all market data into the system.

### Key Files

| File | What it does |
|---|---|
| `service.py` | Entry point — starts WebSocket connections and the main event loop |
| `connectors/zerodha_connector.py` | Handles Zerodha Kite Ticker WebSocket (NSE market data) |
| `connectors/alpaca_connector.py` | Handles Alpaca streaming WebSocket (US market data) |
| `connectors/base.py` | Abstract base class that both connectors implement |
| `processors/tick_processor.py` | Normalizes broker-specific tick formats into unified `MarketTick` |
| `storage/dynamo_writer.py` | Writes latest price to DynamoDB `latest-prices` table |
| `storage/s3_writer.py` | Buffers ticks and writes Parquet files to S3 |

### Configuration

Key settings (from environment variables / `shared/config/settings.py`):

| Variable | Purpose | Example |
|---|---|---|
| `KITE_API_KEY` | Zerodha API authentication | `abc123xyz` |
| `KITE_ACCESS_TOKEN` | Zerodha session token (refreshed daily) | Set at runtime |
| `ALPACA_API_KEY` | Alpaca API authentication | `PKXYZ123` |
| `ALPACA_BASE_URL` | Alpaca endpoint (paper vs live) | `https://paper-api.alpaca.markets` |
| `S3_BUCKET_DATA` | S3 bucket for tick data | `quantembrace-market-data-history` |
| `DYNAMODB_TABLE_PREFIX` | Prefix for DynamoDB table names | `qe-prod` |

### How It Connects to Other Services

```
data_ingestion ──writes──► DynamoDB: latest-prices table
               ──writes──► S3: market data bucket (Parquet files)
               ──publishes► SQS: quantembrace-market-data queue → strategy_engine reads this
```

### Zerodha Token Refresh

Kite Connect access tokens expire **every day at approximately 07:30 IST**. The connector handles this automatically:

1. On startup, fetch token from AWS Secrets Manager
2. Subscribe to instruments via WebSocket
3. If WebSocket disconnects (e.g., token expired), catch the disconnect error
4. Trigger re-authentication flow → store new token in Secrets Manager
5. Reconnect with new token

### Instrument Subscription

**You do not hardcode symbols in code.** The instrument universe is loaded at startup from `configs/instruments.yaml`. Data ingestion subscribes to exactly the symbols marked `active: true` in that file.

```yaml
# configs/instruments.yaml  — edit this to control what is watched
nse:
  default_strategy:
    short_window: 10
    long_window: 50
    min_confidence: 0.60
  instruments:
    - symbol: RELIANCE
      sector: Energy
      lot_size: 25
      active: true          # ← data ingestion subscribes to this

    - symbol: WIPRO
      sector: IT
      lot_size: 50
      active: false         # ← ignored, no subscription, no signals

    - symbol: RELIANCE
      sector: Energy
      active: true
      strategy_override:
        short_window: 8     # ← per-symbol MA override, faster signals

us:
  instruments:
    - symbol: NVDA
      active: true
      strategy_override:
        short_window: 8     # NVDA moves fast — shorter window
```

**To add a stock:** Add an entry with `active: true`, restart service.
**To pause a stock:** Set `active: false`, restart service. No code change.
**To tune sensitivity:** Change `short_window`/`long_window` for that symbol.

The pre-built universe covers 20+ Nifty 50 blue-chips (NSE) and 10+ S&P 500 names (US).

### Common Failure Modes

| Symptom | Likely Cause | How to Diagnose |
|---|---|---|
| No ticks flowing | WebSocket disconnected | Check CloudWatch logs for `websocket_disconnected` events |
| Stale prices in DynamoDB | S3 writes succeeding but DynamoDB writes failing | Check `dynamo_write_errors` CloudWatch metric |
| `AUTH_INVALID` errors | Kite token expired | Check if today's token is in Secrets Manager |
| High S3 write latency | S3 bucket policy or VPC endpoint issue | Check `s3_write_latency_ms` metric |

---

## 2. strategy_engine — Signal Generation

### Purpose

Receives normalized market ticks from SQS, runs them through all registered strategies, and publishes generated signals to the Risk Engine's SQS queue.

This service is stateless in its hot path — strategies compute purely from market data. Strategy state (indicator values, position tracking for strategy logic) is persisted to DynamoDB for restart safety.

### Key Files

| File | What it does |
|---|---|
| `service.py` | Main event loop — loads universe, registers strategies, SQS consumer, signal publisher |
| `universe/instrument_loader.py` | **Reads `configs/instruments.yaml`** — returns active instruments per market with strategy params |
| `strategies/base_strategy.py` | Abstract interface all strategies must implement |
| `strategies/momentum_strategy.py` | Momentum crossover strategy (currently implemented) |
| `signals/signal.py` | `Signal` Pydantic model — the core data structure |
| `backtesting/backtester.py` | Replays historical S3 data through strategies for testing |

### How to Read `momentum_strategy.py`

This is the only implemented strategy today. Here's what it does:

```python
class MomentumStrategy(BaseStrategy):
    """
    Golden/Death cross strategy.
    
    BUY signal when:  short moving average crosses ABOVE long moving average
    SELL signal when: short moving average crosses BELOW long moving average
    
    Default params: short_window=10, long_window=50 (10-tick and 50-tick averages)
    """
    
    async def on_tick(self, symbol, price, volume, timestamp):
        """
        Called every time a new price arrives for a symbol this strategy watches.
        Updates internal state (price history, moving averages).
        """
        
    async def generate_signal(self) -> Optional[Signal]:
        """
        After processing a tick, check if a signal should be generated.
        Returns Signal if crossover detected, None otherwise.
        """
```

The strategy watches these symbols by default:
- **NSE:** RELIANCE, TCS, INFY, HDFCBANK, ICICIBANK
- **US:** AAPL, MSFT, GOOGL, AMZN, NVDA

### Adding a New Strategy

To add a new strategy (see [07_contributing.md](07_contributing.md) for the full process):

1. Create `services/strategy_engine/strategies/your_strategy.py`
2. Implement `BaseStrategy` (all 4 abstract methods)
3. Register in `service.py → _register_default_strategies()`
4. Write unit tests in `tests/unit/strategy_engine/`
5. Backtest before deploying to production

### Signal Data Model

The `Signal` object carries everything a downstream consumer (risk engine, execution engine) needs:

```python
@dataclass
class Signal:
    signal_id: str          # UUID — unique forever, used for idempotency
    strategy_name: str      # Which strategy generated this
    market: str             # "NSE" or "US"
    instrument: str         # "NSE:RELIANCE" or "US:AAPL"
    direction: Direction    # Direction.BUY or Direction.SELL
    quantity: int           # Number of shares/units
    order_type: OrderType   # MARKET, LIMIT, etc.
    limit_price: Decimal    # Only for LIMIT orders
    stop_price: Decimal     # Stop-loss price — REQUIRED
    confidence: float       # 0.0–1.0, used by risk engine for position sizing
    metadata: dict          # Strategy-specific context for debugging
    created_at: datetime    # UTC timestamp
```

### Configuration

| Variable | Purpose | Default |
|---|---|---|
| `STRATEGY_CONFIG_PATH` | Path to strategy config YAML in S3 | `s3://quantembrace-configs/strategies.yaml` |
| `MAX_SIGNALS_PER_MINUTE` | Rate limit on signal generation | 60 |

### Common Failure Modes

| Symptom | Likely Cause | How to Diagnose |
|---|---|---|
| No signals generated | SQS tick queue not being consumed | Check `sqs_messages_received` metric |
| Signals generated but no trades | Risk engine rejecting all signals | Check S3 audit logs in `risk-audit/` |
| Strategy using stale data | DynamoDB latest-prices table stale | Check data_ingestion service health |
| "Not enough data" — signals delayed at startup | Moving average windows need N ticks to warm up | Normal — takes ~50 ticks per symbol |

---

## 3. risk_engine — Risk Validation

### Purpose

The mandatory gatekeeper between Strategy and Execution. Validates every signal before it can become an order. No signal bypasses this service. If this service is down, trading halts.

### Key Files

| File | What it does |
|---|---|
| `service.py` | Main service — SQS consumer, validation pipeline, decision publisher |
| `killswitch/killswitch.py` | Kill switch state management (DynamoDB-backed) |
| `validators/position_validator.py` | Checks position count and size limits |
| `validators/exposure_validator.py` | Checks total portfolio exposure |
| `validators/loss_validator.py` | Checks daily P&L drawdown limits |
| `limits/risk_limits.py` | Configurable threshold values |

### The Validation Pipeline in Detail

```python
# service.py → validate_signal()
#
# Order matters: cheapest checks first to fail fast on obvious rejections

async def validate_signal(self, signal: Signal) -> RiskDecision:
    
    # 1. Kill switch — fastest check, no DB needed (cached in memory)
    if await self._kill_switch.is_active():
        return reject("kill_switch_active")

    # 2. Position validator — reads DynamoDB positions table
    #    Checks: max open positions per market, max size per instrument
    result = await self._position_validator.validate(signal)
    if not result.approved:
        return reject(result.reason)

    # 3. Exposure validator — reads DynamoDB positions table
    #    Checks: gross exposure, net exposure as % of portfolio
    result = await self._exposure_validator.validate(signal)
    if not result.approved:
        return reject(result.reason)

    # 4. Daily loss validator — reads DynamoDB risk-state table
    #    Checks: today's realized + unrealized P&L against daily loss limit
    result = await self._loss_validator.validate(signal)
    if not result.approved:
        await self._kill_switch.activate(reason=result.reason)  # Auto-halt
        return reject(result.reason)

    return approve()
```

### Risk Limits (Default Configuration)

These are the out-of-the-box limits. All are configurable in `risk_limits.py` and can be changed at runtime via DynamoDB (no service restart needed):

| Limit | Default Value | What It Prevents |
|---|---|---|
| `max_position_size_pct` | 5% of portfolio | Overconcentration in one stock |
| `max_total_exposure_pct` | 80% of portfolio | Overleveraging |
| `max_daily_loss_pct` | 3% of portfolio | Catastrophic daily loss |
| `max_single_order_value` | ₹5,00,000 / $10,000 | Accidentally large orders |
| `max_open_orders` | 10 | Too many unconfirmed orders |

### Kill Switch States

The kill switch stores these fields in DynamoDB (`risk-state` table, key `kill_switch`):

```json
{
    "active": false,
    "reason": "",
    "activated_at": null,
    "activated_by": "auto|manual",
    "deactivated_at": "2026-04-24T03:00:00Z"
}
```

To manually activate from the command line (during an emergency):
```bash
# Set kill switch ON via AWS CLI
aws dynamodb put-item \
    --table-name qe-prod-risk-state \
    --item '{"key": {"S": "kill_switch"}, "active": {"BOOL": true}, "reason": {"S": "manual halt"}}' \
    --region ap-south-1
```

### Audit Log Format

Every decision is stored in S3 at:
```
s3://{S3_BUCKET_LOGS}/risk-audit/{YYYY-MM-DD}/{risk_decision_id}.json
```

To query all rejections for a specific day using S3 Select:
```sql
SELECT * FROM S3Object
WHERE s.status = 'REJECTED'
AND s.timestamp >= '2026-04-24T00:00:00Z'
```

### Common Failure Modes

| Symptom | Likely Cause | How to Diagnose |
|---|---|---|
| All signals rejected | Kill switch ON | Check DynamoDB `risk-state` for `kill_switch.active` |
| Risk engine not starting | DynamoDB connectivity issue | Check ECS task logs for DynamoDB errors |
| Loss limit triggering unexpectedly | P&L calculation bug or stale price data | Check `daily_pnl` value in DynamoDB `risk-state` |
| High rejection rate for position limits | Too many positions open | Check DynamoDB `positions` table |

---

## 4. execution_engine — Order Placement

### Purpose

Receives risk-approved signals from SQS and translates them into broker orders. Handles routing (Zerodha for NSE, Alpaca for US), idempotency, retry logic, and order lifecycle tracking.

**Critical:** This service only accepts signals that carry a `risk_decision_id`. If a signal arrives without one, it is immediately rejected. This is a hard enforcement of the Risk Engine requirement.

### Key Files

| File | What it does |
|---|---|
| `service.py` | Main service — SQS consumer, signal validation, order routing |
| `brokers/zerodha_broker.py` | Zerodha Kite Connect adapter (NSE) |
| `brokers/alpaca_broker.py` | Alpaca Trading API adapter (US) |
| `brokers/base_broker.py` | `BrokerClient` protocol — the common interface both brokers implement |
| `orders/order.py` | Order, OrderRequest, OrderResponse Pydantic models |
| `orders/order_manager.py` | DynamoDB order lifecycle management |
| `retry/retry_handler.py` | Exponential backoff retry logic |

### The Broker Abstraction

Both Zerodha and Alpaca implement the same `BrokerClient` protocol:

```python
class BrokerClient(Protocol):
    async def place_order(self, order: OrderRequest) -> OrderResponse: ...
    async def cancel_order(self, order_id: str) -> CancelResponse: ...
    async def get_positions(self) -> list[Position]: ...
    async def get_order_status(self, order_id: str) -> OrderStatus: ...
    async def subscribe_quotes(self, symbols: list[str], callback) -> None: ...
```

To add a new broker (e.g., Interactive Brokers), you implement this protocol and register it. The routing logic maps `market` → broker. No other code changes needed.

### Zerodha-Specific Notes

- **Token expiry:** Access tokens expire daily. The broker adapter reads the current token from AWS Secrets Manager. If a 403 error is received, it triggers token refresh.
- **Rate limits:** 10 requests/second for order APIs. The adapter tracks request times and sleeps if needed.
- **Product types:**
  - `MIS` (Margin Intraday Square-off) — intraday positions, auto-squared at 15:15 IST
  - `CNC` (Cash and Carry) — delivery positions, held overnight
  - `NRML` — F&O normal positions

### Alpaca-Specific Notes

- **Paper trading:** Set `ALPACA_BASE_URL=https://paper-api.alpaca.markets` for simulation. This is the default for non-production environments.
- **Fractional shares:** Alpaca supports fractional shares. Position sizing can use non-integer quantities.
- **PDT rules:** For accounts under $25k, Pattern Day Trader restrictions apply. The execution engine does not enforce this — it relies on Alpaca to reject PDT-violating orders.

### DynamoDB Orders Table Schema

```
Table: qe-{env}-orders
Partition key: order_id (String)
GSI: signal_id-index (for idempotency lookups)

Item structure:
{
    "order_id":        "qe-ord-abc123",
    "signal_id":       "f47ac10b-...",       ← Links back to signal
    "risk_decision_id": "abc-789-xyz",        ← Links back to risk decision
    "broker_order_id": "240424000012345",     ← Broker's own order ID
    "symbol":          "RELIANCE",
    "market":          "NSE",
    "side":            "BUY",
    "quantity":        20,
    "status":          "FILLED",
    "average_price":   "2454.00",
    "filled_quantity": 20,
    "created_at":      "2026-04-24T03:46:04Z",
    "placed_at":       "2026-04-24T03:46:04.500Z",
    "filled_at":       "2026-04-24T03:46:05.200Z",
    "TTL":             1745578800             ← Auto-expire from DynamoDB after 90 days
}
```

### Common Failure Modes

| Symptom | Likely Cause | How to Diagnose |
|---|---|---|
| Orders stuck in PENDING | Broker API down or auth failure | Check broker API status page, check auth tokens |
| Duplicate orders | Idempotency bug | Check DynamoDB for signal_id duplicates (should never happen) |
| High retry rate | Broker rate limiting | Check `order_retry_count` CloudWatch metric |
| Reconciliation failing on startup | Broker API order history window expired | Increase history window in `_reconcile_state()` |

---

## 5. ai_engine — ML Predictions

### Purpose

Provides machine-learning predictions to enrich strategy signals. Currently provides:
- **Volatility prediction** — estimated next-hour realized volatility (used for position sizing)
- **Regime classification** — whether the market is trending, ranging, or volatile (used for strategy selection)

This service runs as an **in-process library** inside the strategy engine. It is not a separate network service.

### Key Files

| File | What it does |
|---|---|
| `service.py` | Entry point — loads models, exposes prediction API |
| `features/feature_pipeline.py` | Computes ML features from raw tick data |
| `models/model_registry.py` | Loads model artifacts from S3 |
| `inference/predictor.py` | Runs inference using loaded models |

### Model Registry Structure in S3

```
s3://quantembrace-model-artifacts/
  models/
    volatility_predictor/
      v1.0.0/
        model.onnx           ← ONNX format, framework-agnostic
        metadata.json        ← training date, metrics, feature names, version
      v1.1.0/
        model.onnx
        metadata.json
    regime_classifier/
      v1.0.0/
        model.pickle         ← Scikit-learn model
        metadata.json
  features/
    2026-04-23/
      nse_features.parquet   ← Pre-computed features for training
      us_features.parquet
```

### Adding a New Model

1. Train your model offline (locally or SageMaker)
2. Export to ONNX (preferred) or pickle
3. Write a `metadata.json`:
   ```json
   {
       "model_name": "your_model",
       "version": "v1.0.0",
       "trained_at": "2026-04-24",
       "features": ["feature_1", "feature_2"],
       "metrics": {"sharpe": 1.4, "accuracy": 0.68},
       "description": "What this model predicts and why"
   }
   ```
4. Upload to `s3://quantembrace-model-artifacts/models/your_model/v1.0.0/`
5. Add inference code to `predictor.py`
6. The model registry auto-discovers new versions on next startup

### Feature Pipeline

Features are computed nightly from S3 historical data. The batch ECS task (`ai-engine-batch`) runs at 01:00 UTC:

```
Input:  S3 historical ticks (previous 30 days)
Output: Feature Parquet files per market

Features computed:
  - Rolling volatility (5m, 1h, 1d windows)
  - VWAP deviation
  - Volume profile
  - Price momentum (1h, 4h, 1d)
  - Bid-ask spread statistics
  - Market correlation matrix
```

---

## 6. shared — Shared Libraries

### Purpose

Common utilities used by all services. Not a separate running service — it's a Python package imported by every service.

### Key Files

| File | What it does |
|---|---|
| `config/settings.py` | `AppSettings` Pydantic model — loads and validates all env vars |
| `logging/logger.py` | Structured JSON logger factory with correlation ID support |
| `utils/helpers.py` | `utc_now()`, `utc_iso()`, and other utility functions |

### Structured Logging

All services use the same logger, which outputs JSON to stdout (captured by CloudWatch):

```python
from shared.logging.logger import get_logger, set_correlation_id

logger = get_logger(__name__, service_name="risk_engine")
set_correlation_id()  # Generates a UUID correlation ID for this request

logger.info("Signal validated", signal_id="f47ac10b...", status="APPROVED")
```

Output:
```json
{
    "timestamp": "2026-04-24T03:46:04.123Z",
    "service": "risk_engine",
    "level": "INFO",
    "event": "Signal validated",
    "signal_id": "f47ac10b...",
    "status": "APPROVED",
    "correlation_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
}
```

The `correlation_id` lets you trace a single request (tick → signal → risk decision → order) across all service logs in CloudWatch Insights:
```
fields @timestamp, service, event, signal_id
| filter correlation_id = "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
| sort @timestamp asc
```

### Settings Validation

`shared/config/settings.py` uses Pydantic to validate all configuration at startup. If a required environment variable is missing or has an invalid format, the service fails immediately with a clear error — not silently at runtime:

```python
class AppSettings(BaseSettings):
    kite_api_key: str                        # Required, will fail if missing
    alpaca_api_key: str                      # Required
    max_daily_loss_pct: float = 3.0         # Optional, has default
    environment: Literal["dev", "staging", "prod"] = "dev"  # Must be one of these
```

---

## Service Dependency Map

```
                     ┌────────────────┐
                     │  data_ingestion │
                     │  (NSE + US)     │
                     └───────┬────────┘
                             │ SQS: market-data queue
                             ▼
                     ┌────────────────┐    ┌──────────┐
                     │ strategy_engine │◄───│ ai_engine│ (in-process)
                     └───────┬────────┘    └──────────┘
                             │ SQS: signals.fifo queue
                             ▼
                     ┌────────────────┐
                     │  risk_engine   │ ◄── DynamoDB (risk-state, positions)
                     └───────┬────────┘     S3 (audit logs)
                             │ SQS: orders.fifo queue
                             ▼
                     ┌────────────────┐
                     │execution_engine│ ◄── DynamoDB (orders table)
                     └───────┬────────┘
                         ┌───┴────┐
                         ▼        ▼
                      Zerodha   Alpaca
                       (NSE)     (US)

All services:
  ← AWS Secrets Manager (API keys)
  ← DynamoDB (shared state, each service owns specific tables)
  → CloudWatch Logs (all service logs)
  → S3 (various — logs, artifacts, historical data)
```

---

## Environment Variables Reference

Complete list of all required and optional environment variables across all services:

| Variable | Service | Required? | Description |
|---|---|---|---|
| `KITE_API_KEY` | data_ingestion, execution_engine | Yes | Zerodha API key |
| `KITE_API_SECRET` | data_ingestion, execution_engine | Yes | Zerodha API secret |
| `KITE_ACCESS_TOKEN` | data_ingestion, execution_engine | Runtime | Generated daily at login |
| `ALPACA_API_KEY` | data_ingestion, execution_engine | Yes | Alpaca key ID |
| `ALPACA_API_SECRET` | data_ingestion, execution_engine | Yes | Alpaca secret key |
| `ALPACA_BASE_URL` | data_ingestion, execution_engine | Yes | `paper-api` or `api.alpaca.markets` |
| `AWS_REGION` | All | Yes | e.g. `ap-south-1` |
| `DYNAMODB_TABLE_PREFIX` | All | Yes | e.g. `qe-prod` |
| `S3_BUCKET_DATA` | data_ingestion, ai_engine | Yes | Market data bucket name |
| `S3_BUCKET_LOGS` | risk_engine, execution_engine | Yes | Audit logs bucket name |
| `SQS_MARKET_DATA_QUEUE_URL` | data_ingestion, strategy_engine | Yes | SQS queue URL |
| `SQS_SIGNALS_QUEUE_URL` | strategy_engine, risk_engine | Yes | SQS FIFO queue URL |
| `SQS_ORDERS_QUEUE_URL` | risk_engine, execution_engine | Yes | SQS FIFO queue URL |
| `PORTFOLIO_VALUE` | risk_engine | Yes | Total portfolio value in base currency |
| `MAX_POSITION_SIZE_PCT` | risk_engine | No | Default: `5.0` |
| `MAX_TOTAL_EXPOSURE_PCT` | risk_engine | No | Default: `80.0` |
| `MAX_DAILY_LOSS_PCT` | risk_engine | No | Default: `3.0` |
| `ENVIRONMENT` | All | Yes | `dev`, `staging`, or `prod` |
| `LOG_LEVEL` | All | No | `DEBUG`, `INFO`, `WARNING`. Default: `INFO` |

---

*Last updated: 2026-04-24 | Update this document whenever: a new service is added, a service is split or merged, a new configuration variable is introduced, or broker integrations change.*
