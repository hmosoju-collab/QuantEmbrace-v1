# QuantEmbrace - System Design

## Overview

QuantEmbrace is a production-grade algorithmic trading platform that operates across two markets:
- **NSE India** via Zerodha Kite Connect
- **US Equities** via Alpaca Markets API

The system is built in Python, deployed on AWS (ECS Fargate), and follows a strict 6-layer
architecture where every trade must pass through a centralized Risk Layer before reaching
any broker.

---

## Architecture Principles

1. **Risk is non-negotiable.** Every order passes through the Risk Layer. No exceptions. No bypasses.
2. **Broker-agnostic execution.** The Execution Layer uses an adapter pattern. Adding a new broker means adding one adapter.
3. **Idempotent everything.** Every order submission, every state write, every recovery action is idempotent.
4. **Cost-conscious infra.** No Lambda for streaming workloads. No over-provisioned resources. Batch where possible.
5. **Fail safe, not fail open.** If a component fails, it halts trading -- it does not pass through unchecked orders.

---

## 6-Layer Architecture

```
+============================================================================+
|                                                                            |
|  LAYER 6: INFRASTRUCTURE LAYER                                            |
|  AWS ECS Fargate | S3 | DynamoDB | CloudWatch | Secrets Manager           |
|  Terraform-managed | Multi-AZ | Cost-optimized                            |
|                                                                            |
+============================================================================+
|                                                                            |
|  LAYER 5: AI/ML LAYER                                                     |
|  Feature Pipelines | Model Registry (S3) | Inference Service              |
|  Signal Enrichment | Lightweight, not over-engineered                      |
|                                                                            |
+============================================================================+
|                                                                            |
|  LAYER 4: RISK LAYER  <<<< CRITICAL -- SITS BETWEEN STRATEGY & EXEC >>>> |
|  Position Limits | Stop-Loss Enforcement | Exposure Checks                |
|  Kill Switch | Drawdown Monitoring | Per-Instrument Limits                |
|                                                                            |
+============================================================================+
|                                                                            |
|  LAYER 3: EXECUTION LAYER                                                 |
|  Broker Adapters (Zerodha + Alpaca) | Order Manager                       |
|  Retry Logic | Idempotent Submission | Fill Tracking                      |
|                                                                            |
+============================================================================+
|                                                                            |
|  LAYER 2: STRATEGY LAYER                                                  |
|  Signal Generation | Backtesting Framework | Pluggable Strategies         |
|  Multi-Market Support | Parameter Store                                   |
|                                                                            |
+============================================================================+
|                                                                            |
|  LAYER 1: DATA LAYER                                                      |
|  WebSocket Feeds (Kite Ticker + Alpaca) | S3 Historical Store             |
|  DynamoDB State (Latest Prices, Positions) | Data Normalization            |
|                                                                            |
+============================================================================+
```

---

## Layer 1: Data Layer

### Purpose
Ingest, normalize, and store all market data. This layer is the single source of truth for
what the market looks like right now and what it looked like historically.

### Components

#### 1.1 Real-Time Market Data Ingestion

**Zerodha Kite Ticker (NSE India)**
- WebSocket connection via `kiteconnect` Python SDK
- Subscribes to instruments in `full` mode (OHLC, LTP, depth, OI)
- Reconnection logic with exponential backoff (max 5 retries, then alert)
- Runs as a dedicated ECS Fargate task (`data-ingestion-nse`)

**Alpaca WebSocket (US Equities)**
- WebSocket connection via `alpaca-trade-api` Python SDK
- Subscribes to trades and quotes for configured symbols
- Separate ECS Fargate task (`data-ingestion-us`)

**Why two separate services:**
- Different market hours (NSE: 09:15-15:30 IST, US: 09:30-16:00 ET)
- Different reconnection behaviors and rate limits
- Independent failure domains -- one market going down should not affect the other

#### 1.2 Data Storage

**DynamoDB -- Latest State (Hot Path)**
- `latest-prices` table: keyed by `{market}#{instrument}`, stores LTP, bid/ask, volume, timestamp
- TTL set to 24 hours (stale prices auto-expire)
- On-demand capacity mode (pay per request, no idle cost)

