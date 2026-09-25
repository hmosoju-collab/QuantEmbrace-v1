# QuantEmbrace — Data Platform North Star (20-Year Architecture)

> **Status: VISION / DESIGN — `[PLANNED — not yet implemented]` except where it reconciles existing code.**
> This document describes the *end-state* research-data platform and the additive path from today's stack to it. It does **not** describe currently-shipped behavior unless a component is explicitly tagged **[EXISTS]** or **[DESIGNED]**. It changes no trading behavior. The governance wall (*backtesting recommends, humans promote; GenAI explains, GenAI cannot trade*) is preserved verbatim.
>
> Version: NS-1 · Author: Chief Architect (with Hari) · Created: 2026-06-15
> Companion canon: `architecture/system_design.md` · `docs/backtesting/aws-data-lake-contract.md` · `docs/backtesting/aws-backtesting-specification.md` · `memory/decisions.md` (ADR-008 … ADR-031)
> Scope note: this is the **research & data platform** vision. The **live/paper execution platform** (EC2 ARM64 ASGs + MSK Serverless, ADR-009/010/011) is already production-grade and is treated here as a *peer system behind a hard isolation wall*, not something to rebuild.

---

## Reading guide

| If you want… | Read |
|---|---|
| The 5-minute version | §0 Executive Summary |
| The shape of the end-state | §1 North Star + diagrams |
| The single most important storage decision | §2 Data Lake (Iceberg-on-S3) |
| Why your backtests won't lie to you | §7 Backtesting + §11 Governance (overfitting controls) |
| What to build first | §14 Roadmap (Year 1 = "minimum viable lakehouse") |
| What I'd change about the current direction | §16 "If I were starting over" |
| Where this contradicts existing docs and how it's resolved | §17 Reconciliation |

---

## §0 — Executive Summary

**The thesis.** For a quant platform, the durable asset is not the strategy code, the models, or even the execution stack — those get rewritten every few years. The durable asset is a **clean, immutable, point-in-time-correct, lineage-tracked historical record** and the **discipline that keeps research honest**. Everything else is replaceable; the data and the provenance are not. So the 20-year design optimizes for exactly two things: *reproducibility* (any experiment from 2026 reruns bit-for-bit-stable in 2046) and *research integrity* (you cannot accidentally fool yourself into trading a spurious alpha). Performance, scale, and features are subordinate to those two.

**The shape.** An **open lakehouse on S3** — Apache Iceberg tables, Parquet files, AWS Glue Data Catalog — with a strict five-zone progression (Raw → Curated → Feature → Research → Model, plus an Archive tier). Around it sit four metadata services that all share **one pattern you already use**: the DynamoDB *deterministic-id + conditional-write claim* registry (today it backs `orders` and `qe-bt-runs`; tomorrow it also backs datasets, features, experiments, and models). Compute scales from a laptop with DuckDB, to scale-from-zero Graviton EC2/Batch, to Ray — **never** through Lambda-for-compute or a vendor warehouse. A hard, *architectural* (not merely policy) wall separates the research platform from live/paper trading.

