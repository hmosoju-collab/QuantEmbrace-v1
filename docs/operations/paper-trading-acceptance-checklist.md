# Paper Trading Acceptance Checklist

**Status:** PHASE 4 — IN PROGRESS (Days 1-6 complete; Days 7-8 pending)  
**Version:** 1.1  
**Date:** 2026-05-27 (updated after Day 6 readiness review)  
**Blocking:** Live mode gate. Live trading MUST NOT be enabled until all criteria below are checked off.

> **Day 6 readiness review completed 2026-05-27.** All 6 execution gaps fixed. Signal age root cause confirmed and resolved (Days 1-4 zero trades). Pre-flight check script added. Platform is GO for Day 7 paper session. See `memory/decisions.md` ADR-020 and `memory/paper_trading_fixes.md` for full fix log.

---

## Instructions

Run this checklist for **5 consecutive complete paper sessions** before proceeding to the live gate.  
A session = one full trading day (09:15–15:30 IST for NSE). Mark each item after each session.  
Any unchecked item after 5 sessions = live mode remains blocked.

---

## Session Tracking

| Session | Date | Operator | Passed? | Notes |
|---|---|---|---|---|
| 1 | 2026-05-20 | Hari | ☐ | 0 fills — signal age default 5s rejected all candle signals (root cause Days 1-4) |
| 2 | 2026-05-21 | Hari | ☑ | 6 fills, 2 kill switch activations (fixed FIX-2), MIS not squared (fixed FIX-6) |
| 3 | 2026-05-22 | Hari | ☐ | 0 fills — asyncio starvation in kafka_tick_publisher (fixed Day 4) |
| 4 | 2026-05-22 | Hari | ☐ | 0 fills — candle stream writing broken (LocalStack schema) |
| 5 | 2026-05-25 | Hari | ☐ | 0 fills — monitoring-only session |
| 6 | 2026-05-26 | Hari | ☐ | 0 fills — readiness review session; all 6 execution gaps fixed |
| 7 (next) | TBD | Hari | ☐ | **FIRST SESSION with all fixes applied** — requires `docker-compose down -v && setup` |
| 8 | TBD | Hari | ☐ | |

---

## Criteria

### S1 — Startup

| # | Check | Evidence |
|---|---|---|
| S1.1 | `execution_service.startup_reconciliation_started` log emitted at service start | Log line with `mode=paper` |
| S1.2 | `execution_service.startup_reconciliation_completed` log emitted with `clean=True` | Or `mismatch_count=N` with explanation |
| S1.3 | No `UNMANAGED` mismatches found on startup (all open positions have `stop_price`) | Zero `UNMANAGED` in reconciliation report |
| S1.4 | No `ZERO_QTY_OPEN` mismatches requiring repair (positions correctly set from prior session) | Zero `ZERO_QTY_OPEN` repairs |
| S1.5 | Reconciliation metric `reconciliation.mismatch_detected_total` = 0 on a clean startup | CloudWatch / log metric |

### S2 — Position Entry and Exit Policy

| # | Check | Evidence |
|---|---|---|
| S2.1 | Every position fill triggers `attach_exit_policy` immediately after | `order_manager.attach_exit_policy` log per fill |
| S2.2 | Every filled position has `stop_price` set in DynamoDB within 5 seconds of fill | DynamoDB check: `stop_price` field present |
| S2.3 | No position remains open with `stop_price = NULL` after 15 minutes | TEE poll cycle confirms no `UNMANAGED` alerts |
| S2.4 | `product_type = MIS` on all NSE intraday positions | DynamoDB check: `product` field = "MIS" |

### S3 — Trade Exit Engine

| # | Check | Evidence |
|---|---|---|
| S3.1 | TEE polls positions every `TEE_POLL_INTERVAL_SECONDS` (60s default) | Log: `tee.poll_cycle_started` timestamps |
| S3.2 | Stop-loss exits fire on correct side: LONG exits when `last_price ≤ stop_price` | At least 1 observed SL exit with correct direction |
| S3.3 | Take-profit exits fire on correct side: LONG exits when `last_price ≥ take_profit` | OR: confirm no TP fires if none triggered |
| S3.4 | Trailing stop activates when price moves `≥ trailing_activation_pct` from entry | Log: `tee.trailing_stop_activated` with correct symbol |
| S3.5 | Trailing stop advances but never loosens (ratchet invariant) | DynamoDB: `stop_price` only increases for LONG |
| S3.6 | TP suppressed when `exit_state = TRAILING_ACTIVE` | No `tee.firing_exit` with TAKE_PROFIT when trailing active |
| S3.7 | `trade_exit.trailing_activated_total` metric increments on activation | CloudWatch counter > 0 if trailing occurred |
| S3.8 | `trade_exit.unmanaged_position_detected_total` = 0 in session | CloudWatch counter = 0 |
| S3.9 | No exit order routed through `signals.pending` or `signals.approved` | Zero `publish_signal` calls from TEE (source + log check) |
| S3.10 | `signals_today` counter unaffected by exits (compare before/after exit) | Risk engine log: no cap increment on exit |
| S3.11 | TEE never calls live broker API in paper mode | Zero `zerodha_broker.place_order` calls from TEE |

