---
name: backtest-replay-engine
description: Run and extend the QuantEmbrace replay engine over the Parquet lake with sharding and checkpoint/resume. Use to execute a backtest, add a Parquet source, or wire checkpointing. Reuses services/strategy_engine/backtesting/backtester.py — never builds a parallel engine. Backtest-only.
---

# Backtest Replay Engine

Reuse `services/strategy_engine/backtesting/backtester.py` (`prefer_refactor_over_rewrite`). Contracts: `aws-data-lake-contract.md`, `aws-backtest-run-registry.md`, `cost-slippage-model.md`, `no-lookahead-rules.md`.

## When to use
Running a single/batch backtest; adding the Parquet `BarSource`; implementing shard runner + checkpoint hooks.

## Procedure
1. Load bars via the Parquet `BarSource` (read-time corp-action adjustment).
2. Run the existing `Backtester` per shard (by symbol/year); keep costs + slippage on.
3. Checkpoint cursor + partial state to `qe-bt-checkpoints` every N bars/M seconds; resume from cursor on restart.
4. Write `runs/{run_id}/` artifacts; record engine/cost/data versions + `git_sha`.

## Rules
- Determinism (same config+snapshot => identical metrics) and resume == uninterrupted.
- No broker SDK; no strategy/risk/execution behavior change; `lookahead_violations` surfaced.
