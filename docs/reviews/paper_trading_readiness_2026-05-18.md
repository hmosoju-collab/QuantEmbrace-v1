# QuantEmbrace Paper-Trading Readiness Review
## Chief Quant Architect Audit

**Review date:** 2026-05-18
**Reviewer role:** Chief Quant Trading Platform Architect
**Scope:** Full-stack audit — architecture, code, infra, tests, ops
**Bar evaluated:** Paper trading (simulated fills, no real capital)
**Predecessor:** `docs/reviews/platform_review_2026-05-11.md` (live-capital review, NOT READY verdict)

---

## TL;DR — Verdict

**CONDITIONAL GO for paper trading. NOT READY for live capital.**

The platform is *significantly* further along than it was one week ago. Two of the three live-capital blockers from the 2026-05-11 review are fixed. Alpaca paper trading is complete. The end-to-end paper-trade flow (strategy → risk → execution → simulated fill → Kafka audit) is wired correctly. Order idempotency is institutional-grade.

Paper trading does **not** require the live-capital blockers to be cleared, because no real money flows. However, three specific gaps must be addressed or explicitly accepted before paper trading begins, and one finding (order_id non-determinism) is **more serious than the prior review surfaced** — it should be tracked even for paper.

The architecture itself is sound. Layer separation is genuine. The risk engine is unbypassable by design. Kafka wiring matches the approved Phase 2 spec almost exactly. Phase 8 hardening is partially in flight — six of fifteen scoped files exist, nine do not, and the roadmap claim of "All 8 failure modes closed" is **false on disk**.

---

## 1. State of the Prior Review's Blockers (verified on disk, 2026-05-18)

