# Phase 2 C7 + C6 — Turn-of-Month & Overnight Studies

**Status:** COMPLETE — advisory. Live trading remains BLOCKED.
**Date:** 2026-06-19   **Universe:** EW NIFTY50   **Period:** 2016-01-01 → 2026-06-12   **Corp-action guard:** ±20% daily/overnight mask.

> In-sample for idea selection; a promising result goes to a forward book vs the
> pre-registered Forward Factor Gate before any capital. Costs explicit, no relaxation.

## C7 — Turn-of-Month

### Stage 1 — event study (parameter-free): mean EW-index return by trading-day-around-turn

| Rel day | mean (bps) | n |
|---|---:|---:|
| +1 | 26.7 | 117 |
| +2 | 19.2 | 117 |
| +3 | -2.2 | 117 |
| +4 | 6.1 | 117 |
| +5 | 17.6 | 117 |
| -1 | 11.7 | 117 |
| -2 | 12.7 | 117 |
| -3 | 14.4 | 117 |
| -4 | 29.9 | 117 |
| -5 | -10.1 | 117 |

(rel +k = k-th trading day of month; −k = k-th from month-end. −1 = last day.)

### Stage 2 — long-in-window (last day + first 3) vs buy-hold vs rest-of-month

| Variant | Ann return | Vol | Sharpe | MaxDD | % days in mkt |
|---|---:|---:|---:|---:|---:|
| buy-hold (always in) | 17.8% | 15.9% | 1.11 | -37.3% | 100% |
| rest-of-month only | 10.5% | 14.3% | 0.77 | -35.7% | 79% |
| **TOM-only (ETF 0.05%)** | 5.9% | 7.1% | 0.84 | -20.4% | 18% |
| **TOM-only (conservative 0.15%)** | 4.6% | 7.2% | 0.67 | -22.1% | 18% |

**TOM-only by year (conservative cost):**

| Year | Return | Sharpe |
|---|---:|---:|
| 2016 | 4.5% | 0.63 |
| 2017 | 9.4% | 2.23 |
| 2018 | -12.0% | -1.95 |
| 2019 | -11.6% | -3.01 |
| 2020 | 7.8% | 0.73 |
| 2021 | 21.5% | 2.43 |
| 2022 | 9.3% | 1.45 |
| 2023 | 11.1% | 2.20 |
| 2024 | 0.4% | 0.08 |
| 2025 | 0.5% | 0.11 |
| 2026 | -6.7% | -0.98 |

**Verdict C7: NO EDGE — TOM window does not beat buy-hold risk-adjusted net of cost**

## C6 — Overnight vs Intraday decomposition

| Segment | mean/day (bps) | cumulative (geom) | ann | Sharpe |
|---|---:|---:|---:|---:|
| overnight (open/prev-close) | 13.12 | 2333% | 38.4% | 3.20 |
| intraday (close/open) | -5.94 | -79% | -14.7% | -1.10 |
| total (buy-hold) | 7.18 | 411% | — | — |

**Tradability (long-overnight-only = buy near close, sell near open, DAILY round-trip):**

| Cost/round-trip | net ann return |
|---|---:|
| 0.10% (×~252/yr) | 7.6% |
| delivery 0.22% (×~252/yr) | -20.5% |

**Verdict C6: REAL but NOT retail-harvestable — overnight = 13.1bps/day vs intraday -5.9bps/day, but daily round-trips make a long-overnight book 8%/yr after cost. A long-only holder already captures it (no incremental edge).**

## Synthesis & decision

- **C7: no standalone edge.** A mild turn-of-month concentration is real (in-window days run
  ~2.5× the per-day return of the rest of the month) but a long-in-window / flat-otherwise
  timing strategy does NOT beat buy-hold risk-adjusted net of cost — sitting in cash ~82% of
  days sacrifices more than the concentration is worth. Not a compelling small-account equity
  edge on its own. SHELVE (a cash-overlay variant is a low-risk cash-plus, not equity-beating).
- **C6: real, dramatic, NOT retail-tradable — and it explains the whole project.** The entire
  NSE large-cap equity premium accrues OVERNIGHT (overnight Sharpe ~3.2, cum ~+2300%); INTRADAY
  return is structurally NEGATIVE (~−6 bps/day, Sharpe ~−1.1, cum ~−79%). Harvesting the
  overnight needs a daily round-trip (cost-dead at realistic cost) and a long-only holder
  already captures it (no incremental edge). **This is the unifying reason every intraday
  strategy failed** (Phase B retirements, C1 gap reaction): intraday isn't merely cost-walled —
  it is a negative-drift desert. The positive premium lives in *holding overnight / positionally*,
  which is exactly the (forward-tracked) factor track.
- Neither becomes a new strategy. Standing posture unchanged: accrue the forward books, deploy
  nothing. C6 is recorded as the structural explanation of the intraday-failure thesis.

> Backtesting can recommend. It cannot promote. A human approves all production changes.
