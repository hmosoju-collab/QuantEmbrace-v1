# Phase 6 Design Review: ML + Agentic Layer

**Version**: 1.1 — REVISIONS APPLIED  
**Date**: 2026-05-08  
**Status**: ✅ APPROVED — implementation ready  
**Author**: QuantEmbrace Architect  
**Prerequisites**: Phase 5 implementation complete (tests green, terraform fmt clean required before merge)

---

## 1. Executive Summary

Phase 6 activates the `ai_engine` service as a live participant in the trading pipeline. It
delivers three concrete capabilities:

1. **Regime classification** — every approved signal is tagged with the current market regime
   (trending / ranging / volatile / crash). Execution sizing adapts per regime.
2. **Signal quality scoring** — a lightweight model scores each signal 0.0–1.0 before it
   reaches the risk engine. Signals below threshold are soft-filtered.
3. **Strategy selector agent** — a read-only LLM-based advisory agent that observes regime
   + portfolio state and emits strategy activation recommendations (operator approves).

The existing `ai_engine` service is a skeleton (FastAPI + placeholder `Predictor`). Phase 6
replaces every `TODO` with working code and inserts the service into the live signal path
via a new Kafka topic `signals.enriched`.

### What Phase 6 is NOT

- It does not train models. All models are trained offline (local or SageMaker). Phase 6 is
  inference-only.
- It does not replace strategies. Signal generation remains in `strategy_engine` unchanged.
- It does not gate orders. The risk engine remains the sole gatekeeper. ML enrichment is
  advisory; a signal with low quality score still reaches risk validation.
- It is not a volatility forecaster (GARCH/ML vol forecast deferred to Phase 7 as a latency
  enhancement input).

---

## 2. Current State (Post Phase 5)

| Component | State |
|-----------|-------|
| `ai_engine/service.py` | FastAPI skeleton, `Predictor` returns 0.0 placeholder |
| `ai_engine/models/model_registry.py` | All S3 calls are TODO/placeholder |
| `ai_engine/inference/predictor.py` | `# TODO: Replace with actual model.predict()` |
| `ai_engine/features/feature_pipeline.py` | Reads from S3 — not wired to Phase 5 DynamoDB feature store |
| `shared/features/feature_reader.py` | ✅ Fully implemented (Phase 5) |
| `shared/models/feature_set.py` | ✅ Fully implemented (Phase 5) |
| `data_ingestion/features/feature_archiver.py` | ✅ EOD S3 parquet archival operational |
| Signal path | `strategy_engine → signals.pending → risk_engine → signals.approved → execution_engine` |
| ML on signal path | ❌ No ML insertion point exists |

---

## 3. Architecture Decision: Where Does ML Sit?

### Option A — Inline in Risk Engine (REJECTED)

Enrich signal inside `risk_engine/service.py` before validation.

**Problems**:
- Violates single-responsibility. Risk engine becomes a ML inference host.
- Adds latency to the kill-switch-critical risk path.
- Can't independently scale or restart inference without restarting risk validation.

### Option B — New `signals.enriched` Kafka Topic + Dedicated Consumer Group (SELECTED)

Insert a new processing step between strategy signal publication and risk consumption:

```
strategy_engine
    └──▶  signals.pending  (existing, unchanged)
              │
              └──▶  ai_engine (NEW consumer group: aiengine-v1)
                        │
                        └──▶  signals.enriched  (NEW topic)
                                  │
                                  └──▶  risk_engine (switches from signals.pending → signals.enriched)
```

**Why this is right**:
- ai_engine failure → risk_engine falls back to `signals.pending` (fail-open on ML, fail-closed
  on risk). No trading halt due to ML service outage.
- Zero changes to strategy_engine.
- ai_engine scales independently (its own EC2 ASG).
- Clear audit trail: `signals.enriched` has all enrichment fields logged alongside original signal.

### Option C — HTTP Request from Risk Engine to AI Engine (REJECTED)

Risk engine makes a synchronous HTTP call to ai_engine per signal.

**Problems**:
- Adds synchronous blocking dependency in the critical risk path.
- ML service restart → risk engine throws, triggering circuit breaker.
- HTTP adds 2–5ms per signal in the same AZ; Kafka is 0.5–1ms.

