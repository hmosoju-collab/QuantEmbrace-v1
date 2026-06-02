# Phase 2 — Open-Questions Verification Report

**Date:** 2026-05-30
**Scope:** The five Phase-2 open questions (Q1–Q5), verified against code, configs, and tests only.
**Binding rules honored:** No live trading enabled. No capital limits changed. No deploy. No broker orders. No DynamoDB mutation. Findings are repo-evidence-based; anything not verifiable from the repo is marked `RUNTIME_VERIFICATION_REQUIRED`. One-fix-per-PR; unsafe live behavior would be fixed fail-closed (none required — see below).

---

## 1. Summary verdict

**Code-path verification: PASS.** All five open questions were resolved against production code. The live path fails **closed** on missing/unknown risk data, the reconciliation halt is wired as a **hard reject** for live new-entries, and the paper entry path provably **never reaches a live broker**.

**Runtime verification: `RUNTIME_VERIFICATION_REQUIRED` (Q4).** A repo-only audit cannot read the live/paper DynamoDB tables, and must not connect to real infrastructure. The runtime state (token freshness, per-strategy `paper_trade`, kill-switch, reconciliation flag, mode env) is unverified until an operator runs the delivered read-only script on the trading host.

**Overall: NOT GO for Stage-1 live.** Code is ready on the audited questions; the GO decision remains blocked on (a) the Q4 runtime run and (b) the residual items in §9. This is consistent with "do not mark GO if runtime-only items were not verified."

One Phase-1 finding was **corrected** during this work (see Q2): the earlier claim that registry-load failure caused fail-**open** in live was wrong; the code fails **closed**.

---

## 2. Open-question status

| Q | Topic | Status | Verdict |
|---|-------|--------|---------|
| Q1 | `reconciliation_validator` wiring | **PASS** | `HARD_HALT` for live new-entries; wired into the risk chain; paper + closeout exempt. Residual: fails **open** on DynamoDB read error (by design; kill switch is the backstop). |
| Q2 | Registry-load failure (HIGH-003) fail-closed | **PASS** | Live fails **closed** on unknown sector / missing risk data; paper warns + approves. **Corrects** Phase-1's fail-open claim. Lock-in tests added. |
| Q3 | `_get_broker(market)` live branch reachable in paper? | **PASS** | Paper entry **never** reaches a live broker. New residual (LOW–MED): restart reconciliation queries real broker status for `PARTIALLY_FILLED` paper orders — read-only, exception-guarded, but violates strict paper-isolation. Fix recommended, not auto-applied. |
| Q4 | Read-only runtime verification | **`RUNTIME_VERIFICATION_REQUIRED`** | Strictly read-only script delivered + companion report. Runtime not reachable from this audit → not verified → no GO. |
| Q5 | Audit `test_phase8_hardening.py` | **PASS** | Suite mapped; gaps identified; two highest-value fail-closed gaps filled with new tests (Q2 lock-in). |

---

### Q1 — Reconciliation validator: HARD_HALT, wired

**Files:** `services/risk_engine/service.py` (constructs `ReconciliationValidator` at lines 270–274; runs it in the `validate_signal` chain, short-circuiting to REJECTED on the first failure); `services/risk_engine/validators/reconciliation_validator.py` (full); `services/shared/risk_state.py` (`reconciliation_key()` → `PK=RECONCILIATION#STATE`, `SK=GLOBAL`).

**Behavior (verified in code):**

- `required=True` → `RiskValidationResult(approved=False, reason="RECONCILIATION_REQUIRED: …")` → the risk chain short-circuits → signal REJECTED. This is a **HARD_HALT**, not advisory.
- **Paper exempt:** `paper_trade=True` → approved with reason `paper_trade_exempt` (validate(), lines 104–109). Paper does not halt.
- **Closeout exempt:** `metadata["is_closeout"]=True` → approved with reason `closeout_exempt` (lines 110–115). **Exit / position-reducing orders are never blocked by the reconciliation halt** — this satisfies the rule "exit/risk-reduction orders must not be blocked by entry-safety failures."
- Flag read is `ConsistentRead=True`, cached 1s (`_refresh_if_stale`, lines 155–185).

