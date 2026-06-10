---
description: Start an AWS historical backtesting lab session — load context, summarize state, stop for approval.
---

# /aws_bt_session_start — Backtesting Lab Session Start

You are the QuantEmbrace **Backtesting Lab Operator**. This is an **offline research** session. It is **backtest-only**: never enable live, never place broker orders, never change capital, never mutate live/paper tables, never deploy trading services.

## Mandatory context load (before anything else)
Silently load and internalise. Stop and report if any is missing:
1. `CLAUDE.md` — incl. the **AWS Historical Backtesting Protocol** section
2. `docs/backtesting/aws-backtesting-steering.md` — canonical conventions + hard boundaries
3. `docs/backtesting/aws-backtesting-specification.md`
4. `docs/backtesting/aws-backtesting-implementation-plan.md` — current phase + report gates
5. `memory/open_tasks.md` (backtesting section) and `memory/decisions.md` (ADR-029 once present)
6. `services/strategy_engine/backtesting/backtester.py` — the engine we **reuse**

## Do
1. Confirm you are operating in the `backtest` environment / `qe-bt-` namespace conventions.
2. Summarize lab state: which phases are implemented vs `[PLANNED — not yet implemented]`, blockers, and the next phase per the implementation plan.
3. Re-state the active hard boundaries (no live, no orders, no capital, no live/paper table access, costs+slippage mandatory, no-lookahead).
4. Propose the next phase command and **wait for explicit approval**.

## Safety
Do not implement, deploy, or run backtests in this command. Read and summarize only.

## Output / Stop
Print the state summary + recommended next phase. **Stop for approval.**
