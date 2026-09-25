# Strategy-Thesis Evidence — Cross-Sectional Factor Study (Daily NSE)

**Status:** COMPLETE — advisory. Live trading remains BLOCKED.
**Date:** 2026-06-15
**Thesis:** `docs/strategy/strategy-thesis-redirection-2026-06-15.md` (ADR-034)

Tests whether a positional/CNC cross-sectional factor strategy clears the full NSE
delivery cost stack where intraday technicals did not.

| Parameter | Value |
|---|---|
| Universe | top-200 NSE by trailing 60d turnover, point-in-time (survivorship-robust) |
| Symbols seen | 2750 |
| Portfolio | long-only top-20 equal-weight, monthly rebalance |
| Period | 2020-01-01 → 2025-06-30 |
| Cost model | NSE delivery (`in-eq-delivery-2024.10`) + 5bps/leg slippage; round-trip ≈ 0.322% |

## Results (net of costs unless noted)

| Factor | Net CAGR | Gross CAGR | Vol | Sharpe | MaxDD | Hit% | Turn/mo | Cost drag |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **momentum** | 22.1% | 23.8% | 25.1% | 0.93 | -34.7% | 66% | 36% | 6.2% |
| **reversal** | 16.2% | 20.2% | 22.6% | 0.78 | -35.6% | 57% | 89% | 15.2% |
| **lowvol** | 12.8% | 14.2% | 15.7% | 0.85 | -22.2% | 62% | 33% | 5.6% |
| **delivery** | 21.9% | 23.6% | 15.1% | 1.40 | -21.7% | 68% | 36% | 6.2% |
| **combo** | 25.5% | 28.5% | 16.8% | 1.44 | -26.9% | 66% | 64% | 10.9% |
| _benchmark (EW univ, gross)_ | — | 17.4% | 17.9% | 0.99 | -25.8% | 66% | — | — |

> **ETF/fund exclusion (methodology):** NSE ETFs and liquid funds (`*BEES`, `*ETF`, `LIQUID*`,
> `SETF*`, gold/silver, etc.) trade in the EQ segment with trivially ~100% delivery %, so an
> earlier run had the delivery book holding `LIQUIDBEES`/`LIQUIDCASE` (cash), `GOLDBEES`,
> `NIFTYBEES` — cash/index, not stock conviction. They are now filtered out (`_drop_funds`).
> **The delivery edge survives the exclusion essentially unchanged** (Sharpe 1.40 either way) —
> i.e. it is genuine equity selection, not an ETF artifact.

## Read (the honest version — see also the walk-forward report)

- **Most raw return is beta.** 2020–25 was a strong bull; the equal-weight liquid benchmark
  itself did 17.4% gross. `lowvol` *underperformed* it; `momentum`/`reversal` beat on return but
  not clearly on Sharpe. Headline CAGRs are flattered by the market, not pure alpha.
- **The robust, regime-stable edge is `delivery-%`** — Sharpe **1.40** vs benchmark 0.99, lowest
  drawdown (−21.7%), and **positive in 5/5 calendar years OOS** (see
  `delivery-walkforward-report.md`). The combo's slightly higher full-period Sharpe (1.44) is
  **period-dependent** (spectacular 2020–22, only ~benchmark in 2023–25) — not a stable edge.
- So: carry **`delivery-%`** forward, not the kitchen-sink combo.

> **Backtesting can recommend. It cannot promote. A human approves all production
> changes.** Long-only, monthly, ffill on delisting gaps (slightly optimistic on
> delisting losses). The next gate is the walk-forward (`delivery-walkforward-report.md`,
> done) + paper validation (positional/CNC) — NOT promotion. Live trading remains BLOCKED.
