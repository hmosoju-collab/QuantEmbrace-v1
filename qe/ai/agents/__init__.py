"""qe.ai agents — specs (role, task, versioned prompt, output schema) run by
``qe.ai.agents.base.run_agent``. Agents have no tools and no side effects."""

from qe.ai.agents import (
    bear,
    bull,
    critic,
    fundamental,
    news,
    regime,
    risk,
    sentiment,
    synthesizer,
    technical,
)
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

DEBATERS = {m.SPEC.agent_id: m.SPEC for m in (bull, bear, critic, synthesizer)}

__all__ = [
    "ANALYSTS",
    "DEBATERS",
    "AgentContext",
    "AgentRun",
    "AgentSpec",
    "parse_output",
    "render_prompt",
    "run_agent",
    "run_analyst",
]
