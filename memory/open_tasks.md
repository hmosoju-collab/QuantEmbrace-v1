# QuantEmbrace - Open Tasks

> Tracked tasks that need to be completed. Each task includes context,
> acceptance criteria, and dependencies. Tasks are removed from this file
> and recorded in the decision log when completed.
>
> **Priority levels**: P0 (blocking, do first), P1 (important, do soon), P2 (needed, can wait), P3 (nice to have)

---

## ✅ Completed (Step 1 — boto3 Wiring Sprint)

| Task | Description | Completed |
|------|-------------|-----------|
| DONE-001 | `shared/aws/clients.py` — singleton boto3 client factory with LocalStack auto-detection | ✓ 2026-04-25 |
| DONE-002 | `data_ingestion/storage/s3_writer.py` — real S3 batched writes | ✓ 2026-04-25 |
| DONE-003 | `data_ingestion/storage/dynamo_writer.py` — real DynamoDB batch_writer + conditional puts | ✓ 2026-04-25 |
| DONE-004 to DONE-016 | **ARCHIVED** — SQS-era implementation (SQSTickPublisher, SQS long-poll consumers, sqs_market_data_queue). All deleted. System is now Kafka-native. See ARCHITECTURE.md. | ✓ 2026-05-03 |
| DONE-017 | `AlpacaBroker` — full rewrite with alpaca-py (`TradingClient` + `TradingStream`), paper mode, Secrets Manager, order-update stream, `Position` model | ✓ 2026-04-26 |
| DONE-018 | `order.py` — added `Position` pydantic model with all required fields | ✓ 2026-04-26 |
| DONE-019 | `OrderManager` — added `record_order`, `get_order_by_signal`, `update_order_status`, `get_open_orders`, `wait_for_inflight_orders` | ✓ 2026-04-26 |
| DONE-020 | `tests/unit/test_alpaca_broker.py` — 17 unit tests covering connect, place_order, cancel, status, positions, paper mode, rate limiter, status translation | ✓ 2026-04-26 |
| DONE-021 | `killswitch.py` — enhanced with SNS publish on activate/deactivate, `activated_by` field, `get_status()` dict, concurrent persist+notify via `asyncio.gather` | ✓ 2026-04-26 |
| DONE-022 | `auto_triggers.py` (new) — `KillSwitchMonitor` with 4 background tasks: order rate runaway, broker connectivity lost (>30s), data feed stale (>60s during market hours), single-strategy loss (>threshold%) | ✓ 2026-04-26 |
| DONE-023 | `risk_engine/api/killswitch_api.py` (new) — aiohttp handlers for GET /risk/kill-switch/status, POST /risk/kill-switch/activate, POST /risk/kill-switch/deactivate (requires explicit confirmation string) | ✓ 2026-04-26 |
| DONE-024 | `scripts/kill_switch_cli.py` (new) — operator CLI: `status`, `activate --reason`, `deactivate` with confirmation prompt; `--yes` flag for automation | ✓ 2026-04-26 |
| DONE-025 | `risk_engine/service.py` — wired `KillSwitchMonitor` (start in `start()`, stop in `stop()`), injected `sns_client` + `sns_topic_arn` into `KillSwitch`, `main()` now creates real SNS boto3 client | ✓ 2026-04-26 |
| DONE-026 | `tests/unit/test_killswitch.py` (new) — 24 unit tests across KillSwitch core (12) and KillSwitchMonitor auto-triggers (12); all logic-verified via asyncio inline runner | ✓ 2026-04-26 |
| DONE-027 | `settings.py` — added `dynamodb_table_sessions` + `sns_kill_switch_topic_arn` to `AWSConfig` | ✓ 2026-04-26 |
| DONE-028 | `execution_engine/auth/zerodha_auth.py` (new) — `ZerodhaTokenManager`: Secrets Manager creds, DynamoDB token store with TTL, `get_valid_token()`, `exchange_request_token()`, `_next_expiry_utc()` (02:00 UTC boundary) | ✓ 2026-04-26 |
| DONE-029 | `zerodha_broker.py` — `connect()` uses `ZerodhaTokenManager` (DynamoDB → env fallback → `needs_authentication` mode); `place_order()` guards against unauthenticated state; `refresh_access_token()` delegates to token manager | ✓ 2026-04-26 |
| DONE-030 | `scripts/zerodha_login.py` (new) — operator CLI: prints login URL, accepts request_token, exchanges + stores token; `status` subcommand shows current token state | ✓ 2026-04-26 |
| DONE-031 | `tests/unit/test_zerodha_auth.py` (new) — 15 unit tests: creds loading, token get/expiry, exchange flow, DynamoDB schema, TTL boundary logic, broker connect states | ✓ 2026-04-26 |

---

---

## ✅ TASK-001: Implement Zerodha Kite Connect Authentication Flow — COMPLETE

**Priority**: P0 → **DONE 2026-04-26**
**Files**: `execution_engine/auth/zerodha_auth.py`, `zerodha_broker.py`, `scripts/zerodha_login.py`, `tests/unit/test_zerodha_auth.py`

### Delivered

- [x] `ZerodhaTokenManager` implements the full token lifecycle.
- [x] API credentials loaded from Secrets Manager (`quantembrace/{env}/zerodha/api-credentials`), env-var fallback for local dev.
- [x] Access token persisted in DynamoDB (`quantembrace-sessions` table) with a `ttl` attribute for auto-expiry by AWS.
- [x] Token expiry boundary: 02:00 UTC daily (~07:30 IST). `_next_expiry_utc()` correctly handles before/after boundary.
- [x] `ZerodhaBrokerClient.connect()` resolves token: DynamoDB first → env-var fallback → `needs_authentication=True` mode (no exception; service starts and rejects orders gracefully with a clear message).
- [x] `place_order()` raises `BrokerAPIError` with actionable message when `needs_authentication=True`.
- [x] `refresh_access_token()` delegates to `ZerodhaTokenManager.exchange_request_token()` — no duplicate token exchange logic.
- [x] `scripts/zerodha_login.py` — operator morning workflow: prints login URL → accepts request_token → exchanges + stores in DynamoDB. `status` subcommand shows current token state.
- [x] 15 unit tests: creds loading (3), token get/expiry (5), exchange flow (3), store/is_valid (2), TTL boundary (3). All passing.

### Daily operator workflow

```bash
# Each morning before NSE market open (08:30–09:15 IST):
python scripts/zerodha_login.py
# → Open URL → paste request_token → done
```

---

## TASK-002: Implement Alpaca Authentication and Paper Trading

**Priority**: P0
**Service**: `/services/execution_engine/adapters/alpaca_adapter.py`
**Depends on**: Secrets Manager setup

### Context

Alpaca uses API key + secret authentication. Paper trading uses the same API with a different base URL. This is simpler than Zerodha and should be implemented first as the initial development and testing broker.

### Acceptance Criteria

- [ ] `AlpacaAdapter` implements the full `BrokerAdapter` interface.
- [ ] Authentication uses API key/secret from Secrets Manager (`quantembrace/{env}/alpaca/api-credentials`).
- [ ] Paper trading mode is configurable via environment variable (`QUANTEMBRACE_EXECUTION_ENGINE_ALPACA_PAPER_MODE=true`).
- [ ] Paper trading and live trading use the same adapter code, only the base URL differs.
- [ ] Real-time market data subscription works via Alpaca's WebSocket API.
- [ ] Order placement, cancellation, and status query are implemented and tested.
- [ ] Position fetching returns normalized `Position` objects.
- [ ] All adapter methods have type hints, docstrings, and unit tests.
- [ ] Integration test places a paper trade and verifies fill.

---

## ✅ TASK-003: Set Up S3 Lifecycle Policies for Tick Data Archival — COMPLETE

**Priority**: P1 → **DONE 2026-04-26**
**Files**: `infra/terraform/modules/s3/main.tf`, `infra/terraform/modules/s3/outputs.tf`

### Delivered

- [x] 5 purpose-built buckets: tick_data, ohlcv_data, trading_logs, backtest_results, model_artifacts.
- [x] tick_data: Standard → IA at 30d → Glacier IR at 90d → Deep Archive at 365d.
- [x] ohlcv_data: Standard → IA at 90d → Glacier IR at 365d.
- [x] trading_logs: Standard → IA at 90d → Glacier IR at 365d (compliance retention).
- [x] backtest_results: versioned, IA at 90d, NO current-version expiry (retained forever).
- [x] model_artifacts: versioned, noncurrent IA at 90d → Glacier IR at 365d → expire at 730d.
- [x] All buckets: AES256 encryption, public access blocked, bucket-key enabled.

---

## ✅ TASK-004: Configure CloudWatch Alarms for Trading Anomalies — COMPLETE

**Priority**: P1 → **DONE 2026-04-26**
**Files**: `infra/terraform/modules/monitoring/main.tf`, `variables.tf`, `outputs.tf`

### Delivered

- [x] Two SNS topics: `alerts` (general) + `kill_switch` (auto-halt triggers).
- [x] Per-service ECS log groups with configurable retention (default 30d).
- [x] Per-service ECS alarms: task count (< min → breaching), CPU > 90%, memory > 85%, error rate.
- [x] Trading alarms: daily P&L loss alert, daily P&L halt (→ both topics), order rejection rate, no-orders sentinel, WebSocket gap, data feed staleness, risk engine health.
- [x] Infra alarms: execution latency p99, DynamoDB throttles, daily cost.
- [x] 4-row CloudWatch dashboard: ECS health / trading activity / latency+DynamoDB / error summary.
- [x] All alarm thresholds parameterised via variables.tf.

