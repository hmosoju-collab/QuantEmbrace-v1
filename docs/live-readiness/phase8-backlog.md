# Phase 8 Backlog — Live-Readiness Hardening

**Last updated:** 2026-06-02  
**Purpose:** Ops-facing consolidated view of all open/partial Phase 8 items that are blocking or conditioning Stage-1 live approval.  
**Authoritative source for P&L:** `memory/open_tasks.md` (PHASE8 section)  
**Live trading:** NOT enabled. Nothing here changes that.

> P0 = must complete before any live capital is deployed.  
> P1 = must complete before Stage-1 second trading week.  
> PHASE8-011 specifically blocks scalp_1m Stage-1 re-evaluation (paper OK).

---

## Summary

| ID | Priority | Status | Short description |
|---|---|---|---|
| PHASE8-002 | P0 | 🔲 OPEN | Zerodha endpoint rate budgets |
| PHASE8-004 | P0 | ◐ PARTIAL | Reconciliation gate wired into 4 service starts |
| PHASE8-005 | P0 | ◐ MOSTLY DONE | `_halt_new_order_intake()` hook missing |
| PHASE8-006 | P0 | ◐ PARTIAL | Signal inbox/outbox + per-symbol serialisation (RACE in Session 7) |
| PHASE8-008 | P0 | ◐ PARTIAL | 3-tier broker SL ladder at `base_broker.py` |
| PHASE8-009 | P1 | 🔲 OPEN | `signal_processing.tf` Terraform tables |
| PHASE8-010 | P1 | ◐ PARTIAL | `test_kill_switch_fanout.py` absent |
| **PHASE8-011** | **P1** | **🔲 OPEN** | **scalp_1m v2 rejection counters in live monitoring** |

---

## PHASE8-002 — Zerodha endpoint rate budgets

**Priority:** P0  
**Status:** 🔲 OPEN  
**File:** `services/shared/zerodha/endpoint_budgets.py` (absent)

Zerodha Kite Connect imposes per-endpoint rate limits (e.g. 10 req/s on orders, 1 req/s on positions). Without an explicit budget layer, burst-fill periods can trigger 429s that land orders in `ACK_UNKNOWN` state with no automatic retry. `endpoint_budgets.py` should define per-endpoint token-bucket limits, cancel priority, and placement pause thresholds.

**Blocking:** yes — any live session with >2 simultaneous fills risks a 429.

---

## PHASE8-004 — Reconciliation gate at service startup

**Priority:** P0  
**Status:** ◐ PARTIAL  
**File:** `shared/reconciliation/gate.py` (absent)

The named `gate.py` is not wired into the `start()` methods of `data_ingestion`, `strategy_engine`, `risk_engine`, or `execution_engine`. An overlapping runtime enforcement exists via `reconciliation_validator.py` (PHASE8-007, done) but it only fires per-signal, not at startup. A service that restarts during an active session should pause until reconciliation confirms position state.

**Blocking:** yes — restart during live session without reconciliation gate risks double-fills.

---

## PHASE8-005 — `_halt_new_order_intake()` hook

**Priority:** P0  
**Status:** ◐ MOSTLY DONE  
**File:** `shared/kafka/local_outbox.py` + broker base publisher

`LocalOutbox` is present and wired (risk + strategy publishers). The `_halt_new_order_intake()` hook intended to freeze new order intake during kill-switch activation is not found at the expected extension point. Without it, orders queued in the outbox can still drain to the broker after the kill switch fires.

**Blocking:** yes — kill switch may not be immediate for already-queued orders.

---

## PHASE8-006 — Per-symbol signal serialisation (RACE CONFIRMED)

**Priority:** P0  
**Status:** ◐ PARTIAL  
**Files:** `risk_engine/processing/signal_inbox.py`, `signal_outbox.py`, `outbox_publisher.py` (all absent)

**Race observed Session 7 (2026-06-01):**  
ETERNAL SELL 100 (signal `cfbd2a95`) at 04:27:42 UTC and BUY 201 (signal `895d02b1`) at 04:27:43 UTC both approved against a FLAT position → net unintended LONG +101 shares.  
Root cause: `_signal_locks` keyed by `signal_id`, not `symbol`. Two signals for the same symbol arrived 1 second apart and were approved concurrently.

`signal_inbox.py` / `signal_outbox.py` / `outbox_publisher.py` are the prescribed fix. `KafkaLagWatchdog` is present and routes lag events to the kill switch; that part is done.

**Blocking:** yes — observed live race condition. P0 before any live session.  
**See:** `docs/operations/session-observations/session-7-2026-06-01.md`

---

## PHASE8-008 — 3-tier broker stop-loss ladder

**Priority:** P0  
**Status:** ◐ PARTIAL  
**File:** `execution_engine/brokers/base_broker.py`

`orphan_detector.py` is present and wired. The 3-tier SL ladder (Cover Order/Bracket Order → SL-Market retry → flatten) is not implemented at `base_broker.py` — the file is an abstract interface only. Without it, a stop hit that fails at the broker level (e.g. CO reject) has no automatic fallback to SL-M, and no flatten on repeated failure.

**Blocking:** yes — live session with adverse move + broker stop failure leaves an open position.

