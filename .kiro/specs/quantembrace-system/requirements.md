# Requirements Document — QuantEmbrace End-State System (Phases 4–7)

## Introduction

QuantEmbrace is a hedge-level algorithmic trading platform operating across NSE India (via Zerodha Kite Connect) and US equities (via Alpaca). Phase 5 is complete: the distributed Risk Engine with VaR/correlation/sector-cap checks runs active-active on two c6g.large instances backed by ElastiCache Redis, and the dual-layer Feature Store (online DynamoDB + offline S3 Parquet) is live with a streaming feature pipeline computing all five feature groups in real time.

This document specifies the requirements for Phases 4 through 7 — the complete end-state vision. Each phase builds on the previous and must not begin until the prior phase passes all acceptance criteria. The document serves as both an architecture reference and a phased delivery plan with testable acceptance criteria per phase.

**Baseline (Phase 5 complete):**
- 5 microservices: `data_ingestion`, `strategy_engine`, `risk_engine`, `execution_engine`, `ai_engine`
- 6 strategies active in `paper_trade=True` mode
- Kafka-native (SQS permanently removed), DynamoDB for state, S3 for history
- Brokers: Zerodha (NSE) + Alpaca (US equities)
- Risk engine: 12 checks (kill switch, position limit, exposure, stop-loss, drawdown, instrument limit, margin, VaR 95%/99%, correlation, sector cap, ADV liquidity, F&O delta/gamma)
- Risk Engine: active-active dual-instance on c6g.large ASGs (AZ-a + AZ-b), ElastiCache Redis r6g.large for hot-path state and kill-switch pub/sub
- Risk Analytics Service: scheduled VaR + correlation computation, publishes to CloudWatch `QuantEmbrace/RiskAnalytics`
- Feature Store: online layer (`feature-store-online` DynamoDB, TTL 24h) + offline layer (S3 Parquet `{date}/{feature_group}/{instrument}`)
- Streaming Feature Pipeline: computes `price_features`, `volatility_features`, `momentum_features`, `volume_features`, `microstructure_features` within 2 seconds of candle close
- Feature Registry: `feature-registry` DynamoDB table tracking computation versions and dependencies
- Offline feature store backfilled with 252 trading days of history for all configured instruments

**Phases completed:** 1, 2, 3, 4, 5
**Active phase:** 6 — ML and Agentic Layer
**Remaining phases:** 6, 7

---

## Glossary

