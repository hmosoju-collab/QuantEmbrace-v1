# Hybrid AI Research Layer — Phase 0–5 Report

> ADR-043 · branch `feature/hybrid-ai-research` (from `dev` checkpoint `d8ca741`) · 2026-09-25
> **Status: Phases 0–5 built and tested with a fake LLM. STOP for operator review.** No real LLM spend. No change to any trading path. This is **not** production-ready, and it is **not an edge**.

## 1. Architecture summary

- **Placement.** `qe/ai/` is an offline, advisory research package with its own entry point, `python -m qe.ai research|report|fuse`.
- **What it does:**
  1. Reads the lake point-in-time through `ResearchDataAPI`, a wrapper around `Context.at`.
  2. Runs code-invoked tools: technical, quant (the engine's own factor rank and basket), a point-in-time regime proxy, risk metrics.
  3. Runs analyst agents: technical, regime, risk. Fundamental, news and sentiment report **UNAVAILABLE** with zero LLM calls, because the lake holds prices only.
  4. Runs an optional bounded bull/bear debate, a critic, and a synthesizer.
  5. Emits typed **`ResearchSignal` v1** records into its own journal, `journals/ai/`.
- **Fusion.** A deterministic fusion layer shows the AI recommendation **next to** QuantEmbrace's own decision. The default is **AI_ADVISORY with AI weight 0**, under which the fused decision is the engine's pick. Tests prove this at every sim rebalance.
- **Isolation.** The engine never imports `qe.ai`, and `qe.ai` imports only an allowlist of `qe` modules. Both directions are AST-tested, plus a fresh-interpreter runtime check.

## 2. Modified files (pre-existing)

| File | Change |
|---|---|
| `.gitignore` | `*.tfplan` added (checkpoint commit; plan files can embed sensitive values) |
| `architecture/system_design.md` | New section "Hybrid AI Research Layer — `qe.ai`" (status: implemented, offline, not wired) |
| `memory/decisions.md` | ADR-043 appended |
| `memory/open_tasks.md` | Hybrid AI research track added |
| `CLAUDE.md` | 3-line pointer to `qe.ai` |

**No engine file changed.** `git diff d8ca741..HEAD -- qe ':(exclude)qe/ai'` is empty. The paper-book config hashes are pinned by a test: delivery `7c95f33da911…`, momentum `d377a933b653…`.

## 3. New files

| Area | Files |
|---|---|
| Package (≈3.7k lines) | `qe/ai/{__init__,__main__,cli,config,paths,guardrails,reporting}.py`<br>`qe/ai/models/` (common, evidence, observation, signal, report)<br>`qe/ai/llm/` (base, fake, bedrock, cache, budget, gateway)<br>`qe/ai/tools/` (pit, market_data, quant_signal, regime, risk, unavailable)<br>`qe/ai/agents/` (base, prompts, technical, regime, risk, fundamental, news, sentiment, bull, bear, critic, synthesizer)<br>`qe/ai/orchestration/` (modes, state, debate, graph, journal)<br>`qe/ai/fusion/` (config, quant, engine, view) |
| Configs | `configs/qe_ai_research.yaml` (backend `fake`), `configs/research_fusion.yaml` (AI_ADVISORY, weight 0) |
| Tests (≈1.9k lines) | `tests/qe/ai/`: conftest plus 12 `test_ai_*.py` files |
| Docs | `docs/architecture/`: `current-state.md`, `current-state-diagram.md`, `hybrid-ai-system.md`, `ai-quant-boundary.md`, `security-model.md`, `data-flow.md`<br>`docs/tradingagents/adaptation-analysis.md`<br>`docs/research/`: `lookahead-prevention.md`, `strategy-discovery.md`, `experiment-framework.md`, `tradingagents-adaptation.md`, this report<br>`docs/operations/`: `ai-configuration.md`, `observability.md`, `failure-handling.md` |

## 4. TradingAgents components adapted

These were reimplemented as ideas; no code was copied (clean-room, Apache-2.0 reference):
- specialist analysts;
- the bull/bear debate with bounded rounds;
- a critic/judge;
- a synthesizer;
- quick/deep model tiers;
- honest data-vendor chains;
- point-in-time fundamentals discipline, adopted as a rule.

Full analysis: `docs/tradingagents/adaptation-analysis.md`.

## 5. Components intentionally not copied

| Component | Reason |
|---|---|
| Trader / portfolio-manager **authority** | The engine and humans keep all authority |
| The risk-debate team as a decision maker | Risk is a deterministic veto |
| LangGraph / LangChain | An explicit DAG is enough and auditable, with no dependency |
| Reflection memory | Look-ahead leak; deferred to P7 with knowledge-time stamps |
| yfinance / Reddit / StockTwits / news tools | Unvetted egress, untrusted text, licensing |
| LLM tool-calling | Tools are called by code |
| Free-text decisions | Replaced by a typed schema |
| Historical backtests of LLM decisions as evidence | Contamination |

## 6. Dependency changes

**None.** `pyproject.toml` and the `requirements*.txt` files are unchanged. `boto3` is already pinned and is imported lazily, only in `qe/ai/llm/bedrock.py`. `hypothesis` is already in `services/requirements.txt`.

## 7. Security findings

**Controls implemented** (matrix and tests in `docs/architecture/security-model.md`):
- No LLM action surface: no tools, no network, no shell, no environment access in `qe.ai`, all AST-enforced.
- Untrusted-data blocks with delimiter neutralization.
- Forbidden-output scan: BLOCKED, no retry.
- Secret scan on prompts before sending, and recursive redaction of every journal payload.
- Sanitized aborts; provider errors reduced to the exception type.
- A spend flag guarding any paid backend.
- Confined writes (`safe_write_path`), never under `reports/qe/` or `journals/paper-*`.

**Bug found and fixed during P2.** `safe_write_path` initially resolved symlinks on the *allowed root* as well as the target. A symlinked `journals/ai → reports/qe` would therefore have let research output land in gate-evidence territory. A test caught it; it was fixed before the first commit of the package.

**Documented in Phase 0; triaged 2026-09-25** — dispositions in `docs/architecture/current-state.md §10a` (F-1, F-2, F-10, F-11, F-12, F-13 fixed):

| ID | Severity | Finding |
|---|---|---|
| F-1 | ~~HIGH~~ MED | v1 `paper_trade`: a *missing* field was already refused by schema validation (this report originally overstated it); the real gap was a `null`/string value routing live. **Fixed 2026-09-25** (`f929947`) |
| F-2 | HIGH | Non-NSE markets bypass universe validation in every mode, including LIVE — **fixed 2026-09-25** (`40c6792`) |
| F-3 | MED | `QE_EXECUTION_LIVE_TRADING_ENABLED` is not a settings field |
| F-4 | MED | `RISK_PROFILE` defaults to `tiny-live` in pydantic but `paper` in env readers |
| F-6 | MED | The universe validator is skipped when it is `None` |
| F-7 | MED | MIS and protective orders bypass the idempotency funnel |
| F-10 | MED | v2 look-ahead: `wf_v1.regime_series` picks its market proxy from total-period turnover |
| F-11 | MED | v2: a walk-forward study with no gates reports PASS |
| F-12 | LOW | v2: the family test-budget count is not persisted |
| F-13 | MED | CI runs neither `tests/qe` nor the TID251 rule |

**Residual risk.** Regex scanning is incomplete, and prompt injection is *contained* rather than prevented. A hijacked model can only write bounded text and scores into a weight-0 research record.

## 8. Look-ahead prevention design

- **Data channel (closed by construction):**
  - `information_cutoff = decision date @ market close`.
  - Every `Evidence` carries `knowledge_ts`, so the same-day close is invisible before the close.
  - Tools see only `Context.at(panel, pos)`.
  - Evidence after the cutoff fails the symbol **before any agent sees it**.
  - The regime proxy uses trailing turnover, unlike the look-ahead `wf_v1` version.
  - Index data is never forward-filled.
  - Tested by future-row mutation invariance, a panel-mutation guard, and a same-day-close test.
- **Model-memory channel (governed, not engineered):**
  - `contamination_risk` is computed from model knowledge cutoffs plus a 90-day guard; an unknown cutoff counts as contaminated.
  - A contaminated signal can never carry weight.
  - **Historical AI backtests are not evidence.**
  - Ticker masking and date-free prompts reduce the leak but never clear the flag.

Details: `docs/research/lookahead-prevention.md`.

## 9. AI/quant fusion methodology

```
q = 2·rank_pct(book factor | PIT liquid universe) - 1
S = q if w = 0, else (1-w)·q + w·c·a
```

- **Weight ceilings (in code):** DISABLED 0, ADVISORY 0, WEIGHTED 0.20, EXPERIMENTAL 0.50.
- **Weighted modes** run in the `study` context only, and use only uncontaminated signals for the exact cutoff.
- **Regime** is reported, not blended, because it is market-level.
- **Risk** is a veto, not a weight. REJECT always wins.
- **AI alone never decides.** No quant score means NO_DECISION.
- **The AI recommendation** is shown next to the decision and never merged into it.

Details: `docs/operations/ai-configuration.md §3`.

## 10. Test results (actual)

Environment: Python 3.11.x in a scratchpad `uv` venv installed from `requirements.txt` plus `pytest`, `hypothesis` and `ruff`; pandas 3.0.6, pydantic 2.13.5.

| Measurement | Result |
|---|---|
| Baseline before any change (`pytest tests/qe -rs`) | **89 passed, 0 skipped** |
| Final (`pytest tests/qe -rs`) | **424 passed, 0 skipped** (89 engine + 335 `qe.ai`) |
| Engine parity tests (₹0.00 paper == sim, NSE and US) | Unchanged and passing |
| `ruff check` on `qe/ai` and `tests/qe/ai` (full pyproject rules, incl. TID251) | Clean |

`qe.ai` tests by file:

| File | Tests |
|---|---|
| boundary | 195 (per-file parametrizations of the AST rules) |
| guardrails | 27 |
| fusion | 22, incl. hypothesis property tests |
| llm | 18 |
| agents | 16 |
| orchestration | 15 |
| schemas | 14 |
| tools_pit | 13 |
| engine_untouched | 5 |
| journal | 5 |
| cli | 3 |
| fusion_parity | 2 |

**End-to-end runs:**
- **Synthetic lake:** `python -m qe.ai research → report → fuse` on an on-disk synthetic lake passes (`test_ai_cli.py`).
- **Real lake — not completed.** The run against the real lake (as-of 2026-07-14, fake backend) **blocked**: 1,003 of 7,548 daily lake files (2024–26) are iCloud-evicted ("dataless"), and reading them stalls with no CPU use. The run was stopped before writing anything. This affects `qe study` and `qe paper` too. Fix: `brctl download backtest-data/lake`, or keep the lake outside iCloud (`docs/operations/failure-handling.md`).

## 11. Performance impact

- **On trading: none.** No engine file changed, no engine module imports `qe.ai`, and the paper path is byte-identical.
- **Research runtime:** tests run in seconds. On a warm lake, research cost is dominated by the panel load, the same as `qe paper`, plus about 2–8 LLM calls per symbol.

## 12. Cost considerations

- **Spend to date: $0.** Only the fake backend has run.
- **Controls:** the spend flag; per-run token budget with worst-case reservation; per-call `max_tokens`; timeouts; bounded retries; circuit breaker; content-addressed cache (reruns are free and byte-identical); `max_symbols`; no-data agents cost 0.
- **Rough size:** FAST ≤ 2 calls per symbol plus 1 regime call per run; STANDARD ≤ 6; DEEP ≤ 8.

## 13. Paper-mode validation plan (P10, `[PLANNED — not yet implemented]`)

1. Fix the iCloud lake eviction.
2. Choose Bedrock model IDs and record their knowledge cutoffs.
3. Set up a least-privilege IAM role.
4. Pre-register an AI shadow gate. For example: rank IC of `ai_score` vs forward 21-day return, net of `q`, over N post-cutoff months.
5. At each month-end cadence: `qe.ai research` (STANDARD) on the book basket plus a control sample, then `fuse --context shadow --engine-journal <paper journal>`, with results appended to a shadow ledger.
6. Evaluate only after the pre-registered horizon. Any weight change requires a new ADR and human approval. `AI_ADVISORY` stays the default in the meantime.

## 14. Remaining risks

- The layer has no edge. With price-only data the analysts restate the factor model.
- CI does not run `tests/qe`, so the isolation guarantees are local-only until the CI change is approved (F-13).
- Regex secret and forbidden-output scanning is incomplete.
- The Bedrock adapter is tested only against a fake runtime; real Converse behaviour (throttling, model-specific output) is unproven.
- The iCloud-evicted lake can stall any `qe` run.
- The prompt-hash pins make prompt edits deliberate, but prompt *quality* is untested against real models.
- F-1 and F-2 (v1, HIGH) remain open. They are harmless while live is blocked and v1 is frozen, but they must be fixed before any v1 path could trade.

## 15. Recommended next phase

In this order, each gated by operator approval:

1. **Triage the documented findings.** Especially F-13 (CI runs `tests/qe`), F-11 and F-12, which are prerequisites for P6, and F-10.
2. **Fix the iCloud lake eviction.** Then re-run the real-lake end-to-end (`python -m qe.ai research --as-of 2026-07-14` → `fuse --engine-journal …`) with the fake backend.
3. **P6: hypothesis → study pipeline and the pre-registered forward AI shadow gate** (design in `docs/research/strategy-discovery.md` and `experiment-framework.md §4`).
4. Only then P10: the first real Bedrock spend, in forward shadow on post-cutoff dates.

P7 (post-trade analysis), P8 (dashboard) and P9 (external data) can follow in any order, but none of them should precede a forward shadow gate.