---

## 4. Scope

### In scope

| # | Component | Description |
|---|-----------|-------------|
| 6-1 | `RegimeClassifier` | HMM-based regime detector: trending / ranging / volatile / crash |
| 6-2 | `SignalQualityScorer` | Gradient-boosted tree scoring signal quality 0.0–1.0 |
| 6-3 | `ModelRegistry` (real) | Replace S3 TODO stubs with working boto3 + joblib deserialization |
| 6-4 | `FeaturePipeline` (real) | Replace S3 read with Phase 5 `FeatureReader` (DynamoDB online store) |
| 6-5 | `SignalEnricher` | New class: consumes `signals.pending`, enriches, publishes `signals.enriched` |
| 6-6 | `KafkaSignalConsumer` (ai_engine) | New Kafka consumer (aiengine-v1) for signals.pending |
| 6-7 | `KafkaEnrichedPublisher` | New Kafka publisher to signals.enriched |
| 6-8 | `signals.enriched` topic | New Kafka topic, 4 partitions, key=symbol |
| 6-9 | Risk engine topic switch | `risk_engine` switches from `signals.pending` → `signals.enriched` |
| 6-10 | Fallback mechanism | If `signals.enriched` is stale (ai_engine lag > 2s), risk_engine reads `signals.pending` |
| 6-11 | Strategy selector agent | Read-only advisory agent: `claude-3-5-haiku-20241022` via Anthropic SDK |
| 6-12 | Terraform | EC2 ASG for ai_engine, IAM policy for DynamoDB features + S3 model_artifacts |
| 6-13 | Tests | Unit: RegimeClassifier, SignalQualityScorer, SignalEnricher; Integration: enrichment pipeline |

### Out of scope

- Model training (offline, not part of Phase 6 service code)
- GARCH/ML volatility forecaster (Phase 7 — latency enhancement, not signal quality)
- US feature store integration (deferred from Phase 5, still no Alpaca candle stream)
- A/B framework (Phase 6 delivers shadow mode via `enrichment_mode` config flag, full A/B in Phase 7)
- Real-time Kafka feature stream (`features.computed` topic — still unnecessary at ~30 signals/day)

---

## 5. Component Design

### 5.1 RegimeClassifier

**Model**: Hidden Markov Model (HMM) with 4 hidden states, trained offline on NSE daily
returns + volatility. Serialized with `joblib`. Loaded at service start.

**Inputs** (from Phase 5 `FeatureSet`):
- `rsi_14`, `adx_14`, `atr_14`, `macd`, `volume_ratio`

**Outputs**:
```python
@dataclass(frozen=True)
class RegimeOutput:
    regime: Literal["trending", "ranging", "volatile", "crash"]
    confidence: float          # 0.0–1.0
    computed_at: datetime
```

**Degradation**: If FeatureReader returns `None` (stale or missing), regime defaults to
`"unknown"`. Signal is still forwarded to `signals.enriched` — regime is advisory.

**File**: `services/ai_engine/classifiers/regime_classifier.py`

---

### 5.2 SignalQualityScorer

**Model**: LightGBM (or sklearn GradientBoostingClassifier). Trained offline on historical
signal outcomes (did signal result in profitable fill at SL/TP level within N bars?).
Serialized with `joblib`.

**Inputs** (all from Phase 5 `FeatureSet`):
- `rsi_14`, `ema_9`, `ema_21`, `vwap`, `atr_14`, `adx_14`, `macd`, `macd_signal`, `macd_hist`, `volume_ratio`
- Plus signal metadata: `direction` (encoded 1/-1), `candle_interval`

**Output**: `quality_score: float` in [0.0, 1.0]

**Soft filter threshold** (configurable via DynamoDB strategy-config table):
- `quality_score < threshold` → signal is forwarded with `filtered=True` flag
- Risk engine respects `filtered=True` by rejecting the signal
- `threshold` defaults to `0.0` (no filtering) — operator must explicitly raise it after paper validation

**Degradation**: Feature read failure → `quality_score = 0.5` (neutral, no filter). Always log.

**File**: `services/ai_engine/scorers/signal_quality_scorer.py`

