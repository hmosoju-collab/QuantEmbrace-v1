# Delivery-% Factor — Walk-Forward + Market Regime Overlay

**Status:** COMPLETE — advisory. Live trading remains BLOCKED.
**Date:** 2026-07-06
**Thesis / ADR:** ADR-034 · `docs/strategy/strategy-thesis-redirection-2026-06-15.md`
**Prior:** `docs/backtesting/factor-study-report.md` (delivery-% = robust factor).

> **Hard data limit:** the daily lake starts Oct 2019 — there is **no sustained bear**
> (2008/2011/2015/2018) in sample. A major-bear stress is therefore **untested**; the
> overlay below is validated only against the 2020 COVID crash and the 2022 dip.

Long-only top-20, monthly, top-200 liquid universe, NSE delivery cost stack (round-trip ≈ 0.322%). Overlay = market proxy > 200d SMA, else CASH.

## Headline (full period 2020–2025, net of costs)

| Variant | CAGR | Sharpe | MaxDD | Hit% |
|---|---:|---:|---:|---:|
| delivery (no overlay) | 23.7% | 1.41 | -22.2% | 72% |
| **delivery + 200d overlay** | 16.9% | 1.17 | -15.9% | 55% |
| benchmark (no overlay) | 17.4% | 0.99 | -25.8% | 66% |
| benchmark + overlay | 13.3% | 0.94 | -27.1% | 49% |

Overlay parked the delivery book in cash for **12** of 53 months.

## Walk-forward — per-calendar-year OOS (delivery, no overlay)

| Year | Return | Sharpe | MaxDD | Months |
|---|---:|---:|---:|---:|
| 2021 | 52.0% | 3.27 | -3.6% | 12 |
| 2022 | 1.7% | 0.18 | -10.2% | 12 |
| 2023 | 42.9% | 2.54 | -2.4% | 12 |
| 2024 | 10.4% | 0.62 | -15.6% | 12 |
| 2025 | 4.7% | 0.63 | -7.3% | 5 |

## Walk-forward — per-calendar-year OOS (delivery + overlay)

| Year | Return | Sharpe | MaxDD | Months |
|---|---:|---:|---:|---:|
| 2021 | 52.0% | 3.27 | -3.6% | 12 |
| 2022 | -9.3% | -1.17 | -10.1% | 12 |
| 2023 | 31.5% | 1.94 | -2.7% | 12 |
| 2024 | 10.4% | 0.62 | -15.6% | 12 |
| 2025 | -0.3% | -1.55 | 0.0% | 5 |

## Read

- **Walk-forward consistency:** delivery-% was positive in **5/5** calendar years OOS (the real test for a non-parametric factor — it cannot be curve-fit).
- **200d regime overlay HURT:** Sharpe 1.41 → 1.17, CAGR +6.7 pts (23.7% → 16.9%), MaxDD relief -6.2 pts (-22.2% → -15.9%).
- The overlay is a **standard, untuned 200d filter** (not optimised to this data). Its purpose is *sustained*-bear protection, which is **untestable here** (no such bear in sample); against V-shaped in-sample dips a trend filter whipsaws.

## Verdict & next gate

Delivery-% holds up out-of-sample across 5 years. The 200d overlay is NOT justified by this data; the sustained-bear question it targets needs pre-2019 data. **Carry delivery-% to paper validation (CNC, positional)** — it is the strongest positional candidate. It is NOT promotable: the major-bear case is untested (data limit), drawdowns are equity-sized, and paper validation against the live gate is required before any capital.

> Backtesting can recommend. It cannot promote. A human approves all production changes.
> Live trading remains BLOCKED; the 5-session paper gate is unaffected.