---

## ✅ TASK-005: Build Momentum Strategy with Full Backtesting — COMPLETE

**Priority**: P1 → **DONE 2026-04-26**
**Files**: `strategy_engine/strategies/momentum_strategy.py`, `strategy_engine/backtesting/backtester.py`, `scripts/backtest/run_backtest.py`, `tests/unit/test_momentum_backtester.py`

### Delivered

- [x] `MomentumStrategy` v2: dual MA crossover + ATR-based SL/TP + risk-based position sizing.
- [x] `Backtester`: full P&L sim with stop-loss/TP exit, commission, short selling, EOD close.
- [x] Metrics: total return, CAGR, Sharpe, Sortino, max drawdown, win rate, profit factor, avg win/loss.
- [x] `BacktestResult.summary()` one-liner + full trade log.
- [x] `scripts/backtest/run_backtest.py` — CLI runner supporting local CSV and S3 data sources.
- [x] 26 unit tests: math helpers (7), strategy signals (6), edge cases (2), metrics (5), exits (3), commission (1), short (2). All passing.

---

## ✅ TASK-006: Set Up CI/CD Pipeline (GitHub Actions → ECR → ECS) — COMPLETE

**Priority**: P1 → **DONE 2026-04-26**
**Files**: `.github/workflows/ci.yml`, `.github/workflows/build.yml`, `.github/workflows/deploy.yml`, `scripts/deploy/check_ecs_health.py`

### Delivered

- [x] `ci.yml`: lint (ruff + black + isort) + unit tests (85% coverage gate) + Terraform validate. Triggered on every push/PR touching Python or infra.
- [x] `build.yml`: change-detection (dorny/paths-filter) — only rebuilds services whose files changed. Builds Docker image and pushes to ECR tagged `<sha>` + `latest-staging`. OIDC auth (no long-lived keys).
- [x] `deploy.yml`: auto-deploy to staging on build success → smoke tests → manual approval gate (GitHub environment `prod`) → prod rolling update → auto-rollback on failure.
- [x] `risk_engine` deployed before `execution_engine` in prod matrix (architectural invariant preserved).
- [x] `scripts/deploy/check_ecs_health.py` — post-deploy health poll; used by both staging and prod verify steps.
- [x] Immutable SHA tags; `latest-prod` mutable pointer updated after successful prod deploy.

---

## ✅ TASK-007: Implement Kill Switch with Manual and Automatic Triggers — COMPLETE

**Priority**: P0 → **DONE 2026-04-26**
**Files**: `killswitch/killswitch.py`, `killswitch/auto_triggers.py`, `api/killswitch_api.py`, `scripts/kill_switch_cli.py`, `tests/unit/test_killswitch.py`

### Delivered

- [x] Kill switch state stored in DynamoDB (PK=KILLSWITCH, SK=GLOBAL) with `activated_by` field.
- [x] Activation: concurrent DynamoDB persist + SNS publish via `asyncio.gather` for ≤5s propagation SLA.
- [x] SNS failure does not prevent activation — state is still persisted.
- [x] `get_status()` returns serializable dict for API/CLI use.
- [x] Manual activation: CLI (`scripts/kill_switch_cli.py`) + HTTP API (`POST /risk/kill-switch/activate`).
- [x] Manual deactivation: CLI + HTTP API, both require explicit confirmation string.
- [x] **5 automatic triggers** (all implemented + unit tested):
  1. Daily portfolio loss (existing, `DailyLossValidator` in service.py) → `activated_by="loss_validator"`
  2. Single strategy loss > threshold% (`KillSwitchMonitor._monitor_strategy_loss`)
  3. Order rate runaway > N orders/window (`KillSwitchMonitor._monitor_order_rate`)
  4. Broker connectivity lost > 30s (`KillSwitchMonitor._monitor_broker_connectivity`)
  5. Data feed stale > 60s during market hours (`KillSwitchMonitor._monitor_data_staleness`)
- [x] `KillSwitchMonitor` started/stopped with `RiskEngineService` lifecycle.
- [x] Kill switch status checked before every signal (existing path in `validate_signal`).
- [x] 24 unit tests passing (12 KillSwitch core, 12 auto-trigger conditions).

---

## TASK-008: Add Integration Tests for Order Flow

**Priority**: P2
**Service**: `/tests/integration/`
**Depends on**: All core services implemented (strategy, risk, execution)

### Context

Integration tests verify the complete signal-to-order pipeline works correctly with all services connected. These tests use mock broker adapters (no real broker calls) but exercise the real risk engine, real DynamoDB interactions, and real inter-service communication.

### Acceptance Criteria

- [ ] Test: Signal approved by risk engine results in order submission via mock broker.
- [ ] Test: Signal rejected by risk engine (position limit) does not result in order submission.
- [ ] Test: Signal rejected by risk engine (daily loss limit) does not result in order submission.
- [ ] Test: Kill switch active prevents all order submission.
- [ ] Test: Duplicate signal (same signal ID) results in only one order (idempotency).
- [ ] Test: Broker adapter failure triggers circuit breaker after threshold.
- [ ] Test: Circuit breaker open state rejects orders immediately without calling broker.
- [ ] Test: Order cancellation flow works end-to-end.
- [ ] Test: Position is updated correctly after order fill.
- [ ] Test: Graceful shutdown flushes pending operations.
- [ ] All tests run in under 60 seconds.
- [ ] Tests use localstack or DynamoDB Local for AWS dependencies.
- [ ] Tests are included in the CI pipeline.

---

## TASK-009: Set Up Monitoring Dashboard

**Priority**: P2
**Service**: `/infra/modules/monitoring/`
**Depends on**: CloudWatch alarms (TASK-004), ECS services deployed

### Context

A centralized monitoring dashboard provides real-time visibility into platform health, trading activity, and risk metrics. This is essential for operational confidence and incident response.

### Acceptance Criteria

- [ ] Dashboard solution selected (CloudWatch Dashboards for initial simplicity, Grafana for advanced needs).
- [ ] Dashboard panels for infrastructure health:
  - ECS task status per service (running, pending, stopped).
  - CPU and memory utilization per service.
  - Error rate per service (from CloudWatch Logs Insights).
  - Network throughput.
- [ ] Dashboard panels for trading activity:
  - Signals generated per minute (by strategy).
  - Signals approved vs. rejected (by rejection reason).
  - Orders submitted per minute (by broker).
  - Order fill rate and average fill latency.
  - Current open positions (by instrument and broker).
- [ ] Dashboard panels for risk metrics:
  - Portfolio P&L (real-time, intraday).
  - Daily drawdown vs. kill switch threshold.
  - Position exposure by sector and exchange.
  - Kill switch status (prominent, color-coded).
- [ ] Dashboard panels for data quality:
  - Tick data ingestion rate (ticks per second).
  - WebSocket connection status per broker.
  - Data feed latency (time from broker to our processing).
  - S3 write success rate and batch sizes.
- [ ] Dashboard is accessible via URL (no CLI required).
- [ ] Dashboard auto-refreshes at minimum every 30 seconds.
- [ ] Terraform config to provision the dashboard (CloudWatch) or Helm chart (Grafana on ECS).

---

---

## 🚧 PHASE 2 — Kafka Migration (Personal Trader Edition)

**Architecture**: `architecture/phase2_final_approved.md` (v3.0) — APPROVED 2026-04-30
**Decision log**: `memory/decisions.md` ADR-011

### Phase 2 Blocker Status

| Blocker | Description | Status |
|---------|-------------|--------|
| B1 | Zerodha fill tracking — `subscribe_quotes` stub broken | ✅ **FIXED 2026-04-30** |
| B2 | ALB Terraform for Zerodha postback | ✅ **DEFERRED** — polling is approved solution for personal trading |

### B1 Implementation Details (DONE)

**Files changed**:
- `services/execution_engine/polling/__init__.py` — new package
- `services/execution_engine/polling/fill_poller.py` — `ZerodhaFillPoller` (300ms polling loop)
- `services/execution_engine/service.py` — poller wired into `asyncio.gather`, shutdown via `stop()`
- `services/shared/config/settings.py` — added `dynamodb_table_risk_state` + `dynamodb_table_fills` (were referenced but never declared — latent bug fixed)

**How it works**:
1. `ZerodhaFillPoller.start()` runs as a 4th coroutine in `ExecutionService.asyncio.gather`
2. Every 300ms: fetches PLACED/PARTIALLY_FILLED NSE orders from DynamoDB (status-index GSI)
3. For each, calls `zerodha.get_order_status(broker_order_id)` (rate-limited via shared NSE semaphore)
4. On FILLED: `fill_id = sha256("{broker_order_id}|{qty}|{price}")[:16]`
5. DynamoDB conditional write `attribute_not_exists(PK)` on `FILL#{fill_id}` — idempotency gate
6. Gate passes → update order status → `apply_fill_to_position()` → log `ORDER_FILLED` event
7. Gate fails (duplicate) → silently skip — positions never double-counted

**Phase 2 TODO in fill_poller.py**: replace structured log at step 6 with Kafka `orders.events` publish once MSK Serverless topic is available.

### Phase 2 Next Tasks (Implementation)

