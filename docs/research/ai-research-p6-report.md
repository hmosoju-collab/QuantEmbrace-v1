# Hybrid AI Research — Findings Triage, Real-Lake Validation, and Phase 6 Report

> ADR-043 (Phase 6 addendum) · 2026-09-25 · branch `feature/hybrid-ai-research`
> (fixes on `fix/findings-triage` off `dev`, merged in). **STOP for operator review.**
> Zero LLM spend. No change to trading decisions. The forward AI shadow gate is an **unsigned DRAFT**.

## 1. Findings triage (step 1)

All dispositions are recorded in `docs/architecture/current-state.md §10a`. Each fix is one commit, with tests that fail without it:

| Finding | Commit | Change |
|---|---|---|
| F-1 | `f929947` | `paper_trade` must be a JSON boolean at every signal boundary. `null` or a string goes to the DLQ. **Severity corrected:** a *missing* flag was already refused, and the Phase 0 report overstated it. |
| F-2 | `40c6792` | Non-NSE orders BLOCKED in LIVE; paper modes allowed with a WARNING |
| F-10 | `c73fa19` | Point-in-time regime overlay reported beside the verbatim (leaky) one; a test proves the leak |
| F-11 | `a0e4ec1` | An ungated walk-forward study reports FAIL |
| F-12 | `b47e158` | Family test-budget count persisted in every ledger record |
| F-13 | `4a441bc` | CI `test-qe` job: full ruff rules on `qe/`, `pytest tests/qe`, fails on an unexpected skip. **Not yet run on GitHub** (nothing pushed). |
| F-3 – F-9, F-14 | — | Deferred to the v1 decommission, accepted as a documented paper degrade, tracked elsewhere, or superseded (reasons in §10a) |

v1 regression check: the failing set in `tests/unit` is **identical to the pre-change baseline** (245 environment-caused failures, e.g. asyncio event loop and missing settings env); only new passing tests were added.

## 2. Real-lake validation (step 2)

- **iCloud eviction fixed.** 4,764 evicted lake files were re-downloaded. `brctl download` works per file only; the folder form silently does nothing.
- **`python -m qe.ai research`** on the real lake (as-of 2026-07-14, fake backend) ran in **8 s**: 20 signals, 0 failures, 41 LLM calls. All signals are flagged contaminated, correctly: the fake models have no knowledge cutoff.
- **`fuse` against the latest delivery paper journal:**
  - AI_ADVISORY SELECT equals the engine's basket (20, **0 divergences**).
  - The view correctly shows the engine did not rebalance that day (mid-month, not due).
  - Regime reads RISK_ON (R +0.87).
- **`hypothesize`:** 1 CANDIDATE draft, correctly flagged.
- **F-10 re-measurement.** The registered delivery walk-forward was re-run: engine CAGR 20.8% / Sharpe 1.32 and v1 23.7% / 1.41 are **reproduced exactly**.

  | Variant | Sharpe (look-ahead proxy) | Sharpe (point-in-time proxy) |
  |---|---:|---:|
  | Delivery + overlay (no overlay: 1.41) | 1.17 | 1.20 |
  | Benchmark + overlay (no overlay: 0.99) | 0.94 | **1.16** |

  - The **delivery "overlay not adopted" decision stands.**
  - The **benchmark-leg conclusion reverses**: point-in-time, the overlay helps the equal-weight market proxy, and its max drawdown improves from −25.8% to −17.5%.
  - Recorded as an ADR-034 correction note and in `docs/backtesting/delivery-walkforward-report.md`.

## 3. Phase 6 deliverables (step 3)

### Strategy lifecycle ledger — `qe/research/lifecycle.py`, `python -m qe lifecycle`

- **States:** CANDIDATE → RESEARCH → BACKTEST → VALIDATION → PAPER → PRODUCTION_ELIGIBLE, with GRAVEYARD reachable from any state.
- **Ledger:** append-only, at `governance/strategy-lifecycle.jsonl`.
- **Approvals:** every step needs a named **human** approver; AI or automation identities are refused.
- **Evidence per state**, verified against the experiment registry where applicable:

  | Target state | Evidence required |
  |---|---|
  | BACKTEST | A registered study run |
  | VALIDATION | Passed its pre-registered gates |
  | PAPER | A paper config, whose hash is recorded |
  | PRODUCTION_ELIGIBLE | A forward-gate artifact bound to that hash |
  | GRAVEYARD | A cause of death |

- **Versions recorded:** strategy, dataset, backtest, and research-signal versions.
- **No AI path:** `qe.ai` cannot import `qe.research`, so AI has no code path to promote a strategy.
- **Not live:** PRODUCTION_ELIGIBLE is not live trading.

### AI hypothesis drafts — `qe/ai/hypotheses`, `python -m qe.ai hypothesize`

- The hypothesis agent proposes 1–3 falsifiable hypotheses. Each must carry at least one pre-registered gate on a real study metric; anything else is MALFORMED.
- **Code annotates each draft:**
  - whether it re-proposes a settled family, using the new machine-readable `governance/research-eliminated-families.yaml` (from the consolidation memo);
  - whether it is testable with data the lake holds;
  - the family's multiple-testing count.
- Drafts are CANDIDATE-only, under `reports/qe-ai/hypotheses/`. Nothing is registered and nothing is written to governance.

### Forward AI shadow gate — `configs/qe_ai_shadow_gate.yaml`, `python -m qe.ai shadow`

- **The only route to AI weight > 0.** It mirrors the Forward Factor Gate: ≥12 months, IR ≥ 0.50, ≥58% positive months, no month > 50%.
- **Metric:** the *incremental* IC of `ai_score` over the factor rank.
- **Accrual is fail-closed**, and every exclusion is counted:
  - only runs of the bound config hash and model;
  - only decision dates after sign-off;
  - no contaminated signals;
  - the first run per date counts, so re-runs cannot cherry-pick;
  - one decision date per month;
  - the outcome must be knowable;
  - at least 8 names per month.
- **Committed as DRAFT.** The evaluator refuses any verdict until a human signs off. `--show-binding` prints the sign-off values and warns while the backend is fake or the cutoff unknown.
- **A test caught a real bug:** a factor-parroting AI score produced a spurious IC from floating-point noise. It now scores exactly 0.0, and is counted rather than dropped, because dropping it would inflate the IC ratio.

## 4. Test results (actual)

| Suite | Result |
|---|---|
| `pytest tests/qe` | **490 passed, 0 skipped** (was 424 before this session) |
| `ruff` | Clean on `qe/` and `tests/qe/` (full rules) |
| `tests/unit` | Failure set identical to baseline |
| Engine | Paper-book config hashes unchanged (pinned test); no change to `qe/engine`, `qe/execution.py`, `qe/risk.py`, `qe/strategy`, `qe/config.py` |

## 5. What is left, and what needs you

1. **Push and review.** `fix/findings-triage` can be opened as its own PR against `dev`. The CI `test-qe` job has never run on GitHub.
2. **P10 prerequisites.** Pick Bedrock model IDs and record their knowledge cutoffs (`docs/operations/ai-configuration.md §2.2`). Then sign off the shadow gate (§4), not before.
3. **P7 (post-trade analyst), P8 (dashboard), P9 (external data)** remain `[PLANNED — not yet implemented]`.
4. **The iCloud eviction can recur** (Optimize Mac Storage). Check for dataless files before each cadence run, or move `backtest-data/` outside iCloud.
