---
description: "Phase 5 — wire the mandatory cost/slippage/no-lookahead fill model and assert realism."
---

# /aws_bt_execution_simulator — Phase 5: Execution Simulator

Make fills realistic and provably leak-free. **Calls no broker.**

## Load first
`cost-slippage-model.md`, `no-lookahead-rules.md`, `backtester.py` (`_apply_execution_price`, `_estimate_costs`, `_check_exits`).

## Do
1. Confirm/extend cost model (`IndianCostModel`), slippage + half-spread, gap-through stops, order-vs-bar-volume cap — all **on by default**.
2. Enforce next-bar execution; assert `lookahead_violations == 0` as a run validity gate.
3. Record `cost_model_version`; surface any disabled cost component as a report caveat (`no_silent_failures`).
4. Support `slippage_model` (fixed/percentage/volume_based) and `commission_model` (zerodha/alpaca/zero) selection.

## Safety
No broker SDK in the worker path. Disabling costs requires explicit flag + report flag.

## Output / Stop
Realism report incl. `lookahead_violations == 0` and cost/slippage attribution. **Stop for approval.**
