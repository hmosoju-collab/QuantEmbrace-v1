# Design Document — QuantEmbrace End-State System (Phases 4–7)

## Overview

This document specifies the technical architecture for QuantEmbrace Phases 4 through 7. **Phase 5 is complete**: the Risk Engine with VaR/correlation/sector-cap checks is live, and the dual-layer Feature Store (online DynamoDB + offline S3 Parquet with streaming feature pipeline) is fully operational. **Phase 6 is active**: the Signal Enrichment Service, ONNX in-process inference, and read-only agentic layer are under active development.

The design covers four phases:
- **Phase 4**: Distributed Risk Engine + Portfolio Layer — ✅ COMPLETED (ADR-014: Redis/active-active deferred; in-memory `KillSwitchCache` with 1s DynamoDB poll delivered instead)
- **Phase 5**: Data Platform + Feature Store — ✅ COMPLETED (9 features: RSI-14, EMA-9/21, VWAP, ATR-14, ADX-14, MACD/signal/hist, volume_ratio; US feature store deferred to Phase 6)
- **Phase 6**: ML + Agentic Layer (Signal Enrichment Service, ONNX in-process inference, Regime Classifier + Volatility Forecaster + Signal Quality Scorer, A/B shadow mode, read-only agents) — **ACTIVE**
- **Phase 7**: Latency Optimization + Smart Order Router (SR-IOV, CPU pinning, NUMA, HTTP/2 persistent connections, TWAP/VWAP/Iceberg SOR, OpenTelemetry, Grafana latency dashboard) — PLANNED

All new components follow the 6-layer architectural invariants established in Phase 1. Every signal flows `strategy_engine → [signal_enrichment] → risk_engine → execution_engine` with no bypass. SQS is permanently banned (ruff TID251). Every order carries a `risk_decision_id`. The `trace_id` propagates unchanged from tick to fill. Every service raises `RuntimeError` on startup if `KAFKA_BOOTSTRAP_SERVERS` is not set.

---

## Architecture Diagram (End-State, Phase 7)

```
  Kite Ticker (NSE)   Alpaca WS (US)
        |                  |
        v                  v
  +------------------------------+
  |    Data Ingestion            |  Layer 1 — t4g.medium ASG x2
  |  KafkaTickPublisher          |
  +-------------+----------------+
                |  ticks.nse / ticks.us
                v
  +------------------------------+
  |    Strategy Engine           |  Layer 2 — c6g.large ASG 1-2
  |  StrategyRunner x6           |
  |  DynamoCandleConsumer        |
  +-------------+----------------+
                |  signals.pending
                v
  +------------------------------+
  |  Signal Enrichment Service   |  Layer 5 (Phase 6+) — c6g.large ASG 1
  |  ONNX: Regime + Vol + Score  |
  |  A/B Shadow Mode             |
  +-------------+----------------+
                |  signals.enriched
                v
  +------------------------------+     +---------------------------+
  |    Risk Engine (x2 AA)       |<--->|  Risk Analytics Service   |  Layer 4
  |  7 checks + VaR + Corr +     |     |  VaR + Correlation        |
  |  Sector + ADV + F&O          |     |  Scheduled background     |
  |  Redis hot path              |     |  EC2 Spot (off-hours)     |
  +-------------+----------------+     +---------------------------+
                |  signals.approved
                v
  +------------------------------+
  |    Execution Engine          |  Layer 3 — c6gn.large (Phase 7)
  |  Smart Order Router          |  SR-IOV + CPU pin + NUMA
  |  TWAP / VWAP / Iceberg       |  HTTP/2 persistent conns
  +--------+-------------+-------+
           |             |
           v             v
      Zerodha API    Alpaca API
      (NSE orders)   (US orders)
           |             |
           +------+------+
                  |  orders.events
                  v
           Risk Engine (P&L update)

  ElastiCache Redis (cluster mode, r6g.large)
    <--> Risk Engine x2 (shared state, pub/sub kill switch)

  Feature Store
    Online:  DynamoDB feature-store-online (TTL 24h)
    Offline: S3 Parquet {date}/{feature_group}/{instrument}
    <--> Strategy Engine (online reads)
    <--> Signal Enrichment (online reads for inference)
    <--> Backtest / Model Training (offline reads)

  OpenTelemetry (Phase 7)
    Every Kafka hop instrumented with spans
    trace_id from existing envelope = OTel trace context
    --> CloudWatch QuantEmbrace/Latency namespace
    --> Grafana latency dashboard (P50/P95/P99 per hop)

  kill.switch topic --> All services (kill-switch-listener task)
  ops.audit topic   <-- Risk Engine + Execution Engine + Signal Enrichment
```

---

## Kafka Topic Additions (Phases 4–7)

| Topic | Partitions | Key | Producer | Consumers | Phase |
|---|---|---|---|---|---|
| `signals.enriched` | 2 | instrument_id | signal_enrichment | risk-v1 | 6 |

All existing topics from Phase 3 are preserved unchanged. From Phase 6 onward, `signals.pending` is consumed by `enrichment-v1` (Signal Enrichment Service) instead of `risk-v1`. The Risk Engine's `risk-v1` consumer group is remapped to consume `signals.enriched`.

---

## New DynamoDB Tables (Phases 4–7)

| Table | Owner | Purpose | Phase |
|---|---|---|---|
| `risk-analytics` | risk_analytics (write), risk_engine (read) | VaR and correlation results, TTL 48h | 4 |
| `feature-store-online` | streaming_pipeline (write), strategy_engine / signal_enrichment (read) | Latest feature values per instrument, TTL 24h | 5 |
| `feature-registry` | streaming_pipeline (write), all services (read) | Feature name to computation version and dependencies | 5 |

---

## New AWS Resources (Phases 4–7)

| Resource | Type | Phase | Status | Purpose |
|---|---|---|---|---|
| ~~ElastiCache Redis r6g.large~~ | ~~cluster mode, 1 shard~~ | ~~4~~ | **Deferred (ADR-014)** | Replaced by in-memory KillSwitchCache |
| ~~Risk Analytics EC2~~ | ~~c6g.large Spot~~ | ~~4~~ | **Not needed** | RiskAnalyticsEngine runs in-process in risk_engine |
| Risk Engine EC2 | c6g.large ASG, single instance | 4 | ✅ DONE | Risk validation with VaR/sector/liquidity checks |
| Streaming Feature Pipeline | in-process in data_ingestion | 5 | ✅ DONE | Real-time feature computation on candle close |
| Signal Enrichment EC2 | c6g.large | 6 | 🔲 PLANNED | ONNX in-process ML inference |
| Execution Engine upgrade | c6gn.large | 7 | 🔲 PLANNED | Network-optimized Graviton for latency |

---

## Phase 4 Design: Distributed Risk Engine (COMPLETED)

### ADR-014 Implementation Note

The original Phase 4 spec called for ElastiCache Redis (r6g.large, cluster mode) and an active-active dual-instance Risk Engine across two Availability Zones. The actual implementation diverged as follows:

| Original Spec | Actual Implementation | Rationale |
|---|---|---|
| ElastiCache Redis r6g.large | In-memory `KillSwitchCache` | Eliminates per-signal DynamoDB reads without Redis cost or operational complexity |
| Active-active dual-instance Risk Engine | Single-instance Risk Engine | Sufficient for current signal throughput; HA deferred to Phase 7 if latency targets require it |
| Redis SETNX distributed lock for kill switch | DynamoDB conditional writes | Idempotency without Redis dependency |
| Redis pub/sub for kill switch propagation | Kafka `kill.switch` topic | Already in the stack; no additional infrastructure |

**Redis and active-active HA remain deferred to Phase 7.** If Phase 7 latency benchmarks show the Risk Engine hot path exceeds 0.5ms P99 without Redis, ElastiCache will be introduced at that point.

### Risk Engine Component Map

```
risk_engine/
├── validators/
│   ├── position_limits.py          # Max position size, daily loss limit
│   ├── spread_gate_validator.py    # Bid-ask spread gate
│   ├── exposure_validator.py       # Gross/net exposure caps
│   ├── margin_validator.py         # Available margin check
│   ├── var_validator.py            # 1-day 95%/99% VaR limit
│   ├── correlation_validator.py    # Pairwise correlation threshold
│   ├── sector_concentration_validator.py  # GICS sector cap
│   ├── liquidity_validator.py      # ADV 5% limit
│   └── fno_validator.py            # F&O delta/gamma limits
├── analytics/
│   └── risk_analytics_engine.py   # Background VaR + sector + NAV loop
├── cache/
│   └── kill_switch_cache.py       # In-memory cache, 1s DynamoDB poll
├── context/
│   └── risk_context_builder.py    # 7 parallel DynamoDB reads
└── registry/
    └── risk_decision_registry.py  # DynamoDB idempotency gate
```

### KillSwitchCache Design

```python
class KillSwitchCache:
    """In-memory kill switch state with 1-second DynamoDB poll.

    Replaces ElastiCache Redis (ADR-014). Achieves equivalent hot-path
    performance for kill switch reads without Redis infrastructure.
    """
    _state: dict[str, KillSwitchState]  # GLOBAL | MARKET | INSTRUMENT
    _last_poll: float                    # monotonic timestamp
    _poll_interval_s: float = 1.0

    async def is_halted(self, instrument_id: str, market: str) -> bool:
        """Returns True if any applicable kill switch is active."""
        await self._refresh_if_stale()
        return (
            self._state.get("GLOBAL") == KillSwitchState.ACTIVE
            or self._state.get(f"MARKET:{market}") == KillSwitchState.ACTIVE
            or self._state.get(f"INSTRUMENT:{instrument_id}") == KillSwitchState.ACTIVE
        )
```

### RiskAnalyticsEngine Design

Runs as a background `asyncio` loop within the `risk_engine` process. No separate EC2 instance.

```python
class RiskAnalyticsEngine:
    """Scheduled VaR + correlation + NAV computation.

    Runs every 5 minutes during market hours. Writes results to
    DynamoDB risk-analytics table and publishes to CloudWatch
    QuantEmbrace/RiskAnalytics namespace.
    """
    async def run_cycle(self) -> None:
        positions = await self._load_positions()
        returns = await self._load_returns(window_days=252)
        var_95 = self._compute_var(returns, confidence=0.95)
        var_99 = self._compute_var(returns, confidence=0.99)
        corr_matrix = self._compute_correlation(returns)
        sector_weights = self._compute_sector_weights(positions)
        await self._write_to_dynamodb(var_95, var_99, corr_matrix, sector_weights)
        await self._publish_to_cloudwatch(var_95, var_99)
```

---

## Phase 5 Design: Feature Store (COMPLETED)

### Feature Set

The delivered feature set is 9 features across a single unified feature group:

| Feature | Description | Staleness Threshold |
|---|---|---|
| `rsi_14` | RSI with 14-period lookback | 1m=5min, 5m=11min, 15m=31min |
| `ema_9` | Exponential Moving Average, 9 periods | Same as above |
| `ema_21` | Exponential Moving Average, 21 periods | Same as above |
| `vwap` | Volume-Weighted Average Price (intraday) | Same as above |
| `atr_14` | Average True Range, 14 periods | Same as above |
| `adx_14` | Average Directional Index, 14 periods | Same as above |
| `macd` | MACD line (EMA-12 minus EMA-26) | Same as above |
| `macd_signal` | MACD signal line (EMA-9 of MACD) | Same as above |
| `macd_hist` | MACD histogram (MACD minus signal) | Same as above |
| `volume_ratio` | Current volume / 20-period average volume | Same as above |

**Note:** US feature store was deferred to Phase 6 (no Alpaca candle stream in Phase 5). NSE features are live.

### DynamoDB Access Pattern

```
feature-store-online
  PK: {market}#{instrument}#{feature_group}
  SK: LATEST                    → TTL 24h  (online reads by Strategy Engine)
  SK: CANDLE#{iso_timestamp}    → TTL 7d   (intraday history, archived to S3 at POST_CLOSE)
```

### FeatureWriter Dual-Write

```python
async def write_features(
    self,
    market: str,
    instrument: str,
    feature_group: str,
    features: dict[str, float],
    candle_time: datetime,
) -> None:
    pk = f"{market}#{instrument}#{feature_group}"
    # Write 1: LATEST record (online read path)
    await self._dynamo.put_item(pk=pk, sk="LATEST", features=features, ttl_hours=24)
    # Write 2: CANDLE record (intraday history + archival)
    sk_candle = f"CANDLE#{candle_time.isoformat()}"
    await self._dynamo.put_item(pk=pk, sk=sk_candle, features=features, ttl_days=7)
```

### Staleness Detection

```python
STALENESS_THRESHOLDS: dict[str, timedelta] = {
    "1m":  timedelta(minutes=5),
    "5m":  timedelta(minutes=11),
    "15m": timedelta(minutes=31),
    "1h":  timedelta(hours=2),
    "1d":  timedelta(hours=26),
}

def is_stale(self, feature_time: datetime, interval: str) -> bool:
    threshold = STALENESS_THRESHOLDS[interval]
    return (datetime.utcnow() - feature_time) > threshold
```

---

## Phase 6 Design: ML + Agentic Layer (ACTIVE)

### Signal Enrichment Service — Architecture

The Signal Enrichment Service is a new microservice (Layer 5) that intercepts every signal between the Strategy Engine and the Risk Engine. It enriches signals with ML-derived metadata using ONNX Runtime in-process inference — no network hop to an external endpoint.

**Signal flow change from Phase 6 onward:**

```
Before Phase 6:  signals.pending  →  risk-v1 (Risk Engine)
After Phase 6:   signals.pending  →  enrichment-v1 (Signal Enrichment)
                                  →  signals.enriched  →  risk-v1 (Risk Engine)
```

**Service directory layout:**

