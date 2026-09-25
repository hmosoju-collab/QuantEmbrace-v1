# QuantEmbrace — Strategy Research Program Consolidation

**Date:** 2026-06-20 · **Status:** capstone summary · **Live trading: BLOCKED throughout**
Advisory research record. Backtesting can recommend; it cannot promote. A human approves all production
changes. No capital was deployed into any strategy in this program.

> **One-line conclusion:** Across every avenue tested — intraday, gap, calendar, overnight, positional
> factors, factor combinations, options volatility, event volatility, and futures trend — **no edge
> survived proper, costed, out-of-sample scrutiny.** At the horizons and instruments a ₹5L retail account
> can reach, NSE large-cap is efficient. The deliverables are a rigorous elimination, a reusable toolkit,
> and one disciplined forward experiment — with zero capital lost to mirages.

---

## 1. Mandate (the lens everything was judged against)
Personal NSE account ≈ **₹5L**. **Capital protection > trade count > profit.** Transaction costs are large
relative to capital; leverage is ruin risk, not just drawdown. Every evaluation used the full NSE statutory
cost stack, reported trade counts + confidence, and treated ~1–3 yr of data as hypothesis-screening, not
proof. The standing rule throughout: **would rather shelve a strategy than overfit it into looking good.**

## 2. What was tested, and why each died

### Intraday cash equity — RETIRED / DEAD
- ORB, VWAP-reversion, intraday-trend-15m, pre-close-momentum: all **PF < 1 net of intraday costs**
  (`strategy-retirement-register-2026-06-19.md`).
- **C6 overnight decomposition** gave the structural reason: NSE large-cap **intraday return is negative-
  drift** (−5.9 bps/day, Sharpe −1.1, cum −79% 2016–26); the **entire equity premium accrues overnight**
  (+13.1 bps/day, Sharpe 3.2). Intraday isn't merely cost-walled — it's a negative-sum desert.

### Gap reaction (C1) — SHELVED
Large-gap continuation real but thin and year-inconsistent (n=259; 2022 +29 bps, 2023 −5 bps, 2024 +43 bps).

### Calendar / turn-of-month (C7) — SHELVED
Mild real concentration (in-window ~2.5× rest) but a long-in-window/flat timing strategy loses to buy-hold
risk-adjusted (sitting in cash 82% of days sacrifices more than the concentration is worth).

### Positional cross-sectional factors — FORWARD-TESTING (deploy nothing) / mostly REJECTED
- **Delivery-% conviction factor:** the one regime-stable backtest edge (2019–25 Sharpe 1.40), but **decayed
  across periods (1.60→0.61)** and **weak forward** (−9.85%, −9.7 pts behind market over 5 months) — reads
  as defensive beta, not alpha-yet.
- **Momentum:** pre-2019 bear-robust (Sharpe 0.63 > market 0.59) = 2nd verified factor, but **mostly beta**.
- **Delivery + momentum combo:** "diversification" **REFUTED** — legs +0.67 correlated; 50/50 combo gives
  no diversification (MaxDD −28.9% worse than delivery alone). Forward "decoupling" was a 1-regime artifact.
- **LowVol:** risk reducer, not alpha. **Reversal:** killed by costs.
- **H4 delivery-spike** and **H5 PEAD (proxy):** both **REJECTED** — abnormal returns significantly
  *negative* at every horizon (NSE large-cap event stocks underperform post-trigger). The delivery signal
  lives in the persistent monthly *level*, not in spikes/events.

### Options volatility — DEAD (static)
- **O-1 VRP screen:** PASS — ATM VRP is real (+2.5 vp, VIX>realised 80% of days, positive every year),
  cross-confirmed on free (Yahoo) and broker-grade (Kite) data. But with a fat crash tail.
- **O-2 defined-risk iron condor (real NIFTY option chains, 31 monthly cycles):** **FAIL** — loses
  **gross even at zero slippage** (PF 0.67); credit/max-loss 0.25 needs ~81% win, achieves 65%.
- **O-2 structure sweep (10 configs):** **0/10 clear the gate; only 2/10 gross-positive, trivially.**
  Structural: ATM VRP does not convert to a profitable OTM condor — at the wings, net of put skew +
  directional risk, the premium isn't there.

### Options/Futures hedge-level program — ALL TIER-1 ELIMINATED
- **F1 overnight index-futures:** spot-proxy screen PASSed (+11.3 bps, Sharpe 1.98), but on **real near-
  month futures FAILED** (+3.2 bps, Sharpe 0.34, 2025 negative). Decomposition: ~⅔ of the index overnight
  move is **non-harvestable** on real futures (basis decay + the non-tradable index "open"). *The spot
  proxy overstated the harvestable edge ~3.5×.* Hedged variant also dead (binding failure is return, not
  the tail — a protective put adds cost).
