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
| DONE-004 | `strategy_engine/service.py` — SQS long-poll consumer + FIFO signal publisher + DynamoDB state | ✓ 2026-04-25 |
| DONE-005 | `risk_engine/service.py` — `main()` now passes real boto3 clients (dynamo, s3, sqs) | ✓ 2026-04-25 |
| DONE-006 | `execution_engine/service.py` — real SQS long-poll loop replaces stub; `_handle_approved_signal_message()` added; `main()` entry point added | ✓ 2026-04-25 |
| DONE-007 | `configs/instruments.yaml` — 25+ NSE + US instruments, config-driven universe | ✓ 2026-04-25 |
| DONE-008 | `strategy_engine/universe/instrument_loader.py` — dynamic strategy registration from config | ✓ 2026-04-25 |
| DONE-009 | All 7 docs (`README`, `01`–`07`) written for beginner audience | ✓ 2026-04-25 |
| DONE-010 | `ZerodhaConnector` — real KiteTicker wiring (instrument cache, thread→async bridge, reconnect re-subscribe) | ✓ 2026-04-26 |
| DONE-011 | `AlpacaConnector` — real alpaca-py `StockDataStream` wiring (trade + quote handlers, async background task) | ✓ 2026-04-26 |
| DONE-012 | `SQSTickPublisher` (new) — latest-value-per-symbol batching, `SendMessageBatch`, graceful drain on stop | ✓ 2026-04-26 |
| DONE-013 | `TickProcessor` — wired `SQSTickPublisher` as 4th output alongside S3 and DynamoDB | ✓ 2026-04-26 |
| DONE-014 | `DataIngestionService` — subscribe after connect, instruments from config, SQS publisher lifecycle | ✓ 2026-04-26 |
| DONE-015 | `StrategyEngine` — fixed queue name from `sqs_signals_queue` → `sqs_market_data_queue` for tick input | ✓ 2026-04-26 |
| DONE-016 | `settings.py` — added `sqs_market_data_queue`, `dynamodb_table_prefix` to `AWSConfig` | ✓ 2026-04-26 |
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
