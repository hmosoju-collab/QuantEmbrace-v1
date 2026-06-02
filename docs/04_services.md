# QuantEmbrace — Services Reference

> **For how services interact,** read [02_architecture.md](02_architecture.md).  
> **For the step-by-step trade flow,** read [03_signal_lifecycle.md](03_signal_lifecycle.md).  
> This document is the per-service reference for debugging, configuration, and understanding key files.

Each section covers:
- What the service does and why it exists
- Key files to look at when debugging
- Kafka connections (topics consumed and produced)
- Configuration (environment variables)
- Common failure modes and how to diagnose them

---

## Table of Contents

1. [data_ingestion — Market Data](#1-data_ingestion--market-data)
2. [strategy_engine — Signal Generation](#2-strategy_engine--signal-generation)
3. [ai_engine — ML Enrichment](#3-ai_engine--ml-enrichment)
4. [risk_engine — Risk Validation](#4-risk_engine--risk-validation)
5. [execution_engine — Order Placement](#5-execution_engine--order-placement)
6. [shared — Shared Libraries](#6-shared--shared-libraries)
7. [Service Dependency Map](#service-dependency-map)
8. [Environment Variables Reference](#environment-variables-reference)

---

## 1. data_ingestion — Market Data

### Purpose

Connects to broker WebSocket feeds, normalises all tick data into a unified `MarketTick` format, and publishes to Kafka. Also maintains a DynamoDB hot-cache of latest prices and archives all ticks to S3.

This is the entry point for all market data. Nothing downstream ever calls a broker WebSocket directly.

### Health Check

`GET http://localhost:8081/health` (local) or `http://<ec2-private-ip>:8081/health` (AWS)

### Kafka Connections

| Direction | Topic | Key | Description |
|---|---|---|---|
| Produces → | `ticks.nse` | instrument symbol (e.g. `RELIANCE`) | Normalised NSE tick |
| Produces → | `ticks.us` | instrument symbol (e.g. `AAPL`) | Normalised US tick |

### Key Files

| File | What it does |
|---|---|
| `service.py` | Main entry point — WebSocket connections, tick pipeline, health server |
| `connectors/zerodha_connector.py` | Zerodha Kite Ticker WebSocket handler. Subscribes to instruments, handles reconnect |
| `connectors/alpaca_connector.py` | Alpaca streaming WebSocket handler |
| `processors/tick_processor.py` | Normalises broker-specific formats into `MarketTick` |
| `storage/dynamo_writer.py` | Writes latest price to DynamoDB `latest-prices` table |
| `storage/s3_writer.py` | Buffers ticks and writes Parquet files to S3 in batches |
| `features/feature_pipeline.py` | Computes features (VWAP, candles, etc.) for downstream use |

### Instrument Configuration

The system subscribes to exactly the instruments marked `active: true` in `configs/instruments.yaml`. You never hardcode symbols in code.

```yaml
# configs/instruments.yaml
nse:
  instruments:
    - symbol: RELIANCE
      active: true     # ← subscribed, ticks published to Kafka
    - symbol: WIPRO
      active: false    # ← ignored completely
```

To add or remove instruments: edit `instruments.yaml`, restart `data_ingestion`.

### Zerodha Token Refresh (Daily Requirement)

Kite Connect access tokens expire **every day at approximately 07:30 IST**. Before market open each morning:

```bash
make zerodha-login
# or:
python scripts/zerodha_login.py
```

This opens a browser login flow, exchanges the authorization code, and stores the fresh token in DynamoDB (local) or AWS Secrets Manager (production).

### Common Failure Modes

| Symptom | Likely Cause | How to Diagnose |
|---|---|---|
| No ticks on Kafka | WebSocket disconnected | `make logs-data` — look for `websocket_disconnected` |
| `AUTH_INVALID` errors | Kite token expired | Run `make zerodha-login` |
| NSE ticks present, US ticks absent | Alpaca WebSocket issue | Check US market hours; `make logs-data` for Alpaca errors |
| Stale DynamoDB latest-prices | DynamoDB write errors | `make logs-data` for `dynamodb_write_error` |

---

## 2. strategy_engine — Signal Generation

### Purpose

Consumes tick data from Kafka, runs it through 6 independent trading strategies, and publishes generated signals for AI enrichment. This service owns signal generation and nothing else — it never places orders, checks risk limits, or queries broker APIs.

### Health Check

`GET http://localhost:8082/health`

### Kafka Connections

| Direction | Topic | Key | Consumer Group | Description |
|---|---|---|---|---|
| Consumes ← | `ticks.nse` | — | `strategy-v1` | NSE market data |
| Consumes ← | `ticks.us` | — | `strategy-v1` | US market data |
| Produces → | `signals.pending` | instrument | — | Raw trading signals |

### The 6 Strategies

| Strategy | Type | Signal Logic | Symbols |
|---|---|---|---|
| `MomentumStrategy` | Tick-based | BUY on golden cross (short MA > long MA), SELL on death cross | NSE + US |
| `ORB` (Opening Range Breakout) | Candle-based | BUY when price breaks above the first 15-minute candle range | NSE |
| `Scalp1m` | Candle-based | 1-minute momentum on volume confirmation | NSE + US |
| `VWAPReversionStrategy` | Candle-based | Mean reversion to VWAP — BUY when price dips below VWAP | NSE + US |
| `IntradayTrend15m` | Candle-based | 15-minute trend following with ADX confirmation | NSE + US |
| `PreCloseMomentum` | Candle-based | Momentum burst in the last 30 minutes of NSE session | NSE |

All strategies start with `paper_trade=True` in DynamoDB `strategy-config`. An operator manually sets `paper_trade=False` to promote a strategy to live capital after 5 READY paper days.

### What a Signal Looks Like

```python
Signal(
    signal_id="f47ac10b-...",              # UUID — never reused
    strategy_name="nse_momentum_v1",
    market="NSE",
    instrument="NSE:RELIANCE",
    direction=Direction.BUY,
    quantity=20,
    order_type=OrderType.MARKET,
    stop_price=Decimal("2420.00"),         # Stop-loss price — REQUIRED
    confidence=0.75,                       # 0.0–1.0
    paper_trade=True,                      # False only after operator promotes to live
    metadata={"reason": "golden_cross"},
    created_at=datetime(2026, 4, 24, tzinfo=UTC),
)
```

### Adding a New Strategy

See [07_contributing.md — How to Add a New Strategy](07_contributing.md#how-to-add-a-new-strategy) for the step-by-step process.

### Key Files

| File | What it does |
|---|---|
| `service.py` | Main event loop — loads universe, registers strategies, Kafka consumer + publisher |
| `strategies/base_strategy.py` | Abstract interface all strategies must implement |
| `strategies/momentum_strategy.py` | Tick-based momentum strategy |
| `strategies/orb_strategy.py` | Opening range breakout |
| `signals/signal.py` | `Signal` Pydantic model |
| `registry.py` | Strategy registry — maps strategies to instruments |

### Signal Timestamp Behavior

Candle-based strategies set `Signal.generated_at = candle.candle_close_time`. The candle for minute T is not available until T+5s. After DynamoDB poll and ai_engine enrichment, the signal arrives at the risk engine 7-12s after candle close. `RISK_MAX_SIGNAL_AGE_SECONDS` must be ≥ 30s. If you see 100% rejection on `signal_age` in the session report, this is the root cause.

### Common Failure Modes

| Symptom | Likely Cause | How to Diagnose |
|---|---|---|
| No signals on `signals.pending` | No ticks being received | Check `data_ingestion` health, check Kafka consumer lag on `strategy-v1` |
| "Not enough data" at startup | Moving average windows warming up | Normal — takes ~50 ticks per symbol. Wait or replay historical ticks. |
| Signals generated but no approvals | Risk engine rejecting everything | Check `make kill-switch-status`, check S3 risk-audit logs |
| 100% `signal_age` rejections | Candle signal age exceeds limit | Ensure `RISK_MAX_SIGNAL_AGE_SECONDS=30` in docker-compose risk_engine block |
| Strategy crash on one symbol | Bad tick data for that symbol | `make logs-strategy` — look for unhandled exceptions with the symbol name |

---

## 3. ai_engine — ML Enrichment

### Purpose

Consumes pending signals, enriches them with machine-learning predictions (market regime, signal quality score, volatility estimate), and republishes enriched signals for the risk engine. Runs as a standalone Kafka service — not embedded in any other service.

### Health Check

`GET http://localhost:8085/health`

### Kafka Connections

| Direction | Topic | Key | Consumer Group | Description |
|---|---|---|---|---|
| Consumes ← | `signals.pending` | — | `aiengine-v1` | Raw signals from strategy engine |
| Produces → | `signals.enriched` | instrument | — | ML-enriched signals |
| Produces → | `signals.enriched.retry` | — | — | Failed enrichments for retry |

### What Enrichment Adds

```python
# Before enrichment (signals.pending):
Signal(instrument="NSE:RELIANCE", direction=BUY, confidence=0.75, ...)

# After enrichment (signals.enriched):
Signal(
    ...,                                    # All original fields unchanged
    quality_score=0.82,                     # How reliable is this signal right now?
    market_regime="trending",               # "trending" | "ranging" | "volatile"
    volatility_estimate=0.018,              # Expected next-hour volatility (annualised fraction)
    enrichment_version="v1.2",
    enriched_at=datetime(2026, 4, 24, tzinfo=UTC),
)
```

The `quality_score` is the most important field. The risk engine's `QualityScoreValidator` rejects any signal with `quality_score < min_quality_score` (default: 0.30 from `configs/risk_limits_production.yaml`).

### ML Models

Models are loaded from S3 on startup and kept in memory:

```
s3://quantembrace-{env}-data/models/
  volatility_predictor/v1.0.0/model.pkl
  regime_classifier/v1.0.0/model.pkl
  quality_scorer/v1.0.0/model.pkl
```

Models are trained offline and uploaded to S3. The service auto-loads the latest version on startup. No model serving infrastructure required.

### Key Files

| File | What it does |
|---|---|
| `service.py` | Kafka consumer loop — one enrichment cycle per message |
| `inference/predictor.py` | Runs all three models — returns regime, quality_score, volatility |
| `features/feature_pipeline.py` | Computes features from candle cache and tick history |
| `models/model_registry.py` | Loads model files from S3 |

### Common Failure Modes

| Symptom | Likely Cause | How to Diagnose |
|---|---|---|
| `EnrichmentWatchdog` fires fallback | ai_engine Kafka consumer lagging | `make logs-ai`, check `aiengine-v1` consumer group lag |
| Quality scores all near 0 | Model degradation or bad features | Check S3 for recent model artifacts, `make logs-ai` |
| High enrichment latency | Model inference too slow | Check EC2 instance CPU. Consider c6g instance type for ai_engine. |
| All signals going to retry | Feature pipeline failing | `make logs-ai` for Python exceptions in feature computation |

---

## 4. risk_engine — Risk Validation

### Purpose

The mandatory gatekeeper between AI enrichment and order execution. Validates every enriched signal through 11 validators in sequence. If any validator rejects the signal, the signal dies here. No approved order ever reaches the execution engine without a `risk_decision_id`.

**If the risk engine is down, trading halts. This is by design.**

### Health Check

`GET http://localhost:8083/health`

### Kafka Connections

| Direction | Topic | Key | Consumer Group | Description |
|---|---|---|---|---|
| Consumes ← | `signals.enriched` | — | `risk-v1` | Normal path — enriched signals |
| Consumes ← | `signals.pending` | — | `risk-v1-fallback` | Fallback path — if ai_engine lags |
| Consumes ← | `orders.events` | — | `risk-v1-order-events` | Fill events for P&L tracking |
| Consumes ← | `risk.kill-switch` | — | `risk-v1-kill-switch` | Kill switch events |
| Produces → | `signals.approved` | instrument | — | Risk-approved signals |
| Produces → | `risk.kill-switch` | — | — | Kill switch activate/clear |
| Produces → | `signals.enriched.retry` | — | — | Transient-failed signals |
| Produces → | `signals.enriched.dlq` | — | — | Permanently-failed signals |

### The 11 Validators (in execution order)

| # | Validator | What it checks | Limit |
|---|---|---|---|
| 1 | `SignalAgeValidator` | Signal freshness | < 30 seconds old |
| 2 | `KillSwitchValidator` | Kill switch state | Must be OFF |
| 3 | `PositionValidator` | Open position count | Max 8 concurrent positions |
| 4 | `ExposureValidator` | Total portfolio exposure | Max 50% deployed |
| 5 | `DailyLossValidator` | Daily P&L drawdown | Max −2% (auto kill switch at −3%) |
| 6 | `MarginValidator` | Broker margin available | Must have sufficient margin |
| 7 | `SlippageValidator` | Market move since signal | Max 0.15% from signal price |
| 8 | `SpreadGateValidator` | Bid-ask spread | Max 50 bps |
| 9 | `SectorConcentrationValidator` | Single GICS sector exposure | Max 20% of portfolio |
| 10 | `LiquidityValidator` | Order size vs average daily volume | Max 1% of ADV |
| 11 | `QualityScoreValidator` | AI enrichment quality_score | Min 0.30 (from risk_limits_production.yaml) |

All limits are configured in [configs/risk_limits_production.yaml](../configs/risk_limits_production.yaml). No code change required to adjust them.

### Risk Limits Configuration

```yaml
# configs/risk_limits_production.yaml
portfolio_value:          1_000_000   # ₹10 lakh in rupees
max_daily_loss_pct:       2.0         # 2% of portfolio = ₹20,000 max daily loss
kill_switch_daily_loss_pct: 3.0       # 3% triggers automatic kill switch
max_position_size_pct:    5.0         # 5% of portfolio per position
max_total_exposure_pct:   50.0        # max 50% of portfolio deployed
max_single_order_value:   5000        # hard ₹5,000 ceiling on any single order
max_open_orders:          10
max_concurrent_positions: 8
min_quality_score:        0.3
max_signal_age_seconds:   30
max_slippage_pct:         0.15
max_spread_bps:           50.0
max_sector_exposure_pct:  20.0
```

### Kill Switch — Manual Operation

```bash
# Check current state
make kill-switch-status

# Activate (halt all trading immediately)
make kill-switch-on
# You will be prompted for a reason.

# Deactivate (resume trading)
make kill-switch-off
# You will be asked to type "YES" to confirm.
```

### Key Files

| File | What it does |
|---|---|
| `service.py` | Main service — Kafka consumer, validation pipeline, audit writer |
| `validators/` | 11 validator classes, one per file |
| `killswitch/` | Kill switch state management (DynamoDB-backed) |
| `consumers/enrichment_watchdog.py` | Monitors ai_engine consumer lag, switches fallback path |
| `consumers/kafka_kill_switch_listener.py` | Consumes `risk.kill-switch` topic |

### Common Failure Modes

| Symptom | Likely Cause | How to Diagnose |
|---|---|---|
| All signals rejected, `kill_switch_active` | Kill switch is ON | `make kill-switch-status` |
| All signals rejected, `daily_loss_limit` | Daily loss exceeded | Check DynamoDB `risk-state` for `daily_pnl` |
| Risk engine not starting | Kafka connectivity issue | `make logs-risk` for MSK connection errors |
| High rejection rate, `position_limit_exceeded` | Too many open positions | Scan DynamoDB `positions` table |
| Fallback mode active | ai_engine lagging | `make logs-ai`, check `aiengine-v1` Kafka lag |

---

## 5. execution_engine — Order Placement

### Purpose

Receives risk-approved signals and translates them into broker orders. Manages routing (Zerodha for NSE, Alpaca for US), idempotency, retry, and order lifecycle tracking in DynamoDB. Handles paper trading simulation when `paper_trade=True`.

**Critical:** Any signal without a `risk_decision_id` is rejected immediately. This enforces the risk engine requirement at the execution layer as a hard check.

### Health Check

`GET http://localhost:8084/health`

### Kafka Connections

| Direction | Topic | Key | Consumer Group | Description |
|---|---|---|---|---|
| Consumes ← | `signals.approved` | — | `execution-v1` | Risk-approved signals |
| Consumes ← | `risk.kill-switch` | — | `execution-v1-kill-switch` | Kill switch events |
| Produces → | `orders.events` | order_id | — | Fill events, order state changes |

### Trading Universe Enforcement

Before placing any order (paper or live), the execution engine validates the symbol against the day's approved universe snapshot. Controlled by `UNIVERSE_MODE` env var:

| Mode | Symbols Approved | When To Use |
|---|---|---|
| `PAPER_SAFE_START` | NIFTY 50 only | Default — first 5 paper sessions |
| `PAPER_EXPAND` | NIFTY 100 + F&O eligible | After PAPER_SAFE_START gates pass |
| `LIVE_ADVANCED` | Full NSE universe | Live mode only |

The daily snapshot is built at service start and rebuilt at midnight IST by `_universe_snapshot_refresh_loop` (background task 12). Paper mode allows stale snapshots; live mode blocks all orders if no valid snapshot exists for today.

### Paper Trading Simulator

When `paper_trade=True`, the paper simulator runs instead of the broker API:

```python
# Paper fill is deterministic — same signal always produces the same outcome
seed = int(hashlib.sha256(signal.signal_id.encode()).hexdigest()[:8], 16)
rng  = Random(seed)

fill_price   = signal_price * (1 + slippage_bps / 10000 * rng.choice([-1, 1]))
latency_ms   = EXECUTION_PAPER_LATENCY_MS  # 250ms default
```

The simulator:
- Writes the same DynamoDB order records as a real fill
- Publishes the same `orders.events` Kafka events
- Never calls Zerodha or Alpaca
- Is fully reproducible (same signal = same fill price)

> **Duplicate suppression:** `submit_order` uses a DynamoDB conditional write (`attribute_not_exists(PK)`). If two consumers race on the same signal, the second caller gets `False`. The handler checks the return value and returns immediately — `record_order` and `apply_fill_to_position` are never called on the losing race. This prevents double-counting positions under concurrent Kafka redelivery.

### The Broker Abstraction

Both brokers implement the same `BrokerClient` protocol:

```python
class BrokerClient(Protocol):
    async def place_order(self, order: OrderRequest) -> OrderResponse: ...
    async def cancel_order(self, order_id: str) -> CancelResponse: ...
    async def get_positions(self) -> list[Position]: ...
    async def get_order_status(self, order_id: str) -> OrderStatus: ...
```

Adding a new broker means implementing this protocol. No other code changes needed.

### DynamoDB Orders Table Schema

```
Table: quantembrace-{env}-orders
Partition key: order_id
GSI: signal_id-index (used for idempotency lookups by signal_id)

Fields:
  order_id          UUID
  signal_id         UUID (from strategy — never changes)
  risk_decision_id  UUID (from risk engine — proves risk approval)
  broker_order_id   str  (Zerodha's or Alpaca's ID)
  instrument        str  "NSE:RELIANCE" or "US:AAPL"
  market            str  "NSE" or "US"
  side              str  "BUY" or "SELL"
  quantity          int
  status            str  PENDING | PLACED | FILLED | REJECTED | FAILED | CANCELLED | PARTIALLY_FILLED
  average_price     Decimal
  filled_quantity   int
  paper_trade       bool
  created_at        ISO 8601 UTC
  placed_at         ISO 8601 UTC
  filled_at         ISO 8601 UTC
  ttl               int  Unix timestamp (90 days from created_at — auto-expires)
```

### Key Files

| File | What it does |
|---|---|
| `service.py` | Main service — Kafka consumer, order routing, idempotency, reconciliation |
| `brokers/zerodha_broker.py` | Zerodha Kite Connect adapter |
| `brokers/alpaca_broker.py` | Alpaca Trading API adapter |
| `paper_simulator.py` | Deterministic paper fill simulation |
| `orders/order_manager.py` | DynamoDB order lifecycle management |
| `monitors/orphan_detector.py` | Detects orders stuck in PENDING/PLACED |
| `consumers/kafka_kill_switch_listener.py` | Stops new order placement when kill switch fires |

### Common Failure Modes

| Symptom | Likely Cause | How to Diagnose |
|---|---|---|
| Orders stuck in PENDING | Broker API down or auth expired | Check broker API status; run `make zerodha-login` if auth error |
| Duplicate orders in DynamoDB | Bug in idempotency check | Should never happen — search DynamoDB for duplicate signal_ids |
| High FAILED order rate | Broker rate limiting or margin insufficient | Check `make logs-execution` for 429 or margin errors |
| `missing risk_decision_id` error | Signal bypassed risk engine | Critical defect — escalate immediately |
| Paper orders not appearing | `paper_trade=False` set accidentally | Check DynamoDB `strategy-config` for the strategy |
| Symbol rejected `universe_not_approved` | Symbol not in today's approved snapshot | Check `UNIVERSE_MODE`; verify snapshot was built for today — restart service if midnight rollover missed |
| Paper fills not recording (no log) | `submit_order` returning False | Two consumers raced — one won the conditional write; this is correct behavior, not a bug |

---

## 6. shared — Shared Libraries

### Purpose

Common Python packages imported by all 5 services. Not a running service — compiled into each service's process at startup.

### Kafka Auth (Critical Shared Component)

```python
# services/shared/kafka/config.py

def get_kafka_auth_config(aws_region: str) -> dict[str, Any]:
    """
    Returns the correct Kafka security config for the current environment.

    Local dev (KAFKA_USE_IAM=false):
      → {"security.protocol": "PLAINTEXT"}   (Redpanda, no auth)

    AWS production (KAFKA_USE_IAM=true, default):
      → {"security.protocol": "SASL_SSL",
         "sasl.mechanism": "OAUTHBEARER",
         "oauth_cb": <MSK IAM token callback>}
    """
```

Every Kafka producer and consumer calls `get_kafka_auth_config(region)`. Never hardcode security settings.

### Key Files

| File | What it does |
|---|---|
| `config/settings.py` | `AppSettings` Pydantic model — loads and validates all env vars. Fails fast if required vars missing. |
| `kafka/config.py` | `get_kafka_auth_config()` — centralised Kafka security config |
| `kafka/failure_publisher.py` | Publishes failed messages to `.retry` or `.dlq` topics |
| `logging/logger.py` | Structured JSON logger factory with correlation ID support |
| `events/schemas.py` | Kafka event schemas — `EventType`, `validate_event()` |
| `utils/helpers.py` | `utc_now()`, `utc_iso()`, and other utility functions |

### Structured Logging

All services log JSON to stdout, captured by CloudWatch:

```python
from shared.logging.logger import get_logger
logger = get_logger(__name__, service_name="risk_engine")

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
    "correlation_id": "9b1deb4d-..."
}
```

Use the `correlation_id` to trace a signal across all services in CloudWatch Logs Insights:
```
fields @timestamp, service, event
| filter correlation_id = "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
| sort @timestamp asc
```

---

## Service Dependency Map

```
                     ┌──────────────────┐
                     │  data_ingestion   │
                     │  :8081            │
                     └──────────────────┘
                        │          │
               Kafka:   │          │
           ticks.nse    │          │   DynamoDB: latest-prices
           ticks.us     │          │   S3: tick archives
                        ▼
                     ┌──────────────────┐
                     │ strategy_engine  │
                     │  :8082            │
                     └──────────────────┘
                              │ Kafka: signals.pending
                              ▼
                     ┌──────────────────┐
                     │   ai_engine      │
                     │  :8085            │
                     └──────────────────┘
                              │ Kafka: signals.enriched
                              ▼
                     ┌──────────────────┐
                     │  risk_engine     │ ◄── DynamoDB: risk-state, positions
                     │  :8083           │ ──► S3: audit logs
                     └──────────────────┘
                              │ Kafka: signals.approved
                              ▼
                     ┌──────────────────┐
                     │execution_engine  │ ◄── DynamoDB: orders
                     │  :8084           │
                     └──────────────────┘
                         │         │
                         ▼         ▼
                    Zerodha API   Alpaca API
                      (NSE)         (US)

All services:
  ← DynamoDB (service-specific tables)
  → CloudWatch Logs (all service stdout)
  → S3 (audit logs, tick archives, model artifacts)
  (Production only) → AWS Secrets Manager (API keys)
```

---

## Environment Variables Reference

Complete list of all variables. Set these in `.env` for local dev.

| Variable | Required? | Services | Description |
|---|---|---|---|
| `QE_ENVIRONMENT` | Yes | All | `development` / `staging` / `production` |
| `KAFKA_BOOTSTRAP_SERVERS` | Yes | All | MSK endpoint (port 9098 in AWS) or `redpanda:9092` (local) |
| `KAFKA_USE_IAM` | No | All | `true` (MSK IAM, default) / `false` (Redpanda PLAINTEXT, local) |
| `AWS_REGION` | Yes | All | e.g. `ap-south-1` |
| `DYNAMODB_TABLE_PREFIX` | Yes | All | e.g. `quantembrace-development` |
| `AWS_ENDPOINT_URL` | Local only | All | `http://localhost:4566` for LocalStack |
| `RISK_PROFILE` | Yes | risk_engine | `paper` / `shadow` / `tiny-live` / `medium-live` |
| `RISK_DAILY_LOSS_HALT_PCT` | No | risk_engine | Default `3.0` — % at which kill switch auto-fires |
| `ZERODHA_API_KEY` | execution_engine | execution_engine | Zerodha Kite Connect API key |
| `ZERODHA_API_SECRET` | execution_engine | execution_engine | Zerodha secret |
| `ZERODHA_ACCESS_TOKEN` | Runtime (daily) | data_ingestion, execution | Updated daily by `make zerodha-login` |
| `ALPACA_API_KEY` | execution_engine | execution_engine | Alpaca key ID |
| `ALPACA_API_SECRET` | execution_engine | execution_engine | Alpaca secret |
| `ALPACA_BASE_URL` | Yes | execution_engine | `https://paper-api.alpaca.markets` or `https://api.alpaca.markets` |
| `ALPACA_USE_PAPER` | No | execution_engine | `true` (default) — paper trading at Alpaca |
| `EXECUTION_PAPER_SLIPPAGE_BPS` | No | execution_engine | Paper simulator slippage. Default `5.0` |
| `EXECUTION_PAPER_SPREAD_BPS` | No | execution_engine | Paper simulator bid-ask spread. Default `10.0` |
| `EXECUTION_PAPER_LATENCY_MS` | No | execution_engine | Paper simulator fill latency. Default `250` |
| `EXECUTION_PAPER_RANDOM_SEED` | No | execution_engine | Seed for deterministic fills. Default `quantembrace-paper-v1` |
| `UNIVERSE_MODE` | Yes | execution_engine | Trading universe gate: `PAPER_SAFE_START` (NIFTY 50), `PAPER_EXPAND`, `LIVE_ADVANCED`. Controls which symbols may receive orders. |
| `QE_EXECUTION_LIVE_TRADING_ENABLED` | No | execution_engine | Live broker gate. Must be absent or `false` for all paper sessions. Hard-blocked. |
| `RISK_MAX_SIGNAL_AGE_SECONDS` | No | risk_engine | Max signal age before rejection. **Default `30`. Do not set below 20.** Candle signals are 7-12s old at risk_engine (stamped at candle_close_time, not poll time). Root cause of Days 1-4 zero fills when default was 5s. |
| `STRATEGY_WATCHLIST_NSE` | Yes | data_ingestion | Comma-separated NSE symbols for LiveQuotePoller. Required for candle-based strategies (ORB, VWAP, intraday trend, pre-close). |
| `DATA_INGESTION_TICK_STALE_THRESHOLD` | No | data_ingestion | Seconds before a live tick is considered stale. Used for stale-LTP LIVE blocking (ADR-018). |
| `PAPER_SEED_NAV` | Yes (paper) | setup service | Starting NAV in rupees for paper sessions. Default `1000000` (₹10L). Seeded into DynamoDB by `docker-compose run --rm setup`. |
| `LOG_LEVEL` | No | All | `DEBUG` / `INFO` (default) / `WARNING` |
| `QE_HEALTH_CHECK_PORT` | No | All | HTTP health server port. Default per service. |

---

*Last updated: 2026-05-28 | Update this document when: a new service is added, Kafka topics or consumer groups change, or new environment variables are introduced.*