**Residual (documented, by design):** on a DynamoDB read exception the validator sets `_required=False` (fails **open**) so a transient outage doesn't halt all trading (lines 176–183); the comment names the kill switch as the harder backstop. This is an accepted trade-off, not a defect, but it means reconciliation protection depends on DynamoDB availability. Recommend a runtime alarm on persistent reconciliation-read failures (§9).

### Q2 — Registry-load failure (HIGH-003): live fails CLOSED

**Files:** `services/risk_engine/validators/common.py` (full); `services/risk_engine/validators/sector_validator.py` (full); `services/risk_engine/service.py` (lines 245–274 — registry load + validator construction); `services/risk_engine/context/risk_context_builder.py` (`_fetch_adv`, `_fetch_live_spread_bps`).

`risk_data_unavailable_result()` is the shared mode-aware primitive: paper → `approved=True`, reason `PAPER_WARN_RISK_DATA_UNAVAILABLE`; live → `approved=False`, reason `LIVE_RISK_DATA_UNAVAILABLE`. `is_paper_signal()` defaults to **False** (i.e. treated as live) when the attribute is absent — fail-closed default.

| Validator | `registry=None` behavior | PAPER | LIVE | Safe? | Fix |
|-----------|--------------------------|-------|------|-------|-----|
| Sector (`sector_validator.py`) | `_get_sector()` returns `"UNKNOWN"`; UNKNOWN sector → `risk_data_unavailable_result()` | approve + warn | **REJECT** (`LIVE_RISK_DATA_UNAVAILABLE`) | ✅ | none |
| Spread (`spread_validator`) | registry-**independent**; spread comes from context (DynamoDB). Missing spread → `risk_data_unavailable_result()` | approve + warn | **REJECT** | ✅ | none |
| Liquidity (`liquidity_validator`) | registry-**independent**; ADV comes from context (DynamoDB). Missing ADV → `risk_data_unavailable_result()` | approve + warn | **REJECT** | ✅ | none |

**Phase-1 correction:** Phase 1 (HIGH-003) claimed registry failure caused fail-**open** in live. Reading the code shows the opposite — all three validators fail **closed** in live. Only the sector validator depends on the registry; spread/liquidity read live data from the risk-context builder (DynamoDB `PRICE#{symbol}`), so a registry load failure does not blind them. **Lock-in tests added** (see §5) so this cannot silently regress to fail-open.

### Q3 — `_get_broker(market)`: paper never reaches a live broker

**Files:** `services/execution_engine/service.py`; `services/execution_engine/orders/order_manager.py`; `services/execution_engine/brokers/zerodha_broker.py`.

- `_handle_approved_signal_event` (≈1918–2062): if `approved.paper_trade` → `_handle_paper_order(...)` then **`return`** — the paper branch completes and never falls through to the live branch. The live branch (`execute_approved_signal`) is entered only when `paper_trade=False`.
- `_handle_paper_order` (≈2123–2368) **never calls `_get_broker`**; it writes a `PAPER-…` order, publishes a synthetic event, applies the fill, attaches the exit policy. Zero real-broker calls.
- `_get_broker(market)` (1420–1434) is mode-agnostic (NSE→Zerodha, US→Alpaca). There is **no** `LiveGateChecker`/approval-token/entry-path `live_trading_enabled` gate on the entry path — live entry is decided solely by the per-signal `paper_trade` flag plus the `risk_decision_id` / expiry / kill-switch / `UniverseOrderValidator` gates. `live_trading_enabled` gates only the ExitOrderRouter live-exit path (start(), ≈line 428).