---

### 5.3 ModelRegistry (real implementation)

Replaces the current placeholder. Key changes:

```python
# Before (placeholder)
model = None  # Placeholder

# After (real)
import joblib
response = self._s3_client.get_object(Bucket=self._s3_bucket, Key=s3_key)
model = joblib.load(io.BytesIO(response["Body"].read()))
```

**Model artifact layout on S3** (`model_artifacts` bucket):
```
models/
  regime_classifier/
    v1/
      model.joblib          # HMM model
      features.json         # ["rsi_14", "adx_14", ...]
      metadata.json         # {"trained_at": "...", "accuracy": 0.78, ...}
    latest -> v1            # symlink via DynamoDB pointer record
  signal_quality_scorer/
    v1/
      model.joblib
      features.json
      metadata.json
    latest -> v1
```

**Hot reload**: A background task polls `models/{name}/latest_version` from DynamoDB every
60 seconds. If version changes, model is reloaded in background without blocking inference.
New version becomes active atomically via lock-free `_active_model` pointer swap.

**File**: `services/ai_engine/models/model_registry.py` (real implementation)

---

### 5.4 FeaturePipeline (real implementation)

Current `feature_pipeline.py` reads features from S3. Replace with Phase 5 `FeatureReader`
(DynamoDB online store, interval-aware staleness).

```python
# Before (reads from S3 — stale, wrong store)
features = await self._s3_client.get_object(...)

# After (reads from DynamoDB feature store — live, Phase 5)
from shared.features.feature_reader import FeatureReader
feature_set = await self._feature_reader.get_latest(symbol, market, interval)
```

**File**: `services/ai_engine/features/feature_pipeline.py` (replace S3 read with FeatureReader)

---

### 5.5 SignalEnricher

The core orchestrator for the enrichment step.

```python
@dataclass(frozen=True)
class EnrichedSignal:
    # All original Signal fields (pass-through)
    signal_id: str
    strategy_name: str
    symbol: str
    market: str
    direction: str
    price: float
    generated_at: datetime
    expires_at: datetime
    paper_trade: bool
    trace_id: str

    # Enrichment fields (new in Phase 6)
    regime: str                    # "trending" | "ranging" | "volatile" | "crash" | "unknown"
    regime_confidence: float       # 0.0–1.0
    quality_score: float           # 0.0–1.0 (0.5 = degraded/unknown)
    filtered: bool                 # True if quality_score < threshold
    enriched_at: datetime
    enrichment_latency_ms: float   # for observability
```

**Enrichment pipeline** (per signal):
```
1. Read FeatureSet from DynamoDB (FeatureReader.get_latest)         ~3-5ms
2. Classify regime (RegimeClassifier.classify)                       ~1ms
3. Score signal quality (SignalQualityScorer.score)                  ~1ms
4. Resolve filter threshold from strategy-config DynamoDB            cached
5. Build EnrichedSignal                                              <0.1ms
6. Publish to signals.enriched                                       ~1ms
                                                                   --------
Total enrichment budget:                                            ~6-8ms
```

**Non-fatal design**: every step is wrapped individually. FeatureReader failure → regime
`"unknown"`, quality_score `0.5`, filtered `False`. Signal always flows through.

**File**: `services/ai_engine/enrichment/signal_enricher.py`

---

### 5.6 Kafka Consumer + Publisher (ai_engine)

**Consumer** (`aiengine-v1` group) subscribes to `signals.pending`:
```python
# services/ai_engine/consumers/kafka_signal_consumer.py
# Consumer group: aiengine-v1
# Subscribes to: signals.pending
# Parses: same schema as risk_engine's KafkaSignalConsumer (reuse shared parsing)
```

**Publisher** publishes to `signals.enriched`:
```python
# services/ai_engine/publishers/kafka_enriched_publisher.py
# Topic: signals.enriched
# Key: symbol (same partitioning as signals.pending — preserves ordering per symbol)
# Schema: EnrichedSignal as JSON, schema_version=4.0
```

**Consumer group naming**: `aiengine-v1` (follows existing convention: `strategy-v1`, `risk-v1`, `execution-v1`)

---

### 5.7 Risk Engine Topic Switch

