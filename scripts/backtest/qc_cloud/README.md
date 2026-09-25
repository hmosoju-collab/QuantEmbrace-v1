# QC Cloud — XSMOM Survivorship Falsification (operator runbook)

_ADR-041 Phase 2b · ~10 minutes · $0 (QC free tier)_

## Why

The local screen's XSMOM passed (Sharpe 0.86) **on a survivorship-selected
universe** (today's mega-caps). Before it can proceed to Phase 3, it must be
re-tested on QuantConnect's **survivorship-bias-free point-in-time** US equity
data. This is the platform's F1 lesson applied: always validate on data that
could not have known the winners.

## Pre-registered verdict rule (do not adjust after seeing results)

- Survivorship-free net **Sharpe ≥ 0.80** → XSMOM stays shortlisted.
- **Sharpe < 0.80** → XSMOM is falsified and dies, recorded like every other
  falsified hypothesis.

## Steps

1. Create a free account at https://www.quantconnect.com (no card needed).
2. In the web IDE: **Create Project → Python Algorithm**.
3. Replace the project's `main.py` with
   `scripts/backtest/qc_cloud/xsmom_survivorship_test.py` (paste verbatim).
4. Click **Backtest** (free tier includes cloud backtests on QC data;
   2006→2026 daily may take several minutes).
5. Report back from the backtest report page:
   - Sharpe Ratio · Compounding Annual Return · Max Drawdown
   - the annual-returns table
   - (screenshot or paste is fine)

## Notes

- While logged in, also run `lean login` locally (User ID + API token from
  Account page) — that unlocks `lean init`/`lean cloud` for future sessions.
- The algorithm holds the top-10 by 12-1 momentum from a monthly point-in-time
  top-100-dollar-volume universe, QC default fee model, monthly rebalance —
  the same design as the local screen's XSMOM, minus the survivorship bias.
