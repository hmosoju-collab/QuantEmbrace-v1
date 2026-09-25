# US Candidate Screen — Phase 2 Report (ADR-041)

_2026-07-10 · US equities pivot Phase 2 · Plan: `docs/strategy/us-equities-pivot-plan.md`_

**Status: SCREEN COMPLETE — 2 of 7 candidates clear the pre-registered gate.
Shortlist proposed below. Awaiting operator approval. LEAN cross-check of the
shortlist = Phase 2b (LEAN CLI not yet installed).**

---

## 1. Method (everything pre-registered before the run)

- **Harness:** `scripts/backtest/run_us_rotation_study.py` (self-test PASS: signal
  selection, T+1 no-lookahead boundary both directions, cost monotonicity, gate logic).
- **Data:** Phase 1 curated lake, snapshot `ds-b9b110ac58d57cae`, adjusted (total-return)
  closes, window 2006-06-30 → 2026-07-09 (≈20.0 years, 19 gateable calendar years).
- **TR sanity anchors passed exactly:** lake-computed SPY total returns 2023/24/25 =
  **26.18 / 24.89 / 17.72%** vs slickcharts published 26.18 / 24.89 / 17.72% —
  agreement to the hundredth of a percentage point. The screen hard-aborts if anchors
  fail.
- **Execution convention:** signals at month-end close T; trade at close of T+1; new
  weights earn returns from T+2. No same-bar execution.
- **Costs:** one-way turnover × 5bps (zero commission; ETF spread+impact cushion);
  full re-run at 10bps as sensitivity. Sharpe computed on returns in excess of SHY
  (cash proxy).
- **Gate (all 5 to pass):** G1 net excess Sharpe ≥0.80 · G2 ≥70% positive years ·
  G3 maxDD ≤ SPY's · G4 Sharpe > SPY's · G5 both ±25% lookback perturbations ≥0.60.

**Candidates** — all canonical QuantConnect-Strategy-Library / published-literature
families: Antonacci dual momentum (GEM), Faber GTAA-5 (SSRN 962461), SPDR sector
rotation, Jegadeesh-Titman 12-1 cross-sectional momentum, Keller-style adaptive asset
allocation, the low-volatility anomaly, and SPY/TLT/GLD risk parity.

## 2. Results

Benchmarks (same window, net): SPY buy-hold CAGR 11.32%, Sharpe 0.54, maxDD −55.2%,
16/19 positive years · 60/40 SPY/IEF CAGR 8.73%, Sharpe 0.62, maxDD −31.4%.

| Candidate | Verdict | Sharpe (5bps) | CAGR | maxDD | +years | ±25% Sharpes | 10bps Sharpe |
|---|---|---|---|---|---|---|---|
| GEM (12m dual momentum) | FAIL 1/5 | 0.33 | 6.1% | −35.1% | 13/19 | 0.35 / 0.44 | 0.31 |
| GTAA5 (10m SMA timing) | FAIL 2/5 | 0.51 | 5.9% | −16.2% | 15/19 | 0.56 / 0.54 | 0.48 |
| SECTOR (6m top-3 rotation) | FAIL 1/5 | 0.33 | 6.2% | −24.4% | 13/19 | 0.45 / 0.59 | 0.30 |
| **XSMOM (12-1 top-10 mega-cap)** | **PASS** ⚠️ | **0.86** | 23.0% | −49.0% | 15/19 | 0.81 / 0.78 | 0.85 |
| AAA (6m top-3 + inv-vol) | FAIL 4/5 (G1) | 0.63 | 9.4% | −29.3% | 15/19 | 0.63 / 0.73 | 0.60 |
| LOWVOL (252d bottom-20) ⚠️ | FAIL 4/5 (G1) | 0.66 | 11.4% | −38.0% | 18/19 | 0.64 / 0.63 | 0.65 |
| **RPLITE (SPY/TLT/GLD inv-vol)** | **PASS** | **0.86** | 9.5% | −22.2% | 15/19 | 0.86 / 0.84 | 0.85 |

