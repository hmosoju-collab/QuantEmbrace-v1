---
name: paper-trading-fixes
description: Critical paper trading bugs found and fixed during 5-day validation. Must preserve these fixes across rebuilds.
metadata:
  type: feedback
---

## Paper Trading Bug Fixes (Day 1–2, 2026-05-20/21)

### FIX-1: QE_PORTFOLIO_VALUE must be set to 5000000
**File**: `.env`
**Fix**: `QE_PORTFOLIO_VALUE=5000000`
**Why:** Default in `shared/config/settings.py` is `1_000_000`. `loss_validator.py` and `order_manager.py` both read `self._settings.portfolio_value` for opening_nav. Without this env var, every fill writes NAV=₹1M to DynamoDB, causing `position_validator` to reject all subsequent signals as "exceeds 5% of portfolio".
**How to apply:** Always ensure `.env` has `QE_PORTFOLIO_VALUE=5000000` before any paper session. This persists across container rebuilds.

### FIX-2: broker_timeout_secs=float("inf") in paper mode
**File**: `services/risk_engine/service.py` lines ~196-204
**Fix**:
```python
_is_paper = getattr(self._settings.risk, "profile", "paper") == "paper"
self._kill_switch_monitor = KillSwitchMonitor(
    kill_switch=self._kill_switch,
    settings=self._settings,
    broker_timeout_secs=float("inf") if _is_paper else 30.0,
)
```
**Why:** `record_broker_ping()` is only called after paper fills. With any finite timeout (even 3600s), the kill switch auto-activates N seconds after the last fill, halting trading. `float("inf")` means `elapsed > broker_timeout` is always False in paper mode.
**How to apply:** This is now in source code. If `auto_triggers.py` is changed, verify this property is preserved.

### FIX-3: startup_lookback_minutes=0 in DynamoCandleConsumer
**File**: `services/strategy_engine/service.py`
**Fix**: Pass `startup_lookback_minutes=0` to `DynamoCandleConsumer` constructor.
**Why:** Default startup_lookback=60 replays 60 minutes of historical candles at startup. vwap_reversion fires on every candle where price is outside band — with 19 symbols × 60 min backlog, the daily cap (max_signals_per_day=10) burns in seconds. All signal_ids are deterministic on candle_close_time so they're deduplicated, but the cap counter is still incremented.
**How to apply:** Do not change this back to 60. Strategies warm up their indicators from DynamoDB candle data; they don't need startup replay for warmup.

### FIX-4: kill_switch_cli.py uses wrong DynamoDB table
**Status**: UNFIXED in code (workaround documented)
**Why:** CLI generates table name `quantembrace-risk-state` but containers use `DYNAMODB_TABLE_PREFIX=quantembrace-development` → table is `quantembrace-development-risk-state`. CLI silently fails and reports "already inactive."
**Workaround**: Use direct `aws dynamodb update-item` with `--table-name quantembrace-development-risk-state --endpoint-url http://localhost:4566`:
```bash
aws dynamodb update-item \
  --table-name quantembrace-development-risk-state \
  --key '{"PK":{"S":"KILLSWITCH"},"SK":{"S":"GLOBAL"}}' \
  --update-expression "SET #a = :f, #s = :i, updated_at = :t, reason = :r" \
  --expression-attribute-names '{"#a":"active","#s":"status"}' \
  --expression-attribute-values '{":f":{"BOOL":false},":i":{"S":"INACTIVE"},":t":{"S":"2026-05-21T08:01:00+00:00"},":r":{"S":""}}' \
  --endpoint-url http://localhost:4566 --region ap-south-1
```