```
services/signal_enrichment/
├── __init__.py
├── main.py                         # Entry point; raises RuntimeError if KAFKA_BOOTSTRAP_SERVERS unset
├── service.py                      # Main asyncio service loop
├── consumers/
│   └── kafka_signal_consumer.py   # Consumer group: enrichment-v1, topic: signals.pending
├── publishers/
│   └── kafka_enriched_publisher.py # Produces to: signals.enriched, key=instrument_id
├── models/
│   ├── model_loader.py             # ONNXModelLoader with hot-reload
│   ├── regime_classifier.py        # Regime: trending | ranging | volatile | crash
│   ├── volatility_forecaster.py    # Predicted next-hour realized volatility (float)
│   └── quality_scorer.py           # Signal confidence score 0.0–1.0
├── shadow/
│   └── shadow_runner.py            # A/B shadow mode framework
├── agents/
│   ├── strategy_selector.py        # Read-only Strategy Selector Agent
│   └── parameter_tuner.py          # Read-only Parameter Tuner Agent
└── logging/
    └── enrichment_logger.py        # Per-signal S3 audit log (Parquet)
```

### EnrichedSignal Data Model

```python
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field
from shared.models.signal import Signal

RegimeLabel = Literal["trending", "ranging", "volatile", "crash"]

class EnrichedSignal(BaseModel):
    """Signal envelope extended with ML enrichment metadata.

    schema_version is "4.0" for enriched signals. The base Signal
    fields are preserved unchanged to maintain trace_id propagation.
    """
    # --- Base signal fields (preserved from Signal) ---
    signal_id: str
    trace_id: str                          # Propagated unchanged from tick origin
    strategy_name: str
    instrument_id: str
    market: Literal["NSE", "US"]
    direction: Literal["BUY", "SELL"]
    quantity: int
    price: float
    signal_time: str                       # ISO8601 UTC
    paper_trade: bool
    risk_decision_id: str | None = None    # Set by Risk Engine after approval

    # --- Enrichment fields (Phase 6+) ---
    regime: RegimeLabel | None = None
    volatility_forecast: float | None = None   # Predicted next-hour realized vol
    quality_score: float | None = None          # 0.0–1.0 confidence
    model_version: str | None = None            # e.g. "regime-v1.2.0"
    enrichment_latency_ms: float | None = None  # Wall-clock enrichment time

    # --- Schema version ---
    schema_version: str = Field(default="4.0")

    def to_kafka_payload(self) -> dict:
        """Serialize to Kafka payload, omitting None fields."""
        return {k: v for k, v in self.model_dump().items() if v is not None}

    @classmethod
    def from_signal(cls, signal: Signal) -> "EnrichedSignal":
        """Construct an EnrichedSignal from a base Signal (pre-enrichment)."""
        return cls(**signal.model_dump(exclude={"schema_version"}))
```

### ONNX Model Pipeline

All three models run sequentially in-process within the Signal Enrichment Service. The total enrichment budget is 5ms.

```
Signal received from signals.pending
    │
    ▼  (~0.5ms)
FeatureReader.get_latest(instrument_id, market)
    │  reads from feature-store-online DynamoDB
    ▼  (~1.5ms)
RegimeClassifier.infer(feature_vector)
    │  ONNX Runtime, in-process
    │  output: "trending" | "ranging" | "volatile" | "crash"
    ▼  (~1.5ms)
VolatilityForecaster.infer(feature_vector)
    │  ONNX Runtime, in-process
    │  output: float (predicted next-hour realized vol)
    ▼  (~1.0ms)
SignalQualityScorer.infer(feature_vector + signal_metadata)
    │  ONNX Runtime, in-process
    │  output: float 0.0–1.0
    ▼
IF quality_score < MIN_QUALITY_THRESHOLD:
    → Drop signal
    → Log SIGNAL_FILTERED_LOW_CONFIDENCE to ops.audit
    → Log to S3 enrichment audit path
ELSE:
    → Attach regime, volatility_forecast, quality_score to EnrichedSignal
    → Publish to signals.enriched
    → Log to S3 enrichment audit path
```

### ONNXModelLoader with Hot-Reload

```python
import asyncio
import onnxruntime as ort
import boto3
from pathlib import Path

class ONNXModelLoader:
    """Loads ONNX models from S3 and hot-reloads on new version detection.

    S3 path convention: s3://quantembrace-model-artifacts/models/{name}/{version}/model.onnx
    Polls S3 every 60 seconds for a new version. Hot-swaps via asyncio.Lock
    so in-flight inference completes before the session is replaced.
    """
    def __init__(self, model_name: str, s3_bucket: str) -> None:
        self._model_name = model_name
        self._s3_bucket = s3_bucket
        self._session: ort.InferenceSession | None = None
        self._current_version: str | None = None
        self._lock = asyncio.Lock()
        self._s3 = boto3.client("s3")

    async def infer(self, input_array: "np.ndarray") -> "np.ndarray":
        async with self._lock:
            if self._session is None:
                raise RuntimeError(f"Model {self._model_name} not loaded")
            return self._session.run(None, {"input": input_array})[0]

    async def poll_for_updates(self) -> None:
        """Background task: poll S3 every 60s, hot-swap if new version found."""
        while True:
            await asyncio.sleep(60)
            latest = await self._get_latest_version()
            if latest != self._current_version:
                await self._load_version(latest)

    async def _load_version(self, version: str) -> None:
        local_path = await self._download_from_s3(version)
        new_session = ort.InferenceSession(str(local_path))
        async with self._lock:
            self._session = new_session
            self._current_version = version
```

### A/B Shadow Mode

Shadow mode runs a challenger model in parallel with the production model. The challenger's output is logged to S3 but never affects the live signal path. Errors in the challenger are caught and logged without propagating.

```python
class ShadowRunner:
    """Runs challenger model in parallel; logs both outputs to S3.

    Enabled via DynamoDB strategy-config flag: shadow_mode_enabled.
    Hot-reloaded by StrategyConfigLoader.
    """
    async def run(
        self,
        production_output: EnrichedSignal,
        feature_vector: "np.ndarray",
        challenger_loader: ONNXModelLoader,
    ) -> None:
        try:
            challenger_output = await challenger_loader.infer(feature_vector)
            await self._log_comparison(production_output, challenger_output)
        except Exception as exc:
            # Shadow errors MUST NOT affect the live signal path
            structlog.get_logger().warning("shadow_model_error", error=str(exc))
```

### Risk Engine Enrichment Integration

Two new validators are added to the Risk Engine validation chain in Phase 6:

**RegimeValidator** — reads `regime` from `EnrichedSignal`:

```python
class RegimeValidator:
    """Applies position size reduction for volatile/crash regimes.

    For volatile or crash regime: max_position_size *= 0.5
    For trending or ranging: no adjustment
    """
    REDUCTION_REGIMES: frozenset[str] = frozenset({"volatile", "crash"})

    def validate(self, signal: EnrichedSignal, context: RiskContext) -> RiskContext:
        if signal.regime in self.REDUCTION_REGIMES:
            context = context.with_max_position_size(
                int(context.max_position_size * 0.5)
            )
        return context
```

**VolatilitySizingValidator** — reads `volatility_forecast` from `EnrichedSignal`:

```python
class VolatilitySizingValidator:
    """Scales approved position size inversely to predicted volatility.

    Only applies when volatility_forecast > HIGH_VOLATILITY_THRESHOLD.
    approved_size = min(base_size, base_size * (threshold / volatility_forecast))
    """
    def validate(self, signal: EnrichedSignal, context: RiskContext) -> RiskContext:
        vol = signal.volatility_forecast
        if vol is not None and vol > self._threshold:
            scale = self._threshold / vol
            context = context.with_max_position_size(
                int(context.max_position_size * scale)
            )
        return context
```

### Read-Only Agents

Both agents run as background asyncio tasks within the Signal Enrichment Service process. They are strictly read-only during Phase 6 — they log recommendations to `ops.audit` but never write to `strategy-config` DynamoDB or call any state-mutating API.

**StrategySelectorAgent:**

```python
class StrategySelectorAgent:
    """Read-only agent: recommends strategy activations/deactivations.

    Reads: regime classification (from EnrichedSignal stream),
           current position state (DynamoDB positions table).
    Writes: ops.audit Kafka topic (recommendations only).
    NEVER calls: strategy_config_loader.set_enabled() or any DynamoDB write.
    """
    async def run_cycle(self, regime: RegimeLabel, positions: list[Position]) -> None:
        recommendations = self._compute_recommendations(regime, positions)
        for rec in recommendations:
            await self._audit_publisher.publish({
                "event_type": "STRATEGY_SELECTOR_RECOMMENDATION",
                "regime": regime,
                "recommendation": rec,
                "autonomous_action_taken": False,
            })
```

**ParameterTunerAgent:**

```python
class ParameterTunerAgent:
    """Read-only agent: suggests parameter adjustments based on Sharpe ratio.

    Reads: rolling 20-day Sharpe ratio per strategy (DynamoDB risk-analytics).
    Writes: ops.audit Kafka topic (suggestions only).
    NEVER calls: any DynamoDB write or strategy config mutation.
    """
    async def run_cycle(self) -> None:
        sharpe_by_strategy = await self._load_sharpe_ratios(window_days=20)
        for strategy, sharpe in sharpe_by_strategy.items():
            suggestion = self._compute_suggestion(strategy, sharpe)
            if suggestion:
                await self._audit_publisher.publish({
                    "event_type": "PARAMETER_TUNER_SUGGESTION",
                    "strategy": strategy,
                    "sharpe_20d": sharpe,
                    "suggestion": suggestion,
                    "autonomous_action_taken": False,
                })
```

### Enrichment Audit Logging

Every processed signal (whether published or dropped) produces an S3 audit record:

```
S3 path: s3://{S3_BUCKET_LOGS}/trading-logs/enrichment/{date}/
File format: Parquet, partitioned by date
Lifecycle: Transition to S3 Glacier Instant Retrieval after 30 days

Record schema:
  signal_id:            str
  trace_id:             str
  instrument_id:        str
  strategy_name:        str
  feature_values:       dict[str, float]   # 9 features used for inference
  model_version:        str
  regime:               str | None
  volatility_forecast:  float | None
  quality_score:        float | None
  enrichment_latency_ms: float
  action:               "PUBLISHED" | "DROPPED_LOW_QUALITY"
  enrichment_time:      str                # ISO8601 UTC
```

### Phase 6 Infrastructure

| Resource | Type | Purpose |
|---|---|---|
| Signal Enrichment EC2 | c6g.large ASG (min=1, max=1) | ONNX in-process inference |
| S3 model artifacts | `s3://quantembrace-model-artifacts/models/` | ONNX model storage |
| S3 enrichment logs | `s3://{S3_BUCKET_LOGS}/trading-logs/enrichment/` | Per-signal audit |
| S3 shadow logs | `s3://{S3_BUCKET_LOGS}/trading-logs/shadow/` | A/B comparison |
| CloudWatch alarms | CPU > 90%, memory > 85%, error rate > 5%/5min, P99 latency > 10ms | Monitoring |

**IAM policy for Signal Enrichment Service EC2 role:**
- S3 read: `quantembrace-model-artifacts` (model loading)
- S3 write: `{S3_BUCKET_LOGS}/trading-logs/` (enrichment + shadow logs)
- DynamoDB read: `feature-store-online`, `strategy-config`
- Kafka produce: `signals.enriched`, `ops.audit`
- Kafka consume: `signals.pending`, `kill.switch`

### Phase 6 Cost Envelope

| Item | Monthly Cost Delta |
|---|---|
| Signal Enrichment c6g.large EC2 (on-demand) | ~$55/month |
| S3 enrichment log storage (30-day hot) | ~$3/month |
| S3 Glacier Instant Retrieval (>30 days) | ~$1/month |
| **Total Phase 6 delta** | **~$59/month** (within $60 budget) |

---

## Phase 7 Design: Latency Optimization + Smart Order Router (PLANNED)

### Latency Hardening

Phase 7 targets sub-10ms P50 and sub-25ms P99 order-to-wire latency for NSE MARKET orders. Three complementary techniques are applied:

#### 1. HTTP/2 Persistent Connections

Replace per-request `requests.Session` with `httpx.AsyncClient` configured for HTTP/2 and connection pooling. This eliminates per-request TLS handshake overhead (~5–8ms on first request).

```python
# execution_engine/adapters/zerodha_broker.py
import httpx

class ZerodhaBroker:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            http2=True,
            limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=30),
            timeout=httpx.Timeout(connect=2.0, read=5.0, write=2.0),
        )

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        # Connection is reused; no TLS handshake on subsequent calls
        response = await self._client.post("/orders/regular", json=order.to_dict())
        return OrderResponse.from_dict(response.json())
```

Same pattern applied to `AlpacaBroker` for US equity orders.

#### 2. EC2 Network Tuning (SR-IOV + CPU Pinning + NUMA)

The Execution Engine is upgraded to `c6gn.large` (network-optimized Graviton3) in Phase 7.

**Startup script additions:**

```bash
# CPU core 0 pinning — eliminates scheduler migration latency
taskset -c 0 python -m services.execution_engine.main

# NUMA-aware memory allocation — co-locates buffer memory with NIC on NUMA node 0
numactl --cpunodebind=0 --membind=0 python -m services.execution_engine.main
```

SR-IOV is enabled on the c6gn.large instance's primary ENI via the Terraform `ec2_services` module (ENA driver, enhanced networking enabled by default on Graviton instances).

#### 3. Kafka Priority Lanes for High-Conviction Signals

High-conviction signals (quality_score above configured threshold) are routed to partition 0 of `signals.approved`. The Execution Engine polls partition 0 with a dedicated, higher-frequency consumer loop.

```python
# risk_engine/publishers/kafka_approved_publisher.py
def _select_partition(self, signal: EnrichedSignal) -> int:
    """Route high-conviction signals to partition 0 (priority lane)."""
    if (
        signal.quality_score is not None
        and signal.quality_score >= self._high_conviction_threshold
    ):
        return 0
    return 1  # Standard partition

# execution_engine/consumers/kafka_signal_consumer.py
async def _priority_poll_loop(self) -> None:
    """Dedicated high-frequency poll for partition 0 (high-conviction signals)."""
    while True:
        msgs = self._consumer.consume(num_messages=10, timeout=0.001)  # 1ms poll
        for msg in msgs:
            await self._handle_signal(msg)

async def _standard_poll_loop(self) -> None:
    """Standard poll for partition 1."""
    while True:
        msgs = self._consumer.consume(num_messages=50, timeout=0.010)  # 10ms poll
        for msg in msgs:
            await self._handle_signal(msg)
```