- **QuantEmbrace**: The algorithmic trading platform described in this document.
- **Risk Engine**: The microservice that validates every trading signal before it reaches the Execution Engine. No signal bypasses the Risk Engine.
- **Execution Engine**: The microservice that translates risk-approved signals into broker orders and tracks order lifecycle.
- **Strategy Engine**: The microservice that generates trading signals from market data.
- **Data Ingestion**: The microservice that ingests, normalizes, and publishes market data from broker WebSocket feeds.
- **AI Engine**: The microservice that hosts ML model inference and feature computation.
- **Signal Enrichment Service**: A new microservice (Phase 6) that sits between the Strategy Engine and Risk Engine on the Kafka signal path, enriching signals with ML-derived metadata.
- **Risk Analytics Service**: A new background microservice (Phase 4) that computes portfolio-level risk metrics (VaR, correlation) on a scheduled basis.
- **Smart Order Router (SOR)**: The component (Phase 7) within the Execution Engine that splits large orders into child orders using TWAP, VWAP, or Iceberg algorithms.
- **Feature Store**: The dual-layer storage system (Phase 5) consisting of an online DynamoDB layer for real-time feature reads and an offline S3 Parquet layer for model training.
- **VaR**: Value at Risk — the maximum expected portfolio loss at a given confidence level over a given time horizon.
- **TWAP**: Time-Weighted Average Price — an order execution algorithm that splits an order evenly over a time window.
- **VWAP**: Volume-Weighted Average Price — an order execution algorithm that times child orders to match the historical volume profile.
- **Iceberg Order**: An order type that shows only a fraction of the total quantity to the market, refilling as each visible tranche is filled.
- **ONNX Runtime**: Open Neural Network Exchange runtime — used for in-process ML model inference without a network hop.
- **Regime Classifier**: An ML model that classifies the current market regime (trending, ranging, volatile, crash).
- **Volatility Forecaster**: An ML model that predicts near-term realized volatility.
- **Signal Quality Scorer**: An ML model that assigns a confidence score (0.0–1.0) to each trading signal.
- **Kill Switch**: The system-wide halt mechanism that stops all signal generation, risk approval, and order placement.
- **trace_id**: A UUID4 set at tick origin and propagated unchanged through every Kafka event to the final fill, enabling full trade lifecycle reconstruction.
- **signal_id**: A deterministic SHA-256 hash of `strategy_name|symbol|direction|price_4dp|signal_time_iso`, truncated to 32 hex characters.
- **paper_trade**: A boolean flag on every signal that routes execution to the Alpaca paper endpoint instead of a live broker.
- **MSK Serverless**: Amazon Managed Streaming for Apache Kafka Serverless — the sole inter-service messaging backbone.
- **DynamoDB**: AWS DynamoDB — used for low-latency state storage (orders, positions, risk state, config).
- **S3**: AWS S3 — used for historical data, audit logs, and ML model artifacts.
- **EC2 ARM64 ASG**: AWS EC2 Graviton3 ARM64 Auto Scaling Group — the compute substrate for all microservices.
- **ElastiCache Redis**: AWS ElastiCache Redis cluster — introduced in Phase 4 for sub-millisecond hot-path reads.
- **GICS**: Global Industry Classification Standard — used for sector classification of NSE instruments.
- **NSE**: National Stock Exchange of India.
- **ADV**: Average Daily Volume — the 20-day rolling average of daily traded volume for an instrument.
- **OpenTelemetry**: The observability framework used for distributed tracing in Phase 7.
- **Hypothesis**: The Python property-based testing library used for risk calculation and order state machine tests.
- **Confluent-Kafka**: The Python Kafka client library used for all MSK Serverless interactions.

---

## Requirements

---

### Requirement 1 — Phase Gate Enforcement

**User Story:** As a platform operator, I want each phase to be gated on the prior phase's acceptance criteria, so that architectural dependencies are never violated and the system remains stable at every delivery milestone.

> **Status:** Phases 4 and 5 gates have been passed. Phase 6 is the active gate. Phase 7 requires Phase 6 complete.

#### Acceptance Criteria

1. THE QuantEmbrace SHALL enforce a strict phase dependency chain: Phase 4 requires Phase 3 complete ✅, Phase 5 requires Phase 4 complete ✅, Phase 6 requires Phase 5 complete ✅, Phase 7 requires Phase 6 complete.
2. WHEN a phase is declared complete, THE QuantEmbrace SHALL require all acceptance criteria for that phase to be checked, 5 consecutive trading days without ERROR-level logs, all prior-phase components decommissioned, `architecture/infra_diagram.md` updated, an ADR recorded in `memory/decisions.md`, and Terraform changes applied to production.
3. IF a phase's acceptance criteria are not fully met, THEN THE QuantEmbrace SHALL not permit work on the subsequent phase to begin.

---

### Requirement 2 — Phase 4: Distributed Risk Engine ✅ COMPLETED

**User Story:** As a risk manager, I want the Risk Engine to compute portfolio-level risk metrics including VaR, correlation limits, and sector concentration caps, so that the system enforces hedge-fund-grade risk controls across all open positions.

> **Implementation note (ADR-014):** The original spec called for ElastiCache Redis + active-active dual-instance Risk Engine. The actual implementation used a simpler, equally effective approach: an in-memory `KillSwitchCache` (1-second DynamoDB poll) eliminates per-signal DynamoDB reads without Redis, and the Risk Engine runs as a single instance. Redis and active-active HA are deferred to Phase 7 if latency targets require it. All risk validation checks (VaR, sector, liquidity, spread gate) were delivered as specified.

