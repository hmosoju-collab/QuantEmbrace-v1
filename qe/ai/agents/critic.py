"""Critic: audits analysts + debate for contradictions and unsupported claims."""

from qe.ai.agents.base import AgentSpec

SPEC = AgentSpec(
    agent_id="critic",
    prompt_version="critic/1",
    schema_id="critic/1",
    role="Research critic",
    task=(
        "Audit the analyst reports and the bull/bear debate. List contradictions between them "
        "and any claim that the cited evidence does not support. Do not add a directional view."
    ),
)