#### Phase 7 Redis Introduction (Conditional)

If Phase 7 latency benchmarks show the Risk Engine hot path exceeds 0.5ms P99 without Redis, ElastiCache Redis r6g.large (cluster mode, 1 shard) will be introduced at this point (deferred from ADR-014). The `KillSwitchCache` will be extended to use Redis pub/sub for kill switch propagation and Redis GET for position reads on the hot path.

---

### Smart Order Router (SOR)

The Smart Order Router is a component within the Execution Engine that splits large parent orders into child orders using TWAP, VWAP, or Iceberg algorithms. Standard signals bypass the SOR entirely.

**SOR directory layout:**

```
execution_engine/sor/
├── base.py          # SmartOrderRouter ABC, ParentOrder, ChildOrder, SORResult models
├── twap.py          # TWAP algorithm
├── vwap.py          # VWAP algorithm
├── iceberg.py       # Iceberg algorithm
└── venue_router.py  # NSE vs BSE venue selection
```

#### Data Models

```python
from __future__ import annotations
from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field
from datetime import datetime

class SORAlgorithm(str, Enum):
    TWAP = "TWAP"
    VWAP = "VWAP"
    ICEBERG = "ICEBERG"

class ParentOrder(BaseModel):
    """A large order to be split by the Smart Order Router."""
    parent_order_id: str
    signal_id: str
    trace_id: str
    instrument_id: str
    market: Literal["NSE", "US"]
    direction: Literal["BUY", "SELL"]
    total_quantity: int
    algorithm: SORAlgorithm
    # TWAP/VWAP parameters
    time_window_seconds: int | None = None
    num_slices: int | None = None
    # Iceberg parameters
    visible_pct: float = Field(default=0.20, ge=0.01, le=1.0)
    paper_trade: bool = True  # All SOR algorithms default to paper_trade=True

class ChildOrder(BaseModel):
    """A single child order produced by the SOR algorithm."""
    child_order_id: str
    parent_order_id: str
    trace_id: str
    instrument_id: str
    market: Literal["NSE", "US"]
    direction: Literal["BUY", "SELL"]
    quantity: int
    scheduled_time: datetime | None = None  # For TWAP/VWAP
    status: Literal["PENDING", "PLACED", "FILLED", "CANCELLED"] = "PENDING"

class SORResult(BaseModel):
    """Output of the SOR algorithm: execution plan with child orders."""
    parent_order_id: str
    algorithm: SORAlgorithm
    child_orders: list[ChildOrder]
    total_quantity: int

    @property
    def child_quantity_sum(self) -> int:
        return sum(c.quantity for c in self.child_orders)
```

#### TWAP Algorithm

```python
import math
from datetime import datetime, timedelta

class TWAPRouter:
    """Splits parent order into N equal child orders over a time window.

    Invariant: sum(child_quantities) == parent_quantity
    Invariant: all(q > 0 for q in child_quantities)
    Remainder is added to the last slice to handle non-divisible quantities.
    """
    def split(self, order: ParentOrder) -> SORResult:
        n = order.num_slices or 10
        window = order.time_window_seconds or 600
        base_qty = order.total_quantity // n
        remainder = order.total_quantity % n
        interval = window / n
        now = datetime.utcnow()

        children: list[ChildOrder] = []
        for i in range(n):
            qty = base_qty + (remainder if i == n - 1 else 0)
            children.append(ChildOrder(
                child_order_id=f"{order.parent_order_id}-{i:03d}",
                parent_order_id=order.parent_order_id,
                trace_id=order.trace_id,
                instrument_id=order.instrument_id,
                market=order.market,
                direction=order.direction,
                quantity=qty,
                scheduled_time=now + timedelta(seconds=i * interval),
            ))

        return SORResult(
            parent_order_id=order.parent_order_id,
            algorithm=SORAlgorithm.TWAP,
            child_orders=children,
            total_quantity=order.total_quantity,
        )
```

#### VWAP Algorithm

```python
class VWAPRouter:
    """Times child orders to match the instrument's historical volume profile.

    Volume profile is sourced from the offline feature store S3 Parquet.
    Falls back to TWAP if volume profile is unavailable.
    """
    def __init__(self, feature_reader: "OfflineFeatureReader") -> None:
        self._feature_reader = feature_reader

    async def split(self, order: ParentOrder) -> SORResult:
        profile = await self._feature_reader.get_volume_profile(
            instrument_id=order.instrument_id,
            market=order.market,
        )
        if profile is None:
            # Graceful fallback to TWAP
            return TWAPRouter().split(order)

        # Distribute quantity proportionally to volume profile weights
        weights = profile.normalized_weights  # list[float], sums to 1.0
        children: list[ChildOrder] = []
        allocated = 0
        for i, (weight, slot_time) in enumerate(zip(weights, profile.time_slots)):
            if i == len(weights) - 1:
                qty = order.total_quantity - allocated  # Remainder to last slice
            else:
                qty = max(1, round(order.total_quantity * weight))
                allocated += qty
            children.append(ChildOrder(
                child_order_id=f"{order.parent_order_id}-{i:03d}",
                parent_order_id=order.parent_order_id,
                trace_id=order.trace_id,
                instrument_id=order.instrument_id,
                market=order.market,
                direction=order.direction,
                quantity=qty,
                scheduled_time=slot_time,
            ))

        return SORResult(
            parent_order_id=order.parent_order_id,
            algorithm=SORAlgorithm.VWAP,
            child_orders=children,
            total_quantity=order.total_quantity,
        )
```

#### Iceberg Algorithm

```python
import asyncio

class IcebergRouter:
    """Submits 20% of total quantity as visible tranche; refills on fill.

    On fill confirmation: next tranche submitted within 100ms.
    On kill switch activation: all outstanding tranches cancelled within 500ms.
    """
    VISIBLE_PCT: float = 0.20

    def create_first_tranche(self, order: ParentOrder) -> ChildOrder:
        visible_qty = max(1, math.ceil(order.total_quantity * self.VISIBLE_PCT))
        return ChildOrder(
            child_order_id=f"{order.parent_order_id}-tranche-001",
            parent_order_id=order.parent_order_id,
            trace_id=order.trace_id,
            instrument_id=order.instrument_id,
            market=order.market,
            direction=order.direction,
            quantity=visible_qty,
        )

    async def on_fill(
        self,
        parent_order: ParentOrder,
        filled_qty: int,
        remaining_qty: int,
        tranche_num: int,
    ) -> ChildOrder | None:
        """Called on fill confirmation. Returns next tranche or None if complete."""
        if remaining_qty <= 0:
            return None
        next_qty = min(
            max(1, math.ceil(parent_order.total_quantity * self.VISIBLE_PCT)),
            remaining_qty,
        )
        # Must submit within 100ms — caller is responsible for timing
        return ChildOrder(
            child_order_id=f"{parent_order.parent_order_id}-tranche-{tranche_num:03d}",
            parent_order_id=parent_order.parent_order_id,
            trace_id=parent_order.trace_id,
            instrument_id=parent_order.instrument_id,
            market=parent_order.market,
            direction=parent_order.direction,
            quantity=next_qty,
        )
```

