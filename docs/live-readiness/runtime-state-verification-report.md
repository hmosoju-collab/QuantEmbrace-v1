# Runtime-State Verification Report

**Phase:** Runtime Verification Phase (read-only)
**Run timestamp (UTC):** 2026-05-30T05:05:51Z
**Author:** Live-readiness runtime audit
**Status:** `RUNTIME_VERIFICATION_REQUIRED`
**Script:** `scripts/read_only_live_readiness_runtime_check.py`
**Source of truth:** Phase 2.1 hardening report + the actual captured run below.

> Binding constraints honoured by this run: live trading NOT enabled · capital limits NOT changed · nothing deployed · NO broker order placed · NO DynamoDB mutation · strictly read-only · no token value printed.

---

## 1. Final Output (the 11 required items)

| # | Item | Result |
|---|------|--------|
| 1 | **Verdict** | **`RUNTIME_VERIFICATION_REQUIRED`** (exit code `2`) |
| 2 | **Exact command run** | `python3 scripts/read_only_live_readiness_runtime_check.py --json` (from project root, `PYTHONPATH=services`) |
| 3 | **Environment inspected** | Cowork Linux sandbox — `hostname=claude`, `aarch64`, Python 3.10.12. **This is NOT the trading host.** `boto3` not installed; **all** AWS env vars unset; IMDS `169.254.169.254` unreachable; project `.env` (development) was loaded by the script. |
| 4 | **DynamoDB tables inspected** | **None queried** — connectivity is `UNKNOWN` (no boto3, no AWS path). Table *names* were resolved by string convention only (prefix `quantembrace-development`): `-sessions`, `-risk-state`, `-strategy-config`, `-orders`, `-positions`. No live-prefixed tables were observed; **paper/live separation is UNVERIFIED.** |
| 5 | **Token freshness (no value)** | `UNKNOWN` — skipped, DynamoDB unreachable. The Zerodha token row (`ZERODHA#TOKEN`/`CURRENT`) was **not read**. Token value was never read or printed. |
| 6 | **Kill-switch status** | `UNKNOWN` — skipped. Canonical key `PK=KILLSWITCH, SK=GLOBAL, attr=active` was **not read**. Cannot confirm inactive from this environment. |
| 7 | **Reconciliation status** | `UNKNOWN` — skipped. `PK=RECONCILIATION#STATE, SK=GLOBAL, field=required` was **not read**. Cannot confirm `required=false`. |
| 8 | **Strategy `paper_trade` status** | `UNKNOWN` — skipped. No `STRATEGY_CONFIG#` rows were scanned. Cannot confirm `paper_trade=true` for enabled strategies, and cannot confirm no accidental live strategy. |
| 9 | **`live_trading_enabled` value** | `QE_EXECUTION_LIVE_TRADING_ENABLED` is **absent → effective `false`** (live exits disarmed). Sourced from the development `.env`, **not** verified on the trading host. |
| 10 | **Stage-1 one-share validation** | **STILL BLOCKED.** Verdict is not PASS; the safety-critical runtime facts (token, kill switch, reconciliation, per-strategy `paper_trade`, table separation) were not verified. |
| 11 | **₹1,000,000 capital** | **STILL BLOCKED** (default). Capital limits were not changed and remain blocked; no approval, no verification. |

---

## 2. Why `RUNTIME_VERIFICATION_REQUIRED`

The runtime check is designed to inspect the **live runtime state** in DynamoDB. This run executed in the Cowork sandbox, which has **no path to the trading infrastructure**:

- `boto3` is not installed → the script's DynamoDB client cannot be constructed.
- No AWS credentials, region, profile, or endpoint env vars are set.
- The EC2 Instance Metadata Service (`169.254.169.254`) is unreachable → not an EC2 trading host.

Per the script's own rule (`runtime_reachable=false` ⇒ `RUNTIME_VERIFICATION_REQUIRED`), and per the phase rule *"If runtime access fails, mark RUNTIME_VERIFICATION_REQUIRED,"* this is the only honest verdict. The four DynamoDB-backed safety checks could not run and are reported `UNKNOWN`, not assumed safe.

**Important caveat on the two env PASSes (items 9 and `RISK_PROFILE`):** they were read from the project's development `.env` (`prefix=quantembrace-development`, `RISK_PROFILE=paper`, `QE_EXECUTION_LIVE_TRADING_ENABLED` absent). They describe the *dev config in this repo*, **not** the trading host's runtime. They must not be read as runtime confirmation.

---

## 3. Captured run (verbatim summary)

Command:

```bash
PYTHONPATH=services python3 scripts/read_only_live_readiness_runtime_check.py --json
# exit code: 2
```

Top-level JSON:

```json
{
  "utc_now": "2026-05-30T05:05:51.086136+00:00",
  "mode": "READ_ONLY",
  "runtime_reachable": false,
  "verdict": "RUNTIME_VERIFICATION_REQUIRED",
  "summary": { "fail": 0, "warn": 1, "unknown": 7, "total": 11 }
}
```

Per-check results:

