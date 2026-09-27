# Hybrid AI Research System (`qe.ai`)

> ADR-043. Status per component is listed in §6: P0–P5 are **implemented** (offline, advisory, not wired to any engine path); anything marked `[PLANNED — not yet implemented]` does not exist in code.
> Related: [ai-quant-boundary.md](ai-quant-boundary.md) · [security-model.md](security-model.md) · [data-flow.md](data-flow.md) · [lookahead-prevention](../research/lookahead-prevention.md) · [TradingAgents analysis](../tradingagents/adaptation-analysis.md)

## 1. Principle

```
AI          "What might be happening?"       → qe.ai agents (structured observations)
Research    "What evidence supports it?"     → ResearchSignal v1 (evidence attached by code)
Quant       "Does the data support it?"      → qe factor model / qe study (deterministic)
Risk        "Can we safely trade it?"        → qe.risk + fusion hard flags (deterministic veto)
Execution   "Execute only if allowed."       → qe engine only (SimBroker/PaperBroker; LiveBroker locked)
```

AI provides context. QuantEmbrace provides discipline. The risk checks are authoritative. The engine is the only path to a fill. **`qe.ai` builds a research layer, not an AI trading bot.**

## 2. Placement

`qe.ai` is an **offline advisory package** inside `qe/`, with its own entry point (`python -m qe.ai`):

- It reads the lake (read-only, point-in-time) and, optionally, engine journals (read-only).
- It writes only its own research journals and reports.
- The engine (`qe.engine`, `qe.execution`, `qe.risk`, `qe.strategy`, `qe.cli`) never imports `qe.ai`, and `qe.ai` never imports them. Tests enforce both directions ([boundary](ai-quant-boundary.md)).
- It adds **no latency to any trading path**. The engine's code path is unchanged byte for byte, and paper == sim parity (₹0.00) is re-verified after every phase.

This follows RA-1 F-4 ("AI cut from the hot path"). A model can enter the engine only as a library function, after a pre-registered forward test and human promotion. That step is `[PLANNED — not yet implemented]` and would need its own ADR.

## 3. Component map

```
qe/ai/
  config.py            ResearchRunConfig, ModelProfile, BudgetConfig (own frozen hash; not RunConfig)
  paths.py             the only write locations + write guard
  models/              ResearchSignal v1, AgentObservation, Evidence, ResearchReport, status enum
  llm/                 LLMClient protocol · FakeLLM · BedrockLLM + AnthropicLLM (shared Messages-API client) · cache · budget/breaker · gateway
  guardrails.py        secret redaction, untrusted-data blocks, forbidden-output scan
  tools/               point-in-time read-only tools (market_data, quant_signal, regime, risk, unavailable)
  agents/              technical, regime, risk, fundamental, news, sentiment, bull, bear, critic, synthesizer
  orchestration/       modes (FAST/STANDARD/DEEP), debate, graph (explicit DAG), research journal
  fusion/              deterministic fusion: quant view, AI view, hard flags, decision (AI_ADVISORY default)
  cli.py, __main__.py  python -m qe.ai research | report | fuse
```

## 4. Research run lifecycle

1. **Load.** Read the research config and the referenced **book config** (for example `configs/qe_delivery_book_paper.yaml`, read-only), so the factor parameters have one source of truth. Load the panel through `qe.data` for `[as_of − 2y, as_of]` and pin a data snapshot.
2. **Cutoff.** `information_cutoff = trading_date(pos) @ market close` (`qe.clock.market_close_time`). Every tool reads through `ResearchDataAPI(panel, pos)`, which wraps `Context.at`.
3. **Market level.** The regime tool, then the regime analyst, run once per run.
4. **Per symbol.** Symbols are the engine's basket at `as_of` plus any explicitly requested ones, capped by `max_symbols`. For each:
   - The tools run, called by code.
   - The analysts run. Fundamental, news and sentiment return UNAVAILABLE with no LLM call.
   - STANDARD/DEEP only: the bull/bear debate, the critic, and the synthesizer.
   - Code builds the `ResearchSignal`: it attaches evidence, checks point-in-time validity and computes contamination.