#### Venue Router (NSE vs BSE)

```python
class VenueRouter:
    """Routes to NSE or BSE based on tighter bid-ask spread at order time.

    Reads live quotes from LiveQuotePoller DynamoDB cache.
    Falls back to NSE if BSE quote is unavailable.
    """
    async def select_venue(
        self,
        instrument_id: str,
        quote_cache: "LiveQuoteCache",
    ) -> Literal["NSE", "BSE"]:
        nse_quote = await quote_cache.get(instrument_id, "NSE")
        bse_quote = await quote_cache.get(instrument_id, "BSE")
        if bse_quote is None:
            return "NSE"
        nse_spread = nse_quote.ask - nse_quote.bid
        bse_spread = bse_quote.ask - bse_quote.bid
        return "BSE" if bse_spread < nse_spread else "NSE"
```

#### SOR Kill Switch Integration

When the kill switch is activated during an active TWAP, VWAP, or Iceberg execution, all outstanding child orders must be cancelled within 500ms:

```python
class SORExecutor:
    """Manages active SOR executions and handles kill switch cancellation."""

    async def _on_kill_switch(self) -> None:
        """Cancel all outstanding child orders within 500ms."""
        cancel_tasks = [
            self._cancel_child_order(child)
            for child in self._active_children.values()
            if child.status == "PLACED"
        ]
        # asyncio.gather with timeout ensures 500ms deadline
        await asyncio.wait_for(
            asyncio.gather(*cancel_tasks, return_exceptions=True),
            timeout=0.5,
        )
```

#### SOR Paper Trading Gate

All SOR algorithms default to `paper_trade=True`. Promotion to live capital requires 5 consecutive clean trading sessions validated by the operator:

```bash
# After 5 clean paper sessions:
python scripts/strategy/config.py go-live sor_twap_v1
python scripts/strategy/config.py go-live sor_vwap_v1
python scripts/strategy/config.py go-live sor_iceberg_v1
```

### Phase 7 Infrastructure

| Resource | Type | Purpose |
|---|---|---|
| Execution Engine upgrade | c6gn.large ASG | Network-optimized Graviton3 for latency |
| ElastiCache Redis r6g.large (conditional) | Cluster mode, 1 shard | Risk Engine hot path (if latency targets require) |
| CloudWatch high-res metrics | 1s granularity, `QuantEmbrace/Latency` | Per-hop latency tracking |
| Grafana dashboard | Self-hosted or Grafana Cloud | P50/P95/P99 per hop, 10s refresh |

### Phase 7 Cost Envelope

| Item | Monthly Cost Delta |
|---|---|
| c6gn.large vs c6g.xlarge delta | ~$15/month |
| CloudWatch high-res metrics (6 hops × 3 stats) | ~$10/month |
| Grafana hosting | ~$10/month |
| ElastiCache Redis r6g.large (conditional) | ~$0–$130/month |
| **Total Phase 7 delta (without Redis)** | **~$35/month** (within $40 budget) |

---

### OpenTelemetry Instrumentation

Every Kafka message hop is instrumented with OpenTelemetry spans. The existing `trace_id` from the Kafka event envelope is used as the OTel trace context, ensuring a single CloudWatch Logs Insights query on `trace_id` reconstructs the full trade lifecycle.

**Shared telemetry module:**

```
shared/telemetry/
├── otel.py      # init_tracer(), start_span(), inject_context(), extract_context()
└── metrics.py   # Per-hop latency publisher to CloudWatch QuantEmbrace/Latency
```

**Instrumented hops:**

| Hop | Measurement Point | Metric Name |
|---|---|---|
| tick-to-strategy | Tick published → Signal generated | `QuantEmbrace/Latency/TickToStrategy` |
| strategy-to-enrichment | Signal published → Enrichment received | `QuantEmbrace/Latency/StrategyToEnrichment` |
| enrichment-to-risk | Enriched signal published → Risk received | `QuantEmbrace/Latency/EnrichmentToRisk` |
| risk-validation | Risk received → Approved published | `QuantEmbrace/Latency/RiskValidation` |
| risk-to-execution | Approved published → Execution received | `QuantEmbrace/Latency/RiskToExecution` |
| execution-to-broker-API | Execution received → HTTP request dispatched | `QuantEmbrace/Latency/ExecutionToBroker` |

**OTel span injection pattern:**

```python
# shared/telemetry/otel.py
from opentelemetry import trace
from opentelemetry.propagators.textmap import DefaultTextMapPropagator

def inject_context(envelope: dict, trace_id: str) -> dict:
    """Inject OTel trace context into Kafka envelope using existing trace_id."""
    ctx = trace.set_span_in_context(
        trace.NonRecordingSpan(
            trace.SpanContext(
                trace_id=int(trace_id.replace("-", ""), 16),
                span_id=trace.INVALID_SPAN_ID,
                is_remote=True,
            )
        )
    )
    carrier: dict = {}
    DefaultTextMapPropagator().inject(carrier, ctx)
    envelope["otel_context"] = carrier
    return envelope
```

### Grafana Latency Dashboard

The Grafana dashboard displays P50, P95, and P99 latency for each of the 6 hops, auto-refreshing every 10 seconds. CloudWatch alarms fire when any hop's P99 exceeds its configured threshold for 2 consecutive minutes.

**CloudWatch alarm configuration (per hop):**

```hcl
resource "aws_cloudwatch_metric_alarm" "latency_p99" {
  for_each            = var.latency_hops
  alarm_name          = "quantembrace-${var.env}-latency-p99-${each.key}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "p99"
  namespace           = "QuantEmbrace/Latency"
  period              = 60
  statistic           = "p99"
  threshold           = each.value.threshold_ms
  alarm_actions       = [aws_sns_topic.alerts.arn]
  dimensions = {
    Hop = each.key
  }
}
```

---

## Component Interfaces

### Signal Enrichment Service — Kafka Interface

```python
# Consumer: enrichment-v1 consuming signals.pending
# Message key: signal_id (bytes)
# Message value: JSON-serialized Signal (schema_version "3.0")

# Producer: publishing to signals.enriched
# Message key: instrument_id (bytes)
# Message value: JSON-serialized EnrichedSignal (schema_version "4.0")
# Partition: instrument_id hash (confluent_kafka default partitioner)
```

### Risk Engine — Enriched Signal Interface

From Phase 6, the Risk Engine's `KafkaSignalConsumer` (group `risk-v1`) consumes from `signals.enriched` instead of `signals.pending`. The `EnrichedSignal` model is a superset of `Signal` — all existing validators continue to work unchanged. Two new validators (`RegimeValidator`, `VolatilitySizingValidator`) are added to the validation chain.

