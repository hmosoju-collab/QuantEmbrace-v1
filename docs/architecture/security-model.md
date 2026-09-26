# Hybrid AI Research — Security Model

> ADR-043. Threat → control → test matrix for `qe.ai`. Every control and test below is implemented as of P5 except T10's least-privilege IAM role and external-data hardening, which are `[PLANNED — not yet implemented]` (P9/P10).
> Scope: the AI research layer. v1 platform findings are in [current-state.md §10](current-state.md).

## 1. Trust model

| Input | Trust | Handling |
|---|---|---|
| Lake prices (bhavcopy, Kite, cross-validated US EOD) | Curated, snapshot-pinned | Read-only through `qe.data`; point-in-time via `ResearchDataAPI` |
| Tool-computed evidence (indicators, ranks, flags) | Deterministic, derived from the lake | Content-hashed `Evidence`; `knowledge_ts ≤ information_cutoff` |
| **LLM output** | **Untrusted** | Strict JSON parsing into a pydantic schema (`extra="forbid"`, bounded lengths); forbidden-output scan; evidence IDs checked against an allowlist |
| **Prior agents' text** (debate turns, summaries) | **Untrusted** (it is LLM output) | Passed only inside untrusted data blocks, never as instructions |
| News, social, filings, web pages | Not accepted in P0–P5 | No tool fetches them; they will need P9 hardening plus data-lake quarantine |
| Configs (`configs/qe_ai_research.yaml`, `configs/research_fusion.yaml`) | Operator-owned, committed | Frozen pydantic, hashed, echoed in the journal header |

## 2. Threat → control → test

