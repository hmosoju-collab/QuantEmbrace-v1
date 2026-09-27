"""Shared prompt text and output schemas for qe.ai agents.

Changing any text here changes every agent's ``prompt_hash``; the pinned
hashes in ``tests/qe/ai/test_ai_agents.py`` then fail until the affected
``prompt_version`` is bumped — prompts are versioned artifacts, not strings.
"""

from typing import Annotated, Literal

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


# Data kinds a hypothesis may require. Code (not the LLM) decides testability
# against qe.ai.hypotheses.AVAILABLE_DATA — what the lake actually holds.
DataKind = Literal[
    "nse_eod_prices",
    "nse_delivery_pct",
    "nse_turnover",
    "india_vix",
    "nifty_futures_eod",
    "nifty_options_eod",
    "us_eod_prices",
    "fundamentals",
    "news",
    "sentiment",
    "intraday_option_chains",
    "alternative_data",
]
Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{2,48}$")]
# The metrics qe.research walk-forward studies compute (qe/research/metrics.py);
# a gate on anything else would FAIL closed, so it is rejected up front.
GateMetric = Literal[
    "cagr", "vol", "sharpe", "maxdd", "hit", "months", "n_years",
    "positive_years", "per_year_positive_frac", "worst_year_return",
]  # fmt: skip


class GateItem(FrozenModel):
    name: Slug
    metric: GateMetric
    op: Literal[">=", "<=", ">", "<"]
    value: float


class HypothesisItem(FrozenModel):
    name: Slug
    family: Slug
    hypothesis: Text
    rationale: Text
    required_data: Annotated[tuple[DataKind, ...], Field(min_length=1, max_length=6)]
    proposed_study_kind: Literal["walk_forward", "forward_book"]
    # An ungated study proves nothing (F-11): at least one pre-registered gate.
    proposed_gates: Annotated[tuple[GateItem, ...], Field(min_length=1, max_length=6)]


class HypothesisOutput(FrozenModel):
    hypotheses: Annotated[tuple[HypothesisItem, ...], Field(min_length=1, max_length=3)]
    evidence_ids: Ids = ()


class PostTradeOutput(FrozenModel):
    """Narrative only: every classification of a trade is computed by code."""

    unexpected_event: Annotated[str, Field(max_length=240)] = ""
    lesson: Annotated[str, Field(max_length=400)]
    confidence: Unit
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
    "hypothesis/1": HypothesisOutput,
    "post_trade/1": PostTradeOutput,
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
    "post_trade/1": (
        '{"unexpected_event": string (max 240, "" if none), "lesson": string (max 400, one '
        'actionable research lesson), "confidence": number in [0, 1], '
        '"evidence_ids": up to 8 ids from ALLOWED_EVIDENCE_IDS}'
    ),
    "hypothesis/1": (
        '{"hypotheses": 1-3 objects {"name": slug, "family": slug, "hypothesis": string '
        '(testable, max 600), "rationale": string (max 600), "required_data": 1-6 of '
        "[nse_eod_prices, nse_delivery_pct, nse_turnover, india_vix, nifty_futures_eod, "
        "nifty_options_eod, us_eod_prices, fundamentals, news, sentiment, "
        'intraday_option_chains, alternative_data], "proposed_study_kind": walk_forward|'
        'forward_book, "proposed_gates": 1-6 objects {"name": slug, "metric": one of [cagr, '
        "vol, sharpe, maxdd, hit, months, n_years, positive_years, per_year_positive_frac, "
        'worst_year_return], "op": >=|<=|>|<, "value": number}}, '
        '"evidence_ids": up to 8 ids from ALLOWED_EVIDENCE_IDS}'
    ),
}
