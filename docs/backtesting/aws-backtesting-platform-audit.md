# QuantEmbrace — Historical Backtesting & Model-Training Platform Audit

> **Audit only.** Verification, challenge, and gap analysis — no implementation, no redesign. Existing architecture (EC2 ARM64 ASGs, S3, DynamoDB, MSK Serverless; Fargate removed per ADR-009) is followed, not replaced. Recommendations are limited to gaps, risks, contradictions, scalability, and cost.
> Auditor roles: Principal Quant / AWS / MLOps / Trading-Systems Reviewer · Date: 2026-06-06.
> Inputs read: `CLAUDE.md`, `architecture/system_design.md`, `memory/open_tasks.md`, `memory/decisions.md` (ADR-001…028), `docs/live-readiness/*`, all `docs/backtesting/*` (23 docs), Terraform, and the backtesting code (`services/backtesting/**`, 86 tests passing).

---

## SECTION 1 — Executive Summary

**Current state.** The backtesting lab is **code- and design-complete**: 21 modules under `services/backtesting/**` (+ GenAI), 9 scripts, 23 design/report docs, **86 unit tests passing**. It cleanly reuses the existing engine (`strategy_engine/backtesting/backtester.py`) and the production architecture. **However, no real licensed NSE data has been ingested and no AWS backtest infrastructure is provisioned** — every result produced so far is from a synthetic dry-run and is explicitly non-authoritative.

**Planned state.** An offline AWS lab (separate `backtest` env, `qe-bt-` tables, `quantembrace-backtest-*` buckets, EC2 ARM64 scale-from-0 workers, on-demand Bedrock/Step Functions GenAI) that validates strategy edge over 10–15 yr NSE history and produces advisory recommendations only. Governance invariant (now in CLAUDE.md): backtesting recommends but cannot promote; GenAI explains but cannot trade; humans approve all production changes.

**Overall readiness.** Engineering of the lab is strong; **operational/production readiness is low** because the two hardest, most external dependencies — licensed historical data and provisioned cloud infra — are not done. This is the correct order (build the machine, then feed it), but the initiative cannot produce a single authoritative result today.

**Biggest strengths:** disciplined reuse of existing architecture; rigorous no-lookahead + mandatory-cost realism; strong, enforced safety governance (advisory-only, secret/forbidden-action guardrails, trust-tier quarantine); high test coverage; honest, phase-gated documentation.

**Biggest risks:** (1) no real data → zero authoritative output; (2) no provisioned infra → nothing runs on AWS; (3) bucket-name contradiction across docs; (4) registry/checkpoint concurrency model (read-modify-write, single checkpoint item) untested at fleet scale; (5) ML training/registry/experiment-tracking **not designed**; (6) licensed-data cost (external to AWS) likely dominates and is unbudgeted.

### Traffic-light status by area

| Area | Status |
|---|---|
| Existing trading architecture (reused) | 🟢 GREEN |
| Backtesting lab code & design | 🟢 GREEN |
| Backtesting engine realism (costs/no-lookahead) | 🟢 GREEN |
| GenAI analytics (advisory, guardrailed) | 🟢 GREEN |
| Walk-forward validation | 🟡 YELLOW (synthetic only) |
| Data lake | 🟡 YELLOW (contract+tooling, no data) |
| Feature store (offline/backtest) | 🟡 YELLOW (reuses live defs; no offline store) |
| Paper-trading validation | 🟡 YELLOW (1 of 5 valid sessions) |
| Historical data acquisition | 🔴 RED (none ingested, no vendor) |
| AWS backtest infrastructure | 🔴 RED (not provisioned) |
| Model training pipeline / registry / tracking | 🔴 RED (not designed) |
| Promotion readiness | 🔴 RED (nothing promotable — correctly gated) |

---

## SECTION 2 — What Is Already Implemented

