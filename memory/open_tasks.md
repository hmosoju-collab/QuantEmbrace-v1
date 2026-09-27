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

> **Status reconciled against code 2026-05-30** (read-only). The original "all 🔲 unstarted"
> tracking was stale — roughly half of Phase 8 is built. See the
> **"Phase 8 + HIGH reconciliation (2026-05-30)"** section below for full file:line evidence.
> Legend: ✅ DONE · ◐ PARTIAL · 🔲 OPEN.

| Task | File(s) | Priority | Status (2026-05-30) |
|------|---------|----------|--------|
| PHASE8-001 | `shared/models/order.py` + broker resolution — `ACK_UNKNOWN` state + pre-retry tag scan | P0 | ✅ DONE — `OrderStatus.ACK_UNKNOWN` (order.py:37); tag-scan + "refusing blind second broker placement" (execution_engine/service.py:1007/1127/1142) |
| PHASE8-002 | `shared/zerodha/endpoint_budgets.py` — per-endpoint rate budgets + cancel priority + placement pause | P0 | 🔲 OPEN — `endpoint_budgets.py` absent |
| PHASE8-003 | `shared/models/candle.py` + `data_ingestion/candle_stream.py` + `strategy_engine/runners/strategy_runner.py` — `data_quality` field + warm-up suppression | P1 | ✅ DONE — `data_quality` on CandleData (candle_stream.py:138) + Bar (base_strategy.py:52); warm-up suppression (strategy_runner.py:216) |
| PHASE8-004 | `shared/reconciliation/gate.py` — startup reconciliation gate wired into all 4 service start() methods | P0 | ◐ PARTIAL — named `shared/reconciliation/gate.py` absent; overlapping runtime enforcement exists via reconciliation_validator (PHASE8-007) |
| PHASE8-005 | `shared/kafka/local_outbox.py` — SQLite durable outbox + `_halt_new_order_intake()` in base_publisher + CI kill-switch fanout test | P0 | ◐ MOSTLY DONE — `LocalOutbox` present + wired (local_outbox.py:36; risk/strategy publishers); `_halt_new_order_intake()` hook not found |
| PHASE8-006 | `risk_engine/processing/signal_inbox.py` + `signal_outbox.py` + `outbox_publisher.py` + `watchdogs/kafka_lag_watchdog.py` — inbox/outbox + lag kill switch | P0 | ◐ PARTIAL — `kafka_lag_watchdog.py` present (lag→kill switch, :56); `signal_inbox.py`/`signal_outbox.py`/`outbox_publisher.py` absent. **RACE OBSERVED IN SESSION 7 (2026-06-01):** ETERNAL SELL 100 (signal cfbd2a95) at 04:27:42 UTC + BUY 201 (signal 895d02b1) at 04:27:43 UTC both approved against FLAT position → net unintended LONG +101. `_signal_locks` keyed by signal_id not symbol; no per-symbol serialisation. See `docs/operations/session-observations/session-7-2026-06-01.md`. |
| PHASE8-007 | `risk_engine/validators/reconciliation_validator.py` + `scripts/ops/reconcile.py` — hard reconciliation stop + three-way drift tool | P1 | ✅ DONE — validator rejects on flag (reconciliation_validator.py:136-137); `scripts/ops/reconcile.py` present |
| PHASE8-008 | `execution_engine/brokers/base_broker.py` 3-tier SL + `execution_engine/monitors/orphan_detector.py` — broker-native SL + flatten + orphan detection | P0 | ◐ PARTIAL — `orphan_detector.py` present & wired; 3-tier SL ladder not at `base_broker.py` (abstract interface only) |
| PHASE8-009 | `infra/terraform/modules/dynamodb/signal_processing.tf` — `signal-inbox` + `signal-outbox` tables + 3 CloudWatch alarms | P1 | 🔲 OPEN — `signal_processing.tf` absent |
| PHASE8-010 | `tests/unit/test_phase8_hardening.py` + `tests/integration/test_kill_switch_fanout.py` — 158+ tests covering all 8 failure scenarios | P1 | ◐ PARTIAL — `test_phase8_hardening.py` present (888 lines); `test_kill_switch_fanout.py` absent |
| PHASE8-011 | `scripts/monitoring/paper_trading_monitor.py` + `services/execution_engine/monitors/live_counters.py` — surface scalp_1m v2 rejection counters in daily monitoring: `scalp_1m.rejected_total`, `scalp_1m.rejected_spread_too_wide`, `scalp_1m.rejected_tp_inside_spread`, `scalp_1m.rejected_net_edge_too_small`, `scalp_1m.stop_distance_source` counts, `scalp_1m.avg_net_expected_edge`, `scalp_1m.strategy_version` | P1 | 🔲 OPEN — counters exist in-memory in `Scalp1mStrategy` and in structured logs; absent from `/tmp/qe_live_counters.json` and `paper_trading_monitor.py`. Blocking `scalp_1m` Stage-1 re-evaluation. See `docs/live-readiness/stage1-strategy-eligibility.md`. |

**P0 tasks** must complete before any live capital is deployed.
**P1 tasks** must complete before the second live trading week.

**Still-open Phase 8 work after reconciliation:** PHASE8-002 (open), PHASE8-009 (open), PHASE8-011 (open);
PHASE8-004 / 005 / 006 / 008 / 010 (partial). PHASE8-001 / 003 / 007 are DONE.

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
- HIGH-001: `_signal_locks` dict in execution_engine never cleaned up → memory leak — **STILL OPEN** (confirmed 2026-05-30: `setdefault` at execution_engine/service.py:778, no cleanup anywhere)
- HIGH-002: Strategy engine kill switch uses hand-rolled poll, not `KillSwitchCache` — **STILL OPEN** (confirmed 2026-05-30: strategy_engine/service.py:705 `_is_kill_switch_active()` does direct `get_item`, not KillSwitchCache)
- ~~HIGH-003: `InstrumentRegistry` load failure silently disables 3 risk validators in production~~ — **✅ FIXED (verified 2026-05-30)**: sector/liquidity/spread_gate now **fail closed** via `risk_data_unavailable_result(...)` on missing data (sector_validator.py:79/84/95, liquidity:98, spread_gate:96). This bullet is stale — validators reject, they do not silently pass.
- HIGH-004: MIS square-off background task has no watchdog / failure alerting — **STILL OPEN** (confirmed 2026-05-30: no MIS watchdog; MIS import only at execution_engine/service.py:322)
  - **Session 9 incident (2026-06-04):** MIS was scheduled (`mis_square_off.scheduled` logged at startup, `seconds_until_close=16244`) but `mis_square_off.starting` never appeared. 30 positions stranded open at session close. Root cause unknown — candidates: asyncio event loop saturated by 30-symbol stale-LTP TEE cycle blocking the sleep wakeup; or all open positions had stale `exit_order_id` locks causing MIS scan to skip them all. **Requires `mis_square_off.watchdog`**: a separate asyncio task that checks at 15:10 IST whether `mis_positions_flat` counter is still null/zero and alerts (or force-closes) if MIS did not run. Must add before next session.
  - **Stranded positions:** cleared automatically by `docker-compose down -v` at next session start — no manual action needed.
- HIGH-005: `datetime.utcnow()` deprecated in `_OrderPlacementLimiter` — **STILL OPEN (LOW)** (confirmed 2026-05-30: zerodha_broker.py:102/129)

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

## ✅ Day 6 Paper Trading Session — Comprehensive Readiness Review (2026-05-26)

### Session Focus

Full paper trading readiness review as Chief Quant Architect + Senior Execution Engineer. All 7 execution gaps identified and fixed. Platform rated GO for next paper session.

### Root Cause: Days 1-4 Zero Trades (CONFIRMED)

`strategy_engine` stamps `Signal.generated_at = candle.candle_close_time` (not poll time). Candle arriving at risk_engine is 7-12s old. `RISK_MAX_SIGNAL_AGE_SECONDS` was at code default 5.0s → 100% rejection. Fix: `RISK_MAX_SIGNAL_AGE_SECONDS: "30"` in docker-compose (hard ceiling 30s enforced in `SignalAgeValidator`).

### Day 6 Fixes Applied (all merged to main, 2026-05-26/27)

| ID | Severity | File | Description | Status |
|----|----------|------|-------------|--------|
| FIX-A | HIGH | `execution_engine/service.py` | `submit_order` return not checked in `_handle_paper_order` → potential duplicate position on concurrent race | ✅ DONE |
| FIX-B | MEDIUM | `execution_engine/service.py` | Universe snapshot never refreshed after midnight IST — day 2+ filtering effectively disabled | ✅ DONE |
| FIX-C | MEDIUM | `docker-compose.yml` (setup) | `PAPER_SEED_NAV` defaulted to ₹50L; `risk_limits_production.yaml` uses ₹10L — 5x NAV mismatch | ✅ DONE |
| FIX-D | MEDIUM | `scripts/setup_local_tables.py` | Strategy configs not seeded — `_DEFAULT_CONFIG` silently caps each strategy at 10 signals/day (60 total) | ✅ DONE |
| FIX-E | LOW | `risk_engine/service.py` | `RiskDecision.to_dict()` missing `enriched` field — session report always showed 0% enrichment rate | ✅ DONE |
| FIX-F | LOW | `scripts/monitoring/paper_session_report.py` | Enrichment detection checked validator_name substring — no validator named "enriched" | ✅ DONE |
| FIX-G | LOW | `tests/unit/` | New regression tests for FIX-A (duplicate suppression) and FIX-B (stale snapshot behavior) | ✅ DONE |

### New Files Created

| File | Purpose |
|------|---------|
| `scripts/deploy/paper_preflight_check.py` | Local paper trading readiness check: env vars, safety gates, DynamoDB, S3, Kafka |
| `tests/unit/test_signal_age_candle.py` | 8 regression tests pinning the candle signal age fix (Days 1-4 root cause) |
| `tests/unit/test_paper_readiness_gaps.py` | 8 tests for paper duplicate suppression and stale snapshot behavior |

### Test Summary (Day 6)

- `test_signal_age_candle.py`: 8/8 passing
- `test_paper_readiness_gaps.py`: 8/8 passing
- All existing unit tests: unaffected

### Pre-Day-7 Required Actions (before next session)

| Priority | Action | Command |
|----------|--------|---------|
| CRITICAL | Wipe LocalStack to pick up new NAV seed and strategy configs | `docker-compose down -v` |
| CRITICAL | Re-run setup to seed ₹10L NAV and strategy configs | `docker-compose run --rm setup` |
| CRITICAL | Run pre-flight check | `python scripts/deploy/paper_preflight_check.py` (must exit 0) |
| HIGH | Refresh Zerodha access token | `python scripts/zerodha_login.py` |
| HIGH | Watch for `candle_stream.diag_fetch` at market open | `docker logs -f data_ingestion \| grep diag_fetch` |

---

## Open Items Carried Forward to Day 7+

> ⚠️ **Historical snapshot (Day 6 close).** Several statuses here are stale — superseded by the
> **"Open Items Carried Forward (reconciled 2026-05-30)"** table at the end of this file. In
> particular HIGH-003 is FIXED and Phase 8 is ~half built, not "all unstarted." Do not action
> from this table.

