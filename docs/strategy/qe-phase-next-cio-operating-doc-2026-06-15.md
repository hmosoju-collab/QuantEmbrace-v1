# QE Phase-Next — CIO Operating Document

**Date:** 2026-06-15 · **Chair:** CIO / Head of Quant Research / PM
**Status:** Advisory. LIVE TRADING BLOCKED. No capital authorized. Human approval mandatory.
**Decision record:** ADR-036 (`memory/decisions.md`).
**Relates:** `docs/strategy/strategy-thesis-redirection-2026-06-15.md` (ADR-034),
`docs/strategy/retail-quant-investment-committee-report-2026-06-10.md`,
`docs/backtesting/factor-study-report.md`, `docs/backtesting/delivery-walkforward-report.md`,
`docs/backtesting/factor-correlation-report.md`, [[strategy_thesis_pivot_2026_06_15]].

**Assumptions (explicit):** (a) the daily lake covers ~Oct 2019 → 2025-12-31, one broad bull with
V-shaped dips, **no sustained bear**; (b) returns are net of the NSE delivery cost stack
(round-trip ≈ 0.322%); (c) the delivery paper book is an isolated harness (no broker / MIS /
DynamoDB); (d) a pairwise sleeve correlation matrix **was computed on 2026-06-15** — see the
post-publication evidence update below.

> **Mission:** not to maximize CAGR, but to maximize the probability that QE survives long enough
> to discover persistent alpha. Optimize for survivability, robustness, diversification, evidence.

---

## Post-publication evidence update (2026-06-15) — correlation study completed

§5's correlation project was executed the same day (`docs/backtesting/factor-correlation-report.md`,
`scripts/backtest/run_factor_correlations.py`). **Headline finding, which updates §3/§5/§7 below:**

- The candidate long-only equity sleeves are **highly correlated**, not independent: Pearson
  delivery/lowvol **0.84**, delivery/value 0.75, momentum/value 0.73 — three pairs above the 0.70
  co-allocation veto.
- **Blending DILUTES rather than diversifies.** Best blend Sharpe **1.19** < delivery standalone
  **1.40**; diversification ratio 1.14 (below the 1.20 target). Beating the *average* sleeve is a
  textbook illusion here — the average is dragged down by weak sleeves.
- **Stress inverts the naive read:** in the worst-decile months delivery/lowvol correlation rises
  to **0.99** (lowvol is *most* redundant exactly in drawdowns), while momentum/value decouples to
  ~0.00. The one "defensive" sleeve fails when needed.

**Consequence:** the a-priori plan to build a combined factor book is **demoted**. Delivery-%
standalone remains the lead candidate. Genuine diversification must come from **different return
drivers** (event / structural-flow / macro — H4–H8) and **different regimes** (the regime project),
not from more long-only equity factors. Caveat: the correlations are themselves regime-limited (one
bull) — which *strengthens* the regime-expansion case before any combined-book decision.

---

## 1. Executive Summary

QE's survival problem is not finding return — it is **not deploying false alpha.** Posture:

- **Delivery-% → PRE-PRODUCTION (entry).** Cleared the backtest + walk-forward bar (Sharpe 1.40,
  +5/5 OOS years) but has **zero forward-paper and zero bear-regime evidence** — far from PRODUCTION.
  It enters a 12-month forward program (§6).
- The next 90 days buy **evidence quality, not return**: permanent controls (harness-integrity
  assertions + backtest-before-paper gate), start the delivery forward track, **resolve the
  correlation question (done — see update above)**, and launch the **regime-expansion** project —
  the single largest existential unknown for the one validated edge.
- A formal **QE Promotion Score** (§3) makes promotion mechanical and evidence-weighted, with hard
  veto invariants (edge-below-cost = automatic KILL) so the intraday dead-end cannot recur.

## 2. Delivery Status Review

**Classification: PRE-PRODUCTION (entry).** Not RESEARCH (edge established), not PRODUCTION
(forward + regime evidence absent).

