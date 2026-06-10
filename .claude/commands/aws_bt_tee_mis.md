---
description: "Phase 6 — TEE old-vs-new exit comparison + MIS intraday square-off simulation."
---

# /aws_bt_tee_mis — Phase 6: TEE Old-vs-New + MIS Square-Off

Replay identical signals through the **old** exit path and the **new** strategy-aware TradeExitEngine (R-based exits + partial profit booking), and simulate **MIS** intraday square-off. **Simulation only — no broker, no live.**

## Load first
`docs/architecture/hybrid-trade-exit-and-mis-squareoff.md`, `docs/architecture/partial-profit-booking-design.md`, `aws-backtesting-specification.md` (§2.6), `metrics-catalog.md`.

## Do
1. Run the same signal set with old vs new exit logic over the lake; keep all else identical (same data snapshot, costs, slippage).
2. Simulate MIS square-off at 15:05 IST (proactive) / 15:15 IST (hard) — all intraday positions closed; verify none carry over.
3. Produce a side-by-side comparison (P&L, win rate, expectancy, avg R, drawdown, exits-routed-correctly).

## Safety
Does not change live TEE behavior. Read-only replay; advisory comparison.

## Output / Stop
TEE old-vs-new + MIS square-off comparison report. **Stop for approval.**
