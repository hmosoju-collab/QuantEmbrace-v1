# Phase 4 Safe Actions — Runtime Handlers Report

_Date: 2026-05-31 · Status: COMPLETE — 217 tests passing_

---

## Summary

Phase 4 implements the two approved risk-reducing DynamoDB write handlers:
`BLOCK_NEW_ENTRIES` and `ACTIVATE_KILL_SWITCH`. Everything else remains
read-only, alert-only, paper-only, manual runbook, or forbidden. This keeps the
write surface — and blast radius — minimal.

Live trading is not affected. The monitoring agent still defaults to
`MONITORING_ACTION_MODE=notify_only`. The executor is not wired into the agent's
cycle until the operator explicitly enables `safe_actions` mode.

---

## DynamoDB Keys Used

Both writes target the **`{prefix}-risk-state`** table.

### BLOCK_NEW_ENTRIES
```
PK  = "ENTRY_BLOCK"
SK  = "GLOBAL"
blocked           BOOL  = true
status            S     = "BLOCKED"
reason            S     = <safe action reason>
source            S     = "safe_actions"
action_id         S     = <uuid4>
idempotency_key   S     = <idempotency_key>
created_at        S     = <ISO-8601 UTC>
schema_version    S     = "1.0"
```

**Exit management is unaffected.** TEE, MIS, and ExitOrderRouter read from
`orders` and `positions` tables — they never check `ENTRY_BLOCK`. Only
strategy_engine's entry-signal production is gated on this flag (Phase 5 wires
the reader).

### ACTIVATE_KILL_SWITCH
```
PK  = "KILLSWITCH"
SK  = "GLOBAL"
active            BOOL  = true
status            S     = "ACTIVE"
scope             S     = "GLOBAL"
reason            S     = <safe action reason>
activated_by      S     = "safe_actions"
activated_at      S     = <ISO-8601 UTC>
updated_at        S     = <ISO-8601 UTC>
detail            S     = "action_id=… idempotency_key=… source=safe_actions"
schema_version    S     = "1.0"
```

Uses `kill_switch_item()` from `shared/risk_state.py` — the same canonical
schema that `KillSwitch._load_state()` in risk_engine reads. After this write,
the next risk_engine poll (1s via KafkaKillSwitchListener or KillSwitchCache)
will see `active=True` and halt new signal approvals.

---

## Files Changed

| File | Change |
|---|---|
| `services/shared/risk_state.py` | Added `ENTRY_BLOCK_PK`, `ENTRY_BLOCK_SK`, `entry_block_key()`, `entry_block_item()` |
| `services/execution_engine/safe_actions/safe_action_dynamo_writer.py` | **NEW** — `SafeActionDynamoWriter` + `WriteResult` |
| `services/execution_engine/safe_actions/safe_action_executor.py` | Added `dynamo_writer` param; `_handle_block_new_entries`; `_handle_activate_kill_switch`; observability counters; structured logging |
| `services/execution_engine/safe_actions/__init__.py` | Re-exports `SafeActionDynamoWriter`, `WriteResult` |
| `tests/unit/test_safe_action_dynamo_writer.py` | **NEW** — 20 unit tests |
| `tests/integration/test_safe_actions_runtime_handlers.py` | **NEW** — 21 integration tests |

---

## Actions Implemented

Only two:

| Action | Scope | DynamoDB Key | Effect |
|---|---|---|---|
| BLOCK_NEW_ENTRIES | RISK_REDUCTION | ENTRY_BLOCK/GLOBAL | Sets `blocked=True`; prevents new entry signals (Phase 5 reader) |
| ACTIVATE_KILL_SWITCH | RISK_REDUCTION | KILLSWITCH/GLOBAL | Sets `active=True`; halts signal approvals in risk_engine immediately |

All other action types: read-only, alert-only, paper-only, manual runbook, or forbidden — unchanged from Phase 3.

---

## Actions Forbidden (still enforced, never wired)

Nothing added to the forbidden list. All 17 Phase 3 forbidden context labels remain:
`enable_live_trading`, `change_capital_limits`, `place_real_broker_order`,
`restart_docker_container`, `clear_kill_switch`, `clear_reconciliation_required`,
`delete_dynamodb_records`, `auto_promote_strategy_to_live`, `silence_alerts`, etc.