### Implemented (built + tested)
- **Data layer:** `s3_data_catalog.py` (trust tiers, zones, paths), `data_loader.py` (local/S3, CSV/Parquet, IST, partitioned), `data_quality.py` (12 checks). Validator script + tests (12).
- **Run registry & checkpoints:** `run_registry.py`, `checkpoint_manager.py` (metadata-only, live-table guard, idempotent run_id, resume). Tests (9).
- **Replay engine:** `replay_engine.py` (chronological, no-lookahead, sharding, checkpoint, reuses `Backtester`). Tests (8).
- **Strategy adapters:** `strategy_adapter.py` (6 production strategies, production-compatible signal). Tests (8).
- **Execution simulator:** `execution_simulator.py` (NSE cost stack, slippage tiers, signed P&L, partial fills, net-edge gate). Tests (9).
- **TEE/MIS:** `tee_simulator.py`, `mis_simulator.py` (old-vs-new R-based exits, MIS square-off, MFE/MAE/capture/giveback). Tests (11).
- **Metrics & reports:** `metrics_engine.py`, `report_writer.py` (full catalog, gate mapping, local+S3, failure reports). Tests (7).
- **Walk-forward:** `walk_forward.py` (IS/OOS folds, stability/overfit/eligibility). Tests (6).
- **Model dataset:** `model_dataset_builder.py` (leakage-free, time-split, trust-gated). Tests (6).
- **GenAI layer:** `genai/{guardrails,bedrock_client,prompts,athena_queries,analyst}.py` (advisory, secret/forbidden-action guardrails, citations). Tests (5).
- **Ingestion tooling:** `ingestion.py` + 2 CLIs (raw-preserve, checksums, trust routing, quarantine block). Tests (5).
- **Operating model:** `.claude/{commands(13),agents(9),skills(7)}`, CLAUDE.md protocol + governance invariant.

### Partially Implemented
- **Data lake:** contract + loader + quality + ingestion tooling exist; **the lake holds no real data** (only synthetic self-test artifacts).
- **GenAI:** code + guardrails done; **Bedrock KB, Step Functions, Lambda, Glue/Athena not provisioned** (manifest + query templates only).
- **Run-output cataloging:** Parquet outputs written; **no Glue catalog / Athena workgroup** to query them.
- **Paper validation:** monitor + session report exist; **1 of 5 required valid sessions** (Session 12).

### Planned (not built)
- **AWS infra:** Terraform `environments/backtest/`, `qe-bt-*` DynamoDB tables, `quantembrace-backtest-*` buckets, `backtest-worker` ASG, CloudWatch/SNS for the lab — **none in Terraform** (only `dev/staging/prod` exist).
- **Model training pipeline, model registry, experiment tracking.**
- **CI for the lab** (`tests/backtest/` not wired into `.github/workflows/ci.yml` paths explicitly).
- **Real historical data acquisition + vendor contract.**

### Assumptions (stated but not verified)
- That a licensed intraday vendor (TrueData/GlobalDataFeeds) or NSE Data Shop will be procured (none selected).
- That NSE Bhavcopy daily is sufficient as the free backbone (plausible but unverified against the 15-yr requirement, incl. delisted coverage).
- That Bedrock is available in `ap-south-1` for the chosen model (not verified).
- That the execution simulator's cost constants match current NSE/Zerodha schedules (constants are from `IndianCostModel`; not re-verified against 2026 rates).

---

## SECTION 3 — Architecture Verification

| Component | Status | Notes |
|---|---|---|
| Existing trading architecture (6-layer, EC2 ARM64, MSK, DynamoDB, S3) | **VERIFIED** | Mature, ADR-backed (ADR-009/010/011/022), documented in `system_design.md` / `docs/06`. Reused, not changed. |
| Backtesting engine reuse (`backtester.py`) | **VERIFIED** | `replay_engine.run_with_backtester` drives the existing engine; `lookahead_violations==0` end-to-end test passes. |
| Backtesting lab module boundaries | **VERIFIED** | Clean separation: data → registry → replay → adapters → execution → TEE/MIS → metrics → walk-forward → dataset → GenAI. No layer mixing. |
| Live-table isolation | **VERIFIED** | `assert_backtest_table` rejects live/paper tables in registry + checkpoint; tests confirm. |
| AWS backtest infrastructure (Terraform) | **MISSING** | No `environments/backtest/`, no `qe-bt-*` resources, no worker ASG. Design-only. |
| Glue/Athena catalog for outputs | **MISSING** | Query templates exist; no catalog/workgroup. |
| Bedrock KB / Step Functions / EventBridge | **MISSING** | Manifest + design only. |
| Service-boundary contradiction (bucket naming) | **PARTIAL** | `quantembrace-backtest-data`/`-results` (steering/contract) vs `quantembrace-backtests` (model-dataset Phase 10). Must reconcile before infra. |

