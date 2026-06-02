# Implementation Tasks — QuantEmbrace End-State System

## Status Summary

| Phase | Description | Status | Completed |
|---|---|---|---|
| Phase 1 | EC2 Backbone Migration | ✅ COMPLETE | 2026-04-29 |
| Phase 2 | Kafka Streaming Core | ✅ COMPLETE | 2026-05-03 |
| Phase 3 | Strategy Decoupling & Horizontal Scale | ✅ COMPLETE | 2026-05-05 |
| Phase 4 | Distributed Risk Engine + Portfolio Layer | ✅ COMPLETE | 2026-05-06 |
| Phase 5 | Data Platform + Feature Store | ✅ COMPLETE | 2026-05-07 |
| Phase 6 | ML + Agentic Layer | 🔲 ACTIVE | — |
| Phase 7 | Latency Optimization + Smart Order Router | 🔲 PLANNED | — |

**Test health:** 277/277 passing as of Phase 5 completion.

---

## ✅ Phases 1–5 Completed Tasks (Reference)

All tasks for Phases 1–5 are complete. Key deliverables per phase:

- **Phase 1:** EC2 ARM64 ASGs, Terraform `ec2_services` module, ADR-009
- **Phase 2:** MSK Serverless Kafka, all 4 services Kafka-native, SQS permanently removed, ADR-011
- **Phase 3:** `StrategyRunner` + circuit breakers, `DynamoCandleConsumer`, `StrategyConfigLoader` hot-reload, `paper_trade` pipeline end-to-end, ADR-013
- **Phase 4:** `KillSwitchCache` (in-memory, 1s poll), `RiskContextBuilder` (7 parallel DynamoDB reads), `SpreadGateValidator`, `SectorConcentrationValidator`, `LiquidityValidator`, `RiskAnalyticsEngine` (VaR+sector+NAV background loop), `InstrumentRegistry`, ADR-014
- **Phase 5:** `FeatureEngine` (9 features: RSI-14, EMA-9/21, VWAP, ATR-14, ADX-14, MACD/signal/hist, volume_ratio), `FeatureWriter` (dual DynamoDB writes), `FeatureReader` (interval-aware staleness), `FeatureArchiver` (POST_CLOSE → S3 Parquet), service wiring in `data_ingestion/service.py`

**Known deviations from original spec:**
- Phase 4: ElastiCache Redis and active-active dual-instance Risk Engine were **not implemented** (ADR-014). Replaced by in-memory `KillSwitchCache`. Redis/HA deferred to Phase 7 if latency targets require it.
- Phase 5: US feature store deferred to Phase 6. Feature set is 9 features (not the original 5 feature groups). Dual DynamoDB writes use `SK=LATEST` (TTL 24h) + `SK=CANDLE#{ts}` (TTL 7d).

---

## 🔲 Phase 6 — ML + Agentic Layer

**Prerequisite:** Phase 5 complete ✅
**Architecture ref:** `architecture/phase6_design_review.md` (to be created)
**Estimated effort:** 3–4 sessions

### Pre-Phase 6 Prerequisite

- [ ] **PHASE6-PRE-001** — Write 252-day NSE feature backfill script
  - `scripts/backtest/backfill_features.py` — reads S3 historical tick Parquet, runs `FeatureEngine` per candle, writes to offline S3 Parquet `{date}/{feature_group}/{instrument}`
  - Required before ML model training can begin
  - Acceptance: backfill completes for all instruments in `configs/instruments.yaml` without errors; S3 partition count matches expected trading days

- [ ] **PHASE6-PRE-002** — Add Alpaca candle stream to `data_ingestion`
  - `data_ingestion/candle_stream_us.py` — mirrors `IntradayCandleStream` for US equities via Alpaca bar WebSocket
  - Writes to `candle-cache` DynamoDB with `market=US`
  - Wires `FeatureEngine` to US candles (deferred from Phase 5)
  - Acceptance: US features appear in `feature-store-online` within 2s of Alpaca bar close; `test_candle_stream_us.py` ≥ 20 tests passing

### Task 1 — Signal Enrichment Service (Core)

- [ ] **PHASE6-001** — Create `signal_enrichment` service skeleton
  - `services/signal_enrichment/__init__.py`, `main.py`, `service.py`
  - Kafka consumer group `enrichment-v1` consuming `signals.pending`
  - Kafka producer publishing to new topic `signals.enriched`
  - Kill-switch listener task (same pattern as other services)
  - `shared/config/settings.py` — add `signal_enrichment` config block
  - Acceptance: service starts, consumes from `signals.pending`, publishes to `signals.enriched` with passthrough (no enrichment yet); kill switch halts consumption

