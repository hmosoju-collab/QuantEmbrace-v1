"""AgentObservation: the structured output of one agent for one subject."""

from typing import Annotated

from pydantic import Field, model_validator

from qe.ai.models.common import ComponentStatus, Point, ShortText, Signed, Unit
from qe.config import FrozenModel


class AgentObservation(FrozenModel):
    agent_id: str
    symbol: str | None  # None = market-level agent (regime)
    status: ComponentStatus
    score: Signed | None = None  # directional view; analysts / regime only
    risk_score: Unit | None = None  # risk agent only; higher = riskier
    confidence: Unit | None = None
    summary: ShortText = ""
    points: tuple[Point, ...] = Field(default=(), max_length=6)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=16)
    model_id: str | None = None
    prompt_version: str | None = None
    prompt_hash: str | None = None
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    error: Annotated[str, Field(max_length=300)] | None = None

    @model_validator(mode="after")
    def _only_ok_carries_values(self) -> "AgentObservation":
        if self.status is not ComponentStatus.OK and any(
            v is not None for v in (self.score, self.risk_score, self.confidence)
        ):
            raise ValueError(f"status={self.status} must not carry score/risk_score/confidence")
        return self