**Verdict:** the *logical* architecture is verified and internally consistent except the bucket-name contradiction; the *physical* AWS architecture is unbuilt.

---

## SECTION 4 — Historical Data Verification

| Requirement | Plan | Sufficiency |
|---|---|---|
| 10–15 yr daily OHLCV | NSE Bhavcopy (free, official) | **Adequate backbone** — daily depth + delisted coverage available; needs verification of full 15-yr completeness. |
| 1-minute candles (10–15 yr) | Licensed vendor (TrueData/GFDL) | **Not procured.** Deep 15-yr 1-min is the expensive, scarce piece; vendors often cap intraday history (~5–10 yr). Real gap. |
| Corporate actions | `reference/corporate_actions/`, read-time `adj_factor` | **Designed, no data.** Source not selected (NSE/vendor). |
| Delisted symbols (survivorship) | Instrument master incl. delisted (Bhavcopy historical names) | **Designed, no data.** |
| Index constituent history (point-in-time) | `reference/index_membership/{index}/{date}` | **Designed, no data; sourcing hard.** Point-in-time NIFTY membership usually requires a vendor; free sources are LOW-trust. |

**Sources assessment:**
- **NSE Bhavcopy / exchange archives** — coverage: HIGH for daily EOD incl. delisted; reliability: HIGH (official); cost: free. **Recommended backbone.**
- **NSE Data Shop** — coverage: official intraday/historical packs; reliability: HIGH; cost: paid (per-pack). Viable for intraday + corp actions if licensing fits.
- **TrueData / GlobalDataFeeds** — coverage: intraday (depth varies); reliability: HIGH (licensed); cost: subscription (₹/$ thousands/yr). Needed for 1-min.
- **GitHub/Kaggle/free** — LOW trust → quarantine only; never for training/eligibility (enforced in code).

**Bottom line:** the **sourcing plan is sound but unexecuted**, and **deep 15-yr 1-minute history is the single biggest data risk** (availability + cost). Daily-only validation is achievable now for free; intraday strategies (scalp/ORB/VWAP/preclose) cannot be validated without a paid feed.

---

## SECTION 5 — Backtesting Design Review

| Aspect | Assessment |
|---|---|
| Replay engine | ✅ Strict next-bar, chronological, monotonic-asserted; reuses production engine. |
| Position sizing | ✅ Risk-based `position_size(nav, risk_pct, entry, stop)` in execution sim. |
| Commission modelling | ✅ NSE statutory stack (STT/exchange/SEBI/stamp/GST + brokerage), `cost_model_version` stamped. ⚠️ constants not re-verified vs 2026 schedules. |
| Slippage modelling | ✅ tiered bps (liquid/mid/illiquid) + half-spread, baked into fill price. ⚠️ no per-symbol ADV-driven slippage from real liquidity (volume_based declared, not fully implemented). |
| Corporate-action adjustment | ✅ Designed: unadjusted store + read-time `adj_factor`. ⚠️ unverified on real data. |
| Delisting handling | ✅ Designed (survivorship via delisted master). ⚠️ unverified. |

**Bias / leakage audit:**
- **Look-ahead bias:** ✅ strongly controlled — next-bar fills, `lookahead_violations==0` gate, point-in-time features, read-time corp-action adjustment, as-of universe. Tested.
- **Survivorship bias:** ✅ design includes delisted symbols + point-in-time membership; ⚠️ depends on real data actually containing delisted names (unverified).
- **Data-leakage (modelling):** ✅ dataset builder separates point-in-time features from future-only labels, time-split + embargo, asserted disjoint.
- **Hidden assumptions / missing realism:** ⚠️ fills assume full size at the stop/target level (gap-handling exists, but no partial-fill-by-liquidity in the Backtester path); ⚠️ no market-impact model for large orders beyond the bar-volume cap; ⚠️ intraday microstructure (queue position, auction prints) not modelled (acceptable for bar backtests, but a known limitation).

**Verdict:** realism and anti-leakage are **above average for a personal platform** and tested; the gaps are real-data-dependent verification and finer liquidity/impact modelling.

---

## SECTION 6 — Walk-Forward Validation Review