- [ ] **PHASE6-002** — Add `signals.enriched` Kafka topic
  - `scripts/kafka/create_topics.py` — add `signals.enriched` (2 partitions, key=instrument_id, 7-day retention)
  - Update `architecture/system_design.md` Kafka topic table
  - Acceptance: topic created idempotently; `--verify-only` flag confirms topic exists

- [ ] **PHASE6-003** — Remap Risk Engine consumer from `signals.pending` → `signals.enriched`
  - `risk_engine/consumers/kafka_signal_consumer.py` — change topic subscription from `signals.pending` to `signals.enriched`
  - Consumer group remains `risk-v1`
  - Acceptance: end-to-end signal flow works: strategy → `signals.pending` → enrichment-v1 → `signals.enriched` → risk-v1 → `signals.approved` → execution-v1; integration test confirms no signals lost

### Task 2 — ML Model Infrastructure

- [ ] **PHASE6-004** — Define `EnrichedSignal` pydantic model
  - `shared/models/enriched_signal.py` — extends `Signal` with: `regime: str | None`, `volatility_forecast: float | None`, `quality_score: float | None`, `model_version: str | None`, `enrichment_latency_ms: float | None`
  - `to_dict()` / `from_dict()` with schema_version `4.0`
  - Acceptance: model serializes/deserializes round-trip; `None` fields omitted from Kafka payload; 15+ unit tests

- [ ] **PHASE6-005** — ONNX model loader with hot-reload
  - `signal_enrichment/models/model_loader.py` — `ONNXModelLoader`: loads model from S3 path `s3://quantembrace-model-artifacts/models/{name}/{version}/model.onnx`; polls S3 for new version every 60s; hot-swaps model without service restart; thread-safe swap via `asyncio.Lock`
  - Acceptance: model loads on startup; new version uploaded to S3 → loaded within 60s; old version continues serving during swap; `test_model_loader.py` ≥ 20 tests (load, hot-reload, S3 error graceful degradation)

- [ ] **PHASE6-006** — Regime Classifier integration
  - `signal_enrichment/models/regime_classifier.py` — wraps ONNX Runtime session; input: feature vector from `FeatureReader`; output: `{trending, ranging, volatile, crash}`; inference ≤ 5ms
  - `signal_enrichment/service.py` — call classifier per signal; attach `regime` to `EnrichedSignal`
  - Acceptance: Hypothesis PBT verifies inference latency < 5ms across 500 valid feature vectors; `test_regime_classifier.py` ≥ 15 tests

- [ ] **PHASE6-007** — Volatility Forecaster integration
  - `signal_enrichment/models/volatility_forecaster.py` — wraps ONNX Runtime; output: predicted next-hour realized volatility (float)
  - Acceptance: output is non-negative float; graceful `None` on model unavailable; 10+ unit tests

- [ ] **PHASE6-008** — Signal Quality Scorer integration
  - `signal_enrichment/models/quality_scorer.py` — wraps ONNX Runtime; output: confidence score 0.0–1.0
  - `signal_enrichment/service.py` — IF score < configured threshold: drop signal, log `SIGNAL_FILTERED_LOW_CONFIDENCE` to `ops.audit` with signal_id, strategy, score
  - Acceptance: signals below threshold never reach `signals.enriched`; `ops.audit` event logged with correct fields; 15+ unit tests

### Task 3 — Risk Engine Enrichment Integration

- [ ] **PHASE6-009** — Regime-driven position sizing in Risk Engine
  - `risk_engine/validators/regime_validator.py` — reads `regime` from `EnrichedSignal`; IF `volatile` or `crash`: apply 50% reduction to `max_position_size` in `RiskContext`
  - `risk_engine/validators/volatility_sizing_validator.py` — reads `volatility_forecast`; scales approved position size proportionally to inverse of predicted volatility
  - Acceptance: Hypothesis PBT verifies position size is always ≤ original limit; volatile/crash regime always produces ≤ 50% of base size; 20+ unit tests

### Task 4 — A/B Shadow Mode

- [ ] **PHASE6-010** — Shadow mode framework
  - `signal_enrichment/shadow/shadow_runner.py` — runs challenger model in parallel with production model; logs both outputs to S3 `trading-logs/shadow/{date}/` without affecting live signal path
  - Config flag `shadow_mode_enabled` in DynamoDB `strategy-config` (hot-reloaded)
  - Acceptance: shadow outputs logged to S3 with production and challenger scores side-by-side; live signal path unaffected when shadow model errors; 15+ unit tests

