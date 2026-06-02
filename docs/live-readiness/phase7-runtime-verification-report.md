# Phase 7A — Runtime Verification Report

_Date: 2026-05-31 · Author: Chief Architect · Environment: Development sandbox (not trading host)_

---

## Verdict: RUNTIME_VERIFICATION_REQUIRED

**Exit code: 2** — DynamoDB unreachable from sandbox. State could not be fully verified. Per Q4 protocol, this is **not a PASS** and **not a FAIL**. Runtime verification must be re-run on the actual AWS/trading host with live credentials before Stage-1 can proceed.

---

## Command Run

```bash
export PYTHONPATH=services
python3 scripts/read_only_live_readiness_runtime_check.py --json \
  | tee runtime_check_20260531_051749.json
```

Script guarantees (by construction):
- Read-only: `_ReadOnlyDynamo` proxy raises `RuntimeError` on any write call.
- No live enable: never sets, writes, or toggles any live-trading flag.
- No orders: never constructs a broker client.
- No secrets: Zerodha token value is never read into a variable that is printed.

---

## Check Results

| Check | Status | Finding |
|---|---|---|
| connectivity | UNKNOWN | DynamoDB endpoint `http://localhost:4566` unreachable — sandbox has no LocalStack running |
| table_names | PASS | Resolved via `env-prefix:quantembrace-development`: sessions, risk_state, strategy_config all named correctly |
| zerodha_token | UNKNOWN | Skipped — DynamoDB unreachable |
| strategy_config | UNKNOWN | Skipped — DynamoDB unreachable |
| kill_switch | UNKNOWN | Skipped — DynamoDB unreachable |
| reconciliation | UNKNOWN | Skipped — DynamoDB unreachable |
| env:live_trading_enabled | **PASS** | `QE_EXECUTION_LIVE_TRADING_ENABLED` absent/false — live exits disarmed |
| env:risk_profile | **PASS** | `RISK_PROFILE=paper` |
| env:universe_mode | UNKNOWN | `UNIVERSE_MODE` unset — must be `PAPER_SAFE_START` or `PAPER_EXPAND` on trading host |
| env:execution_paper_trading | UNKNOWN | `EXECUTION_PAPER_TRADING` unset — code default is `True` (paper sim); must be confirmed on host |
| env:watchlist | WARN | `STRATEGY_WATCHLIST_NSE` unset — candle strategies emit no signals until set |

Summary: **0 FAIL · 1 WARN · 7 UNKNOWN · 2 PASS · 1 PASS (table_names)**

---

## Environment Variables: Sandbox vs Trading Host

The two env vars that ran cleanly in sandbox and **must remain correct on the trading host**:

| Variable | Required Value | Sandbox Result |
|---|---|---|
| `QE_EXECUTION_LIVE_TRADING_ENABLED` | absent or `false` | ✅ PASS |
| `RISK_PROFILE` | `paper` | ✅ PASS |

The three unset vars **must be explicitly set on the trading host before any session**:

| Variable | Required Value | Action |
|---|---|---|
| `UNIVERSE_MODE` | `PAPER_SAFE_START` | Set in EC2 userdata / `execution_engine.sh` |
| `EXECUTION_PAPER_TRADING` | `true` | Set explicitly (code default is True, but make it explicit) |
| `STRATEGY_WATCHLIST_NSE` | comma-separated NSE symbols | Set before strategy_engine start; absent → no signals |

---

## Required Action on Trading Host

Run the following on the actual AWS/EC2 trading host with IAM role active and all services running:

```bash
# On the trading host (NOT this sandbox)
cd /opt/quantembrace
export PYTHONPATH=services
python3 scripts/read_only_live_readiness_runtime_check.py --json \
  | tee runtime_check_$(date +%Y%m%d_%H%M%S).json
```

Expected result for PASS: `"verdict": "RUNTIME_STATE_PAPER_SAFE"`, exit code 0.

All 10 checks must resolve before the verdict can be PASS. Specifically verify:

1. **connectivity** → PASS (DynamoDB reachable via instance role)
2. **table_names** → PASS (resolved via AppSettings, not env-prefix fallback)
3. **zerodha_token** → PASS (fresh token present; token value never printed)
4. **strategy_config** → PASS (all enabled strategies have `paper_trade=True`)
5. **kill_switch** → PASS (KILLSWITCH/GLOBAL active=False)
6. **reconciliation** → PASS (RECONCILIATION#STATE/GLOBAL required=False)
7. **env:live_trading_enabled** → PASS (absent or false)
8. **env:risk_profile** → PASS (paper)
9. **env:universe_mode** → PASS (PAPER_SAFE_START)
10. **env:execution_paper_trading** → PASS (true)

If any check is FAIL or UNKNOWN → **do not proceed to Stage-1**.

---

## What This Verification Cannot Confirm

- Actual DynamoDB table contents (kill switch, reconciliation, token freshness)
- Strategy config `paper_trade` flags in the live tables
- Whether the Zerodha token was refreshed today (`scripts/zerodha_login.py`)
- Kafka MSK connectivity from the EC2 instances
- NAV seeded correctly in DynamoDB

All of the above require the trading host runtime.

---

## Conclusion

**RUNTIME_VERIFICATION_REQUIRED.** Phase 7A cannot be marked PASS from this environment. The env checks that could run (live_trading_enabled, risk_profile) pass correctly. No FAIL conditions detected. All DynamoDB-dependent checks are deferred to the trading host run.

**Phase 8 is blocked until trading host runtime verification returns RUNTIME_STATE_PAPER_SAFE.**