| Dimension | Status |
|---|---|
| Existing evidence | Net 21.9% CAGR, Sharpe **1.40** > benchmark 0.99 in both sub-periods; MaxDD −21.7%; **+5/5 OOS years**; non-parametric; ETF artifact removed, edge preserved |
| Missing evidence | Forward-paper track (zero); **sustained-bear regime (untested)**; live cost realism vs modeled 0.322%; capacity at size |
| Operational readiness | **Low** — no CNC order path, no rebalance automation, no live corporate-action/dividend handling validated |
| Forward validation | 12-month forward paper track (§6), tracking the backtest within tolerance |
| Regime coverage gap | **Critical** — no 2008/2011/2018-style bear in sample; the 200d "protective" overlay demonstrably HURTS on the dips we do have |

Promotion to PRODUCTION on single-regime evidence is exactly the failure mode QE exists to avoid.
**INSUFFICIENT EVIDENCE — FURTHER VALIDATION REQUIRED** for PRODUCTION.

## 3. QE Promotion Score

**Hard veto invariants (any failure → automatic KILL, score void):** net-negative after realistic
costs · target below product-specific cost floor · unresolved harness artifact / physical
implausibility · invalid instrument universe.

| Dimension | Weight | Rubric (0 → full) |
|---|---:|---|
| OOS persistence | 25 | walk-forward years positive, across regimes |
| Benchmark-relative alpha | 20 | net Sharpe & return vs EW benchmark, both sub-periods |
| Operational stability | 15 | forward-paper + runtime track, no trust-destroying incidents |
| Diversification contribution | 15 | low correlation / drawdown-overlap vs existing core |
| Drawdown profile | 15 | depth, recovery, regime breadth of the DD test |
| Simplicity / explainability | 10 | parameter count; economic mechanism clarity |

**Thresholds** (the production bar is deliberately high — the cost of false alpha is capital loss +
trust destruction; live is blocked, so there is **no urgency premium**):

| Band | Score | Meaning |
|---|---|---|
| **PRODUCTION** | ≥ 80 + all hard gates + forward-paper pass + regime evidence (or explicit operator bear-risk acceptance) | Eligible for staged capital |
| **PRE-PRODUCTION** | 65–79 | Forward-validate + harden ops |
| **WATCH** | 45–64 | Keep cheaply alive; gather evidence |
| **KILL** | < 45 **or any hard veto** | Stop research spend |

**Applied (diversification now evidence-based, not placeholder — and it is LOW for the equity
factors, per the correlation study):**

| Strategy | Total | Band |
|---|--:|---|
| Delivery-% | ≈65 | PRE-PRODUCTION |
| Momentum | ≈46 | WATCH |
| LowVol | ≈50 | WATCH (diversification contribution now scored DOWN — redundant in stress) |
| Combo | ≈27 | KILL (standalone) |
| Intraday (×5) | veto | KILL |

## 4. Regime Expansion Project — TIER-1 (highest strategic importance)

Acquire + validate pre-2020 NSE daily history so the one validated edge is stress-tested against
real bears. Runs in parallel with the forward program (does not block it).

| Regime | Test | Expected failure mode | Invalidating result |
|---|---|---|---|
| 2008 GFC crash | delivery DD depth/recovery in a −60% market | beta dominates; defensiveness insufficient | DD ≫ −22% or no recovery → not bear-robust |
| 2009 recovery | factor re-engages post-crash | late re-entry / whipsaw | persistent post-bear underperformance |
| 2011 Euro crisis | slow grinding bear (not V) | factor decay in chop | negative across a sustained down year |
| 2013 taper | rate/currency shock | lowvol rate-sensitivity | lowvol amplifies rather than dampens |
| 2015 sideways | low-dispersion, no trend | cross-sectional factors starve | flat/negative for a full year |
| 2018 correction | mid/small-cap-led decline | universe shift; delivery loads losers | delivery concentrates in losers |
| 2020 / 2022 | (in sample) cross-check normalization | data-join errors vs current lake | mismatch → data-quality flag |