### FIX-6: _handle_paper_order never called apply_fill_to_position
**File**: `services/execution_engine/service.py` lines ~2067-2091
**Fix**: After Kafka publish in `_handle_paper_order`, added call to `self._order_manager.apply_fill_to_position(...)` when `filled_quantity > 0 and status != OrderStatus.REJECTED`.
**Why:** Paper fills wrote to the orders table and Kafka but skipped the positions table. `MISSquareOffManager._get_open_mis_positions()` scans the positions table — with 0 entries it always logged "no_positions" and never squared off. `orphan_detector` also reads from FILLED entry orders (not positions), but reconciliation and P&L tracking both depend on the positions table being current.
**How to apply:** Fix is already in `service.py`. On Day 3+, each paper fill will write to the positions table via `apply_fill_to_position`. MIS square-off at 15:05 IST will find and close positions. Realized P&L will be trackable.

### FIX-7: Kafka write failure kills switch auto-trigger disabled in non-prod
**File**: `services/data_ingestion/publishers/kafka_tick_publisher.py` — `_record_failure()` method
**Fix**: Added early return in non-production environments before the `_KILL_SWITCH_THRESHOLD` check:
```python
def _record_failure(self) -> None:
    self._consecutive_failures += 1
    # LocalStack instability causes spurious failures outside prod — skip auto-trigger
    if self._environment != "production":
        return
    if self._consecutive_failures >= _KILL_SWITCH_THRESHOLD and not self._kill_switch_fired:
        # ... thread to write DynamoDB kill switch
```
**Why:** `_KILL_SWITCH_THRESHOLD = 3` — only 3 Kafka delivery failures triggers a global kill switch. LocalStack DynamoDB write timeouts during post-reconnect burst cascaded into Kafka delivery failures, firing the kill switch before any real trading happened on Day 3. Same rationale as FIX-2 (`broker_timeout_secs=inf` in paper mode).
**How to apply:** Fix is in source. `self._environment` is passed at construction from `QE_ENVIRONMENT` env var (default "prod"). In containers, `QE_ENVIRONMENT=development` bypasses the threshold check. Production (`production`) retains full kill switch behavior.
**Daily token refresh note**: After refreshing Zerodha token, always use `docker compose up -d --force-recreate execution_engine data_ingestion` — `docker compose restart` does NOT reload `.env` env vars.

### FIX-5: loss_validator writes opening_nav=1M even after fills
**File**: `services/risk_engine/validators/loss_validator.py:595`
**Root cause**: `opening_nav = float(getattr(self._settings, "portfolio_value", 1_000_000.0))` — reads from settings, not from DynamoDB.
**Fix**: Set `QE_PORTFOLIO_VALUE=5000000` (FIX-1) so settings.portfolio_value=5M. Any fill-triggered NAV write will use 5M as opening_nav.

---

## Day 4 Fixes (2026-05-22) — Applied to main

### Asyncio event loop starvation (root cause of all Day 3/4 problems)
All fixes in `services/data_ingestion/publishers/kafka_tick_publisher.py` and related files:
- `_poll_loop`: `self._producer.poll(0)` → `await asyncio.to_thread(self._producer.poll, 0)` — delivery callbacks no longer block event loop
- BufferError path: `self._producer.poll(0.1)` → `await asyncio.sleep(0.15)` — yields to event loop
- `replay_pending_outbox`: 100 concurrent deletes → sequential `batch_write_item` (25/call) — prevents thread pool saturation
- `service.py`: `durable_outbox_enabled=False` for non-production — LocalStack can't handle ~60 DynamoDB writes/sec per-tick
- `zerodha_broker.py`: `self._kite.reqsession.timeout = (5, 12)` in `connect()` — prevents Python 3.11 asyncio `_cancel_and_wait` TCP hang (12s timeout instead of ~120s OS default)

Confirmed: loop_count=1→2 exactly 5.0s, loop_count=2→3 exactly 333ms.
Also: 72,893 stale outbox items purged from LocalStack. `durable_outbox_enabled=False` prevents recurrence in dev.

**LocalStack full reset**: Deleted corrupted `localstack-data` volume. `candle-cache` had wrong schema (manually created with pk+sk lowercase) — writes silently failed. Now correct (PK hash-only + GSI) via `setup_local_tables.py`.

