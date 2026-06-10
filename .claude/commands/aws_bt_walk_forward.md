---
description: "Phase 8 — in-sample/out-of-sample walk-forward study with OOS aggregation."
argument-hint: "[strategy] [rolling|anchored] [train_months] [test_months] [step_months]"
---

# /aws_bt_walk_forward — Phase 8: Walk-Forward Validation

Run a walk-forward study per `docs/backtesting/walk-forward-validation.md`. Each fold is a **registered** backtest run.

## Load first
`walk-forward-validation.md`, `no-lookahead-rules.md` (§4), `aws-backtest-run-registry.md`, `metrics-catalog.md`.

## Do
1. Generate folds from the study spec (rolling/anchored; IS strictly precedes OOS).
2. Optimize params on IS by the chosen objective; run OOS as a registered run; store under `walkforward/{study_id}/`.
3. Aggregate OOS equity/metrics; compute IS-vs-OOS degradation, parameter stability, win-consistency.

## Safety
No live impact. Enforce IS<OOS time separation; assert no leakage.

## Output / Stop
Walk-forward study report (`aggregate.json` + narrative). **Stop for approval.**
