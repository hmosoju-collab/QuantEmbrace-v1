# QuantEmbrace — Strategy Eligibility Decision Report

> ## ⚠️ DECISION MADE ON NON-AUTHORITATIVE EVIDENCE
> The backtest, walk-forward, TEE, and model-dataset inputs are **SYNTHETIC** (no real licensed NSE data has been ingested — see `data-ingestion-pending-vendor-report.md`). The platform also has only **1 valid quality-gate paper session** (Session 12, 2026-06-05) against the **≥5 required** (CLAUDE.md). Therefore the **data-quality-confidence criterion fails for every strategy**, which gates the decision. **No strategy can be promoted on this evidence.**
>
> **No live trading approved or enabled. No capital recommendation above Stage-1.** Generated: 2026-06-06.

---

## Evidence used & authoritativeness

| Source | Artifact | Authoritative? |
|---|---|---|
| Backtest metrics | `aws-10-15y-full-backtest-report.md` / `_full_run_summary.json` | ❌ synthetic |
| Walk-forward | `aws-phase9-walk-forward-report.md` (44 folds, stability 1.0) | ❌ synthetic objective |
| TEE comparison | `aws-phase7-tee-mis-report.md` (old capture 0.54 vs new 0.50) | ❌ synthetic |
| Model dataset | `aws-phase10-model-dataset-report.md` (`ds_bt12`, HIGH-trust **synthetic**) | ❌ synthetic |
| Data quality | `data-ingestion-pending-vendor-report.md` | ✅ confirms **no real data** |
| Paper sessions | platform state (CLAUDE.md): Session 12 = first valid; <5 valid | ✅ verdict `PAPER_OPTIMIZATION` |

## Decision criteria & thresholds

| Criterion | Bar for a paper candidate | Bar for Stage-1-later |
|---|---|---|
| Net P&L after costs | > 0 on **real** data | > 0, persistent |
| Profit factor | > 1.2 | > 1.2 across folds |
| Max drawdown | within risk budget | stable, bounded |
| Sample size | enough **real** trades for significance | large, multi-regime |
| Walk-forward stability | OOS robust on real data | robust + stable params |
| MIS dependency | low (TEE resolves most) | low |
| TEE profit_capture_ratio | reasonable capture, low giveback | strong |
| Slippage sensitivity | edge survives realistic slippage | survives stress |
| **Data-quality confidence** | **HIGH-trust real NSE data** | HIGH-trust, multi-year |

> The last criterion is a hard gate. It is **FAIL** for all strategies today (synthetic data), so none can exceed `NEEDS_MORE_DATA` (or `DISABLED`).

## Per-strategy decision

Legend for evidence cells: `synth` = synthetic only · `n/a` = not produced · `none` = no real data.

| Strategy | Net P&L (real) | PF | MaxDD | Sample (real) | WF stability | MIS dep | TEE capture | Slippage sens. | Data-quality conf. | **Verdict** |
|---|---|---|---|---|---|---|---|---|---|---|
| VWAP reversion | none | n/a | n/a | none (synth signals 63) | synth | n/a | n/a | untested(real) | **NONE** | **NEEDS_MORE_DATA** |
| momentum | none (synth +) | synth ~1.2 | synth ≤1.7% | none (synth trades) | synth 1.0 | synth 0 | synth 0.50–0.54 | untested(real) | **NONE** | **NEEDS_MORE_DATA** |
| ORB | none | n/a | n/a | none (synth signals 0) | synth | n/a | n/a | untested(real) | **NONE** | **NEEDS_MORE_DATA** |
| trend_15m | none | n/a | n/a | none (synth signals 0) | synth | n/a | n/a | untested(real) | **NONE** | **NEEDS_MORE_DATA** |
| preclose | none | n/a | n/a | none (synth signals 1) | synth | n/a | n/a | untested(real) | **NONE** | **NEEDS_MORE_DATA** |
| scalp_1m v2 | none | n/a | n/a | none (synth signals 0) | synth | n/a | n/a | untested(real) | **NONE** | **DISABLED (Stage-1)** |

### Rationale

- **VWAP reversion** — fired most on synthetic noise (63 signals), which says nothing about real edge. No real P&L, PF, or drawdown. → **NEEDS_MORE_DATA**; remains **PAPER_ONLY** operationally.
- **momentum** — the only strategy with (synthetic) P&L runs; synthetic PF hovered ~1.17–1.20 and drawdown was low, but these are random-walk artifacts. Best-instrumented, still unproven on real data. → **NEEDS_MORE_DATA** (PAPER_ONLY).
- **ORB** — produced ~0 signals on synthetic intraday (needs a real opening-range breakout). Cannot assess. → **NEEDS_MORE_DATA** (PAPER_ONLY).
- **trend_15m** — ~0 signals (needs real EMA-cross + ADX regime). → **NEEDS_MORE_DATA** (PAPER_ONLY).
- **preclose** — window-gated (14:45–15:10 IST); 1 synthetic signal. → **NEEDS_MORE_DATA** (PAPER_ONLY).
- **scalp_1m v2** — paper-only by design; its v2 edge floors correctly rejected low-edge synthetic bars. Per the standing rule, it **remains DISABLED for Stage-1 unless separately proven**. → **DISABLED (Stage-1)**; paper research may continue.

## Summary

| Strategy | Verdict | Operational status |
|---|---|---|
| VWAP reversion | NEEDS_MORE_DATA | PAPER_ONLY |
| momentum | NEEDS_MORE_DATA | PAPER_ONLY |
| ORB | NEEDS_MORE_DATA | PAPER_ONLY |
| trend_15m | NEEDS_MORE_DATA | PAPER_ONLY |
| preclose | NEEDS_MORE_DATA | PAPER_ONLY |
| scalp_1m v2 | DISABLED (Stage-1) | PAPER-research only |

**No `CANDIDATE_FOR_PAPER` and no `CANDIDATE_FOR_STAGE1_LATER` awarded** — the evidence base is synthetic and the data-quality gate fails for all.

## Rules honored

- **Do not approve live automatically** — nothing approved; live remains BLOCKED.
- **scalp_1m disabled for Stage-1** unless separately proven — enforced.
- **New TEE remains paper-only** until enough paper evidence — enforced (it is not promoted; MIS stays cleanup-only).
- **No capital recommendation above Stage-1** — none given.

## What would change each verdict (path forward)

1. **Ingest HIGH-trust real NSE data** (official Bhavcopy daily + licensed intraday) → pass data quality → real `data_snapshot_id`.
2. **Re-run the full backtest** (`run_full_backtest_aws.py` wired to the real Parquet `BarSource`) per strategy on real history.
3. A strategy becomes **`CANDIDATE_FOR_PAPER`** when, on real data: net P&L > 0 after costs, PF > 1.2, drawdown within budget, sufficient sample, and **walk-forward OOS robust + stable**.
4. A `CANDIDATE_FOR_PAPER` becomes **`CANDIDATE_FOR_STAGE1_LATER`** only after **≥5 consecutive valid quality-gate paper sessions** pass all strategy-performance gates (expectancy>0, PF>1.2, realized P&L>0, reconciliation clean, Section 16/17 gates) — and even then promotion is a **manual operator decision**. The lab and this report are advisory and never promote.

---

*Decision on synthetic + insufficient-paper evidence → conservative. Re-run after real-data ingestion. No live, no capital above Stage-1, scalp disabled for Stage-1, new TEE paper-only. Stop.*
