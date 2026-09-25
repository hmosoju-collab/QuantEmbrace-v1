# AWS Backtesting Lab — Phase 6 Report: TEE / MIS Comparison

**Status:** COMPLETE — awaiting human approval before Phase 7  
**Date:** 2026-06-14  
**Governance rule:** Every phase writes a report and stops. A human approves before the next phase begins.

---

## Scope

Phase 6 verifies the Trade Exit Engine (TEE) old-vs-new comparison framework and MIS
intraday square-off simulator. The components were already implemented from a prior session.
Phase 6 work: self-test verified, 6 gap tests added (gap-through fill, short trailing, UNRESOLVED
EOD, policy comparison signal, no-broker), 0 regressions.

---

## What Was Already Implemented

| File | Status |
|---|---|
| `services/backtesting/tee_simulator.py` | Pre-existing — complete |
| `services/backtesting/mis_simulator.py` | Pre-existing — complete |
| `scripts/backtest/compare_tee_policies_aws.py` | Pre-existing — complete |
| `tests/backtest/test_tee_mis.py` (11 tests) | Pre-existing — all passing |

---

## TEE Simulator Design

### Exit Priority (production order, per bar)

```
1. Stop-loss / trailing stop        — uses current stop as of this bar (before any advance)
2. MFE / MAE update                 — best_price and excursion tracking
3. Breakeven shift                  — move stop to entry at breakeven_at_r
4. Partial profit booking           — once, idempotent, at partial_profit_at_r
5. Trailing activation / advance    — only tightens; never loosens
6. Final target / fixed TP          — suppressed when trailing active, if configured
7. Max-hold time exit               — held >= max_hold_minutes
8. Hard time exit (IST)             — bar time >= hard_exit_time
```

MIS square-off (15:05 IST) is applied by `MISSimulator` as final cleanup — only on positions
still open after all TEE logic runs for that bar.

### R Math

```
initial_risk = abs(entry_price - initial_stop)
LONG:  R = (price - entry) / initial_risk
SHORT: R = (entry - price) / initial_risk
```

### Gap-Through Fill

When a bar opens beyond the stop (bad direction for the position), the fill is at `open`
rather than at the stop level. This is the conservative assumption: a gap exposes the
full slippage to the `open` price.

```
LONG, bar.open < stop  → fill at bar.open    (gapped through)
LONG, bar.open >= stop → fill at stop        (no gap; clean hit)
```

### Policies

| Policy | Trailing | Breakeven | Partial | Fixed TP | Time Exit |
|---|---|---|---|---|---|
| `old_global` | 1.25R activate / 0.60R dist | None | None | 2.5R | None (rides to MIS) |
| `new_default` | 1.25R activate / 0.75R dist | 1.0R | 50% at 1.0R | 2.5R (suppressed after trailing) | 15:00 IST |
| `new_vwap_reversion` | Same | 0.75R | 50% at 1.0R | 2.0R | 30 min max-hold + 15:00 |
| `new_preclose` | 1.5R activate / 0.75R dist | 0.75R | 50% at 1.0R | 2.0R | 15:00 IST |

### Self-Test Results

```
python scripts/backtest/compare_tee_policies_aws.py --self-test

=== TEE policy comparison (old global vs new strategy-aware) ===
  trades: 3
  metric           old       new   delta
  avg_realized_r   1.667     1.142    -0.525
  avg_capture      0.641     0.391    -0.250
  avg_giveback     0.359     0.609    +0.250
  mis_dependency   0.333     0.000    -0.333
  exit-reason mix  old={'FINAL_TARGET': 2, 'MIS_CLOSE': 1}
                   new={'UNRESOLVED': 1, 'TRAILING': 1, 'TIME_EXIT': 1}
  (advisory only — comparison never promotes a policy or changes trading)
```

**Interpretation:** On these three synthetic trades the old policy captures more R than the
new policy. This is expected: the new policy's 15:00 hard exit and 50% partial booking
trade captured R for reduced MIS exposure and tighter risk management. Which is better
depends on the live trade distribution — exactly what the comparison is designed to measure.

The comparison is **advisory only** — it never changes which policy is active in production.

---

## MIS Simulator Design

```
square_off_ist  = "15:05"   # proactive platform close (controls the fill)
deadline_ist    = "15:10"   # must be flat by here
broker_auto_ist = "15:15"   # broker last-resort (no code path in this simulator)
```

`should_square_off(ts)` → True once bar time (IST) ≥ 15:05. Only applies to positions
not yet closed by TEE. MIS is **never** a profit-booking mechanism — it is exposure cleanup.

