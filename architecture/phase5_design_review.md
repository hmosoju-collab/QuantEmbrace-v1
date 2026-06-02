# Phase 5 Design Review: Data Platform + Feature Store

**Version**: 1.1 — REVIEW DECISIONS APPLIED  
**Date**: 2026-05-06  
**Status**: ✅ APPROVED FOR IMPLEMENTATION  
**Author**: QuantEmbrace Architect  
**Prerequisites**: Phase 4 complete ✅ (including PHASE4-FU-001)

**Review decisions applied**:
- Feature set approved as proposed for v1.0: RSI-14, EMA-9/21, VWAP, ATR-14, ADX-14, MACD/signal/hist, volume_ratio.
- Offline archival uses DynamoDB intraday feature rows, not `LATEST` scan. `LATEST` is for online reads only.
- Feature staleness is interval-aware: `max(5 minutes, 2 * interval + 1 minute)`.
- `volume_ratio` uses `adv_20d` from the prices table / candle prefetch path; no new broker API calls.
- US feature store is deferred until an Alpaca candle stream exists. Phase 5 implementation is NSE-only.

---

## 1. Executive Summary

Phase 5 moves feature computation out of inline strategy code and into a shared, persisted feature store. This closes three gaps that will block Phase 6 (ML + Agentic Layer):

1. **No shared feature state** — five strategies each compute RSI, EMA, VWAP independently with no guarantee of consistency.
2. **No feature history** — backtesting must recompute features from raw candles; ML training has no feature dataset.
3. **No feature source for ML inference** — Phase 6 models need pre-computed, normalised features at signal time.

Phase 5 is **purely additive**. Existing strategies continue to work unchanged. The feature store is built alongside the current pipeline, not replacing it.

---

## 2. Current State (Post Phase 4)

| Component | Status |
|-----------|--------|
| `IntradayCandleStream` | Writes OHLCV candles to DynamoDB candle-cache every interval close |
| `DynamoCandleConsumer` | Polls candle-cache every 500ms; delivers bars to StrategyRunner |
| Strategy `on_bar()` | Computes RSI, EMA, VWAP, ATR inline — duplicated across 5 strategy files |
| `LiveQuotePoller` | Writes live spread to DynamoDB (PHASE4-FU-001) |
| `RiskContextBuilder` | Reads ADV from prices table — only historical ADV available |
| ML inference (Phase 6) | No feature source; blocked |
| Backtesting | Must recompute all features from raw data on each run |

---

## 3. Scope

### In scope

| # | Component | Description |
|---|-----------|-------------|
| 5-1 | `FeatureEngine` | Pure-function feature computation — RSI, EMA, VWAP, ATR, ADX, MACD, volume ratio |
| 5-2 | `FeatureWriter` | DynamoDB online + intraday rows (non-blocking, non-fatal) |
| 5-3 | `FeatureReader` | Shared client for strategy_engine, risk_engine, ai_engine to read latest features |
| 5-4 | `FeatureArchiver` | EOD S3 parquet write — offline store for ML training and backtesting |
| 5-5 | Integration | Wire FeatureEngine + FeatureWriter into `IntradayCandleStream.on_candle` |
| 5-6 | Terraform | New DynamoDB features table, IAM policies, S3 lifecycle |
| 5-7 | Tests | Unit tests: FeatureEngine computation, FeatureWriter schema, FeatureReader degradation |

### Out of scope

- Strategy refactoring (strategies continue computing inline; this is Phase 7 scope)
- Real-time feature streaming via Kafka (no `features.computed` topic — unnecessary at 5-30 signals/day)
- Feature versioning / schema migrations (mitigated by `schema_version` attribute)
- Alpaca / US features (no Alpaca candle stream yet; US feature store deferred to Phase 6; keep Phase 5 NSE-only)

---

## 4. Architecture

### 4.1 Data Flow

```
IntradayCandleStream.on_candle(candle)
    │
    ├── [existing] DynamoDB candle-cache write
    │       └── DynamoCandleConsumer → StrategyRunner → on_bar() [unchanged]
    │
    └── [NEW] FeatureEngine.compute(candle_history)
                │
                ├── FeatureWriter.write_async()
                │       └── DynamoDB features table
                │               PK = FEATURE#{market}#{symbol}#{interval}
                │               SK = LATEST
                │               SK = CANDLE#{yyyy-mm-ddTHH:MM:SSZ}
                │
                └── [EOD, POST_CLOSE] FeatureArchiver.archive_day()
                        └── S3: features/{market}/{symbol}/{interval}/{date}.parquet
```

### 4.2 Feature Store Layers

#### Online Store — DynamoDB