**S3 -- Historical Data (Cold Path)**
- Bucket: `quantembrace-market-data-history`
- Partitioned by: `s3://{bucket}/{market}/{instrument}/{date}/{hour}/ticks.parquet`
- Parquet format for efficient columnar reads during backtesting
- Lifecycle policy: move to S3 Glacier after 90 days, delete after 3 years

#### 1.3 Data Normalization

All incoming data is normalized into a unified `MarketTick` schema before storage:

```python
@dataclass
class MarketTick:
    market: str          # "NSE" or "US"
    instrument: str      # "NSE:RELIANCE" or "US:AAPL"
    ltp: Decimal         # Last traded price
    bid: Decimal
    ask: Decimal
    volume: int
    timestamp: datetime  # Always UTC
    raw: dict            # Original broker payload for debugging
```

This normalization happens at the ingestion layer so downstream consumers never deal with
broker-specific formats.

---

## Layer 2: Strategy Layer

### Purpose
Generate trading signals from market data. Strategies are modular, pluggable, and
completely unaware of execution mechanics.

### Components

#### 2.1 Strategy Interface

Every strategy implements a common interface:

```python
class BaseStrategy(ABC):
    @abstractmethod
    def on_tick(self, tick: MarketTick) -> Optional[Signal]:
        """Process a market tick, optionally produce a signal."""
        pass

    @abstractmethod
    def on_bar(self, bar: OHLCV) -> Optional[Signal]:
        """Process a completed bar, optionally produce a signal."""
        pass

    @abstractmethod
    def get_parameters(self) -> dict:
        """Return current strategy parameters for logging/auditing."""
        pass
```

#### 2.2 Signal Format

```python
@dataclass
class Signal:
    signal_id: str              # UUID, generated at creation
    strategy_name: str          # e.g., "mean_reversion_v2"
    market: str                 # "NSE" or "US"
    instrument: str             # "NSE:RELIANCE" or "US:AAPL"
    direction: str              # "BUY" or "SELL"
    quantity: int
    order_type: str             # "MARKET", "LIMIT", "SL", "SL-M"
    limit_price: Optional[Decimal]
    stop_price: Optional[Decimal]
    confidence: float           # 0.0 - 1.0, from strategy/ML model
    metadata: dict              # Strategy-specific context
    created_at: datetime        # UTC
```

#### 2.3 Pluggable Strategies

Strategies are loaded from a configuration file at startup:

```yaml
strategies:
  - name: momentum_breakout
    module: quantembrace.strategies.momentum
    class: MomentumBreakout
    markets: [NSE, US]
    params:
      lookback_period: 20
      breakout_threshold: 0.02
      
  - name: mean_reversion_nse
    module: quantembrace.strategies.mean_reversion
    class: MeanReversion
    markets: [NSE]
    params:
      window: 50
      z_score_threshold: 2.0
```

#### 2.4 Backtesting Framework

- Reads historical data from S3 (Parquet files)
- Replays ticks/bars through the same strategy interface
- Produces performance reports: Sharpe, max drawdown, win rate, PnL curve
- Uses the exact same `Signal` format, so risk validation can also be backtested
- Runs as a batch ECS task (not always-on)

---

## Layer 3: Execution Layer

### Purpose
Translate approved orders into broker-specific API calls. Handle retries, failures, and
fill tracking.

### Components

#### 3.1 Broker Adapter Pattern

```python
class BrokerAdapter(ABC):
    @abstractmethod
    def place_order(self, order: ApprovedOrder) -> OrderResult:
        pass

    @abstractmethod
    def get_order_status(self, broker_order_id: str) -> OrderStatus:
        pass

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        pass

    @abstractmethod
    def get_positions(self) -> list[Position]:
        pass

    @abstractmethod
    def get_margins(self) -> MarginInfo:
        pass
```

**ZerodhaAdapter** -- wraps `kiteconnect.KiteConnect`
- Handles token refresh (Kite tokens expire daily)
- Maps internal order types to Kite-specific varieties (CNC, MIS, NRML)
- Respects Kite Connect rate limits (10 requests/second)

