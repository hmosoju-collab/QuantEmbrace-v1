# F1 — Overnight Index-Futures Premium (REAL near-month NIFTY futures (basis+roll included))

**Pre-registered:** 2026-06-20 · **Data:** REAL near-month NIFTY futures (basis+roll included) · **Live trading:
BLOCKED** · Advisory only.

## Question
(1) Does the overnight premium survive the **futures** cost stack (in-fut-2024.10)?
(2) Is the leveraged overnight **gap tail** survivable on a **₹500,000** account?

## Overnight vs intraday (2022-06-01 → 2025-06-30, 733 days)
- Overnight (close→next open): **+3.2 bps/day** · Intraday (open→close): **n/a (see C6)**

## Economics @ 1 NIFTY lot (lot 75; notional ≈ 2.7–3.9× the ₹5L NAV — leveraged)
- Mean net overnight P&L: **₹129/night** · net Sharpe (ann): **0.34**
- 1-lot annualised return on ₹5L: **+6.1%** · max drawdown: **-24.5%**
- Positive years: **75%**

### By year (net P&L, 1 lot)
| Year | Net |
|---|---:|
| 2022 | ₹11,411 |
| 2023 | ₹63,192 |
| 2024 | ₹43,546 |
| 2025 | ₹-23,839 |

## The gap tail (the futures killer)
- Worst single overnight: **-3.3%** = **₹-56,749** = **-11.3% of NAV in one night**
- Nights worse than −3%: **1** · worse than −5%: **0**
- Affordable lots on ₹5L (margin ≈15%/lot ≈ ₹242,187): **2**

## Pre-registered F1 gate (fixed 2026-06-20 — not relaxed)
| Criterion | Result |
|---|---|
| G1 net overnight>0 (beats futures cost) | ✅ PASS |
| G2 pos-years>=70% | ✅ PASS |
| G3 worst night ≤25% NAV @1 lot | ✅ PASS |
| G4 Sharpe≥1.0 & ann≥12% | ❌ FAIL |
| G5 maxDD≤25% | ✅ PASS |

## VERDICT
**FAIL — naked real-futures overnight does not clear the DD-aware gate (the tail/DD); next = defined-risk hedged variant (long future + protective put).**

---
*A naked leveraged overnight carry that fails only on the tail (G3) is NOT dead — it argues for the
DEFINED-RISK variant: long future + protective OTM put (priced from our options lake) = risk-capped
overnight carry. F1-full would test that on REAL futures with basis + roll. PASS ⇒ forward book → human
review for a small gated pilot. Never auto-deploy. Live BLOCKED.*
