# O1 — Scheduled-Event IV-Crush Screen (Options/Futures program)

**Pre-registered:** 2026-06-20 · **Data:** INDIA VIX + NIFTY50 (free) · **Live trading: BLOCKED** · Advisory.

Does implied vol crush after scheduled events, and does a defined-risk short straddle held across the event
beat **random-day** short vol (the control that makes this distinct from the failed unconditional VRP)?

## Result (n=28 events; small ⇒ screen, not proof)
- Mean IV crush: **+0.7 vol points** · short-straddle win rate: **68%**
- Mean event net P&L (1 lot proxy): **₹2,062** vs random-day baseline **₹2,009**
  → edge ratio **1.03×**
- Worst event: **Budget-2021** at **₹-54,451** (the event-surprise tail)

## Per-event detail (verify the curated dates here)
| Event | conf | entry | VIX in→out | crush(vp) | move | net ₹ |
|---|---|---|---|---:|---:|---:|
| Budget-2020 | HIGH | 2020-01-31 | 17.4→15.8 | +1.6 | -2.1% | ₹-7,881 |
| Budget-2021 | HIGH | 2021-01-29 | 25.3→23.4 | +2.0 | +7.4% | ₹-54,451 |
| Budget-2022 | HIGH | 2022-01-31 | 21.9→18.6 | +3.3 | +2.5% | ₹-16,241 |
| RBI | BE | 2022-02-09 | 18.6→18.7 | -0.1 | -0.5% | ₹7,599 |
| RBI | BE | 2022-04-07 | 19.0→18.3 | +0.7 | +0.2% | ₹18,271 |
| RBI | BE | 2022-06-07 | 20.4→19.1 | +1.3 | +0.4% | ₹10,147 |
| RBI | BE | 2022-08-04 | 19.3→19.3 | -0.0 | +0.8% | ₹10,166 |
| RBI | BE | 2022-09-29 | 21.3→21.4 | -0.1 | +0.4% | ₹17,178 |
| RBI | BE | 2022-12-06 | 14.0→13.4 | +0.6 | -0.2% | ₹9,008 |
| Budget-2023 | HIGH | 2023-01-31 | 16.9→15.7 | +1.1 | -0.3% | ₹9,245 |
| RBI | BE | 2023-02-07 | 14.1→13.0 | +1.1 | +1.0% | ₹-1,891 |
| RBI | BE | 2023-04-05 | 12.4→12.3 | +0.1 | +0.4% | ₹10,157 |
| RBI | BE | 2023-06-07 | 11.4→11.1 | +0.3 | -0.9% | ₹-2,823 |
| RBI | BE | 2023-08-09 | 11.1→11.5 | -0.4 | -1.0% | ₹-5,720 |
| RBI | BE | 2023-10-05 | 10.9→11.4 | -0.5 | -0.2% | ₹10,811 |
| RBI | BE | 2023-12-07 | 12.7→12.8 | -0.1 | +0.5% | ₹9,316 |
| Budget-2024 | HIGH | 2024-01-31 | 16.1→14.7 | +1.4 | +0.6% | ₹5,759 |
| RBI | BE | 2024-02-07 | 15.5→15.4 | +0.1 | -0.7% | ₹3,886 |
| RBI | BE | 2024-04-04 | 11.2→11.6 | -0.4 | +0.7% | ₹4,371 |
| Election-2024-result | HIGH | 2024-06-03 | 20.9→18.9 | +2.1 | -2.8% | ₹-26,777 |
| RBI | BE | 2024-06-06 | 16.8→16.4 | +0.4 | +1.9% | ₹-8,896 |
| RBI | BE | 2024-08-07 | 16.2→15.3 | +0.8 | +0.3% | ₹12,078 |
| RBI | BE | 2024-10-08 | 14.6→13.5 | +1.1 | -0.1% | ₹14,988 |
| RBI | BE | 2024-12-05 | 14.5→14.1 | +0.4 | -0.4% | ₹15,716 |
| Budget-2025 | HIGH | 2025-01-31 | 16.2→14.3 | +1.9 | -0.6% | ₹9,600 |
| RBI | BE | 2025-02-06 | 14.2→14.4 | -0.3 | -0.9% | ₹4,260 |
| RBI | BE | 2025-04-08 | 20.4→20.1 | +0.3 | +1.3% | ₹2,964 |
| RBI | BE | 2025-06-05 | 15.1→14.7 | +0.4 | +1.4% | ₹-3,118 |

## Pre-registered O1 gate (fixed 2026-06-20 — not relaxed)
| Criterion | Result |
|---|---|
| G1 mean crush>1.0vp | ❌ FAIL |
| G2 win>=55% | ✅ PASS |
| G3 event net>0 & beats baseline | ❌ FAIL |

## VERDICT
**SHELVE — event timing does not add reliable edge over random-day short vol.**

---
*Proxy = VIX-implied ATM straddle (defined-risk fly in the real build). n is small and the calendar curated
(budgets/election HIGH confidence; RBI best-effort). A PASS ⇒ a defined-risk event-vol backtest on the real
options chain, with explicit event-surprise tail sizing — never auto-deploy. Live BLOCKED.*