5. **Journal.** Every step goes to `journals/ai/<run_id>.jsonl` (redacted). `report` derives `reports/qe-ai/<run_id>/`.
6. **Fuse.** `fuse` combines ResearchSignals with the deterministic quant view and hard flags. It reports the **AI recommendation next to QuantEmbrace's decision**; they are never merged by default.

## 5. Research modes (cost control)

| Mode | Analysts | Debate rounds | Critic | Synthesizer | LLM calls per symbol |
|---|---|---|---|---|---|
| FAST | technical, risk (+ regime once per run) | 0 | no | deterministic (no LLM) | ≤ 2 |
| STANDARD | + fundamental, news, sentiment (UNAVAILABLE → 0 calls today) | 1 (bull, bear) | yes | quick model | ≤ 6 |
| DEEP | same | 2 | yes | deep model | ≤ 8 |

Every run also has:
- a token budget per run;
- `max_tokens` per call;
- a timeout;
- a bounded retry count (malformed or timeout);
- a circuit breaker: after N consecutive failures, the remaining agents report UNAVAILABLE;
- a content-addressed response cache, so identical prompts are never paid for twice;
- a `max_symbols` cap;
- the `--allow-llm-spend` flag, which must be passed before any real (non-fake) backend is used.

## 6. Status

| Component | Phase | Status |
|---|---|---|
| Current-state recon, adaptation analysis | P0 | Implemented (docs) |
| Architecture and security design, ADR-043 | P1 | Implemented (docs) |
| Schemas, config, boundary tests | P2 | Implemented — `qe/ai/{config,paths,models}`, `tests/qe/ai/test_ai_{schemas,boundary,engine_untouched}.py` |
| LLM layer, guardrails, tools, analyst agents | P3 | Implemented — `qe/ai/{llm,guardrails,tools,agents}`; fake backend only exercised (zero spend) |
| Debate, orchestration, journal, CLI | P4 | Implemented — `qe/ai/orchestration`, `qe/ai/{reporting,cli}.py`, `python -m qe.ai research|report` |
| Deterministic fusion (AI_ADVISORY) | P5 | Implemented — `qe/ai/fusion`, `python -m qe.ai fuse`; ADVISORY parity with the engine proven at every sim rebalance |
| Hypothesis drafts, strategy lifecycle ledger, forward AI shadow gate | P6 | Implemented 2026-09-25 — `qe/ai/{hypotheses,shadow}`, `qe/research/lifecycle.py`; gate committed as **DRAFT, not signed off** (sign-off waits for the real model, P10) |
| Post-trade analyst, reflection memory stamped with knowledge time | P7 | Implemented 2026-09-26 — `qe/ai/post_trade`, `python -m qe.ai post-trade`; deterministic reviews + LLM lesson; `lessons_known_at(cutoff)` feeds hypotheses |
| Research dashboard (beyond the markdown fusion view) | P8 | Implemented 2026-09-26 — `qe/ai/dashboard.py`, `python -m qe.ai dashboard` (static HTML, escaped, CSP, no JavaScript) |
| External-data (news/fundamentals) security hardening | P9 | Implemented 2026-09-26 for **NSE announcements** — `qe/ai/corpus`, `scripts/backtest/download_nse_announcements.py`; downloader live-verified 2026-09-26 (295 real records, 0 failures); fundamentals and sentiment remain `[PLANNED — not yet implemented]` (no structured source) |
| First real LLM spend and paper-shadow validation | P10 | Bedrock backend, first-party Anthropic backend (added 2026-09-26) and probe **built and tested (fake runtime)**; the real Bedrock smoke run is **BLOCKED by the AWS account** (§ `ai-research-p7-p10-report.md`); the first-party backend has not made a real call (needs an API key). Shadow gate remains an unsigned DRAFT. Paper-shadow accrual `[PLANNED — not yet implemented]` |

## 7. What this system will not do

- It will not place, modify or cancel orders. It will not modify positions, risk limits, configs or book state.
- It will not promote, graduate or retire strategies. It may write *hypotheses* for human review (P6).
- It will not run inside `qe paper`/`qe study`, or change a single engine decision.
- It will not treat historical backtests of LLM output as evidence (contamination rule, [lookahead-prevention](../research/lookahead-prevention.md)).
- It will not call external networks except the configured LLM endpoint (Bedrock or `api.anthropic.com`), and only with `--allow-llm-spend`.
