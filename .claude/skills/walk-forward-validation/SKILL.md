---
name: walk-forward-validation
description: Run in-sample/out-of-sample walk-forward studies with OOS aggregation and overfitting checks. Use to test whether a strategy's edge persists out-of-sample before prioritizing it for paper trading. Backtest-only; each fold is a registered run.
---

# Walk-Forward Validation

Authoritative spec: `docs/backtesting/walk-forward-validation.md`. Also `no-lookahead-rules.md` §4, `metrics-catalog.md`.

## When to use
Guarding against curve-fitting; estimating honest out-of-sample edge.

## Procedure
1. Generate folds from the study spec (rolling/anchored); IS must strictly precede OOS.
2. Optimize params on IS by objective; run OOS as a registered backtest; store under `walkforward/{study_id}/`.
3. Aggregate OOS metrics; compute IS-vs-OOS degradation, parameter stability, win-consistency.

## Rules
No leakage across IS/OOS boundary; OOS metrics are the honest estimate. Robustness is evidence for paper prioritization — never an auto-promotion trigger.
