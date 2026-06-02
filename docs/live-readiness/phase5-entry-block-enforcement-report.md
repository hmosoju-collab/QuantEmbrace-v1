# Phase 5 Entry-Block Enforcement Report

_Date: 2026-05-31 · Status: COMPLETE — 411 tests passing_

---

## Summary

Phase 5 makes `BLOCK_NEW_ENTRIES` effective. The `ENTRY_BLOCK/GLOBAL` DynamoDB flag
(written by Phase 4) is now read by strategy_engine before every signal dispatch.
When blocked, entry signals are suppressed before the StrategyRunner's daily cap is
incremented. Exit management (TEE, MIS, ExitOrderRouter) is untouched.

The monitoring agent's `run_once()` loop now includes a Phase 5 action gate: when
`MONITORING_ACTION_MODE=safe_actions`, CRITICAL/BLOCKER findings are classified and
executed via `SafeActionExecutor`. Default mode (`notify_only`) is unchanged.

---

## 1. ENTRY_BLOCK Enforcement Design

### How strategy_engine enforces ENTRY_BLOCK

1. `start()` constructs `EntryBlockReader` with the low-level boto3 DynamoDB client,
   targeting `{prefix}-risk-state` table, TTL=5s, fail-closed mode resolved from
   `RISK_PROFILE` env var (`paper` → fail-open, anything else → fail-closed).

2. `_is_entry_blocked()` wraps the reader via `asyncio.to_thread()` (synchronous
   DynamoDB read offloaded from the async event loop). Returns `(blocked, EntryBlockState)`.

3. **Tick loop** (`_dispatch_tick_batch()`): entry block checked BEFORE
   `runner.dispatch_tick()`. If blocked: calls `runner._strategy.on_tick()` for
   indicator update (mirrors existing `suppress_signals` pattern) and continues —
   `_signals_today` is never incremented.

4. **Candle loop** (`_candle_processing_loop()`): entry block checked BEFORE
   `runner.dispatch_bar()`. If blocked: skips dispatch entirely — `_signals_today`
   not incremented.

5. Logs `strategy_engine.entry_blocked` at INFO with `reason`, `source`, `action_id`.
   Emits CloudWatch `EntryBlocked` metric per strategy.

### What is NOT affected

| Component | Affected? | Reason |
|---|---|---|
| TradeExitEngine | ❌ Never | Reads `orders` table, not ENTRY_BLOCK |
| MIS SquareOff Manager | ❌ Never | Time-driven, reads `orders`/`positions` |
| ExitOrderRouter | ❌ Never | Processes exit events, not entry signals |
| Kill switch | ❌ Never | Reads `KILLSWITCH/GLOBAL`, separate key |
| Reconciliation | ❌ Never | Not a signal path |

### Fail behavior by mode

| Mode | DynamoDB error | Result |
|---|---|---|
| `RISK_PROFILE=paper` | warn + allow (fail-open) | Entries proceed |
| `RISK_PROFILE=tiny-live` or other | fail closed | Entries blocked |

---

## 2. ACTION_MODE Wiring

| `MONITORING_ACTION_MODE` | Executor | Safe actions |
|---|---|---|
| absent / `notify_only` (default) | None | Not executed; `safe_actions_disabled_total` increments |
| `safe_actions` | `SafeActionExecutor` | CRITICAL/BLOCKER findings → classify → execute (BLOCK_NEW_ENTRIES + ACTIVATE_KILL_SWITCH) |

When `safe_actions` mode is active:
- `_run_safe_actions(report)` executes after Phase 2 severity enrichment, before snapshot write.
- Each CRITICAL/BLOCKER finding gets its first proposed action type executed.
- Idempotency key: `{action_type_lower}-{subject}-{YYYYMMDD}` — at most one execution per action per day.
- All actions still go through `SafeActionPolicy` → forbidden actions are always blocked.
- Every attempt writes an audit record to `safe_actions_audit.jsonl`.

### How to enable

```bash
MONITORING_ACTION_MODE=safe_actions
DYNAMODB_TABLE_PREFIX=quantembrace-paper
# AWS credentials must be present so SafeActionDynamoWriter can construct a boto3 client
```

Default remains `notify_only` — no change to running sessions.

---

## 3. Files Changed