- **O1 scheduled-event IV-crush:** **SHELVED** — event-timed short straddles earn 1.03× the random-day
  baseline (i.e., nothing). Where the crush is real (budgets/election) the realised move is also real and
  short vol loses; where short vol wins (RBI) there's no crush. Pre-event IV is fairly-priced.
- **F2 index-futures trend:** **SHELVED** — drawdown reduction, **not alpha**: long-only trend adds no
  return vs buy-hold (~12% both), modestly better Sharpe (0.54–0.59 vs 0.42) purely by cutting the −72%
  leveraged buy-hold DD to ~−25%, and the result hinges on dodging a single crash (2020, n≈1 trend).

## 3. Why it all died (the recurring mechanisms)
1. **Fair risk compensation, not excess** — VRP at the wings, pre-event IV, gap premiums: the extra return
   is paid for a risk that materialises (skew, the event move, the gap).
2. **Non-tradable measurement artifacts** — the index "open" and close-to-close proxies overstate what a
   real instrument captures (F1: 3.5×).
3. **Costs decisive at retail size** — intraday cash (0.20% round-trip), condors (flat ₹20/leg × 8),
   overnight cash (0.22% delivery).
4. **Beta dressed as alpha** — factors and trend largely deliver index exposure ± whipsaw, not edge.
5. **Single-event/regime dependence** — F2 ≈ one crash; forward factor reads ≈ one regime; small n.

## 4. The methodology that held (process wins, reusable)
- **Pre-registered gates**, fixed before seeing data, never relaxed to force a pass.
- **Baseline controls** — random-day short vol (O1), leg correlation (combo), buy-hold (F2) — repeatedly
  exposed pseudo-edges that raw win-rates hid.
- **Validate on the real tradable instrument**, never a spot/index proxy (the F1 lesson, learned the hard way).
- **Gross-vs-cost decomposition** — separates an edge problem from a friction problem (O-2, F1).
- **Full cost + tail + crash stress**, and honest sample-size caveats on every result.
- **Two harness bugs caught before trusting verdicts** (VRP date-parse; condor cycle-selection) — discipline
  to distrust surprising results until the machinery is proven.

## 5. Reusable toolkit (retained)
- **Data lakes/loaders:** `download_bhavcopy.py` (equity EOD 2016–26), `fetch_zerodha_intraday.py`,
  `fetch_zerodha_indices.py` (NIFTY50+VIX), `kite_fetch_with_token.py` (no-DynamoDB Kite fetch),
  `download_fo_bhavcopy.py` (NIFTY option chains EOD), `download_fo_futures.py` (NIFTY/BANKNIFTY futures EOD).
- **Cost models:** `IndianCostModel` (equity intraday/delivery), `OptionsCostModel`, `FuturesCostModel`.
- **Studies/backtests:** factor study + walk-forward + correlations, combined-book, calendar/overnight (C6/C7),
  gap (C1), delivery-spike (H4), PEAD (H5), `run_vol_premium_study` (O-1), `run_options_vol_backtest` +
  `run_options_vol_sweep` (O-2), `run_overnight_futures_study` (F1, spot + `--futures`), `run_event_vol_study`
  (O1), `run_futures_trend_study` (F2). All self-tested.
- **Forward/paper:** `run_delivery_paper_book.py`, `replay_delivery_book_forward.py`, `check_forward_gate.py`.

## 6. The one live experiment (standing posture)
Two isolated, advisory **forward factor books** (delivery, momentum) accrue monthly against the
**pre-registered Forward Factor Gate** (`docs/live-readiness/forward-factor-validation-gate.md`): ≥12
forward months · cumulative alpha > 0 vs the EW liquid benchmark · IR ≥ 0.50 · ≥58% positive-alpha months
& no single month > 50% of cum alpha · maxDD ≤ benchmark. Seeded 2025-12-31 → **eligible ~Dec-2026**.
Monthly cadence: refresh Bhavcopy → `replay_delivery_book_forward.py --factor {delivery,momentum}` →
`check_forward_gate.py`. **Clearing the gate unlocks human review of a small gated pilot — never auto-deploy.**

## 7. Go-forward stance
- **Deploy nothing.** Let the forward books be the out-of-sample truth-test. Live trading remains BLOCKED.
- **Do not re-chase eliminated edges** (this document is the record of what's settled and why).
- **Do not relax** the forward gate to make a book pass.
- **Remaining levers are data-acquisition decisions**, each with an honest prior:
  - Intraday option chains (paid: Algotest/GDFL/TrueData) — only justified by a non-static thesis; the
    static space is dead.
  - Pre-2019 delivery-% history — to test the delivery factor in a sustained bear (currently untestable).
  - These are open *options*, not recommendations; the burden of proof rests on a new, specific thesis.

---
*Capstone of the 2026 strategy research program. All findings cross-referenced in `memory/decisions.md`
(ADR-033…F2) and the per-study reports under `docs/backtesting/`. Capital protection > trade count >
profit. Live trading BLOCKED.*