`risk_engine` currently consumes `signals.pending`. After Phase 6 it switches to
`signals.enriched`.

**Migration approach** (zero-downtime):
1. Deploy ai_engine with `aiengine-v1` consuming `signals.pending`.
2. ai_engine begins publishing to `signals.enriched`.
3. Verify `signals.enriched` has expected throughput (monitoring, N hours paper mode).
4. Switch risk_engine's consumer to `signals.enriched`.
5. `signals.pending` consumers are now: `aiengine-v1` only.

**Fallback mechanism** (automatic and deterministic):

`risk_engine` maintains a **consumer-lag watchdog** that runs every 500ms:

```python
# risk_engine/consumers/enrichment_watchdog.py
#
# Compares:
#   signals.enriched  consumer-lag (aiengine-v1 group offset vs signals.pending latest offset)
#
# If lag > LAG_THRESHOLD (default: 2s worth of expected messages) for WINDOW consecutive checks:
#   → risk_engine automatically falls back to consuming signals.pending
#   → CloudWatch alarm fires: QuantEmbrace/RiskEngine/EnrichmentFallbackActive = 1
#   → structured log: event="enrichment_fallback.activated"
#
# When lag returns to 0 and ai_engine is producing again:
#   → risk_engine automatically re-enables signals.enriched after RECOVERY_WINDOW checks
#   → structured log: event="enrichment_fallback.recovered"
```

Lag is measured by comparing Kafka consumer group offsets (MSK API: `list_consumer_group_offsets`).
**No operator action is required for routine ai_engine restarts** — the watchdog detects the outage
within 1s and recovers automatically. Manual override (DynamoDB `enrichment_required=false` flag)
is a secondary path for operator-forced rollback only.

| Parameter | Default | Tunable via DynamoDB strategy-config |
|-----------|---------|--------------------------------------|
| `LAG_THRESHOLD` | 10 messages | Yes |
| `WINDOW` (checks before fallback) | 2 | Yes |
| `RECOVERY_WINDOW` (checks before re-enable) | 5 | Yes |

---

### 5.8 Strategy Selector Agent

**Design**: Read-only advisory tool. Never activates or deactivates strategies autonomously.

**Trigger**: Runs once per NSE trading session (post-market, ~15:45 IST) and once at
session open (09:00 IST). Not on the hot signal path.

**Inputs**:
- Current regime distribution for the day (from DynamoDB regime log)
- Per-strategy circuit breaker states (from strategy-config DynamoDB)
- Per-strategy P&L for past 5 days (from fills DynamoDB table)
- Current portfolio NAV and sector exposure (from risk analytics DynamoDB)

**Model**: Claude Haiku (`claude-3-5-haiku-20241022`) via Anthropic SDK.

**Output**: A human-readable recommendation written to DynamoDB:
```
{
  "recommended_action": "Disable VWAPReversion — ADX below 20 for 3 consecutive sessions, ranging regime detected.",
  "affected_strategies": ["VWAPReversionStrategy"],
  "reasoning": "...",
  "generated_at": "...",
  "session": "2026-05-08-NSE"
}
```

**Operator workflow**: recommendation is surfaced in `scripts/strategy/config.py status`
output. Operator reviews and runs `scripts/strategy/config.py disable VWAPReversionStrategy`
manually if they agree.

**File**: `services/ai_engine/agents/strategy_selector.py`

**Why Haiku and not a custom model**: The strategy selector needs to reason over structured
tabular data and produce actionable prose. LLM is better suited than a trained classifier
here — the action space is small but the reasoning context is rich. Haiku is fast (~300ms)
and cheap. No private trading data leaves the prompt (only strategy names, P&L aggregates,
and regime labels).

---

## 6. New Kafka Topic

| Topic | Partitions | Retention | Key | Schema Version |
|-------|-----------|-----------|-----|----------------|
| `signals.enriched` | 4 | 1h | symbol | 4.0 |

Retention is 1h (signals expire in 30s; 1h is generous for debugging). 4 partitions matches
`signals.pending` — enables partition-aligned consumption.

---

## 7. Terraform Changes

### 7.1 EC2 ASG for ai_engine

