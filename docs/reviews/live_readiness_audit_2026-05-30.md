# Live-Trading Readiness Audit — Phase 1

**Date:** 2026-05-30 (Saturday)
**Auditor role:** Live-trading readiness auditor / production trading safety engineer
**Target:** Monday 2026-06-01 — Stage 1 (one-share / minimum-quantity live validation only, subject to gates)
**Scope of this phase:** 1A pre-live blockers · 1B live-path safety gates · 1C Stage-1 requirements mapping

> **Method & honesty constraints applied.** Every claim below is tagged `[VERIFIED]` (read in code/config at the cited path), `[DOC CLAIM]` (asserted in docs/memory but not the source of truth), or `[INSUFFICIENT EVIDENCE]`. Where code and documentation disagree, **code is treated as truth** (per `CLAUDE.md`). No live trading was enabled, no capital limits changed, no deploys, no orders. This is a read-only audit. Stop-for-approval applies at the end of this phase.

---

## 0. Verdict — Monday Stage-1 Go / No-Go

**Recommendation: NO-GO for Monday 2026-06-01 one-share live validation.**

This is **not** because the platform is unsafe — the paper-path safety architecture is strong and largely verified. It is because **there is no coherent live ENTRY+EXIT path that can be turned on for a single share without leaving an unmanaged live position.** The three flags that would have to align for a safe one-share live trade (`paper_trade` per-signal, `execution.paper_trading` global, `live_trading_enabled`) are sourced independently and **cannot currently be set to a consistent live posture**, because one of them (`live_trading_enabled`) is not a real settings field and resolves to a hard `False`.

The single most important finding: **if an operator forced a live entry by setting a strategy's `paper_trade=False`, the entry would reach the real broker, but the protective-exit and end-of-day square-off machinery would still run in paper/simulation mode and would NOT close the real position.** That is the textbook unmanaged-live-position failure. Details in §3.

Secondary blockers (HIGH-001, HIGH-005) and several `[INSUFFICIENT EVIDENCE]` gaps (paper-session count, gate metrics) independently justify NO-GO.

---

## 1. Documentation vs. code divergence (read this first)

`memory/open_tasks.md` describes **Phase 8 (Production Hardening, ADR-015) as "all 10 tasks unstarted."** **That snapshot is stale.** `[VERIFIED]` Code on disk shows substantial Phase-8 work already merged (files dated after the doc snapshot; commit `35c6cb4 "ADR-018 live tightening + Day-6 operational fixes"`).

Implication: do not plan from `open_tasks.md` Phase-8 status. The per-item truth is in §2.

---

## 2. Phase 1A — Pre-live blockers (verified against code)

### 2.1 HIGH-series blockers

| ID | Claim | Status in code | Evidence |
|----|-------|----------------|----------|
| HIGH-001 | `_signal_locks` dict grows unbounded (no eviction) | **OPEN** `[VERIFIED]` | `execution_engine/service.py:155` create, `:778` `setdefault`; grep for `_signal_locks.pop/clear/del` → **no matches**. Every distinct `signal_id` adds a lock that is never removed. Memory leak over a long live session. |
| HIGH-002 | `strategy_engine` does not use `KillSwitchCache` | **OPEN (functions, but inconsistent)** `[VERIFIED]` | `KillSwitchCache` exists only in `risk_engine`. `strategy_engine/service.py` uses a hand-rolled `_is_kill_switch_active()` (`:705`) polling DynamoDB directly (`:146`,`:484`,`:499`,`:598`). The kill switch **does** function in strategy_engine; the gap is consistency/robustness, not a dead switch. |
| HIGH-003 | Registry load failure silently disables 3 validators | **OPEN (by design, fail-open)** `[VERIFIED]` | `risk_engine/service.py:245-253` catches `FileNotFoundError, ValueError` from `InstrumentRegistry.load()` and continues with `registry=None` ("graceful-degradation mode"). `registry.py:149-190` raises on missing/malformed YAML. Net: a missing/broken `instruments.yaml` degrades sector/spread/liquidity checks to warn/skip **regardless of mode** — acceptable for paper, **violates fail-closed for live**. |
| HIGH-004 | MIS pre-close square-off watchdog | **IMPLEMENTED & wired** `[VERIFIED]` (earlier "open" status superseded) | `mis_square_off.py` full impl; instantiated `service.py:324-333`; in the runtime task set `service.py:570` (`mis_manager.run()`). Fires 15:05 IST, escalates to kill switch at 15:10. **Caveat:** its `paper_trading` arg is `getattr(settings.execution,"paper_trading",True)` (`:331`) — see §3. |
| HIGH-005 | `datetime.utcnow()` in `_OrderPlacementLimiter` | **OPEN** `[VERIFIED]` | `zerodha_broker.py:102` (`self._day_key = datetime.utcnow().date()`) and `:129`. Daily order-count window rolls at **UTC** midnight = 05:30 IST, not at IST midnight/market open. Deprecated naive `utcnow()`. Low-severity but real for an NSE daily-limit counter. |

