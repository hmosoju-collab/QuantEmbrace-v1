"""Post-trade analyst: narrates one completed trade. It classifies nothing —
thesis, entry/exit, regime and execution quality are computed by code
(qe/ai/post_trade/review.py); the agent writes the lesson and flags anything
unexpected, citing the trade evidence."""

from qe.ai.agents.base import AgentSpec

SPEC = AgentSpec(
    agent_id="post_trade",
    prompt_version="post_trade/1",
    schema_id="post_trade/1",
    role="Post-trade analyst",
    task=(
        "Review this completed trade using only the evidence, including the code-computed "
        "classifications. Note anything unexpected, and state one concrete research lesson "
        "(about the signal, timing, regime, or costs) for human review."
    ),
)