**Why these calls.** The non-negotiables in your own principles force most decisions: *"no vendor lock-in beyond S3 object storage,"* *"favor open formats,"* *"additive evolution over rewrites,"* and the cost discipline in `CLAUDE.md` (no Lambda for streaming/compute, prefer EC2/Batch, S3 for history, DynamoDB only for low-latency KV). Those rules eliminate Snowflake, Databricks-as-platform, Redshift, Kinesis, and managed feature stores as *foundations* (they're fine as optional accelerators later). What survives is the open S3 lakehouse, which is also — not coincidentally — what Netflix, and increasingly the named institutions, converged on.

**The single biggest risk, stated up front.** It is **not** infrastructure. A solo founder with a fast backtester will run thousands of experiments and **will** discover alphas that are pure noise. Renaissance/HRT/Two Sigma spend enormous effort on *multiple-testing control, point-in-time correctness, and capacity/cost realism* precisely because the default outcome of unconstrained research is self-deception. This platform therefore treats **overfitting control as a first-class, load-bearing feature** (§7, §11): pre-registered hypotheses, a tracked family-wise testing budget, Deflated Sharpe / PBO / walk-forward gates, and the existing advisory wall. If you build only one thing beyond the lake, build this.

**What you already have (and should not rebuild).** A production EC2/MSK/DynamoDB/S3 trading stack (ADR-009/010/011); a shadow-mode **Alpha Engine** with forecast store, cross-sectional ranker, cost model, outcome labeler, EOD rollup, per-model accuracy (ADR-031); a **Data Lake Contract** already specifying ISIN-keyed Parquet, trust tiers + quarantine, snapshot manifests, read-time corp-action adjustment, and no-lookahead rules; a backtest **run registry + checkpoint** design (`qe-bt-runs`/`qe-bt-checkpoints`), walk-forward harness, execution simulator with Indian statutory costs, and a model-dataset generator. **The North Star is ~70% a promotion and unification of things you've already designed, not a greenfield.** This document's job is to name the end-state they're converging toward, fix the known contradictions, and sequence the gaps.

**The one-line North Star:** *An open, immutable, point-in-time S3 lakehouse feeding a reproducible, overfitting-resistant research factory — additively evolvable for 20 years, operable by one person today, and walled off from live capital by construction.*

---

## §1 — North Star Architecture

### 1.1 Design tenets (your principles, made executable)

| Your principle | How the architecture enforces it (not just honors it) |
|---|---|
| Data is the moat | Curated lake + reference data are the only blessed surface; everything downstream is derived and disposable. Spend the reliability budget here. |
| Raw data is immutable | Raw zone is **WORM**: S3 Object Lock (compliance mode) + versioning; no process has `s3:DeleteObject` on `raw/`. Corrections are *new* drops, never edits. |
| Every transformation reproducible | Iceberg snapshot IDs + pinned code (git SHA) + pinned container digest + pinned config hash → a 5-tuple that *names* any derived artifact. |
| Research results lineage-tracked | Every dataset/experiment/model row stores its input snapshot IDs + parent IDs → a queryable DAG from any result back to raw bytes. |
| 2026 reproducible in 2046 | "Reproduction = re-run the pinned container against the pinned Iceberg snapshot." Containers archived in ECR **and** exported to S3; determinism boundaries documented (§15). |
| No lock-in beyond S3 | Open formats (Parquet/Iceberg/Arrow) + portable catalog (Iceberg REST-compatible) + abstraction at every managed-service boundary (`BarSource`, `LLMProvider`, catalog client). |
| Scale horizontally | Stateless, shard-by-(ISIN, year) compute; storage decoupled from compute; no single vertical bottleneck. |
| Favor open formats | Parquet (storage), Iceberg (table), Arrow (in-memory), Avro/JSON-schema (events) — all engine-neutral. |
| Additive over rewrites | New zones/tables/columns are added; old ones are deprecated through a lifecycle, never hard-cut. Iceberg schema evolution makes this physically true. |
| Design for 20 years | Bitemporal modeling (knowledge-time on every fact) and ISIN-as-key from row zero — the two things that are *agony* to retrofit. |

### 1.2 The end-state, in one diagram

```
                          EXTERNAL SOURCES
   NSE Bhavcopy │ Licensed intraday vendor │ Corp actions │ Index membership
   Fundamentals │ News/filings │ Econ calendar │ Options chain │ Alt data │ Broker WS
        │              │            │              │            │          │
        ▼              ▼            ▼              ▼            ▼          ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │  INGESTION PLANE   (pluggable connectors; EC2/Batch — never Lambda-compute)│
 │  • batch loaders (bhavcopy, CA, fundamentals)   • streaming (broker WS→MSK)│
 │  • schema validation • idempotent keys • DLQ/quarantine • observability    │
 └───────────────────────────────┬───────────────────────────────────────────┘
                                  ▼
 ┌──────────────────────────  S3 LAKEHOUSE  (open formats, Glue catalog) ──────┐
 │                                                                             │
 │  RAW (WORM, immutable)  ──►  CURATED (Iceberg)  ──►  FEATURE (Iceberg+DDB)  │
 │   original drops,            PIT OHLCV, reference,    offline (PIT-correct) │
 │   per-source, dated          corp-actions, universe   + online (low-latency)│
 │        │                          │                        │               │
 │        └──────────────►  ARCHIVE (Glacier IR/Deep) ◄───────┘               │
 │                                   ▲                                         │
 │   RESEARCH (experiments,          │           MODEL (registry +            │
 │   backtest runs, datasets) ───────┘           artifacts, versioned)        │
 └───────────────┬───────────────────────────────────┬─────────────────────────┘
                 │  Glue Data Catalog (Iceberg tables)│
                 ▼                                    ▼
 ┌──────────────────────────────┐     ┌──────────────────────────────────────┐
 │  QUERY/COMPUTE PLANE          │     │  METADATA PLANE (DynamoDB + S3 manifests)│
 │  • DuckDB (local, solo)       │     │  • Dataset Registry   • Run Registry  │
 │  • Athena (serverless SQL)    │     │  • Feature Registry   • Experiment Tracker│
 │  • EC2/Batch Graviton (sweeps)│     │  • Model Registry                     │
 │  • Ray / Spark (scale)        │     │  one pattern: deterministic-id + claim│
 │  • Trino (interactive @scale) │     └──────────────────────────────────────┘
 └───────────────┬──────────────┘
                 ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │  RESEARCH FACTORY                                                           │
 │  Backtester (reuse backtester.py) → Walk-forward → PBO/DSR/Monte-Carlo →    │
 │  Capacity/cost realism → Dataset builder → Alpha Engine (shadow) →          │
 │  Champion–Challenger → Drift/IC/Calibration → Explainability                │
 └───────────────────────────────┬───────────────────────────────────────────┘
                                  │   ADVISORY ONLY — recommendations + reports
                ══════════════════╪══════════════════  HARD ISOLATION WALL  ══
                                  ▼   (separate env, IAM, buckets, tables;
 ┌───────────────────────────────────────────────────────────────────────────┐
 │  LIVE / PAPER TRADING PLATFORM   [EXISTS — do not rebuild]                  │
 │  strategy_engine → ai_engine → risk_engine → execution_engine (ADR-031 flow)│
 │  EC2 ARM64 ASGs • MSK Serverless • DynamoDB live tables • a HUMAN promotes  │
 └───────────────────────────────────────────────────────────────────────────┘
```

### 1.3 Major components and why each exists

**Ingestion plane.** One pluggable connector interface (generalize the existing `BarSource`) lands every source into `raw/` unchanged, then a *separate* curation step validates and promotes to Iceberg. Batch sources (bhavcopy, corporate actions, fundamentals) run on scheduled Graviton EC2/Batch jobs; live ticks keep the **existing** broker-WebSocket→MSK path (ADR-010/011) — that is already correct and explicitly *not* Lambda. Separation of "land raw" from "curate" is what makes raw immutable and curation re-runnable.

**S3 lakehouse (five zones + archive).** The spine. Raw is WORM. Curated/Feature/Research/Model are Iceberg tables in Glue so they get snapshots, time-travel, schema evolution, and hidden partitioning *for free* — these are the exact primitives that make 20-year reproducibility physical rather than aspirational. Full design in §2.

**Metadata plane.** Four registries (dataset, run, feature, model) + one experiment tracker. They are the *index of record* and the *lineage graph*. Critically, they all reuse the one DynamoDB pattern you already run in production (deterministic ID from content, conditional-write claim for idempotency) — so there is **one** mental model, one failure mode, one backup story, not five. Artifacts live in S3; only pointers + metadata live in DynamoDB (your `CLAUDE.md` rule: "DynamoDB only for low-latency KV").

**Query/compute plane.** Storage is decoupled from compute, so you pick the cheapest engine per job: DuckDB on your laptop for solo research (reads Parquet/Iceberg directly — no cluster), Athena for serverless ad-hoc SQL, Graviton EC2/Batch for parallel backtests (your approved scale-from-zero worker ASG), Ray/Spark/Trino only when scale demands. Migration is painless because every engine reads the same Iceberg-on-Glue tables.

**Research factory.** The existing backtester, walk-forward, simulator, dataset builder, and Alpha Engine — wrapped in statistical-integrity gates (§7) and an experiment tracker (§9). This is where edge is found and, just as importantly, where false edge is *rejected*.

**The wall.** Research can produce a recommendation and a report. It cannot mutate a live table, call a broker, change capital, or flip a flag. This is enforced by *construction* — separate AWS environment, separate IAM roles with no live/paper/broker permissions, separate buckets (`quantembrace-backtest-*`) and table prefix (`qe-bt-`) — exactly as your backtesting steering already mandates. Policy can be argued with; an IAM boundary cannot.

### 1.4 Service boundaries (research plane)

| Component | Reads | Writes | Never touches |
|---|---|---|---|
| Connectors / ingestion | external sources | `raw/` (WORM), DLQ/quarantine | curated lake directly |
| Curation jobs | `raw/`, reference | Curated Iceberg, `_snapshots/`, DQ reports | raw (immutable), live tables |
| Dataset builder | Curated + Feature (as-of snapshot) | `datasets/`, Dataset Registry | live/paper anything |
| Feature materializer | Curated | Feature offline (Iceberg) + online (DynamoDB) | broker, orders |
| Backtest workers | Curated lake (snapshot-pinned) | `runs/`, Run Registry, Model datasets | broker, live tables, capital |
| Alpha Engine (shadow) | candle-cache, curated features | `alpha-forecasts`, `alpha.opportunities` (shadow topic) | `signals.*`, `orders.*`, kill-switch (ADR-031) |
| Experiment tracker | registries | experiment metadata + S3 reports | anything live |

> **Boundary invariant:** the research plane's IAM has `GetObject` on `*-backtest-data/*`, `PutObject` on `*-backtest-results/*`, and **no** Secrets Manager broker creds, **no** live/paper bucket or table access (per Data Lake Contract §10). This is the wall, expressed as permissions.

---

## §2 — Data Lake Design (the S3 Lakehouse)

This section supersedes-and-extends `aws-data-lake-contract.md`. The contract's substance (ISIN key, unadjusted prices + read-time `adj_factor`, trust tiers + quarantine, snapshot manifests, Hive partitioning, lifecycle) is **correct and retained**; the change is to put **Apache Iceberg over the curated layers** and to formalize five zones.

### 2.1 Zones and their contracts

| Zone | Format | Mutability | Catalog | Purpose | Lifecycle |
|---|---|---|---|---|---|
| **Raw** | Original (CSV/JSON/Parquet as delivered) + Parquet mirror | **Immutable / WORM** (Object Lock) | none (path-addressed) | the immutable system of record; the "20-year tape" | Standard→IA 30d→Glacier IR 365d→Deep Archive 3y; **never deleted** |
| **Curated** | **Iceberg** (Parquet data files) | Append + schema-evolve; corrections via new snapshot | Glue | PIT-correct OHLCV, reference, corp actions, universe | Standard→IA 30d→Glacier IR 365d (data files); snapshots/manifests retained |
| **Feature** | **Iceberg** (offline) + DynamoDB (online) | Versioned; immutable feature-set versions | Glue | research + live features, point-in-time joins | offline: IA/Glacier; online: DynamoDB TTL on stale keys |
| **Research** | Parquet/Iceberg + JSON manifests | Immutable per run/dataset | Glue (datasets) | backtest runs, datasets, walk-forward studies | **never auto-deleted** (audit/repro) |
| **Model** | Artifacts (pickle/ONNX/joblib) + Iceberg metadata | Immutable per version | Glue (metadata) | trained models + cards + lineage | retained; large artifacts → IA |
| **Archive** | Parquet/Iceberg | Immutable | Glue (optional) | cold history + expired snapshots' data files | Glacier IR / Deep Archive |

### 2.2 Canonical layout (unified — resolves the path conflict in §17)

```
s3://quantembrace-backtest-data/                 # DATA plane (read-only to workers)
  raw/                                           # WORM, Object Lock
    {source}/ingest_date={YYYY-MM-DD}/...        # bhavcopy, ca, fundamentals, news, ...
  quarantine/{source}/ingest_date={YYYY-MM-DD}/  # LOW-trust landing (never engine-readable)
  lake/                                          # CURATED — Iceberg tables (Glue: qe_lake.*)
    ohlcv/        market=NSE/segment=EQ|INDEX/symbol=…/interval=1m|5m|15m|1d/year=YYYY/
    reference/
      corporate_actions/  symbol_map/  instruments/  index_membership/  calendars/
  features/                                      # FEATURE offline — Iceberg (Glue: qe_feat.*)
    {feature_set}/version={semver}/market=…/interval=…/year=…/
  _snapshots/{data_snapshot_id}.json             # human-readable manifest mirror of Iceberg snapshot

s3://quantembrace-backtest-results/              # RESEARCH plane (workers write here)
  runs/{run_id}/{config.json,trades.parquet,metrics.json,equity_curve.parquet,labels.parquet,logs/,report.md}
  walkforward/{study_id}/folds/{fold_id}/ -> run_id, aggregate.json
  datasets/{dataset_id}/version={v}/{train,val,test}.parquet, schema.json, manifest.json
  experiments/{experiment_id}/...                # tracker artifacts (§9)

s3://quantembrace-models/                        # MODEL plane
  {model_name}/version={semver}/{artifact, model_card.md, lineage.json, calibration.json}

s3://quantembrace-archive/                       # ARCHIVE (Glacier tiers)
```

**Naming conventions (enforced in governance §11):** lowercase-kebab buckets prefixed `quantembrace-`; Hive `key=value` partitions only; Glue databases `qe_lake`, `qe_feat`, `qe_research`, `qe_model`; table names singular-noun (`ohlcv`, `corporate_action`); every ID is `{kind}_{date}_{contenthash[:8]}` (mirrors `bt_{date}_{config_hash[:8]}` you already use). ISIN is the only stable cross-time join key — **never** the ticker.

### 2.3 Partitioning, file size, compression

Retain the contract's scheme: Hive partitions `market / segment / symbol / interval / year`; Parquet + Snappy; ~128 MB target files; one writer per `(symbol, interval, year)` for idempotent writes; predicate pushdown on `timestamp`. **Add, via Iceberg:** *hidden partitioning* (queries don't hand-write partition predicates — Iceberg prunes from the `timestamp`), *partition evolution* (you can change the scheme in 2031 without rewriting 2016 data), and *sort order* on `(isin, timestamp)` for fast range scans. For tick data (when procured), partition additionally by `month` (not year) and consider `day` for the busiest symbols — tick volume is ~3 orders of magnitude above 1m bars.

