# QuantEmbrace — AWS-BT-9 Walk-Forward Validation Report

> **Phase 9 — walk-forward validation. Implemented + tested.** Backtest-only: advisory only — never promotes a strategy. No broker APIs, no live trading.
> Generated: 2026-06-06 · Governed by `aws-backtesting-steering.md` · Each fold feeds the AWS-BT-8 metrics engine.

---

## 1. What was built

- `services/backtesting/walk_forward.py` — fold generation, train-only optimisation, OOS aggregation, stability/overfit indicators, advisory eligibility, parameter-robustness report.
- `scripts/backtest/run_walk_forward_aws.py` — runner with `--preset` and `--self-test`.
- `docs/backtesting/walk-forward-validation.md` — updated to the implementation.

## 2. Windows

`default` 12/3/3, `medium` 24/6/6, `long` 36/12/12 (train/validate/roll months); custom `WindowSpec` and `anchored` mode supported. Folds are half-open with `train_end == validate_start` → no overlap, OOS strictly after IS, `_assert_no_leakage` enforced.

## 3. Outputs

Per fold: windows, chosen params, IS objective, OOS metrics/objective, optional `run_id`. Aggregate: mean OOS expectancy / profit factor, total OOS net P&L, IS→OOS degradation, win consistency, **stability score**, **overfit warning**, **eligibility recommendation**, and a **parameter-robustness** report (per-param value distribution).

## 4. Indicators & gates

- **Stability** = modal-param-set frequency; `< 0.70` ⇒ unstable.
- **Degradation** = mean OOS / mean IS objective; `< 0.50` ⇒ overfit.
- **Win consistency** = share of folds with positive OOS expectancy; `< 0.50` ⇒ inconsistent.
- OOS gates reuse `metrics_engine.evaluate_gates` (expectancy > 0, PF > 1.2, net P&L > 0).
- **Eligibility**: `ELIGIBLE_FOR_PAPER_PRIORITIZATION` (gates pass + stable + not overfit) · `PAPER_OPTIMIZATION` (gates pass but unstable/overfit) · `REJECT` (gates fail). Advisory.

## 5. Test results

`tests/backtest/test_walk_forward.py` — **6/6 passing** (full lab suite **70/70**).

| Test | Verifies |
|---|---|
| `train_and_validation_windows_do_not_overlap` | `train_end <= validate_start` for all presets |
| `validation_period_always_after_train` | OOS starts at/after IS end |
| `no_future_leakage` | `train_start < train_end <= validate_start < validate_end` (rolling + anchored) |
| `fold_reports_generated` | per-fold rows with required keys; good run → eligible |
| `unstable_strategy_flagged` | alternating best params → stability 0.5 → unstable |
| `parameters_selected_only_from_train_period` | best param == train argmax; OOS used once, never for selection |

## 6. Runner self-test (synthetic)

`--self-test --preset default` over 2010–2024 produced 55 folds: mean OOS expectancy ≈ 3.06, IS→OOS degradation 0.75, win consistency 1.00, stability 1.00, overfit `False`, OOS gates PASS → `ELIGIBLE_FOR_PAPER_PRIORITIZATION`. Synthetic demonstration; a real `evaluate` wires the replay engine + adapter + TEE + metrics over each window's lake data.

## 7. Design notes

- **Decoupled harness**: `evaluate(params, fold, phase)` is injected, so the module is pure and unit-testable; production plugs in the real backtest.
- **No leakage by construction**: parameters are selected only from train-phase results; OOS is evaluated once with the train-selected params; fold geometry is asserted.
- Each fold may be registered as a `qe-bt-runs` entry via an optional `run_spec_factory` + registry.

## 8. Files

| Artifact | Path |
|---|---|
| Harness | `services/backtesting/walk_forward.py` |
| Runner | `scripts/backtest/run_walk_forward_aws.py` |
| Tests (6) | `tests/backtest/test_walk_forward.py` |
| Doc | `docs/backtesting/walk-forward-validation.md` |

## 9. Recommended next phase

**Phase 10 — Model dataset generator** (`/aws_bt_model_dataset`): build leakage-free, time-split training datasets for the `ai_engine` quality scorer and regime classifier from backtest replays.

---

*Implemented + tested. No infra deployed, no live trading, no broker APIs called. Stop for approval before the next phase.*
