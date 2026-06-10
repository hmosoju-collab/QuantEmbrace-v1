# QuantEmbrace — Walk-Forward Validation

> **Status (AWS-BT-9): implemented.** `services/backtesting/walk_forward.py` + `scripts/backtest/run_walk_forward_aws.py` + `tests/backtest/test_walk_forward.py`. Backtest-only; each fold can be a registered run; advisory only — never promotes.
> Last updated: 2026-06-06 · Governed by `aws-backtesting-steering.md`.

Walk-forward validation tests whether a strategy's edge **persists out-of-sample**, guarding against curve-fitting that a single full-period backtest hides.

---

## 1. Concept

Split the timeline into ordered **in-sample (IS)** → **out-of-sample (OOS)** windows. Optimise parameters on IS, then measure performance only on the immediately following OOS window. Roll forward and repeat. The aggregated **OOS** metrics are the honest estimate of live edge.

## 2. Window presets (`PRESETS`)

| Preset | train | validate | roll |
|---|---|---|---|
| `default` | 12 months | 3 months | 3 months |
| `medium` | 24 months | 6 months | 6 months |
| `long` | 36 months | 12 months | 12 months |

Custom `WindowSpec(train_months, validate_months, roll_months, anchored)` is supported. **Rolling** (default) slides the train window; **anchored** fixes the train start and grows it.

## 3. Fold geometry (no leakage)

`generate_folds(start, end, spec)` builds half-open windows `train=[train_start, train_end)` and `validate=[validate_start, validate_end)` with `train_end == validate_start`, so:

- windows **never overlap** (they share only the boundary instant),
- the validate window is **always strictly after** the train window,
- `_assert_no_leakage` enforces `train_start < train_end <= validate_start < validate_end` for every fold.

## 4. Procedure

1. For each fold, evaluate every parameter set on the **train** window and pick the best by `objective` (default `expectancy`; also `profit_factor`, `net_pnl`).
2. Validate the **train-selected** params on the OOS window — **parameters are never chosen using OOS data**.
3. Optionally register each OOS run in `qe-bt-runs` (via `run_spec_factory` + registry).

The harness is decoupled: the caller supplies `evaluate(params, fold, phase)` which runs a backtest over the window (replay engine + adapter + TEE + metrics in production) and returns a metrics dict.

## 5. Aggregation & overfitting indicators

- **Combined OOS metrics**: mean OOS expectancy, mean OOS profit factor, total OOS net P&L; OOS gates via `metrics_engine.evaluate_gates`.
- **IS→OOS degradation** = mean OOS objective / mean IS objective. `< 0.50` ⇒ overfit.
- **Win consistency** = fraction of folds with positive OOS expectancy. `< 0.50` ⇒ inconsistent.
- **Parameter stability score** = modal-parameter-set frequency across folds (1.0 = same params every fold). `< 0.70` ⇒ **unstable**.
- **Parameter robustness report** = per-parameter value distribution across folds.

## 6. Eligibility recommendation (advisory)

| Verdict | Condition |
|---|---|
| `ELIGIBLE_FOR_PAPER_PRIORITIZATION` | OOS gates pass **and** stable **and** not overfit |
| `PAPER_OPTIMIZATION` | OOS gates pass but unstable or overfit — keep iterating |
| `REJECT` | OOS gates fail |

Robustness here is evidence for **paper-trading prioritization** — never an auto-promotion trigger. Promotion to live still requires ≥5 valid paper sessions + operator sign-off (CLAUDE.md).

## 7. Tooling & tests

| Artifact | Path |
|---|---|
| Harness | `services/backtesting/walk_forward.py` |
| Runner | `scripts/backtest/run_walk_forward_aws.py` (`--preset`, `--self-test`) |
| Tests (6) | `tests/backtest/test_walk_forward.py` |

```bash
python scripts/backtest/run_walk_forward_aws.py --self-test --preset default
```

Output per study: per-fold report (windows + chosen params + IS/OOS objective), aggregate OOS metrics, stability score, overfit warning, eligibility, and the parameter-robustness report.