| Window | Design | Assessment |
|---|---|---|
| Train | 12 / 24 / 36 months | ✅ presets; rolling + anchored. |
| Validation (OOS) | 3 / 6 / 12 months | ✅ immediately follows train; half-open, no overlap. |
| Test | (combined OOS track) | ✅ OOS aggregation across folds. |

**Overfitting detection:** ✅ sufficient in principle — IS→OOS degradation, parameter-stability score, win-consistency, OOS gate mapping. ✅ no-leakage asserted (params selected from train only).

**Promotion criteria:** ✅ eligibility verdict is advisory-only and never promotes (`ELIGIBLE_FOR_PAPER_PRIORITIZATION` is the ceiling); promotion still requires ≥5 valid paper sessions + human sign-off.

**Critical improvement (only):** the parameter-optimization step is a **grid search supplied by the caller**; there is no built-in guard against *researcher* multiple-testing across many studies (p-hacking across strategies/params). Recommend (when real data lands) tracking the number of configurations tested and applying a deflation/penalty — **not now, not a blocker.**

---

## SECTION 7 — AI / ML Review

| Component | Classification | Notes |
|---|---|---|
| Feature store (offline/backtest) | **Needs Improvement** | Reuses the live `FeatureReader` definitions; **no dedicated offline feature store** — features are passed into the dataset builder, not served from a versioned store. Risk of train/serve skew if definitions drift. |
| Model dataset generation | **Ready** | Leakage-free, time-split, trust-gated, versioned manifest. Tested. |
| Model registry | **Not Yet Designed** | `ai_engine` loads joblib from S3, but there is no registry/versioning workflow for backtest-trained models. |
| Experiment tracking | **Not Yet Designed** | No MLflow/SageMaker/equivalent. Run registry tracks *backtests*, not *training experiments*. |
| Training pipeline | **Not Yet Designed** | The lab *generates datasets only* (by design); training/eval/promotion is explicitly out of scope and undesigned. |

**Verdict:** ML readiness is **dataset-generation-ready but training-MLOps-undesigned**. This is acceptable per the steering doc (lab is advisory, hands off datasets), but Section 1's "model training" scope is **not addressed** — a deliberate gap to acknowledge, not necessarily fill now.

---

## SECTION 8 — AWS Architecture Review

Following the existing architecture (EC2 ARM64, no Fargate per ADR-009; on-demand serverless for GenAI only):

| Service | Role in lab | Status / note |
|---|---|---|
| S3 | raw/quarantine/lake + results + datasets | designed; lifecycle defined; **not provisioned** |
| Glue | catalog over Parquet outputs | **not provisioned** |
| Athena | query templates over outputs | templates only; **no workgroup** |
| Batch | — | not used; **EC2 ARM64 ASG batch chosen instead** (consistent with arch) |
| Step Functions | GenAI orchestration (on-demand) | design only |
| Lambda | GenAI glue / context build (request-response) | design only; ✅ not used for streaming/polling (cost-rule compliant) |
| Fargate | **excluded** (ADR-009) | ✅ correctly not reintroduced |
| EC2 (ARM64, scale-from-0) | heavy backtest compute | design only; **On-Demand assumed — Spot not specified** |

**Cost optimization:** ✅ scale-from-0, S3 Parquet, on-demand serverless, ARM64 Graviton. ⚠️ **EC2 Spot is the obvious unused lever** for batch backtest workers — they are interruption-tolerant via checkpoint/resume, so Spot could cut compute ~60–70%. Recommend Spot for the `backtest-worker` ASG (a purchase option within the existing ASG design, not a redesign).

**Reliability:** ⚠️ registry uses **read-modify-write** updates (not atomic `UpdateExpression`); the checkpoint is a **single item per run**. Fine for one operator/few workers; **a race risk under fleet-scale parallelism**. PITR on `qe-bt-runs` mitigates audit loss, not concurrency.

**Scalability:** ✅ S3 + on-demand DynamoDB + horizontally-sharded workers scale well. ⚠️ single-item checkpoint contention at very high fan-out (mitigation: per-shard items, already noted as a future variant).

**Operational complexity:** 🟡 moderate — a *second* environment, KB ingestion, Glue/Athena, Step Functions add surface area. Justified for the research value, but it is net-new ops to run and monitor.

**Monthly cost estimates (AWS only; licensed data is separate and likely dominant):**

