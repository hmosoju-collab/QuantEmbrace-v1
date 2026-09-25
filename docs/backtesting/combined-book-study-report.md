# Delivery + Momentum Combined Book — Diversification Study

**Status:** COMPLETE — advisory. Live trading remains BLOCKED.
**Date:** 2026-06-19   **Period:** 2020-01-01 → 2026-06-12   **Universe:** top-200 liquid, top-20 each leg
**Tool:** `scripts/backtest/run_combined_book_study.py` (reuses `run_factor_study`).

> **Scope honesty:** portfolio construction on the SAME data the factors were found on —
> shows whether combining HELPS historically, NOT a fresh OOS test. The two forward paper
> books are the live OOS track. Weights (50/50, inverse-vol) are non-fitted.

**Correlation of the two legs' monthly net returns: +0.67** (the diversification thesis — low/negative is what makes combining work). Mean inverse-vol delivery weight: 60%.

## Full period (net of costs)

| Book | CAGR | Vol | Sharpe | MaxDD | Hit% |
|---|---:|---:|---:|---:|---:|
| delivery | 12.8% | 15.7% | 0.85 | -26.4% | 63% |
| momentum | 17.6% | 26.2% | 0.75 | -35.4% | 60% |
| **combo 50/50** | 15.7% | 19.2% | 0.86 | -28.9% | 63% |
| combo inverse-vol | 15.1% | 18.3% | 0.87 | -28.3% | 65% |
| _benchmark (EW, gross)_ | 14.6% | 18.6% | 0.83 | -25.8% | 63% |

**Verdict: NO MEANINGFUL DIVERSIFICATION** — legs are **+0.67 correlated** (not negative); the
combo's MaxDD (−28.9%) is *worse* than delivery alone (−26.4%) and ~the benchmark (−25.8%), and the
Sharpe lift is trivial (0.86 vs 0.85/0.75, barely above benchmark 0.83). See Read.

## Sub-period robustness

| Book | 2020–22 Sharpe / MaxDD | 2023–26 Sharpe / MaxDD |
|---|---:|---:|
| delivery | 1.60 / -9.9% | 0.61 / -26.4% |
| momentum | 0.86 / -22.8% | 0.76 / -35.4% |
| **combo 50/50** | 1.27 / -16.3% | 0.77 / -28.9% |

## Combo 50/50 — per-year (walk-forward consistency)

| Year | Return | Sharpe | MaxDD | Months |
|---|---:|---:|---:|---:|
| 2021 | 43.6% | 3.10 | -2.1% | 12 |
| 2022 | -5.1% | -0.24 | -16.3% | 12 |
| 2023 | 47.7% | 2.30 | -7.8% | 12 |
| 2024 | 12.8% | 0.71 | -14.1% | 12 |
| 2025 | -3.0% | -0.07 | -10.9% | 12 |
| 2026 | 0.2% | 0.16 | -12.2% | 5 |

## Read — diversification thesis FAILS on proper history

- **Legs are +0.67 correlated, not negative.** The 5-month forward decoupling (delivery −9.9% vs
  momentum +5.4%) that motivated this was a single-regime artifact — the Mar-2026 crash + Apr
  V-recovery, where high-beta momentum and defensive delivery diverged *once*. Over 6 years they
  co-move; both are long-only equity beta. A 5-month window manufactured an illusion 6 years refute.
- **No drawdown benefit — which was the whole point.** Combo 50/50 MaxDD −28.9% is *worse* than
  delivery alone (−26.4%) and the benchmark (−25.8%); it only beats momentum (−35.4%). Sharpe lift
  is trivial (0.86 vs 0.85/0.75) and barely above benchmark (0.83).
- **Both standalone edges have DECAYED.** Delivery Sharpe 1.60 (2020–22) → **0.61 (2023–26)**; the
  combo's strong years (2021 +44%, 2023 +48%) are behind it (2024 +13%, 2025 −3%, 2026 +0.2%).
  Delivery's full-period Sharpe is now 0.85 vs the 1.40 reported for 2020–2025 — the weak 2026
  dragged it down, consistent with the −9.85% forward read.
- **None of these long-only equity books convincingly beats the equal-weight benchmark** risk-
  adjusted over the full window (0.85 / 0.75 / 0.86 vs 0.83). The early-window factor edges have
  largely washed out.

**Decision: do NOT build a combined forward book — combining two +0.67-correlated long-only equity
factors does not diversify.** Genuine diversification needs a driver structurally decorrelated from
long-only equity beta (market-neutral / long-short, or a different asset/structure) — another
long-only equity factor cannot deliver it (echoes the ADR-036 correlation verdict). delivery is still
the best single risk-adjusted equity book, but its edge has weakened recently; the running forward
paper books are the truth serum.

> Backtesting can recommend. It cannot promote. A human approves all production changes.
