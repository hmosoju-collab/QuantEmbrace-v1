# AI Research — Failure Handling

> ADR-043. **Principle: AI failure can never cause unsafe trading, because no trading path depends on `qe.ai`.** Inside `qe.ai`, every failure becomes a component status and the deterministic quant path continues.

| Failure | What happens | Status / event | Test |
|---|---|---|---|
| LLM unavailable / provider error | Bounded retries (`max_retries`), then the component fails | `ERROR` · `LLM_CALL.error` = exception type only | `test_ai_llm.py::test_gateway_timeout_exhausts_retries` |
| LLM timeout | Same as above | `TIMEOUT` | `test_ai_agents.py::test_timeout_is_a_status_not_an_exception` |
| Repeated failures | The circuit breaker opens after `breaker_threshold` consecutive failures. There are no further provider calls, and every remaining component is UNAVAILABLE. **The run still completes.** | `UNAVAILABLE` ("circuit breaker open") | `test_ai_orchestration.py::test_breaker_degrades_to_unavailable_and_run_completes` |
| Token budget exhausted | The call is refused before it is made | `UNAVAILABLE` ("token budget exhausted") | `test_ai_orchestration.py::test_budget_exhaustion_degrades_without_crashing` |
| Malformed output (bad JSON, schema violation, extra fields, out-of-range values, unknown evidence ID) | One corrective retry, then the component fails | `MALFORMED` | `test_ai_agents.py::test_persistently_bad_output_is_malformed` |
| Forbidden content in output (orders, going live, kill switch, limits, shell) | Text is dropped, **no retry** | `BLOCKED` | `test_ai_agents.py::test_forbidden_output_is_blocked_without_retry` |
| Secret in a prompt | The call is refused before sending | `ERROR` | `test_ai_llm.py::test_prompt_with_secret_is_refused_before_any_call` |
| No news/fundamentals/sentiment source | The agent makes **zero** LLM calls | `UNAVAILABLE` | `test_ai_agents.py::test_no_data_agents_make_no_llm_call` |
| Missing index data (e.g. INDIAVIX after 2025) | That evidence is omitted, never forward-filled | — | `test_ai_tools_pit.py::test_index_series_is_pit_and_never_forward_filled` |
| Point-in-time violation (evidence after the cutoff) | **That symbol fails closed before any agent sees the evidence** | `SYMBOL_FAILED` | `test_ai_orchestration.py::test_lookahead_evidence_fails_the_symbol_before_any_llm_call` |
| Bug in one symbol's research | The symbol is isolated and the run continues | `SYMBOL_FAILED` | `test_ai_orchestration.py::test_symbol_failure_is_isolated` |
| Unhandled error in the run itself | Sanitized `SESSION_ABORT`, the journal is closed, and the error is re-raised to the CLI | `SESSION_ABORT` | `test_ai_journal.py::test_abort_is_journaled_and_sanitized` |
| Provider 403 / 404 / 400 / 429 on a real backend | Sanitised `type:status` label only (never the provider message); the breaker opens after `breaker_threshold` failures and the run completes with UNAVAILABLE components; `research` prints a loud WARNING and **exits 3** if no call succeeded; `python -m qe.ai probe` prints the actionable hint | `ERROR` · `LLM_CALL.error` | `test_ai_probe.py`, `test_ai_llm.py::test_bedrock_errors_are_sanitized` |
| Provider `stop_reason=refusal` | Component BLOCKED, no retry | `BLOCKED` | `test_ai_agents.py::test_provider_refusal_is_blocked_without_retry` |
| Hostile / secret-like / malformed announcement text | Held for human review or rejected with a reason; never promoted; every record lands in exactly one bucket | `held/` · `rejected/` | `test_ai_corpus.py` |
| Curated corpus line tampered or no longer passes the screen | Dropped on load and counted (`dropped_on_load`) | — | `test_ai_corpus.py::test_store_visibility_is_point_in_time_and_screens_tampered_lines` |
| Post-trade: trade dates not in the panel | Skipped and counted; the run continues | `skipped` in `SESSION_END` | `test_ai_post_trade.py` |
| Paid backend without `--allow-llm-spend` | Refused before any journal or file is written. Exit code 2. | — | `test_ai_cli.py::test_paid_backend_is_refused_without_spend_flag` |
| Lake changed between research and fusion | Fusion refused. Re-run research first. | `REFUSED` (exit 2) | `test_ai_fusion_parity.py::test_fusion_view_annotates_the_engine_record` |
| Weighted fusion requested outside `study` | Refused | `REFUSED` (exit 2) | `test_ai_fusion.py::test_weighted_modes_refused_outside_study` |
| AI signal missing, unavailable or contaminated | Fusion falls back to the quant score (`S = q`, weight 0) | row `ai_usable=false` with a reason | `test_ai_fusion.py::test_unavailable_ai_falls_back_to_quant` |

## Operational note: iCloud-evicted lake files

The repository lives under iCloud-synced `~/Documents`. On 2026-09-25, **1,003 of 7,548 daily lake files (2024–26) were "dataless"**: macOS had evicted them to iCloud. Reading one blocks until it downloads, and a `qe.ai research` run stalled in `create_snapshot` with no CPU use. Any `qe study` or `qe paper` run reads the same files and would stall the same way.

Check and fix before a run:

```bash
find backtest-data/lake -name '*.parquet' -flags +dataless | wc -l   # should be 0
brctl download backtest-data/lake        # or: Finder → Download Now, or keep backtest-data outside iCloud
```
