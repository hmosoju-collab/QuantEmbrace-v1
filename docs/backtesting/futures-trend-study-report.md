# F2 — Index-Futures Trend/Momentum Screen

**Pre-registered:** 2026-06-20 · **Signal:** NIFTY50 spot close; **P&L:** 1 NIFTY futures lot (cost stack
+ carry drag) · **Live trading: BLOCKED** · Advisory. Window 2020-01-01 → 2025-12-31, 1492 days.

## Benchmark — buy & hold 1 futures lot (the bar trend must beat)
- ann **+12.6%** · Sharpe **0.42** · maxDD **-71.8%**

## Pre-declared grid (all cells; ✅ = beats buy-hold Sharpe & maxDD≤25% & ann>0)
| mode | lookback | ann | Sharpe | maxDD | flips | time-in | beats B&H |
|---|---:|---:|---:|---:|---:|---:|---|
| long-only | 20 | +12.0% | 0.57 | -26.5% | 144 | 62% | — |
| long-only | 50 | +12.8% | 0.59 | -27.0% | 85 | 65% | — |
| long-only | 100 | +12.3% | 0.54 | -24.6% | 59 | 69% | ✅ |
| long-only | 200 | +6.4% | 0.23 | -41.1% | 31 | 74% | — |
| long-short | 20 | +7.8% | 0.23 | -42.5% | 145 | 99% | — |
| long-short | 50 | +6.9% | 0.20 | -51.4% | 86 | 97% | — |
| long-short | 100 | +4.3% | 0.12 | -41.1% | 60 | 93% | — |
| long-short | 200 | -8.1% | -0.18 | -76.8% | 31 | 87% | — |

## VERDICT
**ISOLATED PASS (lb 100/long-only) — likely lookback luck on few trends; needs a neighbour + OOS before any belief.**

---
*Single-index trend usually = leveraged beta minus whipsaw cost. Few independent trends in ~6 yr ⇒ low
confidence; a pass needs an island + OOS + a real-futures cross-check before any forward book. Never
auto-deploy. Live BLOCKED.*
