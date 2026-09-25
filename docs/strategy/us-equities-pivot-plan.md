# US Equities Pivot — QuantConnect Alpha, QuantEmbrace Execution

_Plan-of-record · Approved 2026-07-09 · ADR-041 (`memory/decisions.md`)_

**Status: PLAN ONLY — Phase 0 (this document + ADR-041) is the only completed step.
Every subsequent phase writes a report and stops for human approval (standing protocol).**

---

## 1. Context

The NSE research program is exhausted: the consolidation memo
(`docs/strategy/research-program-consolidation-2026-06-20.md`) eliminated every strategy
family — intraday, gap, calendar, overnight, positional factors, factor combos, options-vol,
event-vol, futures-trend — after costed out-of-sample scrutiny. The standing posture is
"accrue forward factor books, deploy nothing," with the pre-registered Forward Factor Gate
eligible ~Dec-2026 (currently 6/12 months).

Operator decision 2026-07-09: shift research focus to **US equities**, using **QuantConnect
(QC) as the strategy/alpha source** and **QuantEmbrace as the execution, risk, and infra
platform**.

### Operator decisions (confirmed 2026-07-09)

1. **Integration model — port into qe.** QC/LEAN is the research bench; winning strategy
   logic is ported as `qe/strategy/` implementations. One deterministic engine (ADR-037
   preserved), paper==sim parity, all safety gates apply. No LEAN in the live loop, no
   QC-cloud-direct-to-Alpaca.
2. **Scope — positional first.** Daily-to-monthly rebalance strategies only (dual momentum,
   tactical asset allocation, sector/ETF rotation, factor tilts). No intraday in wave 1 —
   qe has no bar-by-bar surface, and intraday is where every NSE edge died.
3. **NSE forward books continue in parallel.** The monthly qe cadence for the delivery and
   momentum books keeps running unchanged (6/12 months accrued, gate ~Dec-2026).
4. **Budget — $0, LEAN CLI local only.** No QC cloud subscription, no paid data.
   Consequence: strategy universes must be **ETFs and mega-caps**, where survivorship bias
   in free data is structurally minimal. We build our own curated US EOD lake and feed the
   *same data* to both LEAN and qe (a parity feature, not a workaround).
5. **Broker path — Robinhood now, Alpaca later** (operator amendment, same day). The
   near-term brokerage home is **Robinhood**, including its AI add-ons; the Alpaca
   deterministic API adapter is deferred to a later stage. See "Broker path" below.

### Broker path (amended 2026-07-09): Robinhood now, Alpaca later

Verified state of Robinhood's programmatic surfaces as of 2026-07:

- **Agentic Trading (beta, launched 2026-05-27):** third-party AI agents connect via
  **Robinhood's official MCP server** (one URL in the agent's MCP config) to a **dedicated,
  separately-funded "agentic" account**, segregated from the primary account. Equities-only
  in beta (options/crypto/futures announced as coming); gradual rollout via email invite;
  desktop-only onboarding; every agent trade is notified in-app and some require an explicit
  human approval preview. This is the **only official programmatic equities surface**
  Robinhood offers.
- **Cortex:** Robinhood's native in-app AI assistant (digests, research, trade builder) —
  a consumer feature for operator-side discretionary research, not an integration API.
- **No official equities REST API exists.** Unofficial private-API wrappers (robin_stocks
  and similar) are unsupported, ToS-violating, and fragile — **never used, at any phase.**

**How this fits QuantEmbrace:**

- Phases 1–5 are broker-independent (research, backtest, and paper all run in-engine), so
  the broker swap changes nothing before Phase 6 except the Phase 0 prerequisites.
