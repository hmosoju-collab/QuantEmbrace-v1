# QuantEmbrace — Serverless GenAI Backtesting Layer

> **Status (AWS-BT-11):** the **guardrails, provider wrapper, prompt templates, Athena templates, and KB manifest are implemented** (`services/backtesting/genai/`, `configs/backtesting/genai/`, `tests/backtest/test_genai_layer.py`). The **AWS serverless infra (Bedrock KB, Step Functions, Lambda, Glue/Athena) is `[PLANNED — not yet implemented]`**.
> **Advisory only.** The layer may observe, analyze, summarize, and recommend. It **must never** place orders, enable live, change capital, mutate config/tables, or auto-promote a strategy.
> Last updated: 2026-06-06 · Governed by `aws-backtesting-steering.md`.

---

## 1. Purpose & forbidden scope

Turn backtest outputs into decision-ready, **cited** analysis: per-run reports, strategy/TEE analysis, RAG Q&A over docs+results, model-dataset explanation, and risk-governance narratives. **Forbidden:** broker orders, live enablement, config mutation, capital mutation, strategy auto-promotion. There is **no action channel** — the layer only returns text.

## 2. Architecture (on-demand / event-driven — no streaming, no SQS)

```
run COMPLETED ─► EventBridge rule ─► Step Functions
   │
   ├─ Lambda: build_context (read qe-bt-runs + S3 metrics/report; REDACT secrets)
   ├─ Lambda/Glue: Athena query templates over Glue-cataloged Parquet (grounding)
   ├─ Bedrock: reasoning + report generation (Claude); Bedrock Knowledge Base for RAG
   └─ Lambda: persist report.md + genai_summary to S3 + qe-bt-runs (advisory)

Operator Q&A (RAG): on-demand request ─► retrieve (KB) ─► Bedrock ─► cited answer
Heavy backtest compute stays on EC2 ARM64 Docker workers (NOT in this layer).
```

| Concern | Service |
|---|---|
| Reasoning + report generation | **Amazon Bedrock** (Claude); `AnthropicProvider` available as the repo's existing alternative |
| RAG over docs/reports | **Bedrock Knowledge Bases** (manifest: `configs/backtesting/genai/kb_ingestion_manifest.json`) |
| Orchestration | **Step Functions** (triggered by EventBridge on run completion / operator request) |
| Lightweight validation/glue | **Lambda** (request/response only — no pollers) |
| Querying backtest outputs | **Glue catalog + Athena** (templates in `genai/athena_queries.py`) |
| Run registry | **DynamoDB** `qe-bt-runs` |
| Data/results | **S3** (`quantembrace-backtest-*`) |
| Logs/alerts | **CloudWatch** `QuantEmbrace/Backtest` + **SNS** `quantembrace-backtest-alerts` |
| Heavy compute | **EC2 ARM64 Docker workers** (unchanged; not part of this layer) |

## 3. Bedrock vs Anthropic SDK (Phase-0 §9 resolved)