---

## Paper Trading Session Summary

### Day 2 (2026-05-21) — COMPLETED
- **6 paper fills**: AXISBANK BUY, JSWSTEEL SELL, RELIANCE SELL, HINDUNILVR BUY, WIPRO BUY, MARUTI SELL
- **Total notional**: ₹587,595 (~2% per trade against ₹5M portfolio)
- **Both strategies fired**: nse_vwap_reversion + nse_scalp_1m
- **Signals.approved watermark**: 6 (1:1 with fills — 0 fill failures)
- **Kill switch activations**: 2 (both fixed with FIX-2)
- **MIS positions not auto-squared**: positions table empty; MISSquareOffManager scans positions table (has 0 entries), not orders table — paper mode limitation

### Day 3 (2026-05-22) — IN PROGRESS
- **Token refreshed**: `80IO1uexxAnrXpRZeq7VaW7baPJ6b6P3` valid until 2026-05-23T02:00 UTC
- **FIX-7 deployed**: kafka_write_failure kill switch bypass in non-prod
- **Both execution_engine + data_ingestion force-recreated** with new token
- **Stale outbox entries cleared**: ~892 DynamoDB outbox items deleted from previous sessions
- **Kill switch**: INACTIVE at market open
- **Verification targets**: FIX-6 (apply_fill_to_position writes positions table), MIS square-off at 15:05 IST

### Remaining Days: 2/5 paper trading days to go after Day 3

---

## Day 6 Comprehensive Readiness Review (2026-05-26/27)

### ROOT CAUSE CONFIRMED: Days 1-4 Zero Trades

**Signal age mismatch** — `strategy_engine` stamps `Signal.generated_at = candle.candle_close_time`. Candle at risk_engine is 7-12s old. Old default `RISK_MAX_SIGNAL_AGE_SECONDS=5.0` → 100% rejection.
**Fix**: `RISK_MAX_SIGNAL_AGE_SECONDS: "30"` in docker-compose `risk_engine` environment block. Hard ceiling `_ABSOLUTE_MAX_AGE_SECONDS = 30.0` in `SignalAgeValidator`.
**File**: `docker-compose.yml` (risk_engine env block)
**Test**: `tests/unit/test_signal_age_candle.py` (8 tests, all passing)

### FIX-A: Paper Duplicate Position Prevention

**File**: `services/execution_engine/service.py` — `_handle_paper_order`
**Problem**: `await self._order_manager.submit_order(paper_req)` return value not checked. If conditional write lost a race (concurrent duplicate signal), code fell through to `record_order` + `apply_fill_to_position`, double-counting the position.
**Fix**: `submitted = await self._order_manager.submit_order(paper_req); if not submitted: logger.warning(...); return`
**How to apply**: Fix is in source. Any future changes to `_handle_paper_order` must preserve this check.

### FIX-B: Daily Universe Snapshot Refresh

**File**: `services/execution_engine/service.py`
**Problem**: Universe validator built once at startup, never refreshed. After midnight IST, every NSE order logs "stale_snapshot_paper_allowed" but filtering is effectively disabled for day 2+ of multi-day sessions.
**Fix**: Added `_universe_snapshot_refresh_loop` background task (task 12 in asyncio.gather). Checks every 60s whether snapshot date matches today (IST). Rebuilds if stale. `self._universe_mode_str` stored as instance var for the loop.
**How to apply**: Fix is in source. Mode is read from `UNIVERSE_MODE` env var at startup and preserved for refresh.

### FIX-C: NAV Seed Alignment

**File**: `docker-compose.yml` (setup service environment block)
**Problem**: `PAPER_SEED_NAV` not set → `setup_local_tables.py` defaults to ₹5,000,000. `risk_limits_production.yaml` uses `portfolio_value: 1,000,000`. 5x mismatch → margin checks over-permissive.
**Fix**: `PAPER_SEED_NAV: "1000000"` added to docker-compose setup service env.
**How to apply**: `docker-compose down -v && docker-compose run --rm setup` required to pick up new NAV. Do NOT restart only — volumes must be wiped.