| ID | Severity | Description | When |
|----|----------|-------------|------|
| HIGH-001 | HIGH | `_signal_locks` dict in execution_engine never cleaned up → memory leak | Pre-live |
| HIGH-002 | HIGH | Strategy engine kill switch uses hand-rolled poll, not KillSwitchCache | Pre-live |
| HIGH-003 | HIGH | InstrumentRegistry load failure silently disables 3 risk validators | Pre-live |
| HIGH-004 | MEDIUM | MIS square-off background task has no watchdog/failure alerting | Pre-live |
| HIGH-005 | LOW | `datetime.utcnow()` deprecated in `_OrderPlacementLimiter` | Pre-live |
| PHASE8 | P0 | All 10 Phase 8 production hardening tasks unstarted — required before live capital | Pre-live |
| CANDLE | P1 | Candle stream diag logs still active — verify at Day 7 market open | Day 7 |

---

## ✅ Day 7 Paper Trading Session (2026-05-27)

### Fixes Applied This Session

Three bugs discovered and fixed during live session:

**FIX-8: Candle stream watchdog** (`services/data_ingestion/service.py`)
- `_candle_stream_task` died silently at 04:48 UTC after Zerodha WebSocket drop (close code 1006). `CancelledError` (BaseException) bypassed `except Exception` in `_stream_loop`.
- Fix: Added `_candle_stream_watchdog()` coroutine — polls `task.done()` every 30s and restarts if dead. Watchdog cancelled before candle stream in `stop()`.
- Verified: 156 candles in DynamoDB within 3 minutes of rebuild.

**FIX-9: Strategy config DynamoDB key prefix mismatch** (`services/strategy_engine/config/strategy_config_loader.py`)
- `_PK_PREFIX = "STRATEGY#"` / `_SK_PREFIX = "CONFIG#"` did not match `"STRATEGY_CONFIG#"` / `"ENV#"` written by `setup_local_tables.py`. Every lookup returned None → fallback to `_DEFAULT_CONFIG(max_signals_per_day=10)`. After 10 early signals, all VWAP signals blocked.
- Fix: Changed constants to `_PK_PREFIX = "STRATEGY_CONFIG#"` / `_SK_PREFIX = "ENV#"`.

**FIX-10: Kill switch staleness during startup warmup** (`services/risk_engine/killswitch/auto_triggers.py` + `service.py`)
- `record_data_tick` was called after age check — age-rejected signals didn't reset the clock.
- No startup grace period — 300s staleness fired 6 min after strategy_engine restart during candle warmup.
- Fix: Moved `record_data_tick` to first line of `validate_signal()`. Added `_startup_grace_secs=600.0` to `KillSwitchMonitor` with uptime check in `_monitor_data_staleness`.

### Session Outcome

- **First paper fill**: `nse_vwap_reversion BUY JINDALSAW qty=429 @ ₹233.0328`, notional ₹99,971 at 05:31 UTC
- **Pipeline status**: All 5 services operational end-to-end after fixes
- **Kill switch**: `active=False` through end of observed session
- **Strategy config**: `refresh_complete updated=7 errors=0`

### CANDLE Task — ✅ RESOLVED

Root cause confirmed: `CancelledError` bypasses `except Exception`; task dies with no watchdog. Watchdog added. Diagnostic logs (`candle_stream.diag_fetch` WARNING, elevated `candles_fetched` WARNING) can be removed — restore `candles_fetched` to DEBUG level in `_fetch_candles`.

### Carried Forward to Day 8+

> ⚠️ **Historical snapshot (Day 7 close).** Several statuses here are stale — superseded by the
> **"Open Items Carried Forward (reconciled 2026-05-30)"** table at the end of this file. In
> particular HIGH-003 and DIAG-LOGS are resolved and Phase 8 is ~half built. Do not action from
> this table.

| ID | Severity | Description | When |
|----|----------|-------------|------|
| HIGH-001 | HIGH | `_signal_locks` dict in execution_engine never cleaned up → memory leak | Pre-live |
| HIGH-002 | HIGH | Strategy engine kill switch uses hand-rolled poll, not KillSwitchCache | Pre-live |
| HIGH-003 | HIGH | InstrumentRegistry load failure silently disables 3 risk validators | Pre-live |
| HIGH-004 | MEDIUM | MIS square-off background task has no watchdog/failure alerting | Pre-live |
| HIGH-005 | LOW | `datetime.utcnow()` deprecated in `_OrderPlacementLimiter` | Pre-live |
| PHASE8 | P0 | All 10 Phase 8 production hardening tasks unstarted — required before live capital | Pre-live |
| US-CONFIG | LOW | `us_momentum_v1` has no DynamoDB config row → uses default cap=10; non-blocking while US markets closed during IST | Day 8 |
| ALPACA-FIX | LOW | Alpaca connector `'str' object has no attribute 'value'` error — investigate enum handling in alpaca_broker.py | Day 8 |
| DIAG-LOGS | LOW | Remove `candle_stream.diag_fetch` WARNING and restore `candles_fetched` to DEBUG in `services/data_ingestion/candle_stream.py` | Day 8 |

---

## ✅ Day 7 Post-Session Fixes (EOD 2026-05-27)

Three additional bugs discovered and fixed after market close during kill switch investigation and MIS analysis:

### FIX-11: exit_order_id stale lock on re-entry (`services/execution_engine/orders/order_manager.py`)

**Root cause**: `apply_fill_to_position` UpdateExpression did not REMOVE `exit_order_id`, `exit_trigger`, `exit_state` when opening or building a position (`direction != "FLAT"`). After an exit closed a position (`direction="FLAT"`), these fields persisted in DynamoDB. Any new entry fill on the same symbol kept the stale lock, causing TradeExitEngine to permanently skip the re-entered position at the "exit already in-flight" guard.

**Fix**: Conditional `REMOVE exit_order_id, exit_trigger, exit_state` appended to UpdateExpression when `direction != "FLAT"`. FLAT writes (exits) leave the field in place (it describes the just-completed exit).

**Files**: `services/execution_engine/orders/order_manager.py` — `apply_fill_to_position()`

---

### FIX-12: MIS square-off called Zerodha directly in paper mode (`services/execution_engine/mis_square_off.py`)

**Root cause**: `_place_mis_close_order` called `self._zerodha.place_order()` with no paper mode branch. In paper mode Zerodha rejects all orders (IP not whitelisted). At 09:35 UTC MIS fired, found 13 open positions, placed 13 Zerodha orders — all failed with `PermissionException: IP not allowed`. Past deadline → kill switch activated. This was the second kill switch event of the session (first was PositionMonitor at 06:25 UTC, second was MIS at 09:35 UTC).

**Fix**: Added `paper_trading: bool = True` to `MISSquareOffManager.__init__`. In `_place_mis_close_order`: when `self._paper_trading`, call `self._order_manager.apply_fill_to_position(...)` directly (simulated fill at `last_price` or `avg_entry_price`) instead of Zerodha. Paper-mode MIS closes now correctly zero-out positions via DynamoDB. Wired `paper_trading=getattr(self._settings.execution, "paper_trading", True)` in `service.py` MIS constructor call.

**Files**: `services/execution_engine/mis_square_off.py`, `services/execution_engine/service.py`

---

### FIX-13: MIS past-deadline skip guard — false-positive kill switch on container restart (`services/execution_engine/mis_square_off.py`)

**Root cause**: `_seconds_until_ist(target)` returns `max(0.0, delta)` — when current time is past the target, returns `0.0` and MIS fires immediately. On container restart after 15:10 IST (e.g., during post-market debugging), MIS fires instantly, finds positions open, past deadline → activates kill switch. This caused kill switch to re-activate on EVERY container restart after market close.

**Fix**: Added past-deadline skip guard at the start of the `run()` loop. When both `_seconds_until_ist(CLOSE_TIME)` and `_seconds_until_ist(DEADLINE_TIME)` return `0.0`, logs `mis_square_off.skipped_past_deadline` and sleeps until the next trading day instead of executing. Verified: third container rebuild logged `skipped_past_deadline`, no kill switch activation, clean startup.

**Files**: `services/execution_engine/mis_square_off.py`

---

### Bug 2 Fix Deployed: PositionMonitor paper gate (`services/execution_engine/service.py`)

**Root cause (Bug 2)**: `PositionMonitor` was instantiated unconditionally. It monitors real Zerodha broker positions vs paper DynamoDB. Real Zerodha has 0 positions; paper DynamoDB has all positions → persistent mismatch → kill switch activated at 06:25 UTC.

**Fix already existed on disk** (`_is_paper` gate at line ~390) but running container was built before the fix. Confirmed via `docker exec grep "_is_paper"` inside old container: gate absent. After rebuild: `_is_paper` gate present at line 390, `position_monitor.started` absent from new logs.

**Files**: `services/execution_engine/service.py`

---

### Final Container State After All Fixes

```
mis_square_off.skipped_past_deadline     ← FIX-13
market_phase_governor.started initial_phase=POST_CLOSE
tee.started mode=paper                    ← TEE active in paper mode
execution_service.started
(no position_monitor.started)             ← Bug 2 fix confirmed
(no kill_switch.activated)                ← Clean startup
```

---

## Open Items Carried Forward (reconciled 2026-05-30)

> Statuses below verified against actual code, read-only. Resolved items are struck through.
> Full evidence in **"Phase 8 + HIGH reconciliation (2026-05-30)"** immediately after this table.

| ID | Severity | Description | Status |
|----|----------|-------------|--------|

| HIGH-001 | HIGH | `_signal_locks` dict in execution_engine never cleaned up → memory leak | 🔲 OPEN (service.py:778, no cleanup) |
| HIGH-002 | HIGH | Strategy engine kill switch uses hand-rolled poll, not KillSwitchCache | 🔲 OPEN (strategy_engine/service.py:705) |
| ~~HIGH-003~~ | ~~HIGH~~ | ~~InstrumentRegistry load failure silently disables 3 risk validators~~ | ✅ FIXED — fail-closed via `risk_data_unavailable_result` (sector:79/84/95, liquidity:98, spread_gate:96) |
| HIGH-004 | MEDIUM | MIS square-off background task has no watchdog/failure alerting | 🔲 OPEN (no MIS watchdog) |
| HIGH-005 | LOW | `datetime.utcnow()` deprecated in `_OrderPlacementLimiter` | 🔲 OPEN (zerodha_broker.py:102/129) |
| PHASE8 | P0 | ~~All 10 Phase 8 tasks unstarted~~ — **stale**. Reconciled: 001/003/007 ✅ DONE; 004/005/006/008/010 ◐ PARTIAL; 002/009 🔲 OPEN | See Phase 8 task table |
| US-CONFIG | LOW | `us_momentum_v1` has no DynamoDB config row → uses default cap=10; non-blocking while US markets closed during IST | 🔲 OPEN |
| ALPACA-FIX | LOW | Alpaca connector `'str' object has no attribute 'value'` — enum handling in alpaca_broker.py (.value calls at 204/287/289/543/609/639) | 🔲 OPEN |
| ~~DIAG-LOGS~~ | ~~LOW~~ | ~~Remove `candle_stream.diag_fetch` WARNING and restore `candles_fetched` to DEBUG~~ | ✅ DONE — `candles_fetched` at logger.debug (candle_stream.py:517); `diag_fetch` removed |
| NAV-INFLATE | LOW | portfolio_value showing ₹49L from ₹50L seed — NAV denominator inflates between fills, loosening 5% position checks. Non-blocking for paper. | 🔲 UNCONFIRMED (not re-verified 2026-05-30) |