| Task | Description | Priority | Status |
|------|-------------|----------|--------|
| PHASE2-001 | Kafka MSK Serverless Terraform module (`infra/terraform/modules/kafka/`) | P0 | ✅ DONE 2026-05-02 |
| PHASE2-002 | `data_ingestion` → `KafkaTickPublisher` (replace `SQSTickPublisher`) | P1 | ✅ DONE 2026-05-02 |
| PHASE2-003 | `strategy_engine` → Kafka consumer for `ticks.nse` / `ticks.us` | P1 | ✅ DONE 2026-05-02 |
| PHASE2-004 | `risk_engine` → Kafka consumer for `signals.pending`, `orders.events`, kill-switch-listener | P1 | ✅ DONE 2026-05-04 |
| PHASE2-005 | `execution_engine` → Kafka consumer for `signals.approved`; `fill_poller` publishes `orders.events` | P1 | ✅ DONE 2026-05-04 |
| PHASE2-006 | Pre-go-live validation checklist + scripts (`scripts/kafka/validate_phase2.py`) | P0 | ✅ DONE 2026-05-04 |

WORKSPACE-CLEANUP deliverables (2026-05-03):
- Deleted: `services/strategy_engine/signals/signal.py` (re-export shim); callers updated to import from `shared.models.signal` directly (base_strategy.py, momentum_strategy.py, backtester.py)
- Deleted: `architecture/v1.md`, `v2-draft.md`, `phase2_kafka_architecture.md`, `phase2_pre_migration_review.md` (superseded by phase2_final_approved.md + phase2_implementation_plan.md)
- Deleted: `infra/terraform/modules/ecs/` (old Fargate module — all services on EC2)
- Added: `ecr_account_id`, `secrets_zerodha_arn`, `secrets_alpaca_arn` variables to all 3 env variables.tf
- Updated: `architecture/system_design.md`, `data_flow.md`, `infra_diagram.md`, `trading_flow.md` — reflect EC2 + Kafka Phase 2 state; removed ECS Fargate references

PHASE2-003 deliverables (2026-05-02):
- `services/strategy_engine/consumers/kafka_tick_consumer.py` — confluent-kafka Consumer, MSK IAM auth, consumer group strategy-v1, subscribes to [ticks.nse, ticks.us]; `poll_tick()` synchronous (asyncio.to_thread); parses v3.0 TICK schema, extracts (symbol, market, price, volume, timestamp, trace_id, sequence_id); schema_version guard (skips non-3.0 messages)
- `services/strategy_engine/publishers/kafka_signal_publisher.py` — confluent-kafka Producer, MSK IAM auth, publishes to signals.pending; deterministic signal_id = sha256(strategy_name|symbol|direction|price_4dp|signal_time_iso)[:32]; trace_id propagation from tick → signal; expires_at = signal_time + 30s; background poll loop for delivery receipts
- `services/strategy_engine/service.py` — Kafka-only (complete)

Strategy engine is Kafka-only. KAFKA_BOOTSTRAP_SERVERS is mandatory; service raises RuntimeError if missing.

PHASE2-002 deliverables (2026-05-02):
- `services/data_ingestion/publishers/kafka_tick_publisher.py` — confluent-kafka producer, MSK IAM auth (SASL/OAUTHBEARER), v3.0 event schema (trace_id, sequence_id, exchange_time, schema_version), kill-switch fallback via daemon thread + boto3 DynamoDB write after 3 consecutive delivery failures, background asyncio poll loop for delivery callbacks
- `services/data_ingestion/processors/tick_processor.py` — added `kafka_publisher: Optional[KafkaTickPublisher]` parameter + step 6 dispatch in `process_tick()`, TYPE_CHECKING import added
- `services/data_ingestion/service.py` — Kafka-only publisher (complete). KAFKA_BOOTSTRAP_SERVERS mandatory.

Migration flag behavior:
- `KAFKA_BOOTSTRAP_SERVERS` must be set (fail-fast on startup if missing)
- `KAFKA_BOOTSTRAP_SERVERS` set → Kafka-only mode (the only mode)

PHASE2-001 deliverables (2026-05-02):
- `infra/terraform/modules/kafka/main.tf` (478L) — MSK Serverless cluster, security group, 4 per-service IAM policies, ops-admin policy, 2 CloudWatch alarms
- `infra/terraform/modules/kafka/variables.tf` (137L) — all params including per-topic retention overrides
- `infra/terraform/modules/kafka/outputs.tf` (86L) — bootstrap_brokers_sasl_iam, cluster_arn, all IAM policy ARNs, topic_config map
- `scripts/kafka/create_topics.py` (400L) — idempotent topic creation via confluent-kafka AdminClient + MSK IAM auth; --dry-run, --short-retention, --verify-only flags
- `infra/terraform/modules/ec2_services/iam.tf` — added missing risk_engine IAM role/policy/profile (Phase 1 gap)
- `infra/terraform/modules/ec2_services/outputs.tf` — added {service}_role_name outputs for kafka module input
- All 3 environments wired: prod (full retention), staging + dev (short retention overrides)

---

---

## 🚧 Zerodha Full Rate Capacity (RT Tasks)

**Architecture**: `architecture/zerodha_rate_capacity_design.md` (v1.1) — APPROVED 2026-05-01
**Decision log**: `memory/decisions.md` ADR-012

### Gate 1 — Foundation (Active)

| Task | File | Status |
|------|------|--------|
| RT-T01 | Token bucket rate limiter — `services/shared/zerodha/rate_limiter.py` | ✅ DONE 2026-05-01 |
| RT-T02 | Market phase governor — `services/shared/zerodha/market_phase.py` | ✅ DONE 2026-05-01 |
| RT-T07 | Broker client extensions — `zerodha_broker.py` get_all_orders / get_batch_quotes / get_historical_candles | ✅ DONE 2026-05-01 |

### Gate 2 — Fill Detection Fix (Active)

| Task | File | Status |
|------|------|--------|
| RT-T03 | BulkOrderPoller — `polling/bulk_order_poller.py` (replaces O(N) fill_poller) | ✅ DONE 2026-05-01 |

### Gate 3 — New Data Feeds ✅ COMPLETE 2026-05-01

| Task | File | Status |
|------|------|--------|
| RT-T04 | PositionMonitor — `polling/position_monitor.py` | ✅ DONE 2026-05-01 |
| RT-T05 | LiveQuotePoller — `polling/live_quote_poller.py` | ✅ DONE 2026-05-01 |
| RT-T06 | IntradayCandleStream — `data_ingestion/candle_stream.py` | ✅ DONE 2026-05-01 |
| RT-T09 | CloudWatch metrics — `infra/terraform/modules/monitoring/` | ✅ DONE 2026-05-01 |
| RT-T10 | 5 operator scripts — `scripts/zerodha/` | ✅ DONE 2026-05-01 |

Gate 3 deliverables:
- **3 alarms** added to `monitoring/main.tf`: P0 (429 errors → kill_switch SNS), P1 (token depletion 30s), P1 (fill latency P95 > 1000ms 2min)
- **2 dashboard rows** added: Zerodha utilization + fill/spread metrics (Row 6–7 in overview dashboard)
- **3 new variables** in `monitoring/variables.tf` for alarm thresholds (tunable without redeploy)
- **5 operator scripts** in `scripts/zerodha/`: `rate_monitor.py`, `candle_prefetch.py`, `position_audit.py`, `budget_optimizer.py`, `stress_test.py`

### Gate 4 — New Signal Types ✅ COMPLETE 2026-05-02

| Task | File | Status |
|------|------|--------|
| RT-T08a | Math helpers + CandleBarAdapter | ✅ DONE 2026-05-02 |
| RT-T08b | ORBStrategy — 1m candles, MARKET_OPEN/NORMAL | ✅ DONE 2026-05-02 |
| RT-T08c | Scalp1mStrategy — EMA(9/21) + volume, NORMAL | ✅ DONE 2026-05-02 |
| RT-T08d | VWAPReversionStrategy — VWAP bands + wick, NORMAL | ✅ DONE 2026-05-02 |
| RT-T08e | IntradayTrend15mStrategy — EMA/ADX, 15m, NORMAL | ✅ DONE 2026-05-02 |
| RT-T08e | PreCloseMomentumStrategy — 5m bias, PRE_CLOSE | ✅ DONE 2026-05-02 |
| RT-T08f | strategies/__init__.py + CandleBarAdapter registry | ✅ DONE 2026-05-02 |

Gate 4 deliverables:
- **7 new files** in `services/strategy_engine/strategies/`
- **All 5 strategies** default `paper_trade=True` — must run 5 trading days in paper mode before live capital
- **CandleBarAdapter** bridges `IntradayCandleStream.on_candle` → strategy `on_bar()` dispatch
- **Interval routing**: ORB/Scalp/VWAP use 1m candles; Trend15m uses 15m; PreClose uses 5m
- **Daily reset protocol**: all strategies expose `reset_daily()` — call at POST_CLOSE via phase governor

⚠️ **NEXT REQUIRED STEP**: 5-day paper trading validation before any strategy is enabled for live capital.
   Monitor via `scripts/zerodha/rate_monitor.py` and CloudWatch `QuantEmbrace/ZerodhaRateLimit` namespace.
   Flip `paper_trade=False` only after: ≥5 sessions, no P0/P1 alarms, signal count within expected range.

---

---

## ✅ PHASE 3 — Decouple Strategy & Scale Horizontally — COMPLETE 2026-05-05

**Architecture**: `architecture/phase3_design_review.md` (v1.2) — APPROVED 2026-05-05
**Decision log**: `memory/decisions.md` ADR-013

### All Phase 3 Tasks — DONE

