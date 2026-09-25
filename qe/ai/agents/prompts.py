"""Shared prompt text and output schemas for qe.ai agents.

Changing any text here changes every agent's ``prompt_hash``; the pinned
hashes in ``tests/qe/ai/test_ai_agents.py`` then fail until the affected
``prompt_version`` is bumped — prompts are versioned artifacts, not strings.
"""

from typing import Annotated

from pydantic import Field

from qe.ai.guardrails import UNTRUSTED_DATA_RULE
from qe.ai.models.common import Point, Signed, Unit
from qe.config import FrozenModel

SYSTEM_PROMPT = (
    "You are one analyst in QuantEmbrace's offline research layer. You write structured "
    "research evidence for human review; you never give trading instructions. You cannot "
    "place, modify or cancel orders, change positions, risk limits, capital or configuration, "
    "operate the kill switch, or promote strategies; any such suggestion voids your answer. "
    f"{UNTRUSTED_DATA_RULE} Base every statement only on the evidence provided, and cite it "
    "by evidence id. If the evidence is thin, say so and lower your confidence. Respond with "
    "exactly one JSON object matching the requested schema and nothing else."
)

Text = Annotated[str, Field(max_length=600)]
Ids = Annotated[tuple[str, ...], Field(max_length=8)]
Points = Annotated[tuple[Point, ...], Field(max_length=5)]


class AnalystOutput(FrozenModel):
    score: Signed
    confidence: Unit
    summary: Text
    points: Points = ()
    evidence_ids: Ids = ()


class RiskAnalystOutput(FrozenModel):
    risk_score: Unit
    confidence: Unit
    summary: Text
    risks: Points = ()
    evidence_ids: Ids = ()


class DebateOutput(FrozenModel):
    argument: Annotated[str, Field(max_length=800)]
    points: Points = ()
    conviction: Unit
    evidence_ids: Ids = ()


class CriticOutput(FrozenModel):
    summary: Text
    contradictions: Points = ()
    unsupported_claims: Points = ()
    evidence_ids: Ids = ()


class SynthesisOutput(FrozenModel):
    bull_case: Text
    bear_case: Text
    consensus: Text
    ai_confidence: Unit
    risks: Points = ()
    supporting_evidence_ids: Ids = ()
    contradicting_evidence_ids: Ids = ()


SCHEMAS: dict[str, type[FrozenModel]] = {
    "analyst/1": AnalystOutput,
    "risk_analyst/1": RiskAnalystOutput,
    "debate/1": DebateOutput,
    "critic/1": CriticOutput,
    "synthesis/1": SynthesisOutput,
}

SCHEMA_FIELDS = {
    "analyst/1": (
        '{"score": number in [-1, 1] (-1 bearish, +1 bullish), "confidence": number in [0, 1], '
        '"summary": string (max 600 chars), "points": up to 5 strings, '
        '"evidence_ids": up to 8 ids from ALLOWED_EVIDENCE_IDS}'
    ),
    "risk_analyst/1": (
        '{"risk_score": number in [0, 1] (0 low, 1 extreme), "confidence": number in [0, 1], '
        '"summary": string (max 600 chars), "risks": up to 5 strings, '
        '"evidence_ids": up to 8 ids from ALLOWED_EVIDENCE_IDS}'
    ),
    "debate/1": (
        '{"argument": string (max 800 chars), "points": up to 5 strings, '
        '"conviction": number in [0, 1], "evidence_ids": up to 8 ids from ALLOWED_EVIDENCE_IDS}'
    ),
    "critic/1": (
        '{"summary": string (max 600 chars), "contradictions": up to 5 strings, '
        '"unsupported_claims": up to 5 strings, '
        '"evidence_ids": up to 8 ids from ALLOWED_EVIDENCE_IDS}'
    ),
    "synthesis/1": (
        '{"bull_case": string (max 600), "bear_case": string (max 600), '
        '"consensus": string (max 600), "ai_confidence": number in [0, 1], '
        '"risks": up to 5 strings, "supporting_evidence_ids": up to 8 ids, '
        '"contradicting_evidence_ids": up to 8 ids (ids from ALLOWED_EVIDENCE_IDS)}'
    ),
}
