# AWS Backtesting Lab — Phase 10 Report: Serverless GenAI Layer

**Status:** COMPLETE — awaiting human approval before Phase 11  
**Date:** 2026-06-14  
**Governance rule:** Every phase writes a report and stops. A human approves before the next phase begins.

---

## Scope

Phase 10 verifies the serverless GenAI analysis layer, closes 6 coverage gaps, and confirms the
critical safety invariant: `recommend_promotion` is always `False` regardless of evidence quality.
All GenAI code was pre-existing from a prior session. **Advisory only** — no broker orders, no
live enablement, no config/capital mutation, no strategy auto-promotion. AWS infra is `[PLANNED]`.

---

## What Was Already Implemented

| File | Status |
|---|---|
| `services/backtesting/genai/guardrails.py` | Pre-existing — complete |
| `services/backtesting/genai/bedrock_client.py` | Pre-existing — complete |
| `services/backtesting/genai/prompts.py` | Pre-existing — complete |
| `services/backtesting/genai/athena_queries.py` | Pre-existing — complete |
| `services/backtesting/genai/analyst.py` | Pre-existing — complete |
| `tests/backtest/test_genai_layer.py` (5 tests) | Pre-existing — all passing |

---

## Design Summary

### `guardrails.py` — four enforcement layers

| Layer | Mechanism |
|---|---|
| Secrets | `_SECRET_PATTERNS` (AWS key, sk-, api_key=, zerodha token); `scan_for_secrets` → `SecretLeakError` before any prompt |
| Redaction | `redact_secrets` scrubs patterns to `[REDACTED]` in context text and in model responses |
| Forbidden actions | `_FORBIDDEN_PATTERNS` (enable live, place order, submit_order, auto-promote, set capital, mutate config); blocks in **untrusted context** and **model response** |
| Citations | Reports require `≥1 source`; `ensure_sources_section` appends if missing; `assert_cited` verifies at least one source id appears |

**Note:** `guardrails.py` legitimately contains the strings `place_order` and `submit_order` as regex pattern literals (what it blocks). The no-broker test correctly checks for broker *imports* rather than string occurrences.

### `GenAIAnalyst` — no action channel

| Method | Category | Requires sources |
|---|---|---|
| `generate_report(ctx)` | Report | Yes |
| `analyze_strategy(ctx)` | Report | Yes |
| `analyze_tee(ctx)` | Report | Yes |
| `explain_dataset(ctx)` | Report | Yes |
| `risk_governance(evidence)` | Governance | No (verdict in code) |

The analyst exposes **zero** action methods. `risk_governance` computes the verdict deterministically
via `evaluate_evidence`; the LLM only explains it and is safe to fail (explanation silently drops
if the model emits a forbidden directive).

### `evaluate_evidence` — governance thresholds (mirrors CLAUDE.md)

| Condition | Blocks |
|---|---|
| `valid_sessions < 5` | Yes |
| `oos_gates_pass` is False | Yes |
| `expectancy ≤ 0` | Yes |
| `profit_factor ≤ 1.2` | Yes (strict; exactly 1.2 blocks) |
| `realized_pnl ≤ 0` | Yes |
| `reconciliation_mismatches ≠ 0` | Yes |

**`recommend_promotion` is permanently `False`** — for both `BLOCK` and `ADVISORY_OK` verdicts.
`ADVISORY_OK` means "evidence supports continued evaluation," not "approved for live."

### `bedrock_client.py` — injectable providers

| Provider | Description |
|---|---|
| `BedrockProvider` | Default (IAM-native; no API key); injectable `runtime_client` for tests |
| `AnthropicProvider` | Matches repo's existing `StrategySelector` pattern; lazy `anthropic` import |
| `StubProvider` | Deterministic, offline; supports `str` or `Callable[[str], str]` response |
| `get_provider(name, **kw)` | Factory; raises `ValueError` for unknown provider names |

### `prompts.py` — 5 advisory-only templates

All templates embed `SYSTEM_PREAMBLE` (advisory-only, cite sources, never act) and end with a
`## Sources` citation directive. Templates: `report_prompt`, `strategy_analysis_prompt`,
`tee_analysis_prompt`, `model_dataset_prompt`, `risk_governance_prompt`.

### `athena_queries.py` — 5 read-only SQL templates

| Query | Returns |
|---|---|
| `top_runs_by_expectancy` | Top N completed runs sorted by expectancy |
| `gate_passing_runs` | Runs where all three live-readiness gates pass |
| `exit_reason_distribution` | Exit-reason breakdown for a specific run |
| `mis_dependency_by_strategy` | Average MIS dependency per strategy |
| `lookahead_violations_check` | Runs with lookahead violations (should always be empty) |

All templates contain only `SELECT` / `FROM` / `WHERE` / `GROUP BY` / `ORDER BY` — no writes,
no DDL. `render(name, **params)` raises `KeyError` for unknown query names.

---

## Tests

Phase 10 added 6 tests; combined total is 11 in `tests/backtest/test_genai_layer.py`.

**Pre-existing (5):**

| Test | Covers |
|---|---|
| `test_genai_prompt_does_not_include_secrets` | Secrets scrubbed from context before prompt; `SecretLeakError` on raw secrets |
| `test_report_generation_uses_cited_sources` | `## Sources` in result; `CitationError` when sources empty |
| `test_risk_governance_blocks_insufficient_evidence` | BLOCK on weak evidence; ADVISORY_OK + forbidden response dropped |
| `test_no_action_prompt_can_enable_live` | `ForbiddenActionError` on request + response; no action methods on analyst |
| `test_bedrock_client_stubbed` | `StubProvider` + `BedrockProvider` with injected fake runtime (no AWS) |