| Task | File(s) | Status |
|------|---------|--------|
| PHASE3-001 | `runners/circuit_breaker.py` — dual-threshold CLOSED/OPEN/HALF_OPEN state machine | ✅ DONE |
| PHASE3-002 | `runners/strategy_runner.py` — TICK\|CANDLE interface, circuit breaker, paper_trade, daily cap | ✅ DONE |
| PHASE3-003 | `consumers/dynamo_candle_consumer.py` — 500ms poll, 3-min overlapping lookback, dedup, phase filter | ✅ DONE |
| PHASE3-004 | `config/strategy_config_loader.py` — DynamoDB hot-reload every 60s, circuit breaker reset flow | ✅ DONE |
| PHASE3-005 | `strategy_engine/service.py` — 4-loop asyncio.gather, all 6 strategies in StrategyRunner | ✅ DONE |
| PHASE3-006 | `shared/models/signal.py` — paper_trade field, to_dict/from_dict | ✅ DONE |
| PHASE3-007 | `kafka_signal_publisher.py` — paper_trade in event payload | ✅ DONE |
| PHASE3-008 | `risk_engine/consumers/kafka_signal_consumer.py` — paper_trade passthrough | ✅ DONE |
| PHASE3-009 | `execution_engine` — paper_trade branch, _handle_paper_order() | ✅ DONE |
| PHASE3-010 | `data_ingestion/candle_stream.py` — constructor injection | ✅ DONE |
| PHASE3-011 | `infra/terraform/modules/ec2_services/iam.tf` — candle-cache + strategy-config IAM policies | ✅ DONE |
| PHASE3-012/013 | Kafka topic partition changes (ticks.nse/ticks.us → 4 partitions) | ✅ DONE |
| PHASE3-014 | Terraform: candle-cache + strategy-config DynamoDB tables | ✅ DONE |
| PHASE3-015 | `scripts/strategy/config.py` — operator CLI (list/get/set/enable/disable/go-live/paper) | ✅ DONE |
| PHASE3-016 | `scripts/strategy/reset_circuit_breaker.py` — manual CB reset via DynamoDB flag | ✅ DONE |
| PHASE3-017 | `scripts/strategy/verify_candle_cache.py` — pre-flight check | ✅ DONE |
| PHASE3-018 | `tests/unit/test_strategy_runner.py` — 43 checks, all passing | ✅ DONE |
| PHASE3-019 | `tests/unit/test_dynamo_candle_consumer.py` — dedup, pagination, parse, trace_id | ✅ DONE |
| PHASE3-020 | `tests/unit/test_strategy_config_loader.py` — hot-reload, reset flow, seed | ✅ DONE |

### Key Bugs Fixed (Phase 3 implementation)

- **5 candle strategies producing zero signals** → fixed via `_candle_processing_loop()` polling DynamoDB candle-cache every 500ms
- **Single strategy crash halting all** → fixed by StrategyRunner per-strategy failure domain
- **Config changes requiring restarts** → fixed by StrategyConfigLoader 60s hot-reload
- **paper_trade never reaching execution** → fixed by full Signal.paper_trade pipeline

### Post-Review Fixes (2026-05-06) — All merged after Hari code review

| # | Severity | File | Fix |
|---|----------|------|-----|
| 1 | CRITICAL | `data_ingestion/candle_stream.py` | Writer schema mismatch — added `market` to `CandleData`, split `"{EXCHANGE}:{SYMBOL}"` key in `_parse_candle()`, rewrote `_write_candle()` to use `PK={market}#{instrument}#{interval}#{candle_open_time}` with `market` + `candle_open_time` as top-level attributes (old `CANDLE#…` PK and `SK` removed) |
| 2 | CRITICAL | `execution_engine/service.py` | Removed invalid `extra_metadata={"paper": True, …}` kwarg from `publish_fill()` call; added explicit `fill_time=datetime.now(timezone.utc)` |
| 3 | HIGH | `strategy_engine/service.py` | `signal_out.generated_at = candle.candle_close_time` stamped before publish — ensures deterministic `signal_id` (sha256 of `generated_at`) across service restarts |
| 4 | HIGH | `strategy_engine/service.py` | `await self._config_loader.refresh_all()` inserted before `set_ready(True)` — service no longer declares readiness with 60-second config blindspot |
| 5 | HIGH | `tests/unit/test_execution_integration.py` | `max_delay=0.0` → `retry_max_delay=0.0` in `_settings()` fixture; added `dynamodb_table_positions` + `dynamodb_table_risk_state` to `_order_manager_settings()` |
| 6 | MEDIUM | `strategy_engine/service.py` | Comment corrected: "Three concurrent loops plus inline kill-switch listener" (was: "Four concurrent loops") |
| 7 | MEDIUM | `strategy_engine/config/strategy_config_loader.py` | Removed `"circuit_breaker_opened_at": None` from `seed_defaults()` — DynamoDB resource API rejects Python `None`; attribute now only written when circuit opens |

### Regression Test Added (2026-05-06)

`tests/unit/test_dynamo_candle_consumer.py` — `TestWriterToConsumerSchemaRoundTrip` class (18 tests):
- Mirrors `_write_candle()` logic to produce the exact DynamoDB wire-format item
- Converts via `_unwire_dynamo_item()` (simulates resource API deserialization)
- Feeds through `_parse_item()` and asserts field-for-field round-trip correctness
- Explicitly proves old broken schema (`CANDLE#…` PK, no `market`) returns `None` from parser
- Covers NSE + US instruments, all 3 active intervals (1m/5m/15m), OHLCV, trace_id determinism

### Final Verification (2026-05-06)

44/44 static + runtime checks passed:
- 4 targeted test files parse without syntax errors
- All 7 post-review fixes confirmed present in source
- Regression test class present with `_build_wire_item()` / `_unwire_dynamo_item()` helpers
- 17 writer→consumer round-trip cases pass

### Next Gate: Paper Trading Validation (Deployment)

All 6 strategies are at `paper_trade=True` in DynamoDB strategy-config. Before promoting any to live:
- ≥5 clean sessions with no P0/P1 alarms
- `CandleBarsProcessed > 0` for all candle strategies during NORMAL phase
- `PaperSignalsByStrategy` within expected range
- No `CircuitBreakerOpen` alarms
- Promote via: `python scripts/strategy/config.py go-live {strategy} --env production`

---

---

## ✅ PHASE 4 — Distributed Risk Engine + Portfolio Layer — COMPLETE 2026-05-06

**Architecture**: `architecture/phase4_design_review.md` (v1.1) — ✅ APPROVED 2026-05-06
**Decision log**: `memory/decisions.md` ADR-014
**Prerequisite**: Phase 3 complete ✅ 2026-05-06

### Design Summary (v1.1 — Zerodha Rate Capacity Aligned)

Phase 4 drops the over-engineered Redis/active-active plan and fills the actual gaps:
1. In-memory `KillSwitchCache` — eliminates per-signal DynamoDB kill switch read
2. `RiskContextBuilder` pre-fetch — 7 parallel DynamoDB reads via asyncio.gather (~5-8ms wall clock)
3. `SpreadGateValidator` — rejects signals when live bid-ask spread > threshold
4. `SectorConcentrationValidator` — enforces sector exposure caps (was never checked before Phase 4)
5. `LiquidityValidator` — rejects illiquid instruments via ADV data from candle cache
6. `RiskAnalyticsEngine` — background portfolio analytics (VaR, sector, NAV), MarketPhase-aware
7. `InstrumentRegistry` — instruments.yaml with per-instrument risk params

### Phase 4 Implementation Tasks — ALL DONE

| Task | File(s) | Priority | Status |
|------|---------|----------|--------|
| PHASE4-001 | `shared/models/risk_context.py` — RiskContext dataclass | P0 | ✅ DONE |
| PHASE4-002 | `risk_engine/cache/kill_switch_cache.py` — in-memory cache, 1s DynamoDB poll | P0 | ✅ DONE |
| PHASE4-003 | `risk_engine/context/risk_context_builder.py` — pre-fetch all validator inputs | P0 | ✅ DONE |
| PHASE4-004 | `risk_engine/validators/spread_gate_validator.py` — live spread from LiveQuotePoller | P1 | ✅ DONE |
| PHASE4-005 | `risk_engine/validators/sector_validator.py` — sector concentration cap | P1 | ✅ DONE |
| PHASE4-006 | `risk_engine/validators/liquidity_validator.py` — ADV from candle cache | P1 | ✅ DONE |
| PHASE4-007 | `risk_engine/analytics/risk_analytics_engine.py` — background VaR+sector+NAV | P1 | ✅ DONE |
| PHASE4-008 | `risk_engine/registry/instruments.yaml` — per-instrument risk params | P1 | ✅ DONE |
| PHASE4-009 | `risk_engine/registry/registry.py` — YAML loader, typed InstrumentConfig | P1 | ✅ DONE |
| PHASE4-010 | `risk_engine/service.py` — wire RiskContext flow, KillSwitchCache, analytics loop | P0 | ✅ DONE |
| PHASE4-011 | `tests/unit/test_risk_context_builder.py` — 42 tests, all passing | P1 | ✅ DONE |
| PHASE4-012 | `tests/unit/test_spread_gate_validator.py` — 30 tests (all 3 context validators), all passing | P1 | ✅ DONE |
| PHASE4-013 | `tests/unit/test_risk_analytics_engine.py` — 47 tests, all passing | P1 | ✅ DONE |

### Bug Fixed During Phase 4 Testing

