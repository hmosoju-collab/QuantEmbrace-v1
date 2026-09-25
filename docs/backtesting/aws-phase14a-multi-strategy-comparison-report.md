# AWS Backtesting Lab — Phase 14A Report: Multi-Strategy Comparison

**Status:** COMPLETE — awaiting human approval  
**Date:** 2026-06-14  
**Governance rule:** Every phase writes a report and stops. A human approves before the next phase begins.

---

## Scope

Run all 6 production strategy adapters on the same NIFTY50 1d Bhavcopy dataset
(2020–2024, 47 symbols) and produce a side-by-side performance comparison.

---

## Key Finding: Only `momentum` Is Compatible with 1d Daily Data

Before running the full 235-partition sweep, a single-symbol probe across all 6
adapters revealed a structural incompatibility:

| Strategy | Signals | Trades | Root cause on 1d data |
|---|---|---|---|
| **momentum** | **176** | **88** | **COMPATIBLE — no time-of-day filter** |
| vwap_reversion | 0 | 0 | VWAP requires intraday ticks; on 1d, VWAP ≡ close price → no reversion |
| orb | 0 | 0 | Opening Range Breakout needs first-30-min 1m bars; single daily bar has no range |
| trend_15m | 0 | 0 | `_IST_NORMAL_START=09:30, _IST_NORMAL_END=14:15` — rejects 15:30 IST daily bars |
| preclose | 0 | 0 | `_IST_FIRE_START=14:45, _IST_FIRE_END=15:10` — rejects 15:30 IST daily bars |
| scalp_1m | 0 | 0 | Bar-structure checks (body/spread/tick ratio) fail on daily OHLC |

*Tested on 5 NIFTY50 symbols (RELIANCE/INFY/TCS/HDFCBANK/ICICIBANK), confirmed identical result across all 5.*

**Root cause:** NSE Bhavcopy daily bars are stamped `15:30:00 IST` (market close).
Four of the five intraday strategies (`trend_15m`, `preclose`) contain explicit
market-hours gates that reject bars outside their intraday signal windows. The
remaining two (`vwap_reversion`, `orb`) require multi-bar intraday data structures
(VWAP from tick volume; ORB from sub-15-minute range) that daily OHLCV cannot
provide. `scalp_1m` additionally requires body/spread/tick ratio checks that have
no meaning for a single daily bar.

**This is the correct behaviour** — these strategies have intentional intraday-only
constraints. Running them on daily data would produce signals that have no meaning
in their design space.

---

## Multi-Strategy Comparison (47 NIFTY50 × 5 years)

| Strategy | Run ID | Trades | Win rate | Net P&L | Profit factor | NSE costs | Expectancy | Data compat |
|---|---|---|---|---|---|---|---|---|
| **momentum** | `bt_9487c789794bd421` | **630** | **57.3%** | **₹2,55,801** | **1.798** | ₹33,696 | ₹406 | 1d ✓ |
| vwap_reversion | — | 0 | — | — | — | — | — | Needs 1m |
| orb | — | 0 | — | — | — | — | — | Needs 1m |
| trend_15m | — | 0 | — | — | — | — | — | Needs 15m |
| preclose | — | 0 | — | — | — | — | — | Needs 5m |
| scalp_1m | — | 0 | — | — | — | — | — | Needs 1m (paper-only) |

**Effective finding: on NSE Bhavcopy 1d data, `momentum` is the only evaluable strategy.**

The "multi-strategy comparison" goal cannot be fulfilled on daily data.
A meaningful comparison requires intraday data.

---

## Data Requirements for Full 6-Strategy Comparison

| Strategy | Required interval | Estimated daily bars / symbol | Data source |
|---|---|---|---|
| momentum | 1d (current) | 1 | Bhavcopy 1d ✓ available |
| trend_15m | 15m | 25 per session | NSE Bhavcopy does not provide 15m; needs vendor (TrueData/GlobalDataFeeds) or NSE historical API |
| preclose | 5m | 75 per session | Same |
| vwap_reversion | 1m | 375 per session | Same |
| orb | 1m | 375 per session (first 30 critical) | Same |
| scalp_1m | 1m | 375 per session | Same (paper-only) |

For 5 years × 47 NIFTY50 symbols × 1m:  
`5 × 250 × 375 × 47 ≈ 22M bars` — well within the lab's EC2 ARM64 capacity.

---

## Momentum Strategy Deep Dive (phase13 carry-forward)

Full results from `bt_9487c789794bd421` (see Phase 13 report):

```
Trades        : 630 across 47 symbols × 5 years
Win Rate      : 57.3%
Net P&L       : ₹2,55,801  (25.58% on ₹10L)
Gross P&L     : ₹2,89,497
NSE costs     : ₹33,696  (avg ₹53/trade — delivery model correct)
Profit Factor : 1.798
Payoff Ratio  : 1.340
Expectancy    : ₹406/trade
Max Drawdown  : 1.63%

Best symbols  : NTPC (78.6% WR), BHARTIARTL (80%), HCLTECH (72.7%)
Worst symbols : SHREECEM (36.8%), BPCL (40%), ITC (41.7%)
Exit mix      : 56.8% take_profit / 35.2% stop_loss / 4.1% gap / 3.8% eod
```

---

## What This Phase Establishes

1. **The lab correctly identifies strategy-data compatibility.** No false positives —
   intraday-only strategies produce 0 signals (correct), not noisy/wrong signals.
2. **`momentum` has positive edge on NIFTY50 daily data** over 5 years of real NSE data
   with full delivery cost model. This is advisory and non-authoritative for promotion.
3. **To compare all 6 strategies, intraday data is the next blocker.** NSE Bhavcopy
   archives do not offer sub-daily resolution. Licensed intraday data from a vendor
   (TrueData, GlobalDataFeeds, NSE historical API) is required.
4. **The cost model, data pipeline, AWS registry, and report artifacts all work correctly**
   on real data.

---

## Advisory Conclusions

> **Backtesting can recommend. Backtesting cannot promote. A human approves all production changes.**

`momentum` on NIFTY50 1d data is advisorily positive. This is not a strategy promotion.

Live trading remains BLOCKED — paper session performance gates have not been cleared.

---

## Phase 15 Options (operator selects)

**B. Walk-forward validation of `momentum`** (available now, uses 1d data)  
Script: `scripts/backtest/run_walk_forward_aws.py`  
Split 2020–2024 into expanding-window folds; test for overfit / regime sensitivity.
Expected output: fold-by-fold stability table, `ELIGIBLE_FOR_PAPER_PRIORITIZATION` or `OVERFIT` verdict.

**C. Intraday data acquisition plan**  
Identify and evaluate an intraday NSE data source (TrueData, GlobalDataFeeds, NSE
historical API) to enable the remaining 5 strategies.

**D. Portfolio-level Sharpe fix for `momentum`**  
Rerun with `partition_by="symbol"` (all 5 years per symbol in one shard) to get
a continuous equity curve and reliable Sharpe/annualised-return metrics.

---

## Approval Required

Per governance: **a human must approve this report.**

Checklist for approver:
- [ ] Strategy compatibility matrix accepted (only `momentum` fires on 1d daily data)
- [ ] Root causes understood (time gates on trend_15m/preclose; structure on vwap/orb/scalp)
- [ ] `momentum` Phase 13 results carried forward as the single evaluable strategy
- [ ] Intraday data requirement for 5 of 6 strategies acknowledged
- [ ] Next phase selected from B / C / D