### 2.2 Phase 8 (ADR-015) item-by-item

| Item | Description | Status | Evidence |
|------|-------------|--------|----------|
| PHASE8-001 | `ACK_UNKNOWN` order state machine | **IMPLEMENTED** `[VERIFIED]` | `orders/order.py:37` + transitions; `order_manager.py:44,511,518-521`; ACK_UNKNOWN handling throughout `service.py` ("refusing blind second broker placement"). |
| PHASE8-002 | `endpoint_budgets.py` per-endpoint budget module | **ABSENT as named module** `[VERIFIED]`; placement-pause exists | No `shared/zerodha/endpoint_budgets.py`. `zerodha/` = `market_phase.py`, `rate_limiter.py`. Token-bucket limiter present (`ZerodhaRateLimiter`, `settings.py:328`); placement-pause backoff present (`service.py:1838-1843,2470-2475`). |
| PHASE8-003 | `data_quality` flag + warm-up suppression | **IMPLEMENTED** `[VERIFIED]` | `candle_stream.py` (multiple), `strategy_runner.py:216-219`, `base_strategy.py:52`, `dynamo_candle_consumer.py:129+`. |
| PHASE8-004 | Reconciliation gate | **PARTIAL** `[VERIFIED]` | `scripts/ops/reconcile.py` and `execution_engine/reconciliation/reconciliation.py` exist; `shared/reconciliation/gate.py` **absent** (no `shared/reconciliation` dir). Startup recon config present (`settings.py:567-590`, `reconciliation_live_auto_repair=False` "Must remain False"). |
| PHASE8-005 | `_halt_new_order_intake` / outbox halt integration | **ABSENT** `[VERIFIED]` | `LocalOutbox` exists (`shared/kafka/local_outbox.py`) with `on_overflow`→kill-switch; grep `_halt_new_order_intake` → **no matches**. |
| PHASE8-006 | Risk-engine inbox/outbox + lag kill switch | **HALF** `[VERIFIED]` | `KafkaLagWatchdog` implemented (`risk_engine/watchdogs/kafka_lag_watchdog.py`, threshold 500 / 3 checks). `risk_engine/processing/` dir (signal_inbox/outbox/publisher) **absent**. |
| PHASE8-007 | `reconciliation_validator` hard-halt wiring | **PARTIAL** `[VERIFIED]` | `risk_engine/validators/reconciliation_validator.py` exists; full hard-halt wiring **not confirmed** → see §6. |
| PHASE8-008 | Orphan detector | **IMPLEMENTED, alert-only** `[VERIFIED]` | `monitors/orphan_detector.py` — "does NOT auto-flatten" (`:19`, ADR-015 §5.4). Wired `service.py:594-600`. |
| PHASE8-009 | `signal_processing.tf` DynamoDB module | **ABSENT** `[VERIFIED]` | No `infra/terraform/modules/dynamodb/signal_processing.tf`. |
| PHASE8-010 | `test_kill_switch_fanout.py` | **ABSENT** `[VERIFIED]` | Not present. `tests/unit/test_phase8_hardening.py` exists (scope not audited this phase). |