### FIX-D: Strategy Config Seeding (Unlimited Signals)

**File**: `scripts/setup_local_tables.py`
**Problem**: Strategy configs never seeded in `setup_local_tables.py`. `StrategyConfigLoader._DEFAULT_CONFIG` falls back to `max_signals_per_day=10`. With 6 strategies × 10 = 60 total signal ceiling per session, silently.
**Fix**: Added `_seed_strategy_configs()` to `main()`. Seeds all 6 strategies (`nse_momentum_v1`, `nse_orb_15m`, `nse_scalp_1m`, `nse_vwap_reversion`, `nse_intraday_trend_15m`, `nse_preclose_momentum`) with `max_signals_per_day=0` (unlimited), `paper_trade=True`, `enabled=True`.
**How to apply**: Requires `docker-compose down -v && docker-compose run --rm setup` to take effect.

### FIX-E: RiskDecision Enriched Field

**File**: `services/risk_engine/service.py`
**Problem**: `RiskDecision.to_dict()` did not include `enriched` field. Session report `_fetch_signal_metrics` always reported `total_enriched=0`, enrichment rate = 0%.
**Fix**: Added `enriched: bool = False` field to `RiskDecision` dataclass. Added `"enriched": self.enriched` to `to_dict()`. Set `decision.enriched = True` in `_enriched_processing_loop` after `validate_signal` returns.
**How to apply**: Fix is in source. Existing DynamoDB records (pre-fix) will read as `enriched=False` — one session's worth of conservative undercount is expected.

### FIX-F: Session Report Enrichment Detection

**File**: `scripts/monitoring/paper_session_report.py`
**Problem**: `_fetch_signal_metrics` checked `any("enriched" in n for n in vr_names)` where `vr_names` are validator names from `validator_results`. No validator has "enriched" in its name. Always returned 0 enriched.
**Fix**: Replaced with `blob.get("enriched", False)` — reads the explicit field added in FIX-E. Old records (pre-FIX-E) are conservatively treated as not enriched.
**How to apply**: Fix is in source.

### PREFLIGHT CHECK SCRIPT ADDED

**File**: `scripts/deploy/paper_preflight_check.py` (NEW)
**Purpose**: Local paper trading readiness check. Run before every session.
**Checks**: env vars, RISK_PROFILE=paper, broker credential safety (HARD RULE enforcement), DynamoDB connectivity+tables, NAV seeded, kill switch inactive, S3 connectivity, Kafka connectivity+topics.
**Usage**: `python scripts/deploy/paper_preflight_check.py` — exit 0 = safe, exit 1 = FAIL.
**IMPORTANT**: Checks `ZERODHA_API_KEY`/`ZERODHA_API_SECRET`/`ALPACA_API_KEY`/`ALPACA_API_SECRET` and fails if they appear to be real credentials while `paper_trade=True`. HARD RULE enforcement.

---

## Day 7 Fixes (2026-05-27)

### FIX-8: Candle stream task dies silently — no watchdog

**File**: `services/data_ingestion/service.py`

**Root cause**: `IntradayCandleStream._stream_loop` catches `except Exception` but Python 3.8+ `CancelledError` is a `BaseException`, not `Exception`. When the Zerodha WebSocket dropped at 04:48:28 UTC (code=1006), an `asyncio.CancelledError` was raised inside a pending `asyncio.wait_for` or `asyncio.sleep` call inside `_stream_loop`. This bypassed the exception handler and killed the task permanently. No watchdog existed to detect or restart it. The 9-minute gap (04:38–04:48) was the `asyncio.to_thread` call stuck in thread pool, followed by a burst of 15 `ConnectionRefusedError` errors and then total silence.