- The Robinhood agentic account maps naturally onto our governance: qe **recommends** target
  orders → a human (or a Claude session connected to Robinhood's MCP, supervised) **relays**
  them into the dedicated agentic account → Robinhood's own preview/notification layer keeps
  the human in the loop → the agentic account's funding is a **hard capital cap**.
- The agentic MCP surface is **not** a qe `LiveBroker` adapter: it is beta, has no paper
  sandbox, and offers no deterministic order-lifecycle guarantees (idempotency, status
  polling) that the `LiveBroker` port requires. Fully automated execution therefore remains
  an **Alpaca-later** milestone (Phase 6b) — Alpaca's paper/live API is still the reference
  target for the deterministic adapter.
- **Eligibility (hard Phase 0 prerequisite):** Robinhood requires a US residential address
  and US citizen / permanent-resident / valid-visa status physically in the US — accounts
  cannot be opened from India. The operator must confirm personal eligibility before any
  Robinhood-dependent step; if ineligible, the broker path reverts to Alpaca (which accepts
  international accounts) and this amendment is void.

## 2. Current-state findings (codebase exploration, 2026-07-09)

- **Two platforms exist.** The v1 Kafka stack (frozen fallback, ADR-037/038) contains a
  genuinely complete Alpaca path: `services/execution_engine/brokers/alpaca_broker.py`
  (813 lines — full order lifecycle, rate limiting, paper/live split),
  `services/data_ingestion/connectors/alpaca_connector.py`, the `ticks.us` Kafka topic,
  dual-broker routing in the execution service, ~25 unit tests. It carries one **open bug
  (ALPACA-FIX: enum `.value` handling)** and has never run against a real account.
- **The active qe v2 engine has zero US awareness.** `qe/costs.py` hardcodes the NSE
  statutory stack unconditionally; `qe/clock.py` is IST-only; `qe/universe.py` and the
  benchmark are NIFTY-only; the Parquet lake holds only `market=NSE`.
  `UniverseConfig.market: "US"` would validate but is silently ignored downstream.
- **qe's architecture is market-agnostic in shape** — frozen content-hashed config,
  SimClock/WallClock, SimBroker → PaperBroker → double-gated LiveBroker (unconstructible
  without a `LiveGateToken`), journal, walk-forward harness. Right foundation; every
  concrete plug-in is NSE.
- **Strategy attachment point:** `qe/strategy/base.py` — the `Strategy` protocol:
  `rebalance(ctx: Context) -> dict[str, float]` (point-in-time panel in, target weights
  out; no-lookahead by construction). Adding a strategy = implement the protocol + widen
  `StrategyConfig.kind` + extend `_build_strategy` in `qe/engine/sim.py` and
  `qe/engine/paper.py`.
- **Safety gap found:** the v1 `UniverseOrderValidator`
  (`services/shared/universe/order_validator.py:89-101`) explicitly **bypasses all non-NSE
  markets** ("allow with warning") — must be closed before any US order path is
  live-capable.
- **No prior QC research artifact exists in the repo** (the 2026-06-11 shelving verdict was
  never written down). This plan starts fresh; the operator's re-raise lifts the hold.

## 3. Invariants (unchanged, extended to US)

- Backtesting **recommends**; a **human promotes**; live is **BLOCKED by construction**
  (M6 `LiveGateToken`; US gets its own preconditions).
- **QC "proven" strategies are hypotheses, not edges.** QC library strategies are
  educational implementations (the Alpha Streams marketplace itself was discontinued);
  every candidate must re-earn its claim on our curated data with the full US cost stack,
  out-of-sample. Expect the NSE funnel again: most candidates should die in validation —
  that is the system working.
- **Full US cost stack mandatory.** Zero commission ≠ zero cost: SEC Section 31 fee
  (sells), FINRA TAF, half-spread + slippage, borrow cost if ever short. India-side
  operator economics (LRS remittance, TCS, US dividend withholding) are documented at the
  ADR level, not modeled in-engine.
- **Free data = LOW trust** → quarantine → cross-source validation → curated lake
  (existing `docs/backtesting/aws-data-lake-contract.md` discipline).
- **Extend qe; never revive v1 for US.** v1's Alpaca code is a porting reference only. The
  frozen-fallback and decommission-gate status of v1 is untouched.
- The NSE monthly cadence and the Dec-2026 Forward Factor Gate run unchanged in parallel.

## 4. Phases

### Phase 0 — Decision record + operator prerequisites (docs only) ✅ this document
- ADR-041 written to `memory/decisions.md`; this plan-of-record committed.
- **Operator checklist (outside repo, no code):**
  - **Confirm Robinhood eligibility** (US residential address + citizen/PR/valid US visa —
    not openable from India). If eligible: open the primary account, request the
    **Agentic Trading beta** (rollout is invite-by-email; onboarding is desktop-only), and
    optionally Gold for **Cortex**. If ineligible: broker path reverts to Alpaca.
  - W-8BEN + funding route (LRS if remitting from India) + intended capital. Note the PDT
    rule (margin account < $25k restricts day-trades — irrelevant for positional books,
    documented for completeness).
  - **Alpaca account: deferred** — needed only when Phase 6b (deterministic LiveBroker
    adapter) approaches.
  - Install Docker + LEAN CLI locally (`pip install lean`, `lean init`).
- `CLAUDE.md` + `architecture/system_design.md` posture sections update only when Phase 3
  changes engine behavior (per doc-update rules), not now.

### Phase 1 — US EOD data lake (curated, $0)
- New downloader `scripts/backtest/download_us_eod.py` following the
  `download_bhavcopy.py` / `download_fo_bhavcopy.py` pattern: daily OHLCV + adjusted closes
  for (a) a fixed ETF set (~20: SPY, QQQ, IWM, sector SPDRs, TLT, IEF, GLD, EFA, EEM,
  SHY…) and (b) current US mega-caps (~50–100), 2005→present where sources allow.
- **Two independent free sources cross-validated** (e.g., Stooq + Tiingo/Yahoo; Alpaca IEX
  API as a third check for recent years) — same LOW-trust → quarantine → promote
  discipline as the NSE lake. Corporate-action handling via adjusted series; log
  split/dividend adjustment deltas between sources.
- Extend the lake layout to `market=US, segment=EQ` Parquet partitions; extend `qe/data`
  loading + `data_snapshot_id` pinning for US panels.
- **Deliverable:** lake QA report (gaps, cross-source deltas, adjustment audit).
  **Stop for approval.**

### Phase 2 — Candidate selection + screening (LEAN local + project-native screens)
- `lean init` a local LEAN workspace; write a converter exporting the curated lake to LEAN
  custom-data format (the same data feeds both engines).
- Curate **5–8 candidates** from the QC Strategy Library / open-source community with hard
  criteria: positional (daily–monthly rebalance), ETF/mega-cap universe, ≥15 yr
  backtestable, economically motivated, low turnover, long-only or defined-risk. Likely
  families: Antonacci dual momentum (GEM), Faber tactical asset allocation, sector
  rotation, 12-1 cross-sectional momentum on mega-caps, low-vol tilt, Keller-style
  adaptive allocation.
- **Pre-register a screen gate before running** (the O-1/F1 discipline): e.g.,
  net-of-cost Sharpe ≥ 0.8, positive in ≥70% of years, maxDD ≤ benchmark's, beats SPY
  buy-and-hold risk-adjusted, survives ±25% parameter perturbation. Screens run both in
  LEAN (fidelity to QC originals, realistic fee/slippage models) and as project-native
  pandas study scripts (`run_us_rotation_study.py` style) for cross-checking.
- **Deliverable:** `docs/strategy/us-qc-candidate-report.md`, shortlist **2–3** strategies.
  **Stop for approval.**

### Phase 3 — qe engine: US market support (code)
- **Costs:** `USEquityCosts` in `qe/costs.py` (commission $0, SEC fee on sells, FINRA TAF,
  half-spread + slippage bps) + a market-dispatched cost model in `qe/engine/sim.py` /
  `qe/engine/paper.py` (NSE behavior byte-identical — the existing ₹0.00 parity tests must
  stay green).
- **Clock/calendar:** market-aware timezone in `qe/clock.py` (`America/New_York`,
  09:30–16:00, NYSE holidays + half-days) for SimClock and WallClock;
  month-end/rebalance-due logic per market calendar.
- **Universe + benchmark:** static config-listed US universe path in `qe/universe.py`
  (explicit symbol list from `RunConfig` — simpler and safer than the NSE dynamic
  universe); SPY (or EW basket) benchmark in `qe/research/benchmark.py`.
- **Config plumbing:** honor `universe.market`; add `currency` (USD NAV, `seed_nav` in $,
  2-dp cent rounding); widen `StrategyConfig` (`kind`, params,
  `rebalance: weekly|monthly`); new `configs/qe_us_*.yaml`.
- Tests mirroring the M1/M2 pattern: US costs, US calendar, config hashing, NSE regression
  untouched. **Stop for approval.**

### Phase 4 — Port shortlisted strategies + validation in qe
- Implement each shortlisted strategy against the `Strategy` protocol in `qe/strategy/`;
  register in `StrategyConfig.kind` + both `_build_strategy` dispatchers.
- **Cross-engine parity:** `qe study` vs LEAN backtest on the identical data snapshot,
  within a pre-declared tolerance (engines differ; document residuals). Then
  **walk-forward OOS** via the existing `qe/research/walkforward` harness.
- **Pre-register a US Forward Gate** (mirror of the `check_forward_gate.py` params:
  ≥12 months, IR ≥ 0.50, cum-alpha > 0 vs SPY, ≥58% positive-alpha months, maxDD ≤
  benchmark) — declared *before* any forward accrual starts. Do not relax it later.
- **Deliverable:** validation report; human decides which US book(s) to stand up.
  **Stop for approval.**

### Phase 5 — US forward paper book(s) via `qe paper`
- Stand up `configs/qe_us_<strategy>_book.yaml` + `_paper.yaml`; prove **paper==sim to
  $0.00** for the US book (the three-clocks invariant, as done for NSE in M4).
- Fold into the operator cadence: lake refresh → `qe study` (NSE + US books) → gate check
  → `qe paper` → `qe report`. Update `docs/runbooks/qe-operator-runbook.md`.
- Accrue forward months against the pre-registered US gate. **This phase is calendar-time
  (months), by design.**

### Phase 6 — Live enablement path (much later, fully gated — listed for completeness)

Both sub-phases require the same evidence first: US forward gate PASS + ≥N clean US paper
sessions + human sign-off. Live remains **BLOCKED by construction** until then.

**6a — Robinhood agentic execution (human-in-the-loop, first live surface):**
- qe emits recommended target orders (journal-recorded); the operator — or a supervised
  Claude session connected to **Robinhood's official Agentic Trading MCP** — relays them
  into the **dedicated agentic account**; Robinhood's preview/notification layer keeps the
  human approval in the loop; the agentic account's funding is the hard capital cap.
- qe-side guards still apply *before* anything is relayed: kill-switch check + explicit
  US symbol whitelist + order-size caps. The relay step is recommend-and-approve, not
  autonomous execution — consistent with "backtesting recommends, a human promotes."
- Never via unofficial private-API wrappers (robin_stocks etc.) — official MCP only.

**6b — Alpaca deterministic adapter (full automation, later still):**
- Fix the **ALPACA-FIX** enum bug; port `AlpacaBroker` (from v1, as reference) into a qe
  `LiveBroker` adapter behind the existing `LiveGateToken` double gate.
- Close the **US universe-validation bypass**: enforce an explicit symbol whitelist in qe
  execution for US books (and fix v1's `order_validator.py` bypass if v1 ever executes US,
  which is not planned).
- Justified only if 6a proves the strategy *and* the manual relay becomes the bottleneck.

## 5. Explicitly out of scope

- Intraday US strategies / a bar-by-bar qe engine surface (wave 2 at earliest, only if a
  positional book proves out).
- Unofficial Robinhood private-API wrappers (robin_stocks and similar) — never, at any
  phase (ToS-violating, unsupported, fragile).
- Paid QC subscription or paid data (revisit only if the free path demonstrably blocks a
  shortlisted candidate).
- Any change to the NSE forward cadence, the Dec-2026 gate, or the v1 decommission gate.
- Any Fargate/EKS/Lambda redesign; any live-trading enablement.

## 6. Verification (per phase)

- Every phase ends with a written report + stop for human approval (standing protocol).
- Phase 1: cross-source delta audit within declared tolerance; gap/adjustment QA
  (s3-parquet-data-quality-style checks).
- Phase 3: full qe test suite green, including the unchanged NSE ₹0.00 parity tests; new
  US cost/calendar unit tests.
- Phase 4: qe-vs-LEAN parity within a pre-declared tolerance; walk-forward OOS gate
  results reported honestly, pass or fail.
- Phase 5: paper==sim $0.00 proof for the US book before any forward accrual counts.

## 7. Key files (representative)

- **New:** `scripts/backtest/download_us_eod.py` · `qe/strategy/<ported_strategies>.py` ·
  `configs/qe_us_*.yaml` · `docs/strategy/us-qc-candidate-report.md`
- **Modified:** `qe/costs.py` · `qe/clock.py` · `qe/universe.py` · `qe/config.py` ·
  `qe/engine/sim.py` · `qe/engine/paper.py` · `qe/research/benchmark.py` ·
  `docs/runbooks/qe-operator-runbook.md` (Phase 5)
- **Reference only (not revived):** `services/execution_engine/brokers/alpaca_broker.py` ·
  `services/data_ingestion/connectors/alpaca_connector.py`