---

## 3. Phase 1B — Live-path safety gates (verified against code)

### 3.1 Gates that are correct and verified

The order-entry function `execute_approved_signal()` (`execution_engine/service.py:707`) enforces, **in this order**:

1. **Risk proof required** — raises `ValueError` if `risk_decision_id` empty (`:745-749`). A signal cannot be executed without passing the risk engine. `[VERIFIED]`
2. **Expiry** — rejects if `expires_at` passed (`:750-754`); duplicate stale check also at `:1931`. `[VERIFIED]`
3. **Kill switch** — refreshes durable state and raises `RuntimeError` if active (`:755-763`). `[VERIFIED]`
4. **Universe hard gate** — `UniverseOrderValidator.validate()` (`:767-776`); rejects symbols not in today's approved snapshot. Built with `fail_if_no_snapshot=mode.is_live` (`:523`); **live init failure is FATAL** (`:531-537` raises and halts startup), so in live the validator can never be silently `None`. **Fail-closed in live: confirmed.** `[VERIFIED]`
5. **Idempotency** — per-signal `asyncio.Lock` then DynamoDB `transact_write_items` with `attribute_not_exists(PK)` on both order row and signal-lock row (`order_manager.py:293-312`); on `TransactionCanceledException` returns **`False`** (duplicate suppressed, no broker call) (`:320-332`). Matches the `CLAUDE.md` invariant. `[VERIFIED]`
6. **Paper/live isolation (entry)** — `if approved.paper_trade:` routes to `_handle_paper_order()` and **returns before the live branch** ("never falls through to live broker", `service.py:1944-1959`). `_handle_paper_order` docstring + body: "Does NOT call Zerodha or Alpaca", "Does NOT consume any ZerodhaRateLimiter tokens" (`:2123-2161`). `[VERIFIED]`
7. **Schema safety** — `paper_trade` is a **required** field on the signal schema (`shared/events/schemas.py:93`); a signal missing it fails validation rather than defaulting to live. `[VERIFIED]`

### 3.2 The live-flag finding (this is the core blocker)

`[VERIFIED]` **`live_trading_enabled` is not a defined settings field anywhere.**

- `service.py:428` reads it as `getattr(self._settings.execution, "live_trading_enabled", False)`.
- `ExecutionConfig` (`settings.py:448-590`, `env_prefix="EXECUTION_"`) declares **no** `live_trading_enabled` field.
- `AppSettings` (`settings.py:657`, `env_prefix="QE_"`, **`extra="ignore"`**) declares none either.
- Therefore the attribute does not exist on the model and `getattr` returns the default **`False` unconditionally**. The env var `QE_EXECUTION_LIVE_TRADING_ENABLED` referenced by `CLAUDE.md`, `hooks/live_trading_gate.yaml`, docs and tests **has no bound field** and is dropped by `extra="ignore"`.

Consequences for a Stage-1 live attempt:

- The value flows into `ExitOrderRouter(live_trading_enabled=...)` (`exit_order_router.py:72`). With it hard-`False`, **live protective exits are blocked pre-lock**: "Live exit blocked pre-lock: live_trading_enabled=False" (`exit_order_router.py:112-113`).
- `MISSquareOffManager` is constructed with `paper_trading=getattr(settings.execution,"paper_trading",True)` (`service.py:331`). Default `paper_trading=True` (`settings.py:557`) → MIS square-off **simulates** fills (`mis_square_off.py:518-540`) and does **not** place real close orders.

