"""Synthesizer: bull case, bear case, consensus, and how strongly the evidence
supports any directional view. It produces text + confidence only — ai_score is
computed from the analysts by code, so the synthesizer cannot override them."""

from qe.ai.agents.base import AgentSpec

SPEC = AgentSpec(
    agent_id="synthesizer",
    prompt_version="synthesizer/1",
    schema_id="synthesis/1",
    role="Research synthesizer",
    task=(
        "Summarize the bull case, the bear case, and a balanced consensus, taking the critique "
        "into account. Set ai_confidence to how strongly the evidence supports any directional "
        "view (0 = none). Classify cited evidence as supporting or contradicting the consensus."
    ),
)
