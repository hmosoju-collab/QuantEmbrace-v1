"""Bear researcher: the strongest evidence-based case for underperformance."""

from qe.ai.agents.base import AgentSpec

SPEC = AgentSpec(
    agent_id="bear",
    prompt_version="bear/1",
    schema_id="debate/1",
    role="Bear researcher",
    task=(
        "Make the strongest evidence-based case that this equity underperforms its market over "
        "the next 1-3 months. If a bull argument is present, answer it directly. Cite evidence "
        "ids; do not claim facts that are not in the evidence."
    ),
)
