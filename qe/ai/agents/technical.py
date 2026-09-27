"""Technical analyst: trend, momentum, mean reversion, volatility, factor rank."""

from qe.ai.agents.base import AgentSpec

SPEC = AgentSpec(
    agent_id="technical",
    prompt_version="technical/1",
    schema_id="analyst/1",
    role="Technical analyst",
    task=(
        "Assess the price trend, momentum, mean-reversion and volatility picture, and the "
        "book's own factor rank, from the evidence. Score the 1-3 month directional outlook."
    ),
)