**Purpose**: Low-latency reads at signal time and during ML inference.  
**Table**: `quantembrace-{env}-features` (new — separate from prices/candle-cache tables)

```
PK  = FEATURE#{market}#{symbol}#{interval}   e.g. FEATURE#NSE#RELIANCE#1m
SK  = LATEST                                 online read path
SK  = CANDLE#{yyyy-mm-ddTHH:MM:SSZ}          intraday archive source

Attributes:
  rsi_14        (N)  — 0.0–100.0; None if < 14 candles
  ema_9         (N)
  ema_21        (N)
  vwap          (N)
  atr_14        (N)  — in price units
  adx_14        (N)  — 0.0–100.0; None if < 14 candles
  macd          (N)  — MACD line (12-period EMA − 26-period EMA)
  macd_signal   (N)  — 9-period EMA of MACD line
  macd_hist     (N)  — macd − macd_signal
  volume_ratio  (N)  — current_volume / adv_20d (from prices table)
  candle_count  (N)  — number of candles used for computation
  computed_at   (S)  — ISO UTC timestamp
  schema_version (N) — integer; currently 1
  ttl           (N)  — Unix epoch; LATEST expires after 24h, CANDLE rows after 7d
```

**Read path**: `get_item` with `ConsistentRead=False`. Expected latency: 3–5ms.  
**Write path**: Two `put_item` calls per feature set: one `LATEST` row for online reads and one `CANDLE#...` row for same-day archival. At 40 NSE symbols × 1m/5m/15m intervals this remains comfortably below personal-trader scale DynamoDB cost limits.

#### Offline Store — S3

**Purpose**: Historical feature dataset for ML training, backtesting, and audit.

```
s3://quantembrace-{env}-data/
  features/
    NSE/
      RELIANCE/
        1m/
          2026-05-06.parquet    ← one file per symbol/interval/day
          2026-05-07.parquet
        5m/
          2026-05-06.parquet
      INFY/
        1m/
          ...
```

**Schema** (parquet columns): `symbol`, `market`, `interval`, `candle_open_time`, `rsi_14`, `ema_9`, `ema_21`, `vwap`, `atr_14`, `adx_14`, `macd`, `macd_signal`, `macd_hist`, `volume_ratio`, `candle_count`, `computed_at`

**Write timing**: POST_CLOSE phase transition → `FeatureArchiver.archive_day()` runs once per day per symbol/interval. It queries `SK begins_with CANDLE#{date}` for each configured NSE symbol/interval and writes consolidated parquet to S3.

> **Note**: The offline store is append-only. Each day's file is written once and never overwritten. Backtesting reads S3 parquet directly via Athena or S3 Select.

### 4.3 Component Design

#### `FeatureEngine` (`data_ingestion/features/feature_engine.py`)

Pure computation — no I/O. Takes a list of OHLCV `CandleBar` objects and returns a `FeatureSet` dataclass.

```python
@dataclass(frozen=True)
class FeatureSet:
    symbol:       str
    market:       str
    interval:     str           # "1m", "5m", "15m"
    candle_time:  datetime      # open time of the latest candle
    candle_count: int
    rsi_14:       Optional[float]
    ema_9:        Optional[float]
    ema_21:       Optional[float]
    vwap:         Optional[float]
    atr_14:       Optional[float]
    adx_14:       Optional[float]
    macd:         Optional[float]
    macd_signal:  Optional[float]
    macd_hist:    Optional[float]
    volume_ratio: Optional[float]   # requires adv_20d from prices table
    computed_at:  datetime
    schema_version: int = 1

class FeatureEngine:
    def compute(
        self,
        candles: list[CandleBar],   # chronological, same symbol/interval
        adv_20d: Optional[float] = None,
    ) -> FeatureSet: ...
```

**Lookback requirements**:

| Feature | Min candles | Returns None if |
|---------|-------------|-----------------|
| EMA(9) | 9 | < 9 candles |
| EMA(21) | 21 | < 21 candles |
| RSI(14) | 15 | < 15 candles |
| ATR(14) | 15 | < 15 candles |
| ADX(14) | 28 | < 28 candles |
| MACD(12,26,9) | 35 | < 35 candles |
| VWAP | 1 | 0 candles |
| volume_ratio | 1 | adv_20d is None |

All features are `Optional[float]`. None values are omitted from the DynamoDB item (no `"N": "None"` noise).

#### `FeatureWriter` (`data_ingestion/features/feature_writer.py`)

Async DynamoDB write. Non-blocking (asyncio.to_thread). Non-fatal — feature write failure never propagates to the candle pipeline.

