# AWS Backtesting Lab — Phase 5 Report: Walk-Forward Validation

**Status:** COMPLETE — awaiting human approval before Phase 6  
**Date:** 2026-06-14  
**Governance rule:** Every phase writes a report and stops. A human approves before the next phase begins.

---

## Scope

Phase 5 delivers the walk-forward validation harness — the primary overfitting guard for
strategy evaluation. The harness generates IS→OOS fold sequences, optimises parameters on
the train window only, validates OOS, and produces an advisory eligibility recommendation.

Phase 5 found `services/backtesting/walk_forward.py` and the CLI runner already implemented
from a prior session. Phase 5 work: verified correctness, ran the end-to-end self-test,
added 6 missing tests (covering eligibility paths, registry integration, anchored mode,
INSUFFICIENT_DATA, and the no-broker invariant), and confirmed 0 regressions.

---

## What Was Already Implemented

| File | Status |
|---|---|
| `services/backtesting/walk_forward.py` | Pre-existing — complete |
| `scripts/backtest/run_walk_forward_aws.py` | Pre-existing — complete |
| `tests/backtest/test_walk_forward.py` (6 tests) | Pre-existing — all passing |

All were verified correct and passing before any Phase 5 changes.

---

## Walk-Forward Design

### Window Presets

| Preset | Train | Validate | Roll |
|---|---|---|---|
| `default` | 12 months | 3 months | 3 months |
| `medium` | 24 months | 6 months | 6 months |
| `long` | 36 months | 12 months | 12 months |

Custom `WindowSpec(train_months, validate_months, roll_months, anchored=False)` is
supported. **Rolling** (default) slides the train window; **anchored** fixes the train
start and grows it forward.

### No-Leakage Guarantee

`generate_folds()` enforces `train_start < train_end ≤ validate_start < validate_end`
for every fold. `_assert_no_leakage()` runs as a post-generation assertion — any
violation raises `ValueError` immediately. The condition `train_end == validate_start`
means windows share only the boundary instant; no bar is in both windows.

Parameters are never selected using OOS data — they are chosen by the train argmax and
then applied to a single OOS evaluation call.

### Fold Execution

```
for fold in folds:
    for params in param_grid:
        metrics = evaluate(params, fold, "train")   # IS only
    best_params = argmax(param_grid, key=train_objective)
    oos = evaluate(best_params, fold, "validate")   # OOS — params fixed from train
    register fold run in qe-bt-runs if registry provided
```

`evaluate(params, fold, phase)` is caller-supplied — it runs the replay engine +
strategy adapter + TEE + metrics over the window's lake data in production.

### Overfitting Indicators

| Indicator | How computed | Threshold |
|---|---|---|
| IS→OOS degradation | `mean_oos_obj / mean_is_obj` | < 0.50 → overfit |
| Win consistency | fraction of folds with positive OOS expectancy | < 0.50 → inconsistent |
| Parameter stability | modal-param-set frequency across folds | < 0.70 → unstable |

All three are advisory — they inform the eligibility recommendation but do not
automatically block anything.

### Eligibility Recommendation

| Verdict | Condition |
|---|---|
| `ELIGIBLE_FOR_PAPER_PRIORITIZATION` | OOS gates pass AND stable AND not overfit |
| `PAPER_OPTIMIZATION` | OOS gates pass but unstable or overfit — keep iterating |
| `REJECT` | OOS gates fail (mean OOS expectancy ≤ 0 or profit factor ≤ 1.2 or net P&L ≤ 0) |
| `INSUFFICIENT_DATA` | Date range too short to generate any fold |

**Advisory only.** This is a paper-prioritization signal, not a promotion trigger.
Live trading still requires ≥5 valid paper sessions + operator sign-off (CLAUDE.md).

### Registry Integration

When `registry=` and `run_spec_factory=` are supplied, each OOS fold registers a run
in `qe-bt-runs` (via `registry.create_run()` → `registry.mark_completed()`), creating
a full audit trail of every fold evaluated.

---

## Self-Test Results

```
python scripts/backtest/run_walk_forward_aws.py --self-test --preset default

=== Walk-forward (default: train 12m / validate 3m / roll 3m) ===
  folds: 55 | objective: expectancy
  ...
  aggregate:
    mean OOS expectancy : 3.059
    mean OOS profit fac : 1.375
    total OOS net P&L   : 168,240.00
    IS→OOS degradation  : 0.750
    win consistency     : 1.00
    stability score     : 1.00  (unstable=False)
    overfit warning     : False
    OOS gates pass      : True
    ELIGIBILITY         : ELIGIBLE_FOR_PAPER_PRIORITIZATION  (advisory)
  (advisory only — walk-forward never promotes a strategy)
```