**New (6):**

| Test | Covers |
|---|---|
| `test_redact_secrets_transforms_patterns` | `redact_secrets` removes patterns; result passes `assert_no_secrets` |
| `test_analyst_strategy_tee_dataset_methods` | `analyze_strategy`, `analyze_tee`, `explain_dataset` all return `AnalysisResult`; each raises `CitationError` without sources |
| `test_evaluate_evidence_boundary_conditions` | Exactly 5 sessions → ADVISORY_OK; `profit_factor=1.2` exact → BLOCK; `recommend_promotion=False` on both verdicts |
| `test_get_provider_factory` | `get_provider("stub")` → `StubProvider`; unknown name → `ValueError` |
| `test_athena_queries_render_and_guard` | Param substitution correct; unknown query → `KeyError`; all templates read-only (no write DDL) |
| `test_no_broker_calls_in_genai_modules` | All 5 genai modules contain no broker import statements |

Full suite: **161 passed, 0 failed, 0 warnings** (`tests/backtest/` — all phases).

---

## File Manifest

```
# Python (modified — tests extended)
tests/backtest/test_genai_layer.py    (+6 tests, 5→11)
```

---

## Safety Invariants Verified

| Invariant | How verified |
|---|---|
| Secrets never enter a prompt | `test_genai_prompt_does_not_include_secrets` (scan → redact → clean prompt) |
| `redact_secrets` renders output safe | `test_redact_secrets_transforms_patterns` (result passes `assert_no_secrets`) |
| Forbidden actions blocked in context | `test_no_action_prompt_can_enable_live` (ForbiddenActionError on request) |
| Forbidden actions blocked in response | `test_no_action_prompt_can_enable_live` + `test_risk_governance_blocks_insufficient_evidence` |
| Reports must cite sources | `test_report_generation_uses_cited_sources` + `test_analyst_strategy_tee_dataset_methods` |
| `recommend_promotion` is always False | `test_evaluate_evidence_boundary_conditions` (both BLOCK and ADVISORY_OK) |
| Governance verdict computed in code, not by LLM | `test_risk_governance_blocks_insufficient_evidence` (verdict stands when response is forbidden) |
| Analyst has no action channel | `test_no_action_prompt_can_enable_live` (no `enable_live`, `place_order`, etc. methods) |
| Athena queries are read-only | `test_athena_queries_render_and_guard` (no INSERT/UPDATE/DELETE/DROP in any template) |
| No broker imports in GenAI modules | `test_no_broker_calls_in_genai_modules` (checks `import kiteconnect/alpaca/zerodha`) |
| 161 tests passing, 0 warnings | ✅ Confirmed: `python -m pytest tests/backtest/ -q` |

---

## Planned AWS Infra (Not Built Here)

The following are `[PLANNED]` — design exists in `docs/backtesting/aws-serverless-genai-backtesting-design.md`:

- Bedrock Knowledge Base provisioning + ingestion job (backtest artifacts only; redact before embed)
- EventBridge rule triggering on S3 run-complete event
- Step Functions state machine orchestrating GenAI analysis flow
- Lambda glue functions
- Glue catalog + Athena workgroup over `quantembrace-backtest-results/`
- IAM: read-only over backtest data; no Secrets Manager broker credentials; no live/paper access
- CloudWatch dashboard + SNS budget alarm

None of these are required for the code layer to be correct and testable. The injectable client
pattern in `BedrockProvider` and `StubProvider` ensures the analysis logic can be validated
fully offline.

---

## Phase 11 Preview (NOT STARTED)

Phase 11 scope: **AWS Infrastructure** (Terraform)

At this point all lab phases 1–10 are implemented at the code level. Phase 11 would:
- Write the `environments/backtest/` Terraform modules (S3, DynamoDB, EC2 ASG, Bedrock KB, Step Functions, IAM)
- Validate the `qe-bt-` table prefix guard and IAM boundary (deny on non-`qe-bt-*` tables and production buckets)
- Run `terraform plan` (read-only; no `apply` without explicit operator approval)
- Write the Phase 11 report

Phase 11 does **not** begin until this report is approved.

---

## Approval Required

Per governance: **a human must approve this report before Phase 11 begins.**

Checklist for approver:
- [ ] Guardrail design accepted (secrets → `SecretLeakError`; forbidden actions blocked in context + response; citations enforced)
- [ ] `recommend_promotion=False` invariant accepted for all verdict types
- [ ] `evaluate_evidence` thresholds accepted (mirrors CLAUDE.md: ≥5 sessions, OOS gates, expectancy>0, PF>1.2, P&L>0, 0 recon mismatches)
- [ ] Provider abstraction accepted (Bedrock default; AnthropicProvider for parity; StubProvider for tests)
- [ ] Athena queries accepted as read-only (no write DDL)
- [ ] No-broker invariant accepted (import-level check on all 5 modules)
- [ ] Advisory-only framing accepted (GenAI can explain, never act; verdict computed in code)
- [ ] 161 tests passing, 0 warnings (`python -m pytest tests/backtest/ -q`)
- [ ] Phase 11 scope (AWS Infrastructure / Terraform) understood and approved