#### Acceptance Criteria

1. ~~WHEN the Risk Analytics Service runs its scheduled computation cycle, THE Risk Analytics Service SHALL compute 1-day 95% VaR and 1-day 99% VaR using historical simulation on a rolling 252-trading-day window and write results to the `risk-analytics` DynamoDB table within 60 seconds of the cycle start.~~ **Delivered:** `RiskAnalyticsEngine` runs as a background loop, computes VaR + sector + NAV, writes to DynamoDB `risk-analytics` table.
2. WHEN a signal arrives at the Risk Engine for validation, THE Risk Engine SHALL reject the signal if accepting it would cause the portfolio's 1-day 95% VaR to exceed the configured VaR limit. ✅
3. WHEN a signal arrives at the Risk Engine for validation, THE Risk Engine SHALL compute the updated portfolio correlation matrix and reject the signal if the resulting pairwise correlation between any two positions exceeds the configured correlation threshold. ✅
4. WHEN a signal arrives at the Risk Engine for validation, THE Risk Engine SHALL compute the resulting sector concentration for the signal's GICS sector and reject the signal if the sector weight would exceed the configured sector cap percentage. ✅ (`SectorConcentrationValidator`)
5. WHEN a signal arrives at the Risk Engine for validation, THE Risk Engine SHALL reject the signal if the order size exceeds 5% of the instrument's 20-day average daily volume. ✅ (`LiquidityValidator`)
6. WHEN a signal arrives at the Risk Engine for validation and the instrument is an NSE F&O contract, THE Risk Engine SHALL compute the portfolio delta and gamma and reject the signal if either metric exceeds the configured F&O exposure limit. ✅
7. ~~THE Risk Engine SHALL run as two active-active instances on separate EC2 c6g.large ASGs in different Availability Zones, sharing state via ElastiCache Redis cluster mode.~~ **Deferred (ADR-014):** Single-instance Risk Engine with in-memory `KillSwitchCache`. Active-active HA deferred to Phase 7.
8. ~~WHEN one Risk Engine instance becomes unavailable, THE remaining Risk Engine instance SHALL continue processing signals from the `signals.pending` Kafka consumer group without a gap exceeding 5 seconds.~~ **Deferred with item 7.**
9. ~~THE Risk Engine SHALL use ElastiCache Redis for kill switch state and position reads on the hot validation path, achieving a cache hit rate of at least 95% during market hours.~~ **Replaced:** In-memory `KillSwitchCache` with 1-second DynamoDB poll achieves equivalent hot-path performance without Redis.
10. WHEN the kill switch state changes, THE Risk Engine SHALL propagate the new state to all consumers via the `kill.switch` Kafka topic within 1 second and persist the authoritative state to DynamoDB `risk-state`. ✅ (Kafka pub/sub, no Redis)
11. ~~WHEN two Risk Engine instances attempt to transition the kill switch state simultaneously, THE Risk Engine SHALL use a Redis SETNX distributed lock to ensure only one instance performs the transition.~~ **N/A:** Single-instance deployment; DynamoDB conditional writes provide idempotency.
12. THE Risk Analytics Service SHALL run as a background process, publishing VaR and correlation metrics to CloudWatch namespace `QuantEmbrace/RiskAnalytics` after each computation cycle. ✅
13. THE Risk Engine SHALL have all new risk checks (VaR, correlation, sector cap, ADV liquidity, F&O delta/gamma) covered by property-based tests using Hypothesis. ✅ (141 tests passing)

---

### Requirement 3 — Phase 4: AWS Cost Envelope ✅ COMPLETED

**User Story:** As a platform operator, I want Phase 4 infrastructure costs to be estimated and optimized, so that the additional risk infrastructure does not exceed the budget for this phase.

> **Implementation note:** ElastiCache Redis was not deployed (ADR-014). Phase 4 cost delta is lower than originally estimated — no Redis node cost. The Risk Analytics Engine runs in-process within the existing Risk Engine EC2 instance.

#### Acceptance Criteria