### Task 5 — Enrichment Logging

- [ ] **PHASE6-011** — Per-signal enrichment audit log
  - `signal_enrichment/logging/enrichment_logger.py` — writes feature values used for inference, model version, and enrichment outputs to S3 `trading-logs/enrichment/{date}/` as Parquet
  - S3 lifecycle: transition to Glacier Instant Retrieval after 30 days
  - Acceptance: every enriched signal produces an S3 audit record; lifecycle rule applied in `s3/main.tf`; 10+ unit tests

### Task 6 — Read-Only Agents

- [ ] **PHASE6-012** — Strategy Selector Agent (read-only)
  - `signal_enrichment/agents/strategy_selector.py` — reads regime classification + current position state from DynamoDB; logs recommended strategy activations/deactivations to `ops.audit`; makes NO autonomous changes
  - Acceptance: agent logs recommendations but never calls `strategy_config_loader.set_enabled()`; 10+ unit tests verifying read-only constraint

- [ ] **PHASE6-013** — Parameter Tuner Agent (read-only)
  - `signal_enrichment/agents/parameter_tuner.py` — monitors rolling 20-day Sharpe ratio per strategy from `risk-analytics` DynamoDB; logs parameter adjustment suggestions to `ops.audit`; makes NO autonomous changes
  - Acceptance: agent logs suggestions but never writes to `strategy-config` DynamoDB; 10+ unit tests

### Task 7 — Infrastructure

- [ ] **PHASE6-014** — Terraform: Signal Enrichment Service EC2 ASG
  - `infra/terraform/modules/ec2_services/` — add `signal_enrichment` service: c6g.large, ASG min=1 max=1, dedicated IAM role, security group
  - IAM policy: S3 read (`model-artifacts`), S3 write (`trading-logs`), DynamoDB read (`feature-store-online`, `strategy-config`), Kafka produce (`signals.enriched`), Kafka consume (`signals.pending`)
  - Acceptance: `terraform plan` shows no errors; service starts on EC2 with correct IAM permissions

- [ ] **PHASE6-015** — CloudWatch alarms for Signal Enrichment Service
  - `infra/terraform/modules/monitoring/main.tf` — add alarms: CPU > 90%, memory > 85%, enrichment error rate > 5%/5min, enrichment latency P99 > 10ms
  - Acceptance: alarms visible in CloudWatch; test alarm fires correctly

### Task 8 — Phase 6 Validation Gate

- [ ] **PHASE6-016** — End-to-end integration test: enriched signal flow
  - `tests/integration/test_signal_enrichment_pipeline.py` — full pipeline: strategy → `signals.pending` → enrichment → `signals.enriched` → risk → `signals.approved`; mock ONNX models; verify `EnrichedSignal` fields propagate correctly; verify low-quality signals are dropped
  - Acceptance: all integration tests pass in < 60s using LocalStack + local Kafka

- [ ] **PHASE6-017** — Update architecture docs
  - `architecture/system_design.md` — add Signal Enrichment Service to service boundaries table and Kafka topic table
  - `architecture/infra_diagram.md` — add new service, EC2 instance, S3 paths, cost delta
  - `memory/decisions.md` — record ADR-015 for Phase 6 ML architecture decisions

---

## 🔲 Phase 7 — Latency Optimization + Smart Order Router

**Prerequisite:** Phase 6 complete
**Architecture ref:** `architecture/phase7_design_review.md` (to be created)
**Estimated effort:** 3–4 sessions

### Task 1 — Execution Engine Latency Hardening

- [ ] **PHASE7-001** — Persistent HTTP/2 connections to brokers
  - `execution_engine/adapters/zerodha_broker.py` — replace per-request `requests.Session` with `httpx.AsyncClient` (HTTP/2, connection pooling, keepalive)
  - `execution_engine/adapters/alpaca_adapter.py` — same pattern for Alpaca
  - Acceptance: no TLS handshake on consecutive order placements; latency histogram shows improvement; 10+ unit tests with mock HTTP/2 server

- [ ] **PHASE7-002** — EC2 network tuning for Execution Engine
  - `infra/terraform/modules/ec2_services/` — upgrade execution_engine to `c6gn.large` (network-optimized Graviton)
  - Startup script: `taskset -c 0 python -m services.execution_engine.main` (CPU core 0 pinning)
  - Acceptance: `taskset -p $(pgrep -f execution_engine)` confirms core 0 affinity; latency P50 measurably lower

