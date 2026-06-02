# Phase 6 — Production Observability, Persistent Idempotency & Live-Readiness Hardening

_Date: 2026-05-31 · Author: Chief Architect · Status: COMPLETE — all tests passing_

---

## 1. Scope

Phase 6 closes the remaining Phase 5 risks:

| Phase 5 Risk | Phase 6 Resolution |
|---|---|
| Entry block cache TTL is 5s (still valid) | Accepted. 5s TTL is appropriate for the paper session signal rate. |
| Safe-action idempotency in-memory only — resets on restart | **Fixed:** DurableIdempotencyStore (DynamoDB) |
| CloudWatch/Prometheus counters not fully published | **Fixed:** SafeActionMetrics wired into executor |
| ENTRY_BLOCK reader not present in risk_engine | **Fixed:** EntryBlockValidator (defense-in-depth) |
| Host-side runtime verification required | **Unblocked:** Phase 6 adds verification commands below |

---

## 2. Idempotency Design

### 2.1 DurableIdempotencyStore

**File:** `services/execution_engine/safe_actions/safe_action_idempotency_store.py`

**DynamoDB schema:**
```
Table: <prefix>-risk-state
PK:  SAFE_ACTION_IDEMPOTENCY
SK:  <idempotency_key>          (e.g. "block_new_entries-stale_ltp-20260531")

Attributes:
  action_type   S   ActionType enum value
  mode          S   TradingMode enum value
  status        S   "executed"
  action_id     S   UUID4 (links to audit log)
  created_at    S   ISO-8601 UTC
  result        S   JSON result_payload (≤256 chars)
  audit_ref     S   Same as action_id
```

**Conditional write:** `attribute_not_exists(SK)` prevents duplicate execution under concurrent restart races.

**Applied to:** `BLOCK_NEW_ENTRIES`, `ACTIVATE_KILL_SWITCH` (all current write actions).

**Restart survival:** A new executor instance reads from the same DynamoDB table. If the key exists, execution is skipped and an audit record is written with `idempotency_skipped=True`.

**Read failure behaviour:** If DynamoDB is unavailable during `check()`, the store returns `(False, None)` — execution is attempted. The primary safety invariant is the DynamoDB write for the action itself (which will also fail if DynamoDB is unavailable).

**Key retention:** Keys are never automatically deleted. Manual cleanup (if ever needed):
```bash
aws dynamodb delete-item \
  --table-name <prefix>-risk-state \
  --key '{"PK":{"S":"SAFE_ACTION_IDEMPOTENCY"},"SK":{"S":"<idempotency_key>"}}'
```

### 2.2 Executor Wire-up

`SafeActionExecutor.__init__` now accepts:
- `durable_idempotency: Optional[DurableIdempotencyStore]`
- `metrics: Optional[SafeActionMetrics]`

Check order for write actions:
```
1. Policy gate
2. Human-approval gate
3a. Durable idempotency (DynamoDB) — write actions only
3b. In-memory idempotency (within-session fast path)
4. Precondition gate
5. Dispatch (execute)
6. Mark durable store after success
7. Audit (fail-closed for write actions)
```

---

## 3. Metrics Design

### 3.1 SafeActionMetrics

**File:** `services/execution_engine/safe_actions/safe_action_metrics.py`

All metrics use the pattern:
```python
metrics.cw_client.record_count(metric_name, value=1.0, dimensions={...})
```

| Metric | Dimensions | When emitted |
|---|---|---|
| `safe_actions.executed_total` | ActionType, Mode | Successful dispatch |
| `safe_actions.blocked_total` | ActionType, Reason | Policy/precondition/human block |
| `safe_actions.forbidden_total` | ActionType | FORBIDDEN action attempted |
| `safe_actions.idempotency_skip_total` | ActionType, Source (memory\|dynamo) | Skip recorded |
| `safe_actions.dynamo_write_failed_total` | ActionType | DynamoDB write failure |
| `safe_actions.block_new_entries_total` | — | BLOCK_NEW_ENTRIES succeeded |
| `safe_actions.kill_switch_activated_total` | — | ACTIVATE_KILL_SWITCH succeeded |
| `safe_actions.kill_switch_write_failed_total` | — | ACTIVATE_KILL_SWITCH DynamoDB failed |
| `strategy.entry_block_active` | — | Gauge: 1/0 |
| `strategy.entry_blocked_total` | Strategy | Entry blocked by strategy_engine |
| `strategy.entry_block_read_failure_total` | — | DynamoDB read failure |
| `strategy.entry_block_cache_hit_total` | — | Cache hit |
| `strategy.entry_block_cache_miss_total` | — | Fresh DynamoDB read |
| `monitoring.safe_actions_executed_total` | — | monitoring_agent cycle executed action |
| `monitoring.safe_actions_blocked_total` | — | monitoring_agent cycle blocked action |
| `monitoring.safe_actions_disabled_total` | — | monitoring_agent cycle, executor inactive |