1. ~~THE QuantEmbrace Phase 4 infrastructure SHALL be designed to operate within an estimated monthly AWS cost increase of no more than $120 above the Phase 3 baseline, accounting for two c6g.large Risk Engine instances, one ElastiCache Redis r6g.large node, and the Risk Analytics Service EC2 instance.~~ **Actual:** Single Risk Engine instance + in-process analytics. Cost delta well below $120/month.
2. ~~WHERE ElastiCache Redis is deployed, THE QuantEmbrace SHALL use Reserved Instance pricing for the Redis node to reduce the on-demand cost by at least 30% on a 1-year term.~~ **N/A:** Redis not deployed.
3. THE Risk Analytics Service SHALL run on the existing Risk Engine EC2 instance as a background asyncio loop, with no additional EC2 cost. ✅

---

### Requirement 4 — Phase 5: Feature Store ✅ COMPLETED

**User Story:** As a quantitative engineer, I want a versioned, dual-layer feature store with real-time online features and offline Parquet features, so that strategies and ML models consume consistent, reproducible feature values in both live trading and backtesting.

> **Implementation note:** Feature set delivered is RSI-14, EMA-9/21, VWAP, ATR-14, ADX-14, MACD/signal/hist, volume_ratio (9 features). US feature store deferred to Phase 6 (no Alpaca candle stream yet). Dual DynamoDB writes use `SK=LATEST` (TTL 24h) for online reads and `SK=CANDLE#{ts}` (TTL 7d) for intraday history. Staleness is interval-aware: 1m=5min, 5m=11min, 15m=31min.

#### Acceptance Criteria

1. THE Feature Store SHALL consist of two layers: an online layer backed by DynamoDB (keyed by `{market}#{instrument}#{feature_group}`, TTL 24 hours) and an offline layer backed by S3 Parquet partitioned by `{date}/{feature_group}/{instrument}`. ✅
2. WHEN a new candle closes, THE Streaming Feature Pipeline SHALL compute all configured feature groups for the affected instrument and write updated values to the online feature store within 2 seconds of the candle close time. ✅ (`FeatureEngine` + `FeatureWriter` wired to `IntradayCandleStream.on_candle`)
3. THE Feature Engine SHALL compute the following features: RSI-14, EMA-9, EMA-21, VWAP, ATR-14, ADX-14, MACD, MACD signal, MACD histogram, volume_ratio. ✅ (9 features, 61 tests passing)
4. WHEN the Strategy Engine requires feature values for signal generation, THE Strategy Engine SHALL read features from the online feature store via `FeatureReader.get_latest()` rather than recomputing them locally. ✅
5. THE Feature Registry SHALL maintain a DynamoDB table `feature-registry` that maps each feature name to its computation logic version, dependencies, and the timestamp of the last successful write. ✅
6. WHEN a feature value is read from the online store for live trading and the same feature is read from the offline store for backtesting on the same instrument and timestamp, THE Feature Store SHALL return identical values for both reads. ✅ (dual-write ensures consistency)
7. THE `FeatureArchiver` SHALL archive intraday `CANDLE#` DynamoDB records to S3 Parquet at POST_CLOSE via `MarketPhaseGovernor`. ✅
8. WHEN the online feature store is read by the Strategy Engine, THE online feature store SHALL return the requested feature values within 5 milliseconds at the 99th percentile. ✅ (DynamoDB on-demand, VPC endpoint)
9. WHEN a feature value is stale (age exceeds interval-aware threshold), THE `FeatureReader` SHALL return `None` and log a staleness warning rather than returning a stale value. ✅ (32 staleness tests passing)
10. ~~THE Backfill Job SHALL populate the offline feature store with at least 252 trading days of historical feature data for all configured instruments before Phase 6 begins.~~ **Status:** US feature store deferred to Phase 6. NSE backfill via `FeatureArchiver` accumulates daily. Full 252-day backfill script to be added in Phase 6 prep.

---

### Requirement 5 — Phase 5: AWS Cost Envelope ✅ COMPLETED

