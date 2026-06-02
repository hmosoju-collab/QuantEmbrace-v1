# QuantEmbrace Platform Review
## Institutional-Grade Trading System Audit

**Review Date:** 2026-05-11  
**Reviewer Role:** Chief Quant Trading Platform Architect / AWS Reliability Engineer  
**Scope:** Full codebase audit — Phases 1–7 complete  
**Capital at Risk:** ₹1,00,00,000 (₹1 crore, ~$120k USD)  
**Markets:** NSE India (Zerodha Kite Connect) + US Equities (Alpaca)

---

## Executive Summary

QuantEmbrace is an architecturally ambitious, well-structured algorithmic trading platform. After reviewing all seven implementation phases — data ingestion, strategy engine, risk engine, execution engine, AI enrichment, retry infrastructure, and live-readiness tooling — this review concludes:

**The platform is NOT ready for live capital deployment today.** There are three critical defects that could produce incorrect P&L calculations under concurrent fills, silent risk limit bypasses on missing configuration, and unbounded memory growth under sustained operation. These are fixable — none require architectural overhaul — but they must be addressed before any real money is committed.

**The platform IS well-architected at the macro level.** Layer separation is enforced. The risk engine is a mandatory gateway. Idempotency through DynamoDB conditional writes is consistently applied at the order layer. The retry/DLQ pattern for enrichment failures is sound. Zerodha rate limiting is dual-mechanism (token bucket + sliding window). The go-live runbook is thorough. The pre-flight check covers the right signals.

**The biggest risk is not the code — it is the missing production risk configuration.** `configs/risk_limits_production.yaml` does not exist in the repository. The runbook references it. The go-live checklist references it. The risk engine reads limits from settings profiles. If the "tiny-live" profile defaults are deployed as-is to a ₹1 crore account, position sizing and loss caps may be misconfigured for actual capital.

**Recommended posture:** Fix the three critical defects, create the production risk config, complete the missing scripts, run 5 consecutive READY paper days, then deploy with the first strategy at 10% capital per the promotion table.

---

## 1. Critical Blockers — Must Fix Before Any Live Capital

These defects will cause incorrect behavior or data loss in production. Do not deploy while any of these remain open.

### BLOCKER-001: Race Condition in Daily P&L Write

**File:** `services/risk_engine/validators/loss_validator.py`, `_update_daily_pnl_and_nav()` (line ~454)

**Defect:** The method performs a read-modify-write on the `PNL_DAY#<date>` DynamoDB record without a conditional expression. The sequence is: `get_item` → compute new value → `put_item`. If two fills arrive within the same millisecond (entirely plausible on a MARKET order that fills in multiple partial tranches, or if two symbols fill simultaneously), both goroutines read the same `realized_pnl`, add their own delta, and each writes back — one write silently overwrites the other. The net effect: one fill's P&L contribution vanishes. The daily loss limit then underestimates realized losses, potentially allowing trading to continue past the loss cap.

**Severity:** The daily loss limit is the last line of defence before the kill switch fires. Underestimating losses past this point is a Category 1 safety defect.

**Fix required:** Replace the bare `put_item` with a `update_item` using an `ADD` expression for atomic increment, or add a `ConditionExpression` that checks `realized_pnl = :prev_value` and retries on `ConditionalCheckFailedException`. The `update_item` + `ADD` path is simpler and avoids all retry logic.

### BLOCKER-002: Unpaginated DynamoDB Queries in Risk Calculations

**File:** `services/risk_engine/validators/loss_validator.py`

**Defect:** Two methods read DynamoDB without pagination:

`_get_unrealized_pnl()` (line ~362) performs a full `scan` of the positions table without a pagination loop. DynamoDB returns at most 1MB per response. If the positions table contains more than ~5,000–8,000 active position records (realistic for a multi-symbol, multi-strategy system over time), positions beyond the first page are silently ignored. Unrealized P&L is then understated, allowing risk limits to pass when they should not.

`_fetch_realized_pnl_from_db()` (line ~301) runs a `query` with no pagination loop. Same truncation risk for high-fill-count days.

**Severity:** Silent understatement of risk exposure. The system believes it has less risk than it actually does.

**Fix required:** Both methods must implement a pagination loop that follows `LastEvaluatedKey` until exhausted. For `_get_unrealized_pnl`, a `query` on a GSI keyed by `trade_date` is preferable to a full `scan` — the scan is also a cost issue (reads every record in the table on every risk decision).

### BLOCKER-003: Missing Production Risk Limits Configuration

**File:** `configs/risk_limits_production.yaml` — does not exist

**Defect:** The go-live runbook (Section 1.6) instructs the operator to "review `configs/risk_limits_production.yaml` and confirm" position size, loss cap, max open positions, and kill switch thresholds before going live. This file does not exist in the repository. The risk engine falls back to the `"tiny-live"` profile from `RiskLimits.for_profile()`. Whether that profile's defaults are appropriate for ₹1 crore in production is unknown from the codebase alone.

**Severity:** If the wrong risk profile is active on go-live day, every subsequent risk decision is calibrated incorrectly.