Implemented as an `LLMProvider` interface (`genai/bedrock_client.py`): **`BedrockProvider`** (design default — IAM-native, no API key), **`AnthropicProvider`** (matches the repo's existing `StrategySelector`), and **`StubProvider`** (offline tests). Clients are injectable, so tests never touch a network. Recommendation: **Bedrock** for IAM-native ops + Bedrock KB integration; keep `AnthropicProvider` for parity with existing code.

## 4. Guardrails (implemented — `genai/guardrails.py`)

| Guardrail | Mechanism |
|---|---|
| No secrets in prompts | `scan_for_secrets` / `redact_secrets` / `assert_no_secrets` (AWS keys, `sk-*`, `*_key/secret/token`, Zerodha token) |
| No forbidden actions | `assert_no_forbidden_action` on **untrusted context** and **model responses** (enable live, go-live, `live_trading_enabled=true`, place/submit order, promote-to-live/auto-promote, capital/config mutation) |
| Reports must cite sources | `ensure_sources_section` + `assert_cited` — generation requires ≥1 source |
| Governance can't authorise promotion | `evaluate_evidence` computes the verdict **in code**; `recommend_promotion` is **always False** |
| Advisory only | `GenAIAnalyst` has no action method; the LLM only explains code-computed verdicts |

The system preamble (`genai/prompts.py`) also instructs the model that it is advisory-only and must cite sources; the preamble legitimately *names* forbidden actions, so the forbidden-action scan targets untrusted context + responses, not the trusted preamble.

## 5. Agents (advisory roles)

| Agent | Role | Backed by |
|---|---|---|
| Backtest Orchestrator | drive the Step Functions flow on run completion | orchestration |
| Data Quality | summarize `data-quality` reports, flag gaps/trust | `data_quality` + Athena |
| Strategy Analyst | edge/robustness narrative across runs | `analyze_strategy` |
| TEE Exit Analyst | old-vs-new exit + MIS dependency narrative | `analyze_tee` |
| Model Dataset | explain dataset features/labels/splits/leakage | `explain_dataset` |
| Risk Governance | explain the code-computed eligibility verdict; **never authorises** | `risk_governance` |

(These map to the repo's `.claude/agents/*` backtest agents; here they are advisory analysis roles, not actors.)

## 6. Implemented artifacts

| Artifact | Path |
|---|---|
| Guardrails | `services/backtesting/genai/guardrails.py` |
| Provider wrapper (Bedrock/Anthropic/Stub) | `services/backtesting/genai/bedrock_client.py` |
| Prompt templates | `services/backtesting/genai/prompts.py` |
| Athena query templates | `services/backtesting/genai/athena_queries.py` |
| Analyst orchestrator | `services/backtesting/genai/analyst.py` |
| KB ingestion manifest | `configs/backtesting/genai/kb_ingestion_manifest.json` |
| Tests (5) | `tests/backtest/test_genai_layer.py` |

## 7. Failure behavior

A GenAI failure **never** blocks or alters a backtest — the quantitative artifacts already exist. On error: log, SNS alert, and fall back to the deterministic non-AI `report.md`. The risk-governance verdict is computed in code, so an LLM failure (or a forbidden response) leaves the verdict intact.

## 8. Out of scope

Real-time inference; model fine-tuning; any write path into trading state; using GenAI output to gate or trigger live/paper actions.

## 9. Required advisory hardening before operator-facing use

> **Audit (2026-06-06): keep the layer** (advisory-only by construction, well-tested — no action channel, secrets fail-closed, forbidden-action scan on context + response, verdict computed in code with `recommend_promotion` always `False`), **but it is NOT yet safe to wire into operator-facing reporting.** The worst outcomes (auto-trade/promote) are structurally impossible; the residual risk is **human over-trust** of the text. Close the gaps below first; until then keep it code-complete and dormant.

**Gaps**
- **No synthetic / non-authoritative caveat (HIGH).** Nothing propagates `trust_level`/authoritativeness; `evaluate_evidence` doesn't check it and `report_prompt` requests gate status as if real — on synthetic data a report reads as authoritative.
- **Citation is cosmetic (MEDIUM).** `ensure_sources_section` always appends a `## Sources` header before `assert_cited`, so the section check can't fail — it proves a Sources section exists, not that claims are grounded; numbers can be restated wrongly or source ids invented.
- **Regex gaps (MEDIUM).** Novel secret formats (PEM private keys, JWT, connection strings) and semantic evasion ("ready for production capital") can slip the patterns; the leak path is to Bedrock only.

**Required before use (controls + wording + tests)**
- Add a `trust_level`/authoritative field to `AnalysisContext`; emit a mandatory top-of-report **NON-AUTHORITATIVE banner** when data is synthetic/not-HIGH-trust; hard-`BLOCK` synthetic/LOW-trust evidence in `evaluate_evidence` regardless of metrics.
- Render headline numbers in code from `ctx.metrics` (LLM adds commentary only); require inline per-claim citations to ids in the provided set; reject invented ids.
- Broaden secret patterns (PEM headers, JWT, connection strings).
- Preamble / `report_prompt`: state trust level up front; never imply readiness/edge/promotion; standing footer "Advisory only · not financial advice · promotion is a manual human decision."
- New tests: synthetic-caveat enforced; ungrounded / invented-citation rejected; novel-secret redaction; report numbers == `ctx.metrics`; evidence-trust gate → `BLOCK`.

**Activation gate.** Operator-facing GenAI reporting waits for (1) these hardenings + tests, and (2) authoritative HIGH-trust data to analyze — mirrors the dataset-only / defer-MLOps decision (`model-dataset-spec.md` §9).