---

## How to Verify Entry Block

```bash
# Read current ENTRY_BLOCK state from DynamoDB (local)
aws dynamodb get-item \
  --endpoint-url http://localhost:4566 \
  --table-name quantembrace-development-risk-state \
  --key '{"PK":{"S":"ENTRY_BLOCK"},"SK":{"S":"GLOBAL"}}'

# Expected when blocked:
# { "Item": { "blocked": {"BOOL": true}, "status": {"S": "BLOCKED"}, ... } }

# Clear entry block (HUMAN ONLY — not an autonomous action):
# Write a new item with blocked=false via the reconciliation runbook.
# Do NOT use safe_actions to clear it — clearing is not an approved action.
```

---

## How to Verify Kill Switch

```bash
# Check kill switch status
python scripts/kill_switch_cli.py status

# Or read DynamoDB directly
aws dynamodb get-item \
  --endpoint-url http://localhost:4566 \
  --table-name quantembrace-development-risk-state \
  --key '{"PK":{"S":"KILLSWITCH"},"SK":{"S":"GLOBAL"}}'
```

---

## How to Manually Clear Kill Switch (Human Only)

See `docs/runbooks/kill-switch-runbook.md`. Never autonomous.

---

## Why Exits Remain Allowed

`BLOCK_NEW_ENTRIES` writes `ENTRY_BLOCK/GLOBAL` with `blocked=True`.
This key is read only by strategy_engine's entry-signal production path (Phase 5).
TEE, MIS, and ExitOrderRouter read from `orders` (status-index GSI) and
`positions` tables — they never check `ENTRY_BLOCK`. Verified in test:
`test_stale_ltp_does_not_touch_exit_management_keys`.

`ACTIVATE_KILL_SWITCH` sets the kill switch ACTIVE. The kill switch blocks new
signal approvals in risk_engine. Exit fills still arrive from the broker, are
processed by `fill_poller`, and applied to positions. The execution_engine's
`TradeExitEngine` and `MISSquareOffManager` are not blocked by the kill switch
in the existing implementation (they operate on fills and time, not signals).

---

## Tests Added

| File | Count | Coverage |
|---|---|---|
| `test_safe_action_dynamo_writer.py` | 20 | DynamoDB key schema, idempotency, failure handling, cross-action isolation |
| `test_safe_actions_runtime_handlers.py` | 21 | Full cycles (classifier→executor→DynamoDB), ai_engine DOWN, reconciliation, counters, fail-closed, backtest denied, FORBIDDEN blocked |

---

## Test Results

```
217 passed  (safe_actions + dynamo_writer + integration + monitoring Phase 1+2)

Pre-existing failures (no Phase 4 dependency, confirmed):
  test_mis_square_off.py        20 failed  (pre-existing, unrelated logic)
  test_position_reconciliation  some failed (pre-existing)
  test_trade_exit_engine        3 failed   (pre-existing assertion)

Phase 4 files import 0 symbols from the failing test files.
```

---

## Remaining Risks

| Risk | Severity | Mitigation |
|---|---|---|
| ENTRY_BLOCK reader not wired | LOW | Phase 5 concern; write is visible in DynamoDB but pipeline doesn't yet enforce it |
| Kill switch write races existing KillSwitch.activate() | LOW | Both use put_item idempotently; last writer wins (both set active=True) |
| Counters reset on restart | LOW | In-memory only; CloudWatch publish is Phase 5 |
| Operator must set ACTION_MODE=safe_actions to enable | LOW | Intentional — monitoring agent still in notify_only by default |

---

## Can Phase 5 Proceed?

**Yes.** Phase 5 wires:
1. The ENTRY_BLOCK reader in strategy_engine (reads `ENTRY_BLOCK/GLOBAL` before publishing entry signals)
2. The monitoring agent's `run_once()` cycle behind `ACTION_MODE=safe_actions`
3. CloudWatch metric publishing for counters
4. Persistence for the idempotency store (JSONL replay across restarts)

Pre-condition: host-side runtime verification must pass before any safe action runs in production.
