# AWS Backtesting Lab — Phase 15B Report: Momentum Walk-Forward Validation

**Status:** COMPLETE — awaiting human approval
**Date:** 2026-06-14
**Governance rule:** Every phase writes a report and stops. A human approves before the next phase begins.

---

## Scope

Walk-forward validation of the `momentum` strategy (SMA crossover) on 47 NIFTY50
constituents over 2020–2024 using the `default` preset.

| Parameter | Value |
|---|---|
| Preset | `default` — 12-month IS / 3-month OOS / 3-month roll |
| Period | 2020-01-01 → 2024-12-31 |
| Symbols | 47 NIFTY50 (M&M / JIOFIN / ETERNAL excluded — post-2020 listing) |
| Folds | 15 IS→OOS folds |
| Param grid | 4 (short/long window combinations) |
| Data source | /Users/harimosoju/Documents/Claude/Projects/QuantEmbrace/QuantEmbrace - A Hedge Level Algo Trading System/backtest-data/lake/ohlcv |
| Data version | `bhavcopy-nse-2020-2024-v1` |
| Code version | `momentum-wf15b-v1` |
| Cost model | `in-eq-delivery-2024.10` (NSE equity delivery CNC) |
| Slippage | 5 bps per leg |

---

## Parameter Grid

| Combo | short\_window | long\_window | Description |
|---|---|---|---|
| A | 5 | 20 | Fast — many signals, captures short momentum bursts |
| B | 10 | 50 | Standard (Phase 13 params) |
| C | 20 | 100 | Slow — fewer signals, targets multi-month trends |
| D | 10 | 100 | Hybrid — fast trigger, slow trend filter |

---

## Fold Table

| Fold | Train window | OOS window | Best params | IS expectancy | OOS expectancy | OOS PF | Run ID |
|---|---|---|---|---|---|---|---|
| 0 | 2020-01-01 → 2021-01-01 | 2021-01-01 → 2021-04-01 | sw=10, lw=50 | ₹1201.9 | ₹-214.8 | 0.679 | `n/a` |
| 1 | 2020-04-01 → 2021-04-01 | 2021-04-01 → 2021-07-01 | sw=5, lw=20 | ₹700.1 | ₹586.6 | 3.043 | `n/a` |
| 2 | 2020-07-01 → 2021-07-01 | 2021-07-01 → 2021-10-01 | sw=10, lw=50 | ₹661.8 | ₹-7.2 | 0.984 | `n/a` |
| 3 | 2020-10-01 → 2021-10-01 | 2021-10-01 → 2022-01-01 | sw=10, lw=50 | ₹507.6 | ₹176.5 | 1.581 | `n/a` |
| 4 | 2021-01-01 → 2022-01-01 | 2022-01-01 → 2022-04-01 | sw=10, lw=50 | ₹437.6 | ₹267.1 | 2.084 | `n/a` |
| 5 | 2021-04-01 → 2022-04-01 | 2022-04-01 → 2022-07-01 | sw=5, lw=20 | ₹62.4 | ₹-693.7 | 0.217 | `n/a` |
| 6 | 2021-07-01 → 2022-07-01 | 2022-07-01 → 2022-10-01 | sw=5, lw=20 | ₹-198.6 | ₹-378.6 | 0.447 | `n/a` |
| 7 | 2021-10-01 → 2022-10-01 | 2022-10-01 → 2023-01-01 | sw=10, lw=50 | ₹17.5 | ₹-77.0 | 0.000 | `n/a` |
| 8 | 2022-01-01 → 2023-01-01 | 2023-01-01 → 2023-04-01 | sw=10, lw=50 | ₹228.4 | ₹-226.2 | 0.430 | `n/a` |
| 9 | 2022-04-01 → 2023-04-01 | 2023-04-01 → 2023-07-01 | sw=10, lw=50 | ₹308.0 | ₹348.2 | 2.655 | `n/a` |
| 10 | 2022-07-01 → 2023-07-01 | 2023-07-01 → 2023-10-01 | sw=10, lw=50 | ₹135.3 | ₹-320.2 | 0.236 | `n/a` |
| 11 | 2022-10-01 → 2023-10-01 | 2023-10-01 → 2024-01-01 | sw=10, lw=50 | ₹154.5 | ₹426.3 | inf | `n/a` |
| 12 | 2023-01-01 → 2024-01-01 | 2024-01-01 → 2024-04-01 | sw=10, lw=50 | ₹467.9 | ₹-204.1 | 0.383 | `n/a` |
| 13 | 2023-04-01 → 2024-04-01 | 2024-04-01 → 2024-07-01 | sw=10, lw=50 | ₹302.4 | ₹271.1 | 2.621 | `n/a` |
| 14 | 2023-07-01 → 2024-07-01 | 2024-07-01 → 2024-10-01 | sw=10, lw=50 | ₹111.4 | ₹-89.9 | 0.729 | `n/a` |

