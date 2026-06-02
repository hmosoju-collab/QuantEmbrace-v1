# Kill Switch Runbook

_Last updated: 2026-05-31 · Owner: Operator · Human approval required for deactivation_

---

## What the kill switch does

When ACTIVE: risk_engine's `KillSwitchCache` (1s DynamoDB poll) sets `active=True`
and every subsequent signal validation returns `REJECTED`. No new orders reach the
execution_engine. Exit fills, TEE, and MIS square-off are NOT blocked by the kill
switch (they operate on fills and scheduled events, not on signals).

When INACTIVE: normal signal flow resumes.

---

## DynamoDB key

Table: `{prefix}-risk-state`
```
PK = "KILLSWITCH"
SK = "GLOBAL"
```

---

## Check kill switch status

```bash
# Via CLI
python scripts/kill_switch_cli.py status

# Via DynamoDB directly (local dev)
aws dynamodb get-item \
  --endpoint-url http://localhost:4566 \
  --table-name quantembrace-development-risk-state \
  --key '{"PK":{"S":"KILLSWITCH"},"SK":{"S":"GLOBAL"}}'
```

Expected output when INACTIVE:
```json
{ "Item": { "active": {"BOOL": false}, "status": {"S": "INACTIVE"} } }
```

Expected output when ACTIVE:
```json
{
  "Item": {
    "active": {"BOOL": true},
    "status": {"S": "ACTIVE"},
    "reason": {"S": "..."},
    "activated_by": {"S": "..."},
    "activated_at": {"S": "..."}
  }
}
```

---

## Activate kill switch (manual)

```bash
python scripts/kill_switch_cli.py activate --reason "describe the reason here"
```

The CLI writes to DynamoDB (`KILLSWITCH/GLOBAL`) and publishes to SNS. The
risk_engine polls every 1 second and will halt new signal approvals within ~1s.

---

## Activate kill switch (via safe_actions — autonomous, Phase 4)

When `SafeActionExecutor` executes `ACTIVATE_KILL_SWITCH`, it writes
`KILLSWITCH/GLOBAL active=True` directly via `SafeActionDynamoWriter`. This uses
the same canonical schema as the manual CLI path. The `activated_by` field will
show `"safe_actions"` and the `detail` field will contain the `action_id` and
`idempotency_key` for the audit trail.

---

## Deactivate kill switch (HUMAN APPROVAL REQUIRED — never autonomous)

Deactivating the kill switch is not an approved safe action. It must be done
manually by the operator after confirming:

- [ ] Root cause of activation identified and resolved
- [ ] No open unmanaged positions
- [ ] Reconciliation is clean (`python scripts/ops/reconcile.py`)
- [ ] No active incidents in the monitoring agent

```bash
# Confirm root cause resolved first, then:
python scripts/kill_switch_cli.py deactivate --reason "describe resolution here"
```

The CLI requires an explicit confirmation string (`--yes` or interactive prompt).

---

## Verify kill switch is working

After activation, confirm risk_engine halted approvals:

```bash
# Check risk_engine logs for kill switch signal
docker-compose logs risk_engine | grep -E "kill_switch|REJECTED|killswitch"
# Expected: "kill_switch_active=True" on signal validation log lines

# Or check CloudWatch Logs
# /quantembrace/prod/risk-engine → filter "kill_switch_active"
```

---

## Kill switch written by safe_actions — audit trail

Every safe_action activation writes to `safe_actions_audit.jsonl`:

```json
{
  "action_type": "ACTIVATE_KILL_SWITCH",
  "executed": true,
  "mode": "PAPER",
  "reason": "unmanaged_live_position",
  "idempotency_key": "ks-unmanaged-20260531",
  "result_payload": {
    "dynamodb_pk": "KILLSWITCH",
    "dynamodb_sk": "GLOBAL",
    "active": true
  }
}
```

Audit log location: `/app/data/safe_actions_audit.jsonl`

---

## Post-incident checklist

After deactivation:

- [ ] Confirm monitoring agent shows GREEN status
- [ ] Run `python scripts/deploy/paper_preflight_check.py` (must exit 0)
- [ ] Confirm no signals backed up in Kafka (check consumer lag)
- [ ] Confirm DynamoDB `ENTRY_BLOCK/GLOBAL` is not blocking entries
- [ ] Run `python scripts/ops/reconcile.py` to confirm position state
- [ ] Zerodha token is fresh before next trading session