The existing `ai_engine` has no EC2 ASG (only a FastAPI skeleton). Phase 6 adds it to
`infra/terraform/modules/ec2_services/`:

```hcl
module "ai_engine" {
  source          = "../../modules/ec2_services"
  service_name    = "ai-engine"
  instance_type   = "c6g.large"    # ARM64, 2vCPU/4GB — sufficient for in-process joblib inference
  min_size        = 1
  max_size        = 2
  kafka_bootstrap = var.kafka_bootstrap_servers
}
```

`c6g.large` is chosen over `t4g` because joblib HMM inference is CPU-bound
(~1ms but requires sustained throughput at signal rate).

### 7.2 IAM Policy additions

**ai_engine** needs:
- `DynamoDBFeaturesRead` (already defined in Phase 5 — attach to ai_engine role)
- `S3ModelArtifactsRead` — new: `s3:GetObject` on `arn:aws:s3:::${model_artifacts_bucket}/models/*`
- `DynamoDBStrategyConfigRead` — `dynamodb:GetItem` on strategy-config table (for filter threshold)
- `DynamoDBRegimeLogWrite` — `dynamodb:PutItem` on regime-log table (new in Phase 6)

### 7.3 New DynamoDB Table: `regime-log`

Captures per-signal regime classification for model evaluation and agent input:

```
PK: REGIME#{market}#{symbol}
SK: SESSION#{date}T{candle_time_iso}
Attributes: regime, confidence, quality_score, filtered, enrichment_latency_ms
TTL: 30 days
```

---

## 8. Signal Schema Evolution (v3.0 → v4.0)

`signals.pending` (published by strategy_engine) remains **v3.0 — no changes**.

`signals.enriched` (published by ai_engine) is a new schema **v4.0** that extends v3.0:

```json
{
  "schema_version": "4.0",
  "signal_id": "...",
  "strategy_name": "...",
  "symbol": "RELIANCE",
  "market": "NSE",
  "direction": "BUY",
  "price": 2450.50,
  "generated_at": "2026-05-08T09:17:00Z",
  "expires_at": "2026-05-08T09:17:30Z",
  "paper_trade": false,
  "trace_id": "...",

  "regime": "trending",
  "regime_confidence": 0.82,
  "quality_score": 0.74,
  "filtered": false,
  "enriched_at": "2026-05-08T09:17:00.006Z",
  "enrichment_latency_ms": 6.2
}
```

Risk engine parses both v3.0 (fallback) and v4.0 (primary). If `schema_version` is absent
or `"3.0"`, enrichment fields are set to defaults (regime=`"unknown"`, quality_score=`0.5`,
filtered=`False`).

---

## 9. Latency Budget

The enrichment step adds ~6–8ms to the signal path:

| Hop | Before Phase 6 | After Phase 6 |
|-----|---------------|---------------|
| Tick → signals.pending (Kafka) | ~5ms | ~5ms (unchanged) |
| signals.pending → signals.enriched (ai_engine) | — | ~6–8ms (new) |
| signals.enriched → risk decision (DynamoDB) | ~15ms | ~15ms (unchanged) |
| Risk → execution (Kafka) | ~5ms | ~5ms (unchanged) |
| Execution → broker API | ~88ms | ~88ms (unchanged) |
| **Total** | **~113ms** | **~119–121ms** |

This is within Phase 6 tolerance. Phase 7 will optimize risk DynamoDB reads to Redis (~1ms)
which recovers the 6–8ms and more.

---

## 10. Observability

### New CloudWatch Metrics (namespace: `QuantEmbrace/AIEngine`)

| Metric | Unit | Alarm |
|--------|------|-------|
| `EnrichmentLatencyMs` (p50, p99) | ms | P99 > 20ms for 2min |
| `RegimeClassificationErrors` | count | > 0 per 5min |
| `QualityFilterRate` | percent | > 50% filtered for 5min (model drift signal) |
| `ModelHotReloadCount` | count | informational |
| `SignalsEnrichedCount` | count | 0 for 15min during market hours |

### Structured Log Fields (every enriched signal)