---

## Tests

Phase 6 added 6 tests; combined total is 17 in `tests/backtest/test_tee_mis.py`.

**Pre-existing (11):**

| Test | Covers |
|---|---|
| `test_r_calculation_long/short` | R math for both directions |
| `test_breakeven_shift` | Stop pulled to entry at breakeven_at_r |
| `test_partial_booking_idempotency` | 50% booked exactly once |
| `test_trailing_never_loosens` | stop_history non-decreasing for long |
| `test_vwap_max_hold_exit` | TIME_EXIT after 30 min for vwap_reversion |
| `test_preclose_hard_exit` | TIME_EXIT at 15:00 for preclose policy |
| `test_mis_eod_exit` | MIS_CLOSE for old policy (no time exit) |
| `test_old_vs_new_deterministic_comparison` | Identical bars → identical outcome on repeat run |
| `test_daily_cap_does_not_block_exits` | daily_cap_hit=True does not suppress exits |
| `test_mis_remains_final_cleanup_only` | MIS does not fire on already-closed positions |

**New (6):**

| Test | Covers |
|---|---|
| `test_gap_through_stop_fill_long` | Bar opens below stop → fill at open (not stop) |
| `test_gap_through_stop_fill_open_worse_than_stop` | Bar opens exactly at stop → fill at stop (clean hit) |
| `test_short_trailing_stop_triggered` | SHORT: trailing activates at 1.25R, tightens from above, rally triggers closure |
| `test_unresolved_position_at_eod_without_mis_bar` | Bars end early, no MIS → UNRESOLVED + flattened at last close |
| `test_compare_policies_runner_new_lower_r_due_to_time_exit` | For a late-session trade, old vs new produce distinct outcomes (comparison has signal) |
| `test_no_broker_calls_in_simulators` | tee_simulator.py and mis_simulator.py contain no broker API references |

Full suite: **135 passed, 0 failed** (`tests/backtest/` — all phases).

---

## File Manifest

```
# Python (modified — tests extended)
tests/backtest/test_tee_mis.py     (+6 tests, 11→17)

# Scripts (verified, no changes needed)
scripts/backtest/compare_tee_policies_aws.py
```

---

## Safety Invariants Verified

| Invariant | How verified |
|---|---|
| No broker API in TEE or MIS | `test_no_broker_calls_in_simulators` |
| MIS is final cleanup only | `test_mis_remains_final_cleanup_only`; only fires on positions not yet closed by TEE |
| Gap-through fills at open (conservative) | `test_gap_through_stop_fill_long` |
| Trailing only tightens, never loosens | `test_trailing_never_loosens` (stop_history sorted) |
| Daily cap never blocks exits | `test_daily_cap_does_not_block_exits` |
| UNRESOLVED flattened at last close | `test_unresolved_position_at_eod_without_mis_bar` |
| Advisory only — comparison never promotes | Print footer in `compare_tee_policies_aws.py`; no code path changes trading behavior |
| 135 tests still passing | ✅ Confirmed: `python -m pytest tests/backtest/ -q` |

---

## Phase 7 Preview (NOT STARTED)

Phase 7 scope: **Metrics Engine and Report Writer**

The `metrics_engine.py` and `report_writer.py` exist from prior sessions. Phase 7 would:
- Audit the metrics catalog against the spec (`docs/backtesting/metrics-catalog.md`)
- Verify all live-readiness gate keys are computed and labeled advisory
- Add any missing metrics (MFE/MAE distribution, per-symbol breakdown)
- Confirm the `ReportWriter` artifact set matches the spec (trades, equity curve, labels)
- Add missing test coverage

Phase 7 does **not** begin until this report is approved.

---

## Approval Required

Per governance: **a human must approve this report before Phase 7 begins.**

Checklist for approver:
- [ ] Exit priority order accepted (stop → MFE/MAE → breakeven → partial → trailing → target → max-hold → hard-time)
- [ ] Gap-through fill logic accepted (fill at open when gap, fill at stop when clean hit)
- [ ] MIS design accepted (15:05 proactive, 15:10 deadline, exposure cleanup only)
- [ ] Old-vs-new self-test results reviewed (new policy lower avg R on runners, lower MIS dependency)
- [ ] Advisory-only framing accepted (comparison never promotes a policy)
- [ ] 135 tests still passing (`python -m pytest tests/backtest/ -q`)
- [ ] Phase 7 scope (Metrics Engine audit) understood and approved
