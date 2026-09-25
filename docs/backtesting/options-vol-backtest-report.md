# Defined-Risk Options-Vol Backtest (Options/Vol track — Phase O-2)

**Pre-registered:** 2026-06-19 · **Mode:** REAL F&O bhavcopy (slip 2.5%) · **Live trading: BLOCKED**
Advisory only. Defined-risk iron condors, held to expiry, full NSE options cost stack
(in-opt-2024.10: ₹20/leg flat + STT 0.1% sell + txn 0.035% + GST + stamp).
A PASS ⇒ human review for a SMALL gated pilot — never auto-deploy. Backtesting cannot promote.

> REAL NSE F&O-bhavcopy chains (EOD), monthly condor held to expiry, 2.5% per-leg slippage haircut. Real strikes/premiums/skew. EOD close is not a guaranteed fill — a PASS warrants an intraday-vendor re-test (Algotest/GDFL) before any pilot.

## Setup
- NAV ₹1,000,000 · NIFTY lot 75 · risk budget 2%/cycle (hard tail cap)
- Structure: iron condor, shorts ±4% OTM, wings +2%, 21 td to expiry
- Cycles: **31**

## Results (net of full cost stack)
- Total net: **₹-53,953** · annualised ≈ **-2.1%**
- Profit factor: **0.67** · expectancy/cycle: **₹-1,740**
- Positive years: **50%** · max drawdown: **-8.7%**
- Worst single cycle: **₹-27,754** (risk budget = ₹20,000/cycle)

### By year (net P&L)
| Year | Net |
|---|---:|
| 2022 | ₹-30,591 |
| 2023 | ₹-49,017 |
| 2024 | ₹21,894 |
| 2025 | ₹3,762 |

## Pre-registered O-2 gate (fixed 2026-06-19 — not relaxed)
| Criterion | Result |
|---|---|
| expectancy>0 | ❌ FAIL |
| PF>1.3 | ❌ FAIL |
| pos-years>=60% | ❌ FAIL |
| maxDD<=20% | ✅ PASS |
| defined-risk cap held | ✅ PASS |

## VERDICT
**FAIL — does not clear the O-2 gate net of costs / risk.**

---
*The defined-risk cap is the whole point: a condor's max loss is bounded by wing width − credit,
sized to ≤2% of NAV/cycle, so even a Mar-2020-type cycle cannot exceed the budget.
A PASS on REAL licensed chains (not synthetic) ⇒ human review for a small gated pilot. Live BLOCKED.*