**Fix required:** Create `configs/risk_limits_production.yaml` with explicit values for every risk parameter. Add a preflight check assertion that the file exists and all required fields are populated before the production service starts.

---

## 2. High-Risk Issues — Major Financial Loss Potential

### HIGH-001: Unbounded `_signal_locks` Dictionary

**File:** `services/execution_engine/service.py`, `execute_approved_signal()`

**Defect:** `self._signal_locks.setdefault(signal_id, asyncio.Lock())` adds a new `asyncio.Lock` keyed by `signal_id` for every signal processed. Completed signals are never removed from this dictionary. On a sustained trading day with hundreds of signals, this dictionary grows without bound. The locks are never contended after the signal completes, so they serve no purpose beyond the first use — but they accumulate in memory. On an EC2 t4g.small (2GB RAM) running for weeks, this is a memory leak with a trading-day time constant.

**Fix required:** Remove the entry from `_signal_locks` after `execute_approved_signal()` completes (inside a `finally` block), or use a `weakref.WeakValueDictionary` to allow GC to collect idle locks automatically.

### HIGH-002: Strategy Engine Kill Switch Uses Local Poll Cache, Not KillSwitchCache

**File:** `services/strategy_engine/service.py`

**Defect:** The strategy engine implements its own kill switch poll with `_ks_active`, `_ks_checked_at`, and `_ks_poll_interval` — a hand-rolled 1s DynamoDB poll that is separate from the `KillSwitchCache` class that Phase 4 introduced in the risk engine. The risk engine's `KillSwitchCache` was specifically built to centralise this logic. The strategy engine's hand-rolled version does not benefit from any future improvements to `KillSwitchCache`, and the two implementations could diverge (e.g., different poll intervals, different handling of DynamoDB errors). If the strategy engine's poll erroneously fails open (due to a DynamoDB timeout that is swallowed), it will continue generating signals even when a kill switch is active.

**Fix required:** Refactor strategy engine to use `KillSwitchCache` directly, matching the risk engine's implementation.

### HIGH-003: `InstrumentRegistry` Graceful Degradation Silently Disables Safety Checks

**File:** `services/risk_engine/service.py`, `__init__()` (line ~235)

**Defect:** If `instruments.yaml` fails to load (file missing, YAML parse error, schema mismatch), `InstrumentRegistry` is set to `None` and a warning is logged. The `SpreadGateValidator`, `SectorConcentrationValidator`, and `LiquidityValidator` then run in "graceful degradation mode," meaning they skip validation and pass the signal through. On a production system, a misconfigured or accidentally deleted `instruments.yaml` causes three risk validators to silently become no-ops. An operator monitoring logs might not notice the warning during a busy trading session.

**Fix required:** In production (`QE_ENVIRONMENT=production`), an `InstrumentRegistry` load failure must be a hard failure that prevents service startup, not a warning. Add this check to `preflight_check.py`. Graceful degradation is acceptable in development, not in production.

### HIGH-004: MIS Square-Off Is a Background Task With No Failure Monitoring

**File:** `services/execution_engine/service.py`, `mis_manager.run()`

**Defect:** The MIS auto-square-off manager (which must close all NSE intraday positions before 15:15 IST) is started as an `asyncio.create_task()`. If this task fails silently (unhandled exception inside `mis_manager.run()`), the task dies and is never restarted. The `asyncio.gather()` in the start sequence will surface the exception and bring down the whole service — which is the correct behaviour — but only if the exception propagates to the gather. If `mis_manager.run()` swallows internal exceptions (which many long-running loops do), the task becomes a zombie coroutine: still running but doing nothing. MIS positions then remain open past 15:15 IST and Zerodha will auto-square-off at market prices with possible adverse slippage.

**Fix required:** Add explicit exception handling and an alert in the MIS manager's main loop. Add a CloudWatch alarm on the `MISSquareOffFailed` metric. Add a watchdog that detects if the MIS task is no longer running and fires an SNS notification before 15:00 IST.

### HIGH-005: `datetime.utcnow()` Usage (Python 3.12 Deprecation)

**File:** `services/execution_engine/brokers/zerodha_broker.py`, `_OrderPlacementLimiter`

**Defect:** `_OrderPlacementLimiter` uses `datetime.utcnow()`, which is deprecated in Python 3.12 and scheduled for removal. The correct replacement is `datetime.now(timezone.utc)`. This is currently harmless but will become a `DeprecationWarning` flood in Python 3.12 logs, and a hard runtime error in a future Python version. More immediately, if this class is ever extended to compare timestamps with timezone-aware datetimes from DynamoDB (which stores ISO 8601 with `+00:00`), mixing naive and aware datetimes will raise `TypeError` at runtime, bypassing the rate limiter entirely.

**Fix required:** Replace all `datetime.utcnow()` calls with `datetime.now(timezone.utc)`. Run `ruff` with the `DTZ` rule set enabled to catch any remaining instances.

---

## 3. Medium-Risk Issues

### MED-001: Missing Operational Scripts Referenced in Runbook

The go-live runbook and post-session checklist reference the following scripts that do not appear in the repository file listing:

- `scripts/ops/reconcile_positions.py` — critical for post-halt reconciliation
- `scripts/auth/refresh_zerodha_token.py` — referenced in T-30 procedure and Section 4.5

Both are invoked during emergency procedures (Section 4.5 and 4.6 of the runbook). If an emergency halt occurs and the operator follows the runbook, they will discover these scripts do not exist at the worst possible moment.

### MED-002: `_candle_processing_loop()` DynamoDB Read Amplification

**File:** `services/strategy_engine/service.py`

The candle processing loop polls DynamoDB every 500ms for all 5 CANDLE-interface strategies. Across multiple symbols (the instrument universe from `instruments.yaml`) and 6-hour trading sessions, this generates sustained DynamoDB read traffic. The current design does not appear to use DynamoDB Streams or change data capture — it is a pure polling pattern. At scale (20+ symbols, 5 strategies), this could produce 200+ read units per second on the candle-cache table, which will exceed the on-demand capacity burst budget and incur throttling.

**Risk:** Throttled DynamoDB reads → missed candles → strategy signals not generated → missed trades (not dangerous, just expensive in opportunity cost).

### MED-003: Strategy State Lost on SIGKILL

**File:** `services/strategy_engine/service.py`

Strategy indicator state (moving average windows, candle accumulators) is persisted to DynamoDB in `stop()`. If the EC2 instance is terminated via SIGKILL (e.g., Spot interruption, OOM kill, or `aws autoscaling set-desired-capacity 0`), the `stop()` method is never called and state is lost. All strategies then cold-start without their indicator windows, requiring a warm-up period during which signals may be generated on incomplete data. MomentumStrategy with a 20-bar VWAP window is the highest risk here — the first 19 bars will produce signals on a partially initialised state.

**Fix required:** Implement a SIGTERM handler that calls `stop()` before the process exits. EC2 ASG termination uses SIGTERM before SIGKILL, giving 30 seconds to flush state. This is already described as a target in CLAUDE.md but is not visible in the current strategy engine implementation.

### MED-004: No DLQ Growth Alerting in Terraform

**File:** `infra/terraform/modules/kafka/main.tf`

Phase 7 added `signals.enriched.dlq` and `signals.pending.dlq` topics. There are no CloudWatch alarms monitoring DLQ consumer-group lag or message count growth. A spike in DLQ messages is a leading indicator of a systematic processing failure (bad enrichment model, schema mismatch, risk validator bug). Without an alarm, a DLQ buildup could go undetected for an entire trading session.

### MED-005: Paper Trading Validation Has No Automated Counter

The 5-day paper trading validation gate requires 5 consecutive READY days. There is no code that tracks this counter automatically. It is entirely manual — an operator must remember to reset the counter if a NOT_READY day occurs and must manually verify 5 consecutive days. This is a governance gap: under time pressure to go live, the counter could be manually advanced incorrectly.

### MED-006: Alpaca US Feature Store Not Implemented

The memory notes Phase 8 as "US feature store (Alpaca candle stream → FeatureEngine)." Phases 1–7 built the feature store for NSE data only. All 6 strategies appear to be designed for both markets, but the AI enrichment pipeline for US equities signals has no feature data to draw from. US signals reaching the AI engine will either be enriched with NSE features (incorrect) or will trigger fallback/degradation. This is a systematic enrichment quality issue for all US equity strategies.

---

## 4. Low-Risk Improvements

### LOW-001: Kafka Producer Circuit Breaker Missing

`KafkaFailurePublisher` has no circuit breaker at the producer level. If the MSK cluster is briefly unreachable, `publish_retry()` and `publish_dlq()` calls will fail. The current implementation returns `False` on failure and commits the original message. This means a signal that encounters a transient Kafka producer failure is committed as "processed" and silently dropped — neither retried nor DLQ'd. A producer circuit breaker with a short open window and exponential backoff would prevent this.

### LOW-002: `_fetch_realized_pnl_from_db` Falls Back to Full DB Query on Cache Miss

When the 30-second P&L cache is cold (after startup), `DailyLossValidator` queries DynamoDB for all fills today. This query is also unpaginated (covered in BLOCKER-002) but additionally executes on every cold start and after every 30-second TTL expiry. On a busy day with thousands of fills, this is a latency spike on the first signal validation after each cache expiry window.

### LOW-003: `signals.enriched` Retry Replayer Uses Same Consumer Group

`KafkaRetryReplayer` for `signals.enriched.retry` appears to replay back to `signals.enriched` using a separate consumer group. If the replayer consumer group falls behind (due to high retry volume), replayed messages accumulate lag and may be delivered to the risk engine after their `expires_at` timestamp, causing them to be routed to the DLQ rather than processed. A staleness check before replay (already implemented) handles this, but the DLQ then becomes the sink for all high-lag retries regardless of whether the underlying error was resolved.

### LOW-004: CloudWatch Namespace Fragmentation

Metrics are emitted under `QuantEmbrace/Trading`, `QuantEmbrace/StrategyEngine`, `QuantEmbrace/AIEngine`, `QuantEmbrace/RiskEngine`, and possibly others. Without a unified dashboard that aggregates these namespaces, the operator's view during a trading session requires switching between multiple CloudWatch dashboard panels. A unified `QuantEmbrace/Operations` namespace with cross-service latency and error rate rollups would improve observability significantly.

