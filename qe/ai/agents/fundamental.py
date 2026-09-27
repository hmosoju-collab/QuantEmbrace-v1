"""Fundamental analyst. No fundamentals source exists (prices-only lake), so
this agent returns UNAVAILABLE without an LLM call until a vetted source lands
(ADR-043, P9 [PLANNED])."""

from qe.ai.agents.base import AgentSpec

SPEC = AgentSpec(
    agent_id="fundamental",
    prompt_version="fundamental/1",
    schema_id="analyst/1",
    role="Fundamental analyst",
    task=(
        "Assess valuation, earnings quality and balance-sheet evidence as filed. Score the "
        "fundamental outlook; use only point-in-time filings provided as evidence."
    ),
)
