# QuantEmbrace v2 — Full Re-Architecture Review & Target Design

> **Status: PROPOSED — `[PLANNED — not yet implemented]`. Nothing in this document changes trading
> behavior. It is a design review and target architecture awaiting operator approval.**
>
> Version: RA-1 · Date: 2026-07-05 · Author: Chief Architect review (commissioned as a
> ground-up, no-sacred-cows redesign)
>
> Relationship to existing canon:
> - `architecture/system_design.md` — describes the **current** system. This document proposes replacing much of it.
> - `architecture/data-platform-north-star.md` (NS-1) — the research-platform vision. RA-1 **agrees with ~80%**
>   of NS-1 and explicitly **disagrees** with two of its calls (§2.6, §5.4 below).
> - `docs/strategy/research-program-consolidation-2026-06-20.md` — the alpha record. RA-1 treats it as the
>   single most important input to the architecture, because architecture must serve the firm you actually are.

---

## §0 — Executive Summary (read this if you read nothing else)

**The brutal, load-bearing finding:** QuantEmbrace's binding constraint is not architecture. It is that,
per your own capstone memo, **no strategy currently possesses a deployable edge**. Every intraday, gap,
calendar, overnight, factor, options-vol, event-vol, and futures-trend hypothesis has been eliminated or
shelved after costed, out-of-sample scrutiny. The only live experiment is two advisory forward factor
books gated ~Dec-2026.

An honest hedge-fund architect must therefore say something uncomfortable before drawing a single box:
**a platform whose product is currently *research* is architected as if its product were *order flow*.**

The numbers make the inversion concrete:

| Where the engineering went | LOC | What it supports |
|---|---|---|
| Real-time distributed trading stack (4 core services + shared + monitoring) | ~51,000 | Paper sessions on a ₹10L simulated NAV, ≤15 entries/day, 0 profitable sessions in 19, live BLOCKED |
| Research studies + tooling (scripts/) | ~23,000 | The only part of the firm that produced validated knowledge in 2026 |
| Backtesting lab (services/backtesting) | ~5,800 | The second most productive asset |
| Tests | ~41,000 | Largely testing the distributed stack's failure modes |

The real-time stack is simultaneously **over-engineered for its scale** (Kafka/MSK Serverless,
4 consumer groups, DLQ topics, enrichment watchdogs, circuit breakers — for ~50 symbols and ≤15
orders/day) and **under-engineered for its correctness** (6 of 19 paper sessions invalidated by config
drift, stale Docker images, and a silent YAML-key mismatch; four kill-switch self-refire bugs; asyncio
task death; a 7–12s internal signal-age problem that required raising a safety threshold to work around).
These are not random bugs. They are the **characteristic bug classes of a distributed system**, paid for
at a scale that gets nothing back from distribution.

**The v2 thesis:** rebuild QuantEmbrace as a **research factory wrapped around one deterministic trading
engine** — a single-process, event-sourced core in which backtest, paper, and live are the *same code*
driven by three different clocks. Keep the governance wall, the cost discipline, the pre-registered
gates, the Parquet lake, and the safety *requirements* (not the safety *implementation*). Delete the
Kafka-based microservice topology from the trading path entirely. Defer all live-scale infrastructure
until the forward factor gate — or a future validated edge — earns it.

**What this buys, quantitatively:**
- The config-drift bug class (invalidated Sessions 10–15) becomes structurally impossible: one typed,
  frozen, content-hashed config per session, stamped into every record.
- The signal-age problem (7–12s Kafka hops, `RISK_MAX_SIGNAL_AGE_SECONDS=30` workaround) disappears:
  in-process risk checks run in microseconds.
- The research→production parity problem (the F1 lesson: a proxy overstated harvestable edge 3.5×)
  is closed at the code level: what you backtest is byte-for-byte what you paper- and live-trade.
- Fixed AWS burn (MSK Serverless, always-on ASG capacity, LocalStack maintenance time) drops to
  approximately: one small EC2 instance during sessions + S3 + a token-store table.
- Operator surface shrinks from "docker-compose stack + 2 validation scripts + rebuild discipline"
  to "run one process with one config file."

**What this does *not* change:** capital protection > trade count > profit; backtesting recommends,
humans promote; GenAI explains, GenAI cannot trade; live remains BLOCKED until gates pass. Governance
is the part of QuantEmbrace that is already institutional-grade. It survives v2 verbatim.

