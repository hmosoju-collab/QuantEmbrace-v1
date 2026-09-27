"""Evidence: one tool-computed, point-in-time fact.

Evidence is created by code (``qe.ai.tools``), never by an LLM. Agents may only
cite evidence by ``evidence_id``; the orchestrator attaches the real record, so
a model cannot invent a source or a timestamp (security-model T16).
"""

from datetime import datetime
import hashlib
import json
from typing import Annotated

from pydantic import Field, field_validator

from qe.config import FrozenModel

EvidenceId = Annotated[str, Field(pattern=r"^[a-z]+\.[a-z0-9_]+$", max_length=64)]


class Evidence(FrozenModel):
    evidence_id: EvidenceId  # "<tool>.<name>", unique within one symbol's research
    tool: str
    symbol: str | None  # None = market-level (e.g. regime)
    knowledge_ts: datetime  # when this fact became knowable (bar date @ market close)
    value: float | int | bool | str | None
    summary: Annotated[str, Field(max_length=240)]

    @field_validator("knowledge_ts")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("knowledge_ts must be timezone-aware")
        return v

    @property
    def content_hash(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
