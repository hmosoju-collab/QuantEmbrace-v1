# QuantEmbrace — First Authoritative Daily-Data Backtest Protocol

> **Status: PLANNED — protocol/design only.** Advisory-only governance is intact:
> backtesting **recommends**, it does not promote; GenAI **explains**, it has no
> verdict authority; **a human approves all production changes.** No code is
> changed by this document.
> Last updated: 2026-06-06 · Governed by `aws-backtesting-steering.md` ·
> Consumes: `daily-bhavcopy-ingestion-design.md` · Canon: `aws-data-lake-contract.md`,
> `no-lookahead-rules.md`, `cost-slippage-model.md`, `walk-forward-validation.md`,
> `metrics-catalog.md`.

The first authoritative backtest for a serious retail trader applying institutional
discipline. **Constraints:** no synthetic results · daily Bhavcopy backbone only ·
no intraday strategy conclusions · no ML · no GenAI verdict authority · advisory-only
governance intact.

---

## 1. Eligible strategies

**Eligible: `momentum` only.** `MomentumStrategy` (dual-MA crossover + ATR stop/TP/
sizing) consumes generic `Bar`s via `on_bar` and declares **no** `candle_interval`,
so it is well-defined on daily closes. Run it as a **daily/positional configuration**
(e.g. 10/50-day SMA) and register it as a **distinct run** (its own `config_hash` ⇒
own `run_id`), separate from any intraday momentum config.

**Ineligible — cannot be tested or concluded on with daily data** (intraday by
construction; daily Bhavcopy can neither generate their signals nor validate their
edge — concluding on them would be a category error):

| Strategy | Why intraday-only |
|---|---|
| `vwap_reversion` | `candle_interval="minute"`, session-reset VWAP |
| `orb` | `candle_interval="minute"`, opening-range breakout |
| `intraday_trend_15m` | `candle_interval="15minute"` |
| `preclose_momentum` | `candle_interval="5minute"`, 14:45–15:10 IST window |
| `scalp_1m` | `candle_interval="minute"`; also Stage-1 DISABLED |

Future purely-daily strategies (daily breakout, daily MA, daily mean-reversion) may
be added later; this protocol concludes only on logic fully defined by daily OHLCV.

## 2. Required data snapshot metadata

Published as `_snapshots/{data_snapshot_id}.json` **only after** the DQ gate passes
(`daily-bhavcopy-ingestion-design.md` §5). A run is invalid without it.

| Field | Requirement |
|---|---|
| `data_snapshot_id` | immutable; referenced by every run |
| `source` / `trust_level` | `bhavcopy` (direct NSE archives) / **HIGH** — synthetic or LOW ⇒ **STOP** |
| provenance | per-file SHA-256 + acquisition URLs/dates (`raw/bhavcopy/...`) |
| `interval` / `segment` / `market` | `1d` / `EQ` / `NSE` |
| date span / row counts | full window; per-symbol trading-day counts |
| universe | symbols incl. **delisted** (survivorship-safe), reconstructed as-of |
| reference versions | `nse_trading_calendar` version; corporate-actions reference version |
| adjustment policy | unadjusted OHLC + `adj_factor` (applied as-of at read) |
| DQ result | report pointer; **0 ERROR issues**; `eligible_for_use == True` |
| `code_version` | builder commit that produced the snapshot |

## 3. Required run registry metadata (`qe-bt-runs`)

Maps to `run_registry.RUN_FIELDS`; registered **before** execution:

`run_id` (=`bt_{config_hash}`, deterministic/idempotent) · `status` · `strategy`
(`momentum_v2_daily`) · `symbols` · `timeframe = 1d` · `start_date`/`end_date` ·
`config_s3_path` · `result_s3_path` · `checkpoint_s3_path` · `code_version` ·
**`data_version` ≡ the authoritative `data_snapshot_id`** · `trust_level = HIGH` ·
**`cost_model_version` = `in-eq-delivery-2024.10` (the delivery profile, §4)** · `exit_policy_version` ·
`operator` · `started_at`/`updated_at`/`completed_at` · `error_reason` · `config_hash`
· `record_version` (optimistic lock). Same `config_hash` must reproduce identical
results.

## 4. Cost / slippage assumptions and caveats

Use **`IndianCostModel.delivery()`** for positional/CNC economics (`cost_model_version`
`in-eq-delivery-2024.10`); the intraday default profile has been corrected to the
current NSE exchange rate (`in-eq-intraday-2024.10`):

- **Statutory (delivery profile):** brokerage **0** (set `brokerage_pct=0`), exchange
  0.00297% both legs, SEBI 0.0001% both, stamp **0.015%** buy, GST 18% on
  (brokerage+exch+SEBI). **STT is the critical difference: 0.1% on both legs** (vs the
  intraday 0.025% sell-only) — this swing alone can flip a thin edge negative.
  (DP/demat ~₹16/sell is a documented minor omission.)
- **Spread + slippage:** keep (baked into a worse fill price). Restrict the universe
  to **liquid** large-caps (NIFTY 50/100) so liquid-tier slippage (≈1 bp) + 5 bp
  spread assumptions hold; daily data cannot model real intraday impact.