---

# PHASE 1 — Architectural Review (brutally honest)

## 1.1 What the current system actually is

- **Live/paper plane:** `data_ingestion → Kafka(ticks) → strategy_engine → Kafka(signals.pending) →
  ai_engine → Kafka(signals.enriched) → risk_engine (11 validators) → Kafka(signals.approved) →
  execution_engine (UniverseOrderValidator → idempotency → PaperSimulator/broker) → Kafka(orders.events)`.
  Deployed on EC2 ARM64 ASGs + MSK Serverless + 10+ DynamoDB tables; developed locally against
  docker-compose + LocalStack + Redpanda.
- **Research plane:** a Parquet bhavcopy lake (2016–2026), a backtesting lab (`services/backtesting/`:
  replay engine, metrics engine, run registry, walk-forward, TEE/MIS sim, dataset builder, Bedrock
  advisory layer), and ~30 standalone study scripts (`run_*_study.py`, `run_options_vol_backtest.py`, …)
  each with its own harness, plus three cost models.
- **Governance plane:** promotion gates, pre-registered forward gates, session-validity rules,
  fail-closed live rules, the advisory wall. Documented in CLAUDE.md/ADRs and enforced by convention +
  a few scripts.

## 1.2 Findings

### F-1 (CRITICAL) — The architecture optimizes the wrong product
The firm's validated output in 2026 was *knowledge* (a rigorous elimination of ~15 strategy families,
a reusable toolkit, one forward experiment). Its unvalidated output was *trading* (19 paper sessions,
0 profitable, live blocked). Yet the platform's complexity budget — distributed messaging, service
isolation, watchdogs, hot-reload, circuit breakers — is spent almost entirely on the trading path.
Institutional firms spend their complexity budget in proportion to where returns come from. Here that
is unambiguously research throughput and research integrity.

### F-2 (CRITICAL) — Distribution without a distribution requirement
Kafka/MSK with 8 topics, retry/DLQ mirrors, 4 consumer groups, and per-service kill-switch listeners
serves a workload of: ~50–200 symbols, candle-driven strategies on 1m–15m bars, ≤15 entries/day, one
operator. This workload fits comfortably in one Python process with an in-memory queue — with three
orders of magnitude of headroom. NSE-colocation HFT firms need distributed low-latency fabrics; a
candle-based single-account system does not. The costs of distribution here were not hypothetical:

- **Signal staleness is self-inflicted.** Candle signals arrive at risk_engine 7–12s old purely from
  pipeline hops; the fix was raising `RISK_MAX_SIGNAL_AGE_SECONDS` from 5→30 — i.e., *weakening a safety
  check to accommodate the architecture*. In-process, the same check could be 100ms.
- **Config/state drift is the #1 cause of invalid sessions.** Sessions 10–11 (stale image), 12–15
  (YAML key prefix mismatch silently disabling quality gates). Root cause: configuration is smeared
  across YAML files, env vars in shell scripts, DynamoDB tables, and Docker image bake-time state,
  with no single hashed artifact asserting "this is the session's config."
- **The bug ledger reads like a distributed-systems textbook:** 4 kill-switch self-refire bugs, asyncio
  task death swallowed by `except Exception` (CancelledError), consumer-group settle races in the
  validation script, EnrichmentWatchdog fallback complexity, duplicate-fill DynamoDB race (PHASE8-006).
  Every one of these bug classes is either impossible or trivially observable in a single-process
  deterministic engine.

### F-3 (CRITICAL) — Research/production parity does not exist
There are at least **four** execution semantics in the repo: (1) live strategy classes under
`strategy_engine/strategies/`, (2) `strategy_engine/backtesting/backtester.py`, (3) the lab replay
engine (`services/backtesting/replay_engine.py` + strategy adapters), and (4) each standalone study's
bespoke loop. The F1 futures study proved what this costs: a proxy mismatch overstated harvestable edge
**3.5×**. The same class of divergence exists *inside the codebase* between what is backtested and what
paper-trades. Institutional rule: **the researched object and the traded object must be the same
object.** Nothing matters more in quant platform design than this invariant, and the current
architecture cannot provide it.

