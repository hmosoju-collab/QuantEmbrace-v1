# Strategy-Thesis Evidence — Cross-Sectional Factor Study (Daily NSE)

**Status:** COMPLETE — advisory. Live trading remains BLOCKED.
**Date:** 2026-06-15
**Thesis:** `docs/strategy/strategy-thesis-redirection-2026-06-15.md` (ADR-034)

Tests whether a positional/CNC cross-sectional factor strategy clears the full NSE
delivery cost stack where intraday technicals did not.

| Parameter | Value |
|---|---|
| Universe | top-200 NSE by trailing 60d turnover, point-in-time (survivorship-robust) |
| Symbols seen | 3271 |
| Portfolio | long-only top-20 equal-weight, monthly rebalance |
| Period | 2016-01-01 → 2025-12-31 |
| Cost model | NSE delivery (`in-eq-delivery-2024.10`) + 5bps/leg slippage; round-trip ≈ 0.322% |

## Results (net of costs unless noted)

| Factor | Net CAGR | Gross CAGR | Vol | Sharpe | MaxDD | Hit% | Turn/mo | Cost drag |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **momentum** | 13.2% | 14.6% | 24.9% | 0.63 | -38.0% | 62% | 33% | 7.0% |
| **reversal** | 0.5% | 4.1% | 29.7% | 0.17 | -53.8% | 57% | 89% | 18.6% |
| **lowvol** | 8.6% | 9.9% | 16.4% | 0.59 | -31.0% | 60% | 33% | 6.8% |
| **delivery** | 5.5% | 7.1% | 19.2% | 0.38 | -42.9% | 54% | 39% | 8.2% |
| **combo** | 12.2% | 14.7% | 18.4% | 0.72 | -29.4% | 63% | 58% | 12.1% |
| _benchmark (EW univ, gross)_ | — | 10.9% | 21.8% | 0.59 | -49.3% | 62% | — | — |

## Read

- Best net factor: **momentum** (13.2% net CAGR, Sharpe 0.63).
- Benchmark (equal-weight liquid universe, gross): 10.9% CAGR, Sharpe 0.59.
- Factors beating the benchmark net of costs: momentum, combo.

> **Backtesting can recommend. It cannot promote. A human approves all production
> changes.** Long-only, monthly, ffill on delisting gaps (slightly optimistic on
> delisting losses); no walk-forward parameter optimisation yet. If a factor clears
> the benchmark net with margin, the next gate is a walk-forward + paper validation
> (positional/CNC) — NOT promotion. Live trading remains BLOCKED.
