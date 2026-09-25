# Momentum (12-1) Paper Book — Monthly Forward Replay (Out-of-Sample)

**Status:** advisory · isolated paper book · live trading remains BLOCKED.
**Date:** 2026-07-15   **Inception:** 2025-12-31   **Latest MTM:** 2026-07-14   **Factor:** momentum
**Tool:** `scripts/paper/replay_delivery_book_forward.py` · **Builds on:** ADR-034/035/036.

Genuine out-of-sample: the Momentum (12-1) factor was characterised on 2020-2025 (momentum also verified 2016-2025) and the book seeded at 2025-12-31, so every 2026 month here is OOS.

> **Sample-size honesty:** this is **6 complete monthly returns** (+1 partial). That is far too few for any
> Sharpe/t-stat claim — treat it as live plumbing proof + early hypothesis monitoring,
> NOT validation. The benchmark column is the point: does the book add alpha over the
> equal-weight market on the SAME months, or just ride beta?

## Monthly returns (net of NSE delivery costs)

| Month-end | Book | Benchmark (EW univ) | Alpha |
|---|---:|---:|---:|
| 2026-01-30 | -6.79% | -3.72% | -3.07% |
| 2026-02-27 | +3.86% | +1.37% | +2.49% |
| 2026-03-31 | -12.35% | -11.61% | -0.74% |
| 2026-04-30 | +20.35% | +15.02% | +5.33% |
| 2026-05-29 | +5.60% | +2.04% | +3.55% |
| 2026-06-30 | -0.62% | +0.42% | -1.04% |
| 2026-07-14 (partial) | -0.71% | +0.42% | -1.13% |

**Cumulative since inception:** book **+6.23%** vs benchmark **+2.09%**  →  spread **+4.14 pts**.
Current NAV ₹1,062,287 (seed ₹1,000,000).

## Read

- A handful of months cannot prove edge; it can only (a) prove the rebalance/cost/NAV
  plumbing runs forward cleanly, and (b) flag early if reality diverges hard from the
  backtest. Judge alpha vs benchmark, not the raw number.
- The honest validation horizon for a monthly strategy is **12-24 months** of forward
  record, or the extended-history walk-forward (2016-2026, now that 2016-2018 is in the
  lake) — NOT this window.

> Backtesting can recommend. It cannot promote. A human approves all production changes.
> Live trading remains BLOCKED.