---

## Phase 8 + HIGH reconciliation (2026-05-30)

Read-only audit of `memory/open_tasks.md` against actual code. Trigger: the tracker claimed
"all 10 Phase 8 tasks unstarted" while roughly half were already built. No code or trading state
was changed by this audit — documentation correction only. Evidence is file:line at time of audit.

### Phase 8 — verified status

| Task | Verdict | Evidence (file:line) |
|------|---------|----------------------|
| PHASE8-001 | ✅ DONE | `OrderStatus.ACK_UNKNOWN` order.py:37; `_recover_ack_unknown_order` execution_engine/service.py:1103; tag-scan `find_order_by_client_order_id` :1007/:1127; "refusing blind second broker placement" :1142; order_manager non-terminal/non-retryable rules :44/:51/:511/:518 |
| PHASE8-002 | 🔲 OPEN | `services/shared/zerodha/endpoint_budgets.py` absent |
| PHASE8-003 | ✅ DONE | `data_quality` on CandleData (candle_stream.py:127/138/151, DynamoDB write :664) + Bar (base_strategy.py:52); consumer parse (dynamo_candle_consumer.py:129/135/148/402); warm-up suppression strategy_runner.py:216-219 |
| PHASE8-004 | ◐ PARTIAL | Named `shared/reconciliation/gate.py` absent / not wired into 4 `start()` methods; overlapping runtime enforcement exists via reconciliation_validator (see PHASE8-007) |
| PHASE8-005 | ◐ MOSTLY DONE | `LocalOutbox` present (local_outbox.py:36), wired into risk_engine/publishers/kafka_approved_publisher.py:49/101/112 + strategy_engine/publishers/kafka_signal_publisher.py:71/122/368/370; `_halt_new_order_intake()` hook NOT found |
| PHASE8-006 | ◐ PARTIAL | `KafkaLagWatchdog` present (kafka_lag_watchdog.py:56) using `KafkaLagKillSwitchMonitor` → lag→kill-switch DONE; `signal_inbox.py`/`signal_outbox.py`/`outbox_publisher.py` absent |
| PHASE8-007 | ✅ DONE | reconciliation_validator.py rejects on flag (`approved=False` :136-137); read-failure fail-open intentional+unchanged (:200 comment); `scripts/ops/reconcile.py` present |
| PHASE8-008 | ◐ PARTIAL | `orphan_detector.py` present & wired; `base_broker.py` is abstract interface only — 3-tier SL ladder (CO/BO → SL-M retry → flatten) not at the named location |
| PHASE8-009 | 🔲 OPEN | `infra/terraform/modules/dynamodb/signal_processing.tf` absent |
| PHASE8-010 | ◐ PARTIAL | `tests/unit/test_phase8_hardening.py` present (888 lines); `tests/integration/test_kill_switch_fanout.py` absent |

### HIGH items — verified status

| ID | Verdict | Evidence |
|----|---------|----------|
| HIGH-001 | 🔲 OPEN | `_signal_locks: dict` init execution_engine/service.py:155; `setdefault(signal_id, asyncio.Lock())` :778; no cleanup anywhere → leak confirmed |
| HIGH-002 | 🔲 OPEN | strategy_engine/service.py:705 `_is_kill_switch_active()` does direct `get_item` with `Key=kill_switch_resource_key()` :721 — not `KillSwitchCache` |
| HIGH-003 | ✅ FIXED | sector_validator.py:79 `if sector == "UNKNOWN":` → :84/:95 `risk_data_unavailable_result(...)` (fail-closed); same in liquidity:98, spread_gate:96 (and position/margin/exposure/slippage/loss). Doc "silently disables" wording is stale. |
| HIGH-004 | 🔲 OPEN | MIS import only at execution_engine/service.py:322; no MIS watchdog coroutine |
| HIGH-005 | 🔲 OPEN (LOW) | `_OrderPlacementLimiter` uses `datetime.utcnow().date()` zerodha_broker.py:102 & :129 |

### Other carried items

- **DIAG-LOGS** ✅ DONE — `candle_stream.candles_fetched` at `logger.debug` (candle_stream.py:517); `diag_fetch` removed.
- **US-CONFIG** 🔲 OPEN — `us_momentum_v1` not seeded in `scripts/setup_local_tables.py`.
- **ALPACA-FIX** 🔲 OPEN — multiple `.value` calls in alpaca_broker.py (204/287/289/543/609/639); enum-handling investigation pending.
- **NAV-INFLATE** 🔲 UNCONFIRMED — not re-verified in this audit.

**Net:** the tracker materially lagged the code. Phase 8 is ~half built; the only HIGH item that
was already fixed-but-still-listed-open is HIGH-003. None of this changes the live-readiness verdict.

---

## Live-Readiness Audit — Fixed Items (2026-05-30, ADR-022)

> Code + infrastructure fixes applied in this session. Terraform changes require `terraform apply`
> + ASG instance refresh before they take effect on running EC2 instances.

| ID | Description | Fix Applied | Requires |
|----|-------------|-------------|---------|
| ~~C-001~~ | `symbol-status-index` GSI missing from orders table — every live position check failed | ✅ GSI added to `dynamodb/main.tf` | `terraform apply` + GSI backfill (5-20 min online) |
| ~~C-002~~ | `RISK_MAX_SIGNAL_AGE_SECONDS`, `RISK_PROFILE`, `UNIVERSE_MODE` missing from EC2 env files | ✅ Added to `risk_engine.sh`, `execution_engine.sh`, `strategy_engine.sh` | ASG instance refresh |
| ~~C-003~~ | Per-symbol fill race in `DailyLossValidator._apply_fill_to_daily_symbol_pnl()` | ✅ `asyncio.Lock()` per symbol in `record_fill()` | Deploy |
| ~~INFRA-3~~ | `deploy.yml` referenced `check_asg_health.py` but only `check_ecs_health.py` existed | ✅ `check_asg_health.py` created with ASG API | None — file now exists |
| ~~C-005/6/9~~ | MarginValidator, SlippageValidator, SectorConcentrationValidator docstrings wrong about live fail-open | ✅ Docstrings corrected | Deploy |
| ~~HIGH-001~~ | `_signal_locks` dict in execution_engine never cleaned up — memory leak on long sessions | ✅ `self._signal_locks.pop(signal_id, None)` before every return in `async with signal_lock:` block | Deploy |
| ~~B-001~~ | Candle signal Kafka publish failure silently dropped — no retry, no metric, no alert | ✅ Return value checked; CRITICAL log + `CandleSignalPublishFailed` CloudWatch metric on failure | Deploy |
| ~~B-002~~ | `asyncio.gather(return_exceptions=True)` in strategy_engine swallowed crashed loops | ✅ Results inspected; crashed loop re-raised with CRITICAL log | Deploy |
| ~~B-003~~ | `get_dynamodb_resource()` recreated on every 1s kill switch cache miss in strategy_engine | ✅ `_ks_dynamo_table` cached in `start()`, reused in `_is_kill_switch_active()` | Deploy |
| ~~ADR-021-P1~~ | `RISK_DATA_FEED_STALE_SECONDS=3600` workaround — unified staleness monitor masked consumer rebalancing | ✅ Split into `_monitor_consumer_lag()` (300s) + `_monitor_producer_heartbeat()` (60s, DynamoDB); data_ingestion writes heartbeat every 10s | Deploy + remove 3600 workaround from `.env` |

---

## ✅ Terraform Blockers — Fixed (2026-05-30)

| ID | Severity | Description | Status |
|----|----------|-------------|--------|
| ~~**INFRA-1**~~ | ~~CRITICAL~~ | `prod/main.tf:58`: `single_nat_gateway = false` → `ha_nat = true` | ✅ Fixed — `ha_nat = true` at `prod/main.tf:58` |
| ~~**INFRA-2**~~ | ~~CRITICAL~~ | `sessions` DynamoDB table absent | ✅ Fixed — `aws_dynamodb_table.sessions` added to `dynamodb/main.tf`; outputs (`sessions_table_name`, `sessions_table_arn`) added to `outputs.tf`; added to `all_table_arns` |
| ~~**INFRA-3**~~ | ~~HIGH~~ | `deploy.yml` referenced `check_asg_health.py` which didn't exist | ✅ Fixed — `scripts/deploy/check_asg_health.py` created with ASG API |

**`terraform plan` / `terraform apply` is now unblocked.** Expected plan changes:
- `aws_dynamodb_table.orders`: update — add `symbol-status-index` GSI (online, no downtime, 5-20 min backfill)
- `aws_dynamodb_table.sessions`: create — new table
- `module.vpc.aws_nat_gateway.*`: may recreate — verify VPC connectivity during apply

---

## ✅ Pre-Live Code Fixes — Completed (2026-05-30)

| ID | Severity | Description | Status |
|----|----------|-------------|--------|
| ~~**HIGH-004**~~ | MEDIUM | MIS square-off task crash during 15:05-15:15 IST window | ✅ Fixed — `try/except` (re-raises `CancelledError`, catches all others) wraps `await self._execute_mis_square_off()` in `run()`. On unhandled exception: logs CRITICAL + fires SNS alert. Does NOT re-raise — service stays alive; Zerodha 15:15 auto-square is the backstop. [`mis_square_off.py`](services/execution_engine/mis_square_off.py) |
| ~~**ACTION**~~ | — | Remove `RISK_DATA_FEED_STALE_SECONDS=3600` from `.env` | ✅ Done — removed from `.env`; replaced with explanatory comment. ADR-021 Phase 1 split (consumer lag 300s + producer heartbeat 60s) makes the workaround obsolete. |
| **ADR-021-P2** | MEDIUM | `risk_limits_production.yaml` not wired — `for_profile()` hardcoded defaults used | 🔲 Acceptable for Stage-1 (per pre-live runbook §1.4). `tiny-live` hardcoded defaults ARE Stage-1 limits (₹5k max order, 1 concurrent position). Wire the YAML before Phase C scaling (₹25L+). |

---

## [RESUME 2026-06-02+] Host-side runtime verification — PARKED

> Standing constraints still in force: no live trading · no capital-limit changes · no deploy ·
> no broker orders · no DynamoDB mutation outside of Terraform apply. Stage-1 one-share validation
> and **₹1,000,000 capital remain BLOCKED** until every runtime check PASSes on the real host.

**Pre-conditions before running runtime check:**
1. INFRA-1 and INFRA-2 Terraform edits applied + `terraform apply` completed
2. ASG instance refresh completed for risk_engine, execution_engine, strategy_engine
3. Morning Zerodha token refresh completed (`python scripts/zerodha_login.py`)

**Runtime check command (from project root on trading host):**
```bash
PYTHONPATH=services python3 scripts/read_only_live_readiness_runtime_check.py --json
```