- [ ] **PHASE7-003** — Kafka priority lanes for high-conviction signals
  - `execution_engine/consumers/kafka_signal_consumer.py` — add dedicated poll loop for partition 0 of `signals.approved` (reserved for high-conviction signals, confidence > threshold)
  - `risk_engine/publishers/kafka_signal_publisher.py` — route high-conviction signals to partition 0
  - Acceptance: high-conviction signals processed before standard signals under load; Hypothesis PBT verifies no signal starvation

### Task 2 — Smart Order Router

- [ ] **PHASE7-004** — SOR base class and order splitting model
  - `execution_engine/sor/base.py` — `SmartOrderRouter` abstract base; `ParentOrder`, `ChildOrder` pydantic models; `SORResult` with child order list and execution plan
  - Acceptance: models serialize/deserialize correctly; 10+ unit tests

- [ ] **PHASE7-005** — TWAP algorithm
  - `execution_engine/sor/twap.py` — splits parent order into N equal child orders over configurable time window; child order count and window configurable per signal
  - Hypothesis PBT: for any valid (quantity, window, n_slices), sum of child quantities == parent quantity and no child quantity is zero
  - Acceptance: PBT passes with 500 examples; 15+ unit tests including edge cases (odd quantities, single slice)

- [ ] **PHASE7-006** — VWAP algorithm
  - `execution_engine/sor/vwap.py` — times child orders to match instrument's historical intraday volume profile sourced from offline feature store S3 Parquet
  - Acceptance: child order schedule matches volume profile within 5%; graceful fallback to TWAP if volume profile unavailable; 15+ unit tests

- [ ] **PHASE7-007** — Iceberg algorithm
  - `execution_engine/sor/iceberg.py` — submits 20% of total quantity as visible tranche; on fill confirmation, submits next tranche within 100ms
  - WHEN kill switch activated during Iceberg execution: cancel all outstanding tranches within 500ms
  - Acceptance: tranche submission latency < 100ms measured in unit test; kill switch cancellation tested; 15+ unit tests

- [ ] **PHASE7-008** — Venue router (NSE vs BSE)
  - `execution_engine/sor/venue_router.py` — for instruments available on both NSE and BSE, routes to venue with tighter bid-ask spread at order submission time (reads from `LiveQuotePoller` DynamoDB cache)
  - Acceptance: routes to NSE when NSE spread < BSE spread; routes to BSE otherwise; graceful fallback to NSE when BSE quote unavailable; 10+ unit tests

- [ ] **PHASE7-009** — Wire SOR into Execution Engine
  - `execution_engine/service.py` — route signals with `sor_type` field to appropriate SOR algorithm; standard signals bypass SOR
  - SOR paper trading validation: all SOR algorithms default `paper_trade=True`; require 5 consecutive clean sessions before live promotion
  - Acceptance: SOR signals routed correctly; non-SOR signals unaffected; integration test covers TWAP + Iceberg paper flow

### Task 3 — OpenTelemetry Instrumentation

- [ ] **PHASE7-010** — OTel span instrumentation across all services
  - `shared/telemetry/otel.py` — `init_tracer()`, `start_span()`, `inject_context()`, `extract_context()` helpers
  - Instrument each Kafka hop: tick-to-strategy, strategy-to-enrichment, enrichment-to-risk, risk-validation, risk-to-execution, execution-to-broker-API
  - Propagate existing `trace_id` from Kafka envelope as OTel trace context
  - Acceptance: single `trace_id` query in CloudWatch Logs Insights reconstructs full lifecycle with microsecond timestamps at each hop

- [ ] **PHASE7-011** — CloudWatch latency metrics and Grafana dashboard
  - `shared/telemetry/metrics.py` — publish per-hop latency to CloudWatch namespace `QuantEmbrace/Latency` (high-resolution, 1s granularity)
  - `infra/terraform/modules/monitoring/main.tf` — add P99 latency alarms per hop (alert threshold configurable); Grafana dashboard JSON with P50/P95/P99 per hop, auto-refresh 10s
  - Acceptance: dashboard shows all 6 hops; alarm fires when P99 > threshold for 2 consecutive minutes

### Task 4 — Latency Validation

- [ ] **PHASE7-012** — Latency benchmark test suite
  - `tests/unit/execution_engine/test_latency_targets.py` — measure order-to-wire latency with mock Zerodha HTTP/2 server; assert P50 < 10ms, P99 < 25ms over 1000 iterations
  - Acceptance: benchmark passes consistently in CI; results logged to CloudWatch `QuantEmbrace/Latency/Benchmark`