### S4 — MIS Square-Off

| # | Check | Evidence |
|---|---|---|
| S4.1 | `mis_square_off.starting` log fires at `MIS_CLOSE_TIME_IST` (default 15:05) | Log timestamp within ±5s of 15:05 |
| S4.2 | ALL open positions (LONG and SHORT) found in scan | `mis_square_off.positions_found_total` = total open positions |
| S4.3 | No short positions missed (historical bug — `quantity < 0` must be included) | `mis_square_off.short_positions_found` > 0 if shorts are open |
| S4.4 | MIS close orders use `product_type = MIS` | Log field `product_type=MIS` on each `mis_square_off.close_order_placed` |
| S4.5 | `mis_square_off.product_type_invalid_total` = 0 | CloudWatch counter = 0 |
| S4.6 | All positions confirmed `direction = FLAT` in DynamoDB by 15:10 | DynamoDB scan after 15:10: zero `direction != FLAT` |
| S4.7 | `mis_square_off.all_positions_closed` log emitted (not deadline escalation path) | Log event confirmed |
| S4.8 | `mis_square_off.positions_still_open_at_deadline` = 0 | CloudWatch counter = 0 |
| S4.9 | Kill switch NOT activated by MIS (means all positions closed normally) | Zero `kill_switch.activated` events from MIS |

### S5 — Idempotency

| # | Check | Evidence |
|---|---|---|
| S5.1 | No duplicate exit orders in the orders DynamoDB table | `orders` table: one exit record per position per session |
| S5.2 | `mis_square_off.duplicates_prevented` > 0 IF TEE fired exits before 15:05 | CloudWatch counter confirms duplicate was blocked |
| S5.3 | `exit_order_id` set on all closed positions | DynamoDB: `exit_order_id` present on all FLAT positions |
| S5.4 | Kill switch (if triggered) overrides existing `exit_order_id` without duplicate | Only 1 exit order ID per position in orders table |
| S5.5 | Reconciliation service never creates exit orders (source check passes each session) | Automated: `test_reconciliation_does_not_call_exit_router` must pass |

### S6 — Reconciliation

| # | Check | Evidence |
|---|---|---|
| S6.1 | `reconciliation.mismatch_detected_total` metric emitted on startup | CloudWatch metric visible |
| S6.2 | `reconciliation.paper_repair_total` reflects actual repairs made | Metric value matches repair log count |
| S6.3 | No `STALE_EXIT_LOCK` mismatches persist more than one session | Zero stale locks across sessions 2-5 |
| S6.4 | Report `clean=True` on sessions 4 and 5 | No mismatches after stable paper operation |

### S7 — No Live Calls

| # | Check | Evidence |
|---|---|---|
| S7.1 | Zero live broker `place_order` calls in entire session log | Search logs for `zerodha_broker.place_order` source = paper path only |
| S7.2 | `exit_router.live_blocked` log NOT emitted (live never attempted) | Absence of this log line |
| S7.3 | `QE_LIVE_EXITS_ENABLED` env var = absent or `false` | `printenv` on EC2 / config dump |
| S7.4 | `live_trading_enabled=False` in ExitOrderRouter constructor | Code audit or startup log |
| S7.5 | `orders.events` Kafka topic contains only paper fills (no broker order IDs from Zerodha) | Topic consumer: all `broker_order_id` values start with `paper-` |

---

## Session Sign-Off

All 5 sessions passed the criteria above:

| Sign-off | Name | Date | Signature |
|---|---|---|---|
| Operator | | | |
| Reviewer | | | |

**After sign-off, proceed to:** [live-mode-gate-checklist.md](live-mode-gate-checklist.md)