```python
class FeatureWriter:
    def __init__(self, dynamo_client, features_table: str) -> None: ...

    async def write(self, feature_set: FeatureSet) -> None:
        """Write feature_set to DynamoDB. Failure is logged and suppressed."""
```

**DynamoDB item construction**: Only non-None features are written as `{"N": str(value)}` attributes. `computed_at` written as ISO string. `ttl` is `computed_at + 86400s` for the `LATEST` row and `computed_at + 7 days` for `CANDLE#...` rows. The longer intraday TTL gives the EOD archiver recovery room after a service restart.

#### `FeatureReader` (`shared/features/feature_reader.py`)

Shared client — importable by strategy_engine, risk_engine, ai_engine. Returns None on any error (graceful degradation contract, same as other Phase 4 validators).

```python
class FeatureReader:
    def __init__(self, dynamo_client, features_table: str) -> None: ...

    async def get_latest(
        self,
        market: str,
        symbol: str,
        interval: str,
    ) -> Optional[FeatureSet]:
        """
        Read the latest FeatureSet from DynamoDB.
        Returns None if not found, stale for the interval, or on any error.
        """
```

**Staleness threshold**: Interval-aware. Use `max(5 minutes, 2 * interval + 1 minute)`, so 1m features stale after 5 minutes, 5m after 11 minutes, and 15m after 31 minutes. This prevents ML inference from acting on stale 1m data without falsely treating healthy 15m features as stale between candle closes.

#### `FeatureArchiver` (`data_ingestion/features/feature_archiver.py`)

Background task started with `data_ingestion` service. Triggered by POST_CLOSE phase transition. Reads today's feature items from DynamoDB by querying each configured `FEATURE#{market}#{symbol}#{interval}` partition for `SK begins_with CANDLE#{date}`, accumulates into a DataFrame, writes to S3 as parquet.

```python
class FeatureArchiver:
    async def on_phase_change(self, phase: MarketPhase) -> None:
        if phase == MarketPhase.POST_CLOSE:
            await self.archive_day()

    async def archive_day(self) -> None:
        """Query same-day CANDLE rows → build DataFrame → write to S3 parquet."""
```

**Dependency**: `pandas` + `pyarrow` (add to `requirements.txt`). Both are lightweight and already likely needed for Phase 6 anyway.

### 4.4 Integration Point

`IntradayCandleStream.on_candle(candle)` gains a feature pipeline hook:

```python
# In IntradayCandleStream
async def on_candle(self, candle: CandleData) -> None:
    # [existing] Write to candle-cache DynamoDB
    await self._write_candle(candle)

    # [NEW] Compute and persist features (non-blocking, non-fatal)
    if self._feature_engine and self._feature_writer:
        history = self._get_candle_history(candle.symbol, candle.interval)
        feature_set = self._feature_engine.compute(history)
        asyncio.create_task(self._feature_writer.write(feature_set))
```

`asyncio.create_task` — fire-and-forget. Feature write never delays the candle acknowledgement path.

---

## 5. Alignment with ADR-012 (Zerodha Rate Capacity)

| Concern | Impact |
|---------|--------|
| FeatureEngine computation | CPU-only, no Zerodha API calls — zero rate budget impact |
| FeatureWriter DynamoDB writes | DynamoDB only — zero rate budget impact |
| FeatureArchiver (POST_CLOSE) | Runs after market closes — rate budget irrelevant |
| FeatureReader DynamoDB reads | DynamoDB only — zero rate budget impact |
| `volume_ratio` feature | Reads `adv_20d` from prices table (already pre-fetched by candle_prefetch.py) — no new API calls |

---

## 6. Terraform Changes

### New: `features` DynamoDB table

```hcl
# infra/terraform/modules/dynamodb/features.tf
resource "aws_dynamodb_table" "features" {
  name         = "${var.env_prefix}-features"
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "PK"
  range_key = "SK"

  attribute { name = "PK"; type = "S" }
  attribute { name = "SK"; type = "S" }

  ttl { attribute_name = "ttl"; enabled = true }

  tags = { Environment = var.environment, Service = "data_ingestion" }
}
```

### IAM policies

| Service | Permission |
|---------|------------|
| `data_ingestion` | `dynamodb:PutItem` on features table; `s3:PutObject` on `features/` prefix |
| `strategy_engine` | `dynamodb:GetItem` on features table |
| `risk_engine` | `dynamodb:GetItem` on features table |
| `ai_engine` | `dynamodb:GetItem`, `dynamodb:Query` on features table; `s3:GetObject` on `features/` |

### S3 lifecycle (features prefix)

```hcl
rule {
  id     = "features-tiering"
  prefix = "features/"
  transition { days = 90;  storage_class = "STANDARD_IA" }
  transition { days = 365; storage_class = "GLACIER_IR"  }
}
```