**In-memory counters** (`SafeActionMetrics.counts`) accumulate regardless of CloudWatch availability and are used in tests to verify metric emission without a live CW client.

### 3.2 Verification

```bash
# Check safe_actions metrics in CloudWatch (last 1h)
aws cloudwatch get-metric-statistics \
  --namespace QuantEmbrace/SafeActions \
  --metric-name safe_actions.executed_total \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 3600 --statistics Sum

# Check entry block gauge
aws cloudwatch get-metric-statistics \
  --namespace QuantEmbrace/EntryBlock \
  --metric-name strategy.entry_block_active \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 3600 --statistics Maximum
```

---

## 4. ENTRY_BLOCK Defense-in-Depth in risk_engine

### 4.1 EntryBlockValidator

**File:** `services/risk_engine/validators/entry_block_validator.py`

**Position in validate_signal() pipeline:** Step 2b — after kill-switch check, before reconciliation check.

**Exemptions:**
| Signal type | Exempt? |
|---|---|
| `metadata["is_closeout"] = True` | Always exempt |
| `paper_trade=True`, `RISK_PROFILE=paper` | Exempt |
| `paper_trade=True`, `RISK_PROFILE=tiny-live` | NOT exempt |

**DynamoDB read failure:**
| Profile | Behaviour |
|---|---|
| `paper` | Warn + allow (fail open). Warning logged at WARNING level. |
| `tiny-live` / any non-paper | Fail closed. Entry rejected. `ENTRY_BLOCK_READ_FAILURE_FAIL_CLOSED` reason. |

**Cache:** 5s TTL. `invalidate()` forces fresh read.

### 4.2 Wire-up

```python
# services/risk_engine/service.py (step 2b, after kill switch)
eb_result = await self._entry_block_validator.validate(signal_obj)
if not eb_result.approved:
    # reject immediately — exits (is_closeout) never reach here
    return decision(REJECTED, eb_result.reason)
validator_results.append(eb_result)
# step 3: reconciliation check ...
```

---

## 5. Audit Hardening

Write actions (`BLOCK_NEW_ENTRIES`, `ACTIVATE_KILL_SWITCH`) now use `_write_audit_fail_closed()`. If the audit file write raises any exception:
- The executor returns `ExecutionResult(executed=False, error="AuditWriteFailed")`.
- The action is considered NOT executed (fail-closed semantics).
- An ERROR log is emitted with the exception type.

Read-only actions (`SEND_ALERT`, `GENERATE_RUNBOOK_COMMAND`, `READ_RUNTIME_STATE`) retain the original behaviour: audit failure falls back to a WARNING log and execution is considered successful.

**Audit paths covered:**

| Outcome | Audit written? |
|---|---|
| executed | Yes (`executed=True`) |
| blocked by policy | Yes (`blocked=True, blocked_reason=<deny_reason>`) |
| forbidden | Yes (`blocked=True, blocked_reason=<deny_reason>`) |
| requires_human_approval | Yes (`blocked=True, blocked_reason=requires_human_approval`) |
| idempotency_skip (memory) | Yes (`idempotency_skipped=True, source=memory`) |
| idempotency_skip (dynamo) | Yes (`idempotency_skipped=True, source=dynamo`) |
| precondition_failed | Yes (`blocked=True, precondition_failed=<desc>`) |
| DynamoDB write failed | Yes (`executed=False, error=<error_type>`) |
| audit write failed (write action) | Returns fail-closed result (no audit — this is the failure) |
| handler exception | Yes (`executed=False, error=<exc_type>`) |