| Scale | Profile | Est. AWS/month |
|---|---|---|
| Small | daily data, occasional backtests, a few worker-hours, light Bedrock | **$10–35** |
| Medium | multi-year daily + partial intraday, weekly walk-forward, Glue/Athena, Bedrock reports | **$80–250** |
| Full 15-yr | full intraday Parquet lake (multi-TB), frequent large batch (Spot), KB+Step Functions+Athena | **$300–900 active / ~$50–150 idle** |

> ⚠️ **Licensed intraday data cost is external and likely the largest line item** (vendor subscription, can be $1k–10k+/yr). Budget it separately; it is unaddressed today.

---

## SECTION 9 — GenAI Review

| Allowed use | Present? |
|---|---|
| Research / analysis | ✅ strategy/TEE analysis |
| Reporting | ✅ run report summarization |
| Strategy explanations | ✅ |
| RAG over docs/results | ✅ (KB design + Athena grounding) |

| Forbidden use | Enforced? |
|---|---|
| Live trading decisions | ✅ no action channel; advisory-only; tested |
| Risk overrides | ✅ governance verdict computed **in code**; LLM only explains; `recommend_promotion` always False |
| Order generation | ✅ forbidden-action guardrail blocks request + response; tested |

**Violations found: NONE.** Additional verified controls: secret scan/redact before any prompt; KB indexes backtest artifacts only (no live/paper data, no PII); on-demand only (no streaming/SQS). The new CLAUDE.md governance invariant ("GenAI can explain, cannot trade") is consistent with the implementation. 🟢 **GREEN.**

---

## SECTION 10 — Gap Analysis

| Area | Gap | Severity | Recommendation |
|---|---|---|---|
| Historical data | No real licensed/official NSE data ingested | **CRITICAL** | Select source (Bhavcopy daily + NSE Data Shop/vendor intraday), procure license, ingest via existing tooling |
| AWS infra | No `backtest` Terraform env / `qe-bt-*` tables / worker ASG | **CRITICAL** | Build `environments/backtest/` reusing existing modules; `terraform plan` must show no live diff |
| Bucket naming | `quantembrace-backtests` vs `quantembrace-backtest-*` contradiction | **HIGH** | Reconcile to the steering canon before any infra is created |
| Registry concurrency | Read-modify-write updates; single checkpoint item | **HIGH** | Use conditional `UpdateExpression` + per-shard checkpoint items before multi-worker fleet runs |
| ML training/registry/tracking | Not designed (Section 1 scope) | **HIGH** | Decide scope: keep lab dataset-only, or design a separate MLOps track (registry + experiment tracking) |
| Index membership / corp-action data | Sourcing hard; not procured | **HIGH** | Identify a point-in-time membership + corp-action source (vendor); validate before survivorship-sensitive runs |
| Cost constants | NSE/Zerodha fee schedule not re-verified for 2026 | **MEDIUM** | Verify `IndianCostModel` constants against current schedules; bump `cost_model_version` |
| EC2 Spot | On-Demand assumed for batch workers | **MEDIUM** | Use Spot for `backtest-worker` ASG (checkpoint-safe) — major cost saving |
| Glue/Athena | Templates only; no catalog | **MEDIUM** | Provision Glue catalog + Athena workgroup when outputs accumulate |
| CI coverage | `tests/backtest/` not explicitly gated in CI | **MEDIUM** | Add `tests/backtest/**` + `services/backtesting/**` to `ci.yml` paths |
| Slippage realism | No ADV/market-impact model | **LOW** | Add volume-based slippage from real liquidity once intraday data exists |
| Licensed-data budget | Not estimated/owned | **MEDIUM** | Add data-license line to the project budget |

---

## SECTION 11 — Risk Register