### LOW-005: Zerodha Token Refresh Has No Liveness Check

`ZerodhaConnector` stores `access_token` at init time. There is no periodic validation that the token is still valid during a session. If Zerodha rotates or invalidates a token mid-session (rare but documented), the connector continues using the stale token until the next API call fails. The execution engine's circuit breaker will then open, halting NSE trading until `refresh_zerodha_token.py` is run manually.

---

## 5. Missing Components

The following components are referenced in documentation, runbooks, or architecture diagrams but do not exist in the codebase as of the review date:

| Component | Referenced In | Impact |
|---|---|---|
| `configs/risk_limits_production.yaml` | Go-live checklist §1.6 | Critical — risk limits unknown |
| `scripts/ops/reconcile_positions.py` | Runbook §4.6 | High — emergency procedure broken |
| `scripts/auth/refresh_zerodha_token.py` | Runbook §4.5, T-30 procedure | High — token refresh procedure broken |
| Alpaca candle stream → FeatureEngine | Phase 8 spec, memory | Medium — US enrichment incomplete |
| `scripts/backtest/run.py` | CLAUDE.md how-to-run | Medium — no validated backtesting |
| Strategy backtests for all 6 strategies | Go-live §2 (Sharpe, drawdown criteria) | High — promotion criteria unverifiable |
| Grafana dashboard configs | CLAUDE.md tech stack | Low — monitoring incomplete |
| `docs/incidents/` directory | Go-live §4.2 | Low — incident log path missing |

---

## 6. Architecture Assessment

### What Is Working Well

**Layer separation is genuinely enforced.** The strategy engine imports no execution or risk code. The risk engine imports no broker clients. The execution engine will refuse to place an order without a `risk_decision_id`. This is not just documentation — it is structural. This is the platform's greatest architectural strength.

**DynamoDB idempotency at order submission is excellent.** `submit_order` uses `transact_write_items` to atomically write the order record and reserve the `SIGNAL#{signal_id}` key. Two concurrent consumers racing on the same signal correctly produce one order. The three-case handling (PENDING retry, ACK_UNKNOWN recovery, terminal dedup) is thorough and well-documented.

**The retry/DLQ pattern is well-designed.** Publishing to `.retry` on transient errors and directly to `.dlq` on expiry (where replay has no value) is the correct pattern. `max_retry_attempts=3` with automatic escalation is a reasonable limit. The `qe-retry-attempt` header tracking is clean.

**The EnrichmentWatchdog fallback is safe.** Trading continuing on `signals.pending` when AI enrichment is unavailable is the correct degradation path — it is a conservative choice that prefers lower-quality approved signals over halted trading. The watchdog's hysteresis (2 checks to enter fallback, 5 checks to exit) prevents oscillation.

**The go-live runbook is genuinely good.** The T-60/T-30/T-15/T-5 procedure sequence, the 6 emergency procedures, the rollback section, and the 5-day READY gate are all institutional-grade. Most individual algo trading systems have no runbook at all.

### Architecture Concerns

**Kill switch has three independent propagation paths.** SNS notification, DynamoDB poll (KillSwitchCache 1s), and direct DynamoDB query in `_refresh_durable_kill_switch_state()`. While redundancy is intentional, three paths means three failure modes and three test surfaces. The strategy engine uses a fourth independent implementation. The recommendation is to consolidate all non-SNS paths into `KillSwitchCache` with consistent poll intervals and error handling.

**AI enrichment in the hot signal path adds irreducible latency.** Every signal must transit: strategy → `signals.pending` → Kafka → ai_engine → model inference → `signals.enriched` → Kafka → risk engine. Each Kafka hop is 5–20ms under normal conditions. AI inference (if models are loaded in memory) adds another 5–50ms. Total enrichment overhead: 15–100ms per signal. For momentum strategies reacting to tick data, this may be acceptable. For the scalp_1m strategy trading 1-minute candles, a 100ms delay on a signal is 0.17% of the candle's entire duration. Whether this is acceptable depends on strategy backtest sensitivity to execution delay — which brings us back to the missing backtests.

**Single-partition Kafka topics for production.** `ticks.nse` and `ticks.us` have 2 partitions each; `signals.pending` and `signals.enriched` have unspecified partition counts. Under a multi-symbol universe with 6 concurrent strategies, a single partition for signals becomes a serialization bottleneck. Risk validation throughput is bounded by one consumer per partition. Consider 3–6 partitions for signal topics, keyed by symbol, to allow parallel risk validation across symbols.

**Candle cache is a DynamoDB polling pattern, not event-driven.** The 500ms candle poll in the strategy engine means signal latency from candle close to signal publication has a worst-case of 500ms + processing time. A DynamoDB Streams-based trigger (or a dedicated Kafka topic `candles.nse`) would reduce this to near-zero. The current design is functionally correct but adds latency for time-sensitive candle strategies.

---

## 7. Risk Engine Assessment

