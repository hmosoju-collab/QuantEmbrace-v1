# Volatility-Premium Screen (Options/Vol track — Phase O-1, FREE)

**Pre-registered:** 2026-06-19 · **Status:** advisory screen · **Live trading: BLOCKED**
A PASS authorises *buying option-chain data for an O-2 backtest only* — never deployment.
Backtesting can recommend; it cannot promote. A human approves all production changes.

## What this measures
VRP = INDIA VIX (implied) − NIFTY 50 realised vol over the next 21 trading days,
in annualised vol points. Forward-realised is an *ex-post measurement* of whether vol
sellers got paid — not a tradable signal, so it is not lookahead.

## Data
- Window: **2020-01-01 → 2025-12-01**  ·  trading days: **1,471**  ·  monthly cycles: **71**
- Source: Zerodha Kite indices + INDIA VIX (free underlying inputs). NIFTY lot = 75
  (current; was 50/25 earlier — the ₹ proxy uses the current lot).

## VRP — is the premium there?
- Mean VRP: **+2.48** vol points · Median: **+3.09**
- VIX > realised on **80%** of days
- Positive-mean-VRP in **100%** of calendar years

### By year (mean VRP, vol points)
| Year | Mean VRP |
|---|---:|
| 2020 | +1.82 |
| 2021 | +3.20 |
| 2022 | +2.98 |
| 2023 | +2.87 |
| 2024 | +1.43 |
| 2025 | +2.59 |

### By regime (the short-vol failure mode lives here)
| Regime | Mean VRP | Days |
|---|---:|---:|
| bear | +3.83 | 336 |
| bull | +3.51 | 828 |
| chop | +1.98 | 258 |
| n/a | -21.52 | 49 |

## ₹ economics — coarse monthly short-straddle vega proxy (defined-risk condor costs)
Per cycle, per NIFTY lot (estimates — formal options cost model is an O-2 deliverable):
- Median **gross** VRP edge: **₹10,559**
- Median estimated **round-trip cost**: **₹330**
- Median **net** edge: **₹10,232**

## Tail — the steamroller (MANDATORY caution, independent of verdict)
- Worst single cycle (short-vol proxy): **₹-111,424**
- Cumulative short-vol proxy max drawdown: **₹-122,197**
- ⚠️ **FAT LEFT TAIL** — worst cycle loses 10.6× the median gross edge

Short vol earns small premiums most of the time and gives them back in crashes. A
positive mean with a fat tail is NOT a green light — O-2 must size for the tail and
test crash days (Mar-2020, budget/election/expiry gaps) explicitly.

## Pre-registered gate (fixed 2026-06-19 — not relaxed)
| Criterion | Result |
|---|---|
| G1 mean VRP>1.0 | ✅ PASS |
| G2a VIX>RV on>=65% days | ✅ PASS |
| G2b +mean VRP in>=70% years | ✅ PASS |
| G3 gross>=2x cost & net>0 | ✅ PASS |

## VERDICT
**PASS — VRP justifies buying option-chain data (O-2). NOT deployment.**

---
*Advisory only. No orders, no live/paper trading, no capital changes. If PASS, the next
step is procuring 3–5 yr NIFTY option-chain history (Algotest export / GDFL / TrueData)
for a defined-risk O-2 backtest with the real options cost stack — then human review.*