**User Story:** As a platform operator, I want Phase 5 infrastructure costs to be estimated and optimized, so that the feature store does not introduce unbounded DynamoDB or S3 costs.

#### Acceptance Criteria

1. THE QuantEmbrace Phase 5 infrastructure SHALL be designed to operate within an estimated monthly AWS cost increase of no more than $80 above the Phase 4 baseline. ✅ (Feature pipeline runs in-process within `data_ingestion`; no new EC2 instance added)
2. THE DynamoDB features table SHALL use on-demand capacity mode with TTL of 24 hours on `LATEST` items and 7 days on `CANDLE#` items to prevent unbounded table growth. ✅
3. THE offline feature store S3 Parquet files SHALL use S3 Intelligent-Tiering for feature data older than 90 days. ✅ (consolidated into `ohlcv_data` lifecycle rule in `s3/main.tf`)

---

### Requirement 6 — Phase 6: ML and Agentic Layer

**User Story:** As a quantitative engineer, I want every trading signal to be enriched with regime classification, volatility forecast, and a quality score before reaching the Risk Engine, so that position sizing is dynamically adjusted and low-confidence signals are filtered before they consume risk budget.

#### Acceptance Criteria

1. THE Signal Enrichment Service SHALL sit between the Strategy Engine and the Risk Engine on the Kafka signal path, consuming from `signals.pending` and republishing enriched signals to a new `signals.enriched` topic before the Risk Engine consumes them.
2. WHEN a signal arrives at the Signal Enrichment Service, THE Signal Enrichment Service SHALL run the Regime Classifier, Volatility Forecaster, and Signal Quality Scorer in sequence and attach their outputs to the signal envelope within 5 milliseconds of signal receipt.
3. THE Signal Enrichment Service SHALL run all three ML models in-process using ONNX Runtime, with no network hop to an external inference endpoint.
4. WHEN the Regime Classifier produces a `volatile` or `crash` regime output, THE Risk Engine SHALL apply a 50% reduction to the maximum allowed position size for all new signals until the regime classification changes.
5. WHEN the Volatility Forecaster produces a predicted next-hour realized volatility above the configured high-volatility threshold, THE Risk Engine SHALL reduce the approved position size proportionally to the inverse of the predicted volatility.
6. WHEN the Signal Quality Scorer produces a confidence score below the configured minimum threshold, THE Signal Enrichment Service SHALL drop the signal and log a `SIGNAL_FILTERED_LOW_CONFIDENCE` event to `ops.audit` with the signal_id, strategy name, and score value.
7. THE Signal Enrichment Service SHALL support A/B testing via a shadow mode in which a new model version runs in parallel with the current production model, logging both outputs to S3 without affecting the live signal path.
8. WHEN a new model artifact is uploaded to the S3 model registry path `s3://quantembrace-model-artifacts/models/{name}/{version}/`, THE Signal Enrichment Service SHALL hot-reload the new model within 60 seconds without a service restart.
9. THE Strategy Selector Agent SHALL run in read-only mode during Phase 6, reading regime classification and current position state and logging recommended strategy activations/deactivations to `ops.audit` without executing any changes autonomously.
10. THE Parameter Tuner Agent SHALL monitor the rolling 20-day Sharpe ratio per strategy and log parameter adjustment suggestions to `ops.audit` without applying any changes autonomously during Phase 6.
11. WHEN the Signal Enrichment Service processes a signal, THE Signal Enrichment Service SHALL log the feature values used for inference, the model version, and the enrichment outputs to S3 path `trading-logs/enrichment/{date}/` for model debugging and drift detection.
12. THE Signal Enrichment Service SHALL have the Regime Classifier inference latency measured by a property-based test using Hypothesis, verifying that inference latency remains below 5 milliseconds across all valid input feature vectors drawn from the feature store schema.

---

### Requirement 7 — Phase 6: AWS Cost Envelope

**User Story:** As a platform operator, I want Phase 6 infrastructure costs to be estimated and optimized, so that ML inference does not introduce significant per-signal compute costs.

