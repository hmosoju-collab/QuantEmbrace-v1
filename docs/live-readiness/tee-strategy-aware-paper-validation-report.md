# TEE Strategy-Aware Exit — Paper Validation Report

**Status:** IN PROGRESS  
**Approval gate:** APPROVED_FOR_PAPER_ONLY (2026-06-05)  
**Stage-1 live gate:** Requires all acceptance criteria below to pass across ≥3 sessions  
**Reference:** `docs/live-readiness/tee-strategy-aware-exit-validation-report.md` (static replay basis)

---

## Acceptance Criteria for Stage-1 Live

All of the following must hold before `APPROVED_FOR_STAGE1_LIVE` can be issued.

| # | Criterion | Required | Sessions 1–3 |
|---|---|---|---|
| L1 | ≥ 3 clean paper sessions with new TEE | ≥ 3 | ☐ / ☐ / ☐ |
| L2 | `profit_capture_ratio` ≥ old TEE baseline (0.72) | ≥ 0.72 | — |
| L3 | MIS square-off count per session ≤ old average (30) | ≤ 30 | — |
| L4 | `max_drawdown` does not materially increase (< 1.5× old) | < 1.5× | — |
| L5 | Zero duplicate exits (`idempotency_skips` > 0 is OK; duplicate fills = FAIL) | 0 | — |
| L6 | Zero unmanaged positions at session close | 0 | — |
| L7 | Daily cap never blocked an exit | 0 | — |
| L8 | Zero live broker calls (`router_live_attempts = 0`) | 0 | — |
| L9 | New monitoring fields visible in live_counters.json | present | — |
| L10 | `scalp_1m` uses fixed TP/SL only (no R-based trailing) | confirmed | — |
| L11 | `trailing_activation_count` > 0 across sessions (policy is firing) | > 0 | — |
| L12 | `partial_booking_count` > 0 across sessions | > 0 | — |

**Decision is REJECTED_REVERT if:** any duplicate exit occurs, any live broker call is made,
or max drawdown exceeds 1.5× old TEE baseline in any single session.

---

## Old TEE Baseline (Session 2026-06-04, fixed TP/SL only)

| Metric | Value | Source |
|---|---|---|
| Total trades | 92 exits | live_counters.json |
| Realized P&L | +₹2,789.54 | live_counters.json |
| TP rate | 67.4% (62/92) | live_counters.json |
| SL rate | 32.6% (30/92) | live_counters.json |
| Trailing activations | 0 | live_counters.json |
| Partial bookings | 0 | live_counters.json |
| Breakeven shifts | 0 | live_counters.json |
| MIS square-off count | 30 (stranded, MIS did not fire) | positions.json |
| profit_capture_ratio (est.) | ~0.72 | tee-profit-capture-analysis.md |
| Max intraday drawdown | unknown (not tracked) | — |

---

## Session 1

**Date:** ___________  
**Session archive:** `session-archives/YYYY-MM-DD/`

### 18 Tracked Metrics

| # | Metric | vwap_reversion | momentum | orb | trend_15m | preclose | scalp_1m | ALL |
|---|---|---|---|---|---|---|---|---|
| 1 | Total trades | | | | | | | |
| 2 | Net P&L (₹) | | | | | | | |
| 3 | Win rate (%) | | | | | | | |
| 4 | Avg winner (₹) | | | | | | | |
| 5 | Avg loser (₹) | | | | | | | |
| 6 | Profit factor | | | | | | | |
| 7 | Max drawdown (₹) | | | | | | | |
| 8 | MFE avg (est.) | | | | | | | |
| 9 | MAE avg (est.) | | | | | | | |
| 10 | profit_capture_ratio | | | | | | | |
| 11 | Breakeven shift count | | | | | | | |
| 12 | Partial booking count | | | | | | | |
| 13 | Trailing activation count | | | | | | | |
| 14 | Trailing stop hit count | | | | | | | |
| 15 | Max hold exit count | | | | | | | |
| 16 | Hard time exit count | | | | | | | |
| 17 | MIS square-off count | | | | | | | |
| 18 | Trades old TEE would exit earlier | | | | | | | |

### Safety Gate Checks (Session 1)

| Gate | Required | Actual | Pass? |
|---|---|---|---|
| Duplicate exits | 0 | | ☐ |
| Unmanaged positions at close | 0 | | ☐ |
| Daily cap blocked exits | 0 | | ☐ |
| Live broker calls | 0 | | ☐ |
| tee_trailing_activations_r visible | present | | ☐ |
| tee_partial_bookings visible | present | | ☐ |
| scalp_1m using fixed TP/SL | confirmed | | ☐ |

**Session 1 verdict:** ☐ CLEAN &nbsp; ☐ ISSUES (describe below)

**Notes:**

---

## Session 2

**Date:** ___________  
**Session archive:** `session-archives/YYYY-MM-DD/`

### 18 Tracked Metrics

| # | Metric | vwap_reversion | momentum | orb | trend_15m | preclose | scalp_1m | ALL |
|---|---|---|---|---|---|---|---|---|
| 1 | Total trades | | | | | | | |
| 2 | Net P&L (₹) | | | | | | | |
| 3 | Win rate (%) | | | | | | | |
| 4 | Avg winner (₹) | | | | | | | |
| 5 | Avg loser (₹) | | | | | | | |
| 6 | Profit factor | | | | | | | |
| 7 | Max drawdown (₹) | | | | | | | |
| 8 | MFE avg (est.) | | | | | | | |
| 9 | MAE avg (est.) | | | | | | | |
| 10 | profit_capture_ratio | | | | | | | |
| 11 | Breakeven shift count | | | | | | | |
| 12 | Partial booking count | | | | | | | |
| 13 | Trailing activation count | | | | | | | |
| 14 | Trailing stop hit count | | | | | | | |
| 15 | Max hold exit count | | | | | | | |
| 16 | Hard time exit count | | | | | | | |
| 17 | MIS square-off count | | | | | | | |
| 18 | Trades old TEE would exit earlier | | | | | | | |

