# Factor Diversification & Correlation Study (Daily NSE)

**Status:** COMPLETE — advisory. Live trading remains BLOCKED.
**Date:** 2026-06-15
**Operating doc:** `docs/strategy/qe-phase-next-cio-operating-doc-2026-06-15.md` §5 (ADR-036).
**Prior:** `factor-study-report.md`, `delivery-walkforward-report.md`.

Fills the §5 *INSUFFICIENT EVIDENCE* gap: are the candidate sleeves genuinely low-correlated to the delivery-% core, or beta-redundant? All series are monthly NET returns (full NSE delivery cost stack, round-trip ≈ 0.322%), top-200 liquid universe, long-only top-20, monthly rebalance.

> **`value` is a price-based proxy** (price vs trailing 252d mean = long-horizon reversion). A true value factor needs fundamentals (absent from the lake). Its standalone numbers are indicative only; it is included here mainly to measure its *diversification*.

Sleeves: delivery, momentum, lowvol, value · period 2020-01-01 → 2025-06-30 · 53 common months.

## 1. Standalone net metrics

| Sleeve | Net CAGR | Sharpe | MaxDD | Hit% | Months |
|---|---:|---:|---:|---:|---:|
| delivery | 21.9% | 1.40 | -21.7% | 68% | 53 |
| momentum | 22.1% | 0.93 | -34.7% | 66% | 53 |
| lowvol | 12.8% | 0.85 | -22.2% | 62% | 53 |
| value | 19.1% | 0.96 | -26.8% | 58% | 53 |
| _benchmark (EW, gross)_ | 17.4% | 0.99 | -25.8% | 66% | 53 |

## 2. Pearson correlation (monthly net returns)

| | delivery | momentum | lowvol | value |
|---|---|---|---|---|
| **delivery** | 1.00 | 0.63 | 0.84 | 0.75 |
| **momentum** | 0.63 | 1.00 | 0.54 | 0.73 |
| **lowvol** | 0.84 | 0.54 | 1.00 | 0.65 |
| **value** | 0.75 | 0.73 | 0.65 | 1.00 |

## 3. Spearman (rank) correlation

| | delivery | momentum | lowvol | value |
|---|---|---|---|---|
| **delivery** | 1.00 | 0.56 | 0.77 | 0.73 |
| **momentum** | 0.56 | 1.00 | 0.49 | 0.72 |
| **lowvol** | 0.77 | 0.49 | 1.00 | 0.61 |
| **value** | 0.73 | 0.72 | 0.61 | 1.00 |

## 4. Rolling 12-month pairwise correlation

| Pair | Full-sample | Roll mean | Roll min | Roll max |
|---|---:|---:|---:|---:|
| delivery/momentum | 0.63 | 0.53 | 0.13 | 0.79 |
| delivery/lowvol | 0.84 | 0.83 | 0.55 | 0.97 |
| delivery/value | 0.75 | 0.73 | 0.48 | 0.92 |
| momentum/lowvol | 0.54 | 0.54 | 0.02 | 0.77 |
| momentum/value | 0.73 | 0.70 | 0.16 | 0.89 |
| lowvol/value | 0.65 | 0.70 | 0.14 | 0.86 |

## 5. Down-month correlation (benchmark worst decile, n=6 months)

| Pair | Down-month corr |
|---|---:|
| delivery/momentum | 0.11 |
| delivery/lowvol | 0.99 |
| delivery/value | 0.54 |
| momentum/lowvol | 0.01 |
| momentum/value | 0.00 |
| lowvol/value | 0.50 |

## 6. Drawdown-overlap matrix (% of months both in drawdown)

| | delivery | momentum | lowvol | value |
|---|---|---|---|---|
| **delivery** | 51% | 43% | 49% | 47% |
| **momentum** | 43% | 60% | 51% | 55% |
| **lowvol** | 49% | 51% | 64% | 58% |
| **value** | 47% | 55% | 58% | 66% |

## 7. Diversification benefit

| Construction | Net CAGR | Sharpe | MaxDD |
|---|---:|---:|---:|
| best single sleeve (delivery) | 21.9% | 1.40 | -21.7% |
| equal-weight blend | 19.5% | 1.16 | -23.4% |
| inverse-vol blend (trailing 12m) | 19.5% | 1.19 | -23.5% |

- Best single sleeve (delivery) Sharpe **1.40**; best blend Sharpe **1.19**; average standalone sleeve Sharpe **1.03**.
- **Decision-relevant comparison:** the blend Sharpe (1.19) is BELOW the best single sleeve (delivery 1.40). Beating the *average* sleeve is the textbook diversification test, but the average is dragged down by the weak sleeves — the honest test is whether blending beats the *best* sleeve.
- Diversification ratio (equal-weight): **1.14** (target > 1.20; >1 means the blend's risk is below the weighted-average sleeve risk — i.e. real diversification).

## 8. Read & §5 veto evaluation

- **Co-allocation veto (corr > 0.70):** **delivery/lowvol, delivery/value, momentum/value** — do NOT count as independent sleeves.
- **Stress veto (down-month corr > 0.85):** **delivery/lowvol** — these converge when it matters.
- **Stress behaviour:** in the worst-decile months **momentum/value** decouples (corr 0.00) — a genuine stress diversifier — while **delivery/lowvol** stays redundant (corr 0.99). Note this can invert the naive read: a sleeve that looks defensive on average may be most redundant exactly in drawdowns.
- **Diversification benefit:** best blend Sharpe 1.19 DILUTES the best single sleeve (delivery 1.40); diversification ratio 1.14 is BELOW the 1.20 target. Within long-only NSE equity factors in this single bull regime, diversification is largely **illusory** — the sleeves share equity beta.

## 9. Verdict & next gate

This study computes the diversification inputs the QE Promotion Score left as *INSUFFICIENT EVIDENCE*. It does **not** promote anything. **Headline finding: blending the candidate long-only equity factors does NOT improve on delivery-% standalone** — it dilutes it (best blend Sharpe < delivery 1.40), and the pairwise correlations are too high (several above the 0.70 veto) to treat them as independent sleeves. The strategic consequence: do **not** rush a combined factor book; **delivery-% standalone remains the lead candidate**, and genuine diversification must come from *different return drivers* (event / structural-flow / macro — the data-gated hypotheses H4–H8) and from *different regimes* (the regime-expansion project), not from more long-only equity factors.

**Caveat:** these correlations are themselves regime-limited — in a single bull, long-only factors co-move; a real bear could change the picture, but the lake cannot test it (no sustained bear). That *strengthens* the case for the regime-expansion project (operating doc §4) before any combined-book decision.

Risk control remains position sizing / portfolio drawdown limit — not a market-timing overlay (it hurt; ADR-034).

> Backtesting can recommend. It cannot promote. A human approves all production changes.
> Live trading remains BLOCKED.