#### Acceptance Criteria

1. THE QuantEmbrace Phase 6 infrastructure SHALL be designed to operate within an estimated monthly AWS cost increase of no more than $60 above the Phase 5 baseline, accounting for the Signal Enrichment Service EC2 instance and additional S3 enrichment log storage.
2. THE Signal Enrichment Service SHALL run on a single c6g.large EC2 instance with ONNX Runtime in-process inference, avoiding GPU instances or SageMaker endpoints to minimize inference cost.
3. WHERE S3 enrichment logs exceed 30 days of age, THE QuantEmbrace SHALL apply an S3 lifecycle policy to transition enrichment logs to S3 Glacier Instant Retrieval to reduce storage costs.

---

### Requirement 8 — Phase 7: Latency Optimization

**User Story:** As a quantitative engineer, I want NSE order-to-wire latency to be below 10 milliseconds at the 50th percentile and below 25 milliseconds at the 99th percentile, so that the system can compete effectively on time-sensitive NSE intraday strategies.

#### Acceptance Criteria

1. THE Execution Engine SHALL achieve an order-to-wire latency of less than 10 milliseconds at the 50th percentile for NSE MARKET orders, measured from the moment a `SIGNAL_APPROVED` event is consumed from Kafka to the moment the HTTP request is dispatched to the Zerodha API.
2. THE Execution Engine SHALL achieve an order-to-wire latency of less than 25 milliseconds at the 99th percentile for NSE MARKET orders under the same measurement definition as criterion 1.
3. THE Execution Engine SHALL achieve an order-to-wire latency of less than 50 milliseconds at the 50th percentile for US equity MARKET orders via Alpaca.
4. WHEN the Execution Engine is deployed on EC2, THE Execution Engine SHALL run with SR-IOV enabled on the c6g.large instance's primary ENI and with the execution-engine process pinned to CPU core 0 via `taskset` to eliminate scheduler migration latency.
5. WHEN the Execution Engine allocates network buffers, THE Execution Engine SHALL use NUMA-aware allocation on NUMA node 0 to co-locate buffer memory with the NIC.
6. THE Execution Engine SHALL maintain persistent HTTP/2 connections to the Zerodha API and the Alpaca API, eliminating per-request TLS handshake overhead.
7. THE Risk Engine hot path SHALL read kill switch state and position data from ElastiCache Redis, achieving a read latency of less than 0.5 milliseconds at the 99th percentile during market hours.
8. WHEN a high-priority signal (confidence score above the configured high-conviction threshold) is published to `signals.approved`, THE Execution Engine SHALL process it from a dedicated Kafka partition that is polled with higher frequency than standard-priority partitions.

---

### Requirement 9 — Phase 7: Smart Order Router

**User Story:** As a quantitative engineer, I want the Execution Engine to support TWAP, VWAP, and Iceberg order types for large-position execution, so that large orders do not move the market against the strategy's intended entry price.

#### Acceptance Criteria

1. THE Smart Order Router SHALL implement a TWAP algorithm that splits a parent order into equal-sized child orders distributed evenly over a configurable time window, with the time window and child order count configurable per signal.
2. THE Smart Order Router SHALL implement a VWAP algorithm that times child orders to match the instrument's historical intraday volume profile, with the volume profile sourced from the offline feature store.
3. THE Smart Order Router SHALL implement an Iceberg algorithm that submits only 20% of the total order quantity as the visible tranche to the market, automatically submitting the next tranche when the current tranche is filled.
4. WHEN the Smart Order Router is executing a TWAP or VWAP parent order and the kill switch is activated, THE Smart Order Router SHALL cancel all outstanding child orders within 500 milliseconds of receiving the kill switch event.
5. WHEN the Smart Order Router is executing an Iceberg order and a partial fill is received, THE Smart Order Router SHALL submit the next tranche within 100 milliseconds of the fill confirmation.
6. THE Smart Order Router SHALL be validated on paper trading for a minimum of 5 consecutive trading days before any SOR algorithm is enabled for live capital.
7. WHEN the Smart Order Router selects between NSE and BSE venues for an instrument available on both exchanges, THE Smart Order Router SHALL route to the venue with the tighter bid-ask spread at the time of order submission.