`risk_context_builder.py` — `pending_quantity` was fetched by `_fetch_pending_quantity()` but
discarded: `PositionState` in `RiskContext` always had `pending_quantity=0.0`. Fix: after
`asyncio.gather`, construct a new frozen `PositionState(confirmed_quantity=...,
avg_entry_price=..., pending_quantity=pending_quantity)` before building `RiskContext`.

### Phase 4 Follow-up — DONE

| Item | Description | Status |
|------|-------------|--------|
| PHASE4-FU-001 | `execution_engine/polling/live_quote_poller.py` — add DynamoDB write of `QUOTE#{market}#{symbol}/LATEST`; `ExecutionService` wired to start poller; `test_live_quote_poller_dynamo.py` (22 tests) added | ✅ DONE 2026-05-06 |

### Test Summary (141/141 passing)

- `test_risk_context_builder.py` — 42 tests: all 7 DynamoDB read methods + end-to-end build(), stale spread detection, graceful degradation, pagination, pending_quantity merge bug verified
- `test_spread_gate_validator.py` — 30 tests: SpreadGateValidator, SectorConcentrationValidator, LiquidityValidator (all 3 context validators in one file), graceful degradation for all
- `test_risk_analytics_engine.py` — 47 tests: phase intervals, sector computation, P&L, VaR, snapshot structure, DynamoDB writes, loop control (analytics loop stops after one full compute+persist cycle)
- `test_live_quote_poller_dynamo.py` — 22 tests: QuoteSnapshot.captured_at_utc, DynamoDB write schema (PK/SK/fields), spread_bps=None omitted, multi-instrument concurrency, failure non-fatal, schema cross-check vs RiskContextBuilder reader, poll_cycle integration

---

---

## ✅ PHASE 5 — Data Platform + Feature Store — FULLY OPERATIONAL 2026-05-06

**Architecture**: `architecture/phase5_design_review.md` (v1.1) — ✅ APPROVED 2026-05-06
**Prerequisite**: Phase 4 complete ✅ (including PHASE4-FU-001)

### Design Decisions (v1.1 — approved by Hari)

- Feature set: RSI-14, EMA-9/21, VWAP, ATR-14, ADX-14, MACD/signal/hist, volume_ratio
- Dual DynamoDB writes: SK=LATEST (online, TTL 24h) + SK=CANDLE#{ts} (intraday, TTL 7d)
- Interval-aware staleness: max(5min, 2×interval+1min) → 1m=5min, 5m=11min, 15m=31min
- volume_ratio uses adv_20d from prices table / candle_prefetch (no extra broker API calls)
- US feature store deferred to Phase 6 (no Alpaca candle stream yet)

### All Phase 5 Tasks — DONE

| Task | File(s) | Priority | Status |
|------|---------|----------|--------|
| PHASE5-001 | `shared/models/feature_set.py` — FeatureSet frozen dataclass | P0 | ✅ DONE |
| PHASE5-001 | `data_ingestion/features/feature_engine.py` — FeatureEngine, all 9 features | P0 | ✅ DONE |
| PHASE5-002 | `data_ingestion/features/feature_writer.py` — dual DynamoDB writes, TTL, None-omit | P0 | ✅ DONE |
| PHASE5-003 | `shared/features/feature_reader.py` — get_latest, interval-aware staleness, graceful None | P0 | ✅ DONE |
| PHASE5-004 | `data_ingestion/candle_stream.py` — feature hook on_candle, rolling history buffer | P0 | ✅ DONE |
| PHASE5-005 | `data_ingestion/features/feature_archiver.py` — POST_CLOSE CANDLE# query → S3 parquet | P1 | ✅ DONE |
| PHASE5-006 | `infra/terraform/modules/dynamodb/features.tf` — features table + IAM policies (reference) | P1 | ✅ DONE |
| PHASE5-007 | `shared/config/settings.py` — added `dynamodb_table_features` | P1 | ✅ DONE |
| PHASE5-008 | `tests/unit/test_feature_engine.py` — 61 tests: all features, lookback guards, math | P0 | ✅ DONE |
| PHASE5-009 | `tests/unit/test_feature_writer.py` — 24 tests: dual-write, schema, TTL, None-omit, failure | P1 | ✅ DONE |
| PHASE5-010 | `tests/unit/test_feature_reader.py` — 32 tests: staleness 1m/5m/15m, degradation, schema guard | P1 | ✅ DONE |

### PHASE5-FU-001 — Integration Wiring — DONE 2026-05-06

4 integration gaps found by Hari after core delivery — all fixed:

| Fix | File | Status |
|-----|------|--------|
| FU-001 service wiring | `data_ingestion/service.py` — `_setup_feature_pipeline()` method added; FeatureEngine/Writer/Archiver instantiated; archiver registered with MarketPhaseGovernor; IntradayCandleStream started as background task | ✅ DONE |
| FU-001 IAM inline | `ec2_services/iam.tf` — DynamoDBFeaturesWrite added to data_ingestion policy; DynamoDBFeaturesRead added to strategy_engine + risk_engine policies | ✅ DONE |
| FU-001 S3 lifecycle | `s3/main.tf` — features/ tiering rule (90d→IA, 365d→Glacier IR) consolidated into ohlcv_data lifecycle; conflicting standalone resource removed from `dynamodb/features.tf` | ✅ DONE |
| FU-001 wiring tests | `tests/unit/test_data_ingestion_feature_wiring.py` — 16 tests: instantiation, archiver registration, candle stream has feature instances, graceful degradation on broker/loader failures, stop() cancels both candle stream and governor tasks | ✅ DONE |

### Bugs Fixed During Testing

`feature_engine.py` — `_compute_macd` guard was `n < slow + signal - 1` (= 34). Design spec
mandates 35 minimum (conservative: ensures EMA(9) signal has a full seed period rather than
seeding on the last available value). Fixed to `n < slow + signal` (= 35). All 61 tests pass.

### Post-Review Bug Fix Sprints — ALL DONE 2026-05-07

Three successive review rounds by Hari after PHASE5-FU-001 core delivery.

**Round 1 — 4 bugs (2026-05-06):**

1. **CRITICAL — FeatureWriter kwarg mismatch**: `service.py` called `FeatureWriter(..., table_name=...)` but real constructor param is `features_table`. Raises `TypeError` at runtime, swallowed by non-fatal catch, silently disables entire feature pipeline. Fixed: kwarg renamed to `features_table`.

2. **CRITICAL — Governor loop never started**: `MarketPhaseGovernor` was created and registered, but `asyncio.create_task(governor.run())` was never called. POST_CLOSE never fires to archiver; candle stream never receives initial phase. Fixed: added `self._phase_governor_task = asyncio.create_task(self._phase_governor.run(), name="phase_governor")`.

3. **HIGH — `instrument_tokens` always empty**: `service.py` checked `hasattr(loader, "token_map")` — `InstrumentLoader` has no such attribute. `IntradayCandleStream` queue was always empty. Fixed: removed dead branch.

4. **CRITICAL — Invalid HCL `Statement = [,`**: Pre-existing at `iam.tf` line 190 (strategy_engine) and line 434 (risk_engine). `terraform fmt -check` fails immediately. Fixed: removed trailing comma from both locations.

Also fixed in Round 1:
- `stop()` now cancels and awaits `_phase_governor_task` + calls `await self._phase_governor.stop()`
- `_MarketPhaseGovernor` test stub gained `async def run()` / `async def stop()` coroutines
- `_FeatureWriter` stub corrected to `features_table` kwarg (was masking Bug 1 in tests)
- 2 new tests added: `test_phase_governor_task_created` (Bug 2 regression) and `test_stop_cancels_governor_task`

**Round 2 — 3 findings (2026-05-06):**

1. **HIGH — `instrument_tokens` always `{}`**: Root cause of Round 1 Bug 3. Dead branch removed but no replacement mechanism. Added `get_instrument_tokens(exchange, symbols)` to `ZerodhaBrokerClient` (calls `kite.instruments()` via `run_in_executor`, filters to known symbols, returns `{f"{exchange}:{tradingsymbol}": int(token)}`). `_setup_feature_pipeline()` calls this after broker connect.

2. **MEDIUM — Governor starts before candle stream registers**: `asyncio.create_task(governor.run())` was placed before `IntradayCandleStream.__init__` (which registers `on_phase_change`). Stream missed initial phase broadcast. Fixed: governor task created AFTER candle stream construction.

3. **LOW — `terraform fmt -check` failing**: iam.tf `Statement`-level attributes had col-9 alignment padding. Corrected to exactly 1 space before `=` inside all `jsonencode({})` object expressions (103 affected lines).

New tests in Round 2: `test_candle_stream_receives_non_empty_token_map`, `test_candle_stream_registered_with_governor_before_task_start`.

**Round 3 — 2 findings (2026-05-07):**

1. **HIGH — Dangerous `None` fallback in token resolution**: `symbols=set(nse_symbols) if nse_symbols else None` — if InstrumentLoader returns empty list, this passes `symbols=None` to `get_instrument_tokens()`, pulling all ~1800 NSE instruments silently. Fixed fail-closed: guard on `not nse_symbols` → skip stream entirely with warning; always pass `symbols=set(nse_symbols)` explicitly (never `None`).

2. **LOW — terraform fmt alignment wrong direction**: Round 2 fix used col-9 multi-space alignment. Reverted: `terraform fmt` uses **single space** for all `=` inside `jsonencode({})` object literal expressions. All 103 Statement-level attributes now verified as exactly 1 space.

New test in Round 3: `test_candle_stream_skipped_when_no_nse_symbols`.

### Test Summary (277/277 passing)