```python
# risk_engine/consumers/kafka_signal_consumer.py
TOPIC = "signals.enriched"   # Changed from "signals.pending" in Phase 6
GROUP_ID = "risk-v1"         # Unchanged
```

### Execution Engine — SOR Interface

```python
# execution_engine/service.py
async def _handle_approved_signal(self, signal: EnrichedSignal) -> None:
    if signal.sor_type is not None:
        result = await self._sor_dispatcher.dispatch(signal)
        await self._execute_sor_plan(result)
    else:
        await self._place_direct_order(signal)
```

### Feature Store — Read Interface

```python
# shared/features/feature_reader.py
class FeatureReader:
    async def get_latest(
        self,
        market: str,
        instrument: str,
        feature_group: str = "default",
    ) -> dict[str, float] | None:
        """Returns latest feature values or None if stale/missing."""
        ...

    async def get_feature_vector(
        self,
        market: str,
        instrument: str,
    ) -> "np.ndarray | None":
        """Returns 9-element numpy array for ONNX inference, or None if stale."""
        # Order: [rsi_14, ema_9, ema_21, vwap, atr_14, adx_14, macd, macd_signal, macd_hist, volume_ratio]
        ...
```

### Mandatory Environment Variables (Phase 6 additions)

| Variable | Required By |
|---|---|
| `KAFKA_BOOTSTRAP_SERVERS` | signal_enrichment (raises RuntimeError on startup if unset) |
| `AWS_REGION` | signal_enrichment |
| `S3_BUCKET_MODEL_ARTIFACTS` | signal_enrichment (ONNX model loading) |
| `S3_BUCKET_LOGS` | signal_enrichment (enrichment + shadow logs) |
| `DYNAMODB_TABLE_PREFIX` | signal_enrichment |
| `QE_ENVIRONMENT` | signal_enrichment |
| `MIN_QUALITY_THRESHOLD` | signal_enrichment (default: 0.4) |
| `HIGH_VOLATILITY_THRESHOLD` | signal_enrichment / risk_engine (default: 0.03) |
| `HIGH_CONVICTION_THRESHOLD` | risk_engine / execution_engine (default: 0.75) |

---

## Error Handling

### Signal Enrichment Service

| Failure Mode | Behavior |
|---|---|
| ONNX model not loaded at startup | `RuntimeError` — service does not start |
| Feature vector unavailable (stale/missing) | Publish signal with `regime=None`, `quality_score=None`, `volatility_forecast=None`; log warning |
| ONNX inference error | Log error, publish signal with enrichment fields as `None` (fail-open for enrichment, fail-safe for risk) |
| S3 model download failure | Log error, continue serving with current model version |
| Shadow model error | Log warning, continue with production model; shadow error never propagates |
| `signals.enriched` publish failure | Log error, retry with exponential backoff; activate kill switch after 3 consecutive failures |

### Smart Order Router

| Failure Mode | Behavior |
|---|---|
| Kill switch activated during TWAP/VWAP | Cancel all outstanding child orders within 500ms |
| Kill switch activated during Iceberg | Cancel current visible tranche within 500ms |
| VWAP volume profile unavailable | Graceful fallback to TWAP |
| Child order placement failure | Log error, cancel remaining child orders, publish `ORDER_SOR_FAILED` to `ops.audit` |
| Iceberg next-tranche timeout (>100ms) | Log warning, retry once; if second attempt fails, cancel parent order |

### Risk Engine (Phase 6 additions)

| Failure Mode | Behavior |
|---|---|
| `EnrichedSignal` missing enrichment fields | Apply conservative defaults: treat as `volatile` regime, apply 50% size reduction |
| `RegimeValidator` exception | Log error, reject signal (fail-safe) |
| `VolatilitySizingValidator` exception | Log error, reject signal (fail-safe) |

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

---

### Property 1: Enrichment Latency Bound

*For any* valid feature vector drawn from the feature store schema (9 features: RSI-14, EMA-9, EMA-21, VWAP, ATR-14, ADX-14, MACD, MACD signal, MACD histogram, volume_ratio), the Signal Enrichment Service SHALL complete all three ONNX model inferences (Regime Classifier, Volatility Forecaster, Signal Quality Scorer) and produce an `EnrichedSignal` within 5 milliseconds of signal receipt.

**Validates: Requirements 6.2, 6.12**

---

### Property 2: Low-Quality Signal Filtering

*For any* signal where the Signal Quality Scorer produces a confidence score strictly below the configured `MIN_QUALITY_THRESHOLD`, the signal SHALL NOT appear on the `signals.enriched` Kafka topic, and a `SIGNAL_FILTERED_LOW_CONFIDENCE` event SHALL be published to `ops.audit` containing the `signal_id`, `strategy_name`, and `quality_score`.

**Validates: Requirements 6.6**

---

### Property 3: Volatile/Crash Regime Position Size Reduction

*For any* `EnrichedSignal` where `regime` is `"volatile"` or `"crash"`, the Risk Engine SHALL approve a position size of at most 50% of the base `max_position_size` configured for that instrument. This property holds regardless of the signal's other attributes (instrument, direction, strategy, quality score).

**Validates: Requirements 6.4**

---

### Property 4: Volatility-Proportional Position Sizing

*For any* `EnrichedSignal` where `volatility_forecast` is a positive float exceeding the configured `HIGH_VOLATILITY_THRESHOLD`, the Risk Engine SHALL approve a position size that is proportional to the inverse of the predicted volatility and is strictly less than or equal to the base `max_position_size`. Specifically: `approved_size ≤ base_size × (threshold / volatility_forecast)`.

**Validates: Requirements 6.5**

---

### Property 5: Shadow Mode Isolation

*For any* shadow model failure (exception, timeout, or invalid output), the production `EnrichedSignal` SHALL be published to `signals.enriched` unchanged, with the same enrichment fields as if shadow mode were disabled. Shadow errors SHALL NOT affect the live signal path.

**Validates: Requirements 6.7**

---

### Property 6: Agent Read-Only Invariant

*For any* regime classification, position state, or Sharpe ratio value presented to the Strategy Selector Agent or Parameter Tuner Agent, neither agent SHALL write to the `strategy-config` DynamoDB table, call `strategy_config_loader.set_enabled()`, or publish any event to `signals.pending` or `signals.enriched`. All agent outputs are confined to `ops.audit`.

**Validates: Requirements 6.9, 6.10**

---

### Property 7: Enrichment Audit Completeness

*For any* signal processed by the Signal Enrichment Service (whether published or dropped), an S3 audit record SHALL exist at `trading-logs/enrichment/{date}/` containing the feature values used for inference, the model version, and the enrichment outputs (regime, volatility_forecast, quality_score).

**Validates: Requirements 6.11**

---

### Property 8: TWAP Child Order Quantity Invariant

*For any* valid `ParentOrder` with `algorithm=TWAP`, `total_quantity ≥ 1`, `num_slices ≥ 1`, and `time_window_seconds ≥ 1`, the TWAP algorithm SHALL produce a `SORResult` where:
1. The sum of all child order quantities equals `total_quantity` exactly.
2. Every individual child order quantity is strictly greater than zero.
3. The number of child orders equals `num_slices`.