---

## 7. Settings Addition

```python
# shared/config/settings.py — AWSConfig
dynamodb_table_features: str = Field(
    default_factory=lambda: f"{os.getenv('DYNAMODB_TABLE_PREFIX', 'qe-dev')}-features",
    description="DynamoDB table for pre-computed feature store (online layer).",
)
```

---

## 8. Implementation Tasks

| Task | File(s) | Priority |
|------|---------|----------|
| PHASE5-001 | `data_ingestion/features/feature_engine.py` + `shared/models/feature_set.py` — FeatureEngine + FeatureSet dataclass; all 9 features | P0 |
| PHASE5-002 | `data_ingestion/features/feature_writer.py` — DynamoDB write, TTL, schema_version, omit-None | P0 |
| PHASE5-003 | `shared/features/feature_reader.py` — get_latest, staleness check, graceful None | P0 |
| PHASE5-004 | Wire FeatureEngine + FeatureWriter into `services/data_ingestion/candle_stream.py` on_candle hook | P0 |
| PHASE5-005 | `data_ingestion/features/feature_archiver.py` — POST_CLOSE query of `CANDLE#...` rows → parquet → S3 | P1 |
| PHASE5-006 | `infra/terraform/modules/dynamodb/features.tf` — features table + IAM | P1 |
| PHASE5-007 | `shared/config/settings.py` — add `dynamodb_table_features` | P1 |
| PHASE5-008 | `tests/unit/test_feature_engine.py` — 40+ tests: each feature math, None on insufficient data, edge cases (all-zero volume, single candle, exact min-candle boundary) | P0 |
| PHASE5-009 | `tests/unit/test_feature_writer.py` — DynamoDB schema, TTL computation, None-attribute omission, write failure non-fatal | P1 |
| PHASE5-010 | `tests/unit/test_feature_reader.py` — happy path, staleness, missing item, DynamoDB error → None | P1 |

---

## 9. Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| ADX/MACD need 28–35 candles; always None at market open for ~30min | LOW | Features are Optional; consumers must handle None. Document in FeatureSet docstring. |
| pandas + pyarrow adds ~15MB to data_ingestion Docker image | LOW | Add to requirements.txt; acceptable for the archive use case. |
| Strategy inline computation vs feature store drift | MEDIUM | Strategies unchanged in Phase 5. FeatureEngine uses same formulas as strategy code — validate via regression test comparing inline vs. FeatureEngine output for same inputs. |
| DynamoDB archival query misses rows | MEDIUM | FeatureWriter writes one `CANDLE#timestamp` row per feature set; FeatureArchiver queries by symbol/interval/date prefix instead of scanning `LATEST`. |
| Feature store schema change across deployments | MEDIUM | `schema_version` attribute in every item. FeatureReader rejects items with mismatched schema_version, returns None. |
| S3 parquet write fails at EOD | LOW | FeatureArchiver logs error; retries on next run (if service restarts before midnight). Historical gap is acceptable for personal trader. |

---

## 10. Migration from Phase 4

No migration required. Phase 5 is additive:

- New DynamoDB table `features` (no existing table modified)
- New files under `data_ingestion/features/` and `shared/features/`
- One new hook in `IntradayCandleStream.on_candle` (existing path unmodified)
- New Terraform module (no existing infra changed)
- Existing services: no changes to risk_engine, execution_engine, strategy_engine

Rollback: remove the hook and delete the table. All other services unaffected.

---

## 11. Review Decisions

1. **Feature set scope** — Approved as proposed: RSI-14, EMA-9/21, VWAP, ATR-14, ADX-14, MACD/signal/hist, volume_ratio. Bollinger Bands and OBV are deferred until there is a strategy or model that consumes them.

2. **Offline store write strategy** — Do not scan `LATEST` and do not rely on in-memory-only buffers. FeatureWriter writes both `SK=LATEST` and `SK=CANDLE#{timestamp}` rows. FeatureArchiver queries same-day `CANDLE#...` rows at POST_CLOSE and writes parquet.

3. **FeatureReader staleness threshold** — Use interval-aware staleness: `max(5 minutes, 2 * interval + 1 minute)`.

4. **`volume_ratio` computation** — Use `adv_20d` from the prices table / candle prefetch path and pass it into `FeatureEngine.compute()`. No new broker API calls and no long historical candle dependency in the feature hot path.

5. **US feature store** — Confirmed deferred to Phase 6. Phase 5 is NSE-only, matching the PHASE4-FU-001 broker-boundary decision that Zerodha-backed live quote polling must not receive US symbols.
