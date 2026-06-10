# QuantEmbrace — AWS 10–15 Year Full Backtest Report (AWS-BT-12)

> ## ⚠️ NON-AUTHORITATIVE — CONTROLLED SYNTHETIC DRY-RUN
> This run executed the **full pipeline end-to-end on SYNTHETIC data**. **No real licensed 10–15-year NSE history has been ingested** and **no AWS infrastructure is provisioned** — both are the standing Phase-0 §16 blocker. The numbers below prove the *orchestration* works; they are **not investment-meaningful** and must not inform any keep/tune/disable/live decision. Re-run on licensed/official NSE data after infra build for authoritative results.
>
> **No live trading was approved or enabled. No broker orders. No capital change.** Backtest-only.
> Generated: 2026-06-06 · Driver: `scripts/backtest/run_full_backtest_aws.py` · Governed by `aws-backtesting-steering.md`.

---

## Prerequisites (gate)

All prior phases PASS before execution — full lab suite **81/81**: data quality (12), run registry (9), checkpointing (incl. resume), replay engine (8), strategy adapters (8), execution simulator (9), cost/slippage, TEE/MIS (11), metrics/reporting (7), S3 output (stubbed), no-lookahead (across replay + adapters + execution), walk-forward (6), model dataset (6), GenAI (5).

## Run order executed

1. 1-month smoke (10 symbols) · 2. 1-year · 3. 5-year · 4. 5-year (wider) · 5. 10–15-year · 6. walk-forward · 7. model dataset · 8. GenAI report — all completed in the dry-run.

---

## 1. Run IDs

| Step | Label | run_id | Trades | Gate (advisory) |
|---|---|---|---:|---|
| 1 | smoke 1-month, NIFTY-10 | `bt_bc6b5f9e9dd579c3` | 0 | n/a (warm-up too short) |
| 2 | 1-year, NIFTY-50 (proxy) | `bt_192b0a29eba85b45` | 77 | PASS* |
| 3 | 5-year, NIFTY-50 (proxy) | `bt_cf7bee5610901b81` | 400 | FAIL* |
| 4 | 5-year, NIFTY-100 (proxy) | `bt_7df7ef17f7db8dae` | 591 | FAIL* |
| 5 | 10–15-year, high-liquidity (proxy) | `bt_9c407c5ccd0aa977` | 1193 | PASS* |

\* Gate = (expectancy>0, PF>1.2, net P&L>0) on **synthetic** data — noise, not edge.

## 2. Config paths

Each run wrote a full artifact set to `reports/backtests/<run_id>/` (config.yaml, summary.md, metrics.json, trades.parquet, equity_curve.parquet, strategy/symbol/exit_reason breakdowns, mfe_mae.parquet, rejected_signals.parquet, model_labels_preview.parquet, logs/). Configured S3 targets:

- `s3://quantembrace-backtest-results/runs/cfg/<label>.json` (config) · `s3://quantembrace-backtest-results/runs/<run_id>/` (outputs).

## 3. Data versions

`data_version = SYNTHETIC-no-real-nse-data` for every run. **No `data_snapshot_id` over real NSE history exists.** Authoritative runs require a HIGH-trust licensed/official NSE snapshot (see §12).

## 4. Code version

`code_version = aws-bt-12-dryrun` (lab modules at the AWS-BT-11 state). Stamped into every `metrics.json` and `config.yaml`.

## 5. Symbols

Synthetic placeholder universes (not real tickers): smoke `N10_00..09`; 1y/5y NIFTY-50 proxy `N50_00..11`; NIFTY-100 proxy `N100_00..17`; 10–15y high-liquidity proxy `HL_00..14`. Real runs must use as-of, survivorship-correct NSE constituents from point-in-time index membership (`aws-data-lake-contract.md` §7).

## 6. Strategies

Six production strategies via adapters: `momentum` (5m), `vwap_reversion` (1m), `orb` (1m), `intraday_trend_15m` (15m), `preclose` (5m), `scalp_1m` (1m, **paper-only**). The 5 horizon runs used **momentum on daily** bars (fast proxy); all six were exercised for the ranking (§7).

## 7. Strategy ranking (NON-AUTHORITATIVE)

By signals generated on a small synthetic intraday set (P&L ranking needs real intraday data):

| Rank | Strategy | Interval | Signals | Notes |
|---|---|---|---:|---|
| 1 | vwap_reversion | 1m | 63 | fired on synthetic mean-reversion noise |
| 2 | momentum | 5m | 51 | |
| 3 | preclose | 5m | 1 | window-gated (14:45–15:10) |
| 4 | orb | 1m | 0 | needs a real opening-range breakout |
| 5 | intraday_trend_15m | 15m | 0 | needs EMA-cross + ADX regime |
| 6 | scalp_1m | 1m | 0 | edge floors reject low-edge synthetic bars (paper-only) |

This ranking reflects **synthetic-data behavior**, not edge. Several strategies require real intraday structure to fire; do not infer quality from it.

## 8. Symbol ranking (NON-AUTHORITATIVE)