| # | Category | Risk | Likelihood | Impact | Rank |
|---|---|---|---|---|---|
| R1 | Data | No real data → zero authoritative results; analysis paralysis | High | High | **CRITICAL** |
| R2 | Data | Deep 15-yr 1-min history unavailable/too costly | High | High | **CRITICAL** |
| R3 | Data | Survivorship/corp-action data incomplete → biased results | Medium | High | **HIGH** |
| R4 | AWS | Infra never provisioned → lab stays a prototype | Medium | High | **HIGH** |
| R5 | AWS | Registry RMW race corrupts run state at fleet scale | Medium | Medium | **MEDIUM** |
| R6 | Cost | Licensed data + active full-scale compute exceeds budget | Medium | High | **HIGH** |
| R7 | Cost | On-Demand (not Spot) workers inflate compute cost | Medium | Medium | **MEDIUM** |
| R8 | Model | Train/serve feature skew (no offline feature store) | Medium | High | **HIGH** |
| R9 | Model | Researcher multiple-testing / overfit across many studies | Medium | High | **HIGH** |
| R10 | Model | Synthetic results misread as real (governance/comms failure) | Medium | High | **HIGH** |
| R11 | Operational | Second env + KB/Glue/Step Functions increases ops load | Medium | Medium | **MEDIUM** |
| R12 | Operational | Bucket-name contradiction causes mis-routed data | Medium | Medium | **MEDIUM** |
| R13 | Security | Secrets/PII leak into prompts or KB | Low | High | **MEDIUM** (mitigated: guardrails + redaction tested) |
| R14 | Security | Quarantine (LOW-trust) data promoted to training | Low | High | **MEDIUM** (mitigated: trust gate enforced + tested) |
| R15 | Governance | GenAI/backtest used to justify auto-promotion | Low | Critical | **HIGH** (mitigated: invariant in CLAUDE.md + advisory-only code) |

Top mitigations already in place: governance invariant, advisory-only GenAI, trust quarantine, no-lookahead gate, live-table guard. Top *unmitigated*: R1/R2/R4/R6 (data + infra + budget).

---

## SECTION 12 — Phase Validation (backtesting lab phases)

| Phase | Objective | Deliverables | Missing | Readiness |
|---|---|---|---|---|
| 0 Discovery | inventory + gaps | `aws-phase0-discovery-report.md` | — | **100%** |
| 1 Specification | what to build | `aws-backtesting-specification.md` (20 §) | — | **100%** |
| 2 Data lake + validation | Parquet lake + DQ | loader, DQ, validator, contract | **real data** | **70%** (tooling) |
| 3 Run registry + checkpoints | durable runs + resume | registry, checkpoints + tests | atomic updates; infra tables | **80%** |
| 4 Replay engine | candle replay, no-lookahead | `replay_engine.py` + tests | scale runs on real data | **90%** |
| 5 Strategy adapters | wire 6 strategies | adapters + tests; yaml fixed | intraday data to exercise | **85%** |
| 6 Execution simulator | costs/slippage realism | `execution_simulator.py` + e2e | fee-constant re-verify; ADV slippage | **85%** |
| 7 TEE/MIS | old-vs-new + square-off | simulators + compare + tests | real-trade comparison | **90%** |
| 8 Metrics & reports | catalog + report + gates | metrics/report + tests | Glue/Athena cataloging | **90%** |
| 9 Walk-forward | IS/OOS + overfit | harness + runner + tests | multiple-testing guard; real data | **85%** |
| 10 Model dataset | leakage-free datasets | builder + tests | offline feature store | **80%** |
| 11 GenAI layer | advisory analysis | genai pkg + guardrails + tests | Bedrock/KB/Step Functions provisioned | **70%** (code) |
| 12 Full run | end-to-end orchestration | driver + synthetic dry-run | real data + infra | **60%** |
| Ingest | licensed data → S3 | ingestion tooling + tests | **vendor data + license** | **50%** (tooling) |
| Decision | eligibility verdicts | decision report | real evidence | **100%** (process) |

**Aggregate:** code/process ~**85%**; execution/production ~**20%** (gated on data + infra).

---

## SECTION 13 — Go / No-Go Assessment

### Status: **GO WITH CONDITIONS**

**Why not NO-GO:** the engineering is sound, follows the existing architecture, is well-tested (86 tests), and the governance is correct. There are no architectural defects that block proceeding — only unbuilt infra and unacquired data.

**Why not GO (unconditional):** the two hardest dependencies — **licensed/official data** and **provisioned AWS infra** — are not done, and the only outputs to date are synthetic. Proceeding as if results exist would be dangerous.