---

## Walk-Forward Aggregate

| Metric | Value | Gate |
|---|---|---|
| Folds | 15 | — |
| Mean IS expectancy | ₹339.9/trade | — |
| Mean OOS expectancy | ₹-9.0/trade | >₹0 → FAIL |
| Mean OOS profit factor | inf | >1.2 → PASS |
| Total OOS net P&L | ₹-18,681 | >₹0 → FAIL |
| IS→OOS degradation | -0.03 | >0.50 = not overfit → OVERFIT WARNING |
| Win consistency | 0.40 | >0.50 = robust → INCONSISTENT |
| Parameter stability | 0.80 | >0.70 = stable → OK |

---

## Parameter Robustness

How many folds selected each param value:

**long_window:**
  - `50` → 12/15 folds
  - `20` → 3/15 folds
**short_window:**
  - `10` → 12/15 folds
  - `5` → 3/15 folds

---

## Warnings

- IS→OOS degradation -0.03 < 0.5 — likely overfit.
- Win consistency 0.40 — OOS edge not consistent across folds.

---

## Eligibility Verdict

```
REJECT
```

OOS gates do not pass. Momentum in this configuration does not show robust
edge across the validation windows. Further parameter exploration or a different
strategy is warranted.

---

## Advisory Conclusions

> **Backtesting can recommend. Backtesting cannot promote. A human approves all production changes.**

Walk-forward validation gives a more robust estimate of out-of-sample edge than
a single held-out period. However:

- These results are advisory and non-authoritative for promotion.
- Live trading remains BLOCKED — paper session gates (≥5 consecutive passing sessions)
  have not been cleared.
- Shard-based capital resets still apply within each symbol window, so Sharpe and
  annualised return remain unreliable.
- The walk-forward IS→OOS gap tests for parameter overfitting, not concept-level edge.

---

## Phase 16 Options (operator selects)

**A. Intraday data acquisition** — identify a vendor (TrueData / GlobalDataFeeds /
  NSE historical API) to enable the remaining 5 strategies on 1m/5m/15m data.

**B. Portfolio-level Sharpe fix** — rerun walk-forward with `partition_by='symbol'`
  (all years in one shard per symbol) for a reliable equity curve and Sharpe.

**C. ORB / VWAP on a subset of intraday data** — if any intraday data is available,
  run a single-strategy probe on 1m Bhavcopy data for ORB.

**D. GenAI analysis** (Phase 10) — run Bedrock analysis over the walk-forward
  artifacts to generate a narrative summary and strategy improvement suggestions.

---

## Approval Required

Per governance: **a human must approve this report.**

Checklist for approver:
- [ ] Walk-forward setup reviewed (preset, param grid, date range, symbols)
- [ ] Fold table reviewed (IS→OOS expectancy pairs, param selection pattern)
- [ ] Aggregate metrics reviewed (degradation, win consistency, stability)
- [ ] Eligibility verdict understood and advisory-only nature confirmed
- [ ] No production changes will be made based solely on this report
- [ ] Next phase selected from A / B / C / D above