- Phase 4 tests unchanged: 141/141 ✅
- `test_feature_engine.py`  — 61 tests: all 9 features math, lookback boundary conditions, RSI/ADX bounds, MACD histogram invariant, FeatureSet structure
- `test_feature_writer.py`  — 24 tests: dual LATEST+CANDLE write, PK/SK format, all attribute types, None-omission, TTL 24h vs 7d, failure non-fatal
- `test_feature_reader.py`  — 32 tests: staleness threshold math (1m=300s/5m=660s/15m=1860s), fresh/stale items, schema version guard, DynamoDB error → None, all field parsing
- `test_data_ingestion_feature_wiring.py` — 19 tests: service wiring, archiver registration, governor task started, candle stream token map non-empty, governor ordering, fail-closed empty universe, graceful degradation, shutdown (candle + governor tasks)

---

---

## ✅ PHASE 6 — AI/ML Signal Enrichment — COMPLETE 2026-05-11

**Architecture**: `architecture/system_design.md` (Layer 5, Phase 6 section) — ✅ UPDATED 2026-05-11
**Decision log**: `memory/decisions.md` ADR-014 §5 (enrichment pipeline)
**Prerequisite**: Phase 5 complete ✅ (FeatureReader, features DynamoDB table, data_ingestion feature pipeline)

### Design Summary

Phase 6 inserts a new asynchronous ML enrichment hop between strategy_engine and risk_engine:

```
signals.pending (v3.0)
    → ai_engine (aiengine-v1 consumer group) [joblib HMM + GBT inference, c6g.large EC2]
    → signals.enriched (v4.0: + regime, quality_score, filtered, enrichment_latency_ms)
    → risk_engine (risk-v1, KafkaEnrichedConsumer)
    → signals.approved
```

Fallback path (EnrichmentWatchdog activates when aiengine-v1 lag > threshold):
```
signals.pending → risk_engine (risk-v1, existing KafkaSignalConsumer) → signals.approved
```

### All Phase 6 Tasks — DONE