**Derived failure mode (NO-GO driver):** entry routing keys off `approved.paper_trade` (per-signal, from strategy-config), while exit/square-off safety keys off `live_trading_enabled` (hard-False) and `execution.paper_trading` (default True). These are independent. Setting one strategy's `paper_trade=False` to place a one-share live entry would open a **real** position whose **stop-loss/TP live exits are blocked and whose 15:05 MIS square-off is simulated** — i.e. an unmanaged live position. This violates "capital protection > trade count > profit."

---

## 4. Phase 1C — Stage-1 one-share requirements (mapped)

### 4.1 Flags and where they live `[VERIFIED]`

| Control | Source | Default | Effect |
|--------|--------|---------|--------|
| `approved.paper_trade` | per-signal, from strategy-config (DynamoDB) | required field, no default | Entry routing: `True`→PaperSimulator, `False`→real broker (`service.py:1944`) |
| `execution.paper_trading` | `EXECUTION_PAPER_TRADING` | `True` (`settings.py:557`) | MIS square-off sim vs real (`service.py:331`); global paper posture |
| `live_trading_enabled` | **no field** (getattr default) | hard `False` | ExitOrderRouter live exits; **cannot be turned on via env today** |
| `UNIVERSE_MODE` | `UNIVERSE_MODE` | `PAPER_SAFE_START` (`service.py:518`) | Approved-symbol snapshot; `is_live` controls fail-closed |
| `RISK_PROFILE` (`profile`) | `RISK_PROFILE` | **`tiny-live`** (`settings.py:226-228`) | **Differs from `CLAUDE.md` paper requirement** (`paper`). Must be set explicitly for paper. |
| `portfolio_value` | `QE_PORTFOLIO_VALUE` | **`1_000_000.0`** (`settings.py:706`) | This is the "1M". Not deployed wholesale — see caps below. |

### 4.2 Capital / size caps already enforced (good for one-share) `[VERIFIED]` (`settings.py:226-273`)

`max_single_order_value=₹5,000` · `max_position_per_symbol=100` · `max_concurrent_positions=1` · `max_open_orders=1` · `max_position_size_pct=5%` · `max_daily_loss_pct=0.5%` · `allow_leverage=False`. The full ₹10L ("1M") is structurally **blocked from wholesale deployment** by these caps; a one-share order is well within them.

### 4.3 Signal-age default mismatch `[VERIFIED]`

`max_signal_age_seconds` default is **`5.0`** in code (`settings.py:274-281`), but `CLAUDE.md` mandates **30 (never < 20)** because candle signals arrive 7–12 s old. The safe value is enforced only by the env var `RISK_MAX_SIGNAL_AGE_SECONDS`, **not** by the code default. Day-6 incident (100% rejection) was exactly this. Any live session must set it explicitly.

### 4.4 Promotion gates — report-only, and there is no "Stage 1" rung `[VERIFIED]`

- `scripts/evaluate_promotion_gate.py` is **report-only** (exit 0/1/2; "never changes the active mode", `:5-7,36-39`).
- `configs/promotion_gates.yaml` defines exactly two gates: `PAPER_SAFE_START_TO_EXPAND` (min **5** paper days) and `PAPER_EXPAND_TO_LIVE` (min **10** paper days + security/credential-isolation checks).
- The promotion ladder is `PAPER_SAFE_START → PAPER_EXPAND → LIVE_ADVANCED` (NIFTY 200, full checklist, FAIL-CLOSED on missing data). **There is no codified one-share / Stage-1 live mode.** A Monday one-share trade would be an ad-hoc off-ladder configuration with no gate definition behind it.
- `hooks/live_trading_gate.yaml` hard-blocks `QE_EXECUTION_LIVE_TRADING_ENABLED=true` in committed config and requires the full checklist + both gates + manual sign-off before live.

### 4.5 Zerodha token `[VERIFIED]`

