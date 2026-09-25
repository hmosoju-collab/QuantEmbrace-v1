# AI-Assisted Strategy Discovery

> ADR-043. **`[PLANNED — not yet implemented]`: Phase 6.** Design only. Nothing in this document exists in code.

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
2. **Every hypothesis debits the family test budget.** `qe.research.registry` counts distinct experiments per family, so a marginal pass in a heavily mined family counts for less. AI makes it cheap to generate hypotheses, which is exactly why this budget has to be enforced. Current-state F-12 (the count is not persisted) must be fixed before P6.
3. **Gates are pre-registered and never relaxed.** This is the discipline from the 2026 research program. A study with no gates must FAIL; current-state F-11 (`walkforward.py:157`) must be fixed before P6.
4. **The researched object is the traded object.** Hypotheses become `qe.strategy` code run by the same engine (RA-1 §2.3), never a bespoke loop.
5. **Settled edges are not re-chased without a new angle and fresh data.** See `docs/strategy/research-program-consolidation-2026-06-20.md`. The AI hypothesis generator must be given that memo's elimination list as context, and it must flag re-proposals of eliminated families.

## 3. Lifecycle states (proposed, to be codified in P6)

The repo has no lifecycle state in code today ([current-state §5](../architecture/current-state.md)). The proposal maps the brief's states onto existing governance:

| State | Meaning | Entry requires |
|---|---|---|
| CANDIDATE | Hypothesis draft exists | AI or human draft |
| RESEARCH | Human accepted; specification being written | Human acceptance |
| BACKTEST | Pre-registered study committed and run | Experiment block + gates committed |
| VALIDATION | Walk-forward / OOS / robustness passed | All pre-registered gates pass |
| PAPER | Forward paper book running (qe paper) | Human paper-candidacy review |
| PRODUCTION_ELIGIBLE | Forward gate passed; eligible for a live-gate ceremony | Forward gate + `qe live` evidence + human approval |
| GRAVEYARD | Retired with cause of death | Any gate failure or human decision |

Each state transition records: `strategy_version` (code SHA), `dataset_version` (snapshot ID), `feature_version`, `model_version`, `research_signal_version` (`research_signal/1`) and `backtest_version` (study config hash).
