# AI Research — Observability

> ADR-043. `qe.ai` uses the same observability model as the `qe` engine: **the journal is the ground truth, and everything else is a reader.**

## 1. Research journal — `journals/ai/<run_id>.jsonl`

The format is `qe.journal` JSONL with a monotonic `seq`, and the reader fails closed on gaps. `run_id = ai-<name>-<UTC µs>-<rand6>-<config short hash>`. Research journals are never named `paper-*`, so `qe.live_gate` can never count them as paper sessions.

| Event | Key fields |
|---|---|
| `SESSION_START` | `session_id` (= run_id), `mode="ai-research"`, `config_hash`, full config echo, `code_sha`, `data_snapshot_id` |
| `RUN_MANIFEST` | `as_of`, `decision_date`, `information_cutoff`, `market`, `book_config` and its hash, `quant_spec`, `symbols`, `research_mode`, `backend`, model IDs and `knowledge_cutoffs_used`, `prompts` {version: hash}, `tools_version`, `budget`, `seed`, `mask_identifiers` |
| `TOOL_CALL` | `trace_id`, `tool`, `symbol`, `status`, `reason`, evidence `[{id, value, hash}]` |
| `LLM_CALL` | `agent_id`, `symbol`, `backend`, `model_id`, `prompt_version`, `prompt_hash`, `status`, `attempts`, `cached`, `latency_ms`, `input_tokens`, `output_tokens`, `stop_reason`, `error` |
| `AGENT_OBSERVATION` | `trace_id`, the full `AgentObservation` (status, score/risk_score/confidence, summary, points, evidence_ids, tokens, latency, error) |
| `RESEARCH_SIGNAL` | The full `ResearchSignal` v1 (re-validated when read) |
| `SYMBOL_FAILED` | `symbol`, `trace_id`, sanitized `error` (for example a point-in-time violation) |
| `SESSION_END` | `n_symbols`, `n_signals`, `failed_symbols`, `llm_calls`, `cache_hits`, token totals, `llm_failures`, `by_status` |
| `SESSION_ABORT` | Sanitized `error` (redacted and truncated) |

`trace_id` is per symbol within a run, and `research_id` equals `run_id`. Together they reconstruct every tool call, prompt version, model call and observation behind one signal.

## 2. Secrets

Every payload is redacted recursively before it is written:
- AWS keys, PEM private keys, JWTs, `sk-…`/`sk-ant-…`, bearer tokens;
- `api_key`/`secret`/`password`/`token=` assignments, Zerodha tokens.

Exceptions are sanitized before `SESSION_ABORT`, and provider errors cross the Bedrock adapter as the exception *type* only. Prompts are secret-scanned before sending, and a match refuses the call.

Tests: `test_ai_journal.py::test_llm_text_secrets_are_redacted_in_the_journal`, `::test_abort_is_journaled_and_sanitized`, `test_ai_llm.py::test_prompt_with_secret_is_refused_before_any_call`.

### 1b. Other qe.ai journals (same format, `journals/ai/`, never `paper-*`)

| Journal `mode` | Written by | Extra events |
|---|---|---|
| `ai-hypotheses` | `qe.ai hypothesize` | `HYPOTHESIS_MANIFEST`, `HYPOTHESIS_DRAFT` |
| `ai-post-trade` | `qe.ai post-trade` | `POST_TRADE_MANIFEST`, `POST_TRADE_REVIEW` (the full `PostTradeReview`) |

`RUN_MANIFEST` now also records `corpus_hash` (the curated announcement corpus the run saw) and `tools_version`
`qe_ai_tools/2`. `SESSION_END` carries `llm_failures` and `by_status`; the CLI exits **3** with a warning when every
LLM call failed.

## 3. Derived views — `reports/qe-ai/<run_id>/`

| File | Built by | Content |
|---|---|---|
| `signals.jsonl` | `python -m qe.ai report` (also run automatically after `research`) | One validated `ResearchSignal` per line |
| `summary.md` | same | Provenance table, contamination count, regime, per-symbol scores and statuses, bull/bear/consensus, LLM usage, failed symbols |
| `dashboard/index.html` | `python -m qe.ai dashboard` | One static page (no JavaScript, CSP `default-src 'none'`): regime, quant score, strategy signal, AI score/confidence, risk, AI recommendation **next to** the QuantEmbrace final decision, bull/bear/consensus/conflicts, shadow gate, post-trade reviews, hypothesis drafts, lifecycle |
| `post_trade/<engine_session>/{reviews.jsonl,report.md}` | `python -m qe.ai post-trade` | Deterministic classifications + the model's lesson, each review stamped knowable at the trade's exit close |
| `fusion-<mode>-<context>.jsonl` / `.md` | `python -m qe.ai fuse` | The research view: market regime, quant score, strategy signal, AI score and confidence, AI recommendation, risk flags, **QuantEmbrace decision**, agreement, contamination, divergences, bull/bear/consensus/conflicting evidence, and optionally the engine's actual record that day |

These files can be deleted and rebuilt from the journal at any time. **Nothing is ever written under `reports/qe/`**, which the forward and live gates read as evidence.

## 4. Relationship to engine observability

- `python -m qe report --journal journals/paper-…` still covers engine sessions. It ignores `qe.ai` entirely.
- The research view reads an engine journal with `--engine-journal`, read-only.
- CloudWatch/Prometheus: `qe.ai` publishes no metrics in P0–P5, because it is an offline CLI. If research is scheduled later, `SESSION_END` counts (calls, failures, tokens, cache hits) are the natural metric source `[PLANNED]`.