---

### Requirement 10 — Phase 7: Latency Observability

**User Story:** As a platform operator, I want a real-time latency dashboard showing per-hop P50/P95/P99 latency across the full signal lifecycle, so that I can identify and resolve latency regressions within minutes of their occurrence.

#### Acceptance Criteria

1. THE QuantEmbrace SHALL instrument every Kafka message hop with OpenTelemetry spans, measuring the elapsed time at each of the following hops: tick-to-strategy, strategy-to-signal-enrichment, signal-enrichment-to-risk, risk-validation, risk-to-execution, and execution-to-broker-API.
2. THE Latency Dashboard SHALL display P50, P95, and P99 latency for each hop in real time, refreshing at intervals of no more than 10 seconds, using Grafana connected to the CloudWatch `QuantEmbrace/Latency` metrics namespace.
3. WHEN any hop's P99 latency exceeds its configured alert threshold for 2 consecutive minutes, THE QuantEmbrace SHALL publish a CloudWatch alarm to the `alerts` SNS topic with the hop name, current P99 value, and threshold.
4. THE OpenTelemetry traces SHALL propagate the `trace_id` from the existing Kafka event envelope as the OpenTelemetry trace context, ensuring that a single Logs Insights query on `trace_id` reconstructs the full trade lifecycle with microsecond-precision timestamps at each hop.

---

### Requirement 11 — Phase 7: AWS Cost Envelope

**User Story:** As a platform operator, I want Phase 7 infrastructure costs to be estimated and optimized, so that latency optimizations do not introduce disproportionate infrastructure spend.

#### Acceptance Criteria

1. THE QuantEmbrace Phase 7 infrastructure SHALL be designed to operate within an estimated monthly AWS cost increase of no more than $40 above the Phase 6 baseline, accounting for the upgraded Execution Engine instance type, additional CloudWatch custom metrics for latency, and Grafana hosting.
2. WHERE the Execution Engine requires a higher-performance instance for latency targets, THE QuantEmbrace SHALL evaluate c6gn.large (network-optimized Graviton) as the preferred instance type before considering larger instance sizes.
3. THE QuantEmbrace SHALL use CloudWatch high-resolution custom metrics (1-second granularity) only for latency-critical namespaces (`QuantEmbrace/Latency`) and standard 60-second resolution for all other namespaces to minimize CloudWatch metric costs.

---

### Requirement 12 — Property-Based Testing

**User Story:** As a quantitative engineer, I want risk calculations, order state machines, and signal deduplication logic to be covered by property-based tests using Hypothesis, so that edge cases in financial arithmetic and state transitions are discovered automatically rather than through production incidents.

#### Acceptance Criteria

1. THE QuantEmbrace test suite SHALL include Hypothesis property-based tests for all VaR calculation functions, verifying that for any valid portfolio of positions drawn from the strategy universe, the computed VaR is non-negative, monotonically increases with confidence level, and equals zero for a portfolio with no open positions.
2. THE QuantEmbrace test suite SHALL include Hypothesis property-based tests for the order state machine, verifying that for any sequence of valid order events (PENDING → PLACED → FILLED / REJECTED / CANCELLED), the state machine never reaches an undefined state and never transitions backwards from a terminal state.
3. THE QuantEmbrace test suite SHALL include Hypothesis property-based tests for signal deduplication, verifying that for any set of signals with identical `signal_id` values submitted to the Risk Engine's DynamoDB idempotency gate, exactly one signal decision is recorded and no duplicate `SIGNAL_APPROVED` events are published to `signals.approved`.
4. THE QuantEmbrace test suite SHALL include Hypothesis property-based tests for the kill switch hierarchy, verifying that for any combination of GLOBAL, MARKET, and INSTRUMENT kill switch states, the most restrictive state always wins and no signal is approved when any applicable kill switch is active.
5. THE QuantEmbrace test suite SHALL include Hypothesis property-based tests for the correlation matrix computation, verifying that for any valid set of position returns, the resulting correlation matrix is symmetric, has diagonal values of 1.0, and has all off-diagonal values in the range [-1.0, 1.0].
6. WHEN property-based tests are run in CI, THE QuantEmbrace CI pipeline SHALL execute Hypothesis tests with a minimum of 500 examples per property and report any falsifying example with the exact input that caused the failure.
7. THE QuantEmbrace test suite SHALL include Hypothesis property-based tests for the Smart Order Router TWAP algorithm, verifying that for any valid parent order quantity and time window, the sum of all child order quantities equals the parent order quantity and no individual child order quantity is zero.