Results JSONs: `reports/us_screen/screen_20260710_171611_cost5bps.json`,
`…171715_cost10bps.json`.

## 3. Reading the results honestly

- **The famous published TAA strategies failed, consistent with their documented
  post-publication decay.** GEM at 0.33 and sector rotation at 0.33 over a window that
  is mostly *after* their publication is the expected out-of-sample result, and mirrors
  this platform's own NSE findings (edges decay once published). The screen agreeing
  with the literature's decay evidence is a point *for* the methodology.
- **GTAA5 reproduces the F2 lesson:** drawdown reduction (−16% vs −55%) without
  risk-adjusted alpha — "beta with a seatbelt," not an edge.
- **XSMOM's 23% CAGR is survivorship-inflated and must not be taken at face value.**
  The universe is *today's* 74 mega-caps — stocks selected precisely because they won.
  The platform's capstone lesson (F1: a non-tradable proxy overstated harvestable edge
  3.5×) applies directly. The PASS is a hypothesis worth falsifying, not evidence of an
  edge.
- **RPLITE is the clean pass:** ETF-only (survivorship-free), robust to parameter
  perturbation (0.86/0.84) and costs (0.85 @ 10bps), maxDD −22% vs SPY's −55%, and it
  beats both benchmarks risk-adjusted. Framing matters: this is **diversified
  multi-asset beta harvested systematically**, not stock-picking alpha. Its excess
  Sharpe over SPY comes from diversification across risk premia (equities/duration/
  gold) plus vol-balancing. That is a legitimate, durable mechanism — but it should be
  held to the "is this better than just buying it passively?" question in Phase 4
  (walk-forward vs a static equal-risk benchmark, not only vs SPY).

## 4. Proposed shortlist (2, one conditional)

1. **RPLITE — shortlisted.** Clean gate pass, survivorship-free universe, robust,
   cost-insensitive, mechanically simple (3 ETFs, monthly, inverse-vol). Proceeds to
   Phase 2b LEAN cross-check → Phase 3/4 qe port.
2. **XSMOM — conditionally shortlisted, falsification required.** Before any qe port,
   it must be re-tested on a survivorship-free universe. Cheapest $0 route: a one-off
   backtest on QuantConnect's **free cloud tier**, whose US equity data is
   survivorship-bias-free (this uses QC exactly as the plan intends — research bench).
   If the survivorship-free Sharpe collapses below G1, XSMOM dies here and is recorded
   like every other falsified hypothesis.
3. **Not shortlisted, gate not relaxed:** AAA (0.63) and LOWVOL (0.66) fail G1;
   LOWVOL's 18/19 positive years is attractive but sits on the survivorship-selected
   universe. Per standing rule, the pre-registered gate is not relaxed after seeing
   results.

## 5. Phase 2b (remaining before Phase 3)

- Install LEAN CLI (`pip install lean`; Docker is already running) — P0 operator item.
- Lake→LEAN custom-data converter; reproduce RPLITE (+ XSMOM if it survives
  falsification) in LEAN with QC's fee/slippage models; cross-check vs the pandas
  harness within a pre-declared tolerance.
- XSMOM survivorship falsification on QC free cloud tier.
- Then Phase 3 (qe US market support) proceeds for survivors only.

## 6. Limitations

- Single-engine screen so far (pandas harness; LEAN cross-check pending). Mitigations
  already in place: exact TR anchors, T+1 boundary self-tests, dual cost levels.
- 19 gateable years ≈ 2 full market cycles; regime-dependence (RPLITE leans on the
  2008–2020 bond bull) gets explicitly tested in Phase 4 walk-forward (the 2022 rate shock is
  in-window; RPLITE's worst year was −15.75%, within its −22.3% maxDD).
- Screen costs exclude taxes (operator-level, jurisdiction-dependent) — consistent
  with how the NSE screens were run; full statutory treatment belongs to Phase 4+.