**New residual finding (LOW–MED) — restart reconciliation queries the live broker for paper orders.** `_reconcile_state` Pass 2 (≈1561) does `broker = self._get_broker(order.market); await broker.get_order_status(order.broker_order_id)` for any order with a `broker_order_id`, with **no paper filter**. `get_open_orders` (`order_manager.py` 854–921) returns `PARTIALLY_FILLED` paper orders (whose `broker_order_id` is the synthetic `PAPER-…` id). On restart this issues a **real** `kite.order_history(order_id="PAPER-…")` for a paper order.
- It is **read-only** (`get_order_status` only, no `place_order`), and the whole pass is wrapped in try/except, so it cannot place or mutate an order. Impact is a spurious/failed broker lookup, not a trade.
- It nonetheless violates the strict rule that `paper_trade=True` "must never call a real broker API."
- **Not auto-fixed** (one-fix-per-PR; affects execution behavior; needs approval). Recommended fix in §9.

### Q4 — Read-only runtime verification

Delivered `scripts/read_only_live_readiness_runtime_check.py` (strictly read-only — enforced by a `_ReadOnlyDynamo` proxy that raises on any write call; no broker client; never reads the token *value*). Full detail, the operator run command, and the interpretation table are in the companion file **`docs/live-readiness/runtime-state-verification-report.md`**. Because this audit cannot reach DynamoDB, the runtime verdict is `RUNTIME_VERIFICATION_REQUIRED` and **no GO**.

A **secondary finding** surfaced while building it: `scripts/deploy/paper_preflight_check.py` reads the kill switch at the wrong key (`KILL_SWITCH#GLOBAL`/`STATE`/`state`) vs the production `KILLSWITCH`/`GLOBAL`/`active` — its kill-switch check is a false-negative. Out of Q1–Q5 scope; fix recommended in §9.

### Q5 — `test_phase8_hardening.py` audit

Mapped all classes to the risks they cover: `TestLocalOutbox` (F3 outbox), `TestPlacementPause` (F4 — uses a reimplemented `FakeSvc`, not production), `TestKafkaLagWatchdog` (F5), **`TestReconciliationValidator` (F6/Q1 core — 9 tests, isolation/logic, not chain wiring)**, `TestOrphanDetector` (F7), `TestReconcileHelpers` (F8), `TestDataQualitySuppression` (F2 — weak/tautological), `TestGetOrdersByStatus` (tests a stub). **Gaps:** no registry fail-closed test, no paper→broker isolation test, no live-gate-default test, no token-freshness test. The two highest-value, in-scope fail-closed gaps were filled (§5); the rest are recommended in §9.

---

## 3. Files inspected

Risk engine: `services/risk_engine/service.py`; `validators/common.py`; `validators/sector_validator.py`; `validators/reconciliation_validator.py`; `validators/margin_validator.py` (partial); `context/risk_context_builder.py` (partial); `killswitch/killswitch.py`; `limits/risk_limits.py` (referenced).
Execution engine: `services/execution_engine/service.py`; `orders/order_manager.py`; `brokers/zerodha_broker.py`; `auth/zerodha_auth.py`.
Shared / config: `services/shared/risk_state.py`; `services/shared/config/settings.py`.
Strategy engine: `services/strategy_engine/config/strategy_config_loader.py`.
Scripts: `scripts/deploy/paper_preflight_check.py`; `scripts/kill_switch_cli.py`.

## 4. Tests inspected

`tests/unit/test_phase8_hardening.py` (847 lines) — classes listed in Q5 above.

## 5. Tests added

`tests/unit/test_phase2_live_readiness.py` (new) — exercises **real** production modules (`common.py`, `sector_validator.py`) via importlib, mirroring the Phase-8 stub pattern:

- `TestRiskDataUnavailableFailClosed` (4): `test_live_signal_fails_closed`, `test_paper_signal_warns_and_approves`, `test_details_passthrough`, `test_is_paper_signal_defaults_false`.
- `TestSectorValidatorRegistryNone` (3): `test_get_sector_unknown_without_registry`, `test_live_signal_rejected_when_sector_unknown`, `test_paper_signal_approved_with_warning_when_sector_unknown`.