**AlpacaAdapter** -- wraps `alpaca-trade-api.REST`
- Paper trading support for testing
- Maps internal order types to Alpaca order classes
- Handles fractional shares where applicable

#### 3.2 Idempotent Order Submission

Every order has a unique `order_id` (UUID) generated at signal creation time.

Before placing an order:
1. Check DynamoDB `orders` table for existing entry with this `order_id`
2. If exists and status is `PLACED` or `FILLED`, skip (idempotent)
3. If exists and status is `FAILED`, retry with same `order_id`
4. If not exists, insert with status `PENDING`, then place with broker

This prevents duplicate orders during restarts or retries.

#### 3.3 Retry Logic

```
Attempt 1: Place order
  -> Success: update DynamoDB, done
  -> Transient failure (timeout, 5xx): wait 1s, retry
Attempt 2: Retry
  -> Success: update DynamoDB, done
  -> Transient failure: wait 2s, retry
Attempt 3: Retry
  -> Success: update DynamoDB, done
  -> Any failure: mark as FAILED, alert, do NOT retry further
```

Non-retryable errors (insufficient margin, invalid instrument, 4xx) fail immediately.

---

## Layer 4: Risk Layer (CRITICAL)

### Purpose
**This is the most important layer in the entire system.** It sits between the Strategy
Layer and the Execution Layer. No signal can become an order without passing through
every risk check. There are no backdoors, no overrides, no "just this once" flags.

### Architecture Position

```
Strategy Layer                Risk Layer                 Execution Layer
     |                            |                            |
     |--- Signal --------------->>|                            |
     |                            |-- validate()               |
     |                            |   - position_limit_check   |
     |                            |   - exposure_check         |
     |                            |   - stop_loss_check        |
     |                            |   - drawdown_check         |
     |                            |   - kill_switch_check      |
     |                            |   - instrument_limit_check |
     |                            |   - margin_check           |
     |                            |                            |
     |                            |-- IF ALL PASS:             |
     |                            |--- ApprovedOrder -------->>|
     |                            |                            |
     |                            |-- IF ANY FAIL:             |
     |<<-- RejectedSignal -------|                            |
     |    (with reason)           |                            |
```

### Risk Checks (Executed in Order)

#### 4.1 Kill Switch Check
- **First check, always.** If kill switch is active, reject ALL signals immediately.
- Kill switch state stored in DynamoDB (`risk-state` table, key `kill_switch`)
- Can be activated: manually (API call), automatically (drawdown threshold), or via CloudWatch alarm
- When activated: cancel all open orders across all brokers, reject all new signals, send alert

#### 4.2 Position Limit Check
- Maximum number of open positions per market (configurable)
- Maximum position size per instrument (absolute and as % of portfolio)
- Example: max 10 positions NSE, max 5 positions US, max 5% portfolio per instrument

#### 4.3 Exposure Check
- Total exposure across all positions must not exceed configured threshold
- Gross exposure (sum of |position_value|) capped at e.g., 200% of capital
- Net exposure (long - short) capped at e.g., 100% of capital

#### 4.4 Stop-Loss Enforcement
- Every signal MUST have an associated stop-loss (strategy provides it or risk layer computes default)
- Default stop-loss: 2% from entry for intraday, 5% for positional
- If a signal arrives without stop-loss and the strategy does not provide one, the risk layer adds one

#### 4.5 Drawdown Check
- Real-time PnL tracking per day and per week
- Daily max drawdown: configurable (e.g., 3% of capital)
- Weekly max drawdown: configurable (e.g., 7% of capital)
- If breached: activate kill switch, no manual override until next trading day

#### 4.6 Instrument-Level Limits
- Per-instrument maximum quantity
- Per-instrument maximum notional value
- Sector concentration limits (optional)

#### 4.7 Margin Check
- Query broker for available margin before approving order
- Maintain a buffer (e.g., always keep 20% margin available for stop-loss slippage)

### Risk State Storage

All risk state is in DynamoDB (`risk-state` table):

```
{
    "key": "daily_pnl_2026-04-23",
    "value": -12500.00,
    "currency": "INR",
    "market": "NSE",
    "updated_at": "2026-04-23T10:30:00Z"
}
```

This allows the risk engine to recover its state completely after a restart.

---

