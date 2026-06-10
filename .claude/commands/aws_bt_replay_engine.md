---
description: "Phase 3 — extend the existing Backtester with Parquet loading, sharding, and checkpoint/resume."
---

# /aws_bt_replay_engine — Phase 3: Replay Engine (reuse + extend)

**Reuse** `services/strategy_engine/backtesting/backtester.py` — do **not** write a new engine (`prefer_refactor_over_rewrite`).

## Load first
`backtester.py`, `aws-data-lake-contract.md`, `aws-backtest-run-registry.md`, `no-lookahead-rules.md`, `cost-slippage-model.md`.

## Do
1. Add a **Parquet `BarSource`** that reads the curated lake (replacing CSV-only) and applies read-time corporate-action adjustment.
2. Add a **shard runner** (by symbol and/or year) that runs the existing `Backtester` per shard.
3. Add **checkpoint hooks**: persist cursor + partial state to `qe-bt-checkpoints` every N bars/M seconds; resume from cursor on restart.
4. Write artifacts to `runs/{run_id}/` (config, trades, metrics, equity_curve, labels, logs).

## Safety
Keep existing engine behavior intact; additions are wrappers. Costs/slippage stay on. `lookahead_violations` surfaced in output.

## Output / Stop
Determinism + resume design report (same config => identical metrics; resumed == uninterrupted). **Stop for approval.**