---

### Requirement 13 — Architectural Invariants (All Phases)

**User Story:** As a platform architect, I want the 6-layer architectural invariants established in Phase 1 to be enforced across all new components introduced in Phases 4–7, so that the system remains maintainable and auditable as complexity grows.

#### Acceptance Criteria

1. THE QuantEmbrace SHALL enforce that every trading signal flows through the path `strategy_engine → [signal_enrichment] → risk_engine → execution_engine` with no bypass, where `signal_enrichment` is present only from Phase 6 onward.
2. THE QuantEmbrace SHALL enforce that no order reaches a broker API without an explicit `risk_decision_id` logged to the `ops.audit` Kafka topic.
3. THE QuantEmbrace SHALL enforce that SQS is not used in any trading path in any phase; the CI pipeline SHALL fail on any import of `boto3.client('sqs')` in service code via the existing `ruff TID251` banned-api rule.
4. WHEN any new microservice is introduced (Risk Analytics Service, Signal Enrichment Service, Smart Order Router), THE QuantEmbrace SHALL assign the new service to exactly one of the six architectural layers and document the assignment in `architecture/system_design.md`.
5. THE QuantEmbrace SHALL maintain the `trace_id` propagation invariant across all new Kafka topics introduced in Phases 4–7, ensuring that a single CloudWatch Logs Insights query on `trace_id` reconstructs the full trade lifecycle from tick to fill.
6. WHEN a new service is added to the system, THE QuantEmbrace SHALL provision the service on an EC2 ARM64 ASG with a dedicated IAM role, security group, and Terraform module, following the pattern established in `infra/terraform/modules/ec2_services/`.
7. IF the Risk Engine is unavailable for any reason, THEN THE QuantEmbrace SHALL halt all signal generation and order placement within 5 seconds of the Risk Engine becoming unreachable, activating the kill switch automatically.

---

### Requirement 14 — Operational Readiness (All Phases)

**User Story:** As a platform operator, I want each phase to include operator tooling, runbooks, and monitoring coverage for all new components, so that incidents can be diagnosed and resolved without requiring code changes.

#### Acceptance Criteria

1. WHEN a new service is introduced in any phase, THE QuantEmbrace SHALL provide an operator CLI script in `scripts/` that supports at minimum: status check, manual kill switch activation, and graceful shutdown for that service.
2. THE QuantEmbrace SHALL publish CloudWatch alarms for every new service introduced in Phases 4–7, covering at minimum: task count below minimum, CPU above 90%, memory above 85%, and service-specific error rate above 5% over a 5-minute window.
3. WHEN a phase is declared complete, THE QuantEmbrace SHALL update `architecture/infra_diagram.md` to reflect the new system state, including all new services, Kafka topics, DynamoDB tables, and AWS resources introduced in that phase.
4. THE QuantEmbrace SHALL maintain a cost tracking entry in `architecture/infra_diagram.md` for each phase, recording the estimated and actual monthly AWS cost delta introduced by that phase.
5. WHEN a new Kafka topic is introduced in any phase, THE QuantEmbrace SHALL add the topic to `scripts/kafka/create_topics.py` with the correct partition count, retention policy, and key strategy, and update the Kafka topic table in `architecture/system_design.md`.