`ZerodhaTokenManager` (`execution_engine/auth/zerodha_auth.py:69`) accepts and stores `dynamo_client` (`:96,:102`), supporting the required DynamoDB-resolved token (anti-patterns #14/#15). Whether a **fresh** token is currently present is **`[INSUFFICIENT EVIDENCE]`** (runtime/DynamoDB state, not in repo; token expires ~07:30 IST daily and would need `scripts/zerodha_login.py` Monday morning).

### 4.6 Paper-session evidence `[INSUFFICIENT EVIDENCE]`

`docs/reviews/` holds `paper_trading_readiness_2026-05-18.md` and `platform_review_2026-05-11.md`, but **no committed paper-session reports** (`paper_session_report.py` writes runtime artifacts, likely `/tmp`). `open_tasks.md` references first paper **fill** on Day 7 (2026-05-27). The count of completed, clean paper sessions — the input to GATE 1 (≥5) and GATE 2 (≥10) — **cannot be verified from the repo.** No gate metrics file was located.

---

## 5. Why NO-GO (consolidated)

1. **No consistent live posture is settable.** `live_trading_enabled` is not a real field → hard-False → live exits blocked; MIS square-off defaults to simulation. A live entry via `paper_trade=False` would be **unmanaged**. (§3.2) — *blocking, code-verified.*
2. **No Stage-1 gate exists.** Promotion ladder jumps PAPER_EXPAND→LIVE_ADVANCED; a one-share trade is off-ladder with no gate. (§4.4) — *blocking.*
3. **Gate inputs unverifiable.** Paper-session count and gate metrics are `[INSUFFICIENT EVIDENCE]`; GATE 2 (→live) requires ≥10 PAPER_EXPAND days that are not evidenced. (§4.6) — *blocking until evidenced.*
4. **Live fail-closed hole.** Registry load failure degrades risk validators to warn/skip in all modes (HIGH-003). — *must be live-gated first.*
5. **Open hygiene blockers** HIGH-001 (lock leak) and HIGH-005 (UTC day-roll) are non-fatal but should not ride into a first live session.

None of these require enabling anything to fix; they are findings, not actions. Per instructions, **no changes were made.**

---

## 6. Open questions / items to verify in Phase 2 (with approval)

- **PHASE8-007:** Is `reconciliation_validator` actually wired to hard-halt the risk engine on mismatch, or advisory only? (validator file exists; wiring unread)
- **HIGH-003 hardening:** Should registry-load failure **fail closed in live** (raise) rather than degrade? Confirm each of sector/spread/liquidity validators’ behavior when `registry is None`.
- **`_get_broker(market)`** live branch (entry) not yet read end-to-end; confirm it cannot be reached in paper.
- **Token freshness** and **strategy-config `paper_trade` values** are DynamoDB runtime state — require a live read (not done; read-only repo audit).
- **`tests/unit/test_phase8_hardening.py`** scope/coverage not audited.

---

## 7. Files examined this phase (non-exhaustive)

`CLAUDE.md` · `architecture/system_design.md` · `memory/open_tasks.md` · `services/execution_engine/service.py` · `.../mis_square_off.py` · `.../orders/order.py` · `.../orders/order_manager.py` · `.../brokers/base_broker.py` · `.../brokers/zerodha_broker.py` · `.../auth/zerodha_auth.py` · `.../monitors/orphan_detector.py` · `.../exit/exit_order_router.py` · `services/risk_engine/service.py` · `.../registry/registry.py` · `.../watchdogs/kafka_lag_watchdog.py` · `services/shared/kafka/local_outbox.py` · `services/shared/config/settings.py` · `services/shared/events/schemas.py` · `configs/promotion_gates.yaml` · `hooks/live_trading_gate.yaml` · `scripts/evaluate_promotion_gate.py`

---

**Phase 1 complete. Stopping for approval before Phase 2 (per standing instruction). No live trading enabled, no capital limits changed, no deploys, no orders placed.**
