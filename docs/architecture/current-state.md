# QuantEmbrace — Current State (Phase 0 Reconnaissance)

> Snapshot as of commit `d8ca741` (2026-09-25), branch `feature/hybrid-ai-research`.
> This document is descriptive: it records what the code does today, not what it should do.
> Line numbers are accurate at that commit. The findings in §10 are **documented only, not fixed**:
> v1 is feature-frozen (ADR-037) and live trading is blocked on both stacks.
> Diagrams: [current-state-diagram.md](current-state-diagram.md).

---

## 1. Two planes, one repository

| Plane | Location | Status | What it runs |
|---|---|---|---|
| **v1 trading stack** | `services/` (67.6k LOC, 289 files) | **Frozen** (ADR-037) — bug fixes only; fallback until the decommission gate passes | Kafka intraday pipeline: `data_ingestion → strategy_engine → ai_engine → risk_engine → execution_engine`. Its intraday strategies are retired (ADR-033/034). |
| **v2 engine `qe`** | `qe/` (4.1k LOC, 42 files) | **Primary** research + paper path (ADR-038 cutover) | One deterministic process driven by three clocks. Runs the monthly forward factor books (NSE delivery and momentum) and US RPLITE (built but not activated). |
| Research tooling | `scripts/` (24.8k LOC) | Active | Study scripts, downloaders, gate checkers |
| Backtesting lab | `services/backtesting/` (5.8k LOC) | Built; mostly superseded by `qe study` | Replay engine, walk-forward, TEE/MIS simulation, dataset builder, dormant GenAI layer |
| Tests | `tests/` (42.7k LOC) | `tests/qe` = 89 tests, all passing (0 skipped) | — |

**Operating posture** (from `docs/strategy/research-program-consolidation-2026-06-20.md`, the capstone memo):
- No strategy has a validated deployable edge.
- The two NSE forward factor books accrue evidence against a pre-registered gate, eligible around Dec-2026.
- Live trading is BLOCKED.

---

## 2. v1 service map (frozen)

| Service | Reads | Writes | Notes |
|---|---|---|---|
| `data_ingestion` | Kite and Alpaca WebSockets | Kafka `ticks.*`; DynamoDB `candle-cache`, `features` | Builds a full `ZerodhaBrokerClient` just for the candle stream (`services/data_ingestion/service.py:676`) |
| `strategy_engine` | `ticks.*`, `candle-cache`, `strategy-config` | `signals.pending` | Six strategy classes are still registered as runners (`service.py:1193-1310`). Whether they run is gated only by the DynamoDB `enabled` flag. |
| `ai_engine` | `signals.pending`, `features`, S3 models | `signals.enriched`, `regime-log`, `strategy-recommendations` | **Not deployed in prod.** HMM and GBT models are stubs (§6). |
| `risk_engine` | `signals.enriched` or `signals.pending` (EnrichmentWatchdog fallback), `orders.events` | `signals.approved`, `ops.audit`, kill-switch topic | 11 validators |
| `execution_engine` | `signals.approved` | Broker APIs, `orders.events`, DynamoDB | `service.py` is 3,046 lines |
| `alpha_engine` | `candle-cache` | `alpha.opportunities` only (allowlist) | Shadow forecasting, advisory only (ADR-031) |
| `monitoring_agent` | Docker, logs, counters | Slack, reports | Journal readers replace it in v2 |

Deployment: EC2 ARM64 ASGs, MSK Serverless, and 13 DynamoDB tables (`architecture/system_design.md`). Development runs on docker-compose with LocalStack and Redpanda.

## 3. v2 `qe` map (primary)

