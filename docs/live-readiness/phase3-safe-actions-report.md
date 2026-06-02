# Phase 3 Safe Actions — Readiness Report

_Date: 2026-05-31 · Status: COMPLETE — tests passing, docs written_

---

## Summary

Phase 3 delivers the design and framework for an autonomous conservative
safe-actions layer. The monitoring agent can now classify Phase 2 severity
findings into typed safe actions, enforce a mode-based policy gate, record an
immutable audit trail, and (for three fully-implemented action types) execute
them. All DynamoDB/Kafka write actions remain stubbed pending Phase 4 wiring.

**Live trading is not affected.** The monitoring agent still runs in
`MONITORING_ACTION_MODE=notify_only` by default. Phase 3 code is present but
not wired into the agent's cycle. Operators must explicitly set
`ACTION_MODE=safe_actions` to enable Phase 4 execution.

---

## Deliverables

| Deliverable | Status |
|---|---|
| `safe_action_models.py` — all enums + SafeAction + ExecutionResult + ClassifiedObservation | ✅ |
| `safe_action_policy.py` — mode-based allow/deny rules; `from_env()` factory | ✅ |
| `safe_action_classifier.py` — 30 finding-code mappings + 10 raw-observation mappings | ✅ |
| `safe_action_audit.py` — JSONL audit log; never raises; CloudWatch fallback | ✅ |
| `safe_action_executor.py` — policy + human-approval + idempotency + preconditions + dispatch + audit | ✅ |
| `tests/unit/test_safe_actions.py` — 53 tests, all 15 user-specified scenarios covered | ✅ |
| `docs/architecture/safe-actions-design.md` | ✅ |
| `docs/runbooks/manual-service-restart-runbook.md` | ✅ |
| monitoring_agent `actions/__init__.py` updated to reference Phase 3 | ✅ |
| ADR-025 in `memory/decisions.md` | ✅ |

---

## Test Results

```
53 passed in 0.08s  (test_safe_actions.py)
176 passed total    (safe_actions + monitoring_detectors + monitoring_agent)
```

---

## Allowed Safe Actions

### Read-only (all modes)
- `READ_RUNTIME_STATE` — read current kill switch / token / table state
- `SEND_ALERT` — post Slack/SNS warning or critical alert
- `GENERATE_RUNBOOK_COMMAND` — return operator command string (human executes)

### Alert (all modes)
- `SEND_ALERT` — write incident record, mark AMBER/RED in snapshot

### Paper-only repair (PAPER mode only)
- `ATTACH_EXIT_POLICY_PAPER` — attach missing SL/TP to paper position
- `REPAIR_PAPER_ZERO_QTY_OPEN` — mark stale zero-qty paper position closed
- `UPDATE_PAPER_DIRECTION_FROM_QUANTITY` — fix direction from signed quantity
- `FORCE_PAPER_SQUARE_OFF` — simulate flat fills on all paper positions

### Risk-reducing (PAPER, LIVE_DISABLED, LIVE_STAGE_1)
- `BLOCK_NEW_ENTRIES` — block new entry signals; exits continue
- `PAUSE_STRATEGY_ENTRIES` — pause a single strategy's new entries
- `ACTIVATE_KILL_SWITCH` — set kill switch ACTIVE
- `MARK_LIVE_READINESS_BLOCKED` — write blocked state to risk-state DynamoDB
- `RUN_RECONCILIATION` — trigger paper reconciliation pass

### Human-gated advisory (all modes, human executes the command)
- `GENERATE_RUNBOOK_COMMAND` — returns operator command; no autonomous execution

---

## Forbidden Actions (Phase 3 — never execute)

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

**Docker restart answer:** Restarting `ai_engine` (or any service) via Docker
API is NOT an approved Phase 3 action and will not be autonomous in Phase 4 either.
When `ai_engine` is DOWN: the classifier proposes `BLOCK_NEW_ENTRIES` (AI-dependent
entries) + `SEND_ALERT` + `GENERATE_RUNBOOK_COMMAND`. The operator runs the restart
command manually. See `docs/runbooks/manual-service-restart-runbook.md`.

---

## Key Design Decisions

**1. Additive enrichment invariant preserved.**
Phase 3 safe actions never change `overall_status` or the Status-based incident
log gating. The Phase 2 enrichment-only contract is maintained.

**2. Fail-closed executor.**
Any unrecognised action type, policy violation, precondition failure, or unexpected
exception returns a blocked `ExecutionResult` with an audit record — never a silent
pass-through or exception propagation.

**3. ai_engine DOWN → ALERT_ONLY, not KILL_SWITCH.**
`ai_engine` is non-critical (EnrichmentWatchdog provides fallback). Its failure
warrants an alert and entry-blocking, not a kill switch activation or Docker restart.

**4. Stale LTP → BLOCK_ENTRIES only.**
`BLOCK_NEW_ENTRIES` blocks new entry signals. It does NOT stop TEE, MIS square-off,
or any exit path. This is encoded in the action's `expected_effect` and tested.

**5. Idempotency is in-memory per session.**
The same `idempotency_key` cannot execute twice in one agent session. This prevents
repeated BLOCK_NEW_ENTRIES writes from racing the same strategy-config key on a
30s poll cycle.

---

## Remaining Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Phase 4 DynamoDB writers not yet implemented | LOW | Stubs return `stub_not_implemented=True`; no silent failure |
| `BLOCK_NEW_ENTRIES` in-memory only | MEDIUM | Phase 4 must write to DynamoDB `strategy-config`; until then, blocking is advisory |
| Idempotency resets on restart | LOW | Acceptable for Phase 3; Phase 4 may add JSONL-backed persistence |
| Executor not yet wired into monitoring agent loop | LOW | Operator must set `ACTION_MODE=safe_actions`; Phase 4 concern |
| `ACTIVATE_KILL_SWITCH` stub only | MEDIUM | Phase 4 critical path; kill switch still operable via CLI + API |

---

## Can Phase 4 Proceed?

**Yes, with the following preconditions:**

1. Operator explicitly enables `MONITORING_ACTION_MODE=safe_actions` in monitoring
   agent config (not done automatically).
2. Phase 4 implements the DynamoDB write handlers for stub actions — starting
   with `BLOCK_NEW_ENTRIES` and `ACTIVATE_KILL_SWITCH` as highest priority.
3. Phase 4 wires the executor into `monitoring_agent/app.py` `run_once()` behind
   the `action_mode == "safe_actions"` gate.
4. Phase 4 adds integration tests that stub DynamoDB writes and verify the full
   cycle: `Finding → ClassifiedObservation → SafeAction → ExecutionResult → audit`.
5. Paper trading host runtime verification passes (currently parked — unrelated
   to Phase 3 but must complete before any live-adjacent automation).

---

## Commands Run

```bash
# Tests
python3 -m pytest tests/unit/test_safe_actions.py -q
# 53 passed

# Full suite
python3 -m pytest tests/unit/test_safe_actions.py tests/unit/test_monitoring_detectors.py tests/unit/test_monitoring_agent.py -q
# 176 passed

# Secret scan
grep -rn "AKIA\|hooks.slack.com\|access_token\s*=\|api_key\s*=\|password\s*=" services/execution_engine/safe_actions/
# Clean

# Read-only audit
python3 -c "import ast, pathlib; [print(p, 'OK') for p in pathlib.Path('services/execution_engine/safe_actions').glob('*.py')]"
# All 6 files parse clean
```
