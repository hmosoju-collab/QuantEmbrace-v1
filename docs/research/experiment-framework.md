# Experiment Framework (AI + Quant)

> ADR-043. The research-run manifest (§2) and replay guarantee (§3) are implemented as of P4. Experiment-registry integration and the forward shadow ledger (§4) are `[PLANNED — not yet implemented]` (P6).

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

## 4. Forward shadow gate — implemented as a DRAFT pre-registration (P6)

This is how an AI score could ever earn weight:

1. **Pre-register** `configs/qe_ai_shadow_gate.yaml` (committed as **DRAFT**): monthly Spearman IC of `ai_score` residualized on the factor rank `q` vs the 21-trading-day forward return; thresholds mirror the Forward Factor Gate. A human signs off (binding the research-config hash and model; `python -m qe.ai shadow --show-binding`) before accrual starts; the evaluator refuses a verdict until then.
2. **Accrue.** Each month-end, `python -m qe.ai research` on the book basket. The research journals themselves are the ledger (`journals/ai/`); only decision dates after sign-off count, only uncontaminated signals, and only the **first** run per date (re-runs cannot cherry-pick).
3. **Score** outcomes after they are realized, using knowledge-time-correct returns.
4. **Evaluate** with `python -m qe.ai shadow` (mirrors `check_forward_gate.py`): fails closed, counts every exclusion, never relaxes. A constant or factor-parroting AI score scores IC 0.0 — counted, not dropped.
5. **Pass means a human review, not an automatic weight change.** Any change to `ai_weight` or to the engine requires a new ADR and operator approval.
