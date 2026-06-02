# Phase 7.2 — Runtime Host Verification Report

_Date: 2026-05-31T05:45:06Z · Author: Chief Architect_

---

## Verdict: RUNTIME_VERIFICATION_REQUIRED

**Phase 8 remains blocked.** The runtime verification script was executed in the development sandbox (not the actual AWS/EC2 trading host). DynamoDB is unreachable from this environment — `AWS_ENDPOINT_URL=http://localhost:4566` points to a LocalStack instance that is not running. The DynamoDB-dependent checks (token, kill switch, ENTRY_BLOCK, reconciliation, strategy config) are all UNKNOWN.

Per the established protocol: if any of token, kill switch, ENTRY_BLOCK, reconciliation, or strategy config is UNKNOWN, the verdict cannot be PASS. Phase 8 cannot begin until this script returns `RUNTIME_STATE_PAPER_SAFE` on the actual trading host.

---

## 1. Command Run

```bash
export PYTHONPATH=services
python3 scripts/read_only_live_readiness_runtime_check.py --json \
  | tee runtime_check_20260531_054506.json
```

Script exit code: **2** (`RUNTIME_VERIFICATION_REQUIRED`)

---

## 2. Hostname / Environment

| Field | Value |
|---|---|
| Hostname | `claude` (development sandbox — NOT the trading host) |
| Environment | `QE_ENVIRONMENT=development` |
| AWS endpoint | `AWS_ENDPOINT_URL=http://localhost:4566` (LocalStack — not running) |
| Region | `ap-south-1` |
| DynamoDB prefix | `quantembrace-development` |

**This is not the trading host.** The trading host is an AWS EC2 ARM64 instance in `ap-south-1` with an instance IAM role granting DynamoDB access. The sandbox has no instance role and no running LocalStack.

---

## 3. AWS Region / Account

Not determinable from the sandbox. On the trading host, these will resolve via:

```bash
aws sts get-caller-identity  # confirms account + ARN (read-only, no secret printed)
```

Expected on trading host:
- Region: `ap-south-1`
- Account: per Terraform state bucket `quantembrace-terraform-state`
- Identity: EC2 instance role `quantembrace-prod-<service>-role`

---

## 4. DynamoDB Tables Inspected

The script resolved table names from the `.env` prefix `quantembrace-development`:

| Table | Name |
|---|---|
| sessions | `quantembrace-development-sessions` |
| risk_state | `quantembrace-development-risk-state` |
| strategy_config | `quantembrace-development-strategy-config` |
| orders | `quantembrace-development-orders` |
| positions | `quantembrace-development-positions` |

On the trading host, these will resolve from `AppSettings` (pydantic_settings), using the production prefix. Paper and live tables use separate namespace prefixes — no mixing.

**DynamoDB connectivity: UNKNOWN** — endpoint unreachable from sandbox.

---

## 5. Zerodha Token Freshness

**Status: UNKNOWN** — DynamoDB unreachable; token row could not be read.

Token value was never read, printed, or emitted at any point — the script's `_ReadOnlyDynamo` proxy enforces read-only access, and the `check_zerodha_token()` function reads only `token_present` (bool), `expires_at`, and `created_at` — never the token value itself.

On the trading host, expected result: **PASS** (token fresh, expires after 07:30 IST next day) — assuming `scripts/zerodha_login.py` was run today.

---

## 6. Kill Switch Status

**Status: UNKNOWN** — DynamoDB unreachable.

Read path: `KILLSWITCH / GLOBAL` in `<prefix>-risk-state` table, attribute `active`.

On the trading host, expected result: **PASS** (`active=False` — kill switch has not been activated by operator or auto-trigger during paper sessions).

---

## 7. ENTRY_BLOCK Status

**Status: UNKNOWN** — DynamoDB unreachable.

Read path: `ENTRY_BLOCK / GLOBAL` in `<prefix>-risk-state` table, attribute `blocked`.

On the trading host, expected result: **PASS** (`blocked=False` or item absent — no active entry block). If an ENTRY_BLOCK was written during a paper session and not cleared, this will show as WARN (not FAIL) since entry block is not a safety failure, but the operator must confirm it is intentional.

---

## 8. Reconciliation Status

**Status: UNKNOWN** — DynamoDB unreachable.

Read path: `RECONCILIATION#STATE / GLOBAL` in `<prefix>-risk-state` table, attribute `required`.

On the trading host, expected result: **PASS** (`required=False` or item absent — no reconciliation halt active). If `required=True`, a paper session reconciliation issue was flagged and not cleared, which would be a **WARN** — new entries would be blocked by risk_engine until operator clears it.

---

## 9. Strategy Config / paper_trade Status

**Status: UNKNOWN** — DynamoDB unreachable.

Read path: Scan `<prefix>-strategy-config` for `PK begins_with STRATEGY_CONFIG#`. Each row checked for `enabled=True AND paper_trade=False` (would be a **FAIL**).

On the trading host, expected result: **PASS** — all enabled strategies have `paper_trade=True` in the paper session configuration. No strategy should have `paper_trade=False` unless explicitly staged for Stage-1 with operator sign-off.

---

## 10. live_trading_enabled

**Status: PASS** ✅

`QE_EXECUTION_LIVE_TRADING_ENABLED` is absent/false in the sandbox `.env`. This check passed in the sandbox because it reads environment variables, not DynamoDB.

On the trading host (EC2 userdata), this variable must remain absent or `false` until Stage-1 operator gate sign-off.

---

## 11. Risk Profile

**Status: PASS** ✅ (env check, runs in sandbox)

`RISK_PROFILE=paper` confirmed in `.env`. On the trading host, this is injected via `risk_engine.sh` EC2 userdata.

---