```json
{
  "event": "signal.enriched",
  "signal_id": "...",
  "symbol": "RELIANCE",
  "regime": "trending",
  "regime_confidence": 0.82,
  "quality_score": 0.74,
  "filtered": false,
  "enrichment_latency_ms": 6.2,
  "feature_staleness_ms": 1240,
  "model_versions": {"regime": "v1", "quality": "v1"}
}
```

---

## 11. Risks and Tradeoffs

| Risk | Severity | Mitigation |
|------|----------|-----------|
| ai_engine outage stops trading | HIGH | Fallback: risk_engine switches back to `signals.pending` within 60s via ops config |
| Model quality degrades silently | HIGH | QualityFilterRate alarm + weekly model performance review in regime-log |
| Feature staleness for enrichment | MEDIUM | FeatureReader staleness is already interval-aware; enrichment degrades gracefully to defaults |
| LLM prompt includes sensitive data | LOW | Strategy selector prompt contains only strategy names, P&L aggregates, regime labels — no raw prices or positions |
| joblib pickle deserialization attack | LOW | Models loaded only from private S3 bucket with IAM; no user-supplied model paths |
| signals.enriched topic lag | MEDIUM | Lag alarm → ops alert; fallback flag auto-clears ai_engine as bottleneck |
| Phase 6 breaks risk_engine on topic switch | MEDIUM | Risk engine supports dual-topic parse (v3.0 + v4.0) throughout migration; topic switch is config, not code |

---

## 12. Implementation Tasks

| Task | File(s) | Priority |
|------|---------|----------|
| PHASE6-001 | `shared/models/enriched_signal.py` — EnrichedSignal frozen dataclass | P0 |
| PHASE6-002 | `ai_engine/models/model_registry.py` — real S3 + joblib implementation | P0 |
| PHASE6-003 | `ai_engine/features/feature_pipeline.py` — replace S3 read with FeatureReader | P0 |
| PHASE6-004 | `ai_engine/classifiers/regime_classifier.py` — HMM wrapper + degradation | P0 |
| PHASE6-005 | `ai_engine/scorers/signal_quality_scorer.py` — GBT wrapper + degradation | P0 |
| PHASE6-006 | `ai_engine/enrichment/signal_enricher.py` — orchestrator | P0 |
| PHASE6-007 | `ai_engine/consumers/kafka_signal_consumer.py` — aiengine-v1 group | P0 |
| PHASE6-008 | `ai_engine/publishers/kafka_enriched_publisher.py` — signals.enriched | P0 |
| PHASE6-009 | `ai_engine/service.py` — replace FastAPI+HTTP with Kafka loop; retain /health | P0 |
| PHASE6-010 | `risk_engine` — add v4.0 schema parsing; add `enrichment_required` config | P1 |
| PHASE6-011 | `infra/terraform/modules/kafka/main.tf` — add `signals.enriched` topic | P1 |
| PHASE6-012 | `infra/terraform/modules/ec2_services/` — add ai_engine ASG + IAM | P1 |
| PHASE6-013 | `infra/terraform/modules/dynamodb/` — add `regime-log` table | P1 |
| PHASE6-014 | `ai_engine/agents/strategy_selector.py` — Claude Haiku advisory agent | P2 |
| PHASE6-015 | CloudWatch alarms (`QuantEmbrace/AIEngine` namespace) | P1 |
| PHASE6-016 | Unit tests: regime_classifier, signal_quality_scorer, signal_enricher | P0 |
| PHASE6-017 | Integration test: enrichment pipeline end-to-end (mock Kafka + DynamoDB) | P1 |

---

## 13. Migration Plan (from Phase 5)

### Step 1 — Deploy ai_engine with enrichment (no risk_engine change yet)
- Deploy PHASE6-001 through PHASE6-013
- `aiengine-v1` starts consuming `signals.pending`
- `signals.enriched` topic is live
- Risk engine still reads `signals.pending` (unchanged)
- Monitor `SignalsEnrichedCount` and `EnrichmentLatencyMs` for 3 trading days

### Step 2 — Enable quality filter at 0.0 threshold (no signals blocked)
- Set `quality_filter_threshold = 0.0` in strategy-config DynamoDB
- All signals pass through with `filtered = False`
- Validate `quality_score` distribution in CloudWatch Logs Insights over 5 sessions
- Confirm no regime classification or feature read errors

