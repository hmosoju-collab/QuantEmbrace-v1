---
description: "Phase 11 — orchestrate an end-to-end multi-year batch backtest across the worker fleet."
argument-hint: "[strategy|all] [from] [to] [universe]"
---

# /aws_bt_full_run — Phase 11: Full End-to-End Run

Orchestrate a complete multi-year, multi-symbol batch across the `backtest-worker` fleet, end to end. Requires Phases 1-10 approved. **Backtest-only — no live, no orders, no capital change.**

## Load first
All `docs/backtesting/` docs; `aws-backtesting-implementation-plan.md` (dependencies).

## Do
1. Resolve the universe as-of dates (survivorship-safe) and the `data_snapshot_id`.
2. Submit runs to `qe-bt-runs`; scale the worker ASG from 0; workers claim shards, checkpoint, and write artifacts.
3. On completion, compute metrics, run TEE/MIS comparison, optional walk-forward, and trigger the GenAI report.
4. Verify every run is `COMPLETED`, `lookahead_violations == 0`, artifacts present; scale workers back to 0.

## Safety
Confirm no live/paper resources touched; `terraform`/IAM scoped to `backtest` env. No promotion is implied or performed.

## Output / Stop
Full-run acceptance report (coverage, metrics, gate mapping, cost). **Stop for approval.** Do not run implementation tests here.