## 12. Paper / Live Table Separation

Table names are namespace-separated by the `DYNAMODB_TABLE_PREFIX` environment variable:
- Development/local: `quantembrace-development-*`
- Staging: `quantembrace-staging-*`
- Production: `quantembrace-prod-*` (set by AppSettings from pydantic_settings)

Paper and live sessions use separate keys within the risk-state table (e.g. `ENTRY_BLOCK/GLOBAL` is not mode-scoped — it applies globally). Paper broker routes are isolated at the `paper_trade=True` flag level in strategy_config and execution_engine routing. `PaperSimulator` is the only broker called when `paper_trade=True`.

**Separation confirmed by code inspection.** DynamoDB table name correctness requires trading host verification.

---

## 13. Full Check Results (sandbox run)

| Check | Status | Detail |
|---|---|---|
| connectivity | UNKNOWN | LocalStack not running; DynamoDB unreachable |
| table_names | PASS | Resolved from env prefix `quantembrace-development` |
| zerodha_token | UNKNOWN | Skipped — DynamoDB unreachable |
| strategy_config | UNKNOWN | Skipped — DynamoDB unreachable |
| kill_switch | UNKNOWN | Skipped — DynamoDB unreachable |
| reconciliation | UNKNOWN | Skipped — DynamoDB unreachable |
| env:live_trading_enabled | **PASS** | Absent/false |
| env:risk_profile | **PASS** | `paper` |
| env:universe_mode | UNKNOWN | Unset (must be `PAPER_SAFE_START` on trading host) |
| env:execution_paper_trading | UNKNOWN | Unset (code default: True) |
| env:watchlist | WARN | `STRATEGY_WATCHLIST_NSE` unset |

Summary: **0 FAIL · 1 WARN · 7 UNKNOWN · 2 PASS**

---

## 14. Required Action on Trading Host

The operator must run the following on the actual AWS EC2 trading host after all services are started and the daily Zerodha login is complete:

```bash
# On the actual trading host — NOT the development sandbox
cd /opt/quantembrace   # or wherever the repo is deployed

# Run today's Zerodha login first (if not already done)
# python scripts/zerodha_login.py

# Then run the read-only check
export PYTHONPATH=services
python3 scripts/read_only_live_readiness_runtime_check.py --json \
  | tee runtime_check_$(date +%Y%m%d_%H%M%S).json

echo "Exit code: $?"
```

**Expected output on PASS:**
```json
{
  "verdict": "RUNTIME_STATE_PAPER_SAFE",
  "summary": { "fail": 0, "warn": 0, "unknown": 0 }
}
```
Exit code 0.

All 10 checks must be PASS or WARN (no UNKNOWNs except acceptable low-risk ones like `env:watchlist`). Specifically, these 6 must not be UNKNOWN:
1. `connectivity` → PASS
2. `zerodha_token` → PASS (fresh) or WARN (expired → re-login)
3. `kill_switch` → PASS (inactive)
4. `reconciliation` → PASS (not required)
5. `strategy_config` → PASS (all paper_trade=True)
6. `env:live_trading_enabled` → PASS

---

## 15. Whether Phase 8 Can Proceed

**NO. Phase 8 cannot begin.**

Verdict is `RUNTIME_VERIFICATION_REQUIRED`. Per the decision rules:

> If verdict is RUNTIME_VERIFICATION_REQUIRED, Phase 8 remains blocked.

Phase 8 (final live-readiness validation, pre-live checklist, Stage-1 dry run) may begin **as planning and config-pack preparation only** once the trading host returns `RUNTIME_STATE_PAPER_SAFE`. No production changes may be made until that verdict is achieved.

---

## 16. Whether Stage-1 Live Validation Remains Blocked

**YES. Stage-1 live validation remains blocked.**

Stage-1 requires:
1. Trading host runtime verification → `RUNTIME_STATE_PAPER_SAFE` (**not achieved**)
2. All Phase 8 gates (not yet started)
3. `terraform apply` to provision production infrastructure
4. Full pre-live-runbook checklist sign-off
5. Explicit operator gate: `QE_EXECUTION_LIVE_TRADING_ENABLED=true`
6. `RISK_PROFILE` changed from `paper` to `tiny-live`
7. Capital deployment decision

None of these gates are met.

---

## 17. Whether ₹1M Capital Remains Blocked

**YES. ₹1,000,000 remains blocked.**

`PAPER_SEED_NAV=1000000` is the paper simulation seed only. Live capital deployment requires all of:
- Full Phase 8 completion
- `evaluate_promotion_gate.py --gate PAPER_SAFE_START_TO_EXPAND` → PASS
- Operator sign-off (manual gate, never automatic)
- `QE_EXECUTION_LIVE_TRADING_ENABLED=true` (currently absent/false — hard block)

No capital has been modified. No broker orders have been placed. Live trading remains disabled.

---

## Summary

| Item | Status |
|---|---|
| Script executed | ✅ Yes (sandbox environment) |
| DynamoDB reachable | ❌ No (LocalStack not running) |
| Token, kill switch, reconciliation, strategy config | ❌ UNKNOWN (requires trading host) |
| live_trading_enabled | ✅ PASS (absent/false) |
| risk_profile | ✅ PASS (paper) |
| Overall verdict | ⚠ **RUNTIME_VERIFICATION_REQUIRED** |
| Phase 8 can proceed | ❌ **NO** |
| Stage-1 live validation | ❌ **BLOCKED** |
| ₹1M capital | ❌ **BLOCKED** |

**Next operator action:** SSH into the trading EC2 host, ensure services are running and today's Zerodha token is fresh, then run the verification script. Share the output JSON. If verdict is `RUNTIME_STATE_PAPER_SAFE`, Phase 8 planning can begin immediately.