The risk engine is the platform's most mature component. The validator chain (SignalAge → Position → Exposure → DailyLoss → Margin → Slippage → SpreadGate → Sector → Liquidity) is well-ordered: cheap validators (age check, kill switch) run first; expensive DynamoDB reads run only on signals that pass earlier gates.

**The validator sequence is correct.** SignalAgeValidator rejecting stale signals before any DynamoDB reads is an important performance optimisation that also reduces attack surface (stale signals cannot consume DynamoDB capacity).

**KillSwitchMonitor's 4 background checks are appropriate.** Auto-triggering on position drift, margin breach, and loss limit breach means the kill switch fires automatically on risk events that the operator may not observe in real time.

**P&L race condition (BLOCKER-001) is the risk engine's Achilles heel.** Everything else in the risk engine is well-implemented. Fix the `_update_daily_pnl_and_nav` write and the risk engine becomes production-ready.

**DailyLossValidator TTL-based cache is a sound design.** 30-second freshness is reasonable for a risk check. The `rehydrate()` call on startup correctly restores today's P&L from DynamoDB before the first signal arrives.

**Fill dedup via `FILL#<event_key>` conditional put is correct.** Prevents double-counting a fill event that Kafka delivers twice (at-least-once semantics). This is the right approach.

---

## 8. Kafka Architecture Assessment

**MSK Serverless with SASL/OAUTHBEARER IAM is the right choice** for a system of this scale. No cluster capacity planning, automatic scaling, and IAM-native authentication without API key rotation is appropriate for a personal trading system.

**The topic naming convention (`signals.pending`, `signals.enriched`, `signals.approved`, `signals.enriched.retry`, `signals.enriched.dlq`) is clean and extensible.**

**Consumer group naming (`aiengine-v1`, `risk-v1`, `execution-v1`, `strategy-v1`) correctly versions the consumer offset.** Incrementing the group version when deploying a schema-breaking change is the correct rollout pattern.

**Concerns:**

The system does not appear to have consumer group lag alerting for the primary trading topics. Lag on `aiengine-v1` (signals.pending) is monitored by `EnrichmentWatchdog`, but lag on `risk-v1` (signals.enriched), `execution-v1` (signals.approved), and `strategy-v1` (ticks) has no automated response. A CloudWatch alarm on MSK consumer-group lag > 1000 messages for any trading consumer group should trigger an SNS alert.

Kafka topic partition count for `signals.approved` is not visible in the review. If this is 1 partition, the execution engine processes one order at a time regardless of how many concurrent strategies generate signals. Under a coordinated multi-symbol entry (e.g., all strategies react to a market event simultaneously), this creates a submission queue with the last signals experiencing 500ms–2s delay.

Kafka message retention is not reviewed here but should be set to at least 24 hours on all signal topics to allow next-day replay and debugging.

---

## 9. Python Code Quality Assessment

**The codebase is significantly above average for an individual algo trading system.** Type hints are consistently applied. Pydantic models are used for data validation. `asyncio` is used correctly — `asyncio.to_thread()` for DynamoDB calls is the right pattern for integrating blocking boto3 calls into an async event loop.

**Structured logging with correlation IDs is implemented.** The use of `set_correlation_id()` and `get_logger()` throughout the codebase enables cross-service trace correlation in CloudWatch Logs.

**Docstrings follow Google style consistently.** The multi-paragraph lifecycle documentation in service classes (strategy, risk, execution) is genuinely useful — it explains the "why" of design decisions rather than just the "what."

**Code quality concerns:**

The `_signal_locks` memory leak (HIGH-001) is symptomatic of a broader pattern: the execution service never cleans up resources associated with completed signals. A future audit should check for any other per-signal dictionaries or sets that grow without bound.

The `_OrderPlacementLimiter` sliding window using a `deque` is a correct implementation but the `datetime.utcnow()` issue (HIGH-005) should be caught by the `ruff` linter's DTZ rules. The fact that it was not caught suggests the `ruff` configuration does not enable the DTZ rule set. Consider enabling `DTZ001` through `DTZ012` in `ruff.toml`.

Property-based testing with `hypothesis` is listed in the tech stack but no hypothesis tests were observed in the reviewed test files. Risk calculation properties (e.g., "approving a signal that would exceed the daily loss limit is impossible regardless of input ordering") are exactly the kind of invariants that property-based testing excels at. This is a testing gap.

---

## 10. AWS Architecture Assessment

**EC2 ARM64 ASGs with Graviton are the right compute choice** for long-running trading services. The c6g/t4g cost advantage over x86 is 20–40% for equivalent performance. No regrets on removing ECS Fargate.

**MSK Serverless is correctly cost-optimised** — no idle cluster cost when markets are closed.

**S3 lifecycle policies are documented in CLAUDE.md** but their Terraform implementation was not verified in this review. Confirm `aws_s3_bucket_lifecycle_configuration` resources exist in the Terraform modules and are applied to the production buckets.

**VPC endpoints for S3 and DynamoDB** are noted in CLAUDE.md but not verified in Terraform. Without these, all DynamoDB and S3 traffic flows through the NAT Gateway, incurring $0.045/GB in data transfer costs. On a high-fill day with S3 audit log uploads and DynamoDB polling, this adds up.

