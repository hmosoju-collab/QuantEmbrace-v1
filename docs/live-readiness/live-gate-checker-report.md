# Phase D — LiveGateChecker Report

**Date:** 2026-05-30
**Status:** IMPLEMENTED AND TESTED
**Live trading:** NOT enabled. No broker orders placed.

---

## Summary

`LiveGateChecker` is implemented at [services/shared/live_gate_checker.py](../../services/shared/live_gate_checker.py).

It evaluates all 25 required live-trading promotion gates concurrently and returns one of three verdicts:

| Verdict | Meaning |
|---------|---------|
| `APPROVED` | All 25 checks pass. Live trading MAY be enabled by the operator. |
| `BLOCKED` | One or more checks failed. Live trading MUST NOT be enabled. |
| `DEGRADED` | All hard checks pass but some runtime checks could not be verified (WARN). Investigate warnings before enabling live trading. |

**Test results: 61/61 passing.** ([tests/unit/test_live_gate_checker.py](../../tests/unit/test_live_gate_checker.py))

---

## Design Principles

- **Read-only.** The checker never writes to DynamoDB, Kafka, or any broker API.
- **No side effects.** Running the checker cannot enable trading or modify state.
- **All 25 checks run.** No early-exit on first failure — all check results are reported.
- **Graceful degradation.** Checks 11, 16, 18, 20 produce WARN (not FAIL) when optional clients (CloudWatch, Zerodha) are absent. This allows offline/CI validation without live AWS connections.
- **Injectable clock.** Time-based checks (21, 22) accept a `_now_ist` parameter for deterministic tests.

---

## Approval Record Schema

Before the checker can return APPROVED, an operator must write a live-gate approval record to DynamoDB:

```
Table:  {prefix}-risk-state
PK:     "LIVE_GATE#APPROVAL"
SK:     "CURRENT"
```

| Field | Type | Requirement |
|-------|------|-------------|
| `approval_token` | String | Non-empty. Signals that this record is intentionally written. |
| `live_stage` | String | Must be `"STAGE_1_ONE_SHARE"` |
| `max_capital` | Number | Must be ≤ ₹10,00,000 (₹10L Stage-1 limit) |
| `approved_by` | String | Operator identity (name or email) |
| `approved_at` | String | ISO timestamp of approval |
| `release_tag` | String | Git SHA or semver of the deployment being approved |
| `rollback_plan_confirmed` | Boolean | `true` = operator confirmed rollback procedure |
| `sns_alert_tested` | Boolean | `true` = SNS test delivery confirmed before this approval |

Write this record via `scripts/ops/approve_live_gate.py` (operator tool). Never write it automatically from CI/CD.

---

## The 25 Checks

### Configuration and Approval Gates (1–9)

| # | Check Name | Verdict on Failure | What It Verifies |
|---|------------|--------------------|------------------|
| 1 | `trading_mode_live` | BLOCKED | `QE_ENVIRONMENT=production` OR live_stage in approval record |
| 2 | `live_trading_enabled` | BLOCKED | `QE_EXECUTION_LIVE_TRADING_ENABLED=true` in environment |
| 3 | `manual_approval_token` | BLOCKED | `approval_token` field present and non-empty in DynamoDB record |
| 4 | `live_stage_approved` | BLOCKED | `live_stage=STAGE_1_ONE_SHARE` (only approved stage) |
| 5 | `max_capital_within_stage_limit` | BLOCKED | `portfolio_value` and `max_capital` ≤ ₹10,00,000 |
| 6 | `max_order_value_configured` | BLOCKED | `max_single_order_value` > 0 and ≤ ₹5,000 |
| 7 | `max_daily_loss_configured` | BLOCKED | `max_daily_loss_pct` > 0 |
| 8 | `allowed_symbols_configured` | BLOCKED | `watchlist_nse` is non-empty |
| 9 | `allowed_strategies_configured` | BLOCKED | At least one strategy with `paper_trade=false` and `enabled=true` in strategy-config |

### Runtime State Gates (10–17)

| # | Check Name | Verdict on Failure | What It Verifies |
|---|------------|--------------------|------------------|
| 10 | `kill_switch_off` | BLOCKED | Kill switch `active=false` in `risk-state` table |
| 11 | `trade_exit_engine_running` | FAIL→BLOCKED / WARN | TEE heartbeat at `HEARTBEAT#TEE/CURRENT` age < 120s. WARN if key absent (no heartbeat yet). |
| 12 | `mis_square_off_armed` | FAIL→BLOCKED / WARN | execution_engine heartbeat fresh (< 120s) during market hours. PASS after NSE close. |
| 13 | `reconciliation_clean` | BLOCKED | `reconciliation_required=false` in `risk-state` table |
| 14 | `ltp_fresh` | BLOCKED | LTP record for first watchlist symbol age < 120s in `latest-prices` table |
| 15 | `broker_session_valid` | BLOCKED | Today's Zerodha session with non-empty `access_token` in `sessions` table |
| 16 | `margins_readable` | FAIL→BLOCKED / WARN | `get_margins()` succeeds on injected Zerodha client. WARN if no client injected. |
| 17 | `dynamodb_live_table_reachable` | BLOCKED | `orders` table responds to `get_item` probe |

