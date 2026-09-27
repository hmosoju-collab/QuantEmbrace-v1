"""Market-regime analyst: one market-level view per research run."""

from qe.ai.agents.base import AgentSpec

SPEC = AgentSpec(
    agent_id="regime",
    prompt_version="regime/1",
    schema_id="analyst/1",
    role="Market regime analyst",
    task=(
        "Assess the broad market regime from the market-proxy and volatility evidence. Score "
        "how supportive the environment is for long equity exposure over the next 1-3 months."
    ),
)