### F-4 (HIGH) — ai_engine does not pay for its place in the hot path
It adds a Kafka hop, a schema version, a watchdog, and a fallback path; its observed output was "flat
scores" and its models cannot add value while upstream signals have no edge (garbage-in constraint).
The *idea* (quality scoring, regime awareness) is fine — as an **offline research artifact** evaluated
in the lab, promoted into the engine as a library function only after demonstrating uplift in
walk-forward. As a service, it is negative-value: complexity now, benefit never demonstrated.

### F-5 (HIGH) — The netting/portfolio bug class exists because there is no portfolio layer
Signals flow directly to orders. Cross-strategy direction conflicts required a bolt-on gate (ADR-028),
TEE ghost positions appeared, MIS square-off needed its own resilience loop (ADR-029). All symptoms of
a missing abstraction: strategies should emit **target positions/weights**, a portfolio constructor
should net them, risk should clamp the *portfolio delta*, and execution should reconcile
current→target. With that layer, netting bugs are structurally impossible and "unmanaged position"
becomes a reconciliation query, not an incident.

### F-6 (HIGH) — Research tooling is a collection, not a platform
Every 2026 study rebuilt its own loop, its own data loading, its own report format (the toolkit list in
the consolidation memo is ~15 scripts). The shared parts that *did* emerge (cost models, gates,
loaders) are the embryo of the right design. But two harness bugs (VRP date-parse, condor
cycle-selection) were caught only by operator vigilance — in a platform, the harness is written and
tested **once**. Research velocity is the firm's production line; every bespoke harness is production
downtime.

### F-7 (MEDIUM) — Fixed cost and operational drag with zero revenue
MSK Serverless, ASG capacity, 10+ DynamoDB tables, LocalStack/Redpanda local stack, image-rebuild
discipline, 2 mandatory pre-session validation scripts, daily token ceremony — all carried by a system
that trades no capital and (per the research record) should not trade capital soon. Every hour of
stack-babysitting (the "attended-babysit pattern" of Session 17) is an hour not spent on the only
activity with positive expected value: research.

### F-8 (MEDIUM) — Safety is implemented as distributed policy rather than local invariant
The safety *requirements* are excellent. But their implementation is scattered: kill switch = a Kafka
topic + DynamoDB flag + per-service listeners + auto-trigger monitors (4 self-refire bugs); paper/live
isolation = an env var + a DynamoDB flag + shell-script comments; session validity = two scripts that
are themselves racy/non-idempotent. In v2 these become **type-level and process-level invariants**
(e.g., a `PaperBroker` that *cannot* construct a live client; a config hash that *is* the session
identity) — safety by construction, not by vigilance.

### F-9 (LOW) — Repo hygiene
Duplicated agent/spec files ("`spec-design 2.md`", "`spec-design 3.md`"), ~299 cloud-sync duplicate
files flagged 2026-06-13, .docx binaries in the repo root, `archive/` + `session-archives/` mixing
with live code. Minor, but it is friction on every search and every context load.

### What is genuinely good (and must survive)

1. **Governance and research integrity** — pre-registered gates never relaxed, baseline controls,
   gross-vs-cost decomposition, the negative-results capstone, human-only promotion, the advisory wall.
   This is *better* than many professional shops. Keep verbatim.
2. **The India cost mandate** — full statutory cost stack in every evaluation, and three tested cost
   models. This is the moat that killed mirages before they killed capital.
3. **The data lake direction** — Parquet bhavcopy lake 2016–2026, snapshot thinking, trust tiers,
   PIT/no-lookahead rules (NS-1 §2, data-lake contract). Correct and load-bearing.
4. **The lab's registry/walk-forward/metrics designs** — run registry, checkpointing, walk-forward with
   OOS aggregation, TEE/MIS simulation. Right ideas; they become the core of the v2 research factory.
5. **Safety requirements** — fail-closed live, universe hard gate, idempotent orders, kill switch,
   promotion gates. The *requirements* transfer wholesale; the *implementation* gets simpler and stronger.
6. **The strategy math itself** — `_viability.py`, position sizing, ATR logic, regime gate. Small,
   portable, tested.

---

# PHASE 2 — Target Architecture (QuantEmbrace v2)

## 2.1 Design center

