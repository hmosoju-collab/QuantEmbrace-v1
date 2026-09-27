"""Risk analyst: narrative only. Hard risk decisions are deterministic (fusion
hard flags, qe.risk) and never read this output."""

from qe.ai.agents.base import AgentSpec

SPEC = AgentSpec(
    agent_id="risk",
    prompt_version="risk/1",
    schema_id="risk_analyst/1",
    role="Risk analyst",
    task=(
        "Assess downside, drawdown, volatility and liquidity risk from the evidence. "
        "risk_score: 0 = low risk, 1 = extreme risk. List the concrete risks."
    ),
)