## Layer 5: AI/ML Layer

### Purpose
Provide signal enrichment and predictive features to strategies. This layer is intentionally
lightweight -- it augments human-designed strategies rather than replacing them.

### Design Philosophy

- **Not over-engineered.** Simple models that add value, not deep learning for the sake of it.
- **Offline training, online inference.** Models are trained in batch (nightly/weekly), inference runs in real-time.
- **Model artifacts on S3.** No SageMaker, no custom ML infra. Just pickle/ONNX files on S3.

### Components

#### 5.1 Feature Pipeline

Runs nightly as a batch ECS task:

1. Read historical ticks from S3 (Parquet)
2. Compute features: moving averages, volatility, volume profiles, correlation matrices
3. Store feature datasets in S3: `s3://quantembrace-model-artifacts/features/{date}/`

#### 5.2 Model Registry

Simple S3-based model registry:

```
s3://quantembrace-model-artifacts/
  models/
    volatility_predictor/
      v1.0.0/model.onnx
      v1.0.0/metadata.json    # training date, metrics, feature list
      v1.1.0/model.onnx
      v1.1.0/metadata.json
    regime_classifier/
      v1.0.0/model.pickle
      v1.0.0/metadata.json
```

#### 5.3 Inference Service

- Runs as part of the strategy engine ECS task (not a separate service)
- Loads latest model from S3 at startup
- Provides predictions via in-process function calls (no network hop)
- Models used:
  - **Volatility predictor**: estimates next-hour volatility for position sizing
  - **Regime classifier**: identifies market regime (trending/ranging/volatile) for strategy selection

---

## Layer 6: Infrastructure Layer

### Purpose
All compute, storage, networking, and observability. Fully managed by Terraform.

### Compute: ECS Fargate

Five long-running services:

| Service                  | CPU  | Memory | Min Tasks | Max Tasks | Notes                          |
|--------------------------|------|--------|-----------|-----------|--------------------------------|
| data-ingestion-nse       | 256  | 512MB  | 1         | 1         | Single instance, WebSocket     |
| data-ingestion-us        | 256  | 512MB  | 1         | 1         | Single instance, WebSocket     |
| strategy-engine          | 512  | 1GB    | 1         | 2         | Runs all active strategies     |
| risk-engine              | 256  | 512MB  | 1         | 1         | Single instance, consistency   |
| execution-engine         | 256  | 512MB  | 1         | 1         | Single instance per market     |

**Why not Lambda:**
- WebSocket connections need long-lived processes (data ingestion)
- Strategy engine needs in-memory state (loaded models, running calculations)
- Lambda cold starts are unacceptable for trading latency
- Lambda costs more than Fargate for always-on workloads

### Storage

**S3 Buckets:**
- `quantembrace-market-data-history` -- tick data in Parquet format
- `quantembrace-trading-logs` -- structured JSON logs from all services
- `quantembrace-model-artifacts` -- ML model files and feature datasets

**DynamoDB Tables:**
- `latest-prices` -- hot cache of current market prices (on-demand billing)
- `positions` -- current open positions across both markets
- `orders` -- order history with status tracking (idempotency)
- `strategy-state` -- strategy-specific state (e.g., indicator values, counters)
- `risk-state` -- risk engine state (PnL, limits, kill switch)

### Networking

- VPC with public and private subnets across 2 AZs
- ECS tasks run in private subnets
- NAT Gateway for outbound internet (broker API connections)
- No inbound internet access to any trading service

### Secrets

- AWS Secrets Manager for all API credentials:
  - Zerodha API key + secret + access token
  - Alpaca API key + secret
- Rotated via Terraform-managed rotation schedule
- Accessed by ECS tasks via IAM task roles (no hardcoded credentials)

### Monitoring

- CloudWatch Logs: all service logs aggregated
- CloudWatch Metrics: custom metrics for trade count, PnL, latency, error rates
- CloudWatch Alarms:
  - WebSocket disconnection for > 60 seconds
  - Order failure rate > 10% in 5 minutes
  - Daily PnL drawdown approaching threshold
  - ECS task crash/restart
- SNS topic for critical alerts -> email + SMS

### Cost Optimization