> **One deterministic engine, three clocks, one data platform, one journal — wrapped in a research
> factory, governed by the existing wall.**

Institutional inspiration, correctly applied to this scale: what makes Jane Street-class systems good
is not Kafka — it is **determinism, replayability, one artifact from research to production, and
simplicity aggressive enough to be audited by one person**. At QuantEmbrace's scale (one operator, one
account, EOD/positional + at most minute-bar intraday), that translates to a modular monolith, not
microservices.

## 2.2 System overview

```
                            ┌─────────────────────────────────────────────┐
                            │            DATA PLATFORM  (S3 + local)       │
                            │  raw/ (immutable drops)                      │
                            │  curated/ Parquet, PIT-correct, ISIN-keyed,  │
                            │           corp-actions, universe membership  │
                            │  snapshots/ manifest = data_snapshot_id      │
                            │  features/ derived, snapshot-pinned          │
                            └───────────────┬─────────────────────────────┘
                                            │  one loader API: qe.data
                ┌───────────────────────────┼───────────────────────────────┐
                │                           │                               │
                ▼                           ▼                               ▼
     ┌────────────────────┐    ┌─────────────────────────┐    ┌─────────────────────────┐
     │  RESEARCH FACTORY   │    │   TRADING ENGINE (one    │    │  ADVISORY / GENAI        │
     │  qe.research         │    │   process, one binary)   │    │  (existing Bedrock layer, │
     │  • Study = declared  │    │   qe.engine              │    │   event-driven, offline)  │
     │    config, not a     │    │                          │    └─────────────────────────┘
     │    bespoke script    │    │  DataFeed ──► Strategies │
     │  • walk-forward,     │    │      │(bars)     │(target positions)
     │    PBO/DSR gates     │    │      ▼           ▼       │
     │  • run registry      │    │  Portfolio Constructor   │
     │  • metrics+report    │    │      │ (net targets)     │
     │  • experiment log    │    │      ▼                   │
     └─────────┬──────────┘    │  Risk Pipeline (library:  │
               │  promotes      │   pre-trade checks, caps, │
               │  strategy      │   kill-state, universe)   │
               │  configs only  │      │ (approved deltas)  │
               │  via HUMAN     │      ▼                    │
               └───────────────►│  Execution (reconciler:   │
                                │   current→target, idem-   │
                                │   potent orders)          │
                                │      │                    │
                                │      ▼                    │
                                │  Broker Port (one of:)    │
                                │   SimBroker | PaperBroker │
                                │   | ZerodhaBroker(gated)  │
                                │                          │
                                │  EVERY event appended to │
                                │  ──► EVENT JOURNAL ──►   │
                                │  (Parquet/JSONL, local+S3)│
                                └──────────────────────────┘
                                            │
                                            ▼
                              ┌─────────────────────────────┐
                              │ OBSERVABILITY = the journal  │
                              │ session report, trade replay,│
                              │ decision replay, monitoring  │
                              │ TUI all read the journal     │
                              └─────────────────────────────┘
```

## 2.3 The core invariant: one engine, three clocks

The engine is a deterministic event loop: `Event in → State transition → Events out`, with **all**
side effects behind two ports (`DataFeed`, `Broker`) and **all** events appended to a journal.

| Mode | Clock | DataFeed | Broker | Notes |
|---|---|---|---|---|
| **Backtest** | SimClock (event-time, as fast as CPU) | Parquet lake (snapshot-pinned) | SimBroker (cost models, slippage, partial fills, MIS square-off) | Replaces backtester.py *and* replay_engine *and* every study loop |
| **Paper** | WallClock | Broker WS/poller | PaperBroker (same fill logic as SimBroker, fed live LTP) | A paper session **is** a backtest running in real time |
| **Live** | WallClock | Broker WS/poller | ZerodhaBroker — constructible **only** when a signed live-gate file exists | Postponed until edge gate passes |

Consequences:
- **Parity is total.** The strategy, portfolio, risk, and fill logic executed in a backtest are the
  identical code objects executed in paper and live. The F1/F3 divergence class is closed.
- **Every paper session is replayable.** Journal in → same decisions out. "Why did it trade X?" is a
  deterministic re-run, not log archaeology.
- **Testing collapses.** One engine to test. A paper-session incident becomes a backtest fixture the
  same day.