55 rolling folds over 15 years (2010–2024) with deterministic synthetic evaluate.
IS→OOS degradation 0.75 > 0.50 (within tolerance); win consistency 1.00; stability 1.00.

---

## Tests

Phase 5 added 6 tests; combined total is 12 in `tests/backtest/test_walk_forward.py`.

**Pre-existing (6):**

| Test | Covers |
|---|---|
| `test_train_and_validation_windows_do_not_overlap` | `train_end ≤ validate_start` for all presets |
| `test_validation_period_always_after_train` | Strict ordering for all folds |
| `test_no_future_leakage` | IS < OOS for rolling + anchored modes |
| `test_fold_reports_generated` | `fold_report()` rows + `ELIGIBLE_FOR_PAPER_PRIORITIZATION` |
| `test_unstable_strategy_flagged` | Alternating best-param → `unstable=True` |
| `test_parameters_selected_only_from_train_period` | Train argmax only; validate called once per fold with chosen param |

**New (6):**

| Test | Covers |
|---|---|
| `test_reject_eligibility_when_oos_gates_fail` | Negative OOS expectancy → `REJECT` |
| `test_paper_optimization_when_gates_pass_but_overfit` | Gates pass + degradation 0.10 → `PAPER_OPTIMIZATION` |
| `test_registry_run_registration` | `registry.create_run` + `mark_completed` called once per fold |
| `test_insufficient_data_returns_no_folds` | 6-month range with 15-month-minimum spec → `INSUFFICIENT_DATA` |
| `test_anchored_mode_train_window_grows` | All folds share same `train_start`; `train_end` advances |
| `test_no_broker_calls_in_walk_forward` | `walk_forward.py` contains no broker API references |

Full suite: **129 passed, 0 failed** (`tests/backtest/` — all phases).

---

## File Manifest

```
# Python (modified — tests extended)
tests/backtest/test_walk_forward.py    (+6 tests, 6→12)

# Script (verified, no changes)
scripts/backtest/run_walk_forward_aws.py

# No new Python modules — walk_forward.py was already complete
```

---

## Safety Invariants Verified

| Invariant | How verified |
|---|---|
| IS never overlaps OOS | `_assert_no_leakage()` enforces at fold generation; `test_no_future_leakage` |
| Parameters not chosen from OOS | `evaluate("validate")` called only after argmax on train; `test_parameters_selected_only_from_train_period` |
| Advisory only — no auto-promotion | Eligibility field is a string recommendation; no code path changes trading behavior |
| No broker API references | `test_no_broker_calls_in_walk_forward` |
| Registry OOS fold audit trail | `test_registry_run_registration` |
| Insufficient data handled gracefully | `test_insufficient_data_returns_no_folds` |
| 129 tests still passing | ✅ Confirmed: `python -m pytest tests/backtest/ -q` |

---

## Phase 6 Preview (NOT STARTED)

Phase 6 scope: **TEE Old-vs-New Comparison and MIS Square-off Simulation**

The TEE simulator (`services/backtesting/tee_simulator.py`) and MIS simulator
(`services/backtesting/mis_simulator.py`) exist from a prior session. Phase 6 would:
- Wire TEE old-vs-new exit comparison through the `BacktestRunner`
- Confirm MIS square-off behavior in backtesting (all positions closed by 15:20 IST)
- Write a comparison report showing exit-policy delta across strategies
- Add integration tests connecting TEE/MIS to the runner

Phase 6 does **not** begin until this report is approved.

---

## Approval Required

Per governance: **a human must approve this report before Phase 6 begins.**

Checklist for approver:
- [ ] Walk-forward fold geometry reviewed (rolling vs. anchored, half-open windows)
- [ ] No-leakage guarantee accepted (`train_end == validate_start`, enforced at generation)
- [ ] Overfitting thresholds accepted (degradation < 0.50, consistency < 0.50, stability < 0.70)
- [ ] Eligibility categories accepted (advisory only — never auto-promotes)
- [ ] Registry integration design accepted (per-fold `qe-bt-runs` entries)
- [ ] `INSUFFICIENT_DATA` handling accepted (empty folds, no crash)
- [ ] 129 tests still passing (`python -m pytest tests/backtest/ -q`)
- [ ] Phase 6 scope (TEE/MIS comparison) understood and approved