Any invalidating result reclassifies delivery downward.

## 5. Correlation Project — COMPLETE (was INSUFFICIENT EVIDENCE)

Methodology (apples-to-apples, monthly net series per sleeve): Pearson + Spearman pairwise; rolling
12-month; **down-month** (worst-decile) conditional correlation; drawdown-overlap; diversification
ratio + EW/inverse-vol blends. Limits: co-allocation veto at corr > 0.70; stress veto at down-month
corr > 0.85; diversification-ratio target > 1.20. **Result: see the post-publication update above
and `factor-correlation-report.md`.** Net: the four equity factors fail the diversification test
among themselves; delivery standalone dominates the blend. Value (H3) was built as a price proxy
for this study (a true value factor needs fundamentals — flagged).

## 6. Forward Paper Program (Delivery-%) — **12 months**

A monthly-rebalanced strategy makes ~1 decision/month; 60 days (~2 rebalances) is meaningless, 6
months is thin/single-regime, **12 months** = 12 rebalances, captures seasonality, aligns with the
annual OOS unit, and gives a real chance of an in-period drawdown. (12 months still does not cover a
sustained bear — that is the regime project's job.)

| Element | Specification |
|---|---|
| Observation period | 12 months (forward, live data, real timestamps) |
| Review frequency | Monthly at each rebalance + quarterly committee review |
| Metrics | Realized vs modeled net (tracking error); realized slippage vs 5bps; turnover/cost realism; basket name-turnover; benchmark-relative; drawdown vs envelope; operational incidents |
| Failure conditions | Realized net materially below modeled; slippage ≫ modeled; any trust-destroying incident; drawdown beyond envelope unexplained by market |
| Promotion requirements | 12-mo tracking within tolerance **AND** regime evidence (or explicit operator bear-risk acceptance) **AND** QE Score ≥ 80 **AND** operational readiness validated |

## 7. Research Prioritization (updated by the correlation result)

The correlation study **demotes the equity-factor diversifiers** (H1/H2/H3 add little to delivery)
and **promotes the genuinely-different drivers** (H4–H8). Revised priority:

| Rank | Hypothesis | Persistence | Complexity | Data | Diversification (now measured/expected) | Verdict |
|---|---|---|---|---|---|---|
| 1 | **H5 PEAD** | High | High | ⚠ earnings | High (different driver) | **Top data-acquisition priority** |
| 2 | **H4 Delivery Spike** | Med | Low–med | ✅ lake | Med (event vs monthly level) | **TESTED → REJECTED** (abnormal returns significantly negative; ADR-036 addendum) |
| 3 | **H6 Index Reconstitution** | Med | Med | ⚠ membership cal. | Very high (flow) | Scope data |
| 4 | H7 F&O Open Interest | Med | Med | ⚠ OI history | Med | Scope data |
| 5 | H1 Delivery Acceleration | Med | Low | ✅ lake | Low (core-correlated) | Build cheaply; low marginal value |
| 6 | H2 LowVol | High | Low | ✅ lake | **Low (redundant in stress — measured)** | De-prioritised as a *return* sleeve |
| 7 | H3 Value (proxy) | Med | Low | ✅ proxy | **Low (0.75 to delivery — measured)** | Built; fundamentals version later |
| 8 | H8 Earnings Quality | Med | Med | ⚠ fundamentals | Med | Scope data |

**Build immediately on existing data: H4 (Delivery Spike).** **Top data priority: H5 (PEAD)** — the
strongest *different-driver* diversifier. The equity-factor diversifier thesis is empirically weak.

## 8. 90-Day Roadmap

**PHASE 1 (0–30) — Controls + start the clock.** Deliver: harness-integrity assertions +
backtest-before-paper gate; Bhavcopy refreshed to 2026; delivery forward paper book running;
**correlation study (DONE)**; build H4 (Delivery Spike) backtest. *Gate:* H4 must clear costs
net + benchmark-relative. *Risk:* event false positives (liquidity filter).

**PHASE 2 (31–60) — Regime kickoff + different-driver scoping.** Deliver: start pre-2019 data
acquisition (source + normalize + data-quality audit); scope the H5 (PEAD) earnings-event panel;
forward track month 2. *Gate:* ≥1 pre-2019 year normalized + validated against the catalog.
*Risk:* historical data quality / corporate-action gaps.

**PHASE 3 (61–90) — Regime validation.** Deliver: run delivery over 2008/2011/2015/2018; forward
track quarterly review; build the PEAD panel if data acquired. *Go/No-Go:* **does delivery survive
a sustained bear?** Catastrophic failure → reclassify, halt pre-production. *Risk:* the honest one —
the edge may not survive 2008; that is the test's purpose, not a project failure.

## 9. CIO Letter

> **To the QE team — 2026-06-15**
>
> This quarter we deleted most of our strategy book, and that is the most valuable thing we did.
> ORB, VWAP reversion, intraday trend, pre-close, and scalp are retired — not because we executed
> them poorly, but because their expected move is smaller than what it costs to trade them. Three
> independent lines of evidence agreed. Killing them is the system working. A platform that cannot
> kill its own ideas will eventually deploy a bad one with real money.
>
> What survived is worth more than what we lost: the **delivery-% conviction factor** — positive in
> every one of five out-of-sample years, with risk-adjusted returns above the benchmark in every
> sub-period we can measure. The first edge QE has produced that we believe for structural reasons.
>
> But we have seen it only in a bull market, only on paper-in-theory, never forward in real time,
> and never in a crash. This week we also learned — by measuring — that our "diversifiers" (low-vol,
> value) are not diversifiers at all: they are the same equity beta wearing different clothes, and
> the one that looks defensive is *most* correlated exactly when markets fall. Real diversification
> will have to come from genuinely different return drivers, not more equity factors.
>
> We are not blocked because the infrastructure is broken — it is healthy. We are blocked because
> **we have not yet earned the right to be wrong with money.** Our discipline is non-negotiable:
> backtest before paper, paper before production, net-of-cost only, benchmark-relative always,
> walk-forward mandatory, and any "edge" that appears from a harness bug is treated as the bug it is.
> Capital preservation outranks return.
>
> QE earns the right to deploy capital when delivery-% completes a 12-month forward paper track
> within tolerance, survives a real historical bear, sits alongside at least one genuinely
> uncorrelated stream, and clears an 80/100 promotion score with every hard invariant intact. Not
> before. We are building a fund that survives long enough to compound.
>
> — CIO, QE

## 10. Recommended Immediate Actions

1. **Adopt** the PRE-PRODUCTION classification for delivery-% and the QE Promotion Score + thresholds
   (§3) as the standing promotion framework. *(ADR-036.)*
2. **Implement the two permanent controls** — harness-integrity assertions and the
   backtest-before-paper gate — before any new strategy work.
3. **Start the 12-month delivery forward paper track** (refresh Bhavcopy to 2026; isolated harness).
   Operator-gated; no execution_engine wiring.
4. **Correlation study — COMPLETE** (`factor-correlation-report.md`). Conclusion: don't build a
   combined equity-factor book; delivery standalone leads.
5. **H4 (Delivery Spike) — BUILT + TESTED → REJECTED** (`delivery-spike-report.md`, ADR-036
   addendum): 2,278 events, entry at close[t+1] (no lookahead), abnormal returns significantly
   *negative* (anti-predictive). Delivery info lives in the persistent *level* (the monthly factor),
   not in spikes. **Top remaining different-driver priority is now H5 (PEAD)** — needs an earnings
   panel the lake lacks (scope the data acquisition).
6. **Launch the regime-expansion project** (Tier-1) — pre-2019 NSE history; the bear test gates any
   scaling.
7. **Keep LIVE BLOCKED**; delivery book remains isolated and advisory.

> Backtesting can recommend; it cannot promote. GenAI can explain; it cannot trade. A human approves
> all production changes. Live trading remains BLOCKED.