**No multi-AZ strategy is visible for EC2 ASGs.** If all ASG instances land in the same AZ and that AZ has a partial outage, all trading services fail simultaneously. Terraform ASG configurations should specify `availability_zones` across at least 2 AZs or use `vpc_zone_identifier` with multiple subnets.

**CloudWatch log retention policy** — confirm that all log groups have a `retention_in_days` setting. Default CloudWatch log retention is indefinite, which will generate unbounded storage costs over a multi-year trading horizon.

---

## 11. Observability Assessment

**What is in place:** CloudWatch metrics under multiple namespaces, structured logging with correlation IDs, a health server on each service (port 8080–8083), pre-flight check script, paper session report, SNS kill switch notifications.

**What is missing or incomplete:**

There is no confirmed Grafana dashboard configuration in the repository. CLAUDE.md lists Prometheus + Grafana as part of the stack, but no `prometheus.yml`, no Grafana dashboard JSON, and no node-exporter sidecar configuration were observed. Real-time visibility into signal latency, fill rate, and P&L during a live session requires a working dashboard, not just CloudWatch metrics.

The `paper_session_report.py` generates a daily report but does not produce intraday metrics. During a live session, an operator has no automated view of current enrichment rate, approval rate, or fill rate. Adding a `live_session_dashboard.py` that refreshes every 60 seconds during market hours would significantly reduce operational risk.

No alerting on Zerodha WebSocket disconnects is visible. If `KiteTicker` disconnects and fails to reconnect (network partition, token expiry), the data ingestion service stops publishing ticks silently. Strategies continue running but process no new data. Adding a metric `DataIngestionTicksLastSeenAge` with an alarm threshold of >60 seconds would catch this.

---

## 12. Strategy and ML Assessment

**Six strategies are implemented:** MomentumStrategy (tick-based), ORB, Scalp1m, VWAPReversionStrategy, IntradayTrend15mStrategy, PreCloseMomentumStrategy (all candle-based). All start as `paper_trade=True` with manual promotion.

**Critical gap: No backtests are available.** The go-live checklist Section 2 requires individual strategy promotion criteria including annualised Sharpe ≥ 0.5 and max drawdown ≤ 5% over 5 paper days. Paper trading alone cannot validate these metrics — 5 days is statistically insufficient. The `scripts/backtest/` directory exists but contains no scripts. Backtesting each strategy over at least 12 months of historical data should be a prerequisite for paper trading, not something deferred to Phase 8+.

**AI enrichment quality for US equities is unknown.** The FeatureEngine was built on NSE candle data. US equities have different market microstructure (decimal pricing, different exchange hours, no MIS concept). Whether the regime classifier and quality scorer produce meaningful signals for US equities is untested.

**The `quality_score` filter threshold in DynamoDB strategy-config** is mentioned in the go-live checklist as "quality filter threshold > 0 in DynamoDB strategy-config for all live strategies." This implies the risk engine uses `quality_score` from enriched signals to further filter signals. If a strategy's quality threshold is 0 (the default), all signals pass regardless of model confidence. This threshold should be set and documented per strategy before live deployment.

---

## 13. Production Failure Scenarios

### Scenario A: Both fills for the same symbol arrive simultaneously (concurrent fills)

**Current behaviour:** BLOCKER-001 applies. One fill's P&L is lost. Daily loss limit underestimates losses.  
**Required behaviour:** Both fills contribute atomically to P&L. Loss limit is accurate.  
**Status:** ❌ Not handled — fix required.

### Scenario B: EC2 instance is Spot-terminated mid-trading-session

**Current behaviour:** SIGTERM fires. Strategy engine calls `stop()` (if SIGTERM handler is implemented — not confirmed). If SIGKILL fires before stop() completes, strategy state is lost. In-flight Kafka messages are not committed. After restart, at-least-once delivery replays the last uncommitted signals.  
**Required behaviour:** SIGTERM handler flushes state, then exits gracefully within 30s.  
**Status:** ⚠️ Partial — SIGTERM handler existence in strategy_engine not confirmed. Do not use Spot instances for the execution engine or risk engine in production.

### Scenario C: MSK becomes unreachable for 2 minutes

**Current behaviour:** All services retry Kafka connections with exponential backoff. Health servers remain running. Services do not crash. No new signals flow.  
**Required behaviour:** Same — this is correct.  
**Status:** ✅ Handled correctly.

### Scenario D: Zerodha access token expires at 07:30 IST mid-session

**Current behaviour:** Execution engine receives 403 from Kite API. Circuit breaker opens. NSE orders rejected until token is refreshed via `scripts/auth/refresh_zerodha_token.py`.  
**Required behaviour:** Same — this is correct. However, `refresh_zerodha_token.py` does not exist (MED-001).  
**Status:** ⚠️ Runbook procedure is correct but the script it references does not exist.

### Scenario E: `signals.enriched.retry` accumulates backlog faster than replayer drains

**Current behaviour:** Replayer processes retry messages and re-publishes to `signals.enriched`. If the source of errors is not resolved, retried messages fail again, re-enter retry, and escalate to DLQ after 3 attempts. DLQ accumulates. No alarm fires (MED-004).  
**Required behaviour:** DLQ growth alert fires within 5 minutes. Operator investigates.  
**Status:** ⚠️ Retry/DLQ flow is correct; alerting is missing.

