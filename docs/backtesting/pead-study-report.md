# H5 — Post-Earnings Announcement Drift (PEAD) Study (Daily NSE)

**Status:** COMPLETE — advisory. Live trading remains BLOCKED.
**Date:** 2026-06-15
**Relates:** ADR-036, `qe-phase-next-cio-operating-doc-2026-06-15.md` §7 (H5).
**Mode:** H5b (price-implied proxy).

> **No-lookahead:** announcements are after-hours. Every event enters at **close[t+1]**. Costs = full NSE delivery stack (round-trip ≈ 0.322%). Abnormal return = vs EW top-100 market index.

Price-implied proxy: |return(t)| > 3.0σ, volume > 2.0× median, top-200 liquid (funds excluded).
Period 2020-01-01 → 2025-12-31. **1907 positive events, 1013 negative events.**

---

## 1. Positive-surprise / long-side PEAD

### 1a. Forward drift (raw + abnormal)

| Horizon (days) | Mean return | Mean abnormal | t-stat (abn) |
|---|---:|---:|---:|
| T+1 | +0.14% | -0.08% | -1.30 |
| T+2 | +0.31% | -0.06% | -0.67 |
| T+3 | +0.51% | +0.04% | +0.43 |
| T+5 | +0.77% | +0.12% | +0.90 |
| T+10 | +1.08% | -0.27% | -1.43 |
| T+15 | +1.32% | -0.45% | -2.01 |
| T+21 | +2.03% | -0.67% | -2.49 |
| T+42 | +3.81% | -1.20% | -3.02 |
| T+63 | +5.65% | -1.92% | -3.98 |

### 1b. Net per-trade economics (full delivery cost stack)

| Hold (days) | Trades | Mean net | Median net | Hit% | Mean abn-net | t-stat |
|---|---:|---:|---:|---:|---:|---:|
| 5 | 1900 | +0.45% | +0.03% | 50% | -0.20% | -1.55 |
| 10 | 1886 | +0.76% | +0.50% | 54% | -0.59% | -3.13 |
| 21 | 1870 | +1.71% | +1.18% | 55% | -1.00% | -3.68 |
| 42 | 1843 | +3.49% | +2.22% | 57% | -1.52% | -3.83 |

### 1c. Calendar-time daily EW portfolio (hold 21d)

| Series | CAGR | Sharpe | MaxDD | Active days |
|---|---:|---:|---:|---:|
| PEAD long portfolio | 13.3% | 0.74 | -46.8% | 1476 |
| market (EW top-100) | 21.5% | 1.07 | -40.5% | — |

---

## 2. Negative-surprise side (informational only — no short in CNC universe)

1013 negative-surprise events detected. CNC delivery does not support short selling; these results are reported to understand the full signal structure but are NOT proposed as a trading strategy.

| Horizon (days) | Mean return | Mean abnormal | t-stat (abn) |
|---|---:|---:|---:|
| T+1 | +0.37% | +0.01% | +0.17 |
| T+2 | +0.45% | -0.17% | -1.50 |
| T+3 | +0.59% | -0.23% | -1.72 |
| T+5 | +0.84% | -0.19% | -1.03 |
| T+10 | +1.16% | -0.59% | -2.31 |
| T+15 | +1.46% | -0.58% | -1.90 |
| T+21 | +1.68% | -0.92% | -2.57 |
| T+42 | +3.60% | -1.33% | -2.73 |
| T+63 | +5.08% | -1.74% | -3.01 |

---

## 3. Verdict

**REJECTED — anti-predictive.** Abnormal returns significantly NEGATIVE (t down to -3.98). Post-event names underperform. Record negative result; do not carry forward as a long signal (flipping to short is a separate hypothesis).

**Caveats:**
- Single bull-market regime (Oct 2019 – present for the main lake; pre-2019 data extends once 2016–2018 download completes — Tier-1 regime-expansion).
- No analyst consensus data: surprise direction is price-implied, not fundamental.
- Correlation with delivery-% monthly factor not yet tested; if both carry forward, a correlation study determines whether they truly diversify.

> Backtesting can recommend. It cannot promote. A human approves all production changes.
> Live trading remains BLOCKED.
