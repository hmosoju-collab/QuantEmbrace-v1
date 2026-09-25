# Phase 2 C1 — Overnight-Gap Reaction Study

**Status:** COMPLETE — advisory. Live trading remains BLOCKED.
**Date:** 2026-06-19   **Universe:** NIFTY50 (Zerodha Kite 5m)   **Period:** 2022-01-01 → 2024-12-31
**Panel:** 33,212 symbol-days   **Entry:** 09:20 (first 5m close, executable)   **Exit:** EOD (MIS).
**Costs:** MIS statutory 0.0352% round-trip + slippage (5 & 10 bps/leg).
**Params:** bucket bounds 0.5σ / 2σ and fade/continue directions are **a-priori, not fitted**.

## Stage 1 — event study (parameter-free): entry→EOD return by gap bucket

| Bucket | n | mean gap % | r+30m % | r+60m % | rEOD % | t(EOD) | EOD>0 % |
|---|---:|---:|---:|---:|---:|---:|---:|
| large_down | 127 | -3.65 | -0.076 | -0.209 | -0.446 | -2.12 | 46.5 |
| mid_down | 2595 | -1.23 | -0.005 | 0.022 | 0.01 | 0.31 | 51.3 |
| flat | 26126 | 0.11 | -0.016 | -0.029 | -0.02 | -2.36 | 47.4 |
| mid_up | 4232 | 1.06 | -0.016 | -0.019 | -0.073 | -3.21 | 44.8 |
| large_up | 132 | 3.77 | 0.061 | 0.227 | 0.318 | 2.12 | 51.5 |

Reading: for *up* gaps a negative rEOD = fade; positive = continuation (and vice-versa for
down gaps). Compare |rEOD| against the round-trip cost (~0.14–0.24%) — the effect must beat cost to be tradable.

## Stage 2 — a-priori rule (mid=fade, large=continue, EOD exit), net of cost

### Slippage 5bps/leg — overall: **REJECT — no edge after costs**

| Cut | Trades | Win% | PF | Exp (bps) | Net ₹ | Sharpe(d) |
|---|---:|---:|---:|---:|---:|---:|
| **overall** | 7086 | 48.5 | 0.872 | -7.4 | -261,825 | -1.15 |
| mode=fade | 6827 | 48.5 | 0.851 | -8.6 | -293,625 | -1.25 |
| mode=continue | 259 | 48.6 | 1.411 | 24.6 | 31,800 | 1.73 |
| year=2022 | 2616 | 50.0 | 0.928 | -4.4 | -57,871 | -0.62 |
| year=2023 | 2276 | 46.0 | 0.791 | -10.7 | -121,486 | -2.44 |
| year=2024 | 2194 | 49.5 | 0.877 | -7.5 | -82,467 | -1.06 |
| regime=UPTREND | 4561 | 47.9 | 0.822 | -9.8 | -222,700 | -2.1 |
| regime=DOWNTREND | 2478 | 49.7 | 0.951 | -3.2 | -39,046 | -0.36 |

### Slippage 10bps/leg — overall: **REJECT — no edge after costs**

| Cut | Trades | Win% | PF | Exp (bps) | Net ₹ | Sharpe(d) |
|---|---:|---:|---:|---:|---:|---:|
| **overall** | 7086 | 44.9 | 0.725 | -17.4 | -616,125 | -2.66 |
| mode=fade | 6827 | 44.9 | 0.705 | -18.6 | -634,975 | -2.67 |
| mode=continue | 259 | 46.3 | 1.224 | 14.6 | 18,850 | 1.06 |
| year=2022 | 2616 | 46.9 | 0.783 | -14.4 | -188,671 | -2.01 |
| year=2023 | 2276 | 41.9 | 0.635 | -20.7 | -235,286 | -4.67 |
| year=2024 | 2194 | 45.8 | 0.735 | -17.5 | -192,167 | -2.44 |
| regime=UPTREND | 4561 | 44.0 | 0.671 | -19.8 | -450,750 | -4.17 |
| regime=DOWNTREND | 2478 | 46.8 | 0.811 | -13.2 | -162,946 | -1.5 |

## Read (honest)

- Gate to 'carry forward': exp > 0 **and** PF > 1.2 **and** net > 0 at the **conservative
  10 bps/leg** slippage (the open is the widest-spread time), holding across years + regimes.
- This is one month-equivalent... no — it is 3 years; but per-trade edge on event days is thin,
  so judge PF and the cost-sensitivity (does 5→10 bps flip the sign?), not the headline ₹.

## Verdict — C1 (2026-06-19): real-but-thin signal; DO NOT carry to paper

**Hypothesis half-confirmed at the signal level.** Event study (parameter-free): **large gaps
CONTINUE** (large_up rEOD +0.32%, t=+2.12; large_down −0.45%, t=−2.12) and **mid up-gaps FADE**
(−0.073%, t=−3.21, n=4,232 — well-sampled, significant). The microstructure effect is real.

**But it does not clear the bar to trade:**
- The **fade** (mid gaps) is only ~7 bps — *below* the ~13.5–23.5 bps round-trip cost wall. A real
  inefficiency that is simply **sub-cost** (the cleanest illustration that retail intraday is blocked
  by costs, not absence of signal). The blended a-priori rule REJECTS because fade trades dominate
  by count (6,827 of 7,086).
- The **continuation** sub-signal clears cost *in aggregate* (PF 1.41 @5bps / **1.22 @10bps**, exp
  +24.6/+14.6 bps, Sharpe 1.73/1.06) **but fails robustness**:
  - **Tiny sample:** 259 trades in 3 years (~86/yr; ~7/month — *1 month could never screen it*).
  - **Not year-consistent:** 2022 +29 bps (PF 1.48) · **2023 −5 bps (PF 0.91 — loses)** · 2024
    +43 bps (PF 1.70). The aggregate is **carried by 2024**; ex-2024 ≈ break-even-to-negative.
  - **Marginal at conservative slippage** (PF 1.22 at 10 bps — and the open is the widest-spread
    time, so 10 bps is the honest case).
  - The stronger leg is the **short side** (large down-gaps) → single-stock intraday shorting, extra
    friction/borrow risk for a small account.

**Decision: DO NOT build C1 into a paper strategy. Shelve as a monitored hypothesis.** It is the best
intraday result obtained (the only sub-signal to clear costs at all), but building on a 259-trade,
year-inconsistent, 2024-carried effect would be fitting to one good year. **Salvageable:** (1) the
large-gap continuation effect is real, worth re-testing with materially more data / more symbols;
(2) the well-measured-but-sub-cost mid-gap fade is the definitive evidence that the **cost wall — not
lack of signal** — is what makes retail intraday equity unprofitable on liquid names.

> Backtesting can recommend. It cannot promote. A human approves all production changes.