### Task 5 — Phase 7 Infrastructure and Docs

- [ ] **PHASE7-013** — Terraform: upgrade Execution Engine to c6gn.large
  - `infra/terraform/modules/ec2_services/variables.tf` — parameterize instance type per service; set execution_engine default to `c6gn.large`
  - Acceptance: `terraform plan` shows instance type change; no other services affected

- [ ] **PHASE7-014** — Update architecture docs
  - `architecture/system_design.md` — add SOR to execution layer description; update latency hop table with Phase 7 targets vs actuals
  - `architecture/infra_diagram.md` — update execution_engine instance type, add OTel/Grafana, cost delta
  - `memory/decisions.md` — record ADR-016 for Phase 7 latency decisions

---

## 🔲 Open Tasks (Cross-Phase)

These tasks are not phase-gated but should be completed before production go-live:

- [ ] **TASK-002** — Alpaca `AlpacaAdapter` full implementation (P0)
  - `services/execution_engine/adapters/alpaca_adapter.py` — full `BrokerAdapter` interface
  - Secrets Manager auth, paper mode via env var, WebSocket market data, order CRUD, normalized `Position` objects
  - Integration test: place paper trade, verify fill
  - **Blocks:** Phase 6 US feature store (PHASE6-PRE-002)

- [ ] **TASK-008** — Integration tests for full order flow (P2)
  - `tests/integration/test_order_flow.py` — 10 scenarios: approved signal → order, rejected signal → no order, kill switch → halt, duplicate signal → idempotent, circuit breaker, cancellation, position update on fill, graceful shutdown
  - LocalStack + local Kafka; all tests < 60s

- [ ] **TASK-009** — Monitoring dashboard (P2)
  - Grafana dashboard (or CloudWatch) covering: ECS/EC2 health, trading activity (signals/orders/fills), risk metrics (P&L, drawdown, kill switch status), data quality (tick rate, WebSocket status, S3 write rate)
  - Auto-refresh ≤ 30s; accessible via URL

---

## Task Dependency Graph

```
Phase 5 complete ✅
        │
        ├──► PHASE6-PRE-001 (feature backfill)
        │         │
        │         └──► PHASE6-006 (regime classifier — needs trained model)
        │
        ├──► PHASE6-PRE-002 (Alpaca candle stream) ◄── TASK-002 (Alpaca adapter)
        │
        ├──► PHASE6-001 (enrichment service skeleton)
        │         │
        │         ├──► PHASE6-002 (signals.enriched topic)
        │         │         │
        │         │         └──► PHASE6-003 (remap risk engine consumer)
        │         │
        │         ├──► PHASE6-004 (EnrichedSignal model)
        │         │         │
        │         │         ├──► PHASE6-005 (ONNX model loader)
        │         │         │         ├──► PHASE6-006 (regime classifier)
        │         │         │         ├──► PHASE6-007 (volatility forecaster)
        │         │         │         └──► PHASE6-008 (quality scorer)
        │         │         │                   │
        │         │         │                   └──► PHASE6-009 (risk engine integration)
        │         │         │
        │         │         └──► PHASE6-010 (shadow mode)
        │         │
        │         ├──► PHASE6-011 (enrichment logging)
        │         ├──► PHASE6-012 (strategy selector agent)
        │         └──► PHASE6-013 (parameter tuner agent)
        │
        ├──► PHASE6-014 (Terraform EC2 ASG)
        ├──► PHASE6-015 (CloudWatch alarms)
        ├──► PHASE6-016 (integration test)
        └──► PHASE6-017 (architecture docs)
                  │
                  └──► Phase 6 complete
                              │
                              ├──► PHASE7-001 (HTTP/2 connections)
                              ├──► PHASE7-002 (EC2 network tuning)
                              ├──► PHASE7-003 (Kafka priority lanes)
                              ├──► PHASE7-004 (SOR base)
                              │         ├──► PHASE7-005 (TWAP)
                              │         ├──► PHASE7-006 (VWAP)
                              │         ├──► PHASE7-007 (Iceberg)
                              │         ├──► PHASE7-008 (venue router)
                              │         └──► PHASE7-009 (wire SOR)
                              ├──► PHASE7-010 (OTel instrumentation)
                              │         └──► PHASE7-011 (Grafana dashboard)
                              ├──► PHASE7-012 (latency benchmark)
                              ├──► PHASE7-013 (Terraform c6gn.large)
                              └──► PHASE7-014 (architecture docs)
```