Top synthetic symbols by net P&L in the 10–15y proxy run: `HL_14` (8,367), `HL_04` (7,863), `HL_00` (7,799), `HL_03` (6,310), `HL_01` (5,574). Synthetic random-walk artifact — meaningless for real symbol selection.

## 9. Exit-policy comparison (old vs new TEE)

On a synthetic trade: old global → realized 0.70R, capture 0.54, giveback 0.46, final `TRAILING`; new strategy-aware → realized 0.50R, capture 0.50, giveback 0.50, final `STOP_LOSS`; MIS dependency 0 for both. The machinery (R-based exits, breakeven, partial, trailing, MIS) works; the **magnitudes are synthetic**. Real comparison: `scripts/backtest/compare_tee_policies_aws.py` over real trades.

## 10. Walk-forward stability

Harness (synthetic evaluate, `default` 12/3/3 over 2012–2024): **44 folds**, stability **1.00** (not unstable), overfit `False`, mean OOS expectancy 3.0, eligibility `ELIGIBLE_FOR_PAPER_PRIORITIZATION`. Demonstrates the harness; the verdict is from a synthetic objective, not real edge.

## 11. Model-dataset summary

`ds_bt12`: 120 rows → **train 84 / val 18 / test 18** (chronological + embargo), `authoritative=True` (HIGH-trust synthetic), features `[feat_ema_ratio, feat_rsi_14]`, leakage-free (features point-in-time, labels future-only). Linked to the 5 run_ids. Real datasets require real signals + the live `FeatureReader` feature set.

## 12. Data-quality limitations (READ FIRST)

- **No real NSE data.** All inputs are synthetic random walks. There is no ingested 10–15-year history; the data lake is empty of real data.
- **No AWS infra.** No `backtest` env, worker ASG, `qe-bt-*` tables, or `quantembrace-backtest-*` buckets are provisioned; the registry/S3 here are in-memory/local stand-ins.
- **Intraday strategies under-fire** on daily/synthetic bars (ORB/trend_15m/scalp produced ~0 signals) — they need real intraday structure.
- **Sourcing required:** authoritative results need a HIGH-trust feed — official NSE Bhavcopy (daily backbone) + a licensed vendor (intraday). GitHub/free = LOW-trust → quarantine (`aws-data-lake-contract.md` §2).
- Until the above are resolved, **every result here is non-authoritative.**

## 13. Cost / slippage assumptions

Applied on every fill (mandatory): Indian statutory stack — brokerage 0.03%/leg, STT 0.025% (sell), exchange 0.00345%, SEBI 0.0001%, stamp 0.003% (buy), GST 18%; plus slippage 2 bps + half of a 4 bps spread. `cost_model_version = indian-v1`. Synthetic cost impact ranged ~2k (1y) to ~32k (12y). Detail: `cost-slippage-model.md`.

## 14. Recommendations

**No strategy can be ranked, kept, tuned, disabled, or proposed for live on this synthetic dry-run.** The honest verdict for all six:

| Strategy | Verdict (this run) | Rationale |
|---|---|---|
| momentum | **PAPER-ONLY — pending real data** | gate-pass is synthetic noise |
| vwap_reversion | **PAPER-ONLY — pending real data** | |
| orb | **PAPER-ONLY — pending intraday data** | under-fired on synthetic |
| intraday_trend_15m | **PAPER-ONLY — pending intraday data** | under-fired on synthetic |
| preclose | **PAPER-ONLY — pending intraday data** | window-gated, 1 signal |
| scalp_1m | **PAPER-ONLY (locked)** | paper-only by design |

- **keep / tune / disable:** deferred — require authoritative results on real data.
- **paper-only:** all six remain paper-only now (unchanged from platform state).
- **Stage-1 candidate later:** none yet. A candidate emerges only after (a) real-data backtest gates pass, (b) walk-forward robustness on real data, **and** (c) ≥5 valid paper sessions + operator sign-off (CLAUDE.md). The lab is advisory and never promotes.

### Path to authoritative results
1. Provision the `backtest` AWS env (Terraform `environments/backtest/`: worker ASG, `qe-bt-*` tables, `quantembrace-backtest-*` buckets, CloudWatch/SNS).
2. Ingest official NSE Bhavcopy (daily backbone) + licensed intraday into the Parquet lake; pass data-quality + produce a real `data_snapshot_id`.
3. Re-run `scripts/backtest/run_full_backtest_aws.py` wired to the real `BarSource` (replace synthetic generators).
4. Review real reports + walk-forward + TEE comparison; only then consider keep/tune/disable.

---

## Artifacts produced

- 5 run directories under `reports/backtests/<run_id>/` (full artifact set each) + `reports/backtests/_full_run_summary.json`.
- Model dataset `ds_bt12` (train/val/test + manifest).
- GenAI summary: provider `stub`, cited (`## Sources` present), advisory.

*Controlled synthetic dry-run. No infra deployed, no real data, no live trading enabled, no live approved, no broker APIs called. Stop for approval.*
