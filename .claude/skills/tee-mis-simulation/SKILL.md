---
name: tee-mis-simulation
description: Simulate and compare old-vs-new TradeExitEngine exits and MIS intraday square-off in the backtest lab. Use for exit-logic A/B comparisons and verifying intraday positions square off. Simulation only — no broker, no live behavior change.
---

# TEE / MIS Simulation

References: `docs/architecture/hybrid-trade-exit-and-mis-squareoff.md`, `docs/architecture/partial-profit-booking-design.md`, `docs/backtesting/metrics-catalog.md`.

## When to use
Comparing old vs new strategy-aware exits (R-based exits + partial profit booking); validating MIS square-off at 15:05/15:15 IST.

## Procedure
1. Replay the same signal set with old vs new exit logic; hold snapshot/costs/slippage constant.
2. Simulate MIS square-off; assert no intraday position carries over.
3. Produce a side-by-side comparison: P&L, win rate, expectancy, avg R, drawdown, exit routing.

## Rules
Read-only replay of production exit logic. Never modify live TEE behavior. Advisory output only.
