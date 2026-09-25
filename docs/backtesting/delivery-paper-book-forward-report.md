# Delivery-% Paper Book — Monthly Forward Replay (Out-of-Sample)

**Status:** advisory · isolated paper book · live trading remains BLOCKED.
**Date:** 2026-07-15   **Inception:** 2025-12-31   **Latest MTM:** 2026-07-14   **Factor:** delivery
**Tool:** `scripts/paper/replay_delivery_book_forward.py` · **Builds on:** ADR-034/035/036.

Genuine out-of-sample: the Delivery-% factor was characterised on 2020-2025 (momentum also verified 2016-2025) and the book seeded at 2025-12-31, so every 2026 month here is OOS.

> **Sample-size honesty:** this is **6 complete monthly returns** (+1 partial). That is far too few for any
> Sharpe/t-stat claim — treat it as live plumbing proof + early hypothesis monitoring,
> NOT validation. The benchmark column is the point: does the book add alpha over the
> equal-weight market on the SAME months, or just ride beta?

## Monthly returns (net of NSE delivery costs)

| Month-end | Book | Benchmark (EW univ) | Alpha |
|---|---:|---:|---:|
| 2026-01-30 | -7.32% | -3.72% | -3.60% |
| 2026-02-27 | +4.64% | +1.37% | +3.27% |
| 2026-03-31 | -10.62% | -11.61% | +0.99% |
| 2026-04-30 | +6.62% | +15.02% | -8.40% |
| 2026-05-29 | +0.16% | +2.04% | -1.89% |
| 2026-06-30 | -0.81% | +0.42% | -1.22% |
| 2026-07-14 (partial) | +1.28% | +0.42% | +0.86% |

**Cumulative since inception:** book **-7.16%** vs benchmark **+2.09%**  →  spread **-9.25 pts**.
Current NAV ₹928,409 (seed ₹1,000,000).

## Read

- A handful of months cannot prove edge; it can only (a) prove the rebalance/cost/NAV
  plumbing runs forward cleanly, and (b) flag early if reality diverges hard from the
  backtest. Judge alpha vs benchmark, not the raw number.
- The honest validation horizon for a monthly strategy is **12-24 months** of forward
  record, or the extended-history walk-forward (2016-2026, now that 2016-2018 is in the
  lake) — NOT this window.

> Backtesting can recommend. It cannot promote. A human approves all production changes.
> Live trading remains BLOCKED.