| Resource              | Estimated Monthly Cost | Notes                              |
|-----------------------|-----------------------|------------------------------------|
| ECS Fargate (5 tasks) | ~$45-65               | Small tasks, not 24/7 (market hrs) |
| DynamoDB (on-demand)  | ~$5-15                | Low volume, auto-scales to zero    |
| S3 (data + logs)      | ~$3-10                | Lifecycle policies reduce cost     |
| NAT Gateway           | ~$35-45               | Largest fixed cost, unavoidable    |
| CloudWatch            | ~$5-10                | Logs + metrics + alarms            |
| Secrets Manager       | ~$2-3                 | Per-secret pricing                 |
| **Total**             | **~$95-150/month**    | Minimal viable production setup    |

**Cost reduction strategies:**
- ECS tasks scheduled to run only during market hours (ECS scheduled scaling)
- NSE service: 09:00-16:00 IST (~7 hours/day, 5 days/week)
- US service: 09:00-17:00 ET (~8 hours/day, 5 days/week)
- NAT Gateway: consider NAT Instance (t4g.nano ~$3/month) if cost is critical
- DynamoDB on-demand: cheaper than provisioned for bursty trading workloads
- S3 Intelligent-Tiering for data that might not be accessed regularly

---

## Cross-Cutting Concerns

### Logging

All services use structured JSON logging:

```json
{
    "timestamp": "2026-04-23T10:30:00.123Z",
    "service": "risk-engine",
    "level": "WARNING",
    "event": "signal_rejected",
    "signal_id": "abc-123",
    "reason": "daily_drawdown_exceeded",
    "details": {
        "current_drawdown_pct": 3.2,
        "max_allowed_pct": 3.0
    }
}
```

### Configuration

- Strategy parameters: YAML files in S3, loaded at startup
- Risk parameters: DynamoDB `risk-config` items, changeable at runtime
- Infrastructure config: Terraform variables, immutable at runtime
- Feature flags: DynamoDB `feature-flags` table

### Security

- No credentials in code, environment variables, or config files
- All secrets in AWS Secrets Manager
- ECS tasks use IAM task roles with least-privilege policies
- VPC endpoints for S3 and DynamoDB (no internet traversal for AWS services)
- All broker API calls over HTTPS/WSS

### Deployment

- Terraform for all infrastructure
- Docker images built via CI/CD, pushed to ECR
- ECS rolling deployments with health checks
- Blue/green deployment support for zero-downtime updates
- Rollback: revert to previous ECS task definition revision

---

## Component Interaction Summary

```
                        +------------------+
                        |   CloudWatch     |
                        |  (Monitoring)    |
                        +--------+---------+
                                 |
                    monitors all layers
                                 |
+------------------+    +--------+---------+    +------------------+
|  Zerodha Kite    |    |                  |    |   Alpaca         |
|  WebSocket       +--->+  Data Ingestion  +<---+   WebSocket      |
|  (NSE Ticks)     |    |  Services        |    |  (US Ticks)      |
+------------------+    +--------+---------+    +------------------+
                                 |
                    writes normalized ticks
                                 |
                    +------------+------------+
                    |                         |
             +------+------+          +------+------+
             |  DynamoDB   |          |     S3      |
             | (latest     |          | (historical |
             |  prices)    |          |  ticks)     |
             +------+------+          +------+------+
                    |                         |
                    reads latest data         reads for backtesting/ML
                    |                         |
             +------+------+          +------+------+
             |  Strategy   |          |   AI/ML     |
             |  Engine     +<---------+   Layer     |
             |             |  enriches|             |
             +------+------+  signals +-------------+
                    |
                    | generates signals
                    |
             +------+------+
             |    RISK      |
             |   ENGINE     |   <<<< CRITICAL GATE
             | (validates   |
             |  ALL trades) |
             +------+------+
                    |
                    | approved orders only
                    |
             +------+------+
             |  Execution   |
             |  Engine      |
             +------+------+
                    |
          +---------+---------+
          |                   |
   +------+------+    +------+------+
   |  Zerodha    |    |   Alpaca    |
   |  Kite API   |    |   API       |
   |  (NSE)      |    |   (US)      |
   +-------------+    +-------------+
```
