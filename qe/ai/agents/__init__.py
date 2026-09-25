"""qe.ai agents — specs (role, task, versioned prompt, output schema) run by
``qe.ai.agents.base.run_agent``. Agents have no tools and no side effects."""

from qe.ai.agents import fundamental, news, regime, risk, sentiment, technical
from qe.ai.agents.base import (
    AgentContext,
    AgentRun,
    AgentSpec,
    parse_output,
    render_prompt,
    run_agent,
    run_analyst,
)

ANALYSTS = {
    m.SPEC.agent_id: m.SPEC for m in (technical, regime, risk, fundamental, news, sentiment)
}

__all__ = [
    "ANALYSTS",
    "AgentContext",
    "AgentRun",
    "AgentSpec",
    "parse_output",
    "render_prompt",
    "run_agent",
    "run_analyst",
]