**Validates: Requirements 9.1, 12.7**

---

### Property 9: VWAP Volume Profile Proportionality

*For any* valid `ParentOrder` with `algorithm=VWAP` and a non-null volume profile with N time slots and normalized weights summing to 1.0, the VWAP algorithm SHALL produce child orders where:
1. The sum of all child order quantities equals `total_quantity` exactly.
2. Each child order quantity is within 5% of `total_quantity × weight[i]` (rounding tolerance).
3. Every individual child order quantity is strictly greater than zero.

**Validates: Requirements 9.2**

---

### Property 10: Iceberg Visible Tranche Invariant

*For any* valid `ParentOrder` with `algorithm=ICEBERG` and `total_quantity ≥ 1`, the Iceberg algorithm SHALL produce a first visible tranche where `tranche_quantity = ceil(0.20 × total_quantity)` and `tranche_quantity ≥ 1`. The sum of all tranches across the full execution lifecycle SHALL equal `total_quantity` exactly.

**Validates: Requirements 9.3**

---

### Property 11: VaR Non-Negativity and Monotonicity

*For any* valid portfolio of positions drawn from the strategy universe (any combination of NSE and US instruments with non-negative quantities), the computed VaR SHALL satisfy:
1. `VaR(portfolio, confidence=0.95) ≥ 0`
2. `VaR(portfolio, confidence=0.99) ≥ VaR(portfolio, confidence=0.95)` (monotonically increases with confidence level)
3. `VaR(empty_portfolio, any_confidence) == 0`

**Validates: Requirements 12.1**

---

### Property 12: Order State Machine Validity

*For any* sequence of valid order events applied to the order state machine (PENDING → PLACED → FILLED / REJECTED / CANCELLED), the state machine SHALL:
1. Never reach an undefined state (state is always one of: PENDING, PLACED, FILLED, REJECTED, CANCELLED).
2. Never transition backwards from a terminal state (FILLED, REJECTED, CANCELLED are absorbing states).
3. Never transition from FILLED to REJECTED or CANCELLED, or vice versa.

**Validates: Requirements 12.2**

---

### Property 13: Signal Deduplication Idempotency

*For any* set of N ≥ 1 signals with identical `signal_id` values submitted to the Risk Engine's DynamoDB idempotency gate, exactly one `SIGNAL_APPROVED` event SHALL be published to `signals.approved`, and the DynamoDB `risk-decisions` table SHALL contain exactly one record for that `signal_id`.

**Validates: Requirements 12.3**

---

### Property 14: Kill Switch Hierarchy Dominance

*For any* combination of GLOBAL, MARKET, and INSTRUMENT kill switch states, if any kill switch applicable to a given signal is in the ACTIVE state, the Risk Engine SHALL reject the signal and SHALL NOT publish any event to `signals.approved`. The most restrictive state always wins: GLOBAL ACTIVE overrides all; MARKET ACTIVE overrides INSTRUMENT INACTIVE; INSTRUMENT ACTIVE overrides MARKET INACTIVE.

**Validates: Requirements 12.4**

---

### Property 15: Correlation Matrix Mathematical Invariants

*For any* valid set of position return series (any number of instruments, any return values in [-1, 1] per period), the computed correlation matrix SHALL satisfy:
1. Symmetry: `corr[i][j] == corr[j][i]` for all i, j.
2. Unit diagonal: `corr[i][i] == 1.0` for all i.
3. Bounded off-diagonal: `-1.0 ≤ corr[i][j] ≤ 1.0` for all i ≠ j.

**Validates: Requirements 12.5**

---

### Property 16: NSE Order-to-Wire Latency Bound

*For any* valid NSE MARKET order processed by the Execution Engine (Phase 7, with HTTP/2 persistent connections and CPU pinning), the order-to-wire latency (from `SIGNAL_APPROVED` consumed to HTTP request dispatched to Zerodha API) SHALL be less than 10 milliseconds at the 50th percentile and less than 25 milliseconds at the 99th percentile, measured over a minimum of 1000 consecutive order placements against a mock HTTP/2 server.

**Validates: Requirements 8.1, 8.2**

---

### Property 17: trace_id OTel Context Propagation

*For any* Kafka event with a `trace_id` field in the event envelope, the OpenTelemetry span created at that hop SHALL use the same `trace_id` value as its trace context identifier, ensuring that a single CloudWatch Logs Insights query on `trace_id` reconstructs the full trade lifecycle with microsecond-precision timestamps at each hop.

**Validates: Requirements 10.4, 13.5**

---

## Testing Strategy

### Dual Testing Approach

All correctness properties above are implemented as Hypothesis property-based tests. Example-based unit tests cover specific scenarios, integration points, and error conditions.

**Property-based tests** (Hypothesis, minimum 500 examples per property):
- `tests/unit/signal_enrichment/test_enrichment_latency.py` — Properties 1, 5
- `tests/unit/signal_enrichment/test_quality_filter.py` — Property 2
- `tests/unit/risk_engine/test_regime_validator.py` — Property 3
- `tests/unit/risk_engine/test_volatility_sizing.py` — Property 4
- `tests/unit/signal_enrichment/test_agents.py` — Property 6
- `tests/unit/signal_enrichment/test_enrichment_logger.py` — Property 7
- `tests/unit/execution_engine/test_twap.py` — Property 8
- `tests/unit/execution_engine/test_vwap.py` — Property 9
- `tests/unit/execution_engine/test_iceberg.py` — Property 10
- `tests/unit/risk_engine/test_var_calculator.py` — Property 11
- `tests/unit/risk_engine/test_order_state_machine.py` — Property 12
- `tests/unit/risk_engine/test_signal_deduplication.py` — Property 13
- `tests/unit/risk_engine/test_kill_switch_hierarchy.py` — Property 14
- `tests/unit/risk_engine/test_correlation_matrix.py` — Property 15
- `tests/unit/execution_engine/test_latency_targets.py` — Property 16
- `tests/unit/shared/test_otel_propagation.py` — Property 17

**Example-based unit tests** cover:
- ONNX model hot-reload within 60s (Requirement 6.8)
- Kill switch cancellation of TWAP/VWAP within 500ms (Requirement 9.4)
- Iceberg next-tranche submission within 100ms (Requirement 9.5)
- Venue router NSE vs BSE selection (Requirement 9.7)
- CloudWatch alarm firing on P99 threshold breach (Requirement 10.3)

**Integration tests** (`tests/integration/`):
- Full enriched signal pipeline: strategy → `signals.pending` → enrichment → `signals.enriched` → risk → `signals.approved` (PHASE6-016)
- SOR paper trading flow: TWAP + Iceberg end-to-end with mock broker (PHASE7-009)

**CI configuration:**

```ini
# pyproject.toml
[tool.pytest.ini_options]
addopts = "--hypothesis-seed=0"

[tool.hypothesis]
max_examples = 500
deriving = "best"
```

**Hypothesis test tag format:**

```python
@settings(max_examples=500)
@given(feature_vector=feature_vectors())
def test_enrichment_latency_bound(feature_vector):
    # Feature: quantembrace-system, Property 1: Enrichment Latency Bound
    ...
```