| Check | Status | Note |
|-------|--------|------|
| `connectivity` | `UNKNOWN` | boto3 not installed — cannot reach DynamoDB |
| `table_names` | `PASS` | resolved by string convention from prefix `quantembrace-development` (does **not** prove tables exist or are separated) |
| `zerodha_token` | `UNKNOWN` | skipped — DynamoDB unreachable |
| `strategy_config` | `UNKNOWN` | skipped — DynamoDB unreachable |
| `kill_switch` | `UNKNOWN` | skipped — DynamoDB unreachable |
| `reconciliation` | `UNKNOWN` | skipped — DynamoDB unreachable |
| `env:live_trading_enabled` | `PASS` | absent/false (live exits disarmed) — from dev `.env` |
| `env:risk_profile` | `PASS` | `paper` — from dev `.env` |
| `env:universe_mode` | `UNKNOWN` | `UNIVERSE_MODE` unset |
| `env:execution_paper_trading` | `UNKNOWN` | unset — code default is `True` (paper sim) |
| `env:watchlist` | `WARN` | `STRATEGY_WATCHLIST_NSE` unset — candle strategies emit no signals |

---

## 4. Verdict-rule application

Each phase decision rule, mapped to what this run observed:

| Rule | Triggered? | Why |
|------|-----------|-----|
| Runtime access fails → `RUNTIME_VERIFICATION_REQUIRED` | **YES** | boto3 absent, no AWS path, IMDS unreachable — this is the governing outcome. |
| Token stale/missing → `FAIL` | No (N/A) | Token row was not read; status `UNKNOWN`, not asserted safe. |
| Kill switch active → `FAIL` | No (N/A) | Kill-switch row was not read; status `UNKNOWN`. |
| Enabled strategy `paper_trade=false` (no approval) → `FAIL` | No (N/A) | strategy-config not scanned; status `UNKNOWN`. |
| `live_trading_enabled=true` → `FAIL` | No | Observed **false** (absent). |
| Paper/live tables not clearly separated → `FAIL` | No (unverified) | Only the dev prefix was resolvable; separation could **not** be confirmed. Unverified ≠ positively-mixed, so no FAIL is asserted — but this **must be verified on the host** before GO. |
| Do not mark GO unless every check PASS | **NOT GO** | 7 of 11 checks are `UNKNOWN`. |

No check returned `FAIL`. The blocker is unverifiability, not a detected unsafe state.

---

## 5. Read-only safety properties of the script (by construction)

- **Read-only DynamoDB.** All access goes through `_ReadOnlyDynamo`, which exposes only `get_item`/`query`/`scan`/`batch_get_item`/`list_tables`/`describe_table`. `put_item`, `update_item`, `delete_item`, `batch_write_item`, `transact_write_items`, table create/delete/update, and any non-allow-listed attribute raise `RuntimeError`. Mutation is impossible even by accident.
- **No live enable.** It never writes or toggles any live-trading flag.
- **No orders.** It never constructs a broker client and never calls `place_order`.
- **No secrets.** The Zerodha token row is read for metadata only (presence, `expires_at`, freshness). The `access_token` value is never read into any printed/logged/emitted variable.

---

## 6. To close the gap — operator run on the trading host

Run from the project root on the host with DynamoDB access (instance role / AWS profile, or LocalStack for paper):

```bash
# Paper stack (LocalStack)
AWS_ENDPOINT_URL=http://localhost:4566 \
DYNAMODB_TABLE_PREFIX=quantembrace-development \
RISK_PROFILE=paper UNIVERSE_MODE=PAPER_SAFE_START \
python scripts/read_only_live_readiness_runtime_check.py --json

# Real AWS (read-only; uses instance role / AWS profile)
AWS_REGION=ap-south-1 \
AWS_DYNAMODB_TABLE_PREFIX=quantembrace-production \
python scripts/read_only_live_readiness_runtime_check.py --json
```

Verdict stays `RUNTIME_VERIFICATION_REQUIRED` until an operator records here, from a real run:

- [ ] Zerodha token present **and fresh** (`expires_at` in the future; refreshed same trading day via `scripts/zerodha_login.py`).
- [ ] Every **enabled** strategy in strategy-config has `paper_trade=True` (unless a specific Stage-1 one-share strategy is deliberately, manually promoted under separate sign-off).
- [ ] Kill switch **INACTIVE** (`PK=KILLSWITCH, SK=GLOBAL, active=false`).
- [ ] `reconciliation_required` **False** (or a clean reconcile has been run).
- [ ] Mode env on the host: `QE_EXECUTION_LIVE_TRADING_ENABLED` absent/false; `RISK_PROFILE`, `UNIVERSE_MODE` match the intended stage.
- [ ] **Paper vs live table names confirmed distinct** (the script prints resolved names; confirm paper mode is not pointed at live tables).

Until those boxes are checked from a real host run, **no GO. Stage-1 one-share validation and ₹1,000,000 capital remain BLOCKED.**

---

## 7. Secondary finding (resolved this session)

`scripts/deploy/paper_preflight_check.py` previously read the kill switch at the wrong key (`PK=KILL_SWITCH#GLOBAL, SK=STATE, field=state`), which never matched the production row written at `PK=KILLSWITCH, SK=GLOBAL` (`active`/`status`). An active switch would have been silently reported PASS.

**Status: FIXED.** The check now reads the canonical key via `shared.risk_state.kill_switch_key()` / `attr_bool(..., "active")` (with a hardcoded fallback to the same key), extracted into `check_kill_switch(dynamo, prefix, report)`. Regression coverage added in `tests/unit/test_paper_preflight_check.py` (9 tests, all passing), including a test proving the old key would have missed an active switch.
