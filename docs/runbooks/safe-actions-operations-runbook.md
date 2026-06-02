# Safe Actions Operations Runbook

_Owner: Operator · Last updated: 2026-05-31 · Scope: Phase 6+_

This runbook covers day-to-day operator procedures for the safe_actions layer, entry-block flag management, and related DynamoDB state.

**Claude may not execute any step in this runbook autonomously.** Every command requires a human operator.

---

## 1. Check Current Safe Actions State

```bash
# Monitoring status (includes §10a Entry Block + §10b Safe Actions)
python scripts/monitoring/paper_trading_monitor.py --once

# Tail live audit log
tail -n 50 /app/data/safe_actions_audit.jsonl | jq .

# Show today's idempotency keys (actions already executed today)
aws dynamodb query \
  --table-name ${DYNAMODB_TABLE_PREFIX}-risk-state \
  --key-condition-expression "PK = :pk" \
  --expression-attribute-values '{":pk":{"S":"SAFE_ACTION_IDEMPOTENCY"}}' \
  --query 'Items[*].{SK:SK.S,action_type:action_type.S,status:status.S,created_at:created_at.S}'
```

---

## 2. Check Entry Block State

```bash
# Read the ENTRY_BLOCK/GLOBAL flag
aws dynamodb get-item \
  --table-name ${DYNAMODB_TABLE_PREFIX}-risk-state \
  --key '{"PK":{"S":"ENTRY_BLOCK"},"SK":{"S":"GLOBAL"}}' \
  --query 'Item.{blocked:blocked.BOOL,reason:reason.S,source:source.S,action_id:action_id.S,created_at:created_at.S}'
```

Expected when active:
```json
{
  "blocked": true,
  "reason": "stale_ltp",
  "source": "safe_actions",
  "action_id": "act-...",
  "created_at": "2026-05-31T..."
}
```

Expected when not active (item absent or blocked=false):
```json
{}
```

---

## 3. Clear Entry Block (Manual Only)

**Prerequisites before clearing:**
1. Verify the underlying issue is resolved (e.g. LTP feed is fresh, connectivity restored).
2. Confirm exits are proceeding normally.
3. Confirm kill switch is inactive: `python scripts/kill_switch_cli.py status`
4. Confirm paper mode: check `RISK_PROFILE=paper` in risk_engine env.

```bash
# Clear the entry block flag
aws dynamodb delete-item \
  --table-name ${DYNAMODB_TABLE_PREFIX}-risk-state \
  --key '{"PK":{"S":"ENTRY_BLOCK"},"SK":{"S":"GLOBAL"}}'

# Verify cleared
aws dynamodb get-item \
  --table-name ${DYNAMODB_TABLE_PREFIX}-risk-state \
  --key '{"PK":{"S":"ENTRY_BLOCK"},"SK":{"S":"GLOBAL"}}'
# Expected: {} (item absent)
```

After clearing, strategy_engine and risk_engine both re-read within their 5s cache TTL. New entries resume automatically — no service restart required.

---

## 4. Inspect Audit Log

```bash
# Show all executed actions today
cat /app/data/safe_actions_audit.jsonl | jq 'select(.executed == true)'

# Show all blocked actions
cat /app/data/safe_actions_audit.jsonl | jq 'select(.blocked == true)'

# Show all idempotency skips
cat /app/data/safe_actions_audit.jsonl | jq 'select(.idempotency_skipped == true)'

# Show all DynamoDB write failures
cat /app/data/safe_actions_audit.jsonl | jq 'select(.error != null)'

# Show audit for a specific action_id
cat /app/data/safe_actions_audit.jsonl | jq 'select(.action_id == "<action_id>")'
```

---

## 5. Verify Idempotency Key State

```bash
# Check if a specific idempotency key has been executed
aws dynamodb get-item \
  --table-name ${DYNAMODB_TABLE_PREFIX}-risk-state \
  --key "{\"PK\":{\"S\":\"SAFE_ACTION_IDEMPOTENCY\"},\"SK\":{\"S\":\"<idempotency_key>\"}}" \
  --query 'Item.{status:status.S,action_id:action_id.S,created_at:created_at.S}'
```

If you need to allow re-execution of a previously-executed idempotency key (e.g. to re-block after clearing), delete the idempotency key:

```bash
# Delete an idempotency key to allow re-execution (OPERATOR ONLY)
# Example: "block_new_entries-stale_ltp-20260531"
aws dynamodb delete-item \
  --table-name ${DYNAMODB_TABLE_PREFIX}-risk-state \
  --key '{"PK":{"S":"SAFE_ACTION_IDEMPOTENCY"},"SK":{"S":"block_new_entries-stale_ltp-20260531"}}'
```

---

## 6. Kill Switch Operations

See `docs/runbooks/kill-switch-runbook.md` for kill switch activation/deactivation.

For reference — check kill switch state:
```bash
python scripts/kill_switch_cli.py status
```

Safe_actions ACTIVATE_KILL_SWITCH idempotency key format: `activate_kill_switch-<subject>-<YYYYMMDD>`.

---

## 7. Check CloudWatch Metrics

```bash
# Safe actions executed today
aws cloudwatch get-metric-statistics \
  --namespace QuantEmbrace/SafeActions \
  --metric-name safe_actions.executed_total \
  --start-time $(date -u -d 'today 00:00' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 86400 --statistics Sum

# Entry block gauge (should be 0 if not active)
aws cloudwatch get-metric-statistics \
  --namespace QuantEmbrace/EntryBlock \
  --metric-name strategy.entry_block_active \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 3600 --statistics Maximum
```

---

## 8. Run Tests After Any Change

```bash
# Phase 6 safe_actions tests
pytest tests/unit/test_safe_action_idempotency.py \
       tests/unit/test_safe_actions_metrics.py \
       tests/unit/test_risk_engine_entry_block.py \
       tests/unit/test_monitoring_status.py -v

# Full safety suite
pytest tests/unit/test_safe_actions.py \
       tests/unit/test_safe_action_dynamo_writer.py \
       tests/unit/test_entry_block_reader.py \
       tests/unit/test_strategy_entry_block.py \
       tests/integration/test_safe_actions_runtime_handlers.py \
       tests/integration/test_safe_actions_entry_block_flow.py \
       tests/unit/test_exit_order_router.py \
       tests/unit/test_paper_broker_isolation.py \
       tests/unit/test_paper_preflight_check.py -v
```

---

## 9. Monitoring Status GREEN Conditions (Phase 6)

The monitoring status is GREEN only when ALL of the following hold:
- `TradingMode=PAPER`
- `live_trading_enabled=False`
- `TradeExitEngine` active
- `ExitOrderRouter` active
- `MIS Square-Off` armed
- Reconciliation ran or safely skipped
- No unmanaged positions
- No critical alerts
- Daily caps do not block exits
- **NEW (Phase 6):** If ENTRY_BLOCK active, it is intentional and exits are healthy (status is AMBER, not RED)
- **NEW (Phase 6):** If `ACTION_MODE=safe_actions`, `executor_active=True`

---

## 10. Escalation

If any of the following occur, stop trading immediately and escalate:
- Kill switch is active and positions are open — exits must proceed first
- ENTRY_BLOCK DynamoDB read failure in live profile (fail-closed)
- Audit write failures for write actions (`AuditWriteFailed` in error field)
- `live_trading_enabled=True` detected in paper session
- Any live broker order attempt recorded (`router_live_attempts > 0`)
