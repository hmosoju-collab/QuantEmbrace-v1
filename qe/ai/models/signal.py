"""ResearchSignal v1 — the only research record downstream code may consume.

Versioned (``schema_version``) and self-validating. Three properties are
*computed, never claimed*: a signal whose stored value disagrees with the
recomputation is invalid.

  * ``ai_score``            — mean of the OK directional components
  * ``contamination_risk``  — decision date vs the models' knowledge cutoffs
  * point-in-time validity  — every attached evidence knowledge_ts <= cutoff
"""

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, model_validator

from qe.ai.config import ResearchMode
from qe.ai.models.common import ComponentStatus, Point, ShortText, Signed, Unit
from qe.ai.models.evidence import Evidence
from qe.config import FrozenModel

SCHEMA_VERSION = "research_signal/1"

# Components that express a directional (bearish..bullish) view and feed ai_score.
DIRECTIONAL = ("technical", "fundamental", "news", "sentiment")
COMPONENTS = (*DIRECTIONAL, "risk", "regime", "debate", "synthesis")


class LookaheadViolation(ValueError):
    pass


def is_contaminated(
    decision_date: date, knowledge_cutoffs: Mapping[str, date | None], guard_days: int
) -> bool:
    """True when any model used may have seen what happened after decision_date
    (docs/research/lookahead-prevention.md §2). Unknown cutoff ⇒ contaminated."""
    if not knowledge_cutoffs:
        return True
    guard = timedelta(days=guard_days)
    return any(c is None or decision_date <= c + guard for c in knowledge_cutoffs.values())


def ai_score_of(
    scores: Mapping[str, float | None], status: Mapping[str, ComponentStatus]
) -> float | None:
    vals = [
        scores[c]
        for c in DIRECTIONAL
        if status.get(c) is ComponentStatus.OK and scores.get(c) is not None
    ]
    return sum(vals) / len(vals) if vals else None


class ResearchSignal(FrozenModel):
    schema_version: Literal["research_signal/1"] = SCHEMA_VERSION
    research_id: str
    trace_id: str
    symbol: str
    market: str
    timestamp: datetime  # decision instant (EOD research: == information_cutoff)
    information_cutoff: datetime
    timeframe: Literal["1d"] = "1d"
    research_mode: ResearchMode

    market_regime: Literal["RISK_ON", "RISK_OFF", "UNKNOWN"]
    regime_confidence: Unit | None = None

    technical_score: Signed | None = None
    fundamental_score: Signed | None = None
    news_score: Signed | None = None
    sentiment_score: Signed | None = None
    risk_score: Unit | None = None
    component_status: dict[str, ComponentStatus]

    ai_score: Signed | None = None
    ai_confidence: Unit | None = None

    bull_case: ShortText = ""
    bear_case: ShortText = ""
    consensus: ShortText = ""
    supporting_evidence: tuple[Evidence, ...] = ()
    contradicting_evidence: tuple[Evidence, ...] = ()
    risks: tuple[Point, ...] = Field(default=(), max_length=6)
    data_sources: tuple[str, ...] = ()

    model_version: str
    prompt_version: str
    knowledge_cutoffs: dict[str, date | None]
    guard_days: Annotated[int, Field(ge=0)]
    contamination_risk: bool

    @model_validator(mode="after")
    def _consistent(self) -> "ResearchSignal":
        for name in ("timestamp", "information_cutoff"):
            if getattr(self, name).tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.information_cutoff > self.timestamp:
            raise ValueError("information_cutoff is after the decision timestamp")

        missing = set(COMPONENTS) - set(self.component_status)
        if missing:
            raise ValueError(f"component_status missing: {sorted(missing)}")

        for ev in (*self.supporting_evidence, *self.contradicting_evidence):
            if ev.knowledge_ts > self.information_cutoff:
                raise LookaheadViolation(
                    f"evidence {ev.evidence_id} knowledge_ts {ev.knowledge_ts.isoformat()} is "
                    f"after information_cutoff {self.information_cutoff.isoformat()}"
                )

        scores = {c: getattr(self, f"{c}_score") for c in DIRECTIONAL}
        for c in DIRECTIONAL:
            ok = self.component_status[c] is ComponentStatus.OK
            if ok != (scores[c] is not None):
                raise ValueError(f"{c}: status {self.component_status[c]} vs score {scores[c]}")
        if (self.component_status["risk"] is ComponentStatus.OK) != (self.risk_score is not None):
            raise ValueError("risk: status/score mismatch")

        expected = ai_score_of(scores, self.component_status)
        if (expected is None) != (self.ai_score is None) or (
            expected is not None and abs(expected - self.ai_score) > 1e-12
        ):
            raise ValueError(f"ai_score {self.ai_score} != computed {expected}")

        contaminated = is_contaminated(
            self.information_cutoff.date(), self.knowledge_cutoffs, self.guard_days
        )
        if contaminated != self.contamination_risk:
            raise ValueError(
                f"contamination_risk={self.contamination_risk} but computed {contaminated}"
            )
        return self