| Module | Role |
|---|---|
| `qe/config.py` | Frozen pydantic `RunConfig` (`extra="forbid"`). `config_hash = sha256(canonical JSON)`. The hash is the session identity. |
| `qe/journal.py` | Append-only JSONL, one file per session. Monotonic `seq`; `SESSION_START` must be first. Opened with mode `"x"`, so no appends across processes. |
| `qe/clock.py` | `SimClock`, `WallClock`, and per-market timezone and close time (NSE/US) |
| `qe/data/{lake,panel,snapshot,feed}.py` | Read-only Parquet lake. Point-in-time panel. Content-hashed snapshot manifests (`ds-<16hex>`). |
| `qe/strategy/{base,factor_book,risk_parity}.py` | Pure strategies: `rebalance(Context) → target weights`. `Context.at(panel, pos)` is the PIT slice. |
| `qe/portfolio.py` | `net_targets`, `size_targets` |
| `qe/risk.py` | Ordered pure checks: `long_only`, `weight_cap`, `gross_exposure`, `cash_non_negative`, `max_positions`, `max_turnover`. A check that crashes counts as a rejection. |
| `qe/execution.py` | `SimBroker`. `PaperBroker(SimBroker)` has no `place_order`. `LiveBroker` needs a `LiveGateToken` **and** an explicit client, and has no order methods. |
| `qe/engine/core.py` | `execute_rebalance`: the single rebalance step shared by sim and paper |
| `qe/engine/{sim,paper,book_store,null_engine}.py` | The three clocks. Paper resumes from `BookState`, which fails closed when the config hash drifts. |
| `qe/killswitch.py` | One in-process state machine plus one persisted JSON flag. Drawdown and staleness triggers. |
| `qe/live_gate.py` | Six evidence checks, all fail-closed. `qe live` refuses today (5 of 6 fail). |
| `qe/research/*` | Studies, walk-forward, metrics, pre-registered gates, experiment registry (`governance/experiment-registry.jsonl`) |
| `qe/reporting/session_report.py` | Monitoring built entirely from the journal |
| `qe/livecheck/drills.py` | Fail-closed drills: staleness, kill, config drift |
| `qe/cli.py` | `python -m qe null|study|paper|kill|report|live|drill`. It imports `qe.engine` at module load (line 17). |

**Invariants.**
- `qe` imports nothing from `services/`.
- The parity tests pin paper == sim to ₹0.00 (`tests/qe/test_parity_delivery_book.py`, `test_paper_engine.py`, `test_us_*`).

## 4. Data and storage

**Lake** (`backtest-data/lake/`, gitignored):

| Dataset | Coverage |
|---|---|
| NSE EQ daily | 3,476 symbols, 2016–2026 |
| NSE intraday | Some symbols, 1m/5m/15m |
| NSE INDICES | `NIFTY50` and `INDIAVIX` daily, 2020–2025 |
| US EQ | 97 symbols, 2005–2026 |
| Futures | NIFTY and BANKNIFTY |
| Options | NIFTY |

Snapshots (`_snapshots/ds-*.json`) pin datasets by content hash.

**What does not exist:**
- **No news, sentiment, fundamentals, filings or earnings data.** `fetch_earnings_calendar.py` exists, but its output does not.
- **Point-in-time is bar-timestamp ≤ `as_of` only.** There is no knowledge-time or publish-time column.

**Other stores:**
- `journals/` (qe JSONL, gitignored)
- `reports/qe/<session>/{summary.json,report.md}` (gitignored). `qe/live_gate.py:97` and `scripts/paper/check_forward_gate.py` read these as **gate evidence**.
- `governance/experiment-registry.jsonl` (committed)
- `backtest-data/paper_book/` (book state, kill flag)

## 5. Strategy lifecycle, as it exists

**There is no lifecycle state in code.** Lifecycle exists only in documents:
- **QE Promotion Score bands** (`docs/strategy/qe-phase-next-cio-operating-doc-2026-06-15.md`): PRODUCTION ≥80, PRE-PRODUCTION 65–79, WATCH 45–64, KILL <45 or any veto.
- **Retirement register** (`docs/strategy/strategy-retirement-register-2026-06-19.md`): RETIRE / PARKED / KEEP, each with a cause-of-death memo.
- **Research lifecycle** (RA-1 §2.7): pre-registered hypothesis → `qe study` → gates → human paper-candidacy review → paper → human promotion → gated live pilot.
- **Forward Factor Gate** (`scripts/paper/check_forward_gate.py`) and the **US Forward Gate** (`check_us_forward_gate.py`).
- **v1 universe modes:** `PAPER_SAFE_START → PAPER_EXPAND → LIVE_ADVANCED` (`configs/promotion_gates.yaml`, intraday).
- **Experiment registry** (`qe/research/registry.py`): an `experiment_id` derived from name and hypothesis, plus a family multiple-testing count. Only walk-forward studies register.

## 6. AI/ML inventory

