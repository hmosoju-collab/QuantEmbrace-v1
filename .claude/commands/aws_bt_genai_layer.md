---
description: "Phase 10 — on-demand serverless Bedrock analysis/report/RAG over backtest outputs (advisory only)."
---

# /aws_bt_genai_layer — Phase 10: Serverless GenAI Layer

Build the on-demand GenAI analysis layer per `docs/backtesting/aws-serverless-genai-backtesting-design.md`. **Advisory only.** EventBridge + Step Functions + Bedrock. **No SQS, no polling, no streaming.**

## Load first
`aws-serverless-genai-backtesting-design.md`, `aws-backtest-run-registry.md`, `aws-backtesting-steering.md` (§3 boundaries).

## Do
1. EventBridge rule on run `COMPLETED` -> Step Functions -> context builder (redacted) -> Bedrock -> persist `report.md` + `genai_summary`.
2. Implement RAG Q&A over registry summaries + S3 artifacts (backtest data only).
3. Enforce guardrails: no live/paper access, no secrets/PII in prompts, no auto-apply, per-run token cap, budget alarm via SNS.
4. On GenAI failure: log + alert; keep the deterministic non-AI `report.md` (degrade, don't fail).

## Safety
GenAI may observe/analyze/recommend; it must never change trading behavior, place orders, mutate trading state, or enable live.

## Output / Stop
GenAI governance report (data flow, guardrails, sample report). **Stop for approval.**