### Step 3 — Switch risk_engine to `signals.enriched`
- Update `risk_engine` Kafka consumer topic to `signals.enriched`
- Deploy in rolling update (existing `signals.pending` consumer group remains registered; lag will grow and then drain)
- Keep `enrichment_required = False` for 5 sessions (dual-schema fallback active)

### Step 4 — Raise quality filter threshold cautiously
- Start at 0.2 (paper mode only)
- Monitor `QualityFilterRate` — target < 20% of signals filtered
- Raise to 0.3 after 10 sessions with stable distribution
- Never raise above 0.5 without backtesting evidence

### Step 5 — Enable strategy selector agent (read-only)
- Deploy PHASE6-014
- Recommendations visible in `scripts/strategy/config.py status`
- No auto-execution. Operator-in-the-loop only.

---

## 14. Acceptance Criteria

- [ ] `signals.enriched` Kafka topic populated for every signal during paper mode
- [ ] Enrichment latency P99 < 15ms (measured via CloudWatch)
- [ ] Regime classification runs without errors for 5 consecutive trading sessions
- [ ] Quality score distribution is non-degenerate (not all 0.5 — confirms features are live)
- [ ] Risk engine correctly parses both v3.0 and v4.0 signal schemas
- [ ] Model hot-reload tested: upload new version to S3 → live within 60s without restart
- [ ] ai_engine failure simulation: risk_engine falls back to `signals.pending` within 60s
- [ ] Strategy selector agent produces a recommendation for at least one session (paper)
- [ ] All enrichment fields logged per signal in structured format
- [ ] Terraform: `terraform fmt -check` passes on all new/modified `.tf` files
- [ ] Unit + integration tests: all passing

---

## 15. Open Questions for Hari

Before approving this design, the following need a decision:

| # | Question | Options | Implication |
|---|----------|---------|-------------|
| Q1 | **Service mode**: should ai_engine remain a FastAPI HTTP service alongside the new Kafka loop, or become Kafka-only (removing HTTP entirely)? | **A**: Keep FastAPI + add Kafka loop (ai_engine stays HTTP-queryable for debugging) / **B**: Kafka-only (simpler, consistent with other services) | Recommend **B** (Kafka-only) — HTTP is unused in production; `/health` endpoint can be served by a lightweight asyncio HTTP server without FastAPI |
| Q2 | **Model bootstrap**: Phase 6 code is ready but models don't exist yet. Should I include **stub models** (sklearn DummyClassifier/DummyRegressor serialized as joblib) so the full pipeline runs in paper mode from day 1? Or defer model integration until trained models exist? | **A**: Include stub models (DummyClassifier returns "unknown", quality score = 0.5) / **B**: Skip model loading; enricher hardcodes defaults until real models land | Recommend **A** — stub models let the full pipeline run end-to-end and validate the architecture before real training |
| Q3 | **Quality filter default**: `threshold = 0.0` means no filtering in Phase 6. Should Phase 6 implement the filter mechanism but leave it disabled, or should filtering be behind a feature flag entirely? | **A**: Implement filter, default threshold=0.0 (no filtering) / **B**: Feature flag — filter code compiles only when `ENABLE_QUALITY_FILTER=true` | Recommend **A** — simpler code, threshold is the natural toggle |
| Q4 | **Strategy selector cadence**: once per session (post-market at 15:45 IST) or also at session open? | **A**: Once, post-market only / **B**: Pre-market + post-market | Recommend **A** — post-market only. Pre-market recommendations are less actionable (regime from yesterday) |
| Q5 | **Regime log retention**: 30 days proposed. Is that sufficient for model evaluation, or do you want 90 days (matches S3 IA tier)? | 30d / 60d / 90d | 30 days = ~30 NSE trading sessions. 90 days recommended if you plan to retrain models on regime labels |

---

## 16. Decision Needed

This document is ready for Hari's review. Please provide:

1. Answers to Q1–Q5 above (or confirm defaults are acceptable).
2. Any architectural concerns before implementation begins.
3. Explicit approval to proceed with Phase 6 implementation.

Once approved, implementation will proceed in the task order defined in §12.
