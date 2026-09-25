# H4 — Delivery-Spike Event-Drift Study (Daily NSE)

**Status:** COMPLETE — advisory. Live trading remains BLOCKED.
**Date:** 2026-06-15
**Operating doc:** `qe-phase-next-cio-operating-doc-2026-06-15.md` §7 (H4) · ADR-036.
**Hypothesis:** a delivery-% spike on elevated volume = an accumulation event with multi-day drift; a different return driver from the monthly delivery-level factor.

> **No-lookahead:** NSE delivery % is published post-close on day t → every event enters at **close[t+1]**; the spike baseline excludes day t. Costs = full NSE delivery stack (round-trip ≈ 0.322%). Abnormal return = vs an EW top-100 market index.

Signal: delivery z ≥ 2.0, volume ≥ 1.5× median, delivery ≥ 50.0%, top-200 liquid (funds excluded). Period 2020-01-01 → 2025-06-30. **2278 events.**

## 1. Event study — forward drift after a spike

| Horizon (days) | Mean return | Mean abnormal | t-stat (abnormal) |
|---|---:|---:|---:|
| T+1 | +0.11% | -0.05% | -0.94 |
| T+2 | +0.25% | -0.10% | -1.43 |
| T+3 | +0.36% | -0.16% | -2.11 |
| T+5 | +0.51% | -0.31% | -3.30 |
| T+10 | +0.91% | -0.45% | -3.47 |
| T+15 | +1.15% | -0.49% | -3.05 |
| T+20 | +1.87% | -0.51% | -2.82 |

## 2. Net per-trade economics (full delivery cost)

| Hold (days) | Trades | Mean net | Median net | Hit% | Mean abnormal net | t-stat |
|---|---:|---:|---:|---:|---:|---:|
| 5 | 2252 | +0.19% | +0.04% | 50% | -0.64% | -6.68 |
| 10 | 2221 | +0.59% | +0.45% | 54% | -0.78% | -5.93 |
| 20 | 2199 | +1.54% | +1.45% | 57% | -0.83% | -4.60 |

## 3. Calendar-time daily EW portfolio (hold 10d)

| Series | CAGR | Sharpe | MaxDD | Active days |
|---|---:|---:|---:|---:|
| delivery-spike portfolio | 17.4% | 0.95 | -27.5% | 1357 |
| market (EW top-100) | 22.7% | 1.09 | -40.9% | — |

## 4. Read & verdict

- **Drift:** abnormal returns are significantly **NEGATIVE** (t down to -3.47) — post-spike names **underperform** the market. The signal is anti-predictive (mildly contrarian), not merely absent.
- **Beta caveat:** the *nominal* forward returns are positive (these are bull-market names), but that is market beta — the decision-relevant figure is the **abnormal** return vs the market (the abnormal columns above), which is the opposite sign.
- **Net of cost:** **no hold combines positive mean net with a significant positive abnormal t-stat** — H4 does NOT clear costs on this evidence (nominal net is positive but it is pure beta; the abnormal-net t-stats are significantly negative).
- **Portfolio:** Sharpe 0.95 vs market 1.09 (BELOW the market).

**Verdict:** **REJECTED on this evidence.** Delivery-spike names do not out-drift the market net of cost; if anything the long signal is mildly contrarian (significantly negative abnormal returns). Record the negative result; do **not** carry H4 forward as a long event signal. Per policy we do NOT tune-to-fit — flipping it to a short/contrarian signal would be a DIFFERENT hypothesis needing its own economic rationale, not a parameter sweep.

> Backtesting can recommend. It cannot promote. A human approves all production changes.
> Live trading remains BLOCKED. Single bull regime in sample — no sustained-bear evidence.
