# Phase 4 — Port RPLITE into qe, Parity, Walk-Forward, Pre-Register the US Forward Gate (ADR-041)

_2026-07-14 · US equities pivot Phase 4 · Plan: `docs/strategy/us-equities-pivot-plan.md`_

**Status: BUILT + a real historical run over the actual US lake. RPLITE is now a
first-class qe strategy, parity-checked against the validated pandas screen,
walk-forward tested over the real 20-year lake, and the US Forward Gate is
pre-registered. Awaiting operator approval → Phase 5 (US forward paper book).**

---

## 1. What was built

| Component | What it does |
|---|---|
| `qe/strategy/risk_parity.py` — `RiskParityLiteStrategy` | The RPLITE port: inverse-63d-vol weights across a static asset set (SPY/TLT/GLD by default), verbatim from `w_rplite`, plus a small `cash_buffer` (see §2 — a real finding, not cosmetic). |
| `qe/config.py` — `StrategyConfig.kind` | Extended `Literal["factor_book"]` → `Literal["factor_book", "risk_parity_lite"]` with `assets`/`vol_lookback`/(shared)`cash_buffer` fields. NSE `factor_book` fields/defaults untouched. |
| `qe/engine/sim.py` — `_build_strategy` | Dispatches on `config.strategy.kind`; factor_book path byte-for-byte unchanged. |
| `qe/research/us_study.py` — `run_risk_parity_study`, `run_risk_parity_walkforward` | Parallel to (not a modification of) `qe.research.study.run_factor_book_study` — SPY buy-and-hold benchmark, `$` NAV, identical `summary.json` shape so gate tooling can read it the same way. The walk-forward function is a real in-sample/out-of-sample split over the qe-ported engine (no v1 cross-check — there is no v1 US model). |
| `qe/cli.py` | `python -m qe study --config <risk_parity_lite forward_book config>` now dispatches to the US study path. Walk-forward CLI dispatch for this kind is not wired yet — call `run_risk_parity_walkforward` directly (see `qe/research/us_study.py`). |
| `scripts/paper/check_us_forward_gate.py` | The pre-registered US Forward Gate — **identical math/thresholds** to `check_forward_gate.py` (NSE), benchmark = SPY instead of the EW liquid universe, qe-only (no v1 US book exists to cross-check). `--self-test` passes. |
| `configs/qe_us_rplite_book.yaml` | The forward book config for Phase 5 — `start_date: 2026-07-31` (next month-end; the book isn't active yet, Phase 5 activates it), `cash_buffer: 0.005`. |
| `tests/qe/test_us_rplite_parity.py`, `test_us_study.py` | 6 new tests (below). |

## 2. A real finding: RPLITE needs a cash buffer in a real-cash engine

The pandas screen is a **returns-space** model — weights summing to 1.0 have no
"solvency" question there, since there's no literal cash balance. qe's engine
is **real-cash-accounting**: spending 100% of NAV on assets plus even a few bps
of transaction cost needs slightly *more* than 100% of NAV, which tripped the
risk engine's `cash_non_negative` check on **every single rebalance** the first
time RPLITE was run end-to-end (discovered immediately when wiring the test —
first rebalance rejected with `projected cash $-430.50 < 0`). This is not a bug
in the risk engine — real money genuinely cannot be over-spent — it's a
mechanical consequence of porting a frictionless returns-space design into a
real-cash engine, exactly analogous to why `FactorBookStrategy` already has its
own `cash_buffer` field. Fixed with a small (0.5%, vs the factor books' 2% —
RPLITE's round-trip cost is a few bps vs NSE's 30+bps) buffer that scales the
raw inverse-vol weights down before returning them. The buffer is additive and
tested separately from the core port: the parity tests below use
`cash_buffer=0.0` explicitly to prove the *raw* ported math is untouched.

## 3. qe-vs-pandas-screen parity (transitively qe-vs-LEAN)

Two layers, mirroring exactly how the Phase 2b LEAN root-cause was done manually:

1. **Weight-target parity** (`test_weight_target_matches_v1_at_every_rebalance`) —
   `RiskParityLiteStrategy.rebalance` (cash_buffer=0) vs `w_rplite` at every
   rebalance point on a synthetic panel: matches to **1e-10** (float noise).
2. **Zero-cost NAV-path parity** (`test_qe_zero_cost_nav_matches_v1_screen`) —
   with both engines' costs zeroed (`run_sim(..., broker=SimBroker(zero_cost))`,
   a new test-only injection point added to `run_sim`), qe's simulated NAV path
   vs the pandas `run_weights` reference: **9.85bps monthly RMSE** on the
   synthetic panel (well inside the 15bps test tolerance; consistent with the
   whole-share-rounding-only baseline of ~0.7bps found in Phase 2b, scaled up
   somewhat by the small sample size of a synthetic test).