| Component | Reality |
|---|---|
| `services/ai_engine` RegimeClassifier (HMM) and SignalQualityScorer (GBT) | **Stubs.** There are no trained artifacts anywhere and no training code. `hmmlearn`, `lightgbm` and `xgboost` are not installed. `ModelRegistry` falls back to `DummyClassifier`. Paper sessions saw flat output (`regime=unknown`, `quality=0.5`). `model-dataset-spec.md` §9: "no training pipeline; HMM strictly later". |
| `services/ai_engine/agents/strategy_selector.py` | An Anthropic-SDK advisory agent (Claude 3.5 Haiku) that writes DynamoDB recommendations. No tests. No API key is wired (env var only). |
| `services/backtesting/genai/` | Provider protocol (Bedrock, Anthropic, Stub), versioned prompts, guardrails (secret and forbidden-action regexes), 11 offline tests. **"Code-complete and dormant."** Known gaps: no PEM/JWT patterns; the citation check is cosmetic; prompt-injection defense is a regex scan only. |
| `services/alpha_engine` | Shadow forecaster. A topic allowlist (`alpha.opportunities` only) is enforced before the producer exists. An isolation test asserts no broker SDK is imported. |
| `qe` | No AI. RA-1 F-4: AI is cut from the hot path. Models may return only as research-proven library functions after human promotion. |

## 7. PAPER/LIVE controls

| Control | Where enforced | Notes |
|---|---|---|
| `paper_trade` signal field | Stamped at `strategy_runner.py:294` and routed at `execution_engine/service.py:2126` | **Defaults to `False` (live) when the field is missing on deserialize** — see F-1 |
| `EXECUTION_PAPER_TRADING` | `settings.py:567` (default True) | Consulted by MIS, the position monitor, exit mode and reconciliation. **Not consulted on the entry path.** |
| `QE_EXECUTION_LIVE_TRADING_ENABLED` | Read directly from the environment in 6 places (preflight, validators, safe-actions); blocked by the `scripts/hooks/live_trading_gate.sh` hook | **Not a settings field.** `service.py:542` therefore always gets `False`. |
| `RISK_PROFILE` | pydantic default `"tiny-live"` (`settings.py:226`); direct env reads default to `"paper"` | Inconsistent defaults — F-4 |
| `UNIVERSE_MODE` | `execution_engine/service.py:441-469` | Live: fail-closed if there is no snapshot. Paper: the validator may be `None` and is then skipped. |
| Kill switch (v1) | `service.py:857-865`, `:1100`, Kafka listener, DynamoDB poll | Exit and protective paths may send while the kill switch is active (by design, for flattening) |
| `LiveGateToken` (v2) | `qe/live_gate.py`, `qe/execution.py:141-154` | Six fail-closed evidence checks. `LiveBroker` cannot be constructed without the token and an explicit client, and is never constructed at runtime. |
| Kill switch (v2) | `qe/killswitch.py`; checked in `qe/engine/paper.py` and `core.py:91-97` | Idempotent. Checked after risk, before any fill. |

## 8. Broker reachability

**v2 (`qe`):**
- There are no broker SDK imports.
- The only fill path is `execute_rebalance` → `SimBroker/PaperBroker.rebalance_fill`, a pure simulation.
- `LiveBroker` has no order surface.
- **Result: nothing in `qe` can reach a venue.**

**v1.** Every real order leaves through two adapters:
- `zerodha_broker.py:288/316` (`kite.place_order`)
- `alpaca_broker.py:258/280` (`submit_order`)

Neither adapter checks paper or live mode itself.

**The v1 funnel.** `_place_order_with_broker_idempotency` (`execution_engine/service.py:1076`) sends at `:1140` and `:1143`. It checks the kill switch and the rate limiter, but **not** paper/live mode, the universe, or a token.

| Caller → funnel | Guards before the order |
|---|---|
| `execute_approved_signal` (`:809`) | `risk_decision_id`, expiry, kill switch, universe validator (skipped when `None`) |
| `_handle_approved_signal_event` (`:2065`) | `paper_trade=True` → paper simulator; direction-conflict check |
| `_maybe_place_protective_stop`, `_place_emergency_flatten_order`, `_place_pending_protective_order` | May send while the kill switch is active (exits) |
| `_reconcile_state` (`:1578`) | Re-sends PENDING orders via `execute_approved_signal`. `_is_paper_order` applies only in pass 2. |

**Paths that bypass the funnel:**
- `mis_square_off.py:590`: guarded only by `self._paper_trading`.
- `exit/exit_order_router.py:315`: gated on LIVE plus `_live_enabled`, which is always False (F-3).

**Scripts:**
- No script calls `place_order` directly.
- `scripts/strategy/config.py go-live` flips `paper_trade=False` in DynamoDB, which routes that strategy's signals to the live path.

