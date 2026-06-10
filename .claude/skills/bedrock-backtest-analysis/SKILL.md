---
name: bedrock-backtest-analysis
description: Generate on-demand Bedrock (Claude) analysis, reports, and RAG Q&A over backtest outputs. Use to narrate a run, compare runs/studies, or answer questions over run history. Advisory only — event-driven, no SQS/polling, never changes trading behavior or enables live.
---

# Bedrock Backtest Analysis

Authoritative design: `docs/backtesting/aws-serverless-genai-backtesting-design.md`.

## When to use
Turning `metrics.json`/`trades.parquet`/`aggregate.json` into a narrative `report.md`; cross-run comparison; operator Q&A over run history.

## Procedure
1. Trigger on run `COMPLETED` (EventBridge -> Step Functions) or explicit operator request.
2. Build redacted context from registry summaries + S3 artifacts (backtest data only).
3. Call Bedrock with a pinned model + token cap + versioned prompt template.
4. Persist `report.md` + `genai_summary`; on failure, keep the deterministic non-AI report.

## Guardrails (hard)
No SQS, no polling/streaming. No live/paper access, no broker creds, no secrets/PII in prompts. Advisory only — never auto-apply, trigger trades, or promote. Bounded cost with SNS budget alarm.
