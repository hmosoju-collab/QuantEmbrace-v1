"""Hypothesis agent: proposes testable, pre-registrable research hypotheses.

It only drafts. Code flags eliminated-family re-proposals, checks data
availability, and counts the family's test budget; a human decides whether a
draft enters the strategy lifecycle (qe lifecycle, which qe.ai cannot call).
"""

from qe.ai.agents.base import AgentSpec

SPEC = AgentSpec(
    agent_id="hypothesis",
    prompt_version="hypothesis/1",
    schema_id="hypothesis/1",
    role="Research hypothesis generator",
    task=(
        "From the research evidence, propose at most three falsifiable hypotheses that a "
        "backtest can test, each with pre-registered gates on the listed metrics. Do not "
        "re-propose a settled family listed in the data unless you state a genuinely new "
        "angle and the fresh data it needs. Prefer hypotheses testable with available data."
    ),
    tier="deep",
)
