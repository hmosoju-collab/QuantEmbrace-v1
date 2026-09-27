# AI-Assisted Strategy Discovery

> ADR-043. **Phase 6 implemented 2026-09-25**: the lifecycle ledger (`qe/research/lifecycle.py`, `python -m qe lifecycle`), AI hypothesis drafts (`qe/ai/hypotheses`, `python -m qe.ai hypothesize`), and the eliminated-family registry (`governance/research-eliminated-families.yaml`). Stress-test / Monte Carlo / DSR-PBO study kinds remain `[PLANNED — not yet implemented]`.

## 1. Flow

```
AI research (qe.ai)                  — may WRITE a hypothesis draft
  ↓
Hypothesis (governance review)       — HUMAN accepts / edits / rejects
  ↓
Formal strategy specification        — a qe.strategy implementation + RunConfig YAML, human-authored or human-reviewed
  ↓
Pre-registration                     — experiment block (name, family, hypothesis, gates) committed BEFORE any run
  ↓
Backtest → walk-forward → OOS        — python -m qe study (existing engine, cost stack mandatory)
  ↓
Stress tests / Monte Carlo / robustness — [PLANNED] study kinds (DSR/PBO per RA-1 §2.4)
  ↓
Strategy review                      — HUMAN
  ↓
GRADUATE (paper candidacy) | GRAVEYARD (retirement register entry with cause of death)
```

## 2. Rules

1. **AI never promotes, graduates or retires anything.** `qe.ai` may write a *hypothesis draft* file for human review. It cannot write the experiment registry, study configs, strategy code or book state.
2. **Every hypothesis debits the family test budget.** `qe.research.registry` counts distinct experiments per family, so a marginal pass in a heavily mined family counts for less. AI makes it cheap to generate hypotheses, which is exactly why this budget has to be enforced. The count is persisted in every ledger record (F-12, fixed `b47e158`) and shown on every AI draft.
3. **Gates are pre-registered and never relaxed.** This is the discipline from the 2026 research program. A study with no gates FAILs (F-11, fixed `a0e4ec1`); AI drafts without a gate are rejected as MALFORMED.
4. **The researched object is the traded object.** Hypotheses become `qe.strategy` code run by the same engine (RA-1 §2.3), never a bespoke loop.
5. **Settled edges are not re-chased without a new angle and fresh data.** See `docs/strategy/research-program-consolidation-2026-06-20.md`. The AI hypothesis generator must be given that memo's elimination list as context, and it must flag re-proposals of eliminated families.

## 3. Lifecycle states (implemented: `qe/research/lifecycle.py`)

Append-only ledger `governance/strategy-lifecycle.jsonl`. Every transition names a human approver (AI/automation identities are refused) and carries the evidence below, verified against the experiment registry where applicable. `qe.ai` cannot import `qe.research`, so AI has no code path to move a strategy:

| State | Meaning | Entry requires |
|---|---|---|
| CANDIDATE | Hypothesis accepted for tracking | Human approver + family + hypothesis ref (e.g. `qe.ai:<draft_id>`) |
| RESEARCH | Specification being written | Human approver |
| BACKTEST | Pre-registered study run | `experiment_id` with a registered run in the ledger |
| VALIDATION | Walk-forward OOS passed | Latest registered run `engine_pass` = true |
| PAPER | Forward paper book (qe paper) | Human approver + a `mode: paper` RunConfig (its hash is recorded) |
| PRODUCTION_ELIGIBLE | Eligible for the separate `qe live` ceremony (not live) | Forward-gate artifact bound to the recorded paper config hash |
| GRAVEYARD | Retired with cause of death | Any gate failure or human decision |

Each state transition records: `strategy_version` (code SHA), `dataset_version` (snapshot ID), `feature_version`, `model_version`, `research_signal_version` (`research_signal/1`) and `backtest_version` (study config hash).