| Task | File(s) | Status |
|------|---------|--------|
| PHASE6-001 | `services/shared/models/enriched_signal.py` — `EnrichedSignal` frozen dataclass + schema v4.0; `degraded_enrichment()` helper | ✅ DONE |
| PHASE6-002 | `services/ai_engine/model_registry.py` — real S3+joblib load, hot-reload every 60s, stub fallback, thread-safe RWLock | ✅ DONE |
| PHASE6-003 | `services/ai_engine/features/feature_pipeline.py` — `FeatureReader` wiring, interval-aware staleness, graceful None | ✅ DONE |
| PHASE6-004 | `services/ai_engine/models/regime_classifier.py` — HMM wrapper, 4-state regime, `regime="unknown"` on any error | ✅ DONE |
| PHASE6-005 | `services/ai_engine/models/signal_quality_scorer.py` — GBT wrapper, `quality_score=0.5` on any error, `filtered=False` fallback | ✅ DONE |
| PHASE6-006 | `services/ai_engine/enricher.py` — `SignalEnricher` orchestrator, always returns EnrichedSignal, publishes latency CW metric | ✅ DONE |
| PHASE6-007 | `services/ai_engine/consumers/kafka_signal_consumer.py` — `KafkaSignalConsumer` (aiengine-v1), MSK IAM, v3.0 parse | ✅ DONE |
| PHASE6-008 | `services/ai_engine/publishers/kafka_enriched_publisher.py` — `KafkaEnrichedPublisher` → `signals.enriched` v4.0 | ✅ DONE |
| PHASE6-009 | `services/ai_engine/service.py` — Kafka-only loop + asyncio health server + `StrategySelector` agent (advisory) | ✅ DONE |
| PHASE6-010 | `services/risk_engine/consumers/kafka_enriched_consumer.py` — dual v4.0/v3.0 schema parsing, `subscribe_to()` topic switch | ✅ DONE |
| PHASE6-010 | `services/risk_engine/consumers/enrichment_watchdog.py` — `EnrichmentWatchdog` + `EnrichmentState`, 500ms lag check, DynamoDB config, CW metric | ✅ DONE |
| PHASE6-010 | `services/risk_engine/service.py` — `_enriched_processing_loop()`, `_signal_from_enriched()`, `_publish_approved_enriched()`, watchdog wired into start/stop/gather | ✅ DONE |
| PHASE6-011 | `scripts/kafka/create_topics.py` — `signals.enriched` topic added (2 partitions, 1h retention, symbol key) | ✅ DONE |
| PHASE6-011 | `infra/terraform/modules/kafka/outputs.tf` — `signals.enriched` in topic_config + `kafka_policy_arn_ai_engine` | ✅ DONE |
| PHASE6-012 | `infra/terraform/modules/ec2_services/variables.tf` — `ai_engine_instance_type` (c6g.large), `secrets_anthropic_arn` | ✅ DONE |
| PHASE6-012 | `infra/terraform/modules/ec2_services/iam.tf` — `ai_engine` IAM role/policy/profile; conditional Secrets Manager policy | ✅ DONE |
| PHASE6-012 | `infra/terraform/modules/ec2_services/main.tf` — `ai_engine` ASG (min=1, max=2, single AZ), scale-out policy, scheduled on/off | ✅ DONE |
| PHASE6-012 | `infra/terraform/modules/ec2_services/outputs.tf` — ai_engine ASG/role/profile outputs | ✅ DONE |
| PHASE6-012 | `infra/terraform/modules/ec2_services/userdata/ai_engine.sh` — full boot script: Docker, SSM, CloudWatch agent, MSK bootstrap, governor=performance for Graviton2 | ✅ DONE |
| PHASE6-013 | `infra/terraform/modules/dynamodb/ai_engine.tf` — `regime-log` table (PK=REGIME#…, SK=SESSION#…, TTL 30d) + `strategy-recommendations` table (PK=DATE#…, SK=STRATEGY#…, TTL 30d) | ✅ DONE |
| PHASE6-013 | `infra/terraform/modules/dynamodb/outputs.tf` — all 10 table names/ARNs; updated `all_table_arns` | ✅ DONE |
| PHASE6-015 | `infra/terraform/modules/monitoring/ai_engine_alarms.tf` — 6 CloudWatch alarms: latency P99, fallback active, classification errors, quality filter rate, signals silent, hot-reload errors | ✅ DONE |
| PHASE6-016 | `tests/unit/test_phase6_enrichment.py` — 6 test classes (350+ lines): EnrichedSignalModel, RegimeClassifier, SignalQualityScorer, SignalEnricher, EnrichmentWatchdog, pipeline integration | ✅ DONE |

### Key Design Decisions

- **c6g.large for ai_engine**: CPU-bound HMM inference at signal throughput rates. t4g burstable instances risk credit exhaustion.
- **Single AZ for ai_engine ASG**: Stable Kafka partition assignment for aiengine-v1 consumer group. Partition migration during AZ-hop rebalance would cause lag spikes that trigger fallback.
- **Enrichment never halts trading**: `EnrichmentWatchdog` auto-fallback ensures signals flow through risk_engine even if ai_engine is down.
- **signals.enriched has 1h retention**: Matches signals.pending. Enriched signals expire quickly — Phase 7 adds retry infrastructure for the enriched path (signals.enriched.retry + DLQ).
- **regime-log no PITR**: Advisory analytics data, not trading state. Point-in-time recovery not cost-justified.
- **EnrichmentFallbackActive alarm in `QuantEmbrace/RiskEngine` namespace**: Metric is published by `EnrichmentWatchdog._publish_fallback_metric()` which runs inside risk_engine.

### Test Summary

- `tests/unit/test_phase6_enrichment.py` — 6 test classes using importlib.util direct loading + sys.modules stubs:
  - `TestEnrichedSignalModel`: round-trip serialization, instrument_id property, degraded_enrichment helper
  - `TestRegimeClassifier`: normal path (HMM mock), stub model, graceful degradation on exception
  - `TestSignalQualityScorer`: threshold filtering, degraded on model error
  - `TestSignalEnricher`: full enrich path, never raises on crash, schema_version="4.0"
  - `TestEnrichmentWatchdog`: state machine (fallback at window=2, recovery at window=5, metric published)
  - `TestEnrichmentPipelineIntegration`: end-to-end dict envelope, round-trip

### Post-Phase-6 Deployment Checklist

1. Run `python scripts/kafka/create_topics.py --bootstrap-servers <BROKERS>` — creates `signals.enriched` + retry/dlq
2. `terraform apply` in `infra/terraform/environments/{env}/` — creates ai_engine ASG, regime-log/strategy-recommendations DynamoDB tables, 6 CW alarms
3. Build + push `ai_engine` Docker image to ECR
4. Confirm `KAFKA_BOOTSTRAP_SERVERS`, `S3_BUCKET_MODEL_ARTIFACTS`, `DYNAMODB_TABLE_FEATURES`, `DYNAMODB_TABLE_STRATEGY_CONFIG` env vars set in ai_engine userdata
5. Upload initial model artifacts: `s3://quantembrace-model-artifacts/models/regime/{version}/model.joblib` and `s3://quantembrace-model-artifacts/models/quality/{version}/model.joblib`
6. Monitor `QuantEmbrace/AIEngine/SignalsEnrichedCount` → should be > 0 within 1 min of market open
7. Monitor `QuantEmbrace/RiskEngine/EnrichmentFallbackActive` → should be 0 in normal operation
8. Confirm `EnrichmentLatencyHigh` alarm does not fire (P99 < 20ms target)

---

## ✅ Phase 7 — Retry Infrastructure + Live Trading Readiness — COMPLETE (2026-05-11)

### Delivered

| Task | File | Description |
|------|------|-------------|
| PHASE7-001 | `services/risk_engine/consumers/kafka_enriched_consumer.py` | Added `KafkaFailurePublisher` + `publish_retry()` / `publish_dlq()` to `KafkaEnrichedConsumer`. Start/close wired into lifecycle. |
| PHASE7-002 | `services/risk_engine/service.py` | `_enriched_processing_loop()`: expired signals → `publish_dlq()`; processing errors → `publish_retry()` (auto-escalates to DLQ after 3 attempts). Error policy updated in docstring. |
| PHASE7-003 | `services/risk_engine/service.py` | `KafkaRetryReplayer` source_topics extended: `["signals.pending", "signals.enriched", "orders.events"]`. Replayer now drains `signals.enriched.retry → signals.enriched`. |
| PHASE7-004 | `infra/terraform/modules/kafka/main.tf` | Risk engine IAM policy: added consume permissions for `signals.enriched` + `signals.enriched.retry`; added produce permissions for `signals.enriched`, `signals.enriched.retry`, `signals.enriched.dlq`. |
| PHASE7-005 | `tests/unit/test_phase7_integration.py` | 17 unit tests covering: consumer lifecycle, publish_retry/dlq delegation, retry replayer topic wiring, expiry→DLQ, error→retry, duplicate suppression, fallback yield, kill switch halt, approved signal flow. All passing. |
| PHASE7-006 | `scripts/deploy/preflight_check.py` | Pre-market pre-flight check: Kafka connectivity, topics, DynamoDB, S3, Zerodha session, Alpaca account, enrichment lag, kill switch state. Exit 0 = safe, 1 = blocked. |
| PHASE7-007 | `scripts/monitoring/paper_session_report.py` | Daily paper trading report: signal funnel, rejection breakdown, execution quality, per-strategy P&L, circuit-breaker events, go-live readiness assessment against 8 thresholds. |
| PHASE7-008 | `docs/runbooks/go_live_checklist.md` | Complete go-live runbook: 5-day paper criteria, per-strategy promotion table, go-live day procedure (T-60 to T=0), 6 emergency procedures, post-session checklist, rollback procedure. |

### Phase 7 Key Design Decisions

- **Retry not DLQ for transient errors**: `publish_retry()` routes to `signals.enriched.retry` first. `KafkaFailurePublisher` auto-escalates to `.dlq` after `max_retry_attempts=3`. This avoids permanent signal loss on transient DynamoDB timeouts.
- **Expiry always goes to DLQ**: Expired signals have no retry value (the trading window has passed). Routing expired signals to `.retry` would waste CPU on signals that would immediately expire again.
- **Commit after routing, not before**: Offset is committed after `publish_retry()`/`publish_dlq()` returns. If the failure publish itself fails, the consumer will re-deliver the message. This is safe because `_get_risk_decision_reservation()` deduplication prevents double-processing.
- **Go-live threshold of 5 consecutive READY days**: Chosen to avoid promoting strategies that pass one good day but have systemic weaknesses. 5 days covers one full trading week (both NSE and US market patterns).

### Retry Flow (Phase 7)

```
signals.enriched (primary)
    ├──▶  processing error  →  signals.enriched.retry  (attempt 1,2,3)
    │                               └──▶  KafkaRetryReplayer → signals.enriched (replay)
    │         after 3 failures:     └──▶  signals.enriched.dlq
    └──▶  signal expired    →  signals.enriched.dlq  (directly)
```

---

---

## 🚧 PHASE 8 — Production Hardening + Fault Tolerance

**Architecture**: `architecture/phase8_design_review.md` (v1.0) — 🔲 AWAITING APPROVAL 2026-05-08
**Decision log**: `memory/decisions.md` ADR-015 (pending)
**Prerequisite**: Phase 7 complete ✅

### Design Summary

Eight failure modes identified in Phase 7 post-review. All are surgical, defensive-only changes.
No new service boundaries, no Kafka topic additions, no schema version bumps.

| Failure | Root Cause | Mitigation |
|---------|-----------|------------|
| F1 Kafka down | Kill-switch Kafka publish also fails; DynamoDB fallback may be slow | Durable SQLite outbox; broker-halt flag; CI fanout test |
| F2 Zerodha 429 | Global rate bucket starves cancel orders during degradation | Per-endpoint budgets; cancel-priority lane; consumer placement pause |
| F3 ACK timeout | Blind retry on BrokerTimeoutError can duplicate order past idempotency gate | `ACK_UNKNOWN` state; pre-retry broker tag scan |
| F4 Data gap | Post-reconnect candles have corrupted OHLCV; no flag propagated | `data_quality` field on CandleData/Bar; warm-up suppression in StrategyRunner |
| F5 Restart mid-market | New model fields cause silent Pydantic default → wrong position state | Startup reconciliation gate; schema migration helpers |
| F6 Consumer lag/replay | Publish-after-reserve race; offset committed before outbox write | Inbox/outbox pattern; lag-triggered kill switch on risk-v1 |
| F7 Reconciliation drift | Position mismatch logged but trading continues | Hard halt flag in risk-state; `scripts/ops/reconcile.py` three-way tool |
| F8 Stop-loss miss | Child SL rejected after entry fill; no fallback | Broker-native CO/BO → SL-M retry → immediate flatten; orphan detector |

### Phase 8 Tasks

| Task | File(s) | Priority | Status |
|------|---------|----------|--------|
| PHASE8-001 | `shared/models/order.py` + broker resolution — `ACK_UNKNOWN` state + pre-retry tag scan | P0 | 🔲 |
| PHASE8-002 | `shared/zerodha/endpoint_budgets.py` — per-endpoint rate budgets + cancel priority + placement pause | P0 | 🔲 |
| PHASE8-003 | `shared/models/candle.py` + `data_ingestion/candle_stream.py` + `strategy_engine/runners/strategy_runner.py` — `data_quality` field + warm-up suppression | P1 | 🔲 |
| PHASE8-004 | `shared/reconciliation/gate.py` — startup reconciliation gate wired into all 4 service start() methods | P0 | 🔲 |
| PHASE8-005 | `shared/kafka/local_outbox.py` — SQLite durable outbox + `_halt_new_order_intake()` in base_publisher + CI kill-switch fanout test | P0 | 🔲 |
| PHASE8-006 | `risk_engine/processing/signal_inbox.py` + `signal_outbox.py` + `outbox_publisher.py` + `watchdogs/kafka_lag_watchdog.py` — inbox/outbox + lag kill switch | P0 | 🔲 |
| PHASE8-007 | `risk_engine/validators/reconciliation_validator.py` + `scripts/ops/reconcile.py` — hard reconciliation stop + three-way drift tool | P1 | 🔲 |
| PHASE8-008 | `execution_engine/brokers/base_broker.py` 3-tier SL + `execution_engine/monitors/orphan_detector.py` — broker-native SL + flatten + orphan detection | P0 | 🔲 |
| PHASE8-009 | `infra/terraform/modules/dynamodb/signal_processing.tf` — `signal-inbox` + `signal-outbox` tables + 3 CloudWatch alarms | P1 | 🔲 |
| PHASE8-010 | `tests/unit/test_phase8_hardening.py` + `tests/integration/test_kill_switch_fanout.py` — 158+ tests covering all 8 failure scenarios | P1 | 🔲 |

**P0 tasks** must complete before any live capital is deployed.
**P1 tasks** must complete before the second live trading week.

---

## ✅ Institutional Review — Critical Blockers Fixed (2026-05-12)

Full platform review conducted. Three critical blockers identified and fixed before any live capital deployment.

| Blocker | File(s) | Status | Description |
|---------|---------|--------|-------------|
| BLOCKER-001 | `services/risk_engine/validators/loss_validator.py` | ✅ Fixed | Race condition in `_update_daily_pnl_and_nav` — replaced get→put with atomic `update_item ADD realized_pnl :delta` + `ReturnValues=ALL_NEW`. Concurrent fills now always counted. |
| BLOCKER-002 | `services/risk_engine/validators/loss_validator.py` | ✅ Fixed | Unpaginated DynamoDB reads: `_get_unrealized_pnl` (scan) and `_fetch_realized_pnl_from_db` fallback (query) both paginate via `LastEvaluatedKey` loop. Silent 1MB truncation eliminated. |
| BLOCKER-003 | `configs/risk_limits_production.yaml`, `scripts/deploy/preflight_check.py` | ✅ Fixed | Missing production risk config created with all 13 required fields. Preflight check validates config as first check before every session — FAIL in production if file absent. |

**Regression tests added:**
- `tests/unit/test_blocker_001_pnl_race.py` — 7 tests proving atomic ADD is in place
- `tests/unit/test_blocker_002_pagination.py` — 16 tests proving multi-page scan/query

**Remaining high-risk issues (not blockers but must be fixed before scaling):**
- HIGH-001: `_signal_locks` dict in execution_engine never cleaned up → memory leak
- HIGH-002: Strategy engine kill switch uses hand-rolled poll, not `KillSwitchCache`
- HIGH-003: `InstrumentRegistry` load failure silently disables 3 risk validators in production
- HIGH-004: MIS square-off background task has no watchdog / failure alerting
- HIGH-005: `datetime.utcnow()` deprecated in `_OrderPlacementLimiter`

Review document: `docs/reviews/platform_review_2026-05-11.md`

### Pre-Approval Questions (answer before implementation starts)

See `architecture/phase8_design_review.md` § 12 for 5 open questions requiring Hari's input on
SQLite outbox location, drift threshold, orphan grace period, CO instrument scope, and inbox TTL.

---

## Task Dependency Graph

```
TASK-007 (Kill Switch) -----> TASK-001 (Zerodha Auth)
       |                              |
       v                              v
TASK-002 (Alpaca Auth) -------> TASK-005 (Momentum Strategy)
       |                              |
       v                              v
TASK-006 (CI/CD) -----------> TASK-008 (Integration Tests)
       |                              |
       v                              v
TASK-003 (S3 Lifecycle)        TASK-009 (Dashboard)
       |
       v
TASK-004 (CloudWatch Alarms)
```

### Recommended Execution Order

1. **Phase 1 (Foundation)**: TASK-007 (Kill Switch), TASK-002 (Alpaca Auth) -- these enable safe development and testing.
2. **Phase 2 (Pipeline)**: TASK-001 (Zerodha Auth), TASK-005 (Momentum Strategy) -- complete the trading pipeline.
3. **Phase 3 (Quality)**: TASK-006 (CI/CD), TASK-008 (Integration Tests) -- automate quality gates.
4. **Phase 4 (Operations)**: TASK-003 (S3 Lifecycle), TASK-004 (CloudWatch Alarms), TASK-009 (Dashboard) -- operational maturity.

---

## Day 4 Paper Trading Session Closure (2026-05-22)

### Session Outcome
- **Fills**: 0 paper fills — no signals reached execution (expected: candle stream was broken until 15:42 IST)
- **Candle-cache**: 2 candles written (partial success after LocalStack reset at ~15:30 IST)
- **Kill switch**: Not set — safe to trade Day 5
- **Positions**: 0 open positions

### Fixes Applied Today (all merged to main)
- **Fix 1** — `zerodha_broker.py`: `reqsession.timeout = (5, 12)` — root fix for Python 3.11 asyncio `_cancel_and_wait` TCP hang. Each timed-out candle fetch now takes 12s instead of ~120s.
- **Fix 2** — LocalStack full volume reset — deleted corrupted `localstack-data` volume; all 10 tables recreated via `setup_local_tables.py` with correct schemas.
- **Fix 3** — Diagnostic logs added to `candle_stream.py` — `candle_stream.diag_fetch` (WARNING) fires for first 3 fetches per instrument+interval showing `raw_count` and `last_seen`. `candle_stream.candles_fetched` elevated to WARNING. Both fire at next NSE market open (2026-05-23 03:45 UTC).
- ✅ **FIX-4** — `scripts/kill_switch_cli.py` — added `.env` dotenv loading before `get_settings()` so `DYNAMODB_TABLE_PREFIX` is in `os.environ` when `_apply_table_prefix` validator runs. Without this the CLI targeted `quantembrace-risk-state` instead of `quantembrace-development-risk-state`.

### Pre-Day-5 Required Actions (before 2026-05-23 03:45 UTC)
- [ ] **CRITICAL**: Refresh Zerodha access token — current token expires at ~02:00 UTC (Zerodha tokens expire daily at 07:30 IST). Run `python scripts/zerodha_login.py` before 03:45 UTC.
- [ ] **CHECK**: At 03:45 UTC, watch CloudWatch/docker logs for `candle_stream.diag_fetch` messages. See `memory/candle_stream_debug.md` for diagnostic decision tree.

### Preflight Script Gaps (FIX-5 — not yet fixed)
`scripts/deploy/preflight_check.py` has mismatched table names for local dev:
- Checks for `{prefix}-order-state` but table is named `{prefix}-orders`
- Checks for `{prefix}-risk-decisions` (table doesn't exist — kill switch is in `{prefix}-risk-state`)
- Checks for `{prefix}-kill-switch` (doesn't exist in local setup)
- `check_s3_buckets()` and `check_dynamodb()` don't set `endpoint_url=http://localhost:4566` — fail against real AWS in dev
- Fix: update table names and add LocalStack endpoint detection (read `AWS_ENDPOINT_URL` from env)

---

## Day 5 Paper Trading Session (2026-05-25)

### Session Focus: Monitoring Infrastructure

**Both deliverables complete:**

**Option A — Offline Monitoring CLI**
- `scripts/monitoring/__init__.py` — empty package init
- `scripts/monitoring/paper_trading_monitor.py` — programmatic 15-section report runner; loads LiveCounters from JSON; `--watch N` mode; ANSI colour status badge; args: `--prefix`, `--endpoint`, `--counters`, `--watch`, `--trading-mode`
- `scripts/monitoring/seed_local_positions.py` — seeds LocalStack with 4 paper positions (RELIANCE LONG +50, INFY LONG +100, HDFCBANK SHORT -25, TCS LONG +30 with exit_order_id) + kill switch INACTIVE; `--clear` flag
- `scripts/monitoring/sample_counters.json` — full realistic LiveCounters stub; 5 TEE events, 2 strategies, realized_pnl=4206, unrealized from 4 DynamoDB positions
- **Verified**: Full GREEN 15-section report at 15:40 IST

**Option B — LiveCounters Wire-Up into Execution Engine**
- `services/execution_engine/exit/exit_order_router.py` — `live_counters` param; increments router_paper_exits, router_live_attempts/blocked, router_idempotency_skips/successes, router_failed_routes, router_backtest_exits; accumulates realized_pnl (LONG/SHORT formula)
- `services/execution_engine/monitors/trade_exit_engine.py` — `live_counters` param; sets tee_running/poll_interval; increments stop_loss/take_profit/trailing/trailing_activated/unmanaged counters; appends formatted event string to tee_latest_events (capped 10)
- `services/execution_engine/mis_square_off.py` — `live_counters` param; writes mis_positions_discovered, long/short counts, orders_placed/rejected, positions_flat, at_deadline, kill_switch_activated
- `services/execution_engine/service.py` — `self._live_counters = LiveCounters()` in __init__; passes to all 3 components; populates recon_ fields from reconciliation report after startup; sets execution_engine_up=True; adds `_monitoring_flush_loop()` task writing `/tmp/qe_live_counters.json` every 60s (atomic via os.replace); configurable via `QE_MONITORING_COUNTERS_PATH` + `QE_MONITORING_FLUSH_INTERVAL`

### Session State at Close
- Kill switch: INACTIVE — safe to trade Day 6
- Open positions: 4 seeded in LocalStack for monitoring (RELIANCE, INFY, HDFCBANK, TCS)
- Paper fills Day 5: 0 (execution service not running live — monitoring-only session)
- Candle stream: diagnostic logs still active, Day 6 observation pending
- Monitoring status: **GREEN** at 15:40 IST

---

## Pre-Day-6 Required Actions (before 2026-05-26 03:45 UTC)

| Priority | Action | Command | Deadline |
|----------|--------|---------|----------|
| CRITICAL | Refresh Zerodha access token | `python scripts/zerodha_login.py` | Before 02:00 UTC |
| HIGH | Watch for candle_stream.diag_fetch at market open | `docker logs -f data_ingestion \| grep diag_fetch` | 03:45 UTC |
| HIGH | Confirm candles accumulating in candle-cache table | Check after 04:15 UTC | 04:30 UTC |
| ~~MEDIUM~~ | ~~Fix preflight script (FIX-5)~~ | ✅ DONE 2026-05-25 | — |

### Candle Stream Decision Tree (Day 6 — use diagnostic logs)
| `diag_fetch` shows | Diagnosis | Action |
|---|---|---|
| `raw_count=0` | Kite returning empty data | Check historical_data API params / token validity |
| `raw_count>0`, no `candles_fetched` log | `_last_candle_dt` comparison filtering all candles | Fix dedup logic in `_fetch_candles()` |
| `candles_fetched` fires, table empty | DynamoDB write error | Check for `dynamo_write_error` log |
| `candles_fetched` fires, candles accumulate | FIXED | Remove diag logs, restore `candles_fetched` to DEBUG |

### FIX-5 Detail (preflight_check.py)
Wrong table names to fix:
- `{prefix}-order-state` → `{prefix}-orders`
- `{prefix}-risk-decisions` → `{prefix}-risk-state`
- `{prefix}-kill-switch` → remove (lives in risk-state)
- Add `endpoint_url = os.environ.get("AWS_ENDPOINT_URL")` to `check_dynamodb()` and `check_s3_buckets()`

---

## Open Items Carried Forward to Day 6+

| ID | Severity | Description | When |
|----|----------|-------------|------|
| HIGH-001 | HIGH | `_signal_locks` dict in execution_engine never cleaned up → memory leak | Pre-live |
| HIGH-002 | HIGH | Strategy engine kill switch uses hand-rolled poll, not KillSwitchCache | Pre-live |
| HIGH-003 | HIGH | InstrumentRegistry load failure silently disables 3 risk validators | Pre-live |
| HIGH-004 | MEDIUM | MIS square-off background task has no watchdog/failure alerting | Pre-live |
| HIGH-005 | LOW | `datetime.utcnow()` deprecated in `_OrderPlacementLimiter` | Pre-live |
| ~~FIX-5~~ | ~~MEDIUM~~ | ~~preflight_check.py wrong table names + no LocalStack endpoint~~ | ✅ DONE 2026-05-25 |
| PHASE8 | P0 | All 10 Phase 8 production hardening tasks unstarted — required before live capital | Pre-live |
| CANDLE | P1 | Candle stream 0-write mystery — diag logs active, Day 6 observation will tell | Day 6 |