### Safety Gate Checks (Session 2)

| Gate | Required | Actual | Pass? |
|---|---|---|---|
| Duplicate exits | 0 | | ☐ |
| Unmanaged positions at close | 0 | | ☐ |
| Daily cap blocked exits | 0 | | ☐ |
| Live broker calls | 0 | | ☐ |
| tee_trailing_activations_r visible | present | | ☐ |
| tee_partial_bookings visible | present | | ☐ |
| scalp_1m using fixed TP/SL | confirmed | | ☐ |

**Session 2 verdict:** ☐ CLEAN &nbsp; ☐ ISSUES (describe below)

**Notes:**

---

## Session 3

**Date:** ___________  
**Session archive:** `session-archives/YYYY-MM-DD/`

### 18 Tracked Metrics

| # | Metric | vwap_reversion | momentum | orb | trend_15m | preclose | scalp_1m | ALL |
|---|---|---|---|---|---|---|---|---|
| 1 | Total trades | | | | | | | |
| 2 | Net P&L (₹) | | | | | | | |
| 3 | Win rate (%) | | | | | | | |
| 4 | Avg winner (₹) | | | | | | | |
| 5 | Avg loser (₹) | | | | | | | |
| 6 | Profit factor | | | | | | | |
| 7 | Max drawdown (₹) | | | | | | | |
| 8 | MFE avg (est.) | | | | | | | |
| 9 | MAE avg (est.) | | | | | | | |
| 10 | profit_capture_ratio | | | | | | | |
| 11 | Breakeven shift count | | | | | | | |
| 12 | Partial booking count | | | | | | | |
| 13 | Trailing activation count | | | | | | | |
| 14 | Trailing stop hit count | | | | | | | |
| 15 | Max hold exit count | | | | | | | |
| 16 | Hard time exit count | | | | | | | |
| 17 | MIS square-off count | | | | | | | |
| 18 | Trades old TEE would exit earlier | | | | | | | |

### Safety Gate Checks (Session 3)

| Gate | Required | Actual | Pass? |
|---|---|---|---|
| Duplicate exits | 0 | | ☐ |
| Unmanaged positions at close | 0 | | ☐ |
| Daily cap blocked exits | 0 | | ☐ |
| Live broker calls | 0 | | ☐ |
| tee_trailing_activations_r visible | present | | ☐ |
| tee_partial_bookings visible | present | | ☐ |
| scalp_1m using fixed TP/SL | confirmed | | ☐ |

**Session 3 verdict:** ☐ CLEAN &nbsp; ☐ ISSUES (describe below)

**Notes:**

---

## 3-Session Aggregate Comparison

*Populated after all 3 sessions complete.*

| Metric | Old TEE (2026-06-04 baseline) | New TEE avg (Sessions 1–3) | Δ | Better? |
|---|---|---|---|---|
| Total trades per session | 92 | | | |
| Net P&L per session | +₹2,790 | | | |
| TP rate | 67.4% | | | |
| Trailing activations | 0 | | | |
| Partial bookings | 0 | | | |
| Breakeven shifts | 0 | | | |
| MIS square-off count | 30 | | | |
| profit_capture_ratio | ~0.72 | | | |
| Max drawdown | unknown | | | |

---

## Acceptance Criteria Final Assessment

*Populated after 3 sessions.*

| Criterion | Required | Achieved | Pass? |
|---|---|---|---|
| L1: ≥ 3 clean sessions | ≥ 3 | | ☐ |
| L2: profit_capture_ratio ≥ 0.72 | ≥ 0.72 | | ☐ |
| L3: MIS square-off ≤ 30 | ≤ 30 | | ☐ |
| L4: max_drawdown < 1.5× old | < 1.5× | | ☐ |
| L5: zero duplicate exits | 0 | | ☐ |
| L6: zero unmanaged positions | 0 | | ☐ |
| L7: zero cap-blocked exits | 0 | | ☐ |
| L8: zero live broker calls | 0 | | ☐ |
| L9: monitoring fields visible | present | | ☐ |
| L10: scalp_1m fixed TP/SL only | confirmed | | ☐ |
| L11: trailing activations > 0 | > 0 | | ☐ |
| L12: partial bookings > 0 | > 0 | | ☐ |

---

## Final Decision

**Current:** IN PROGRESS — awaiting 3 paper sessions

**Decision options:**
- `APPROVED_FOR_PAPER_ONLY` — continue paper, not ready for live
- `APPROVED_FOR_STAGE1_LIVE` — all L1–L12 pass, proceed to pre-live-runbook.md
- `PAPER_ONLY_NEEDS_MORE_DATA` — some criteria not yet met, run more sessions
- `REJECTED_REVERT` — duplicate exit / live call / major drawdown increase → revert to fixed TP/SL

**Final decision:** ___________  
**Date:** ___________  
**Authorized by:** ___________

---

## How to Populate This Report

After each session, run:
```bash
python scripts/monitoring/paper_session_report.py --date YYYY-MM-DD
bash scripts/monitoring/archive_session.sh
```

Then copy the following from `session-archives/YYYY-MM-DD/`:
- `live_counters.json` → metrics 11–17 (new TEE fields)
- `orders.json` → per-strategy breakdown (metrics 1–6)
- `positions.json` → MIS count (metric 17)

Metric 18 (trades old TEE would exit earlier): compare `exit_reason` in orders.json.
Trades with `TRAILING` or `PARTIAL` exit reason = new TEE exited differently than old TEE would.
