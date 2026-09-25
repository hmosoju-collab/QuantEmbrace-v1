# Phase 2b — LEAN Engine Cross-Check of RPLITE (ADR-041)

_2026-07-14 · US equities pivot Phase 2b · Plan: `docs/strategy/us-equities-pivot-plan.md`_

**Status: PARITY PASS. RPLITE is now cross-validated in an independent engine
(LEAN) against the pandas screen harness. Awaiting operator approval → Phase 3.**

---

## 1. What this checks

RPLITE (SPY/TLT/GLD, inverse-63-day-vol, monthly rebalance — the clean shortlisted
candidate from `docs/strategy/us-qc-candidate-report.md`) was re-implemented inside
the LEAN engine (`scripts/backtest/lean/main.py`, `RpliteCrossCheck`) and run in
Docker against the **same exported total-return series** used by the pandas screen
(`scripts/backtest/export_lake_to_lean.py`). Zero costs on both sides — this is an
engine-mechanics parity check only; the cost model is validated separately in the
pandas 5/10bps harness.

Pre-declared tolerances (before the first run):

| Gate | Threshold | Result |
|---|---|---|
| T1 monthly-return RMSE | ≤ 20bps | **3.4bps** ✅ |
| T2 \|final NAV multiple ratio − 1\| | ≤ 3% | **0.96%** ✅ |
| T3 monthly-return sign agreement | ≥ 95% | **100%** ✅ |

Comparison window 2006-08-01 → 2026-07-09 (240 months), correlation 0.9999.

## 2. This ran once already (2026-07-10) and falsely failed — corrected 2026-07-14

An earlier run on 2026-07-10 produced **"PARITY FAIL"** (T1 RMSE 43.2bps, ~2× the
tolerance), worst months 2022-09, 2022-10, 2023-12. That result was never written up
or stopped-for-approval before this session — it sat in an uncommitted, gitignored
JSON (`reports/us_screen/lean_crosscheck.json`) and was only discovered when this
session resumed the US equities track. Per the standing no-gate-relaxation rule, that
result was treated as real and root-caused rather than waved through.

**Root-cause investigation (three hypotheses tested and ruled out before finding the
real cause):**

1. **Strategy/vol-calculation logic** — replicated LEAN's exact inverse-vol weight
   calculation in pure Python off the same exported CSVs and diffed it against the
   pandas `w_rplite()` weights at all 241 rebalance dates: agreement to ~1e-8
   (floating-point noise). Ruled out.
2. **Whole-share rounding** — LEAN's `SetHoldings` rounds to whole shares (confirmed
   via real fill data, e.g. exact integer quantities). Reconstructed a fractional-share
   version from the same fills/dates and it diverges from the whole-share version by
   only 0.74bps monthly RMSE, with a completely different worst-month profile
   (2008-11/12, 2009-11) than the reported failure. Ruled out.
3. **Margin/buying-power rejections** — the LEAN run log showed 149 "Insufficient
   buying power" order errors across the 20-year backtest, including on 2022-09-01 and
   2022-10-03 directly and 2023-11-01 (bleeding into December). The per-security
   `BuyingPowerModel.Null` override in `main.py` was supposed to prevent exactly this
   but evidently didn't fully — the account was still typed `Margin`. **Investigated
   and fixed** (switched to `AccountType.Cash` + `ImmediateSettlementModel`,
   `main.py:40-64`) — re-running confirmed **zero order errors** afterward. However,
   the actual fill quantities/prices/dates were byte-identical before and after this
   fix, proving it was not the cause of the RMSE (a real bug, but a cosmetic one —
   kept anyway since it removes log noise and better matches the pandas leg's
   frictionless assumption).

**The real cause: a chart-extraction bug in the comparison harness, not a strategy or
execution difference.** LEAN's "Strategy Equity" chart emits a midnight sample every
single trading day (value *before* that day's own bar — reflecting the prior day's
close) but only an *intermittent* same-day 17:00 sample (~15% of trading days — a
charting-decimation artifact of the LEAN result writer, unrelated to trading). The
original `compare()` used `lean_nav.resample("D").last()`, which let that sporadic,
unreliable 17:00 point silently override the reliable midnight point whenever it
happened to exist that day — corrupting the day-to-day alignment inconsistently
across the series. Verified directly: reconstructing LEAN's actual equity curve from
its own fill/order-event data (real quantities × real fill prices) matched the pandas
leg's return series almost exactly (0.02% RMSE, essentially zero at the three
"worst" months) — proving the underlying trades were already correct and the parity
failure was purely an artifact of how the comparison script read LEAN's chart output.

**Fix** (`run_lean_crosscheck.py:139-156`): align using *only* the always-present
midnight samples (never missing, verified across all 5121 trading days in the run
window), mapped one trading day forward per the harness's documented lag convention,
instead of the resample-then-shift approach that mixed in the unreliable 17:00 points.

## 3. Verdict

RPLITE clears all three pre-registered LEAN parity gates. Combined with the clean
Phase 2 pandas screen pass (Sharpe 0.86, survivorship-free ETF universe, robust to
cost and lookback perturbation), RPLITE is now validated in two independent engines
and is ready for Phase 3 (qe US market support) pending operator approval.

## 4. Remaining before Phase 3

- XSMOM survivorship falsification on QC's free cloud tier — still not done (separate
  from this cross-check, applies only to the conditionally-shortlisted candidate).
- Operator approval of this report.