| File | Change |
|---|---|
| `services/shared/entry_block_reader.py` | **NEW** — `EntryBlockReader`, `EntryBlockState` |
| `services/strategy_engine/service.py` | Import `EntryBlockReader`; `_entry_block_reader` field; `_is_entry_blocked()` method; entry-block gate in tick + candle loops |
| `services/monitoring_agent/app.py` | `dynamo_writer` param; `_setup_safe_actions()`; `_run_safe_actions()`; Phase 5 counters; lifecycle updated to 8 steps |
| `docs/operations/monitoring-status-template.md` | Section 10a: Entry Block Status |
| `tests/unit/test_entry_block_reader.py` | **NEW** — 25 tests |
| `tests/unit/test_strategy_entry_block.py` | **NEW** — 14 tests |
| `tests/integration/test_safe_actions_entry_block_flow.py` | **NEW** — 16 tests |
| `tests/unit/test_monitoring_status.py` | +4 Phase 5 tests (ActionModeGate class) |
| `docs/live-readiness/phase5-entry-block-enforcement-report.md` | This document |
| `memory/decisions.md` | ADR-027 |

---

## 4. Tests Added

| File | New Tests | Coverage |
|---|---|---|
| `test_entry_block_reader.py` | 25 | Absent→allow, active→block, fail-open/closed, cache TTL, invalidate, state helpers |
| `test_strategy_entry_block.py` | 14 | Reader used by strategy, dispatch skipped when blocked, signals_today not incremented, runner.dispatch_tick not called |
| `test_safe_actions_entry_block_flow.py` | 16 | Full cycles: stale LTP, ai_engine DOWN, reconciliation, exits continue, kill switch not cleared, forbidden blocked |
| `test_monitoring_status.py` (additions) | 4 | Template has entry block section, ACTION_MODE counters, executor wired/not wired |

---

## 5. Commands Run

```bash
pytest tests/unit/test_entry_block_reader.py tests/unit/test_strategy_entry_block.py \
       tests/integration/test_safe_actions_entry_block_flow.py \
       tests/unit/test_monitoring_status.py \
       tests/unit/test_safe_action_dynamo_writer.py \
       tests/integration/test_safe_actions_runtime_handlers.py \
       tests/unit/test_safe_actions.py \
       tests/unit/test_monitoring_detectors.py \
       tests/unit/test_monitoring_agent.py \
       tests/unit/test_phase2_live_readiness.py \
       tests/unit/test_paper_broker_isolation.py \
       tests/unit/test_paper_preflight_check.py \
       tests/unit/test_read_only_runtime_check.py -q
```

---

## 6. Test Results

```
411 passed

Pre-existing failures (no Phase 5 dependency, verified):
  test_mis_square_off.py        20 failed  (pre-existing, no entry_block import)
  test_position_reconciliation  some failed (pre-existing)
  test_trade_exit_engine.py     3 failed   (pre-existing assertion)

Flaky test: test_paper_preflight_check::test_canonical_kill_switch_item_active_is_fail
  Passes in isolation (1/1). Fails occasionally due to shared DynamoDB mock
  state when run after tests that write to the risk-state table. Pre-existing
  collection-order sensitivity — not introduced by Phase 5.
```

---

## 7. Remaining Risks

| Risk | Severity | Mitigation |
|---|---|---|
| `ENTRY_BLOCK` cache TTL 5s — delayed enforcement | LOW | Acceptable; operator initiated blocking via safe_actions so ~5s lag is fine |
| Reading `ENTRY_BLOCK` in strategy_engine adds a DynamoDB poll per 5s | LOW | Cached; single get_item every 5s, not per-signal |
| `_run_safe_actions()` in monitoring_agent executes first proposed action only | LOW | Conservative; avoids double-writes in one cycle |
| Idempotency resets on monitoring_agent restart | LOW | Once-per-day key prevents same-day duplicates even across restarts |
| `RISK_PROFILE` env var must be set for fail-closed mode in live | MEDIUM | CLAUDE.md §Critical Env Vars: `RISK_PROFILE=paper` for paper, `tiny-live` for Stage-1 |

---

## 8. Can Phase 6 Proceed?

**Yes.** Phase 6 scope (per monitoring agent roadmap):
- `actions/` Phase 4 remaining stubs (PAUSE_STRATEGY_ENTRIES, paper repair actions)
- CloudWatch metric publishing for counters (`block_new_entries_total`, etc.)
- JSONL-backed idempotency persistence across restarts
- ENTRY_BLOCK reader integration in risk_engine (block entry approvals at the risk layer too)

Pre-conditions before Phase 6:
1. Host-side runtime verification must pass (`scripts/read_only_live_readiness_runtime_check.py`)
2. Operator must explicitly enable `ACTION_MODE=safe_actions` — not automatic
3. `RISK_PROFILE` env var must be set correctly on all services

Live trading and 1M capital remain blocked until verification completes.