---

## 6. Monitoring Status Updates

### §10a Entry Block Status

Rendered after §10 (Risk Cap). Fields:
- active (YES ⚠ / no)
- DynamoDB read_status (OK / ERROR)
- source, reason, action_id, idempotency_key, created_at
- CLI command to clear (when active)

### §10b Safe Actions Status

- ACTION_MODE
- executor_active
- actions_proposed / executed / blocked / idempotency_skips
- last_action_type, last_blocked_reason

### Verdict rules (Phase 6 additions)

| Condition | Status |
|---|---|
| ENTRY_BLOCK active, read_ok, exits healthy | AMBER (warning) |
| ENTRY_BLOCK DynamoDB read failure + fail-closed | RED (critical) |
| ENTRY_BLOCK DynamoDB read failure + fail-open (paper) | AMBER (warning) |
| ACTION_MODE=safe_actions, executor_active=False | AMBER (warning) |

---

## 7. Tests Added

| File | Tests | What's covered |
|---|---|---|
| `tests/unit/test_safe_action_idempotency.py` | 13 | DurableIdempotencyStore unit + executor integration |
| `tests/unit/test_safe_actions_metrics.py` | 20 | SafeActionMetrics all metric methods + resilience |
| `tests/unit/test_risk_engine_entry_block.py` | 21 | EntryBlockValidator — all profiles, exemptions, cache, failure modes |
| `tests/unit/test_monitoring_status.py` | +12 | Phase 6 snapshot fields + renderer §10a/§10b |

---

## 8. Operational Verification Commands

```bash
# 1. Verify entry block state
aws dynamodb get-item \
  --table-name <prefix>-risk-state \
  --key '{"PK":{"S":"ENTRY_BLOCK"},"SK":{"S":"GLOBAL"}}'

# 2. Verify idempotency keys written today
aws dynamodb query \
  --table-name <prefix>-risk-state \
  --key-condition-expression "PK = :pk" \
  --expression-attribute-values '{":pk":{"S":"SAFE_ACTION_IDEMPOTENCY"}}'

# 3. Clear entry block (manual operator action ONLY)
aws dynamodb delete-item \
  --table-name <prefix>-risk-state \
  --key '{"PK":{"S":"ENTRY_BLOCK"},"SK":{"S":"GLOBAL"}}'

# 4. Tail audit log
tail -f /app/data/safe_actions_audit.jsonl | jq .

# 5. Verify kill switch still inactive
python scripts/kill_switch_cli.py status

# 6. Run Phase 6 test suite
pytest tests/unit/test_safe_action_idempotency.py \
       tests/unit/test_safe_actions_metrics.py \
       tests/unit/test_risk_engine_entry_block.py \
       tests/unit/test_monitoring_status.py -v
```

---

## 9. Remaining Risks

| Risk | Severity | Notes |
|---|---|---|
| Metrics CloudWatch namespace not configured in prod env | LOW | In-memory counters always work; CW emit is best-effort |
| Idempotency keys accumulate in DynamoDB indefinitely | LOW | No TTL design yet; manageable at paper session volumes |
| risk_engine entry_block_validator cache is 5s — same as strategy_engine | ACCEPTED | Two 5s caches mean up to 10s lag in worst case. Acceptable for paper. |
| Host-side runtime verification not automated | LOW | Operator must run verification commands above after deploy |

---

## 10. Phase 7 Readiness

**Phase 7 can proceed.** All acceptance criteria met:
- ✅ Safe-action idempotency survives restart (DurableIdempotencyStore)
- ✅ Metrics emitted (SafeActionMetrics; test-stubbed with FakeCW)
- ✅ risk_engine enforces ENTRY_BLOCK for new entries
- ✅ Exits/closeouts remain allowed through all validators
- ✅ Audit written for every action path
- ✅ Live trading remains disabled (`QE_EXECUTION_LIVE_TRADING_ENABLED` absent)
- ✅ Capital unchanged (₹10L paper seed, no modifications)
- ✅ No broker orders placed
- ✅ All Phase 6 tests pass