**Operator must confirm (record results in `docs/live-readiness/runtime-state-verification-report.md`):**
- [ ] Zerodha token present **and fresh** (`expires_at` in future; refreshed same day via `scripts/zerodha_login.py`)
- [ ] Kill switch **INACTIVE** (`PK=KILLSWITCH, SK=GLOBAL, active=false`)
- [ ] `reconciliation_required` **False** (or a clean reconcile has been run)
- [ ] Every **enabled** strategy has `paper_trade=true` (no accidental live strategy)
- [ ] `QE_EXECUTION_LIVE_TRADING_ENABLED` absent/false; `RISK_PROFILE=paper`; `UNIVERSE_MODE=PAPER_SAFE_START`
- [ ] **Paper vs live table names confirmed distinct** (script prints resolved names)
- [ ] `sessions` table reachable and contains today's Zerodha token

**Full pre-live gate checklist:** `docs/live-readiness/pre-live-runbook.md §4`

Until every box is checked from a real host run: **NO GO.**


---

## Cross-Strategy Netting Protection — COMMITTED 2026-06-13 (was in working tree since 2026-06-05)

**ADR-028** · Triggered by: Session 10 (2026-06-05) UNIONBANK paper trade anomaly
**Note**: Code was written 2026-06-05 but never committed until checkpoint commit `011ef9a`
on 2026-06-13. The previous "RESOLVED 2026-06-05" date referred to when it was coded,
not committed. Sessions 10–17 ran with this fix on disk but not in git HEAD.

| Blocker | Status |
|---|---|
| Bug: competing entry signals net against open positions | ✅ COMMITTED — `check_direction_conflict` in `order_manager.py` (commit `011ef9a`) |
| Bug: `attach_exit_policy` overwrites active policy from different signal | ✅ COMMITTED — `entry_signal_id` guard in `order_manager.py` (commit `011ef9a`) |
| Live: fail-open on position-store outage | ✅ COMMITTED — `fail_open=False` in live path |
| Reconciliation: no detection of entry-unwound-by-competing-entry | ✅ COMMITTED — `check_competing_entry_unwind` + `check_audit_chain` |
| Monitoring: no visibility into netting protection state | ✅ COMMITTED — Section 16 in `paper_trading_monitor.py` |
| Tests | ✅ 20 tests in `tests/unit/test_cross_strategy_netting.py` |

### Live Promotion Path for This Class: CONDITIONALLY UNBLOCKED

| Step | Status | Condition |
|---|---|---|
| Fix deployed + tested | ✅ | — |
| Section 16 gate: PASS | 🔲 | Requires 1 clean paper session with guard active |
| Shadow-live session | 🔲 | Broker calls disabled; must not skip |
| Limited live (1-symbol, hard notional cap) | 🔲 | Only after clean paper + clean shadow-live |

**Do not skip the shadow-live step.** The UNIONBANK failure class (silent position mutation) is exactly the kind of bug that appears between paper and live.

---

## 5-Session Live Gate Progress (Session 16+ valid sessions only — counter RESTARTED by ADR-030)

> **2026-06-10 (ADR-030):** Sessions 12–15 are NOT valid quality-gate sessions — the
> confidence/RR filters were silently disabled by a strategy-name key mismatch
> (YAML keys unprefixed vs `nse_`-prefixed signal names). The 5-session counter
> restarts at Session 16, the first session with the Week-1 entry-economics
> rebuild active (universal viability gate, ORB v2, VWAP v2, 15-entry/day budget).
> Session 13's prior 1/5 credit is void (its MIS/HIGH-004 validation still stands).

| # | Session | Date | S4 MIS | Result | Notes |
|---|---|---|---|---|---|
| —  | Session 16 | 2026-06-11 | N/A | ❌ NOT COUNTED | 0 trades — VWAP geometrically capped (median R:R 1.05, max 1.48, ALL 184 signals rejected); ORB blind (late 10:19 IST start, opening range never formed). Not a strategy failure — structural test constraints. |
| —  | Session 17 | 2026-06-12 | PASS | ❌ NOT COUNTED | 5 fills, PF 0.47, net −₹238.44. Trailing exits validated (2/2 ratchet held: ADANIPORTS +₹107.92, M&M +₹107.05). Negative expectancy + Bug 5 (kill-switch 11 false fires) + Bug 6 (MIS NAV corruption ₹932k). Bugs do not count toward gate. |
| 1/5 | Session 18 | 2026-06-15 | PASS | ⏳ PENDING REVIEW | 6 fills, 3 entries + 3 exits (2 SL: DLF/PFC, 1 trailing: CGPOWER). 0 kill-switch fires (Bug 5 fix held). MIS closed all 3. Realized −₹473.27. Go-live verdict: READY. Full verdict pending strategy performance gate eval. |
| 2/5 | Session 19 | TBD | — | — | |
| 3/5 | Session 20 | TBD | — | — | |
| 4/5 | Session 21 | TBD | — | — | |
| 5/5 | Session 22 | TBD | — | — | |

**Live gate BLOCKED** until all 5 sessions show S4 PASS + strategy performance gates (PF ≥ 1.2, expectancy > 0).

**Pre-Session 18 blockers:**
- [ ] **Bug 5 fix**: `consumer_lag_monitor` (auto_triggers.py monitor 4) fires on signal silence (normal under ADR-030 gating) — gate on candle/feed staleness or disable in paper. `producer_heartbeat_monitor` 60s threshold too tight for LocalStack (64s blip fired). data_ingestion heartbeat-writer task needs FIX-8-style watchdog. Alpaca connector crash-loop on placeholder creds — disable when creds are placeholders.
- [ ] **Bug 6 fix**: MIS paper-close path (`_place_mis_close_order` → `apply_fill_to_position`) corrupts NAV#CURRENT — writes seed − close_notional instead of ledger-correct update. Fix: use the same NAV ledger path as TEE exits.

### Required before Session 16

- [x] ~~S3 bucket creation in setup~~ — verified 2026-06-10: `_create_s3_buckets()` in `setup_local_tables.py` creates `{prefix}-data` and `{prefix}-logs` (fix landed after Session 13).
- [x] ~~Rebuild ALL service images~~ — done post-close 2026-06-10: strategy_engine, risk_engine, execution_engine rebuilt and **verified to contain ADR-030 code** (_viability.py, orb_v2, _threshold_for, GLOBAL_DAILY_ENTRY_LIMIT, YAML max_trades_per_day=15).

### Week-2 items (committee roadmap)

- [x] ~~intraday_trend_15m 15m warm-up replay from candle history~~ — done 2026-06-13 (commit 3139873): `initialize()`/`get_state()` override in `IntradayTrend15mStrategy`; OHLCV deques persist across sessions via `{prefix}-strategy-state` DynamoDB table; daily counters reset on new IST day; 21 tests.
- [x] ~~Persist `strategy_name` to positions table at fill time~~ — done 2026-06-13 (commit 2623d9a): `order_manager.py` sets `strategy_name` on fill record; execution_engine service propagates.
- [x] ~~NIFTY index regime gate (longs above day-VWAP / shorts below) for trend_15m + ORB~~ — done 2026-06-13 (commit 3139873): `NiftyRegimeGate` in `nifty_regime_gate.py`; wired into both ORB and trend_15m; fails-open when no NIFTY data; auto-resets at IST day boundary; 26 tests. NIFTY 50 candle subscription activated commit f1dfc8c (data_ingestion now includes `"NIFTY 50"` in instrument_tokens at startup; gate no longer fails-open in real sessions).
- [x] ~~Gross-vs-net cost attribution line in `paper_session_report.py`~~ — done 2026-06-13 (commit 2623d9a): gross P&L line added alongside net P&L in session report output.

### HIGH-004 — ✅ FULLY CLOSED (fix validated Session 13, 2026-06-09)

Fix: 60-second polling loop in `MISSquareOffManager.run()` + CRITICAL log on task cancellation.
Evidence: `mis_square_off.all_positions_closed` at 15:05:13 IST — 3 positions closed before deadline.

---

## Alpha Engine (ADR-031) — ✅ PAPER-SESSION-INTEGRATED (2026-06-13)

The alpha_engine shadow-forecast layer is now fully wired for paper sessions.

**Status:** committed, 82/82 tests passing, started by `docker-compose up -d` (paper session stack).

**What it does:** Polls candle-cache DynamoDB → drives shadow copies of ORB v2 / VWAP v2 / trend_15m
→ fans out AlphaForecast per horizon (15/30/60m) → costs via CostModel → ranks cross-sectionally
→ persists ALL forecasts to `{prefix}-alpha-forecasts` → publishes eligible (≥50bps net edge)
to `alpha.opportunities` Kafka topic. Labels matured forecasts every 5min. EOD rollup at 15:35 IST.

**Paper session integration completed 2026-06-13:**
- `services/monitoring_agent/rules.yaml`: alpha_engine service health (non-critical), `alpha.opportunities` topic existence, alpha DynamoDB tables, Docker container watch, log scan
- `scripts/monitoring/paper_session_report.py`: Alpha section showing forecast counts, label status, accuracy (gracefully shows "not running" when alpha not present)