## 9. Dead code and duplication

| Category | Inventory |
|---|---|
| Backtest engines (4 families) | 1. `services/strategy_engine/backtesting/backtester.py`<br>2. `services/backtesting/` lab (wraps #1)<br>3. `qe/engine/{sim,paper,core}.py`<br>4. About 16 standalone pandas study loops in `scripts/backtest/run_*_study.py`, plus the external LEAN and QC harnesses |
| Cost models (about 9) | `IndianCostModel` (`backtester.py:168`) · **a second `IndianCostModel`** (`scripts/backtest/run_phase1_local.py:77`) · `qe/costs.py` (parity-tested against #1) · `alpha_engine/cost/cost_model.py` · `run_factor_study.py` leg fractions · `OptionsCostModel` · `FuturesCostModel` · `_round_trip_cost_inr` · v1 paper-fill costs |
| Retired strategies | `orb`, `vwap_reversion`, `intraday_trend_15m` and `preclose_momentum` are RETIRED; `scalp_1m` is PARKED. All still exported and registered as v1 runners. |
| Superseded scripts | `scripts/paper/run_delivery_paper_book.py` and `replay_delivery_book_forward.py`. **Kept** as parity anchors for the qe tests (see `docs/runbooks/v1-decommission-runbook.md`). |
| Cloud-sync duplicates | 26 files matching `* 2.*`, `* 3.*` or `* 4.*` (none are `.py`), plus 4 empty duplicate directories under `.claude/skills/` and `configs/backtesting/genai 2` |
| Unused safety code | `services/shared/live_gate_checker.py` has no caller outside tests and references a missing `scripts/ops/approve_live_gate.py` |

## 10. Findings (Phase 0 record — see §10a for the 2026-09-25 triage and fixes)

Severity reflects the impact **if the v1 stack were ever run live**. Live is blocked today.

| ID | Sev | Finding | Location |
|---|---|---|---|
| F-1 | ~~HIGH~~ MED (corrected) | `paper_trade` falls back to **`False` (live)** in the parsers. *Correction:* a **missing** field was already refused (it is in `SIGNAL_REQUIRED_FIELDS`); the real gap was a present-but-`null`/string value, which `bool(...)` routed live. | `shared/models/signal.py:143`, `enriched_signal.py:204`, `kafka_signal_consumer.py:323`, `kafka_approved_consumer.py:306`, `candle_adapter.py:196` |
| F-2 | HIGH | Non-NSE markets bypass universe validation in **every** mode, including `LIVE_ADVANCED`. Logged only at DEBUG. | `services/shared/universe/order_validator.py:89-101` |
| F-3 | MED | `QE_EXECUTION_LIVE_TRADING_ENABLED` is not a settings field, so the exit-router live path is unreachable. Its keyword call also does not match `ZerodhaBrokerClient.place_order(order)`. | `execution_engine/service.py:542`, `exit/exit_order_router.py:315` |
| F-4 | MED | `RISK_PROFILE` defaults to `"tiny-live"` in pydantic but `"paper"` in direct env reads | `settings.py:226` vs `risk_engine/service.py:299`, `margin_validator.py:111` |
| F-5 | MED | `EXECUTION_PAPER_TRADING` is not consulted on the entry path; entry routing uses only the per-signal flag | `execution_engine/service.py:2126` |
| F-6 | MED | The universe validator is set to `None` on a paper-mode build failure, and `validate()` is then skipped entirely | `execution_engine/service.py:466-469`, `:869` |
| F-7 | MED | MIS square-off and the protective/flatten orders bypass the idempotency funnel | `mis_square_off.py:590`, `service.py:1304/1482/1896` |
| F-8 | LOW | Both brokers are constructed and connected at startup whatever the mode; `data_ingestion` builds an order-capable Kite client | `execution_engine/service.py:339-343`, `data_ingestion/service.py:676` |
| F-9 | LOW | ALPACA-FIX: unguarded `.value` calls on enums | `alpaca_broker.py:288,290,544` |
| F-10 | MED | **v2 look-ahead:** `regime_series` picks its top-100 market proxy from **total-period** turnover (`turn.sum()`), so the walk-forward regime overlay leg sees future liquidity. It is a verbatim v1 parity anchor, so it is flagged, not changed. | `qe/research/wf_v1.py:33-39`, used at `qe/research/walkforward.py:117` |
| F-11 | MED | **v2:** a walk-forward study with **no gates** reports `engine_pass=True` (fail-open), even though `all_passed([])` is False | `qe/research/walkforward.py:157` |
| F-12 | LOW | **v2:** the family multiple-testing count is returned but **not persisted** to the ledger | `qe/research/registry.py:59-61` |
| F-13 | MED | **CI gap:** CI runs `ruff --select E9,F63,F7,F82` and `pytest tests/unit/` only. `tests/qe` (including the broker-isolation and parity tests) and the TID251 banned-API rule are **not enforced in CI**. | `.github/workflows/ci.yml:76-80` |
| F-14 | LOW | `rules/trading_layer_separation.yaml` lists forbidden imports, but has no enforcement mechanism | `rules/` |

### 10a. Triage and dispositions (2026-09-25, operator-approved)

Fixes are on branch `fix/findings-triage` (off `dev`, one commit per finding, independently
reviewable), merged into `feature/hybrid-ai-research`. Each fix ships with tests that fail without it.

| ID | Disposition | Detail |
|---|---|---|
| F-1 | **FIXED** `f929947` | `validate_event` requires `paper_trade` to be a JSON boolean on pending/enriched/approved; `null`/strings go to the DLQ, never approved or executed. Risk-layer "unknown ⇒ strict" semantics unchanged. 28 tests. |
| F-2 | **FIXED** `40c6792` | Non-NSE orders BLOCKED in LIVE (CRITICAL log); paper modes allowed with WARNING. 5 tests. |
| F-3 | DEFER (v1 decommission) | Net effect is fail-closed today (the v1 live exit path is unreachable). Must be fixed only if v1 were ever revived for live; qe replaces it. |
| F-4 | DEFER (v1) | Pydantic default `tiny-live` is the *stricter* profile, so an unset env var fails safe for limits; run scripts set `RISK_PROFILE=paper` explicitly. Inconsistency recorded, not dangerous. |
| F-5 | MITIGATED by F-1 | Entry routing uses the per-signal flag, now type-validated at every boundary and stamped from strategy-config by StrategyRunner. |
| F-6 | ACCEPT (documented degrade) | Paper-only graceful degrade, logged WARNING; live remains fatal at startup — matches the CLAUDE.md paper-degrade rule. |
| F-7 | DEFER (v1) | Exit/flatten paths bypass the entry funnel by design (must work under kill switch). Revisit only if v1 is revived. |
| F-8 | DEFER (v1 decommission) | Broker construction at startup; removed wholesale by the v1 decommission runbook. |
| F-9 | TRACKED | ALPACA-FIX already owned by ADR-041 P6b (US live automation). |
| F-10 | **FIXED (research)** `c73fa19` | `qe.research.regime.pit_regime_series` (re-selects the proxy monthly from trailing turnover) reported beside the verbatim v1 overlay, which is labelled look-ahead; a test proves the v1 proxy leaks. Re-measurement of the ADR-034 overlay conclusion on the real lake: see ADR-034 correction note. |
| F-11 | **FIXED** `a0e4ec1` | An ungated walk-forward study reports FAIL. |
| F-12 | **FIXED** `b47e158` | Family test-budget count persisted in each ledger record (existing lines untouched — append-only). |
| F-13 | **FIXED** `4a441bc` | CI `test-qe` job: full ruff rules on `qe/`, `pytest tests/qe`, fails on unexpected skips. Not yet run on GitHub (nothing pushed). |
| F-14 | SUPERSEDED for qe | Import rules for qe/qe.ai are now enforced by tests in CI (F-13); `rules/trading_layer_separation.yaml` stays advisory for frozen v1. |

## 11. Tests and CI

- **`tests/qe`:** 89 tests, all passing, 0 skipped, on Python 3.11 at `d8ca741`.
  - Covers config hashing, the journal, lake and snapshot, clocks and the kill switch, costs parity, risk and execution, broker isolation, the live gate, the paper engine (paper == sim), NSE and US parity, walk-forward, gates and registry.
  - Several parity tests import v1 code from `scripts/` and use `importorskip`, so always run with `-rs` and confirm 0 skipped.
- **`tests/unit`:** v1 services (runs in CI).
- **`tests/backtest`:** the lab, including `test_genai_layer.py` (11 tests).
- **Isolation patterns already in use:**
  - Source-text broker-token scans across about 11 lab tests.
  - A runtime `sys.modules` check (`tests/unit/test_alpha_shadow_publisher_isolation.py`).
  - A type-level isolation test (`tests/qe/test_broker_isolation.py`).