Since Phase 2b already proved pandas-screen ≈ LEAN to 3.4bps monthly RMSE, and
this establishes qe ≈ pandas-screen, the chain gives qe-vs-LEAN confidence
without re-running Docker for every future change to the qe engine.

## 4. Real historical run over the actual US lake (not synthetic)

Ran `run_risk_parity_study` + `run_risk_parity_walkforward` over the real
Phase-1 lake (SPY/TLT/GLD, 2006-08-01 → 2026-07-09, the same window Phase 2
screened), through the full qe engine (real `USEquityCosts`, integer share
rounding, 0.5% cash buffer, T+1 execution):

| | CAGR | Sharpe | MaxDD | Hit rate | Months | Positive years |
|---|---:|---:|---:|---:|---:|---:|
| **qe engine, full history** | 7.18% | 0.78 | −21.3% | 58% | 238 | 17/21 |
| Phase 2 pandas screen (5bps, frictionless) | 9.5% | 0.86 | −22.2% | — | — | 15/19 |
| **In-sample** (2006-08 → 2016-01) | 6.55% | 0.72 | −14.6% | — | 111 | 8/10 |
| **Out-of-sample** (2016-01 → 2026-07) | 7.73% | 0.82 | −21.3% | — | 125 | 9/11 |

**Reading this honestly:**

- The qe-engine's real Sharpe (0.78) sits modestly below the Phase 2 screen's
  0.86 — expected, from real US costs (vs the screen's flat 5bps), the 0.5%
  cash buffer, and share-rounding, none of which the frictionless pandas
  screen models. This is the real, achievable number, not the idealized one —
  worth knowing precisely because the original G1 screen gate (≥0.80) was
  calibrated against the frictionless model, not this one.
- **The walk-forward split is genuinely reassuring**: Sharpe *improved*
  slightly out-of-sample (0.72 → 0.82) and CAGR held up (6.55% → 7.73%) — the
  opposite of the decay pattern this platform has found almost everywhere
  else (NSE delivery/momentum, GEM, GTAA5, sector rotation all decayed
  out-of-sample). The larger OOS drawdown (−21.3% vs −14.6%) is the 2020
  COVID shock and 2022 rate shock, both inside that window — expected for an
  unlevered multi-asset book holding duration.
- **The sobering number: mean monthly alpha vs SPY is −0.218%, positive only
  41% of months.** Over this specific 20-year window, SPY's own beta was
  exceptional (a similar finding to the NSE consolidation memo's central
  lesson — most apparent edge is beta, and here the *benchmark itself* was
  the hard-to-beat beta). **This means the pre-registered US Forward Gate
  (§5) — which requires cum_alpha > 0 and ≥58% positive-alpha months vs SPY —
  is a genuinely tough bar for RPLITE to clear forward, and it is plausible
  it never does, exactly as most NSE strategies never cleared their gates.**
  This is not a reason to relax the gate or change the benchmark after seeing
  this result — it is exactly the "let the forward book be the truth test"
  discipline working as designed. The operator should go into Phase 5 with
  this expectation set correctly: RPLITE's case was always "better
  diversification, smaller drawdown, decent risk-adjusted return," never
  "beats SPY outright" (Phase 2 report §3 said this explicitly).

## 5. US Forward Gate — pre-registered now, before Phase 5 accrues a single month

`scripts/paper/check_us_forward_gate.py`, criteria identical to the NSE Forward
Factor Gate:

1. Horizon ≥ 12 complete forward months.
2. Cumulative alpha vs SPY > 0.
3. Monthly-alpha info ratio ≥ 0.50.
4. Positive-alpha in ≥ 58% of months, no single month > 50% of cumulative alpha.
5. Forward max drawdown ≤ SPY's over the same window.

Clearing → human review for a small gated capital pilot. **Never auto-deploy.
Live remains BLOCKED.** `--self-test` passes. Right now there is no forward
book yet (Phase 5 hasn't started), so running it reports "no qe study summary
found" — correct, expected behavior.

## 6. Remaining before Phase 5

- Operator approval of this report.
- XSMOM survivorship falsification (unrelated, still open since Phase 2).
- Phase 5 itself: activate `configs/qe_us_rplite_book.yaml` on the monthly
  cadence, fold into the existing NSE+US cadence run, start forward accrual.
- Minor/optional: CLI-wire `study_kind: walk_forward` for `kind:
  risk_parity_lite` (currently must be called directly from Python).