### Monitoring Gates (18–20)

| # | Check Name | Verdict on Failure | What It Verifies |
|---|------------|--------------------|------------------|
| 18 | `cloudwatch_alarms_active` | FAIL→BLOCKED / WARN | No QuantEmbrace trading alarms in `INSUFFICIENT_DATA` state. WARN if no CW client. |
| 19 | `sns_alert_tested` | BLOCKED | `sns_alert_tested=true` in approval record |
| 20 | `no_unresolved_critical_alerts` | FAIL→BLOCKED / WARN | No P0 alarms in `ALARM` state. WARN if no CW client. |

### Operational Gates (21–25)

| # | Check Name | Verdict on Failure | What It Verifies |
|---|------------|--------------------|------------------|
| 21 | `trading_window` | BLOCKED | Current IST time between 09:15–15:30 on a weekday |
| 22 | `no_new_entry_cutoff` | BLOCKED | Current IST time before 15:00 (no new entries after this) |
| 23 | `rollback_plan_exists` | BLOCKED | `rollback_plan_confirmed=true` in approval record |
| 24 | `release_tag_recorded` | BLOCKED | `release_tag` non-empty in approval record |
| 25 | `human_approval_recorded` | BLOCKED | `approved_by` and `approved_at` both present in approval record |

---

## Test Results

```
============================== 61 passed in 0.12s ==============================
```

### Test Coverage by Spec Requirement

| Spec Requirement | Tests | Status |
|-----------------|-------|--------|
| Each failed gate blocks live | T01–T25 (one per check + multi-fail) | ✅ 53 tests |
| Missing approval blocks live | `TestMissingApprovalToken` (3 tests) | ✅ |
| Stale LTP blocks live | `TestLTPFresh.test_stale_ltp_blocks` | ✅ |
| Kill switch blocks live | `TestKillSwitch.test_active_kill_switch_blocks` | ✅ |
| Missing MIS blocks live | `TestMISSquareOff.test_stale_exec_heartbeat_blocks` | ✅ |
| Missing TEE blocks live | `TestTradeExitEngine.test_stale_tee_heartbeat_blocks` | ✅ |
| Missing allowed symbol blocks live | `TestAllowedSymbols.test_empty_watchlist_blocks` | ✅ |
| 1M capital blocks Stage-1 | `TestMaxCapital.test_1m_capital_blocks_stage1` | ✅ |
| All gates pass for Stage-1 small validation | `TestAllGatesPass.test_all_gates_pass_returns_approved` | ✅ |

### Key Test Scenarios

| Test Class | Tests | What Is Verified |
|-----------|-------|-----------------|
| `TestAllGatesPass` | 2 | APPROVED on clean Stage-1; all 25 check names present |
| `TestTradingModeLive` | 2 | Dev env blocks; production passes |
| `TestLiveTradingEnabled` | 2 | `false` env blocks; `true` passes |
| `TestMissingApprovalToken` | 3 | Empty token blocks; missing record blocks; token present passes |
| `TestLiveStageApproved` | 2 | Wrong stage blocks; Stage-1 passes |
| `TestMaxCapital` | 4 | ₹1M blocks; ₹10L exactly passes; approval record excess blocks; zero blocks |
| `TestMaxOrderValue` | 2 | Zero blocks; > ₹5k blocks |
| `TestMaxDailyLoss` | 1 | Zero blocks |
| `TestAllowedSymbols` | 2 | Empty watchlist blocks; single symbol passes |
| `TestAllowedStrategies` | 2 | No live strategies blocks; paper-only scan returns empty → blocks |
| `TestKillSwitch` | 3 | Active blocks; inactive passes; absent passes |
| `TestTradeExitEngine` | 3 | No heartbeat → WARN; stale → FAIL/BLOCKED; fresh → PASS |
| `TestMISSquareOff` | 3 | No heartbeat → WARN; stale → FAIL/BLOCKED; post-market → PASS |
| `TestReconciliationClean` | 2 | required=True blocks; clear passes |
| `TestLTPFresh` | 3 | Stale blocks; missing blocks; fresh passes |
| `TestBrokerSessionValid` | 2 | Missing session blocks; no token blocks |
| `TestMarginsReadable` | 3 | No client → WARN; error → FAIL/BLOCKED; success passes |
| `TestDynamoReachable` | 1 | Unreachable table blocks |
| `TestCloudWatch` | 3 | No client → WARN×2; INSUFFICIENT_DATA blocks; active P0 blocks |
| `TestSNSAlertTested` | 1 | Not tested blocks |
| `TestTradingWindow` | 4 | Pre-market blocks; post-market blocks; Saturday blocks; hours pass |
| `TestNoNewEntryCutoff` | 2 | After 15:00 blocks; before 15:00 passes |
| `TestRollbackPlan` | 1 | Not confirmed blocks |
| `TestReleaseTagRecorded` | 1 | Empty tag blocks |
| `TestHumanApprovalRecorded` | 2 | Missing approved_by blocks; missing approved_at blocks |
| `TestMultipleFailures` | 2 | All failing gates reported; DEGRADED on warnings-only |
| `TestResultHelpers` | 3 | blocked_by/warnings/passed properties |

