---
description: "Phase 4 — adapters that present production strategies to the replay engine without modifying them."
argument-hint: "[strategy: momentum|orb|scalp_1m|vwap_reversion|intraday_trend_15m|pre_close_momentum]"
---

# /aws_bt_strategy_adapters — Phase 4: Strategy Adapters

Thin adapters so each production strategy runs unchanged in the lab. **Do not modify strategy logic** (trading-layer separation; no behavior change).

## Load first
`services/strategy_engine/strategies/*`, `backtester.py`, `aws-backtesting-specification.md` (§2.4).

## Do
1. For each of the 6 strategies, provide an adapter supplying correct interval, params (from `strategy-config` defaults / config override), and bar interface.
2. Ensure deterministic `signal_id` and `generated_at` stamping match live semantics (no-lookahead).
3. Register adapters so `/aws_bt_replay_engine` and `/aws_bt_full_run` can select by name; reuse `commands/run_backtest.yaml` parameters, do not fork them.

## Safety
No edits to strategy code, risk, or execution. Adapters are read-only wrappers.

## Output / Stop
Per-strategy smoke report (signals generated on a small sample). **Stop for approval.**
