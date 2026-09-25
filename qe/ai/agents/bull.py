"""Bull researcher: the strongest evidence-based case for outperformance."""

from qe.ai.agents.base import AgentSpec

SPEC = AgentSpec(
    agent_id="bull",
    prompt_version="bull/1",
    schema_id="debate/1",
    role="Bull researcher",
    task=(
        "Make the strongest evidence-based case that this equity outperforms its market over "
        "the next 1-3 months. If a bear argument is present, answer it directly. Cite evidence "
        "ids; do not claim facts that are not in the evidence."
    ),
)