**ADVISORY ONLY — governance constraints (ADR-031 #4):**
- Never publishes to `signals.*` / `orders.*`
- Never places broker orders or reads risk-state for trading decisions
- `AlphaShadowPublisher` allowlist = `frozenset({"alpha.opportunities"})` — hard-coded
- Kill switch pauses forecast output (no store/publish) but does not stop alpha_engine
- A human approves all production changes; alpha_engine may recommend, never promote

**Remaining PLANNED work (not blocking paper sessions):**
- [ ] alpha.opportunities consumer → self-improvement assistant integration
- [x] ~~Alpha accuracy stats in per-model breakdown~~ — done 2026-06-13 (commit 78e947b): `ModelAccuracy` dataclass, `by_model` field in `AlphaMetrics`, `_fetch_alpha_metrics` groups by `model_id` from DynamoDB; per-model table rendered in alpha section; 19 tests.
- [ ] alpha_engine champion-challenger promotion workflow (ADR-031 #9)
- [ ] AWS Phase 9: model dataset generation from labeled alpha-forecasts

---

## ✅ Backtesting Data Lake — Phase 1 Local Download — COMPLETE (2026-06-15)

**Status:** COMPLETE. Full lake 2016-01-01 → 2026-06-12 verified clean.

**Lake stats (verified 2026-06-15):**
- 22,000 Parquet files · 4,240,893 rows · 3,450 symbols
- Formats: modern `sec_bhavdata_full` (2019–2026) + legacy `cm` ZIP (2016–2018)
- Pre-2019 (2016–2018): OHLCV + volume only — **no delivery_pct** (free NSE archives don't include it)
- 2019–2026: full delivery_qty + delivery_pct
- Schema: all years consistent (`large_string` for string columns), no merge conflicts
- `backtest-data/` in `.gitignore` — not committed (~7GB)

**Refresh command (run monthly or after trading day):**
```bash
nohup env PYTHONUNBUFFERED=1 python scripts/backtest/download_bhavcopy.py \
  --start YYYY-MM-01 --end YYYY-MM-DD --base backtest-data > backtest-data/download.log 2>&1 &
```

**Remaining:**
- [ ] Phase 2 (AWS S3 lake) — requires explicit operator approval per CLAUDE.md AWS backtesting protocol

---

## Backtesting Lab Advisory — COMPLETE (2026-06-14, ADR-032)

Walk-forward validation phases 12–15C are complete. Operator selected Phase 16 = A (continue paper sessions).

**Key findings (advisory, non-authoritative):**
- `momentum sw=10/lw=50` on NIFTY50 daily data: **PAPER_OPTIMIZATION** (OOS expectancy +₹90/trade, PF 1.52, win consistency 60%)
- Strategy is regime-sensitive: wins in bull/trending, loses in bear/choppy
- Parameter is stable: 5/5 folds in 24m IS window select `sw=10/lw=50`
- Intraday strategies (`vwap_reversion`, `trend_15m`, `orb`, `preclose`) not yet validated — require 1m/5m/15m data

**Files produced:**
- `scripts/backtest/run_momentum_walk_forward.py` — walk-forward harness with --preset, --lw-max flags
- `docs/backtesting/aws-phase15b-walk-forward-report.md` — Phase 15B results (15C-F data due to overwrite)
- `docs/backtesting/aws-phase15c-walk-forward-corrected-report.md` — Phase 15C combined report (authoritative)

**Next backtesting work (requires operator approval to start):**
- [~] B: Intraday data — **tooling built 2026-06-14** via Zerodha Kite (not GDF — no GDF data/creds + ~3mo depth). See below.
- [ ] C: Portfolio-level walk-forward with `partition_by='symbol'` for reliable equity curve / Sharpe
- [ ] D: GenAI analysis (Phase 10) via Bedrock over walk-forward artifacts

### Phase B — Zerodha intraday fetch + backtest (tooling built 2026-06-14, ADR-033)

Evaluates the 4 intraday strategies the daily lake couldn't (orb/vwap_reversion/trend_15m/preclose;
scalp_1m excluded — Stage-1 disabled). Source = Zerodha Kite `historical_data` (HIGH trust per
contract §1, but LIMITED ~3yr depth → advisory edge exploration, not authoritative backbone).

**Built + self-tested (12 tests, full backtest suite 188 passing):**
- `scripts/backtest/fetch_zerodha_intraday.py` — Kite → Parquet lake (`interval=1m|5m|15m`), chunked by per-interval cap, idempotent, manifest (source=zerodha_kite, trust=HIGH). `--self-test` runs offline.
- `scripts/backtest/run_intraday_backtest.py` — **per-day** backtest (fresh strategy/day = daily reset + MIS EOD flatten; preserves global daily signal budget). Reuses `data_loader.load_candles` + adapters + `Backtester` + `compute_metrics`/`evaluate_gates`. `--self-test` builds synthetic lake + runs pipeline.
- `services/backtesting/s3_data_catalog.py` — added `zerodha`/`zerodha_kite`/`kite` to HIGH-trust sources (contract §1 alignment; was omitted in code).
- Procedure doc: `docs/backtesting/zerodha-intraday-fetch-and-backtest.md`.

**DONE 2026-06-14** — fetched + backtested. Operator logged in (token exchanged in-process, never persisted/printed; Docker was down so DynamoDB path bypassed). Fetched 16.1M bars, 46/47 symbols × 1m/5m/15m × 2022-2024 (610 MB lake).

**Phase B verdict (advisory): ALL intraday strategies REJECT or inconclusive.**
- orb -₹94/trade PF 0.45 REJECT · vwap_reversion -₹174 PF 0.25 REJECT · preclose -₹84 PF 0.05 REJECT
- trend_15m 0 trades = NO_TRADES (warm-up limited under per-day reset — inconclusive, NOT judged)
- Report: `docs/backtesting/aws-phaseB-intraday-backtest-report.md`. All three losers fail to clear the NSE cost stack — consistent with the daily-momentum-only thesis.

**Bug found+fixed during the run:** shared `Backtester.run()` multi-symbol EOD flatten marked positions out at the *wrong symbol's* close (`backtester.py:401`) → first run gave impossible >4000% win rates / ₹crore P&L. Fixed + regression-pinned (`test_eod_multi_symbol_uses_own_symbol_price`), 192 backtest tests green. See ADR-033 (decisions.md).

**trend_15m — RESOLVED 2026-06-14 (warm-start mode added).** Runner now has opt-in
`WARM_START_STRATEGIES={"trend_15m"}`: one persistent instance, buffers accumulate across days,
`reset_daily()` between days. trend_15m's EMAs warm fully but it STILL fires 0 trades at default
(ADX≥25 + confidence≥0.65 mutually exclusive on NIFTY50 15m); filters-off it loses (PF 0.30,
−₹368k). REJECT either way. NIFTY gate disabled for backtest (no index data; fails-open in prod).
216 backtest tests green. **All 4 evaluable intraday strategies now have a verdict: no edge.**

**Follow-ups (open, not blocking):**
- [ ] TATAMOTORS tradingsymbol mismatch (46/47 resolved) — backfill on next login.
- [ ] (optional) Intraday walk-forward / deeper licensed data only if a strategy ever shows edge — none did, so low priority.

Backtesting did fix the Session-16 "ORB blind" problem (full 09:15 opening range present in history) — ORB now trades (2403 trades) but still loses to costs.

---

## Paper Session Gate Tracker (v1 intraday — FROZEN, ADR-037; historical)

> **2026-07-08:** v1 intraday sessions are frozen (strategies retired ADR-033/034; stack is
> fallback-only per ADR-037/038). "Start paper trading" now means the qe cadence — see the
> V2 track below and `docs/runbooks/qe-operator-runbook.md`. This tracker is retained as history.

**Status: Session 18 COMPLETE (2026-06-15). 1/5 counting sessions done. Session 19 is next.**

| Bug | Description | Status |
|---|---|---|
| Bug 5 | Kill-switch false fires (consumer_lag/heartbeat thresholds), Alpaca placeholder crash-loop | ✅ FIXED — commit `bc54c6b` (2026-06-13) |
| Bug 6 | MIS NAV corruption ₹932k — computed `seed − close_notional` instead of realized-P&L delta | ✅ FIXED — commit `eb9a14a` (2026-06-13) |

**5-session gate progress:** 0/5 counting sessions complete (Sessions 16–17 were not counted — see decisions.md § 5-Session Live Gate Progress).

**Next valid session = Session 19.** Run `make start-paper-session` or trigger the paper trading start protocol. Remember: rebuild risk_engine image before each session (`docker-compose build risk_engine`).

---

## Strategy Thesis Pivot — Research Gate Tracker (ADR-034 → ADR-036)

**Status: ACCRUE FORWARD, DEPLOY NOTHING (operator decision 2026-06-19).** Delivery edge DECAYED
(Sharpe 1.60 2020-22 → 0.61 2023-26; weak forward −9.85%); momentum mostly beta; delivery+momentum
COMBINED book gives NO diversification (legs +0.67 correlated — the forward "decoupling" was a
1-regime artifact); **none beats the EW benchmark risk-adjusted 2020-26.** Both single-factor paper
books now run forward as the OOS truth-test vs the **pre-registered Forward Factor Gate**
(`docs/live-readiness/forward-factor-validation-gate.md`, `scripts/paper/check_forward_gate.py`);
5/12 mo, eligible ~Dec-2026. Deploy NO capital until a book clears the gate forward → human review.
Do NOT relax the gate. Live BLOCKED.
**All research work is advisory-only. No capital, no live/paper signal pipeline.**

### Factor Studies Completed (2026-06-15)

| Study | Result | Files |
|---|---|---|
| Cross-sectional factor backtest (2020–2025) | ✅ delivery-% ONLY clears risk-adj (Sharpe 1.40 vs market 0.96/1.09 both sub-periods) | `run_factor_study.py`, `factor-study-report.md` |
| Walk-forward 5-fold (delivery-%, 2021–2025 OOS) | ✅ 5/5 years positive; 200d overlay REJECTED (hurts) | `run_delivery_walkforward.py`, `delivery-walkforward-report.md` |
| Factor correlation study | ✅ equity factors HIGHLY correlated (3 pairs >0.70 in sample), blending dilutes; delivery standalone wins | `run_factor_correlations.py`, `factor-correlation-report.md` |
| H4 delivery-spike event study | ❌ REJECTED — abnormal returns negative (t −2.1→−3.5), delivery info lives in persistent level | `run_delivery_spike.py`, `delivery-spike-report.md` |
| H5b price-implied PEAD (large-move proxy) | ❌ REJECTED — same anti-predictive pattern as H4 (T+63 abn −1.92%, t −3.98) | `run_pead_study.py`, `pead-study-report.md` |

### Paper Book Forward Track

| Item | Status |
|---|---|
| Isolated advisory harness stood up | ✅ `scripts/paper/run_delivery_paper_book.py` (ADR-035) |
| Inaugural basket (2025-12-31) | ✅ 20 holdings, top-200 universe, full delivery costs |
| Corporate action filter (`_drop_corp_action_dislocations`) | ✅ catches demerger/split ex-dates (KOTAKBANK case) |
| Clean monthly forward replay | ✅ `scripts/paper/replay_delivery_book_forward.py` (2026-06-19) — fixed contaminated state (Dec→Jun single jump + same-day re-rebalances); deterministic monthly walk, computes benchmark/alpha; re-run after each Bhavcopy refresh to advance |
| First OOS read (Dec-2025 → Jun-2026, 5 mo) | ⚠️ book −9.85% vs bench −0.16% = −9.7pts behind; NAV ₹9,01,531; monthly alpha Jan −3.6/Feb +3.3/Mar +1.0/Apr **−8.4**/May −1.9. `delivery-paper-book-forward-report.md` |
| June-2026 monthly advance | ✅ DONE 2026-07-08 — lake refreshed to 2026-07-07; both books through the 2026-06-30 rebalance on BOTH paths (qe study == v1 replay to the rupee, ADR-040): delivery cum −5.85% vs bench +2.20% (NAV ₹941,470), momentum +4.28% (NAV ₹1,042,811); forward gate 6/12 months, both IN PROGRESS (delivery failing alpha; momentum +5.49% alpha but pos-alpha 50% < 58% after negative June) |
| Next monthly rebalance | ⏳ End of July 2026 — refresh lake in early August, then run the qe cadence (runbook § monthly) |
| Meaningful OOS read | ⏳ 12–24 forward months; a handful of months is plumbing/monitoring, not validation |
| Parallel momentum paper book | ✅ DONE 2026-06-19 — harness now `--factor {delivery,momentum}`; `replay_delivery_book_forward.py --factor momentum`. Forward **+5.43%** vs bench −0.16% (+5.6pts) vs delivery **−9.85%** SAME window → factors **DECOUPLE** (Apr recovery: momentum +20.4 caught / delivery +6.6 lagged). Regime-complementary; n=5 (not validation). `momentum-paper-book-forward-report.md` |
| delivery+momentum COMBINED book | ❌ TESTED 2026-06-19 → diversification FAILS. Legs +0.67 correlated (forward decoupling was a 1-regime artifact); combo Sharpe 0.86 ~ legs, MaxDD −28.9% worse than delivery −26.4% & ~bench. Both edges decayed (delivery 1.60→0.61). `run_combined_book_study.py`, `combined-book-study-report.md`. Real diversification needs a NON-long-only-equity driver |

### Regime-Expansion Gate (Tier-1 Priority)

| Item | Status |
|---|---|
| Pre-2019 bhavcopy URL format (legacy `cm` ZIP) | ✅ `download_bhavcopy.py` updated 2026-06-15 |
| Pre-2019 OHLCV (2016–2018) | ✅ DONE 2026-06-15 — 739 trading days, 1,104,114 EQ rows, ~1,700 symbols/year |
| Pre-2019 delivery % | ❌ NOT available in free NSE archives — cm format has no DELIV_QTY/DELIV_PER |
| IL&FS crisis + 2016 demonetization test for delivery % | ❌ BLOCKED — requires paid data (Refinitiv/Bloomberg/NSE subscription) |
| Factor study on extended OHLCV (momentum/vol pre-2019) | ✅ DONE 2026-06-15 — momentum Sharpe 0.63 > market 0.59 through IL&FS + demonetization; combo 0.72; delivery 0.38 (artifact of NULL 2016–18) |

### Open Different-Driver Hypotheses

| Hypothesis | Status |
|---|---|
| H5a — True PEAD with real earnings calendar | ❌ BLOCKED — NSE API 404, BSE API empty body in automated sessions; requires browser session or paid data |
| H6 — F&O OI divergence | Not started |
| H7 — Index inclusion/exclusion event | Not started |

**Regime-expansion gate: COMPLETE (2026-06-15).** Operator decision: accept 2019–2025 boundary for delivery% (COVID + 2022 bear OOS already validated in walk-forward). IL&FS test deferred — requires paid data.

**Next research gate: H6 F&O OI or H7 Index-inclusion** (awaiting operator direction). Or just run Session 18.

---

## 🔬 Options/Vol track — O-1 PASSED (2026-06-19); next = optional Kite re-confirm → O-2 vendor decision

**Priority**: P2 (research; no spend, no live impact) · **Opened + O-1 run**: 2026-06-19
**O-1 RESULT: PASS** on FREE Yahoo data (^NSEI/^INDIAVIX via yfinance, 2020–25, 1,449 days, 69 cycles):
mean VRP **+2.34 vp** · VIX>realised **79%** days · +VRP **100%** of years · all 4 gates ✅.
⚠️ **FAT LEFT TAIL** (worst cycle −₹110k = 11.7× median gross; cum-DD −₹120k — the short-vol steamroller,
concentrated in the Mar-2020 spike). Report: `docs/backtesting/vol-premium-study-report.md`; full log:
`docs/backtesting/options-vol-track-spec.md`.

**Kite login gotcha (resolved):** `zerodha_login.py` persists the token to DynamoDB → it fails after a
successful Kite exchange when LocalStack (localhost:4566) is down, burning the single-use request_token.
Use the new **`scripts/backtest/kite_fetch_with_token.py --request-token XXXX`** instead — exchanges +
fetches into the lake + runs the screen in-process, NO DynamoDB. (Login URL uses api_key in `.env`.)

**Kite HIGH-trust re-confirm: DONE 2026-06-19** — lake populated (`segment=INDICES`, NIFTY50+INDIAVIX,
1,492 daily bars each); screen → mean VRP +2.48 vp, VIX>realised 80% days, all gates PASS (matches Yahoo).
PASS now stands on broker-grade data.

**O-2 HARNESS BUILT 2026-06-19 (zero spend)** — `scripts/backtest/run_options_vol_backtest.py`:
OptionsCostModel (₹20/leg flat + STT 0.1% sell + txn 0.035% + GST + stamp), BS pricer, iron condor,
≤2%-NAV/cycle risk cap, pre-registered O-2 gate. Self-test PASS; synthetic-on-real-NIFTY-path (incl
Mar-2020) PASS by construction (engine + cost stack + crash cap validated; NOT a real edge).

**O-2 chain source = FREE NSE F&O Bhavcopy (EOD), NOT Zerodha.** Kite can't backfill expired option
chains (purged tokens). F&O bhavcopy has all NIFTY strikes/expiries 3+ yr, free, same archive as equity
bhavcopy. EOD suffices for held-to-expiry condor. **Pipeline BUILT + self-tested 2026-06-19 (zero spend):**
- `scripts/backtest/download_fo_bhavcopy.py` — both NSE formats (legacy + UDiFF), NIFTY options → chain
  lake `backtest-data/lake/options/underlying=NIFTY/`. Self-test PASS.
- `run_options_vol_backtest.py --chain backtest-data/lake/options` — `backtest_real`: real condor, held to
  expiry, NIFTY50-spot settle, 2.5% slippage haircut. Self-test PASS (slippage erodes edge as expected).

**O-2 RAN 2026-06-20 → FAIL.** Lake downloaded (760 td, 2022-06→2025-06, 1.24M NIFTY option rows).
31 monthly condor cycles (after fixing 2 harness bugs: long-dated-expiry leakage + sub-1-lot drop bias):
net −₹54k, PF 0.67, expectancy −₹1,740, pos-years 50% → FAIL. **Decomposition = EDGE problem not cost:**
loses gross −₹35k even at ZERO slippage; win 65% but credit/max-loss 0.25 needs ~81% win to break even;
losses cluster in directional 2022–23. ATM VRP (O-1) does NOT convert to a profitable OTM condor (put skew
+ directional risk). Report: `docs/backtesting/options-vol-backtest-report.md`.

**RESOLVED 2026-06-20 → SHELVE (pre-declared sweep).** `run_options_vol_sweep.py`: OTM{1-5%}×wing{1,2%}
= 10 monthly condor configs. **0/10 clear the O-2 gate; only 2/10 gross-positive (barely, <0.5%/yr, negative
after costs).** Structural FAIL — no retail-affordable static defined-risk structure harvests the NSE VRP
after skew+directional risk+costs. Only untested lever = intraday active management (needs PAID data — not
justified when every static structure loses gross). Report: `options-vol-sweep-report.md`.

**OPTIONS/VOL TRACK CLOSED.** Return to STANDING POSTURE: forward factor books accrue monthly vs the
pre-registered Forward Factor Gate, deploy nothing. Live BLOCKED. Tooling retained + reusable
(fetch_zerodha_indices / run_vol_premium_study / download_fo_bhavcopy / run_options_vol_backtest / _sweep).
Don't re-propose static index short-vol without a genuinely new angle + fresh data.

---

## 🧭 NEW PROGRAM: NSE Options & Futures hedge-level strategies (plan)

**Opened**: 2026-06-20 · **Plan**: `docs/strategy/nse-options-futures-strategy-plan.md` · No build yet.
Operator pivoted to designing options+futures strategies (keep intraday, work forward). Plan reconciles with
prior findings (cash-intraday dead, static short-vol dead) and tests DIFFERENT return sources where those
failures don't apply. **Data reality:** futures (intraday Kite + EOD bhavcopy) and EOD/event options are
free-testable now; intraday options chains are PAID/forward-only (data-blocked).
Ranked Tier-1 candidates (free, evidence-grounded, account-survivable):
- **F1 overnight index-futures premium** (grounded in C6 overnight finding; most evidence-backed)
- **O1 scheduled-event IV-crush** (defined-risk, event-timed; cheap EOD screen first like O-1 VRP)
- **F2 positional index-futures trend/momentum**
Tier-2 (thin): F3 futures basis/roll, O2 vol calendars. Tier-3 (deferred): O3 intraday options (paid data).
Hedging overlay: H1 protective-put/collar, H2 tail hedge (risk layer, not alpha).
Same discipline: pre-registered gates, full futures cost+margin+gap-tail, defined risk, forward book before
any pilot. Live BLOCKED. Operator picked **F1 first**.

**F1 SCREEN DONE 2026-06-20 → PASS (with material caveats).** `run_overnight_futures_study.py` (NIFTY50
spot proxy, 2020-25). Overnight +11.3 bps/night SURVIVES futures costs (~0.023% vs cash 0.22%), net Sharpe
1.98, **positive all 6 years** — most promising edge yet. BUT naked leveraged form account-inappropriate:
maxDD −41% (COVID); gap tail scales with index — same −9% COVID gap at today's ~26000 = −35% NAV in ONE
night (G3 passed only because worst 2020 gaps hit at low index; gate also lacked a max-DD limit).
**Operator chose F1-full (real futures) first. Pipeline BUILT + self-tested 2026-06-20 (zero spend):**
`download_fo_futures.py` (index-futures bhavcopy, NIFTY+BANKNIFTY, both NSE formats) + `run_overnight_futures_study.py
--futures` (`study_futures`: near-month front-contract overnight, basis in prices, **DD-aware 5-gate adds
G5 maxDD≤25%**). Both self-tests PASS.
**F1-FULL RAN 2026-06-20 → FAIL; F1 DEAD (naked AND hedged).** Real NIFTY futures (760-day lake, 733 nights):
overnight only **+3.2 bps/night, Sharpe 0.34, ann +6.1% → FAIL G4**; 2025 negative. Decomposition (same
window): spot +8.9 bps → futures +3.2 bps = **−5.7 bps lost to basis-decay + non-tradable index "open"**;
C6 overnight is a real index property but ~2/3 NOT harvestable on the tradable instrument. F1-hedged also
dead (binding failure is RETURN not tail → a put adds cost, makes it worse). **Lesson: spot/index proxy
overstated edge ~3.5×; always validate on the real tradable instrument.** Report:
`docs/backtesting/overnight-futures-study-report.md`.

**OPTIONS/FUTURES PROGRAM EXHAUSTED AT TIER-1 (2026-06-20): F1 DEAD, O1 SHELVE, F2 SHELVE.**
- F1 overnight futures DEAD (real-futures +3.2bps Sharpe 0.34; ~⅔ non-harvestable).
- O1 event IV-crush SHELVE (edge over random-day baseline 1.03×; where crush real the move is real).
- **F2 futures trend SHELVE** (`run_futures_trend_study.py`): drawdown-reduction NOT alpha — long-only trend
  ann ~12% = buy-hold (no return added), Sharpe 0.54-0.59 vs B&H 0.42, cuts −72% leveraged DD to ~−25% but
  hinges on dodging ONE crash (2020, n≈1 trend); isolated/threshold-marginal pass. Report:
  `docs/backtesting/futures-trend-study-report.md`.
Tier-2 (F3 basis, O2 calendars) low prior; Tier-3 (intraday options) data-blocked. **Return to STANDING
POSTURE: forward factor books accrue, deploy nothing. Live BLOCKED.** All tooling retained + reusable.
**CAPSTONE DONE 2026-06-20:** `docs/strategy/research-program-consolidation-2026-06-20.md` — full program
record (every hypothesis, why each died with numbers, the recurring mechanisms, the reusable toolkit, the
forward program, and the go-forward stance). The definitive "what's settled and why" reference — don't
re-chase eliminated edges. Only remaining work = monthly forward-book cadence (→ ~Dec-2026 gate eligibility).

---

## V2 RE-ARCHITECTURE TRACK (ADR-037, approved 2026-07-05)

**Design:** `architecture/re-architecture-2026-07.md`. v1 trading stack is FEATURE-FROZEN (bugfixes only).

| Milestone | Scope | Status |
|---|---|---|
| M0 | ADR-037 + freeze + hygiene sweep | ✅ ADR done 2026-07-05 (hygiene sweep pending) |
| M1 | `qe/` skeleton: frozen hashed config, JSONL journal, snapshot-pinned lake loader, null engine | ✅ DONE 2026-07-05 (18 tests; acceptance run vs real lake, pinned-snapshot rerun verified) |
| M2 | SimClock engine at parity (first user: forward factor books) | ✅ DONE 2026-07-05 — **EXACT parity, both books**: `qe study` reproduces `replay_delivery_book_forward.py` with max NAV diff ₹0.00 across all 6 rebalances + final MTM, benchmark legs diff 0.0, delivery AND momentum, real lake (snapshot `ds-80b8dad5c52c2870`). New: qe.costs (parity-tested vs v1 IndianCostModel), qe.data.panel, qe.universe, qe.strategy (FactorBookStrategy), qe.portfolio, qe.risk (4-check pipeline), qe.execution (SimBroker), qe.engine.sim, qe.research.study. 30 tests. **Monthly forward-book cadence can now run as `python -m qe study --config configs/qe_delivery_book.yaml` (+ `qe_momentum_book.yaml`)** — v1 scripts retained until M5. Walk-forward parity deliberately moved to M3 (belongs with study declarations). |
| M3 | Full research-factory port | ✅ DONE 2026-07-06 — **walk-forward as a declared study** (`python -m qe study --config configs/qe_delivery_walkforward.yaml`): engine leg (share-based, actual costs) + **v1 cross-check EXACT** (CAGR 23.7% / Sharpe 1.41 / MaxDD −22.2% / 5/5 years — identical to regenerated `run_delivery_walkforward.py` report at full precision, per-year rows included); engine model confirms verdict at realism (CAGR 20.8% / Sharpe 1.32 / 5/5). New: qe.research.{metrics (v1-exact), wf_v1 (verbatim returns-space port), gates (pre-registered, fail-closed), registry (governance/experiment-registry.jsonl, family test-budget), walkforward}; risk checks max_positions/max_turnover (default-off); 43 tests. **TEE/MIS-as-engine-policies DEFERRED to M4 decision point** — intraday is retired (ADR-033/034); building exit-simulation machinery for retired strategies violates RA-1's own spend-follows-funnel principle; revisit only if a strategy needing intraday exits reaches paper candidacy. |
| M4 | Real-time paper engine + kill-switch v2 | ✅ DONE 2026-07-06 — **paper==sim EXACT** (paper WallClock sessions driven at each real delivery-book rebalance reproduce sim NAV to ₹0.00, real lake + synthetic; proves RA-1 §2.3 three-clocks invariant AND resume/persistence). New: qe.clock (Sim/WallClock IST), qe.killswitch (ONE in-process state machine + persisted flag, idempotent activate = self-refire class retired), qe.engine.core (shared `execute_rebalance` step — sim refactored onto it, parity preserved), PaperBroker (subclass of SimBroker, type-isolated, no place_order surface) + LiveBroker unconstructible without M6 token, qe.engine.book_store (typed resume state, fail-closed on config-hash drift), qe.engine.paper, qe.reporting.session_report (monitoring = journal reader; retires LiveCounters). CLI: `qe paper`, `qe kill status/activate/deactivate`, `qe report`. 57 tests. Bug fixed en route: same-second journal filename collision (now µs+uuid). **Live dry-run proved fail-closed for real**: latest lake 2026-06-12 vs today → 24d-stale → staleness trigger fired → kill activated → due rebalance BLOCKED. **RA-1 M4 'shadow-diff vs v1 stack' REFRAMED**: the v1 paper stack is the Kafka INTRADAY pipeline trading retired strategies (ADR-033/034); the live experiment (monthly positional factor book) was never in it, so the meaningful invariance is paper==sim (done) — v1 `run_delivery_paper_book.py` parity was already proven in M2. **TEE/MIS DECISION: NOT built** (intraday retired; no factor-book intraday exits to simulate; revisit only if a strategy needing them reaches paper candidacy). |
| M5 | Cutover (done); decommission (GATED, not executed) | 🔶 CUTOVER DONE 2026-07-06 / TEARDOWN GATED — **qe is now the primary research+paper path**: monthly forward-book cadence + paper via `python -m qe study|paper|kill|report`; one-page operator runbook `docs/runbooks/qe-operator-runbook.md`; CLAUDE.md v2 section added; v1 factor scripts marked SUPERSEDED but retained (fallback + qe-test parity anchors). Paper configs added (qe_delivery_book_paper.yaml, qe_momentum_book_paper.yaml). Repo hygiene: 12 stale .pyc dups removed (.docx left — user's files). **v1 infra teardown NOT executed** — RA-1's decommission gate (N clean v2 paper sessions) is NOT met (0 real sessions; lake stale 2026-06-12), and it needs destructive live-AWS actions + human sign-off. Exact procedure staged in `docs/runbooks/v1-decommission-runbook.md` with the key hazard flagged: IndianCostModel + 3 v1 scripts are qe-test parity anchors — retain or golden-value-convert before `rm`. ADR-038. |
| M6 | Live readiness — machinery BUILT, live BLOCKED BY CONSTRUCTION | 🔒 BUILT 2026-07-06, LIVE BLOCKED — 'approved M6' = build the apparatus, NOT enable live. New: qe.live_gate (`LiveGateToken` + evidence ceremony: 6 pre-registered fail-closed checks — forward-gate-pass artifact, ≥12 forward months re-verified, ≥3 clean qe paper sessions, config-bound operator approval, kill clear, lake ≤7d; mints ONLY if all pass), LiveBroker double-gated (valid token AND explicit client; no real adapter wired), qe.livecheck.drills (staleness/kill/config-drift, all PASS). CLI `qe live` (refuses w/ ledger) + `qe drill` (PASS). `governance/live-gate/README.md` documents the human evidence artifacts. 71 tests. **VERIFIED: `qe live` → BLOCKED (5/6 fail: no forward-gate record, 5/12 months, 0/3 sessions, no approval, 24d-stale lake); `qe drill` → all PASS.** Live unblocks only on evidence (forward gate ~Dec-2026 + human), never authorization. ADR-039. |

**Standing work continues unchanged:** monthly forward-book cadence (bhavcopy refresh →
`replay_delivery_book_forward.py` → `check_forward_gate.py`) until M2 absorbs it.

### ADR-040 (2026-07-08) — first real qe cadence run: 3 defects found + fixed

The first post-cutover cadence run surfaced and fixed: (1) paper `due` bug — panel-frontier
day always looked like a month-end → the first real delivery paper session bought its basket
mid-month; fixed to sim's complete-month rule (`_pending_rebalances`, executes owed month-ends
at their own rows; mid-month = MTM-only); buggy book state reverted, journal retained as audit.
(2) study configs pinned at `end_date: 2026-06-30` → opened to 2027-12-31 (one-time config-hash
change; qe study now == v1 replay to the rupee incl. the 2026-06-30 rebalance). (3) gate-checker
reads only v1 state files — v1 replay is the interim feeder (runbook step 2b).

**Decommission-gate progress:** first clean qe paper sessions run 2026-07-08 — delivery 1/≥3,
momentum 1/≥3 (status OK, due=False, 0 orders, NAV ₹10,00,000 each; first rebalance = July
month-end, executes when August-proving data lands). **2026-07-15 cadence run: delivery 2/≥3,
momentum 2/≥3** (both status OK, due=False mid-month, 0 orders, 0 risk rejections, 0 kill
events) — after an operator-approved one-time rebind of both book states' `config_hash`
(ADR-042: ADR-041 P4's `assets`/`vol_lookback` schema defaults moved every config hash;
drift proven benign — studies == v1 replay to the rupee, stripped hash reproduces old
exactly; backups `*.bak-pre-rebind-20260715`). Lake at 2026-07-14; forward gate 6/12 both
IN PROGRESS (delivery alpha −9.85%, momentum +5.49%). **Open (P2) from ADR-042:** decide
schema-evolution-stable hashing or a journaled `qe book rebind` ceremony before the next qe
schema change; also `download_bhavcopy.py --end` defaults to 2025-12-31 (silently skips 2026
catch-up on a bare run).

**~~Open (P2)~~ ✅ DONE same day (2026-07-08):** `check_forward_gate.py` ported to read the qe
study summaries (`reports/qe/<factor>-book-*/summary.json`) as primary source — `--source
qe|v1|auto` (default auto = qe + cross-check vs v1 state when frontiers match; mismatch is
reported loudly). Evaluation math + pre-registered thresholds byte-identical for both sources;
self-test extended (qe-reader parity + shape-drift guard); real-data cross-check → MATCH both
books. Runbook step 2b is now an optional monthly cross-check, not a dependency. v1 replay
remains a parity anchor — do not delete.

---

## 🇺🇸 US EQUITIES PIVOT TRACK (ADR-041, approved 2026-07-09)

**Plan-of-record:** `docs/strategy/us-equities-pivot-plan.md`. QC/LEAN = local $0 research
bench; winning strategies ported into `qe/strategy/`; QuantEmbrace = execution/risk/infra.
Positional only (wave 1). NSE forward cadence + Dec-2026 gate run unchanged in parallel.
Every phase writes a report and **stops for human approval**.

| Phase | Scope | Status |
|---|---|---|
| P0 | ADR-041 + plan-of-record + operator prerequisites: **Robinhood** primary account + Agentic Trading beta invite (+ optional Gold/Cortex) — ⚠️ requires US residency status, NOT openable from India, operator must confirm eligibility (else revert to Alpaca); W-8BEN/funding; Docker + LEAN CLI install. Alpaca account DEFERRED to P6b | 🔶 Docs DONE 2026-07-09 (incl. Robinhood amendment); operator eligibility check + account/CLI setup pending |
| P1 | Curated US EOD lake (`market=US, segment=EQ`): 23 ETFs + 75 mega-caps, 2005→2026-07-09, Yahoo (primary, curl_cffi) × Nasdaq API (verifier) cross-validated in RETURNS space (`scripts/backtest/download_us_eod.py`; report `docs/backtesting/us-eod-lake-phase1-report.md`) | ✅ BUILT+VERIFIED 2026-07-10 — 97 promoted (MMC FAIL/excluded), 515,691 rows, snapshot `ds-b9b110ac58d57cae`, qe suite 69/69; **awaiting operator approval → P2** |
| P2 | Candidate screen: 7 QC-library positional strategies vs **pre-registered 5-gate screen** (`run_us_rotation_study.py`, T+1 exec, 5+10bps, Sharpe-vs-SHY; TR anchors matched slickcharts to 0.01pp) → `docs/strategy/us-qc-candidate-report.md` | 🔶 SCREEN DONE 2026-07-10 — **2/7 PASS: RPLITE 0.86 (shortlisted, clean) + XSMOM 0.86 ⚠️ (conditional — survivorship falsification on QC free cloud required)**; GEM/GTAA5/SECTOR fail (post-publication decay confirmed), AAA/LOWVOL 4/5 (G1, gate NOT relaxed); awaiting operator approval |
| P2b | LEAN CLI cross-check of RPLITE (`export_lake_to_lean.py` + `run_lean_crosscheck.py`, pre-declared tolerances T1 monthly-RMSE≤20bps / T2 NAV-ratio≤3% / T3 sign-agreement≥95%, zero-cost engine-mechanics-only check) | ✅ **PARITY PASS 2026-07-14** — report `docs/strategy/us-lean-crosscheck-p2b-report.md`. The 2026-07-10 "PARITY FAIL" (T1 RMSE 43.21bps) was root-caused: NOT a strategy/execution bug (vol-calc logic verified identical to 1e-8; whole-share rounding only 0.74bps; a real-but-cosmetic margin/BuyingPower bug fixed in `main.py` — `AccountType.Cash`+`ImmediateSettlementModel` — changed zero fills). **Actual cause: `run_lean_crosscheck.py`'s `compare()` used `resample("D").last()`, which let LEAN's intermittent (~15% of days) 17:00 chart sample silently override the always-present, reliable midnight sample, corrupting day alignment.** Fixed to align on midnight samples only. Re-run: **RMSE 3.4bps, NAV-ratio 0.96%, sign 100%, corr 0.9999 — all 3 gates pass.** RPLITE now cross-validated in two independent engines; ready for P3 pending operator approval. XSMOM survivorship falsification (QC free cloud) still not done — separate, unaffected. |
| P3 | qe US support: `USEquityCosts` (SEC fee/TAF/slippage, $0 commission), `America/New_York` clock + NYSE calendar, static config-listed universe, SPY benchmark, USD NAV. NSE ₹0.00 parity tests must stay green | ✅ **BUILT 2026-07-14** — report `docs/strategy/us-qe-phase3-report.md`. Added `USEquityCosts`+`cost_model_for_market()` (`qe/costs.py`), `market_tz()` (`qe/clock.py`), fixed `Panel.date_at()` + `month_end_positions`/`rebalance_schedule` to stop force-converting to IST (was mislabeling non-NSE panels — the P1-flagged "+1d" gap), `run_sim()` now dispatches tz+costs by `config.universe.market` (fails closed on an unrecognized market), `buy_hold_benchmark_return()` for SPY (`qe/research/benchmark.py`), currency-aware NAV in `render_session_report()`. Paper/live path (`qe/engine/paper.py`) and v1-cross-check (`qe/research/wf_v1.py`) deliberately untouched — still NSE-only by design (P5/never). 13 new tests `tests/qe/test_us_market.py`; **full qe suite 83/83 green, all pre-existing NSE parity tests unchanged**. Awaiting operator approval → P4. |
| P4 | Port shortlist to `qe/strategy/`; qe-vs-LEAN parity (pre-declared tolerance); walk-forward OOS; **pre-register US Forward Gate** (mirror of NSE gate, vs SPY) | ✅ **BUILT 2026-07-14 + real historical run** — report `docs/strategy/us-qe-phase4-report.md`. `RiskParityLiteStrategy` ported (`qe/strategy/risk_parity.py`, verbatim from `w_rplite`); **real finding: needed a small 0.5% cash_buffer — the pandas screen is a returns-space model with no solvency concept, qe's real-cash engine rejected every rebalance (100% weight + any cost > 100% cash) until fixed, exactly why FactorBookStrategy already has one**. qe-vs-pandas-screen parity: weight-target matches to 1e-10, zero-cost NAV path 9.85bps RMSE (transitively qe-vs-LEAN via P2b's 3.4bps). **Real historical run over the actual 2006-2026 US lake**: qe engine Sharpe 0.78/CAGR 7.18%/MaxDD −21.3% (vs Phase 2 screen's frictionless 0.86/9.5%/−22.2% — the gap is real costs+buffer+rounding); walk-forward IS 2006-16 (Sharpe 0.72) → OOS 2016-26 (Sharpe 0.82, actually improved, unlike every NSE strategy); **⚠️ mean monthly alpha vs SPY is −0.218%, positive only 41% of months — SPY's own beta was exceptional this window, so the pre-registered Forward Gate (cum_alpha>0, ≥58% pos-alpha) is a genuinely tough bar RPLITE may never clear forward — expected per Phase 2's own framing (diversification+lower-DD, not "beats SPY"), not a reason to relax anything**. US Forward Gate pre-registered `scripts/paper/check_us_forward_gate.py` (identical math/thresholds to NSE's, `--self-test` passes). `configs/qe_us_rplite_book.yaml` ready for P5 (start_date 2026-07-31). 6 new tests, full qe suite 87/87 green. Awaiting operator approval → P5. |
| P5 | US forward paper book(s): paper==sim to $0.00; fold into monthly cadence; accrue vs gate (calendar-time) | ✅ **BUILT+TESTED 2026-07-14, NOT ACTIVATED** — report `docs/strategy/us-qe-phase5-report.md`. Extended `qe/engine/paper.py` (real-time WallClock engine) for market=US: `_build_strategy` dispatches risk_parity_lite; broker/feed/clock all dispatch by `config.universe.market` (`cost_model_for_market`, `reference_symbol_for_market` — new, NSE=RELIANCE/US=SPY, `WallClock(tz=market_tz(...))`). **Found+fixed a real bug**: `SimClock.today()` hardcoded `.astimezone(IST)` — harmless for NSE (already IST) but a US clock pinned at NYSE close (16:00 ET) converted to 01:30 IST *next day*, mislabeling every US paper session's trading date; fixed to read the clock's own tz. `sim_clock_at()` takes `market=` (default NSE, backward compatible). **paper==sim proven for RPLITE** (`test_us_paper_reproduces_sim_exactly`, to the cent, same M4 discipline as NSE) + a direct regression-guard test for the SimClock bug. `configs/qe_us_rplite_book_paper.yaml` added; operator runbook updated (US book added to cadence + real-time paper sections, clearly marked NOT YET ACTIVATED). Deliberately did NOT run a real paper session — that creates the persisted book-state file marking official inception, which is the actual activation event awaiting operator approval. Full qe suite 89/89 green. Awaiting operator approval → first real session ≥2026-07-31. |
| P6a | First live surface (much later, gated): **Robinhood Agentic Trading MCP** — qe recommends → human/supervised agent relays into dedicated capital-capped agentic account (Robinhood approval previews + qe kill-switch/whitelist before relay); official MCP only, unofficial wrappers banned | 🔒 BLOCKED by design |
| P6b | Full automation (later still): ALPACA-FIX enum bug, Alpaca adapter behind `LiveGateToken`, close US universe-validation bypass (`order_validator.py:89-101`), US M6 preconditions; only if P6a proves out | 🔒 BLOCKED by design |

**Standing cautions:** QC "proven" strategies are hypotheses — most should die in validation.
Free data = LOW trust → quarantine → cross-validate → promote. Extend qe; never revive v1 for
US (v1 Alpaca code = porting reference only). Live BLOCKED.

---

## 🤖 HYBRID AI RESEARCH TRACK (ADR-043, approved 2026-09-25)

**Design:** `docs/architecture/hybrid-ai-system.md` · **Branch:** `feature/hybrid-ai-research`
(branched from `dev` checkpoint `d8ca741`). Offline advisory `qe/ai/` package; never in a
trading path; AI weight 0 by default; historical AI backtests are not evidence (contamination).
Operator authorised Phases 0–5 as one run, then STOP for review.

| Phase | Scope | Status |
|---|---|---|
| P0 | Current-state recon (`docs/architecture/current-state*.md`), TradingAgents adaptation analysis | ✅ DONE 2026-09-25 |
| P1 | Architecture/boundary/security/data-flow/look-ahead/discovery/experiment docs + ADR-043 | ✅ DONE 2026-09-25 |
| P2 | Schemas (`ResearchSignal` v1), `qe.ai` config, import-boundary + engine-untouched tests | ✅ DONE 2026-09-25 (fixed a real `safe_write_path` symlink bypass en route) |
| P3 | LLM protocol/fake/Bedrock adapter, guardrails, PIT tools, analyst agents | ✅ DONE 2026-09-25 |
| P4 | Bull/bear/critic/synthesizer, FAST/STANDARD/DEEP orchestration, research journal, CLI | ✅ DONE 2026-09-25 |
| P5 | Deterministic fusion (AI_ADVISORY default) + parity/invariance tests + ops docs + report | ✅ DONE 2026-09-25 — `tests/qe` 424 passed / 0 skipped (89 engine + 335 qe.ai); report `docs/research/ai-research-p0-p5-report.md`; **STOPPED for operator review** |
| P6 | Hypothesis drafts, strategy lifecycle ledger, forward AI shadow gate | ✅ DONE 2026-09-25 — F-11/F-12 fixed first; gate committed **DRAFT (unsigned)**; report `docs/research/ai-research-p6-report.md`; **STOPPED for review** |
| P7 | Post-trade analyst; knowledge-time-stamped reflection memory | ✅ DONE 2026-09-26 (`qe.ai post-trade`; real-lake run 20 trades) |
| P8 | Research dashboard | ✅ DONE 2026-09-26 (`qe.ai dashboard`, static/escaped/CSP) |
| P9 | External-data security hardening + quarantine | ✅ DONE 2026-09-26 for NSE announcements; downloader LIVE-VERIFIED same day (295 real records, 0 failures; 307/308 promoted, 0 held); fundamentals/sentiment still no source |
| P10 | First real Bedrock spend; paper-shadow validation (forward, post-cutoff only) | 🔶 backend + probe BUILT; **real run BLOCKED by the AWS account** (403 not-available / 404 across regions; $0 spent). Shadow gate still unsigned DRAFT. First clean decision date 2026-09-30 |

**Documented-only findings awaiting operator triage** (`docs/architecture/current-state.md §10`):
F-1 v1 `paper_trade` missing-field → live (HIGH) · F-2 non-NSE universe bypass in all modes
(HIGH) · F-10 `wf_v1.regime_series` total-period-turnover look-ahead · F-11 walk-forward with no
gates reports pass · F-12 family test-budget count not persisted · F-13 CI does not run `tests/qe`.

**✅ iCloud lake eviction RESOLVED 2026-09-25:** 4,764 evicted lake files re-downloaded (`brctl download` must
be run per FILE — the folder form silently does nothing). Real-lake `qe.ai` E2E then ran in 8 s. It can recur
(Optimize Mac Storage) — check `find backtest-data/lake -flags +dataless | wc -l` before a cadence run.

**Findings triage 2026-09-25** (`current-state.md §10a`): F-1, F-2, F-10, F-11, F-12, F-13 FIXED on
`fix/findings-triage` (off `dev`, merged into the AI branch); F-3…F-9, F-14 deferred/accepted with reasons.
**Open:** push + first GitHub run of the new CI `test-qe` job; sign-off of the AI shadow gate only after P10.

**Hybrid AI P7–P10 (2026-09-26):** built; report `docs/research/ai-research-p7-p10-report.md`. **Open, operator:** (1) enable Anthropic model
access on the AWS account, **or `export ANTHROPIC_API_KEY` and use `configs/qe_ai_research_anthropic.yaml` (first-party adapter BUILT 2026-09-26, ADR-043 addendum 3, no real call yet)**, then `python -m qe.ai probe … --allow-llm-spend`;
(2) ~~run the NSE downloader~~ DONE 2026-09-26 — 0 held on 307 genuine docs;
(3) research run on 2026-09-30 (first uncontaminated date), then sign off the shadow gate; (4) push + PRs so CI `test-qe` runs.
