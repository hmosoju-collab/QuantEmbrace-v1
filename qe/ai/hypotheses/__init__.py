"""AI-assisted hypothesis drafts (ADR-043 P6) — CANDIDATE drafts for human review.

    research journal ──► hypothesis agent ──► HypothesisDraft (code-annotated)
                                               └─► reports/qe-ai/hypotheses/<research_id>/

Nothing here registers an experiment, writes governance, or touches the
strategy lifecycle. A human reads the drafts and, if one is worth testing,
runs ``python -m qe lifecycle transition --to CANDIDATE ... --approved-by <you>``
and writes a pre-registered study config.
"""

from qe.ai.hypotheses.pipeline import (
    AVAILABLE_DATA,
    HypothesisDraft,
    HypothesisRun,
    generate_hypotheses,
    load_eliminated_families,
    match_eliminated,
)

__all__ = [
    "AVAILABLE_DATA",
    "HypothesisDraft",
    "HypothesisRun",
    "generate_hypotheses",
    "load_eliminated_families",
    "match_eliminated",
]
