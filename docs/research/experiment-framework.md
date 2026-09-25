# Experiment Framework (AI + Quant)

> ADR-043. The research-run manifest is built in P2–P4 and is `[PLANNED — not yet implemented]` until then. Experiment-registry integration and the forward shadow ledger are `[PLANNED — not yet implemented]` (P6).

## 1. What already exists (quant side)

- **Experiment registry** (`qe/research/registry.py`, `governance/experiment-registry.jsonl`):
  - `experiment_id = sha256(name, hypothesis)`, grouped by family, with gates declared in `RunConfig.experiment`.
  - Walk-forward studies register `session_id`, `config_hash`, `data_snapshot_id`, `code_sha` and the pass/fail metrics.
- **Reproducibility primitives:**
  - The frozen config hash.
  - Data snapshot manifests (`ds-…`, re-verifiable).
  - `code_sha` with a `-dirty` flag.
  - The append-only journal.

## 2. AI research run manifest

Every `python -m qe.ai research` run records the following in its journal header (`SESSION_START` and `RUN_MANIFEST`):

| Brief field | Where recorded |
|---|---|
| `experiment_id` | `run_id` (the research run ID). An `experiment_id` is attached when a P6 study consumes the run. |
| `git_commit` | `code_sha` (`qe.version.code_version`, `-dirty` if the tree is dirty) |
| `dataset_version` | `data_snapshot_id` (pinned at run start) |
| `strategy_version` | Book-config hash plus factor parameters (the engine pick the research annotates) |
| `feature_version` | `qe.ai` tool version (`TOOLS_VERSION`) plus the evidence IDs used |
| `model_version` | `model_id` per tier, plus `knowledge_cutoff` |
| `prompt_version` | Per-agent `prompt_version` plus a SHA-256 of the prompt template text |
| `LLM_model` | `model_id` / backend (`fake` or `bedrock`) |
| `parameters` | The full `ResearchRunConfig` echo (hash computed with `exclude_none`) |
| `random_seed` | `seed` (drives the fake LLM and any tie-breaking) |
| `start_date` / `end_date` | `as_of` (a research run is one decision date); panel window `[as_of − 2y, as_of]` |
| `training_period` / `validation_period` / `test_period` | Not applicable to single-date research. For P6 forward shadow evaluation, the test period must start after `knowledge_cutoff + guard` ([lookahead-prevention §2](lookahead-prevention.md)). |

## 3. Reproducibility guarantee

Given the same config, snapshot, code SHA and LLM cache, a research run replays **byte-identically** (except timestamps and run ID):

- The fake backend is deterministic; its output is a function of the prompt and the seed.
- The real backend is made replayable by the content-addressed cache: `sha256(model_id, prompt_version, system, prompt, max_tokens, temperature)` → the stored response.
- Prompts never contain `run_id`, `trace_id` or wall-clock time, so the cache keys are stable across runs.

Tested in `test_ai_orchestration.py::test_replay_from_cache_is_identical`.

## 4. Forward shadow ledger — `[PLANNED — not yet implemented]` (P6)

This is how an AI score could ever earn weight:

1. **Pre-register** `configs/qe_ai_shadow_gate.yaml`: the metric (for example the rank IC of `ai_score` against forward 21-day return, net of the quant score), thresholds, minimum months, and the model ID. It is committed before accrual starts.
2. **Accrue.** Each month-end, run research on the engine basket plus a control sample, only for decision dates after `knowledge_cutoff + guard_days`. Append to `governance/ai-shadow-ledger.jsonl`.
3. **Score** outcomes after they are realized, using knowledge-time-correct returns.
4. **Evaluate** with a `check_ai_shadow_gate.py` that mirrors `check_forward_gate.py`. It fails closed and never relaxes.
5. **Pass means a human review, not an automatic weight change.** Any change to `ai_weight` or to the engine requires a new ADR and operator approval.
