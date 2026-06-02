# Safe Actions — Design Document (Phases 3–6)

_Last updated: 2026-05-31 · Owner: Chief Architect · Status: Phase 6 implemented — tests passing_

---

## 1. Purpose

Phase 3 introduces a `safe_actions` layer that enables a conservative, autonomous
"observe → classify → act safely" loop for the monitoring agent. It is the bridge
between Phase 2 (severity classification, observe-only) and Phase 4 (wiring the
executor into the agent's live cycle behind the `ACTION_MODE=safe_actions` gate).

**Core constraint:** every Phase 3 safe action either reduces risk, improves
observability, or repairs paper/runtime state. No action increases market exposure,
bypasses human approval for irreversible operations, or modifies live trading
configuration autonomously.

---

## 2. Files Changed

| File | Role |
|---|---|
| `services/execution_engine/safe_actions/__init__.py` | Package re-exports |
| `services/execution_engine/safe_actions/safe_action_models.py` | All enums + SafeAction + ExecutionResult + ClassifiedObservation |
| `services/execution_engine/safe_actions/safe_action_policy.py` | Mode-based allow/deny rules |
| `services/execution_engine/safe_actions/safe_action_classifier.py` | Phase 2 finding → ClassifiedObservation |
| `services/execution_engine/safe_actions/safe_action_audit.py` | Append-only JSONL audit log |
| `services/execution_engine/safe_actions/safe_action_executor.py` | Policy + idempotency + dispatch + audit |
| `tests/unit/test_safe_actions.py` | 53 tests, all passing |
| `docs/architecture/safe-actions-design.md` | This document |
| `docs/live-readiness/phase3-safe-actions-report.md` | Readiness assessment |
| `docs/runbooks/manual-service-restart-runbook.md` | Manual restart runbook (human-only) |

---

## 3. Architecture

```
Phase 2 Finding (code, subject, severity)
    │
    ▼
SafeActionClassifier.classify_finding()
    │  pure function — no I/O — never proposes Docker restart
    ▼
ClassifiedObservation
    classification:   NO_ACTION / ALERT_ONLY / BLOCK_ENTRIES / PAPER_REPAIR / KILL_SWITCH / …
    proposed_actions: [BLOCK_NEW_ENTRIES, SEND_ALERT, …]
    suppress_docker_restart: always True
    │
    ▼  (caller builds SafeAction from proposed_actions)
SafeAction
    action_id · action_type · scope · mode · risk_level
    requires_human_approval · idempotency_key
    preconditions · expected_effect · rollback_behavior · audit_payload
    │
    ▼
SafeActionPolicy.is_allowed(action_type, scope)   ← first gate (stateless)
    │  fail closed — deny if mode/scope/type not in allowlist
    ▼
SafeActionExecutor.execute(action)
    ├── policy gate ────────── BLOCKED (audit written)
    ├── human-approval gate ── BLOCKED (audit written; GENERATE_RUNBOOK_COMMAND still returns command)
    ├── idempotency gate ───── SKIPPED (audit written)
    ├── precondition gate ──── BLOCKED (audit written)
    └── dispatch ──────────── executed / stub_not_implemented (audit written)
    │
    ▼
ExecutionResult + SafeActionAudit record (JSONL)
```

---

## 4. Enumerations

### ActionType
| Value | Scope | Phase 3 Status |
|---|---|---|
| `READ_RUNTIME_STATE` | READ_ONLY | ✅ Fully implemented |
| `SEND_ALERT` | READ_ONLY | ✅ Fully implemented |
| `GENERATE_RUNBOOK_COMMAND` | LIVE_HUMAN_GATED | ✅ Fully implemented (human executes) |
| `RUN_RECONCILIATION` | RISK_REDUCTION | ◐ Framework complete; Phase 4 wires writer |
| `ATTACH_EXIT_POLICY_PAPER` | PAPER_ONLY | ◐ Framework complete; Phase 4 wires writer |
| `REPAIR_PAPER_ZERO_QTY_OPEN` | PAPER_ONLY | ◐ Framework complete; Phase 4 wires writer |
| `UPDATE_PAPER_DIRECTION_FROM_QUANTITY` | PAPER_ONLY | ◐ Framework complete; Phase 4 wires writer |
| `FORCE_PAPER_SQUARE_OFF` | PAPER_ONLY | ◐ Framework complete; Phase 4 wires writer |
| `BLOCK_NEW_ENTRIES` | RISK_REDUCTION | ◐ Framework complete; Phase 4 wires DynamoDB write |
| `PAUSE_STRATEGY_ENTRIES` | RISK_REDUCTION | ◐ Framework complete; Phase 4 wires DynamoDB write |
| `ACTIVATE_KILL_SWITCH` | RISK_REDUCTION | ◐ Framework complete; Phase 4 wires DynamoDB + Kafka |
| `MARK_LIVE_READINESS_BLOCKED` | RISK_REDUCTION | ◐ Framework complete; Phase 4 wires DynamoDB write |
| `FORBIDDEN` | FORBIDDEN | 🚫 Always blocked at policy gate |

### ActionScope
| Value | Meaning |
|---|---|
| `READ_ONLY` | No state changes; reads and alerts only |
| `PAPER_ONLY` | Modifies paper simulation state; forbidden in live modes |
| `RISK_REDUCTION` | Reduces exposure; allowed in paper + live (not BACKTEST/LIVE_FULL_BLOCKED) |
| `LIVE_HUMAN_GATED` | Advisory artifact; human executes the actual change |
| `FORBIDDEN` | Never executable by the autonomous layer |

### TradingMode (policy modes)
| Mode | Derives from |
|---|---|
| `PAPER` | `RISK_PROFILE=paper` + live flag absent/false |
| `BACKTEST` | Backtester context |
| `LIVE_DISABLED` | Live flag absent/false (any profile) |
| `LIVE_STAGE_1` | `RISK_PROFILE=tiny-live` + `QE_EXECUTION_LIVE_TRADING_ENABLED=true` |
| `LIVE_FULL_BLOCKED` | Unknown / emergency state |

### ClassificationResult
| Value | Meaning |
|---|---|
| `NO_ACTION` | Component healthy |
| `ALERT_ONLY` | Post alert; no state change |
| `READ_ONLY_VERIFY` | Run read-only verification |
| `PAPER_REPAIR` | Apply paper-state repair (paper mode only) |
| `RISK_REDUCTION` | Block entries / pause strategy |
| `BLOCK_ENTRIES` | Block new entries platform-wide; exits continue |
| `KILL_SWITCH` | Activate kill switch (critical unmanaged exposure only) |
| `HUMAN_APPROVAL_REQUIRED` | Generate runbook; human executes |
| `FORBIDDEN` | Categorically disallowed |

---

## 5. Allowed Actions by Mode

| Action | PAPER | BACKTEST | LIVE_DISABLED | LIVE_STAGE_1 | LIVE_FULL_BLOCKED |
|---|---|---|---|---|---|
| READ_RUNTIME_STATE | ✅ | ✅ | ✅ | ✅ | ✅ |
| SEND_ALERT | ✅ | ✅ | ✅ | ✅ | ✅ |
| GENERATE_RUNBOOK_COMMAND | ✅ | ✅ | ✅ | ✅ | ✅ |
| ATTACH_EXIT_POLICY_PAPER | ✅ | 🚫 | 🚫 | 🚫 | 🚫 |
| REPAIR_PAPER_ZERO_QTY_OPEN | ✅ | 🚫 | 🚫 | 🚫 | 🚫 |
| UPDATE_PAPER_DIRECTION_FROM_QUANTITY | ✅ | 🚫 | 🚫 | 🚫 | 🚫 |
| FORCE_PAPER_SQUARE_OFF | ✅ | 🚫 | 🚫 | 🚫 | 🚫 |
| BLOCK_NEW_ENTRIES | ✅ | 🚫 | ✅ | ✅ | 🚫 |
| PAUSE_STRATEGY_ENTRIES | ✅ | 🚫 | ✅ | ✅ | 🚫 |
| ACTIVATE_KILL_SWITCH | ✅ | 🚫 | ✅ | ✅ | 🚫* |
| MARK_LIVE_READINESS_BLOCKED | ✅ | 🚫 | ✅ | ✅ | 🚫 |
| RUN_RECONCILIATION | ✅ | 🚫 | ✅ | ✅ | 🚫 |
| FORBIDDEN | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 |

*In LIVE_FULL_BLOCKED, kill switch activation must come from the human-executed runbook.

---

## 6. Forbidden Actions (Phase 3 — categorically never execute)

These are checked by `SafeActionPolicy.is_context_forbidden()` and by the
classifier's `classify_forbidden_context()` method.

```
enable_live_trading             set_trading_mode_live
change_capital_limits           place_real_broker_order
restart_live_execution_engine   restart_live_risk_engine
restart_live_ai_engine          restart_docker_container
mutate_strategy_parameters      clear_kill_switch
clear_reconciliation_required   clear_exit_order_id
delete_dynamodb_records         auto_promote_strategy_to_live
modify_allowed_symbols          modify_allowed_strategies
silence_alerts
```

---

## 7. Decision: Docker Restart NOT Approved for Phase 3

**Decision:** Restarting `ai_engine` (or any service) through Docker API is not
an approved Phase 3 safe action.

**Reasoning:**
1. **Hides root cause.** A restart that succeeds masks the crash; without the crash
   log the operator cannot determine whether this is a bug, a resource constraint,
   or an infrastructure issue.
2. **Operationally invasive.** A restart interrupts the Kafka consumer group (aiengine-v1),
   triggers a rebalance, and may spike consumer lag — potentially activating the
   EnrichmentWatchdog fallback path in an unexpected way.
3. **Does not directly reduce exposure.** The EnrichmentWatchdog already handles
   ai_engine unavailability gracefully. A restart helps the service but does not
   change the platform's risk state.
4. **Human approval required.** Any service restart in a live-adjacent environment
   must be a deliberate operator decision, not an autonomous response.

**Phase 3 behaviour when ai_engine is DOWN:**
- Classify as `ALERT_ONLY`
- Proposed actions: `BLOCK_NEW_ENTRIES` (for AI-dependent strategies) + `SEND_ALERT` + `GENERATE_RUNBOOK_COMMAND`
- `suppress_docker_restart = True` (always)
- The runbook command is returned for the operator to execute manually

**Phase 4+ path for Docker restart:** add to `docs/runbooks/manual-service-restart-runbook.md`
as a human-executed runbook action gated by explicit sign-off. Never autonomous.

---

## 8. Classifier — Finding Code to Classification Mapping (key entries)

| Finding Code | Subject | ClassificationResult | Proposed Actions |
|---|---|---|---|
| `service.down` | ai_engine | ALERT_ONLY | BLOCK_NEW_ENTRIES, SEND_ALERT |
| `service.down` | execution_engine | KILL_SWITCH | ACTIVATE_KILL_SWITCH, SEND_ALERT |
| `service.down` | risk_engine | KILL_SWITCH | ACTIVATE_KILL_SWITCH, SEND_ALERT |
| `kafka.unreachable` | cluster | KILL_SWITCH | ACTIVATE_KILL_SWITCH, SEND_ALERT |
| `kafka.group_lag_critical` | any | BLOCK_ENTRIES | BLOCK_NEW_ENTRIES, SEND_ALERT |
| `broker.feed_very_stale` | feed | BLOCK_ENTRIES | BLOCK_NEW_ENTRIES, SEND_ALERT |
| `broker.feed_stale` | feed | ALERT_ONLY | SEND_ALERT |
| `dynamodb.down` | any | KILL_SWITCH | ACTIVATE_KILL_SWITCH, SEND_ALERT |
| `docker.restart_loop` | ai_engine | ALERT_ONLY | BLOCK_NEW_ENTRIES, SEND_ALERT, GENERATE_RUNBOOK_COMMAND |
| `docker.restart_loop` | any | ALERT_ONLY | SEND_ALERT, GENERATE_RUNBOOK_COMMAND |

Raw observations:

| Observation Key | ClassificationResult | Proposed Actions |
|---|---|---|
| `reconciliation_required_live` | BLOCK_ENTRIES | BLOCK_NEW_ENTRIES, SEND_ALERT, MARK_LIVE_READINESS_BLOCKED |
| `paper_position_missing_exit_policy` | PAPER_REPAIR | ATTACH_EXIT_POLICY_PAPER, SEND_ALERT |
| `paper_position_zero_qty_open` | PAPER_REPAIR | REPAIR_PAPER_ZERO_QTY_OPEN, SEND_ALERT |
| `unmanaged_live_position` | KILL_SWITCH | ACTIVATE_KILL_SWITCH, SEND_ALERT |
| `stale_ltp_market_hours` | BLOCK_ENTRIES | BLOCK_NEW_ENTRIES, SEND_ALERT |

---

## 9. Audit Log

Every executor call writes one JSONL record to
`/app/data/safe_actions_audit.jsonl` (configurable). Fields:

```json
{
  "timestamp": "2026-05-31T09:30:00.000000+00:00",
  "action_id": "<uuid4>",
  "action_type": "BLOCK_NEW_ENTRIES",
  "scope": "RISK_REDUCTION",
  "mode": "PAPER",
  "risk_level": "LOW",
  "requires_human_approval": false,
  "idempotency_key": "block-entries-stale-ltp-20260531",
  "executed": false,
  "blocked": false,
  "blocked_reason": null,
  "idempotency_skipped": false,
  "precondition_failed": null,
  "error": null,
  "stub_not_implemented": true,
  "requested_by": "monitoring_agent",
  "reason": "stale_ltp_market_hours",
  "audit_payload": {"reason": "stale_ltp", "market": "NSE"},
  "result_payload": {}
}
```

Secrets are never written. The `audit_payload` follows the same secret-free
contract as Phase 1 collector `details`.

---

## 10. Idempotency

The executor tracks executed idempotency keys in-memory per session. The key
convention is: `<action_type_lower>-<subject>-<yyyymmdd>`. A duplicate key
within the same session produces `ExecutionResult(idempotency_skipped=True)` and
an audit record — no repeated execution. On service restart the in-memory store
resets; keys are not persisted across restarts (Phase 4 may add persistence).

---

## 11. Phase 4 — COMPLETE (2026-05-31)

Phase 4 implemented exactly two write handlers (per design decision). All other
actions remain stubs — unchanged from Phase 3.

| Action | Phase 4 Status | DynamoDB Key |
|---|---|---|
| BLOCK_NEW_ENTRIES | ✅ **IMPLEMENTED** | `ENTRY_BLOCK/GLOBAL` in risk-state table |
| ACTIVATE_KILL_SWITCH | ✅ **IMPLEMENTED** | `KILLSWITCH/GLOBAL` in risk-state table |
| PAUSE_STRATEGY_ENTRIES | ◐ Stub | Phase 5 |
| ATTACH_EXIT_POLICY_PAPER | ◐ Stub | Phase 5 |
| REPAIR_PAPER_ZERO_QTY_OPEN | ◐ Stub | Phase 5 |
| UPDATE_PAPER_DIRECTION_FROM_QUANTITY | ◐ Stub | Phase 5 |
| FORCE_PAPER_SQUARE_OFF | ◐ Stub | Phase 5 |
| RUN_RECONCILIATION | ◐ Stub | Phase 5 |
| MARK_LIVE_READINESS_BLOCKED | ◐ Stub | Phase 5 |

Phase 5 remaining work: wire executor into monitoring agent `run_once()` behind
`ACTION_MODE=safe_actions`; wire ENTRY_BLOCK reader in strategy_engine; add
CloudWatch counter publishing; add JSONL-backed idempotency persistence.

## Phase 4 ENTRY_BLOCK schema

```
Table: {prefix}-risk-state
PK = "ENTRY_BLOCK"  SK = "GLOBAL"
blocked=BOOL true | status=S "BLOCKED" | reason=S | source=S "safe_actions"
action_id=S | idempotency_key=S | created_at=S ISO | schema_version=S "1.0"
```

Clearing: human-executed only; not an approved safe action.

## Phase 4 KILL_SWITCH schema (canonical, shared with KillSwitch reader)

Uses `kill_switch_item()` from `shared/risk_state.py`. See
`docs/runbooks/kill-switch-runbook.md` for how to verify and deactivate.

## Phase 4 original stub section (for reference)
| PAUSE_STRATEGY_ENTRIES | Write `max_signals_per_day: 0` to per-strategy config item |
| ATTACH_EXIT_POLICY_PAPER | DynamoDB `update_item` on paper positions table |
| REPAIR_PAPER_ZERO_QTY_OPEN | DynamoDB `update_item` to set `status=CLOSED` |
| UPDATE_PAPER_DIRECTION_FROM_QUANTITY | DynamoDB `update_item` on direction field |
| FORCE_PAPER_SQUARE_OFF | Batch DynamoDB writes simulating flat fills |
| RUN_RECONCILIATION | Call `scripts/ops/reconcile.py` via subprocess |
| MARK_LIVE_READINESS_BLOCKED | DynamoDB write to `risk-state` table |

The monitoring agent's `app.py` change for Phase 4:
```python
if self.config.action_mode == "safe_actions":
    executor = SafeActionExecutor(policy=..., audit=..., notifier=self.notifier.notify)
    for finding in report.findings_at_or_above(Severity.WARNING):
        obs = classifier.classify_finding(finding.code, finding.subject)
        for action_type in obs.proposed_action_types:
            action = _build_action(action_type, obs, mode)
            executor.execute(action, reason=finding.message)
```

---

## Phase 6 — Production Observability & Persistent Idempotency

_Added: 2026-05-31_

### 6.1 Persistent Idempotency (DurableIdempotencyStore)

**Problem:** The Phase 4/5 executor used an in-memory `dict` for idempotency. A process restart (e.g. container restart, OOM kill) cleared the in-memory store, allowing BLOCK_NEW_ENTRIES or ACTIVATE_KILL_SWITCH to execute again on restart even though the write had already landed in DynamoDB.

**Solution:** `DurableIdempotencyStore` (`safe_action_idempotency_store.py`) persists idempotency keys to the risk-state DynamoDB table.

**Schema:**
```
PK  = "SAFE_ACTION_IDEMPOTENCY"
SK  = <idempotency_key>          (e.g. "block_new_entries-stale_ltp-20260531")
action_type   S — ActionType value
mode          S — TradingMode value
status        S — "executed"
action_id     S — UUID4 of the SafeAction
created_at    S — ISO-8601 UTC
result        S — JSON result_payload (truncated to 256 chars)
audit_ref     S — same as action_id
```

**Conditional write:** Uses `attribute_not_exists(SK)` so two concurrent executor instances cannot both claim the same key. A `ConditionalCheckFailedException` from DynamoDB is treated as "already marked" and returns `False` from `mark()`.

**Executor integration:**
- Applied only to write actions: `BLOCK_NEW_ENTRIES`, `ACTIVATE_KILL_SWITCH`.
- Check order: durable store (DynamoDB) → in-memory store → execute.
- On success: `mark()` is called best-effort; failure to mark does not roll back the write.
- DynamoDB unavailability during `check()`: treated as not-found (execution is attempted). The primary safety is the DynamoDB write for the action itself.
- Keys are never deleted automatically.

### 6.2 CloudWatch Metrics (SafeActionMetrics)

`safe_action_metrics.py` wraps a CloudWatch client and emits the following counters:

| Metric | When |
|---|---|
| `safe_actions.executed_total` | Action dispatched and executed |
| `safe_actions.blocked_total` | Policy, precondition, or human-gate block |
| `safe_actions.forbidden_total` | FORBIDDEN action type attempted |
| `safe_actions.idempotency_skip_total` | Skipped (memory or dynamo), dimension Source |
| `safe_actions.dynamo_write_failed_total` | DynamoDB write failed for write action |
| `safe_actions.block_new_entries_total` | BLOCK_NEW_ENTRIES succeeded |
| `safe_actions.kill_switch_activated_total` | ACTIVATE_KILL_SWITCH succeeded |
| `safe_actions.kill_switch_write_failed_total` | ACTIVATE_KILL_SWITCH DynamoDB write failed |
| `strategy.entry_block_active` | Gauge: 1 if active, 0 if not |
| `strategy.entry_blocked_total` | New entry blocked by strategy_engine |
| `strategy.entry_block_read_failure_total` | DynamoDB read failure in entry_block_reader |
| `strategy.entry_block_cache_hit_total` | Cache hit in entry_block_reader |
| `strategy.entry_block_cache_miss_total` | Cache miss (fresh DynamoDB read) |
| `monitoring.safe_actions_executed_total` | monitoring_agent executed a safe action |
| `monitoring.safe_actions_blocked_total` | monitoring_agent action was blocked |
| `monitoring.safe_actions_disabled_total` | ACTION_MODE not safe_actions — disabled cycle |

Metrics never raise. If CloudWatch is unavailable the metric is silently dropped; in-memory counters (`SafeActionMetrics.counts`) always accumulate for test verification.

### 6.3 ENTRY_BLOCK Defense-in-Depth in risk_engine

**Primary gate:** strategy_engine blocks new entry signal production when ENTRY_BLOCK is active.

**Secondary gate (Phase 6):** `EntryBlockValidator` in risk_engine provides defense-in-depth. If a new-entry signal somehow reaches risk_engine while ENTRY_BLOCK is active in DynamoDB, it is rejected here.

**Wire-up position:** After kill-switch check (step 2b), before reconciliation check (step 3).

**Exemptions:**
- `signal.metadata["is_closeout"] = True` → always approved regardless of ENTRY_BLOCK state.
- `signal.paper_trade = True` in `paper` risk profile → approved (paper signals carry no real-money risk).

**Fail behaviour on DynamoDB read error:**
- `RISK_PROFILE=paper` → WARN + allow (fail open). DynamoDB transients don't halt paper trading.
- `RISK_PROFILE=tiny-live` or any non-paper profile → fail closed. New entries rejected. Closeouts still approved.

**Cache:** 5s TTL (default), matches EntryBlockReader in shared layer.

### 6.4 Audit Fail-Closed for Write Actions

Write actions (`BLOCK_NEW_ENTRIES`, `ACTIVATE_KILL_SWITCH`) now use `_write_audit_fail_closed()` instead of `_write_audit()`. If the audit file write raises an exception (disk full, permission error), the executor returns `ExecutionResult(executed=False, error="AuditWriteFailed")` rather than silently continuing. Read-only alert actions retain the original log-fallback behaviour.

### 6.5 Monitoring Status §10a and §10b

`MonitoringStatusRenderer` now emits two new sections after §10 (Risk Cap):

- **§10a Entry Block Status** — active/inactive, source, reason, action_id, idempotency_key, created_at, DynamoDB read_status. Includes the DynamoDB CLI command to clear the flag.
- **§10b Safe Actions Status** — ACTION_MODE, executor_active, actions_proposed/executed/blocked, idempotency_skips, last_action_type, last_blocked_reason.

**Verdict rules:**
- ENTRY_BLOCK active + read_ok → `AMBER` (warning: exits unaffected).
- ENTRY_BLOCK DynamoDB read failure (fail-closed) → `RED` (critical: entries blocked due to read failure).
- `ACTION_MODE=safe_actions` + `executor_active=False` → `AMBER` warning.

### 6.6 Files Changed in Phase 6

| File | Change |
|---|---|
| `services/execution_engine/safe_actions/safe_action_executor.py` | DurableIdempotencyStore + SafeActionMetrics wired; audit fail-closed for write actions |
| `services/execution_engine/safe_actions/safe_action_idempotency_store.py` | New — DurableIdempotencyStore |
| `services/execution_engine/safe_actions/safe_action_metrics.py` | New — SafeActionMetrics |
| `services/risk_engine/validators/entry_block_validator.py` | New — EntryBlockValidator |
| `services/risk_engine/service.py` | Import + instantiate EntryBlockValidator; wire in validate_signal() step 2b |
| `services/shared/monitoring/monitoring_status.py` | EntryBlockStatus + SafeActionsStatus dataclasses; LiveCounters Phase 6 fields; build_snapshot _fetch_entry_block + _build_safe_actions_status; renderer §10a + §10b |
| `tests/unit/test_safe_action_idempotency.py` | New — 13 tests |
| `tests/unit/test_safe_actions_metrics.py` | New — 20 tests |
| `tests/unit/test_risk_engine_entry_block.py` | New — 21 tests |
| `tests/unit/test_monitoring_status.py` | Phase 6 class appended — 12 new tests |
| `docs/architecture/safe-actions-design.md` | Phase 6 section added |
| `docs/live-readiness/phase6-observability-idempotency-report.md` | New |
| `docs/runbooks/safe-actions-operations-runbook.md` | New |