- **Daily-data caveats:** execution is **next-bar = next-day open** (no same-day
  peeking); **intraday stop-vs-target ordering is unknowable** from daily OHLC →
  adopt a conservative resolution (assume the adverse level is hit first);
  **gap-through fills at the worse bar open**, never at the stop level. Costs and
  slippage are **mandatory ON**; any disabling is flagged in the run report.

## 5. Walk-forward setup

Use `walk_forward.py` presets over 10–15 yr: **`long` (36m train / 12m validate /
12m roll)** as primary; `medium` (24/6/6) as a denser cross-check. **Rolling**,
half-open folds with `train_end == validate_start` (`_assert_no_leakage`). Optimise
parameters on IS only; report **OOS** as the honest edge. Objective `expectancy`
(cross-check `profit_factor`). Indicators: IS→OOS degradation, win consistency,
parameter-stability score, robustness report. Each OOS fold may be its own
registered `qe-bt-runs` entry. Advisory verdicts only:
`ELIGIBLE_FOR_PAPER_PRIORITIZATION` / `PAPER_OPTIMIZATION` / `REJECT`.

## 6. Required output reports

- **Per-run `report.md`** (`results/runs/{run_id}/`): config + `data_snapshot_id` +
  `code_version` + `cost_model_version`; metrics per `metrics-catalog.md` (net P&L
  after costs, profit factor, expectancy, max drawdown, trade count, exposure);
  **cost breakdown** (statutory vs spread vs slippage); **`lookahead_violations == 0`**;
  equity curve + trades in S3.
- **Walk-forward study report**: per-fold windows + chosen params + IS/OOS objective;
  aggregate OOS metrics + gate pass/fail; stability / overfit / consistency;
  parameter-robustness; advisory verdict.
- **Data-quality / snapshot report** referenced by id.
- **Reproducibility manifest** (versions + checksums).
- GenAI may produce a plain-language **summary only — no verdict.**

## 7. Promotion boundary — what this result can and cannot justify

**Can:** serve as *advisory* evidence to **prioritise a daily/positional strategy for
paper trading**, rank/triage candidates, reject strategies with no out-of-sample
edge, and estimate a cost-aware historical edge.

**Cannot:** promote to live; mark a strategy paper-*approved*; change any trading
config, universe, or capital; prove live-readiness; substitute for the **≥5 valid
paper sessions + operator sign-off** (CLAUDE.md); validate the execution/risk live
path, MIS/TEE intraday exits, or fill quality; conclude **anything** about the five
intraday strategies or any intraday behavior. GenAI explains, never decides. **A
human approves all production changes.** Ceiling for even an excellent result:
`ELIGIBLE_FOR_PAPER_PRIORITIZATION`.

## 8. Pass/fail checklist

**Data gate (hard):**
- [ ] `data_snapshot_id` published
- [ ] `trust_level = HIGH` direct-NSE (not synthetic / LOW)
- [ ] DQ `eligible_for_use = True`, 0 ERROR issues
- [ ] trading-calendar reconciled
- [ ] corporate-actions reference present
- [ ] delisted names included (survivorship-safe)

**Run integrity:**
- [ ] registered in `qe-bt-runs`
- [ ] `data_version == data_snapshot_id`
- [ ] delivery `cost_model_version` recorded
- [ ] costs + slippage ON
- [ ] `lookahead_violations == 0`
- [ ] deterministic (same `config_hash` reproduces)
- [ ] no broker calls possible

**Edge (advisory, after costs):**
- [ ] net P&L > 0
- [ ] profit factor > 1.2
- [ ] expectancy > 0
- [ ] max drawdown within budget
- [ ] enough *real* trades for significance

**Walk-forward:**
- [ ] OOS gates pass
- [ ] IS→OOS degradation ≥ 0.50 (not overfit)
- [ ] win consistency ≥ 0.50
- [ ] parameter stability ≥ 0.70

**Governance:**
- [ ] result labeled advisory
- [ ] no promotion / capital change
- [ ] GenAI verdict-free
- [ ] intraday strategies excluded from conclusions
- [ ] human review recorded

**Automatic FAIL:** synthetic / LOW data · DQ fail · `lookahead_violations > 0` ·
costs disabled unflagged · any intraday conclusion · any auto-promotion.

---

## References

- `daily-bhavcopy-ingestion-design.md` — the HIGH-trust daily snapshot this protocol consumes
- `aws-data-lake-contract.md` — trust tiers, schema, DQ gate, snapshot manifest
- `no-lookahead-rules.md` — next-bar execution, as-of adjustment, determinism
- `cost-slippage-model.md` — statutory/spread/slippage engine (intraday defaults; re-version for delivery)
- `walk-forward-validation.md` — fold geometry, presets, overfit/stability indicators
- `metrics-catalog.md` — metric definitions and gates
- `aws-backtesting-implementation-plan.md` — phase orchestration
- CLAUDE.md — advisory-only governance + live-readiness gate
