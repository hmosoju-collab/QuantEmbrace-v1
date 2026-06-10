# QuantEmbrace — AWS-BT-11 Serverless GenAI Layer Report

> **Phase 11 — serverless GenAI analysis layer. Implemented (guardrails + wrapper + templates) + tested.** Advisory only: no broker orders, no live enablement, no config/capital mutation, no strategy auto-promotion. AWS serverless infra is `[PLANNED]`.
> Generated: 2026-06-06 · Governed by `aws-backtesting-steering.md`.

---

## 1. What was built

- `services/backtesting/genai/guardrails.py` — secret scan/redact, forbidden-action detection, citation enforcement, code-computed risk-governance evidence sufficiency.
- `services/backtesting/genai/bedrock_client.py` — `LLMProvider` with `BedrockProvider` / `AnthropicProvider` / `StubProvider` (injectable clients; offline tests).
- `services/backtesting/genai/prompts.py` — advisory-only prompt templates (report, strategy, TEE, dataset, governance) with a cite-sources directive.
- `services/backtesting/genai/athena_queries.py` — read-only Athena templates over backtest outputs.
- `services/backtesting/genai/analyst.py` — `GenAIAnalyst` orchestrator (no action channel).
- `configs/backtesting/genai/kb_ingestion_manifest.json` — Bedrock KB ingestion manifest (backtest artifacts only; redact-before-embed).
- `docs/backtesting/aws-serverless-genai-backtesting-design.md` — full design (Bedrock, KB, Step Functions, Lambda, Glue/Athena, DynamoDB, S3, CloudWatch/SNS, EC2 workers).

## 2. Phase-0 §9 decision resolved

`LLMProvider` abstraction supports **Bedrock** (design default, IAM-native) and the existing **Anthropic SDK** pattern, plus a **Stub** for tests. Production recommendation: Bedrock (KB-native, no API key); `AnthropicProvider` retained for parity with the repo's `StrategySelector`.

## 3. GenAI use cases (allowed)

orchestration assistance · report summarization · RAG over docs/results · strategy analysis · TEE analysis · risk-governance narrative · model-dataset explanation.

## 4. Forbidden — enforced

broker orders · live enablement · config mutation · capital mutation · strategy auto-promotion. The analyst has **no action method**; forbidden directives are blocked in untrusted context and in model responses; governance verdicts are computed in code with `recommend_promotion` permanently `False`.

## 5. Test results

`tests/backtest/test_genai_layer.py` — **5/5 passing** (full lab suite **81/81**).

| Test | Verifies |
|---|---|
| `genai_prompt_does_not_include_secrets` | secrets scrubbed before the prompt; `assert_no_secrets` raises on raw secrets |
| `report_generation_uses_cited_sources` | report includes a `## Sources` section + source id; uncited → `CitationError` |
| `risk_governance_blocks_insufficient_evidence` | weak evidence → `BLOCK`; strong evidence → `ADVISORY_OK` but `recommend_promotion=False`; a "promote to live" model response is dropped |
| `no_action_prompt_can_enable_live` | forbidden directive blocked in request **and** response; analyst exposes no action method |
| `bedrock_client_stubbed` | works offline via `StubProvider`; `BedrockProvider` parses via an injected fake runtime (no AWS) |

## 6. Safety design notes

- The trusted system preamble *names* forbidden actions (to prohibit them), so the forbidden-action scan targets **untrusted context + responses**, not the preamble.
- Risk governance is **deterministic** (`evaluate_evidence`, mirroring CLAUDE.md's ≥5-valid-sessions + gate rules); the LLM only explains it and is safe to fail.
- KB ingestion redacts secrets before embedding and indexes **backtest artifacts only** — no live/paper data, no PII.
- All providers are request/response (no streaming, no SQS, no polling), consistent with the cost rules.

## 7. Planned infra (not built here)

Bedrock Knowledge Base provisioning + ingestion job, EventBridge rule on run completion, Step Functions state machine, Lambda glue, Glue catalog + Athena workgroup, IAM (read-only over backtest data; no Secrets Manager broker creds; no live/paper access), CloudWatch dashboard + SNS budget alarm. Built in the AWS infra phase, gated by its own report.

## 8. Files

`services/backtesting/genai/{guardrails,bedrock_client,prompts,athena_queries,analyst,__init__}.py` · `configs/backtesting/genai/kb_ingestion_manifest.json` · `tests/backtest/test_genai_layer.py` · `docs/backtesting/aws-serverless-genai-backtesting-design.md`.

## 9. Lab status

Phases 0–11 are implemented at the code+design level: discovery, spec, data lake + validation, run registry + checkpoints, replay engine, strategy adapters, execution simulator, TEE/MIS, metrics + reports, walk-forward, model dataset, and this GenAI layer. **81 lab tests pass.** Remaining: provision the AWS infra (Terraform `environments/backtest/`, worker ASG, tables, buckets, Bedrock/Step Functions/Glue) and ingest real licensed NSE history — the standing Phase-0 blocker.

---

*Implemented + tested. No infra deployed, no live trading, no broker APIs called, no model trained/deployed. Stop for approval before the next phase.*