These lock in the Q2 fail-closed behavior so it cannot regress to fail-open.

A read-only-proxy verification for the Q4 script was run **ad hoc** during this audit (confirming all write methods raise `RuntimeError`). Recommend promoting it to a committed unit test (§9).

## 6. Commands run

```bash
python3 -m py_compile scripts/read_only_live_readiness_runtime_check.py
python3 tests/unit/test_phase2_live_readiness.py
python3 tests/unit/test_phase8_hardening.py
# Q4 script — read-only proxy unit test (ad hoc) and unreachable-runtime run (exit 2) and --json
AWS_ENDPOINT_URL=http://127.0.0.1:1 AWS_REGION=ap-south-1 python3 scripts/read_only_live_readiness_runtime_check.py
```

Environment: repo sandbox, **Python 3.10.12** (project targets 3.11+), **boto3 not installed / DynamoDB unreachable** — which is itself why Q4 is `RUNTIME_VERIFICATION_REQUIRED`.

## 7. Test results

- `tests/unit/test_phase2_live_readiness.py` — **7 passed, 0 failed** (`Ran 7 tests … OK`).
- `tests/unit/test_phase8_hardening.py` — **47 ran, OK, skipped=19.** The Q1-relevant `TestReconciliationValidator` ran and **passed**. The 19 skips are environmental, not failures: ~10 `TestReconcileHelpers` skip on a `datetime.UTC` import (3.11-only; sandbox is 3.10), ~9 `TestOrphanDetector` skip on a `CloudWatchMetrics` stub-name mismatch. On the project's 3.11 runtime these are expected to run.
- Q4 read-only proxy test — all write methods (`put_item`/`update_item`/`delete_item`/`batch_write_item`/`transact_write_items`) raise `RuntimeError("BLOCKED …")`; reads pass through.
- Q4 unreachable run — verdict `RUNTIME_VERIFICATION_REQUIRED`, **exit code 2**; `--json` valid.

## 8. Remaining runtime verification required

Must be performed on the trading host before Stage-1 (see the companion report for the exact command and checklist): Zerodha token present **and fresh**; every enabled strategy `paper_trade=True` (unless a specific one-share strategy is deliberately, manually promoted under separate sign-off); kill switch INACTIVE; `reconciliation_required=False`; mode env correct for the intended stage; resolved paper vs live table names confirmed. Until a real run records these, **no GO**.

## 9. Final recommendation & next improvements

**Recommendation: HOLD — do not enable live.** Code-path verification passes on Q1–Q3 and Q5; Q4 runtime is unverified by design. Proceed only after the Q4 script is run on the trading host with a clean result **and** an explicit operator GO. No capital-limit, deploy, or live-enable action should follow from this report.

Suggested follow-ups (each its own approval-gated change):

1. **Q3 paper-isolation guard (LOW–MED).** In `_reconcile_state` Pass 2, skip orders whose `broker_order_id` starts with `PAPER-` (or whose stored `paper_trade`/metadata marks them paper) before calling `_get_broker(...).get_order_status(...)`. Add a regression test that a paper order in `PARTIALLY_FILLED` never triggers a real broker call on restart.
2. **Q1 reconciliation read-failure alarm.** Since the validator fails open on a DynamoDB read error, add a runtime alarm/metric on persistent reconciliation-read failures so the open state is observable (the kill switch remains the hard backstop).
3. **Q4 paper-preflight kill-switch key fix (MED).** Point `scripts/deploy/paper_preflight_check.py` at `KILLSWITCH`/`GLOBAL`/`active` (ideally import `shared.risk_state.kill_switch_key`) so its kill-switch check stops returning false-negatives.
4. **Q5 test gaps.** Add committed tests for: paper→broker isolation (entry + restart reconciliation), live-gate default (`live_trading_enabled` absent ⇒ live exits disarmed), token freshness, and the Q4 read-only proxy.

**STOP — awaiting approval before any of the above is implemented or before any live-readiness GO decision.**