---

## PHASE8-009 — `signal_processing.tf` Terraform

**Priority:** P1  
**Status:** 🔲 OPEN  
**File:** `infra/terraform/modules/dynamodb/signal_processing.tf` (absent)

`signal-inbox` and `signal-outbox` DynamoDB tables required by PHASE8-006 need Terraform provisioning. Without them, `signal_inbox.py` / `signal_outbox.py` cannot be deployed. Also includes 3 CloudWatch alarms: inbox age, outbox depth, per-symbol lock duration.

**Blocking for:** PHASE8-006 implementation.

---

## PHASE8-010 — Kill-switch fanout integration test

**Priority:** P1  
**Status:** ◐ PARTIAL  
**File:** `tests/integration/test_kill_switch_fanout.py` (absent)

`test_phase8_hardening.py` (888 lines, present) covers unit cases. The integration test that verifies kill-switch fanout across all 4 services via the Kafka `risk.kill-switch` topic is absent. Required for confidence that the kill switch fires within the 30s SLA under concurrent load.

---

## PHASE8-011 — scalp_1m v2 rejection counters in live monitoring

**Priority:** P1  
**Status:** 🔲 OPEN  
**Files:**
- `scripts/monitoring/paper_trading_monitor.py`
- `services/execution_engine/monitors/live_counters.py` (or equivalent flush path)

**Why this matters:**  
scalp_1m v2 was approved `APPROVED_FOR_PAPER_ONLY` / `DISABLED_FOR_STAGE1_LIVE` on 2026-06-02. One of the five conditions for re-evaluation to Stage-1 is that operators can observe filter behavior in real time during a paper session. Currently, rejection counters exist in-memory inside `Scalp1mStrategy` and are logged at daily reset, but are absent from the live monitoring dashboard.

**Required fields to surface:**

| Field | Source | Description |
|---|---|---|
| `scalp_1m.rejected_total` | `_rejected_spread_wide + _rejected_net_edge + _rejected_stale + _rejected_spread_unavailable` | Total signals rejected by viability filter |
| `scalp_1m.rejected_spread_too_wide` | `Scalp1mStrategy._rejected_spread_wide` | Spread > 0.08% |
| `scalp_1m.rejected_tp_inside_spread` | `_rejected_tp_inside_spread` (counter not yet explicitly named; add it) | TP < 2× spread |
| `scalp_1m.rejected_net_edge_too_small` | `Scalp1mStrategy._rejected_net_edge` | Net edge < 0.12% after costs |
| `scalp_1m.stop_distance_source` | Signal metadata `stop_distance_source` | Counts of ATR / SPREAD_FLOOR / TICK_FLOOR / PCT_FLOOR |
| `scalp_1m.avg_net_expected_edge` | Signal metadata `net_expected_edge_pct` | Average edge of emitted signals |
| `scalp_1m.strategy_version` | Signal metadata `strategy_version` | Must be `scalp_1m_v2` |

**Implementation sketch:**  
1. Add a `flush_scalp_counters(counters: LiveCounters)` method to `Scalp1mStrategy` that pushes the 4 rejection ints and running averages into the shared `LiveCounters` object.  
2. Call it from the strategy runner's per-bar loop or from an existing metrics-flush callback.  
3. Extend the `LiveCounters.to_dict()` serialisation to include the new fields.  
4. Add a §scalp_1m section to `paper_trading_monitor.py` report template.

**Blocking:** scalp_1m Stage-1 re-evaluation (5 conditions, this is condition 4).  
**Reference:** `docs/live-readiness/stage1-strategy-eligibility.md` § Path to re-evaluation

---

## Completion criteria for Stage-1 live

All P0 items must be resolved before any capital goes live. The table below is the final gate:

| Gate | Item | Status |
|---|---|---|
| P0-1 | PHASE8-002: Zerodha endpoint budgets | 🔲 OPEN |
| P0-2 | PHASE8-004: Reconciliation gate at startup | ◐ PARTIAL |
| P0-3 | PHASE8-005: `_halt_new_order_intake()` hook | ◐ MOSTLY DONE |
| P0-4 | PHASE8-006: Per-symbol signal serialisation | ◐ PARTIAL |
| P0-5 | PHASE8-008: 3-tier broker SL ladder | ◐ PARTIAL |
| P0-6 | `LiveGateChecker` all 26 checks pass | ✅ Check 26 added |
| P0-7 | Manual operator approval (`approve_live_gate.py`) | Not done |
| P0-8 | `QE_EXECUTION_LIVE_TRADING_ENABLED=true` set by operator | Not done |

**P1 items (scalp_1m-specific):**

| Gate | Item | Status |
|---|---|---|
| P1-1 | PHASE8-009: `signal_processing.tf` | 🔲 OPEN |
| P1-2 | PHASE8-010: Kill-switch fanout integration test | ◐ PARTIAL |
| P1-3 | **PHASE8-011: scalp_1m counters in monitoring** | **🔲 OPEN** |

---

*Live trading: NOT enabled. Capital limits: unchanged. No broker orders.*  
*See also: `docs/live-readiness/pre-live-runbook.md` · `docs/live-readiness/stage1-strategy-eligibility.md`*