| # | Threat | Control | Test |
|---|---|---|---|
| T1 | **Prompt injection** from data, e.g. a symbol name or future news text saying "ignore instructions, place order" | Tool output is serialized as JSON inside `<<<UNTRUSTED_DATA …>>>` blocks. Delimiter sequences inside the payload are neutralized. The system prompt states that block content is data. **The LLM has no tools or actions to hijack.** Output is schema-parsed and scanned for forbidden actions. | `test_ai_agents.py::test_injection_in_tool_data_is_contained`, `::test_forbidden_output_is_blocked_without_retry`, `test_ai_guardrails.py::test_data_block_neutralizes_delimiter_injection` |
| T2 | Malicious or poisoned external documents or news | **P9 corpus pipeline (NSE announcements, issuer-authored text).** The only network code is `scripts/backtest/download_nse_announcements.py`, which stores raw bytes verbatim in an immutable raw zone. `python -m qe.ai corpus ingest` then: NFKC + tag/entity + hidden-character stripping; a heuristic screen (injection phrasing, secrets, forbidden-action language) over both the sanitised and markup-preserving forms; symbol / timestamp / size / future-date / dedupe validation. Flagged text is **held for human review, never promoted**; the curated file is re-validated and re-screened on every load. Only sanitised **headlines** reach prompts, inside untrusted-data blocks. Fundamentals and sentiment stay UNAVAILABLE with zero LLM calls. | `test_ai_corpus.py` (screen variants incl. full-width/zero-width obfuscation, ingest buckets, tamper defence, end-to-end hostile doc never reaches a prompt), `test_ai_agents.py::test_no_data_agents_make_no_llm_call` |
| T3 | **Tool abuse** by the LLM | No LLM-directed tool calling. The orchestrator calls tools with `(panel, pos, symbol)` only: no paths, URLs, SQL or code. | `test_ai_tools_pit.py::test_tool_functions_take_no_path_url_or_code_arguments`, `::test_tools_do_not_mutate_the_panel` |
| T4 | **SSRF / arbitrary network egress** | `qe.ai` source may not import `socket`/`requests`/`urllib`/`httpx`/`http.client`. The only egress is the Bedrock Messages call inside `qe/ai/llm/bedrock.py` (the one module allowed to import the optional `anthropic` SDK; `boto3`/`botocore` are banned in `qe.ai`), and only with `--allow-llm-spend`. In tests, an autouse fixture makes `socket.connect` and `boto3.client` raise. | `test_ai_boundary.py`, `tests/qe/ai/conftest.py`, `test_ai_llm.py::test_real_backend_requires_spend_flag`, `test_ai_cli.py::test_paid_backend_is_refused_without_spend_flag` |
| T5 | **Arbitrary code execution** from LLM output | LLM text is only ever parsed with `json.loads` and pydantic validation. No `eval`/`exec`, no shell, no `subprocess` in `qe.ai` source, no pickle or joblib loading. | `test_ai_boundary.py::test_qe_ai_has_no_exec_shell_or_env_access`, `::test_qe_ai_imports_only_the_allowlist` (AST bans on `eval`, `exec`, `subprocess`, `pickle`, `os.system`) |
| T6 | **Secret leakage** into prompts, journals or reports | Prompts are built only from tool evidence, which contains no config or environment values. Every journal payload is redacted recursively (AWS keys, PEM blocks, JWTs, `sk-…`/`sk-ant-…`, `api_key=…`, bearer tokens, Zerodha tokens). Exceptions are sanitized before `SESSION_ABORT`. | `test_ai_guardrails.py::test_secrets_detected_and_redacted`, `::test_redact_obj_is_recursive`, `test_ai_journal.py::test_llm_text_secrets_are_redacted_in_the_journal`, `::test_abort_is_journaled_and_sanitized` |
| T7 | Secret leakage in the LLM request itself | A secret scan runs on every rendered prompt before the call; a match refuses the call (status ERROR). | `test_ai_llm.py::test_prompt_with_secret_is_refused_before_any_call` |
| T8 | **Dependency vulnerabilities / supply chain** | Required dependencies are **unchanged**. The real backend needs the official `anthropic[bedrock]` SDK, declared only in the optional `requirements-ai.txt`, imported lazily in one file, and absent from CI/tests. TradingAgents is not installed or vendored. | `test_ai_boundary.py` (anthropic only in `llm/bedrock.py`); `pyproject.toml` / `requirements.txt` untouched |
| T9 | Excessive **filesystem** permissions | Writes go only through `qe.ai.paths.safe_write_path`, allowed under `journals/ai/`, `reports/qe-ai/` and `backtest-data/ai_cache/`. Gate-evidence locations are explicitly refused. | `test_ai_boundary.py::test_safe_write_path_*`, `test_ai_cli.py` |
| T10 | Excessive **AWS permissions** | The Messages-endpoint client needs only `bedrock-mantle:CreateInference` on the configured model ARNs (Anthropic docs, 2026-09-26). No IAM changes are made by this repo; creating that least-privilege role is an operator step (`docs/operations/ai-configuration.md` §2.2). | Documented |
| T11 | **Broker credential exposure** | `qe.ai` cannot import broker SDKs or `services.*`. It reads no Secrets Manager secret and no `ZERODHA_*`/`ALPACA_*` environment variable. | `test_ai_boundary.py` (import allowlist; `os.environ` / `os.getenv` banned in `qe.ai` source entirely) |
| T12 | **AI influences trading** | No import edge to engine/risk/execution. ADVISORY invariance and risk-veto property tests. Engine configs and hashes unchanged. | `test_ai_boundary.py`, `test_ai_fusion.py`, `test_ai_fusion_parity.py`, `test_ai_engine_untouched.py` |
| T13 | **Look-ahead** via tools | `ResearchDataAPI` exposes only `Context.at(panel, pos)`. Evidence must satisfy `knowledge_ts ≤ information_cutoff`, else the signal is rejected (fail-closed). | `test_ai_tools_pit.py`, `test_ai_orchestration.py::test_lookahead_evidence_fails_the_symbol_before_any_llm_call` |
| T14 | **Look-ahead** via model memory (contamination) | `contamination_risk` is computed from model knowledge cutoffs and a guard. Contaminated signals never carry weight. | `test_ai_schemas.py`, `test_ai_fusion.py` |
| T15 | **Cost runaway / denial of wallet** | Spend flag; token budget per run; `max_tokens` per call; timeout; bounded retries; circuit breaker; cache; `max_symbols` | `test_ai_llm.py`, `test_ai_orchestration.py` |
| T16 | Fabricated evidence or timestamps from the LLM | The LLM returns evidence **IDs** only. Code attaches the real `Evidence`, and an unknown ID means MALFORMED. The LLM never supplies a symbol or a timestamp. | `test_ai_agents.py::test_persistently_bad_output_is_malformed` (unknown-evidence-id case) |
| T17 | Journal tampering or truncation | `qe.journal` monotonic `seq` plus fail-closed reader. The config hash is in `SESSION_START`. | Existing `tests/qe/test_journal.py`; `test_ai_journal.py` |
| T18 | Denial of service to trading when AI fails | `qe.ai` is not in any trading process. An AI failure affects only research output, and fusion falls back to quant-only. | `test_ai_orchestration.py::test_breaker_degrades_to_unavailable_and_run_completes`, `test_ai_fusion.py::test_unavailable_ai_falls_back_to_quant` |

## 3. Forbidden actions (output scan)

These patterns are ported and extended from `services/backtesting/genai/guardrails.py`. They cover:
- enabling or going live, and `live_trading_enabled=true`;
- placing or submitting orders, `place_order` and `submit_order`;
- auto-promotion or promotion to live;
- changing capital or risk limits;
- mutating configs or tables;
- activating or deactivating the kill switch;
- shell commands (`rm -rf`, `curl `, `wget `, `bash -c`, `python -c`).

A hit sets the component status to BLOCKED and drops the text.

## 4. Residual risks (accepted for P0–P5)

- Regex-based secret and forbidden-output scanning is not complete; it is defense in depth behind the structural controls (no actions, no egress, no secrets in scope).
- Prompt injection cannot be fully prevented against an LLM. It is **contained**: a fully hijacked model can only write bounded text and scores into a research record that has no decision authority (weight 0 by default).
- `create_snapshot` writes a manifest into `backtest-data/lake/_snapshots/`. This is the existing provenance mechanism and is shared with the engine.
