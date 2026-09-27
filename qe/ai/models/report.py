"""ResearchReport: one research run's signals plus its provenance."""

from datetime import date, datetime
from typing import Literal

from pydantic import Field

from qe.ai.config import ResearchMode
from qe.ai.models.common import Unit
from qe.ai.models.signal import ResearchSignal
from qe.config import FrozenModel


class RegimeSummary(FrozenModel):
    label: Literal["RISK_ON", "RISK_OFF", "UNKNOWN"]
    value: float | None = None  # R in [-1, 1]; None when unavailable
    confidence: Unit | None = None


class ResearchReport(FrozenModel):
    schema_version: Literal["research_report/1"] = "research_report/1"
    research_id: str
    as_of: date
    information_cutoff: datetime
    market: str
    research_mode: ResearchMode
    config_hash: str
    data_snapshot_id: str
    code_sha: str
    regime: RegimeSummary
    signals: tuple[ResearchSignal, ...]
    failed_symbols: tuple[str, ...] = ()
    llm_calls: int = 0
    llm_failures: int = 0
    llm_errors: dict[str, int] = Field(default_factory=dict)
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