> **Compression note:** Snappy for hot/curated (fast), **Zstd level ~9** for archive and tick history (≈30–40% smaller than Snappy, worth it on 20-year tick scale; pay the CPU once on write). Set per-zone, not globally.

### 2.4 Parquet vs Iceberg vs Delta — and why Iceberg

**Recommendation: raw layer = plain Parquet/files; every curated+ layer = Apache Iceberg.** Reasoning grounded in your principles:

| Criterion | Iceberg ✅ | Delta Lake | Hudi | Plain Parquet+Hive (today) |
|---|---|---|---|---|
| Engine neutrality (no lock-in) | **Broadest**: Athena v3, Trino, Spark, Flink, Snowflake, DuckDB, BigQuery all read it | Best tooling is Databricks-gravity (OSS exists but lags) | Spark-centric | Universal but **no table semantics** |
| AWS-native management | **S3 Tables + Glue Iceberg REST + Athena native** | via EMR/3rd-party | via EMR | Glue only as a dumb catalog |
| Time-travel / snapshots (repro!) | **First-class** (`AS OF snapshot/timestamp`) | Yes | Yes | **None** — fatal for your repro principle |
| Schema + **partition evolution** | **Yes, both** (additive, your principle) | Schema yes; partition evolution weak | Partial | Manual, painful rewrites |
| Hidden partitioning | **Yes** (no leaky partition predicates) | No | No | No |
| Maturity / governance | Apache TLP, broad vendor backing | Mature, Linux Foundation | Apache, narrower | n/a |
| 20-year bet risk | **Lowest** (most independent of any one vendor) | Tied to Databricks roadmap | Niche | High (you'd reinvent table semantics) |

Iceberg wins on the two axes you weighted highest — *lock-in avoidance* and *reproducibility* — and AWS made it the default-blessed table format (Athena, Glue, EMR, and S3 Tables all manage Iceberg natively now). Delta is excellent but its best ergonomics live inside Databricks, which you've (rightly, given cost discipline) excluded as a foundation. **Migration is additive:** Iceberg tables sit *on top of* your existing Parquet files; you register current `lake/ohlcv` data into an Iceberg table without moving bytes.

### 2.5 Glue Catalog vs Hive Metastore — Glue

**Recommendation: AWS Glue Data Catalog as the Iceberg catalog.** A self-managed Hive Metastore is a stateful service you'd have to run, patch, back up, and scale — exactly the "unnecessary service" your principles forbid. Glue is serverless, integrates with Athena/EMR/Trino/Spark, and now speaks the **Iceberg REST catalog** protocol. *Lock-in mitigation:* Iceberg's catalog layer is pluggable — if you ever need to leave Glue (e.g., multi-cloud, or a Nessie/Polaris "git-for-data" catalog with branching), the tables move because the metadata is open. So Glue is a *reversible* convenience, not a trap.

### 2.6 Athena vs Trino — both, in sequence

**Recommendation: Athena first; add Trino only at a measured cost crossover.** Athena is serverless ($/TB scanned, zero ops) — ideal for solo→small-team ad-hoc SQL and BI. Self-managed Trino (on EC2/EKS or EMR) has a *fixed* cluster cost but unlimited included queries and supports long-running/federated joins. The crossover: when sustained Athena spend exceeds a right-sized Trino cluster's monthly cost, or when you need interactive sub-second joins across many large tables, stand up Trino — pointed at the *same* Glue+Iceberg tables, so nothing else changes. **Do not** reach for Redshift or Snowflake: Redshift couples storage+compute and adds an ETL hop; Snowflake is superb but is the canonical violation of "no lock-in beyond S3" and carries cost you've explicitly disciplined against. Keep the warehouse *virtual* (query engines over the lake), not physical.

---

## §3 — Historical Data Platform (20+ years, point-in-time)

The lake stores bytes; the *historical platform* is the set of rules and tables that let you ask **"what would I have known, and what was tradeable, on date D?"** and get a survivorship-correct, leakage-free answer. This is the part most retail backtesters get wrong and the part institutions obsess over.

### 3.1 What's stored

| Dataset | Grain | Source tier | Notes |
|---|---|---|---|
| Daily bars (backbone) | `1d` OHLCV per ISIN | NSE Bhavcopy (**HIGH**, free, official) | 15+ yr incl. delisted — survivorship-safe backbone |
| Minute/5m/15m bars | intraday per ISIN | licensed vendor (**HIGH**) / Zerodha gap-fill | layered in once procured; 5m/15m may be `source=derived` from 1m |
| Tick data | per-trade / depth | vendor (**HIGH**) | largest volume; partition by month/day; Zstd |
| Corporate actions | event + effective date | NSE/vendor | splits/bonuses/dividends/face-value → `adj_factor` |
| Symbol map | ISIN ↔ symbol, effective ranges | NSE/vendor | ISIN is the stable key; handles renames/mergers |
| Instruments master | symbol incl. delisted | NSE | retains delisted for their active window |
| Index membership | constituent ISIN set per rebalance | NSE/vendor | point-in-time NIFTY 50/100/200/500 + sectors |
| Trading calendar | session days/holidays/halts | NSE | per-segment; crypto = 24/7 calendar (§ multi-asset) |
| Fundamentals | per-company, per-filing | vendor | **knowledge-time = filing/availability date**, not period-end |
| News / filings | timestamped docs | vendor/scrape | heavy quarantine; `available_at` mandatory |

### 3.2 The two retrofitting-is-agony decisions (do them now)

**(a) ISIN as the only stable key.** Already in your contract — keep it absolute. Tickers are reused and reassigned over 15 years; ISIN is not. Every join, universe reconstruction, and feature key is ISIN; the ticker is a *display attribute* resolved as-of.

**(b) Bitemporality — `available_at` on every fact.** This is the upgrade beyond the current contract. Store two times on facts that are *revised* or *announced* (fundamentals, corp actions, index changes, even some price corrections):

- **valid_time** — when the fact was true in the world (e.g., quarter-end 2025-03-31).
- **knowledge_time / `available_at`** — when *you could have known it* (e.g., results filed 2025-05-14 18:40 IST).

A point-in-time query for simulated date `D` selects facts with `available_at <= D` and the latest `valid_time <= D`. Without `available_at`, fundamentals leak the future (you'd "know" Q1 earnings on Mar 31 instead of mid-May) — the single most common silent alpha-inflater. Iceberg's snapshot/transaction-time helps, but it captures *when you ingested*, which is a proxy, not the truth; store `available_at` explicitly as data.

### 3.3 Point-in-time reconstruction, snapshots, replay

- **Reconstruction.** "Universe/price/feature as of D" = as-of index membership (constituents effective ≤ D) ∩ instruments active at D ∩ corp-action adjustment computed as-of D (read-time `adj_factor`) ∩ facts with `available_at ≤ D`. All ISIN-keyed.
- **Dataset snapshots.** A `data_snapshot_id` pins a *set of Iceberg snapshot IDs* (one per table) + manifest (sources, versions, checksums, trust levels). A backtest references exactly one snapshot → its inputs are frozen forever even as the lake grows. (Extends contract §6/§14.)
- **Replay.** The curated lake is the only surface the replay engine reads (contract §4). Replays are deterministic (fixed seeds, no wall-clock in logic, deterministic same-timestamp ordering — spec §11), so a snapshot + code + container reproduces metric-stable results.

### 3.4 Avoiding future data leakage (the checklist that earns trust)

This formalizes `no-lookahead-rules.md` into an enforced gate, asserted per run (`lookahead_violations == 0`):

1. **Next-bar execution only** — fill iff `fill_ts > generated_at`; same-bar fills are violations.
2. **Trailing-window indicators only** — no centered/forward windows; no `.shift(-k)`.
3. **As-of corporate actions** — future splits never adjust past bars (read-time `adj_factor` for date D).
4. **As-of index membership & instruments** — never today's constituents; absent membership ⇒ date excluded, logged (not back-filled).
5. **`available_at` honored** — fundamentals/news enter features only after their availability time + realistic lag.
6. **Survivorship** — delisted names present for their active window; universes built from PIT membership.
7. **Train/test separation** — walk-forward IS strictly precedes OOS; datasets use time-split **+ embargo** to kill autocorrelation bleed.
8. **Determinism** — so leakage can't hide behind randomness; CI re-runs a fixed seed and diffs metrics.

> **Hidden risk (challenged assumption):** "point-in-time correct" is not a state you reach; it's a property you *continuously re-verify*. A vendor backfilling a "corrected" historical value silently breaks PIT. Mitigation: raw is WORM (you keep the original), corrections arrive as *new* facts with their own `available_at`, and a nightly DQ job diffs vendor redeliveries against the WORM original and alarms on divergence.

---

## §4 — Market Data Ingestion

One connector contract, three temporal modes (historical / live / future-asset), landing everything in `raw/` first and curating second. **No Lambda for streaming or polling** (your hard rule) — live ingestion is the existing long-running EC2-ASG WebSocket→MSK path; batch ingestion is scheduled Graviton EC2/Batch.

### 4.1 Connector contract (generalize `BarSource`)

```python
class SourceConnector(Protocol):
    source_id: str            # 'bhavcopy', 'truedata', 'zerodha_ws', 'alpaca_ws', 'binance_ws', ...
    trust_level: TrustLevel   # HIGH | LOW (LOW ⇒ quarantine; never engine-readable until reconciled)
    asset_class: AssetClass   # NSE_EQ | NSE_FNO | US_EQ | CRYPTO | ALT
    def discover(window) -> list[Drop]            # what's available to pull
    def fetch(drop) -> RawArtifact                # land bytes unchanged → raw/ (or quarantine/)
    def validate(raw) -> ValidationReport         # DQ gate before curation
    def curate(raw) -> Iterable[CanonicalRow]     # → Iceberg curated, schema-checked, ISIN-keyed
```

Adding a source = implementing this once; the lake, DQ gate, registry, and backtester are unchanged. This is your "additive evolution" principle as code.

### 4.2 Historical batch (bhavcopy, corp actions, fundamentals)

Scheduled job (Graviton `t4g`/`c6g`, or AWS Batch for fan-out) on a cron: discover → fetch to `raw/{source}/ingest_date=…/` (WORM) → DQ gate → curate to Iceberg → publish `data_snapshot_id`. **Idempotent** by deterministic drop key `(source, business_date, content_hash)`; re-running a day is a no-op. Bhavcopy is the proven end-to-end backbone (already designed: `daily-bhavcopy-ingestion-design.md`).

### 4.3 Live streaming (keep what works)

The existing path is correct and stays: broker WebSocket (Zerodha Kite / Alpaca) on a dedicated EC2 ASG → MSK topics `ticks.nse` / `ticks.us` → DynamoDB `latest-prices` + `candle-cache`, S3 tick archive. **For research**, a thin tee writes the same ticks to `raw/ticks/` for the historical lake (so live becomes tomorrow's history) — *one-way*, research-side, never reading back into live. This is the only new wire and it respects the wall.

### 4.4 Reliability primitives (validation, retry, DLQ, idempotency, observability)

| Concern | Batch | Streaming (existing) |
|---|---|---|
| Validation | DQ gate (contract §6) blocks bad snapshots | schema check on produce; `data_quality` tag on candles |
| Retry | bounded exponential backoff on fetch; resumable | consumer offset commit after process; redrive |
| Dead-letter | failed records → `quarantine/_failed/` + report | MSK `signals.*.dlq` (exists, 7d) |
| Idempotency | deterministic drop key; conditional write | partition key = `instrument_id`; dedup on `(symbol, interval, ts)` |
| Observability | per-run report.md + CloudWatch `QuantEmbrace/Ingest` | existing CloudWatch alarms (WS gap >10s, DLQ depth, consumer lag) + monitoring_agent |

> **Future assets, designed-for now, built-later.** Crypto (24/7) breaks two NSE assumptions baked across the stack: the *trading calendar* (no sessions/square-off) and the *EOD rollup* (the Alpha Engine's 15:35-IST EOD task, `service.py:_eod_loop`). Make `Calendar` and `SessionModel` first-class, asset-class-parameterized abstractions *now*, even while only NSE exists, so crypto/US are additive later. F&O adds instrument keys (expiry/strike/lot/rollover) and, for options, Greeks/IV — design the instrument schema with these nullable fields from the start (the contract already flags `FNO later`).

---

## §5 — Dataset Registry (MLflow-grade, your DynamoDB pattern)

You already have the embryo: `qe-bt-datasets` + `_snapshots/*.json` manifests. Promote it to a first-class, immutable, lineage-tracked registry that is the *index of record* for every blessed dataset.

### 5.1 Record schema (DynamoDB `qe-bt-datasets`, artifacts in S3)

| Field | Purpose |
|---|---|
| `dataset_id` | `ds_{date}_{contenthash[:8]}` — deterministic from content |
| `dataset_version` | semver; immutable once published |
| `created_at` / `created_by` | provenance |
| `source_lineage` | input `data_snapshot_id`(s) + raw drop keys + parent dataset IDs (the DAG edges) |
| `git_sha` / `container_digest` | code + environment that built it |
| `content_hashes` | per-file + manifest hash (tamper-evident; verifies reproduction) |
| `universe_definition` | as-of index membership snapshot used |
| `corp_action_snapshot` | CA reference applied |
| `validation_report` | DQ result; `eligible_for_use` flag (LOW-trust ⇒ false until reconciled) |
| `trust_level` | HIGH/LOW (worst-of inputs) |
| `promotion_state` | `draft → validated → promoted → deprecated` (human gate on promote) |
| `schema` | `schema.json` pointer; class balance for label datasets |
| `splits` | train/val/test boundaries + embargo (leakage-proof) |

### 5.2 Behaviors

- **Snapshotting:** a published version freezes its input snapshot IDs + content hashes → reproducible forever.
- **Promotion:** `validated → promoted` is a **human-approved** state transition (governance §11), never automatic — mirrors your "backtesting recommends, humans promote."
- **Reproducibility:** rebuild from `(git_sha, container_digest, input snapshot IDs)`; recompute content hashes; assert equality. A dataset that won't reproduce is quarantined.
- **Idempotency:** deterministic `dataset_id` + conditional-write claim prevents duplicate builds (the exact `orders`/`qe-bt-runs` pattern).

> **Why not adopt MLflow's Model/Dataset registry wholesale?** You'd run MLflow's server + backend DB for dataset metadata you already model better in DynamoDB (idempotent, integrated, low-ops). Reserve MLflow for *ML experiment tracking* (§9), where it's genuinely strong, and keep datasets/runs in the registry pattern you operate well. One lineage graph, two storage homes, clear seam.

---

## §6 — Feature Store

### 6.1 Requirements and the leakage trap

A feature store serves the **same feature definition** to research (offline, point-in-time-correct, over history) and live (online, low-latency, current value). The whole reason it exists is **train/serve consistency** — if research computes RSI one way and live computes it another, your backtest is fiction. The hard requirement is the **point-in-time join**: for a label at time `t`, fetch each feature's value *as it was at `t`* (`available_at ≤ t`), never the latest. Your `no-lookahead-rules.md` + `available_at` (§3.2) make this enforceable.

### 6.2 Build vs buy — *build a thin layer now, adopt Feast's interface as the target, migrate when it hurts*

| Option | Verdict |
|---|---|
| **Home-grown thin layer (now)** | ✅ **Start here.** Reuse the existing `shared/features/feature_reader.py` set (RSI/EMA/VWAP/ATR/ADX/MACD/vol_ratio) — already shared by live *and* the model-dataset builder, so train/serve parity exists. Offline = Iceberg `features/` with PIT joins; online = DynamoDB (you run it; sub-ms KV). Zero new services — respects "no unnecessary microservices." |
| **Feast (later)** | The migration target. Open-source, **no lock-in beyond S3+DynamoDB which you already use**, native PIT joins, offline=Parquet/Iceberg + online=DynamoDB. Adopt when feature count/team growth makes hand-rolled materialization painful (≈ Year 2–3). Design your thin layer's API to match Feast's (entities, feature views, `get_historical_features`/`get_online_features`) so the swap is additive. |
| **Tecton** | ❌ Excellent but expensive SaaS; overkill for solo; lock-in. Revisit only at institutional scale with a team. |
| **SageMaker Feature Store** | ❌ AWS lock-in + cost for capability Feast gives openly. |

### 6.3 Design

```
Feature definition (git, versioned)  ──►  Materialization job (Graviton/Batch)
        │                                          │
        │ offline: PIT joins over curated lake     ├─► features/{set}/version=…  (Iceberg, research)
        │ online:  latest value per (ISIN,feature) └─► DynamoDB qe-features (TTL)  (live, low-latency)
        ▼
  Backtest/dataset builder reads OFFLINE (as-of)    Live ai/risk reads ONLINE (current)
```

- **Versioning:** feature definitions in git; materialized sets are immutable `version=` partitions; a dataset pins the feature version it used.
- **Leakage prevention:** offline reads are as-of (`available_at`); CI test asserts a feature computed offline at `t` equals the online value that *was* live at `t` (train/serve parity gate).
- **Materialization:** batch backfill for history; incremental for live (piggyback the existing candle pipeline). Online store is DynamoDB only (your KV rule), keyed `(isin, feature_set, version)`.

> **Don't over-build this.** For Year 1 you may not need a "store" at all — a PIT feature *function* over the lake + the existing online candle/feature path is enough. Add the store when you have >~30 features used across >2 consumers. Premature feature-store adoption is a classic solo-founder time sink.

---

## §7 — Backtesting Platform (QuantConnect-grade engine + Renaissance-grade integrity)

You already have the engine core (`services/strategy_engine/backtesting/backtester.py`, reused — never rewritten per `prefer_refactor_over_rewrite`), a replay engine, execution simulator with Indian statutory costs, walk-forward harness, run registry, checkpoints, and a metrics catalog. The North Star adds the **two things institutions have that retail backtesters don't**: massively parallel search, and the *statistical machinery to not be fooled by it*.

### 7.1 Engine capabilities (existing → target)

| Capability | Status | Target |
|---|---|---|
| Event-driven backtest | [EXISTS] | keep |
| Portfolio simulation | [EXISTS] | multi-strategy netting (ADR-028 logic) in sim |
| Minute replay | [DESIGNED] | Parquet `BarSource` over Iceberg snapshot |
| Tick replay | planned | needs tick data; same `BarSource` contract |
| Cost + slippage (mandatory) | [EXISTS] `IndianCostModel` | add `percentage`/`volume_based` slippage (spec §12) |
| No-lookahead asserts | [DESIGNED] | `lookahead_violations==0` gate per run |
| Walk-forward | [EXISTS] harness | anchored + rolling folds, each a registered run |
| Parallel sweeps | gap | shard-by-(ISIN,year) on scale-from-zero Graviton ASG / Batch / Ray |
| Parameter sweeps | gap | config-grid → N idempotent runs; registry dedups |
| Monte Carlo | gap | bootstrap/permute trade sequence → return distribution, not a point estimate |
| Deflated Sharpe (DSR) | **gap — high value** | adjust Sharpe for #trials + non-normality |
| PBO (prob. of backtest overfitting) | **gap — high value** | CSCV: does IS-best stay OOS-good? |
| FDR / multiple-testing | **gap — high value** | Benjamini–Hochberg over a tracked trial family |
| Capacity analysis | gap | ADV/participation limits → at what AUM does edge die? |

### 7.2 The research-integrity layer (this is the moat-defender)

This sits on the metrics catalog and **gates** what may even be *recommended* to shadow/paper:

- **Deflated Sharpe Ratio (Bailey & López de Prado).** A Sharpe of 2.0 found after 500 trials is not the same as one found on the first try. DSR discounts the observed Sharpe by the number of trials, their variance, and skew/kurtosis. Report DSR, not raw Sharpe, on every study.
- **PBO via CSCV (Combinatorially-Symmetric Cross-Validation).** Split history into combinatorial IS/OOS partitions; measure how often the IS-optimal config underperforms median OOS. PBO > ~0.5 ⇒ your selection process is overfitting; the "best" params are noise. This is the single most decision-relevant number for a solo founder doing parameter search.
- **FDR control over a *tracked trial family*.** Every backtest is logged to the experiment tracker (§9) as one test. Benjamini–Hochberg controls the false-discovery rate across the family, so "I found a p<0.05 strategy" is judged against *how many things you tried*. The testing budget (§11) is what makes this real rather than self-reported.
- **Monte Carlo / bootstrap.** Resample the trade sequence and the entry timing to get *distributions* of Sharpe/drawdown/expectancy. A strategy whose 5th-percentile outcome is ruin is not "profitable on average" — it's a coin flip you got lucky on once.
- **Capacity & cost realism.** Re-run with participation caps (% of bar/ADV) and the mandatory `IndianCostModel`; plot edge vs AUM. Most retail "alphas" are real at ₹1L and gone at ₹1Cr because of slippage and impact — know your number before you scale capital.

> Each study's `report.md` maps to your live-readiness gates (expectancy > 0, profit factor > 1.2, realized P&L > 0) **clearly labeled advisory** — a passing backtest never auto-promotes (spec §16). DSR/PBO are *additional* gates layered above those.

### 7.3 Architecture & parallelism

```
config grid / hypothesis ─► run planner ─► N deterministic run_ids (registry claim, idempotent)
                                              │  shard by (ISIN, year)
                                              ▼
        scale-from-zero Graviton worker ASG (Spot) ── checkpoint (qe-bt-checkpoints) ── resume-safe
                                              │  each shard → trades/metrics/labels → S3
                                              ▼
                 aggregator ─► study metrics (DSR/PBO/MC/capacity) ─► report.md ─► STOP for human
```

Workers scale from zero, run on Spot (checkpointing makes interruption safe — spec §15 guarantees a resumed run reproduces identical metrics), and write only to `*-backtest-results`. Ray is the upgrade when sweeps outgrow Batch's task model (RL, large in-memory cross-sectional studies) — same workers, different scheduler.

---

## §8 — Alpha Research Platform (integrate the existing Alpha Engine)

Your `alpha_engine` (ADR-031) already implements the hard parts of a shadow research loop: per-bar forecast generation from registered `AlphaModel`s, cost-aware net-edge ranking, full forecast persistence (`alpha-forecasts`), shadow-only publishing (`alpha.opportunities` — never `signals.*`/`orders.*`), outcome labeling, EOD rollup, per-model accuracy (`alpha-performance`), and a kill-switch pause. The North Star wraps this into a full **alpha factory** without touching its governance invariants.

### 8.1 Workflow: from idea to (advisory) champion

```
hypothesis (pre-registered §11) ─► AlphaModel impl ─► offline backtest + DSR/PBO gate (§7)
       │ pass ─► register in alpha registry ─► SHADOW mode (live forecasts, no orders)
       ▼
  CHAMPION ── runs in shadow ──┐
  CHALLENGER ── runs in shadow ─┤─► compare on OOS backtest + live-shadow IC over N sessions
                               ▼
        promotion review (HUMAN) ── challenger beats champion on rank-IC + calibration + net-edge,
                                     PBO acceptable, no drift ── then human promotes (advisory→paper)
```

### 8.2 Monitoring states (extend `alpha-performance`)

| Signal | What it catches | Source |
|---|---|---|
| **IC / rank-IC tracking** | is the forecast still predictive? | extend per-model accuracy (already 2026-06-13) |
| **Calibration** | do forecast magnitudes match realized? (reliability curve) | labeler outputs vs forecasts |
| **Feature drift** | input distribution shift (PSI/KL vs training) | feature store snapshots |
| **Label drift / decay** | edge fading over time | rolling IC slope |
| **Health state** | GREEN/AMBER/RED rollup → alarms | EOD rollup → CloudWatch `QuantEmbrace/AIEngine` |
| **Explainability** | per-forecast SHAP/attribution, stored for audit | new artifact alongside forecast |

A challenger may only be *proposed* for promotion when GREEN on IC + calibration, drift within band, and OOS edge survives DSR/PBO. Promotion remains a human decision behind the wall — the platform produces the dossier, never the trade.

### 8.3 The "WorldQuant Brain" analog, done safely

WorldQuant's model is *many* researchers generating *many* alphas with strict combination + multiple-testing control. Your solo analog: a library of small, orthogonal `AlphaModel`s; each registered as a tracked trial; combined by the existing cross-sectional ranker (net-edge) with **correlation-aware** selection (don't stack 5 alphas that are the same bet). The integrity layer (§7.2) and testing budget (§11) are what keep "many alphas" from becoming "many false positives." This is the difference between an alpha factory and a noise factory.

---

## §9 — Research Experiment Tracking

Every backtest, sweep, dataset build, and model training is an **experiment** — and reproducibility means pinning *five* things, every time.

### 9.1 The reproducibility 5-tuple (pin all, or it didn't happen)

```
experiment = f( git_sha , container_digest , data_snapshot_id(s) , feature_version , config_hash )
            → metrics + artifacts + report   (content-hashed)
```

Miss any one and 2046-you cannot rebuild 2026-you's result. This tuple is the spine of §3 (snapshots), §5 (datasets), §6 (features), and §7 (runs) — they all reference it.

### 9.2 Build vs buy — reuse the registry for runs; **MLflow for ML training**

| Concern | Home (registry pattern) | MLflow |
|---|---|---|
| Backtest runs / datasets | ✅ DynamoDB `qe-bt-runs`/`qe-bt-datasets` (idempotent, integrated) | — |
| ML model experiments (params/metrics/curves/artifacts) | possible but reinventing | ✅ **MLflow** (open, S3 artifacts = no lock-in, rich UI, model registry) |
| Unification | shared `experiment_id` + the 5-tuple links both | links via tags |

**Recommendation:** keep backtests/datasets in the DynamoDB registry you already operate; stand up **MLflow self-hosted** (tracking server on a small Graviton instance, **S3 artifact store**, Postgres/RDS or SQLite backend) for model-training experiments. Avoid Weights & Biases as the *system of record* — it's a polished SaaS but is external lock-in for your most sensitive IP (alpha research) and recurring cost; fine as an optional visualization layer, not the source of truth. One experiment graph, two stores, joined by the 5-tuple. (Open item, mirrors spec §18's Bedrock-vs-SDK decision: keep the tracker behind a thin interface so MLflow is swappable.)

### 9.3 Pre-registration (the anti-overfitting keystone)

Before a study runs, register the hypothesis, the universe, the metric, and the success threshold (§11). The tracker timestamps it. This converts "I'll try things until something works" (guaranteed false discovery) into "I made N pre-specified tests" (FDR-correctable). It is the cheapest, highest-leverage discipline on this entire list and costs only a JSON write.

---

## §10 — Compute Layer (laptop → Graviton → Ray, no Lambda-for-compute)

Storage/compute are decoupled (everything reads Iceberg-on-Glue), so compute is a *cost-and-scale* choice per job, switched without data migration.

| Stage | Engines | Use | Cost shape |
|---|---|---|---|
| **Initial (solo, now)** | Laptop + Docker + **DuckDB** + LocalStack | interactive research; DuckDB queries Parquet/Iceberg locally with **zero infra**; LocalStack mirrors AWS for paper (you already use it) | ~free |
| **Growth (small team)** | **Graviton EC2 ASG (scale-from-zero, Spot)** + **AWS Batch** + Athena | parallel backtests/sweeps; serverless SQL; the approved `backtest-worker` ASG | pay-per-use; $0 idle |
| **Advanced (institutional)** | **Ray** (on EC2/EKS) + **Spark/EMR** + **Trino** | distributed research, RL, big ETL, interactive SQL at scale | fixed clusters; reserved/savings plans |

**Migration path & rules.** Docker → EC2 ASG → Batch → Ray, each additive (same containers, same Iceberg tables). **Never Lambda for compute or polling** (`CLAUDE.md` hard rule — Lambda only for tiny event glue like the kill-switch writer, EventBridge→Step Functions GenAI trigger). **EKS is deferred** until a team and multi-tenant isolation justify its real operational cost; EC2 ASG + Batch carries you to surprisingly large scale. Prefer **Graviton (ARM64)** everywhere — ~20–40% better price/perf and already your live standard (ADR-009). Spot for everything stateless/checkpointed (workers are; the registry makes interruption safe).

> **Anti-pattern to avoid:** standing up EKS/Ray/Spark "to be ready." For a solo founder, DuckDB + Parquet answers ~80% of research questions for ~$0. Add distributed compute the day a job won't fit on one big Graviton box — not before. (Challenged assumption: "institutional-grade from day one" means institutional *discipline and data correctness*, not institutional *infrastructure footprint*.)

---

## §11 — Governance (and how it prevents you fooling yourself)

Governance here is not bureaucracy; it is the set of gates that keep a fast research loop from manufacturing false discoveries and from leaking into live capital. It reuses your existing invariant verbatim: **backtesting recommends, humans promote; GenAI explains, GenAI cannot trade; a human approves all production changes.**

| Governance domain | Control | Enforcement |
|---|---|---|
| **Data quality** | DQ gate blocks bad snapshots (contract §6); `no_silent_failures` | a `data_snapshot_id` publishes only on pass; CI |
| **Dataset approval** | `draft→validated→promoted` state machine; LOW-trust quarantined until reconciled | human gate on `promote`; registry (§5) |
| **Research approval** | study `report.md` + DSR/PBO gates; advisory label | human reads, approves promotion to shadow/paper |
| **Champion promotion** | challenger dossier (OOS + live-shadow IC + calibration + drift) | **human** decision behind the wall (§8) |
| **Experiment review** | pre-registration + tracked trial family | tracker timestamps before run (§9.3) |
| **Overfitting budget** | family-wise testing budget; FDR over the family | tracker counts every test; periodic FDR sweep |
| **Audit trail** | WORM raw + content hashes + `ops.audit` + immutable runs | S3 Object Lock; never auto-deleted |
| **Human-in-the-loop** | no auto-deploy/merge/promote/trade | IAM wall + `CLAUDE.md` prohibitions |

**The testing budget, concretely.** Maintain a counter of *independent strategy hypotheses tested per period*. Every backtest study registers as one trial. When you claim a discovery, the experiment tracker reports how many trials preceded it, and the DSR/FDR adjustment is computed against that count — not against a flattering "I only ran this once." This is the institutional habit that separates Renaissance-grade research from curve-fitting, and for a solo founder it is *the* discipline that protects real capital. Pre-registration (§9.3) + the budget + DSR/PBO (§7.2) are three views of the same defense.

**Bus-factor governance (solo-founder reality).** The platform must be operable by someone who isn't you. Every registry record is self-describing (lineage + config + container); every phase writes a `report.md`; runbooks live in `docs/runbooks/`. The 20-year goal fails if the system only runs in one person's head.

---

## §12 — Cost Optimization & Estimates

> **Caveat:** these are *planning-grade* AWS estimates (ap-south-1, 2026 on-demand-ish pricing, USD/month), to size decisions — not a quote. The dominant cost at scale is **market-data licensing**, which dwarfs compute and is *not* an AWS line item. Validate against the AWS Pricing Calculator before committing. Research-plane only; the live/paper stack is separate.

### 12.1 By stage

| Driver | Solo / ₹10L AUM | Small team / ₹1Cr AUM | Institutional |
|---|---|---|---|
| **S3** (lake + archive) | daily backbone + some intraday: 50–300 GB → **$2–10** | + intraday/tick + features: 1–10 TB w/ tiering → **$30–200** | 50–500 TB tick history, tiered → **$500–3k** |
| **Compute (research)** | DuckDB local + occasional Batch/Spot: **$10–60** | scale-from-zero sweeps + small MLflow/Trino: **$150–700** | Ray/EMR/Trino clusters, Spot-heavy: **$3k–20k** |
| **Athena** | low ad-hoc, pruned: **$5–30** | moderate: **$50–250** | Trino replaces most: **$0** (in cluster cost) |
| **DynamoDB** (registries+online features) | on-demand, tiny: **$5–25** | **$30–150** | provisioned + autoscale: **$300–1.5k** |
| **MSK / streaming (research tee)** | negligible (reuse live) | shared | dedicated: **$500–2k** |
| **GenAI analysis** (advisory, on-demand) | per-run token cap: **$5–40** | **$40–200** | **$300–1.5k** |
| **Misc** (CloudWatch, ECR, data transfer) | **$10–30** | **$50–150** | **$300–1k** |
| **AWS subtotal (research)** | **≈ $50–250 / mo** | **≈ $400–1,800 / mo** | **≈ $6k–30k / mo** |
| **Market-data licensing (separate!)** | free (bhavcopy) → low | **vendor intraday: often ₹ tens of thousands/mo** | exchange + vendor licenses: **$ thousands–tens of thousands/mo**, + redistribution licensing if multi-tenant |

The headline: **at your stage the AWS bill is lunch money; the real money and the real risk are data licensing and your own time.** Architect for cheap compute (you've done this) and spend management attention on data acquisition and research discipline.

### 12.2 Levers (in impact order)

1. **S3 lifecycle + tiering** — Standard→IA 30d→Glacier IR 365d→Deep Archive; Intelligent-Tiering for unpredictable access. Tick/archive in Zstd. (Contract §9 already specifies this — extend to all zones.)
2. **Scale-from-zero + Spot + Graviton** — $0 idle workers, Spot for checkpointed jobs, ARM price/perf. Biggest compute lever.
3. **Athena hygiene** — partition pruning (Iceberg hidden partitioning helps), columnar Parquet, compression, `CTAS` to materialize hot aggregates, `LIMIT`/projection. Cost is $/TB *scanned* — scan less.
4. **Iceberg maintenance** — scheduled compaction (kill small files), snapshot expiry + orphan-file cleanup. **Skipping this is a silent cost+performance leak** (metadata bloat, millions of tiny files). Budget a weekly maintenance job.
5. **VPC gateway endpoints** for S3 + DynamoDB — removes them from NAT data charges (you already have this — keep it).
6. **DuckDB-first** — every question answered on a laptop is $0 of Athena/Batch. Underrated lever for solo stage.
7. **Reserved/Savings Plans** — only after a steady baseline emerges (≥3–6 mo); never pre-commit during exploration.

---

## §13 — Technology Decisions Matrix

Each row: preferred, alternatives, why, risks, migration path. Bias: **open formats, managed-but-portable, no fashion without justification, your cost/lock-in rules win ties.**

| Component | Preferred | Alternatives | Why preferred | Risks | Migration path |
|---|---|---|---|---|---|
| Object storage | **S3** | GCS, Azure Blob, MinIO | the one sanctioned lock-in; durability/ecosystem | region/account lock (mild) | S3-compatible API ⇒ MinIO/other if ever needed |
| Table format | **Apache Iceberg** | Delta, Hudi, plain Parquet | broadest engine support, snapshots, schema+partition evolution; AWS-native | Iceberg maintenance ops | open metadata ⇒ any catalog/engine |
| File format | **Parquet** (Zstd archive) | ORC, Avro | columnar, universal, pushdown | none material | n/a (universal) |
| In-memory | **Arrow** | pandas-native | zero-copy across DuckDB/Spark/Ray | none material | n/a |
| Catalog | **Glue Data Catalog** | Hive Metastore, Nessie, Unity, Polaris | serverless, AWS-native, Iceberg REST | AWS-proprietary | Iceberg REST ⇒ Nessie/Polaris (git-for-data) |
| Ad-hoc SQL | **Athena** | Trino, Spark SQL, Redshift | serverless, $0 idle, reads Iceberg/Glue | $/TB at high volume | same tables ⇒ Trino at cost crossover |
| Interactive @scale | **Trino** | Presto, Spark, Dremio | open, fast joins, federation | cluster ops | additive over same Glue tables |
| Local engine | **DuckDB** | pandas, Polars | reads Parquet/Iceberg, zero infra, fast | single-node | scale-out ⇒ Spark/Ray on same files |
| Feature store | **home-grown → Feast** | Tecton, SageMaker FS | reuse `feature_reader`; Feast open + S3/DDB | DIY effort early | thin API mirrors Feast ⇒ swap in |
| Experiment tracking | **MLflow (self-host)** | W&B, Neptune, SageMaker | open, S3 artifacts, model registry | run a small server | S3 artifacts portable; tracker behind interface |
| Registries (run/dataset/model) | **DynamoDB pattern** | MLflow registry, RDS | idempotent claim you already operate; KV rule | DDB-proprietary | export to Iceberg metadata if leaving |
| Batch compute | **AWS Batch + EC2 ASG (Graviton, Spot)** | Lambda(❌ rule), Fargate(removed), EKS | $0 idle, your approved pattern, cheap ARM | Spot interruption (checkpointed) | → Ray/EKS at scale, same containers |
| Distributed research | **Ray** | Dask, Spark | unifies sweeps/RL/serving, Pythonic | newer ops surface | add when single-node insufficient |
| Big ETL | **Spark/EMR** (only if needed) | Glue ETL, Flink | mature at TB+ scale | cost/ops | EMR-on-EC2 Spot; or skip via DuckDB/Trino |
| Streaming bus | **MSK Serverless** (exists) | Kinesis(❌ rule), self-Kafka, Redpanda | already live (ADR-010/011); IAM-auth | AWS-proprietary | Kafka API ⇒ self-host/Redpanda if needed |
| Online KV | **DynamoDB** | Redis, ScyllaDB | KV rule, managed, sub-ms | DDB-proprietary | abstraction layer ⇒ Redis/Scylla |
| Orchestration | **Step Functions + EventBridge** (event glue) + cron | Airflow, Dagster, Prefect | serverless, no scheduler to run; fits "no Lambda-compute" (glue only) | AWS-proprietary; limited DAG ergonomics | → Dagster/Airflow if DAGs get complex |
| GenAI (advisory) | **`LLMProvider` interface** (Anthropic SDK today; Bedrock optional) | hard-wire Bedrock | keeps spec §18 decision open; no premature lock | provider drift | interface ⇒ swap provider |
| IaC | **Terraform** (exists) | CDK, Pulumi | already standard here; multi-cloud-portable | state mgmt | n/a |
| Containers | **Docker + ECR (+ S3 export)** | raw AMIs | reproducibility; pin digests for 20-yr repro | registry rot | export images to S3 archive |

**Explicitly rejected as *foundations* (allowed as optional later):** Snowflake, Databricks-as-platform, Redshift (lock-in + cost vs. open lakehouse); Kinesis, Lambda-for-compute, ECS/Fargate (your rules / already removed); W&B-as-source-of-truth, managed feature stores (lock-in for core IP). None are "bad" — they violate *your* stated principles, which is the whole point of having principles.

---

## §14 — Five-Year Roadmap

Each year ends on a **capability milestone** and a **decision point**. Sequencing rule: *correctness and reproducibility before scale; discipline before features.* Everything is additive — no year requires rewriting a prior year.

### Year 1 — Minimum Viable Lakehouse  *(foundation; mostly promoting what you've designed)*

- Register existing `lake/ohlcv` + reference data as **Iceberg tables in Glue** (no byte movement); raw zone → **WORM** (Object Lock).
- Land the **daily bhavcopy backbone** (15 yr, survivorship-safe) + corp actions + symbol map + index membership + calendar; publish first `data_snapshot_id`.
- **Dataset Registry v1** (promote `qe-bt-datasets`); reproducibility 5-tuple wired; **DuckDB** local workflow.
- Add **`available_at`** to revisable facts; ISIN-key audit.
- **Milestone:** *any backtest is reproducible from a pinned snapshot + git SHA + container digest.*  **Decision point:** which intraday vendor to license (cost vs. coverage — `intraday-data-procurement-memo.md`).

### Year 2 — Institutional Backtesting  *(integrity layer)*

- Parallel sweeps on **scale-from-zero Graviton/Batch**; parameter grids; checkpoint/resume proven (identical-metrics guarantee).
- **Research-integrity layer:** DSR, PBO/CSCV, Monte Carlo, capacity, FDR over a tracked trial family; **pre-registration** + **testing budget** live.
- **Feature layer v1** (home-grown PIT over the lake; train/serve parity gate); **MLflow** self-hosted for model experiments.
- Onboard licensed **intraday** data; tick ingestion contract ready.
- **Milestone:** *every strategy decision is gated by walk-forward + DSR/PBO, with a logged trial count.*  **Decision point:** Athena→Trino crossover? Feast adoption?

### Year 3 — Alpha Factory  *(the Alpha Engine grows up)*

- Productionize **champion–challenger** behind the wall; **drift/IC/calibration/health states** on `alpha-performance`; **explainability** (SHAP) persisted.
- Correlation-aware multi-alpha combination; orthogonal alpha library; multiple-testing governance enforced.
- Serverless **GenAI advisory** layer (EventBridge→Step Functions→`LLMProvider`) for run summaries + operator RAG — advisory only (spec §18).
- **Milestone:** *N alphas in shadow with honest OOS + live-shadow IC and controlled false-discovery; a human promotion dossier generates automatically.*  **Decision point:** which alphas (if any) earn paper→live promotion under the existing 5-session gate.

### Year 4 — Multi-Asset Expansion  *(additive asset classes)*

- Activate the `Calendar`/`SessionModel`/instrument abstractions: **NSE F&O** (expiry/strike/lot; options Greeks/IV), deeper **US equities** (Alpaca), **crypto** (24/7 calendar — breaks EOD/square-off assumptions; handle explicitly).
- **Feast** if feature/team growth warrants; **Ray** for distributed research; alt-data landing + quarantine.
- **Milestone:** *the same reproducible research workflow runs across ≥3 asset classes from one lakehouse.*  **Decision point:** EKS yet? (team size / multi-tenant pressure.)

### Year 5 — QuantConnect-for-India  *(platformization)*

- Multi-user/multi-tenant research: per-tenant isolation, quotas, **data entitlements** (exchange redistribution licensing — a legal gate, not just technical), hosted backtesting API, shareable/forkable strategies + datasets (Nessie/Polaris catalog branching shines here).
- **Milestone:** *external researchers run isolated, reproducible, entitlement-controlled backtests on the curated lake.*  **Decision point:** managed-service vs. self-host trade-offs at platform scale; data-redistribution licensing model.

> **Critical-path dependencies:** Year 2's integrity layer depends on Year 1's snapshots/registry; Year 3's factory depends on Year 2's gates; Year 5's multi-tenancy depends on Year 4's entitlement-aware data model. **Do not skip ahead** — a Year-3 alpha factory built on a Year-1-incomplete PIT foundation manufactures confident, reproducible *wrong answers*, which is worse than no platform.

---

## §15 — Risks, Hidden Risks & Challenged Assumptions

The brief asked me to challenge assumptions and surface hidden risks. The honest ones:

### 15.1 The risks that actually kill platforms like this

| # | Risk | Why it's lethal | Mitigation (where in this doc) |
|---|---|---|---|
| R1 | **Self-deception via multiple testing** | A solo founder + fast backtester *will* find noise that looks like alpha; default outcome of unconstrained search is false discovery. | Testing budget + pre-registration + DSR/PBO/FDR (§7.2, §9.3, §11). **The #1 risk — by a wide margin.** |
| R2 | **Silent PIT/survivorship breakage** | One vendor backfill, one ticker reuse, one `.shift(-1)` and every downstream backtest lies — invisibly. | WORM raw + redelivery diffing + ISIN key + `available_at` + leakage gate (§3.2–3.4). |
| R3 | **Data licensing & redistribution law** | The real cost and a *legal* wall, esp. multi-tenant (Year 5): you generally **cannot redistribute NSE/vendor data** to external users without entitlement licensing. | Treat entitlements as a first-class Year-4/5 data-model concern, not an afterthought (§14). |
| R4 | **Reproducibility rot over 20 years** | Python, libs, CUDA, even CPUs drift; "reproducible" silently degrades to "approximately." | Pin container *digests* + archive images to S3; document determinism boundaries (R8); aim **metric-stable**, not bit-identical, across decades. |
| R5 | **Over-engineering for a stage you're not at** | Building Two-Sigma infra at ₹10L AUM burns the runway and the founder. | DuckDB-first; defer Feast/Ray/EKS/Spark until pain (§6, §10); roadmap sequencing (§14). |
| R6 | **Iceberg/lakehouse maintenance debt** | Unmaintained Iceberg = millions of small files, snapshot bloat, slow + expensive queries. | Scheduled compaction + snapshot expiry + orphan cleanup as a budgeted job (§12.2). |
| R7 | **The wall erodes under success pressure** | When an alpha looks great, the temptation to auto-promote grows; policy alone won't hold. | Wall is **architectural** (separate env/IAM/buckets/tables), not policy (§1.3, §11). |
| R8 | **Determinism boundaries** | Float non-associativity, parallel reductions, library RNG changes ⇒ "deterministic" replays diverge subtly. | Fixed seeds, deterministic same-ts ordering, no wall-clock in logic (spec §11); define + test the tolerance band; CI diff. |
| R9 | **Crypto/24-7 breaks NSE-shaped assumptions** | Trading-calendar, EOD rollup (15:35 IST), MIS square-off are hard-coded mental models across the stack. | Parameterize `Calendar`/`SessionModel` by asset class *now*, before crypto (§4.4). |
| R10 | **Bus factor = 1** | A 20-year platform that lives in one head dies on one bad week. | Self-describing registries, per-phase reports, runbooks, governance §11. |

### 15.2 Assumptions worth challenging

- **"Institutional-grade from day one."** Reframed: institutional *correctness and discipline* from day one (PIT, lineage, multiple-testing control); institutional *infrastructure* only when scale demands. The former is cheap and essential; the latter is expensive and, early, a liability. Conflating them is the most expensive mistake in the brief.
- **"Data is the moat."** True — but *correct, point-in-time, licensed* data is the moat. Raw volume of sloppy data is an *anti*-moat: it gives confident wrong answers. The moat is the discipline as much as the bytes.
- **"No major rewrites for 20 years."** Achievable for *data and lineage* (open formats + additive evolution make this real). **Not** achievable for *compute engines and models* — those will turn over several times, and that's fine. Design the data/contracts to outlive the compute. The North Star is built on exactly this split.
- **"Combine ideas from QuantConnect + WorldQuant + HRT + Two Sigma + Jane Street + RenTech + Databricks + Snowflake + Netflix."** Most of these are *organizations of hundreds of specialists*. The transferable ideas for a solo founder are: open lakehouse (Netflix/Databricks), multiple-testing rigor (RenTech/HRT), alpha-factory-with-controls (WorldQuant), event-driven backtesting + hosted research (QuantConnect). The *org-scale* tooling (Snowflake warehouses, Jane Street's OCaml/FPGA stack) is explicitly **not** transferable and chasing it is a trap. This doc keeps the ideas, drops the headcount assumptions.
- **"AWS is the target."** Fine — but the design's value is that *only S3 is a hard dependency*. If AWS ever becomes wrong (cost, policy, acquisition), the open lakehouse moves. Don't let convenient managed services (Glue, DynamoDB, MSK) quietly become load-bearing in ways the abstractions don't cover; audit the lock-in surface yearly.

---

## §16 — "If I Were Starting Over"

Eight things I'd lock in from row zero — most are cheap *now* and agony to retrofit:

1. **Iceberg from the first table, not raw Hive Parquet.** You're early enough that registering Iceberg costs almost nothing and buys snapshots/time-travel/evolution — the literal mechanics of your reproducibility principle. Retrofitting table semantics onto a sprawling Parquet lake later is real work.
2. **`available_at` (knowledge-time) on every revisable fact from the first row.** The cheapest insurance against the most common silent alpha-inflater. Adding it after you have 5 years of fundamentals is a migration; adding it now is a column.
3. **ISIN as the only stable key — everywhere, no exceptions.** (You did this. Keep it absolute; never let a "quick" ticker-join sneak in.)
4. **One registry pattern for runs, datasets, features, models, experiments.** You already have the idempotent DynamoDB claim pattern in production. Resist standing up four different metadata systems; one pattern = one mental model, one backup, one failure mode.
5. **Pin container *digests* (not tags) from day one; archive images to S3.** "Reproducible in 2046" is meaningless without the exact environment. Tags move; digests don't.
6. **Treat multiple-testing control as a feature, not a footnote.** Pre-registration + testing budget + DSR/PBO should exist *before* the first serious sweep, not be bolted on after you've already convinced yourself of three fake alphas.
7. **Don't build the feature store / Ray / EKS / Spark until it hurts.** DuckDB + Parquet + the existing PIT feature function carry you absurdly far. Every premature platform component is runway and focus you don't get back.
8. **Make the research/live wall architectural before you ever need it.** Separate env/IAM/buckets/tables now, while it's a config choice — not later, under the pressure of a great-looking backtest, when it's a risky migration.

And one thing I'd actively **resist**: **Snowflake/Databricks gravity.** They are genuinely excellent and will keep tempting you with ergonomics. For *this* set of principles (no lock-in beyond S3, cost discipline, 20-year open evolution), the open S3 lakehouse is the correct call, and it's the one the named institutions themselves trend toward at the storage layer. Convenience now is lock-in later.

> **The compression of all of it:** get *point-in-time correctness*, *lineage*, and *multiple-testing discipline* right while the system is small and cheap to shape. Those three are the platform. Everything else — engines, models, asset classes, even AWS itself — is replaceable around them.

---

## §17 — Reconciliation with Existing Docs (resolving known conflicts)

This North Star is *reconcile-and-extend*, so here is exactly how it meets what exists.

### 17.1 The path conflict (named in your own contract) — resolved

`aws-data-lake-contract.md §0` already flags that it supersedes the conflicting paths in `architecture/system_design.md` (`backtest/results/{run_id}/`) and `commands/run_backtest.yaml` (`backtests/{strategy}/{timestamp}/`). **This document adopts the contract's layout as canonical** (§2.2) and extends it with Iceberg + five zones. Action items (governance, advisory): fix the stale refs in `run_backtest.yaml` (`scripts/backtest/run.py`, `services/strategy_engine/registry.py`) per spec §219; record this North Star as an ADR (suggest **ADR-032**) so the layout has one source of truth.

### 17.2 Existing component → North Star mapping (almost nothing is thrown away)

| Existing (status) | North Star role | Change |
|---|---|---|
| `aws-data-lake-contract.md` (DESIGNED; validation tooling EXISTS) | §2 Data Lake | **+Iceberg** over curated; +5 zones; +`available_at`; substance retained |
| `s3_data_catalog.py` / `data_loader.py` / `data_quality.py` (EXISTS) | §2/§4 catalog, loader, DQ gate | reuse; add Iceberg read path |
| `qe-bt-runs` / `qe-bt-checkpoints` (DESIGNED) | §7 run registry + resume | reuse pattern; extend to §5/§9 registries |
| `qe-bt-datasets` + `_snapshots` (DESIGNED) | §5 Dataset Registry | promote to first-class + promotion state machine |
| `backtester.py` + replay + sim + walk-forward (EXISTS/DESIGNED) | §7 engine | reuse (`prefer_refactor`); **+** DSR/PBO/MC/capacity integrity layer |
| `IndianCostModel` + cost/slippage (EXISTS) | §7 cost realism | reuse; add `percentage`/`volume_based` |
| `model-dataset-spec.md` + builder (DESIGNED) | §6 features + §8 labels | reuse; feed feature store |
| `alpha_engine/*` (EXISTS, ADR-031, shadow) | §8 Alpha factory | wrap with champion-challenger + drift/IC/calibration/explainability; invariants untouched |
| `shared/features/feature_reader.py` (EXISTS) | §6 Feature store | becomes the offline/online feature function; Feast-compatible API later |
| `aws-serverless-genai-backtesting-design.md` (DESIGNED) | §9/§11 GenAI advisory | reuse; keep `LLMProvider` interface (Bedrock-vs-SDK open) |
| EC2 ARM64 ASGs + MSK + DynamoDB + S3 (EXISTS, ADR-009/010/011) | §1/§10 compute + bus | reuse; research uses scale-from-zero worker ASG |
| `no-lookahead-rules.md` (DESIGNED) | §3.4 leakage gate | reuse; elevate to enforced per-run assert |
| Backtesting steering / isolation (DESIGNED) | §1.3/§11 the wall | reuse verbatim — it *is* the wall |

### 17.3 Net-new in the North Star (the actual gaps to build)

Iceberg-on-Glue over the curated layers · WORM/Object-Lock on raw · **`available_at` bitemporality** · unified Dataset/Feature/Model/Experiment registries on the one pattern · **research-integrity layer (DSR/PBO/MC/FDR/capacity)** · **pre-registration + testing budget** · champion-challenger + drift/IC/calibration/explainability on the Alpha Engine · MLflow for ML experiments · DuckDB local workflow · asset-class-parameterized `Calendar`/`SessionModel` · container-digest pinning + image archival · Iceberg maintenance jobs.

### 17.4 Governance invariants — carried verbatim, never weakened

*Backtesting recommends, backtesting cannot promote. GenAI explains, GenAI cannot trade. A human approves all production changes.* Research IAM has no broker creds and no live/paper access. Paper and live paths stay fully isolated. LOW-trust data stays quarantined until reconciled. Nothing in this 20-year vision relaxes a single safety rule in `CLAUDE.md` — the platform gets bigger; the wall does not move.

---

## Appendix A — One-page cheat sheet

```
STORAGE   S3 (only hard dep) · Iceberg tables · Glue catalog · Parquet/Zstd · 5 zones + archive
KEY       ISIN (stable) · available_at (knowledge-time) · unadjusted price + read-time adj_factor
QUERY     DuckDB (solo) → Athena (serverless) → Trino (scale) — all over the same Iceberg tables
COMPUTE   Docker → Graviton EC2/Batch (Spot, scale-from-0) → Ray/Spark — NEVER Lambda-for-compute
METADATA  one DynamoDB idempotent-claim pattern → run/dataset/feature/model registries + MLflow
INTEGRITY pre-registration + testing budget + DSR + PBO/CSCV + Monte-Carlo + FDR + capacity
ALPHA     alpha_engine shadow → champion/challenger → drift/IC/calibration/explainability (advisory)
REPRO     pin {git_sha, container_digest, data_snapshot_id, feature_version, config_hash}
WALL      separate env/IAM/buckets(qe-bt-*)/tables — research RECOMMENDS, a human PROMOTES
FIRST     Year 1 = Iceberg + WORM raw + bhavcopy backbone + dataset registry + reproducible backtest
NEVER     weaken risk controls · mix research/live · Lambda streaming · skip PIT · trust LOW data
```

*End of North Star (NS-1). Vision/design only — no infra deployed, no trading behavior changed. Suggest recording as ADR-032 and linking from `architecture/system_design.md` and `aws-data-lake-contract.md`.*