## 2.4 Component specifications

### qe.data — the data platform (adopts NS-1, right-sized)
- Zones: `raw/` (immutable) → `curated/` (Parquet, PIT-correct, unadjusted + adj-factors, ISIN-keyed,
  trust-tiered) → `features/`. `data_snapshot_id` = manifest hash; every run pins one.
- Engine: **DuckDB + Parquet + manifest files now; Iceberg/Glue only when a real multi-writer or
  schema-evolution need appears** (see §2.6 disagreement with NS-1).
- One loader API (`qe.data.load_bars(universe, interval, snapshot_id, as_of)`) used by research,
  engine, and reports. The bespoke per-study loading dies.
- Ingestion: the existing downloaders (`download_bhavcopy.py`, `download_fo_*.py`, Kite fetchers)
  become `qe.data.ingest.*` connectors writing to `raw/` then promoting to `curated/` with DQ checks
  (the s3-parquet-data-quality checks become the promotion gate).

### qe.strategy — strategies as pure functions over market state
- Interface: `on_bar(ctx) -> list[TargetPosition]` (or target weights). No side effects, no broker
  awareness, no paper/live awareness. `paper_trade` stamping disappears — mode is an engine property,
  not a signal field.
- Existing strategy math (`_viability.py`, sizing, regime gate, ORB/VWAP v2 logic) ports as libraries.
- A strategy is *identified* by `(code_version, config_hash)` — the same identity the run registry and
  the promotion gate use.

### qe.portfolio — the missing layer, now first-class
- Consumes all strategies' target positions; nets them into one book-level target vector;
  applies budgets (per-strategy capital, 15/day entry budget as a portfolio rule, not a strategy hack).
- Output: desired book. Execution's job is reconciliation. ADR-028's netting gate and Section-16
  monitor become *assertions* here (violation = bug, not runtime patch).

### qe.risk — a pipeline of pure checks, not a service
- The 11 validators become ordered pure functions: `check(book_delta, state, config) -> Approve|Reject(reason)`.
  Same checks, microsecond latency, exhaustively unit-testable, and — because the engine is
  deterministic — replayable against any journal.
- **Kill switch v2:** a single in-process state machine with one persisted flag (S3/DynamoDB) checked
  before every order emission, plus process-level triggers (loss, staleness, order-rate). No topics, no
  listeners, no self-refire loops. The 4-bug history is retired by construction.
- **Fail-closed live** stays: in Live mode, missing/stale universe, LTP, or margin ⇒ reject. In
  Paper/Sim modes, degrade per config with journal annotation.

### qe.execution — reconciler + idempotent order port
- Diffs current book vs target book → child orders. Idempotency via deterministic order IDs +
  conditional persist (the one DynamoDB pattern that has earned its keep — or SQLite locally).