**Symptom**: `candle_stream.fetch_error: ConnectionRefusedError [Errno 111]` burst at 04:48, then 0 candles written to DynamoDB for the rest of the session. `docker logs data_ingestion | grep candle_stream` showed no activity after 04:48. `candle-cache` table had entries only up to 04:38.

**Fix**: Added `_candle_stream_watchdog()` coroutine and `_candle_stream_watchdog_task` instance variable to `DataIngestionService`. The watchdog polls every 30 seconds. If `_candle_stream_task.done()` returns `True` (cancelled or raised), it logs CRITICAL and immediately creates a new task. Also added cancellation of `_candle_stream_watchdog_task` in `stop()` BEFORE stopping the candle stream itself (order matters — prevents watchdog from restarting a stream that's being intentionally stopped).

**Code location**: `services/data_ingestion/service.py` — `_candle_stream_watchdog()` method, `_setup_feature_pipeline()` task creation, `stop()` teardown sequence.

**How to apply**: Any time `_candle_stream_task` is created, also create `_candle_stream_watchdog_task`. In `stop()`, always cancel the watchdog before stopping the stream. Verify after restart: `docker logs data_ingestion | grep candle_stream_watchdog` should show `.restarted` if ever a task dies.

**Verification**: After rebuild, `candle_stream.started` + `candle_stream.phase_changed phase=NORMAL` logged within 30s. 156 candles appeared in DynamoDB within 3 minutes.

---

### FIX-9: strategy_config_loader.py DynamoDB key prefix mismatch — always falls back to cap=10

**File**: `services/strategy_engine/config/strategy_config_loader.py`

**Root cause**: `_PK_PREFIX = "STRATEGY#"` and `_SK_PREFIX = "CONFIG#"` did NOT match the items written by `setup_local_tables.py`, which uses `PK = "STRATEGY_CONFIG#<name>"` and `SK = "ENV#<env>"`. Every `get_item` call returned `None`. The loader fell back to `_DEFAULT_CONFIG(max_signals_per_day=10)`. After 10 early signals fired around 04:27 UTC, the daily cap was hit and ALL subsequent signals from that strategy were blocked for the rest of the session.

**Symptom**: `strategy_runner.daily_cap_reached cap=10 signals_today=10` logged for every VWAP signal. DynamoDB strategy-config table scan confirmed `PK="STRATEGY_CONFIG#nse_vwap_reversion"` / `SK="ENV#development"` / `max_signals_per_day="0"` — correct values in DB, but loader never found them due to wrong keys.

**Fix**:
```python
# BEFORE (wrong):
_PK_PREFIX = "STRATEGY#"
_SK_PREFIX = "CONFIG#"

# AFTER (correct — matches setup_local_tables.py schema):
_PK_PREFIX = "STRATEGY_CONFIG#"
_SK_PREFIX = "ENV#"
```

**How to apply**: If you ever add a new setup script or change the DynamoDB schema for strategy configs, verify `_PK_PREFIX` and `_SK_PREFIX` in `strategy_config_loader.py` match EXACTLY what the setup script writes. Run `aws dynamodb scan --table-name quantembrace-development-strategy-config --endpoint-url http://localhost:4566 | jq '.Items[] | {PK, SK}'` to confirm key format.

**Verification**: After rebuild: `strategy_config_loader.refresh_complete updated=7 errors=0`. No more `daily_cap_reached` entries in logs. Strategy resumes emitting signals after the config fix.

---

## Day 7 Session Summary (2026-05-27)

- **First paper fill confirmed**: `nse_vwap_reversion BUY JINDALSAW qty=429 @ ₹233.0328`, notional ₹99,971 at 05:31 UTC
- **Pipeline operational end-to-end** after three sequential fixes: FIX-8 (candle stream watchdog), FIX-9 (strategy config key fix), FIX-10 (kill switch startup grace + early record_data_tick — see kill_switch_bugs.md Bug 3/4)
- **Candles flowing**: 156 candles in 3 minutes post-rebuild; candle-cache actively written
- **Kill switch**: `active=False` through end of observed session
- **Strategy config**: `refresh_complete updated=7 errors=0` confirmed all strategies reading unlimited signals from DynamoDB
- **Note**: `us_momentum_v1` has no DynamoDB config row → uses default cap=10; non-blocking (US markets closed during IST hours). Alpaca connector `'str' object has no attribute 'value'` error — non-blocking.

---

## Day 7 Post-Session EOD Fixes (2026-05-27)

### FIX-11: exit_order_id stale lock permanently blocks TEE on re-entered positions

**File**: `services/execution_engine/orders/order_manager.py` — `apply_fill_to_position()`

**Root cause**: UpdateExpression for position opens/builds did not include `REMOVE exit_order_id, exit_trigger, exit_state`. After an exit set `exit_order_id` and the position was flattened (`direction="FLAT"`), these fields persisted in DynamoDB. Any new entry fill on the same symbol preserved the stale lock. TradeExitEngine skips any position where `exit_order_id is not None` at its "exit already in-flight" guard — so re-entered positions were permanently invisible to TEE.

**Fix**: Appended `REMOVE exit_order_id, exit_trigger, exit_state` to UpdateExpression when `direction != "FLAT"`. FLAT writes (exits) intentionally leave the field in place — it correctly describes the in-progress exit.

**How to apply**: Fix is in source. If `apply_fill_to_position` is refactored, ensure FLAT vs non-FLAT distinction is preserved in the UpdateExpression.

---

### FIX-12: MIS square-off calls Zerodha directly in paper mode — always fails (IP not whitelisted)

**File**: `services/execution_engine/mis_square_off.py`

**Root cause**: `_place_mis_close_order` had no paper mode path — it called `self._zerodha.place_order()` unconditionally. In paper mode, Zerodha rejects all orders with `PermissionException: IP not allowed` (IP not whitelisted for paper-only accounts). MIS ran at 09:35 UTC, found 13 open positions, attempted 13 Zerodha orders — all failed. Deadline exceeded → kill switch activated. This was the second kill switch event in the session (first was PositionMonitor Bug 2 at 06:25 UTC).

**Fix**:
1. Added `paper_trading: bool = True` param to `MISSquareOffManager.__init__`, stored as `self._paper_trading`.
2. Added `last_price` to `_get_open_mis_positions` ProjectionExpression and `_resolve_position` return dict.
3. In `_place_mis_close_order`: when `self._paper_trading`, call `self._order_manager.apply_fill_to_position(fill_price=last_price or avg_entry_price)` directly instead of Zerodha. Returns a simulated `order_id`. Real Zerodha path unchanged.
4. Wired `paper_trading=getattr(self._settings.execution, "paper_trading", True)` into MIS constructor in `service.py`.

**How to apply**: Fix is in source. `EXECUTION_PAPER_TRADING` env var (not set → defaults True) controls the gate. Never call a live broker API from any sub-system without first checking a `_paper_trading` or `_is_paper` guard.

---

### FIX-13: MIS fires immediately on container restart after market close → false kill switch

**File**: `services/execution_engine/mis_square_off.py`

**Root cause**: `_seconds_until_ist(target)` returns `max(0.0, delta)` — when time is past the target, returns `0.0` and MIS fires in the next event loop tick. On any container restart after 15:10 IST (e.g., post-market debugging), MIS ran `_execute_mis_square_off()` immediately, found positions open, exceeded the deadline, and activated the kill switch. This caused kill switch to re-activate on EVERY container restart after market close.

**Fix**: Past-deadline skip guard added at the top of the `run()` loop. When `_seconds_until_ist(CLOSE_TIME) == 0.0` AND `_seconds_until_ist(DEADLINE_TIME) == 0.0`, logs `mis_square_off.skipped_past_deadline` and sleeps ~86400s until the next trading day. Does NOT activate kill switch.

**Verification**: Container restart after 15:10 IST now logs `skipped_past_deadline`, clean startup, no kill switch.

**How to apply**: Fix is in source. Any service with time-gated execution that can be restarted after the deadline must check deadline status at the top of its scheduling loop before attempting execution.

---

## Day 8 Fixes (2026-05-28)

### FIX-14: ZerodhaBrokerClient created without dynamo_client → always falls back to stale env var token

**Files**:
- `services/execution_engine/service.py` (line ~271)
- `services/data_ingestion/service.py` (line ~519)

**Root cause**: Both services instantiated `ZerodhaBrokerClient(settings=self._settings)` without passing `dynamo_client`. Inside `ZerodhaTokenManager._load_token_from_dynamo()`, the first guard is `if self._dynamo is None: return None, None`. With no DynamoDB client injected, this returned `(None, None)` immediately — never touching LocalStack. `get_valid_token()` then raised `TokenExpiredError("No Zerodha access token found in DynamoDB")`, and `zerodha_broker.connect()` fell back to the `ZERODHA_ACCESS_TOKEN` env var which was stale/expired. Result: `kiteconnect.exceptions.TokenException: Incorrect api_key or access_token` on every REST and historical data call.

**Symptoms**: candle_stream watchdog restart loops, all `get_historical_candles` calls failing with `TokenException`, `live_quote_poller.cycle_error` repeatedly, `candle_stream.empty_response` only after fix (correct behavior, no crash).

**Fix**:
1. In `execution_engine/service.py`: moved `dynamo = get_dynamodb_client()` and `self._dynamo = dynamo` BEFORE `ZerodhaBrokerClient(...)` instantiation. Passed `dynamo_client=dynamo` to the constructor. Connect sequence now reads token from DynamoDB at startup.
2. In `data_ingestion/service.py`: passed `dynamo_client=dynamo_client` (boto3 client already created at line 453) to `ZerodhaBrokerClient(...)`. Same fix.

**Verification**: After rebuild, execution_engine logs `"Zerodha authenticated via DynamoDB token"` instead of `"Zerodha authenticated via ZERODHA_ACCESS_TOKEN env var"`. No `TokenException` in either service.

**How to apply**: Fix is in source. Any new broker client that reads tokens from DynamoDB must receive `dynamo_client` at construction — never instantiate broker clients before the DynamoDB client is created.

---

### FIX-15: ZerodhaConnector WebSocket uses stale env var token — now reads from DynamoDB

**File**: `services/data_ingestion/service.py` (step 4.5 added before connector construction)

**Root cause**: `ZerodhaConnector` receives `access_token` as a constructor string parameter at service.py line 200 via `self._settings.zerodha.access_token.get_secret_value()`. This reads `ZERODHA_ACCESS_TOKEN` from the env var — which is set once at container build/start time. After the daily `zerodha_login.py` run stores a fresh token in DynamoDB, the env var is NOT updated. Result: WebSocket KiteTicker authenticates with the stale token → `403 Forbidden` on every connection attempt → no live ticks → `ticks.nse` Kafka topic empty → tick-based strategies starved.

**Fix**: Added step 4.5 in `start()` to resolve the access token from DynamoDB via `ZerodhaTokenManager` before `ZerodhaConnector` is constructed. Lazy-imports `ZerodhaTokenManager` from `execution_engine.auth.zerodha_auth` and `get_dynamodb_client` from `shared.aws.clients`. Falls back to the env var if the DynamoDB lookup fails (DynamoDB unavailable, no record, expired). Logs `zerodha_connector.token_loaded_from_dynamodb` on success or `zerodha_connector.token_fallback_env_var` on fallback.

**Verification**: After rebuild, startup logs show `"zerodha_connector.token_loaded_from_dynamodb"` followed by `"Zerodha WebSocket connected"` (200 upgrade) instead of `403 Forbidden`.

**How to apply**: Fix is in source. `ZerodhaConnector` still accepts `access_token` in its constructor — the resolution now happens at call site (service.py), not inside the connector. No connector API change needed.