### Scenario F: AI engine model produces `quality_score = 0` for all signals (model degradation)

**Current behaviour:** All signals are enriched with `quality_score = 0`. If strategy-config quality threshold is > 0, all signals are filtered by the risk engine. No trades are placed. No alert fires. Operator notices zero fills at end of day.  
**Required behaviour:** Alert when approval rate drops below 10% of normal. Alert when enrichment quality_score mean < threshold.  
**Status:** ❌ Model degradation is not monitored.

### Scenario G: Position monitor detects broker-DynamoDB drift and fires kill switch

**Current behaviour:** Kill switch activates. All pending risk approvals block. All pending orders cancel. Execution engine halts.  
**Required behaviour:** Same — this is the correct response. Operator should follow §4.1 → §4.6 → §4.2.  
**Status:** ✅ Handled correctly.

---

## 14. Testing Strategy Assessment

**Unit tests (Phase 7: 17 tests):** Cover enriched processing loop, retry/DLQ routing, kill switch halt, duplicate suppression. These are appropriate and well-structured using source inspection for loop behaviour that is difficult to unit test otherwise.

**Gaps in unit testing:**
- No unit tests for `DailyLossValidator` concurrent fill scenario (the BLOCKER-001 defect)
- No unit tests for `_OrderPlacementLimiter` sliding window edge cases
- No property-based tests for risk calculation invariants
- No unit tests for MIS square-off timing logic

**Integration tests:** Mentioned in CLAUDE.md (LocalStack + Redpanda). A `tests/integration/` directory exists but content was not reviewed. Integration test coverage for the full signal flow (strategy → risk → execution) was not confirmed.

**Missing test categories:**
- **Replay tests:** Feed a recorded production session's Kafka messages back through the full stack and verify fills match expected P&L.
- **Chaos tests:** Simulate Kafka partition leader election, DynamoDB throttling, broker API timeouts. Verify kill switch fires under each failure mode.
- **Market-open stress tests:** Simulate the first 5 minutes of NSE open (highest tick volume, highest signal rate). Verify no signals are dropped, no race conditions triggered.
- **Broker failure tests:** Zerodha returns 503 for 30 seconds. Verify circuit breaker opens, fills paused, no duplicate orders on recovery.

---

## 15. Operational Checklist

### Daily (Market Days)

**Pre-market (T-60 min before NSE open, i.e., 08:15 IST):**
- [ ] Refresh Zerodha access token
- [ ] Run `preflight_check.py --env production`
- [ ] Verify all EC2 ASGs: desired ≥ 1, at least 1 healthy instance
- [ ] Confirm MSK cluster status = ACTIVE
- [ ] Confirm kill switch = INACTIVE
- [ ] Check CloudWatch for any overnight alarms
- [ ] Verify `instruments.yaml` is current (NSE corporate actions)

**During session:**
- [ ] Monitor CloudWatch `QuantEmbrace/RiskEngine/SignalsApproved` (should be > 0 within 15 min of open)
- [ ] Monitor enrichment rate (should be ≥ 80%)
- [ ] Monitor fill rate (should be ≥ 90% of approved)
- [ ] Check that no DLQ messages are accumulating

**Post-market:**
- [ ] Run `paper_session_report.py --date today`
- [ ] Verify all NSE MIS positions are flat (auto square-off confirmed)
- [ ] Verify all US equity positions are intentionally open or flat per strategy
- [ ] Review CloudWatch for anomalies
- [ ] Check S3 audit log for DLQ messages
- [ ] File incident report if any emergency procedure was invoked
- [ ] Update strategy promotion table if metrics changed

### Weekly

- [ ] Review DynamoDB table sizes and WCU/RCU consumption
- [ ] Review S3 storage costs and lifecycle policy effectiveness
- [ ] Review CloudWatch log storage costs
- [ ] Review MSK data transfer costs
- [ ] Run 7-day paper session report across all strategies
- [ ] Verify EC2 instance types are appropriately sized for actual CPU/memory usage

### Before Any Code Deployment

- [ ] Run all unit tests (`pytest tests/unit/ -v`)
- [ ] Run integration tests (`pytest tests/integration/ -v`)
- [ ] Verify `terraform plan` is clean
- [ ] Update `memory/open_tasks.md` with deployment notes
- [ ] If schema change: increment consumer group version

---

## 16. Go-Live Readiness Checklist (Supplemental)

The existing `docs/runbooks/go_live_checklist.md` is comprehensive. These items supplement it:

- [ ] BLOCKER-001 fixed and verified with concurrent fill unit test
- [ ] BLOCKER-002 fixed with paginated DynamoDB queries and unit test
- [ ] BLOCKER-003 resolved: `configs/risk_limits_production.yaml` created, reviewed, and added to preflight check
- [ ] HIGH-001 fixed: `_signal_locks` cleanup implemented
- [ ] HIGH-002 fixed: strategy engine kill switch uses `KillSwitchCache`
- [ ] HIGH-003 fixed: production mode rejects missing `InstrumentRegistry`
- [ ] MED-001 resolved: `reconcile_positions.py` and `refresh_zerodha_token.py` created
- [ ] At least 12 months of NSE backtest completed for all 6 strategies
- [ ] Backtest Sharpe ≥ 0.5 and drawdown ≤ 5% confirmed for strategies being promoted
- [ ] Grafana dashboard operational and visible during paper sessions
- [ ] DLQ growth CloudWatch alarm created and tested
- [ ] Strategy `quality_score` thresholds set per strategy in DynamoDB strategy-config (not 0)
- [ ] SIGTERM handler confirmed working in strategy engine (run `kill -TERM` on process, verify state persists)

---

## 17. Capital Scaling Checklist

Do not scale capital until these gates are satisfied:

| Gate | Metric | Threshold | Verification |
|---|---|---|---|
| Initial live | Sharpe (5 live days) | ≥ 0.5 | `paper_session_report.py` with `QE_ENVIRONMENT=production` |
| Scale to 25% | Sharpe (20 live days) | ≥ 1.0, drawdown ≤ 3% | Same report, 20-day window |
| Scale to 50% | Sharpe (60 live days) | ≥ 1.2, drawdown ≤ 2.5%, 0 kill switch events | Same report, 60-day window |
| Scale to 100% | Sharpe (120 live days) | ≥ 1.5, all preferred thresholds met | Independent review + audit |

Recommendation: Start at ₹10 lakh (10% of ₹1 crore) per promoted strategy. Never commit more than ₹25 lakh per strategy regardless of performance until 60-day live track record is established.

---

## 18. Incident Response Assessment

The runbook's emergency procedures (§4.1–§4.6) are thorough for the scenarios they cover. Two gaps:

**No model degradation procedure.** If the AI engine begins producing systematically incorrect regime classifications or quality scores (model drift, corrupted model artifact), there is no documented procedure. A "disable AI enrichment entirely" procedure (force fallback mode without relying on the watchdog) should be added. This would involve setting a DynamoDB flag that `EnrichmentWatchdog` reads to force `use_enriched=False` permanently.

**No Kafka consumer lag runbook.** If a consumer group falls significantly behind (e.g., strategy engine lags 10,000 ticks on `ticks.nse`), the correct response is unclear. Should the operator reset the offset to latest (skip lag, miss fills)? Wait for natural drain (but that may take hours)? Increase partition count (requires topic recreation)? A documented procedure for each consumer group is needed.

---

## 19. Future Roadmap Recommendations

In priority order:

**Phase 8 (immediate):** Alpaca feature store — without this, US enrichment is blind. Also implement the missing scripts from MED-001.

**Phase 9:** Backtesting framework — `scripts/backtest/run.py` with vectorised replay over S3 historical data. This is the most impactful missing component for strategy confidence.

**Phase 10:** Grafana operational dashboard — real-time intraday P&L, enrichment rate, fill rate, consumer-group lag across all topics.

**Phase 11:** DLQ reprocessing tool — a script to inspect, categorise, and selectively replay DLQ messages. Without this, DLQ messages are a black hole.

**Phase 12:** Multi-strategy correlation monitoring — if all 6 strategies are long simultaneously on the same sector, total exposure may exceed limits even if per-strategy position sizing is within bounds. The `SectorConcentrationValidator` addresses this at signal time, but a portfolio-level correlation monitor would provide earlier warning.

**Phase 13:** Paper trading automation — auto-increment the 5-day READY counter, auto-reset on NOT_READY days, generate a Slack/email report after each session. Remove the manual tracking burden.

---

## Summary Scorecard

| Area | Score | Notes |
|---|---|---|
| Layer separation | ✅ Excellent | Genuine structural enforcement |
| Order idempotency | ✅ Excellent | Three-case handling + transact_write |
| Risk engine design | ✅ Good | One critical defect (BLOCKER-001) |
| Kill switch coverage | ⚠️ Good | Strategy engine not using KillSwitchCache |
| P&L accuracy | ❌ Critical gap | Race condition under concurrent fills |
| Configuration completeness | ❌ Critical gap | Production risk limits file missing |
| Test coverage | ⚠️ Partial | Strong unit tests; backtests absent |
| Operational tooling | ⚠️ Partial | Good runbook; two scripts missing |
| Observability | ⚠️ Partial | Logging good; real-time dashboard absent |
| AWS cost optimisation | ✅ Good | EC2 ARM64, MSK Serverless, on-demand DynamoDB |
| Documentation | ✅ Good | CLAUDE.md, runbook, ADRs |
| Production readiness | ❌ Not ready | Fix 3 blockers, then re-evaluate |

**Overall verdict: Fix BLOCKER-001, BLOCKER-002, BLOCKER-003, then complete 5 consecutive READY paper days with a working Grafana dashboard before committing any real capital. The architecture is sound. The code quality is high. The gaps are specific and fixable.**

---

*Review conducted against codebase state as of Phase 7 completion (2026-05-11). This review covers architecture, code patterns, and operational readiness. It does not constitute financial advice. All trading involves risk of loss.*