- `UniverseOrderValidator` remains the final hard gate, now one function call before the broker port.
- TEE (exits) and MIS square-off become engine policies driven by the same clock abstraction —
  which means they are **backtestable by default** (the entire Phase-6 lab machinery becomes "run the
  engine in Sim mode").

### qe.brokers — ports with safety in the type system
- `SimBroker` / `PaperBroker` share fill simulation (cost models included — the three cost models are
  core, not lab, code).
- `ZerodhaBroker.__init__` **requires** a `LiveGateToken` — an object constructible only by a CLI that
  verifies the promotion-gate file, prints the checklist, and takes typed operator confirmation.
  Paper/live isolation stops being an env-var convention and becomes unrepresentable state.
- Token lifecycle (`ZerodhaTokenManager`, DynamoDB sessions table) is kept — it works.

### qe.journal — event sourcing at the right scale
- Append-only, per-session: every bar-in, signal, target, risk verdict (with reason), order, fill,
  kill-state change, config hash, code SHA. JSONL live + Parquet archive to S3.
- This **replaces**: Kafka topics as audit trail, ops.audit, trace-id log-stitching, LiveCounters JSON,
  and most of the monitoring agent. Session report, monitoring TUI, trade replay, decision replay, and
  the Bedrock advisory layer all become *readers of the journal*.

### qe.config — one tree, one hash
- A single pydantic-settings tree loaded from one file + explicit overrides; **frozen and
  content-hashed at engine start**; hash written to every journal record and every report header.
- Session validity = "journal config-hash matches approved config-hash." The entire Sessions-10–15
  invalidation class (stale image, key mismatch, silently-off gates) becomes a one-line check, and
  `validate_session12_runtime.py`-style scripts become unnecessary.

### qe.research — studies as declarations, not scripts
- A `Study` = YAML/py declaration: universe, snapshot, engine config grid, gate spec, baselines.
  The runner executes it through the **same engine**, registers runs, computes the metrics catalog,
  applies pre-registered gates (including DSR/PBO/multiple-testing budget per NS-1 §7), emits report.md.
- The 2026 toolkit (factor studies, walk-forward, forward books, `check_forward_gate.py`) ports into
  this shape. Two consequences: no more per-study harness bugs, and every historical study becomes
  re-runnable against new data snapshots for free.
- Experiment tracker: the existing run-registry pattern (deterministic ID + conditional claim) extended
  with a hypothesis field and family-wise test budget — NS-1 §11 adopted as-is.

### AI, scoped honestly
- **Cut from the hot path.** No ai_engine service, no enrichment hop, no watchdog.
- Quality scorer / regime models live in the research factory; if one demonstrates walk-forward uplift,
  it ships **into the engine as a library function** with a pinned model artifact — same promotion gate
  as a strategy.
- The Bedrock advisory/report layer (event-driven, offline, explain-only) is architecturally correct
  today: keep unchanged. alpha_engine's shadow forecasting folds into the research factory as a
  journal-reader.

## 2.5 Deployment topology

| Environment | v1 (today) | v2 |
|---|---|---|
| Local dev/research | docker-compose: LocalStack + Redpanda + 6 services + setup | `pip install -e . && qe backtest/paper --config session.yaml` — no containers required |
| Paper sessions | EC2 ASGs + MSK + DynamoDB×10 + CloudWatch | **One** t4g/c6g instance (or the laptop): one process, one config, journal → S3 on close |
| Live (future, gated) | same as paper + flags | same as paper + `LiveGateToken` ceremony + margin/universe fail-closed |
| Research at scale | backtest-worker ASG (lab) | same instances, but running `qe study` shards; scale-from-0 Batch/ASG kept from lab design |
| State | DynamoDB ×10+ | SQLite (local) / one small table set: sessions(token), runs, order-idempotency. S3 for everything bulky |
| Messaging | MSK Serverless, 8+ topics | none in trading path; journal on disk/S3. (MSK decommissioned ⇒ largest single cost line deleted) |

CI/CD simplifies accordingly: one package, one image (optional), no per-service change-detection matrix,
no image-staleness class of bug (the engine prints its git SHA + config hash into the journal at boot —
staleness is self-evident rather than silently invalidating).

## 2.6 Where RA-1 disagrees with NS-1 (explicit, so it can be adjudicated)

1. **NS-1: "the live/paper execution platform is already production-grade — do not rebuild."
   RA-1: rebuild it.** The evidence ledger (F-2, F-3, F-8) shows the stack is production-*hardened*
   but not production-*sound* for its actual mission, and its complexity taxes the research mission.
   NS-1 was right to protect it *from the data platform's scope*; it is wrong as a permanent verdict.
2. **NS-1: Iceberg + Glue as the curated-layer foundation now. RA-1: Parquet + manifests + DuckDB now;
   Iceberg when (a) a second concurrent writer, (b) schema-evolution pain, or (c) >5TB/table appears.**
   For a solo operator, Iceberg adds catalog infrastructure, commit semantics, and a Glue dependency
   before any of its problems exist. The migration is additive later (Iceberg can adopt existing
   Parquet); adopting it now is complexity-forward. Everything else in NS-1 §2 (zones, WORM raw,
   ISIN keys, bitemporal knowledge-time, snapshot manifests, PIT reads) is adopted unchanged.

## 2.7 Trade lifecycle & research lifecycle (end-state)

**Research lifecycle:** hypothesis (pre-registered, test-budget debited) → `qe study` on pinned
snapshot → gates (cost-stack, baselines, walk-forward OOS, DSR/PBO) → if pass: paper candidacy review
(HUMAN) → engine Paper mode N sessions (same code) → promotion gate report (HUMAN) → live pilot
(HUMAN + LiveGateToken). Any fail → retirement register with reason. This formalizes exactly the
process that worked in 2026 — but on one harness.

**Trade lifecycle (paper/live):** bar → strategies emit targets → portfolio nets → risk pipeline
approves delta (or rejects with journaled reason) → reconciler emits idempotent orders → broker port →
fills update book → TEE/MIS policies manage exits → EOD: square-off check, reconciliation
(journal vs broker), session report auto-generated from journal → journal archived to S3.

---

# PHASE 3 — Migration Map

## 3.1 Disposition of every major asset

| Asset | Disposition | Rationale | Effort | Risk |
|---|---|---|---|---|
| Governance (gates, wall, CLAUDE.md safety rules, retirement register, forward gate) | **KEEP verbatim** | Already institutional | — | — |
| Cost models (Indian/Options/Futures) | **KEEP** → `qe.costs` | Proven, load-bearing | S | Low |
| Parquet lake + downloaders + DQ checks | **KEEP/refactor** → `qe.data` | The durable asset | M | Low |
| Lab run-registry/checkpoint/walk-forward/metrics designs | **REFACTOR** → `qe.research` | Right ideas, wrong harness count | M | Low |
| Strategy math (viability, sizing, ORB/VWAP v2, regime gate) | **REFACTOR** → `qe.strategy` (pure) | Keep logic, drop service scaffolding | M | Med (behavior-parity tests required) |
| backtester.py + replay_engine + study loops | **REWRITE as one** → `qe.engine` (SimClock) | F-3; four semantics → one | L | Med — mitigated by parity replays |
| risk_engine validators | **REFACTOR** → `qe.risk` pipeline | Same checks, as pure functions | M | Low |
| Kill switch | **REWRITE** (state machine + persisted flag) | F-8; 4-bug history | S | Low |
| execution_engine (order mgr, idempotency, universe gate, TEE, MIS, brokers) | **REFACTOR heavily** → `qe.execution`/`qe.brokers` | Keep broker adapters + idempotency pattern + TEE/MIS logic; drop Kafka shell | L | Med |
| ai_engine (service) | **REMOVE** from hot path; models → research factory | F-4 | S | Low |
| alpha_engine | **FOLD** into research factory (journal reader) | Advisory by design already | S | Low |
| data_ingestion service | **REFACTOR** → in-process DataFeed + `qe.data.ingest` | No standalone service needed | M | Med (WS handling is subtle — port the asyncio fixes' lessons) |
| Kafka/MSK + topics + LocalStack/Redpanda dev stack | **REMOVE** (after cutover) | F-2, F-7 | M (decommission) | Low once shadow-validated |
| DynamoDB tables (10+) | **SHRINK** to sessions/runs/idempotency; rest → SQLite/S3/journal | F-7 | M | Low |
| monitoring_agent + LiveCounters + monitor scripts | **REWRITE small** as journal readers | Journal is the single source | M | Low |
| Bedrock advisory layer | **KEEP** (point it at journal/runs) | Correct already | S | Low |
| Terraform | **PRUNE** to S3 + minimal DynamoDB + 1 EC2 profile + lab ASG | Follows the above | M | Low |
| Tests (41k LOC) | **PORT selectively** | Keep strategy-math, cost, risk-rule, idempotency tests; drop stack-plumbing tests | M | — |
| Repo dupes, .docx in root, "file 2.md" copies | **REMOVE/archive** | F-9 | S | — |

Effort scale: S ≈ a session or two · M ≈ ~a week of sessions · L ≈ 2–4 weeks of sessions (solo + Claude).

## 3.2 Migration principles

1. **Strangler, not big-bang.** The old stack keeps running paper sessions until the new engine has
   proven parity in shadow. No capability gap days.
2. **Parity is the acceptance test.** The new engine must reproduce (within documented tolerance) the
   lab's registered runs and at least two archived paper sessions replayed from journals/logs before
   it may host a session.
3. **Research first.** The first user of `qe.engine` is the research factory (backtests), not paper
   trading — value lands immediately (monthly forward-book runs get a better harness) while risk stays
   at zero.
4. **The wall never moves.** At every intermediate state, live remains blocked, promotion stays human,
   and the advisory layer stays advisory.

## 3.3 Dependency order

`qe.config + qe.journal` → `qe.data` (loader over existing lake) → `qe.engine` (SimClock) + `qe.costs`
→ `qe.strategy` ports → `qe.portfolio` + `qe.risk` → parity vs lab runs → `qe.research` (studies,
forward books) → DataFeed + PaperBroker → shadow paper sessions → cutover → decommission → (much
later, gated) LiveGateToken + ZerodhaBroker live mode.

---

# PHASE 4 — Implementation Roadmap (each milestone ships a working system)

**M0 — Decision & freeze (1 session).**
Operator adjudicates RA-1 (including the two NS-1 disagreements). ADR written. Feature-freeze the v1
trading stack (bugfixes only). Repo hygiene sweep (F-9). *Working system: v1, unchanged.*

**M1 — Skeleton + config + journal (≈1 week).**
`qe/` package; typed frozen hashed config; journal writer/reader; `qe.data` loader over the existing
Parquet lake with snapshot pinning. Acceptance: a "null engine" runs a date range from the lake and
produces a journal with config-hash + code-SHA stamped. *Working system: v1 + a verifiable data spine.*

**M2 — SimClock engine at parity (≈2–3 weeks).**
Engine loop, SimBroker with cost models, one ported strategy (delivery factor book is ideal — it is
the live experiment), portfolio constructor, risk pipeline (subset), metrics. Acceptance: reproduces
the registered forward-book replay results and one lab walk-forward run within tolerance; monthly
forward-book cadence switches to `qe study`. *Working system: research factory v2 in production use.*

**M3 — Full research-factory port (≈2 weeks).**
Remaining risk checks, TEE/MIS as engine policies (validated against Phase-6 lab outputs), study
declarations for the retained toolkit, experiment tracker + test-budget, report generator. Acceptance:
one legacy study re-run end-to-end declaratively; retirement/gate reports auto-generated.
*Working system: all future research on one harness.*

**M4 — Real-time modes in shadow (≈2–3 weeks).**
WallClock, DataFeed (broker WS/poll), PaperBroker, kill-switch v2, monitoring TUI over live journal.
Run ≥3 shadow sessions: v2 engine alongside the v1 stack on the same market days; diff journals vs v1
fills/decisions; reconcile every divergence to a root cause. Acceptance: divergences explained, safety
gates demonstrated (kill drill, staleness drill, MIS drill) in paper. *Working system: two independent
paper stacks, evidence accumulating.*

**M5 — Cutover + decommission (≈1 week + a cooldown).**
v2 becomes the paper stack (counting sessions per existing validity rules — config-hash makes validity
mechanical). After N clean sessions: decommission MSK, ai_engine, surplus DynamoDB tables, LocalStack
dev stack; prune Terraform; archive v1 code under `archive/v1/`. Acceptance: monthly AWS bill drop
visible; operator runbook is one page. *Working system: QuantEmbrace v2, materially cheaper and simpler.*

**M6 — Live readiness (UNSCHEDULED — gated, not planned).**
Only if/when the forward factor gate (~Dec-2026) or a future validated edge passes human review:
LiveGateToken ceremony, fail-closed drills, tiny-capital pilot per existing pre-live runbook. The
architecture will be ready; the *decision* remains evidence-driven and human. *No date by design.*

Total to M5: roughly **8–11 weeks of solo-operator sessions**. The payback begins at M2 (research on
the unified engine), not at the end.

---

## Appendix A — Engineering principles applied (scorecard)

| Principle | v1 | v2 |
|---|---|---|
| Simple over clever | Kafka microservices for 15 orders/day | one process, one journal |
| Deterministic over magical | watchdogs, races, drift | replayable event loop |
| Observable over opaque | logs across 6 services + trace-id stitching | journal = ground truth |
| Research velocity | per-study bespoke harnesses | declared studies on one engine |
| Production-ready | hardened but drift-prone | parity + config-hash validity |
| Institutional robustness | policy-enforced safety | type/process-enforced safety |

## Appendix B — What would change this design

- A validated **intraday** edge requiring sub-second reaction at scale → revisit in-process Python
  (likely: keep architecture, optimize the hot loop or split the data feed out).
- Multiple concurrent operators/writers → revisit Iceberg + a real service boundary.
- Multi-account / OMS-level allocation → promote qe.portfolio to its own deployable.
None of these are current facts; none justify pre-building.