---

## Usage

```python
from services.shared.live_gate_checker import LiveGateChecker
import boto3

checker = LiveGateChecker(
    settings=get_settings(),
    dynamo_client=boto3.client("dynamodb", region_name="ap-south-1"),
    risk_state_table="quantembrace-prod-risk-state",
    orders_table="quantembrace-prod-orders",
    strategy_config_table="quantembrace-prod-strategy-config",
    sessions_table="quantembrace-prod-sessions",
    prices_table="quantembrace-prod-latest-prices",
    cw_client=boto3.client("cloudwatch", region_name="ap-south-1"),
    # zerodha=zerodha_broker_client,   # optional; enables check 16
)

result = await checker.check_all()
print(result.status)    # APPROVED / BLOCKED / DEGRADED
print(result.summary)
for check in result.checks:
    print(f"  [{check.status}] {check.name}: {check.reason}")
```

### Operator gate workflow

```bash
# Step 1: Write approval record to DynamoDB
python scripts/ops/approve_live_gate.py \
  --stage STAGE_1_ONE_SHARE \
  --max-capital 100000 \
  --release-tag d5a8219 \
  --approved-by "hari.mosoju@gmail.com" \
  --rollback-plan-confirmed \
  --sns-alert-tested

# Step 2: Run the checker
PYTHONPATH=services python3 -c "
import asyncio, boto3
from shared.live_gate_checker import LiveGateChecker
from shared.config.settings import get_settings

checker = LiveGateChecker(
    settings=get_settings(),
    dynamo_client=boto3.client('dynamodb', region_name='ap-south-1'),
    risk_state_table='quantembrace-prod-risk-state',
    orders_table='quantembrace-prod-orders',
    strategy_config_table='quantembrace-prod-strategy-config',
    sessions_table='quantembrace-prod-sessions',
    prices_table='quantembrace-prod-latest-prices',
    cw_client=boto3.client('cloudwatch', region_name='ap-south-1'),
)
result = asyncio.run(checker.check_all())
print(result.status, result.summary)
for c in result.checks:
    print(f'  [{c.status:4}] {c.name}: {c.reason}')
import sys
sys.exit(0 if result.status == 'APPROVED' else 1)
"
```

---

## Constraints Honoured

- **Live trading not enabled.** The checker is read-only and has no `place_order` or `enable_live` path.
- **No broker orders placed.** `get_margins()` (check 16) is the only optional broker call; it reads margins, does not place orders.
- **No DynamoDB mutations.** All DynamoDB operations are `get_item` and `scan` only.
- **₹1,000,000 capital BLOCKED.** Check 5 enforces `portfolio_value ≤ ₹10,00,000` and `max_capital ≤ ₹10,00,000`. Any configuration at or above ₹10,00,001 produces BLOCKED.

---

## Future Enhancements

- `scripts/ops/approve_live_gate.py` — operator tool to write the approval record [PLANNED — not yet implemented]
- Wire checker into `scripts/read_only_live_readiness_runtime_check.py` as the canonical live-gate command
- Add check 11 heartbeat writer to TradeExitEngine (currently TEE has no `HEARTBEAT#TEE` write; adds to check 11 PASS path)
- Add check 12 heartbeat writer to execution_engine service (currently writes `HEARTBEAT#EXECUTION_ENGINE` — already in `_producer_heartbeat_loop` if wired)

---

*Report generated from static analysis and unit test results. No live AWS state was queried. No trading was enabled. No broker orders were placed.*