**Conditions to proceed to implementation (all required):**
1. Reconcile the bucket-naming contradiction (single canon).
2. Provision `environments/backtest/` via Terraform, `plan` showing **no diff to live `prod/staging`**.
3. Select + license a data source; ingest at least the **Bhavcopy daily backbone** and pass data quality (real `data_snapshot_id`).
4. Harden the registry (atomic conditional updates, per-shard checkpoints) before multi-worker fleet runs.
5. Decide ML-training scope (dataset-only vs full MLOps track) explicitly.
6. Keep all governance invariants (no auto-promote, no live, GenAI advisory) — unchanged.

---

## SECTION 14 — Recommended Next Actions (priority order)

| # | Priority | Owner | Task | Dependencies |
|---|---|---|---|---|
| 1 | P0 | Architect | Reconcile bucket naming to one canon across all docs/code | — |
| 2 | P0 | Quant/Operator | Select data source(s); confirm license terms | — |
| 3 | P0 | Operator | Procure NSE Bhavcopy daily (free) + plan intraday vendor | #2 |
| 4 | P0 | AWS Architect | Write `environments/backtest/` Terraform (buckets, `qe-bt-*` tables, worker ASG, CW/SNS) reusing modules | #1 |
| 5 | P0 | AWS Architect | `terraform plan` — verify no live diff; operator apply | #4 |
| 6 | P0 | Data Eng | Ingest Bhavcopy daily via `ingest_nse_history_to_s3.py`; checksum manifest | #3,#5 |
| 7 | P0 | Data Eng | Normalize → Parquet lake; pass data quality; publish `data_snapshot_id` | #6 |
| 8 | P1 | Backend | Harden registry (conditional `UpdateExpression`, per-shard checkpoints) | #5 |
| 9 | P1 | Quant | Run real **daily** backtest (momentum + trend_15m) end-to-end | #7,#8 |
| 10 | P1 | Quant | Real walk-forward on daily; record stability/overfit | #9 |
| 11 | P1 | MLOps | Decide ML scope; if full: design model registry + experiment tracking | #2 |
| 12 | P1 | Data Eng | Source point-in-time index membership + corp actions | #3 |
| 13 | P2 | AWS Architect | Switch `backtest-worker` ASG to Spot (checkpoint-safe) | #5,#8 |
| 14 | P2 | Quant | Procure + ingest licensed 1-min intraday; validate | #3 |
| 15 | P2 | Quant | Real backtests for intraday strategies (ORB/VWAP/scalp/preclose) | #14 |
| 16 | P2 | MLOps | Build offline feature store (parity with live `FeatureReader`) | #11 |
| 17 | P2 | Data Eng | Provision Glue catalog + Athena workgroup over outputs | #5 |
| 18 | P2 | MLOps | Provision Bedrock KB + Step Functions for GenAI reports | #5 |
| 19 | P3 | Risk | Verify `IndianCostModel` fee constants vs 2026 schedules; bump version | — |
| 20 | P3 | DevOps | Gate `tests/backtest/**` in CI; add multiple-testing guard to walk-forward | — |

---

## SECTION 15 — Final Verdict

| Dimension | Score |
|---|---|
| Architecture | **8 / 10** |
| Reliability | **6 / 10** |
| Cost Efficiency | **8 / 10** |
| Backtesting Readiness | **7 / 10** |
| ML Readiness | **4 / 10** |
| Production Readiness | **3 / 10** |

**What is excellent:** disciplined reuse of the existing architecture (no needless redesign); rigorous, *tested* no-lookahead and mandatory-cost realism; strong, enforced safety governance (advisory-only, secret/forbidden-action guardrails, trust quarantine, live-table isolation); honest, phase-gated documentation that never overstates status; the governance invariant codified in CLAUDE.md.

**What is dangerous:** mistaking the synthetic dry-run for real evidence (clearly labeled, but a standing communication risk); proceeding to "results" without real data; running multi-worker fleets on the current read-modify-write registry; an unbudgeted licensed-data cost that could dominate; an undesigned ML-training track that Section-1 scope implies but no artifact delivers.

**What must be done before implementation starts:** (1) reconcile bucket naming; (2) provision the `backtest` AWS environment via Terraform with no live diff; (3) procure + ingest at least the official daily data backbone and pass data quality; (4) harden the registry/checkpoint for concurrency; (5) explicitly decide the ML-training scope. Until those are done, the lab remains an **excellent, well-tested machine with no fuel** — and **no strategy is promotable beyond paper**, which the current governance correctly enforces.

---

*Audit only. No code modified, no architecture redesigned, no implementation performed.*