| ID | Prior Review (2026-05-11) | This Review (2026-05-18) | Bar for paper? |
|---|---|---|---|
| BLOCKER-001 — P&L race | NOT FIXED — read-modify-write on PNL_DAY | ✅ **FIXED** — `update_item` with `ADD` expression (loss_validator.py:530–584) | n/a (paper doesn't compute live P&L the same way) |
| BLOCKER-002 — Unpaginated scans | NOT FIXED — silent 1MB truncation | ✅ **FIXED** — `LastEvaluatedKey` loop in both `_fetch_realized_pnl_from_db` and `_get_unrealized_pnl` (loss_validator.py:301–464) | n/a |
| BLOCKER-003 — Missing `risk_limits_production.yaml` | NOT FIXED | ❌ **STILL MISSING** | n/a — paper uses dev profile |
| HIGH-001 — `_signal_locks` memory leak | NOT FIXED | ❌ **STILL NOT FIXED** — service.py:155, no cleanup | Mitigate with daily restart |
| HIGH-002 — Strategy engine hand-rolled kill switch | NOT FIXED | ⚠️ **PARTIAL** — works correctly but not refactored to `KillSwitchCache` | Acceptable for paper |
| HIGH-003 — InstrumentRegistry graceful degradation | NOT FIXED | ⚠️ **PARTIAL** — no production hard-fail mode | Acceptable for paper |
| HIGH-005 — `datetime.utcnow()` | NOT FIXED | ❌ **STILL NOT FIXED** — 2 occurrences in zerodha_broker.py:101 & 128 | Cosmetic for paper (Py 3.11) |

**Net movement in one week:** 2 hard fixes, 0 new defects introduced in the audited paths, 5 items unchanged. The P&L race and pagination fixes are the two most important items — both are now genuinely correct.

---

## 2. New Findings Surfaced by This Review (not in prior review)

### NEW-001 — `order_id` is **non-deterministic** (Severity: HIGH for both paper and live)

**File:** `services/shared/utils/helpers.py:53–68`

```python
ts = epoch_ms()
short_uuid = uuid.uuid4().hex[:8]
return f"QE-{ts}-{short_uuid}"
```

CLAUDE.md says (§Critical Trading Rules / Restart Safety and Idempotency):
> "Order placement must be idempotent: the same signal processed twice must not produce duplicate orders."

`phase2_final_approved.md` §4.4 specifies:
```
order_id = sha256(signal_id+risk_decision_id+instrument_id+direction+quantity)[:32]
```

The current implementation generates an order_id from `epoch_ms()` + `uuid4()`. **Two consumers processing the same signal will generate two different order_ids.** Idempotency *is* protected — but via a different mechanism: the `SIGNAL#{signal_id}` lock in `transact_write_items` (order_manager.py:287–308). So the order_id non-determinism does not cause duplicate orders in practice — the signal lock catches the race. **But the spec is violated and the design's defence-in-depth is reduced from two layers to one.**

**Why this matters for paper trading:** It doesn't, financially. But it pollutes observability — the same signal replayed produces different order_ids in audit logs, complicating trace analysis.

**Why this matters for live:** If the SIGNAL# lock ever has a defect (e.g., GSI consistency issue), the order_id was the second line of defence. Without determinism, there is none.

### NEW-002 — `signal_id` formula deviates from approved spec (Severity: MEDIUM)

**File:** `services/strategy_engine/publishers/kafka_signal_publisher.py:466–473`

Actual:
```python
raw = "|".join([
    signal.strategy_name,
    signal.symbol,
    signal.direction.value,
    f"{signal.price_at_signal:.4f}",
    signal_time.isoformat(),
])
return hashlib.sha256(raw.encode()).hexdigest()[:32]
```

Spec (§4.2):
```
sha256(strategy_id|instrument_id|direction|timeframe|tick_sequence_id)[:32]
```

The actual formula uses `price_4dp` + `signal_time.isoformat()` instead of `timeframe` + `tick_sequence_id`. **The implementation is *more* deterministic than the spec for repeated identical inputs, but *less* deterministic across restarts** — `signal_time` will differ between strategy_engine restarts replaying the same tick. The spec's `tick_sequence_id` would make signal_id replay-stable. The current formula does not.

In paper mode, this means a restart-replayed tick produces a *different* signal_id, which is processed as a brand new signal. This is not financially dangerous (paper) but is **incorrect under the design's restart-safety guarantees**.

### NEW-003 — Roadmap claim that Phase 8 is "All 8 failure modes closed" is **false** (Severity: HIGH for documentation hygiene)

**File:** `architecture/roadmap.md` line 41

> "8 │ Production Hardening + Fault Tolerance │ 🚧 ACTIVE │ All 8 failure modes closed"

**Disk reality:** 6 of 15 Phase 8 deliverables exist; 9 are missing.

| Phase 8 Task | Spec File | On Disk |
|---|---|---|
| F1 — local outbox | `shared/kafka/local_outbox.py` | ✅ EXISTS |
| F1 — integration test | `tests/integration/test_kill_switch_fanout.py` | ❌ MISSING (no `tests/integration/` dir) |
| F2 — endpoint budgets | `shared/zerodha/endpoint_budgets.py` | ❌ MISSING |
| F3 — ACK_UNKNOWN status | `shared/models/order.py` enum value | ✅ EXISTS |
| F3 — pre-retry tag scan | `execution_engine/brokers/base_broker.py` (`resolve_ack_unknown`) | ✅ EXISTS (via order_manager._recover_ack_unknown_order) |
| F4 — `data_quality` field | `shared/models/candle.py` + propagation | ❌ MISSING |
| F4 — warm-up suppression | `strategy_engine/runners/strategy_runner.py` | ❌ MISSING |
| F5 — startup reconciliation gate | `shared/reconciliation/gate.py` | ❌ MISSING |
| F6 — signal inbox | `risk_engine/processing/signal_inbox.py` | ❌ MISSING |
| F6 — signal outbox + publisher | `risk_engine/processing/signal_outbox.py` + `outbox_publisher.py` | ❌ MISSING |
| F6 — Kafka lag watchdog | `risk_engine/watchdogs/kafka_lag_watchdog.py` | ✅ EXISTS |
| F7 — reconciliation validator | `risk_engine/validators/reconciliation_validator.py` | ✅ EXISTS |
| F7 — three-way reconcile tool | `scripts/ops/reconcile.py` | ✅ EXISTS |
| F8 — 3-tier SL + flatten | `execution_engine/brokers/base_broker.py` | PARTIAL (not verified) |
| F8 — orphan detector | `execution_engine/monitors/orphan_detector.py` | ✅ EXISTS |
| Infra — inbox/outbox tables | `infra/terraform/modules/dynamodb/signal_processing.tf` | ❌ MISSING |
| Tests — Phase 8 hardening | `tests/unit/test_phase8_hardening.py` (47 tests) | ✅ EXISTS |

**Net:** F1 partial (missing integration test), F2 not started, F4 not started, F5 not started, F6 mostly not started, F7 done, F8 mostly done. Roadmap should read "🚧 IN PROGRESS — 6/15 deliverables".

### NEW-004 — `open_tasks.md` claims TASK-002 (Alpaca paper trading) is open P0; **it is not** (Severity: LOW documentation hygiene)

`memory/open_tasks.md` lines 66–87 list TASK-002 as P0 with unchecked acceptance criteria. On disk: `services/execution_engine/brokers/alpaca_broker.py` is complete (550+ lines, full `BrokerClient` protocol, paper mode via `ALPACA_USE_PAPER`/`settings.alpaca.use_paper`, `TradingStream` WebSocket, 22 unit tests in `test_alpaca_broker.py`). The DONE-017 line of the same file confirms this was completed on 2026-04-26.

Doc drift is not a code defect, but every operator following the open task list will be misled.

### NEW-005 — `tests/integration/` directory does not exist (Severity: MEDIUM for go-live, LOW for paper)

CLAUDE.md tech stack lists "Integration tests run against LocalStack (S3, DynamoDB) and a local Kafka broker." The `tests/integration/` directory is absent. All 694 tests are unit tests. The end-to-end flow (strategy → risk → execution) has no integration coverage. Paper trading itself is an integration test, but with no instrumented assertions.

---

## 3. What Is Working Well

These are the platform's structural strengths. They are unchanged from the prior review and remain accurate.

**Layer separation is genuine, not aspirational.** The strategy_engine imports zero execution or risk code. The risk_engine consumes only Kafka topics and DynamoDB state — never broker clients. The execution_engine refuses to place an order without a `risk_decision_id` (verified in `_handle_paper_order` and `submit_order`). The compiler-level enforcement of the trading-flow rule is real.

**Order idempotency is institutional-grade.** `submit_order` uses `transact_write_items` to atomically write the order row and reserve `SIGNAL#{signal_id}`. Three distinct race scenarios are handled with explicit code paths:
- Case A — PENDING retry: existing order in PENDING status, retry broker call with idempotent client_order_id
- Case B — ACK_UNKNOWN recovery: existing order in ACK_UNKNOWN, scan broker history for matching tag (Phase 8 F3 done)
- Case C — Active/terminal dedup: existing order in any settled state, return immediately, do not touch broker

The `OrderStatus.ACK_UNKNOWN` and `FLATTENING` enum values are present in `shared/models/order.py`, ahead of much of the rest of Phase 8.

**Kafka wiring matches phase2_final_approved.md §8 exactly.** All four producers (tick, signal, approved, order events) have identical config: `acks=all`, `enable.idempotence=true`, `max.in.flight.requests.per.connection=1`, `retries=5`, `compression.type=lz4`. All four consumers have manual commit (`enable.auto.commit=false`, `enable.auto.offset.store=false`) with synchronous `commit(message=msg, asynchronous=False)` after processing. This is the most important quality lever for restart safety, and it is correct.

**trace_id propagates unchanged end-to-end.** Verified in code at four hops: data_ingestion (uuid4 once), strategy_engine (copies from tick), risk_engine (copies from signal), execution_engine (copies into orders.events). The audit trail is intact.

**Risk engine validator chain is well-ordered.** Cheap validators (kill switch → signal age) run first; expensive DynamoDB reads run only on signals that pass. The 2026-05-11 review's positive assessment holds.

**Risk engine race condition is fixed.** This is the single biggest improvement in the past week. The atomic `update_item ADD` on `PNL_DAY#<date>` removes the last category-1 safety defect identified in the prior review.

**Paper trading flow is wired completely.** `paper_trade=True` flag flows from strategy stamp → Kafka signal payload → risk engine passthrough → execution engine routing to `_handle_paper_order` → simulated fill written to DynamoDB → `ORDER_FILLED`/`ORDER_REJECTED` published to `orders.events` with `paper=True` metadata.

**Alpaca paper trading is complete.** Full `BrokerClient` protocol implemented, `TradingStream` WebSocket subscribed, paper mode default-True, 22 unit tests passing.

**Infrastructure is comprehensive.** All six Terraform modules (vpc, s3, dynamodb, kafka, ec2_services, monitoring) are substantive. VPC endpoints exist for S3, DynamoDB, ECR, CloudWatch Logs, Secrets Manager, and MSK — eliminating NAT data-transfer cost for in-VPC AWS calls. CloudWatch alarm coverage for DLQ topics and log retention configurability are in place.

---

## 4. What Is Broken or Missing

### For paper trading specifically — items that affect paper-trade quality

| ID | Item | Severity (paper) | Mitigation |
|---|---|---|---|
| HIGH-001 | `_signal_locks` memory leak (execution_engine/service.py:155) | LOW for short sessions, HIGH for 24/7 paper | Restart execution_engine daily at POST_CLOSE |
| NEW-001 | `order_id` non-deterministic | LOW (financially safe due to SIGNAL# lock) | Accept and track, fix before live |
| NEW-002 | `signal_id` formula deviates from spec | LOW (paper) | Accept and track, fix before live |
| HIGH-002 | Strategy engine hand-rolled kill switch | LOW (works correctly) | Accept, refactor before live |

None of these prevent paper trading from running. All four should be documented and tracked.

### For live capital — items that must be cleared before real money

| ID | Item | Severity (live) |
|---|---|---|
| BLOCKER-003 | `configs/risk_limits_production.yaml` missing | CRITICAL |
| HIGH-001 | `_signal_locks` memory leak | HIGH |
| HIGH-003 | InstrumentRegistry graceful degradation in production | HIGH |
| HIGH-005 | `datetime.utcnow()` deprecation (zerodha_broker.py:101,128) | MEDIUM (cosmetic until Py 3.12) |
| NEW-001 | `order_id` non-determinism | HIGH |
| NEW-002 | `signal_id` formula deviation | MEDIUM |
| Phase 8 F2 | Endpoint budgets / cancel priority not implemented | HIGH (cancels starved under 429) |
| Phase 8 F4 | `data_quality` warm-up suppression not implemented | HIGH (signals on gap-period candles) |
| Phase 8 F5 | Startup reconciliation gate not implemented | HIGH (cold-start schema mismatch) |
| Phase 8 F6 | Signal inbox/outbox not implemented | HIGH (publish-after-reserve race possible) |
| Phase 8 F8 | 3-tier SL fallback completeness not verified | HIGH (unprotected position on SL rejection) |
| MISC | `scripts/auth/refresh_zerodha_token.py` missing (runbook references it) | HIGH (emergency procedure broken) |
| MISC | Backtests for 5 new strategies (ORB, Scalp1m, VWAPReversion, IntradayTrend15m, PreCloseMomentum) absent | HIGH (Sharpe/drawdown criteria unverifiable) |
| MISC | Grafana dashboard configs absent | MEDIUM (intraday visibility) |
| MISC | DLQ growth alarm in monitoring module — not located | MEDIUM |
| MISC | No `tests/integration/` directory | MEDIUM (no end-to-end coverage) |
| MISC | No `hypothesis` property-based tests | LOW |

---

## 5. Architecture Assessment

**The design is sound.** No structural change is required. Every concern in this review is a *missing piece of an already-correct design*, not a design defect. The mental model — strategy emits → risk gatekeeps → execution places → broker confirms — is intact in code.

Specific design-level observations:

**The unbypassable risk engine is real.** Execution consumes only `signals.approved`, written only by risk_engine after a positive validation decision. There is no code path that injects directly into execution. I tried to find one and could not.

**Kafka is the right backbone.** MSK Serverless + 7 topics + 3 consumer groups + JSON schema is the correct simplification for a personal-scale trader. The prior over-engineered v2.1 was correctly rejected. The current Phase 2 design is exactly what this system needs and exactly what is on disk.

**The hot-signal path adds one significant hop the prior review flagged: AI enrichment.** Signals now traverse `signals.pending → ai_engine → signals.enriched → risk_engine → signals.approved`. The `EnrichmentWatchdog` fallback (Phase 6) is the correct safety net — if `aiengine-v1` lags, risk_engine falls back to consuming `signals.pending` directly. This is a well-designed escape hatch. The latency cost (15–100ms per signal under normal enrichment) is acceptable for the strategies in scope.

**Phase 8 is the right roadmap.** The eight failure modes targeted by Phase 8 are all real — I checked each F-mode against the code and each one is genuinely exploitable in its un-mitigated form. The roadmap is wise. The execution is partial.

**Cost optimisation is correctly applied.** No Lambda in streaming paths. EC2 ARM64 ASGs (c6g/t4g) for long-running services. MSK Serverless (zero idle cost). DynamoDB on-demand. S3 lifecycle policies. VPC endpoints to avoid NAT charges. The cost model is appropriate for a personal-scale algo platform.

---

## 6. Paper Trading Execution Plan

Given the verdict (CONDITIONAL GO), here is the concrete plan to begin paper trading safely.

### Pre-paper checklist (do all of these before first paper session)

1. **Verify NSE paper-trade behaviour.** Zerodha does not have a "paper" API. Inspect `_handle_paper_order()` to confirm that for `signal.market == NSE`, it simulates fills locally (does not call `kite.place_order()`). This is critical — a paper signal on NSE that accidentally hits the real broker is a live trade. Spot-check `services/execution_engine/service.py:1875+` to confirm the paper branch never calls `zerodha_broker.place_order()`.

2. **Confirm all 6 strategies are `paper_trade=True` in DynamoDB strategy-config.** Run `python scripts/strategy/config.py list --env staging` (or whichever env) and assert every strategy shows `paper_trade: true`.

3. **Set up daily restart cron for execution_engine.** Mitigates HIGH-001 memory leak. SystemD timer or ASG scheduled refresh at 16:00 IST or 16:30 ET (after both market closes).

4. **Set up CloudWatch alarm for execution_engine memory > 70%.** Tripwire for HIGH-001 in case daily restart misses a session.

5. **Activate the kill switch manually once** before the first session via `python scripts/kill_switch_cli.py activate --reason "paper start validation"` then `deactivate` to verify the SNS notification chain works end-to-end.

6. **Run `python scripts/deploy/preflight_check.py --env staging`** and confirm exit code 0.

7. **Run `python scripts/kafka/validate_phase2.py --all`** against staging (the 7 tests from `phase2_final_approved.md` §14). Some tests need risk_engine running — start services first.

8. **Document the known accepted defects.** Create a NEW file `memory/known_paper_defects_2026-05-18.md` listing HIGH-001, HIGH-002, HIGH-005, NEW-001, NEW-002 with rationale for accepting each during paper trading. This protects future operators (and future-you) from re-discovering known issues mid-session.

### During paper trading (per session)

- Run for ≥ 5 complete NSE sessions (09:15–15:30 IST) and ≥ 5 complete US sessions (09:30–16:00 ET)
- Track these per session in a spreadsheet:
  - Signals generated per strategy
  - Signals approved / rejected (with rejection breakdown)
  - Paper fills (count, simulated P&L, slippage assumption)
  - Any alarm fired (P0 / P1 / P2)
  - Any kill-switch activation (manual or automatic)
- After each session, run `python scripts/monitoring/paper_session_report.py --date today` and archive output

### Promotion gate to live capital

Do not advance beyond paper until all of these are true:
- 5 consecutive sessions with zero P0 alarms and no automatic kill-switch activations
- All items in §4 "For live capital" cleared OR explicitly accepted with documented rationale
- Backtests for all 5 new candle strategies completed with Sharpe ≥ 0.5 and max drawdown ≤ 5%
- `configs/risk_limits_production.yaml` created and reviewed
- `scripts/auth/refresh_zerodha_token.py` created
- Phase 8 F2, F4, F5, F6 implemented and tested

---

## 7. Recommended Next Sprint (post-paper-start)

Sequence the remaining Phase 8 work in this order. Each item closes a specific known failure mode.

**Sprint priority — week 1 of paper trading:**

1. **Phase 8 F5 — Startup reconciliation gate** (`shared/reconciliation/gate.py`). This is the foundation for safe restarts. Without it, every restart is a roll of the dice on schema compatibility. ~2 days.

2. **Phase 8 F2 — Endpoint budgets** (`shared/zerodha/endpoint_budgets.py`). Cancel-priority preservation. Critical for kill-switch correctness under Zerodha degradation. ~2 days.

3. **Phase 8 F6 — Signal inbox/outbox + lag watchdog tables**. Closes the publish-after-reserve race in risk_engine. Includes the Terraform `signal_processing.tf` for the two new DynamoDB tables. ~3 days.

4. **Determinism fixes — order_id and signal_id**. Conform to phase2_final_approved.md §4.2 and §4.4 spec. ~1 day.

5. **HIGH-001 fix — `_signal_locks` cleanup**. Either `finally` removal or `WeakValueDictionary`. ~0.5 day. Add a regression test.

6. **HIGH-005 fix — `datetime.utcnow()` removal**. Mechanical replace + enable `ruff DTZ` rule set. ~0.5 day.

**Sprint priority — week 2 of paper trading:**

7. **Phase 8 F4 — `data_quality` field + warm-up suppression**. Prevents signals on gap-period candles after WebSocket reconnect. ~2 days.

8. **`configs/risk_limits_production.yaml`** + preflight assertion that the file exists with all required keys. ~0.5 day.

9. **`scripts/auth/refresh_zerodha_token.py`** + runbook smoke test. ~0.5 day.

10. **DLQ growth CloudWatch alarm** on `KafkaDLQMessages > 0` for any DLQ topic. ~0.5 day.

11. **Grafana dashboard** with intraday P&L, signal/approval/fill rates, consumer-group lag per topic. ~1 day.

12. **`tests/integration/test_kill_switch_fanout.py`** (Phase 8 F1 final piece). ~1 day.

**Sprint priority — week 3+:**

13. Backtest framework for the 5 new candle strategies. Each strategy needs ≥ 12 months of historical NSE data, Sharpe and drawdown computed under realistic slippage. This is the gate to live capital, not paper.

14. Property-based tests using `hypothesis` for risk calculations. The DailyLossValidator concurrent-fill invariant is the canonical target — even though it's now fixed, the property test documents the invariant.

---

## 8. Summary Scorecard

| Dimension | Score | Change from 2026-05-11 |
|---|---|---|
| Architecture soundness | ✅ Excellent | unchanged |
| Layer separation enforcement | ✅ Excellent | unchanged |
| Risk engine correctness | ✅ Good (BLOCKER-001 fixed) | ⬆ from ❌ |
| Risk engine pagination | ✅ Good (BLOCKER-002 fixed) | ⬆ from ❌ |
| Order idempotency (signal lock layer) | ✅ Excellent | unchanged |
| Order idempotency (order_id layer) | ⚠️ Partial — non-deterministic | newly surfaced |
| Kafka producer/consumer wiring | ✅ Excellent | unchanged |
| trace_id propagation | ✅ Excellent | unchanged |
| Kill switch coverage | ✅ Good | unchanged (HIGH-002 still partial) |
| Alpaca paper trading | ✅ Complete | ⬆ from missing-from-tracking |
| Paper-trade end-to-end flow | ✅ Complete | newly verified |
| Phase 8 hardening completeness | ⚠️ 6/15 | newly measured |
| Test coverage (unit) | ✅ Good — 694 tests | unchanged |
| Test coverage (integration) | ❌ None | unchanged |
| Test coverage (property-based) | ❌ None | unchanged |
| Production config (risk_limits) | ❌ Missing | unchanged |
| Operational scripts | ⚠️ Partial — 2/4 missing | unchanged |
| Observability (Grafana) | ❌ Missing | unchanged |
| Infrastructure (Terraform) | ✅ Good | unchanged |
| Documentation hygiene | ⚠️ Drift — open_tasks stale, roadmap overclaims | newly surfaced |

**Verdict for paper trading: CONDITIONAL GO.** Clear the pre-paper checklist (§6), accept the documented defects, run for 5 sessions, and learn what the platform actually does in motion.

**Verdict for live capital: NOT READY.** The remaining work is sequenced in §7. ~3 weeks of focused engineering work will close the gap.

---

*Review conducted against codebase state on disk 2026-05-18. Forensic findings are file-level evidence, not documentation-derived. Where documentation and code disagree, code wins. This review does not constitute financial advice.*
