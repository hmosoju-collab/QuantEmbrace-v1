"""Fusion configuration (configs/research_fusion.yaml) — own frozen hash.

Only ONE fusion weight is a free parameter: ``ai_weight`` (the quant weight is
1 - ai_weight). This is a deliberate departure from a four-weight
quant/regime/AI/risk blend (docs/operations/ai-configuration.md §3):

  * regime is a market-level term, identical for every symbol, so it cannot
    change a cross-sectional ranking — it is reported, not blended;
  * risk is a hard veto, never a weight — a hard limit that can be traded off
    against a score is not a hard limit.

``ai_weight`` is capped IN CODE per mode, and the two modes that allow a
non-zero weight run only in the ``study`` context (never shadow/paper).
"""

from typing import Literal

from pydantic import Field, model_validator

from qe.ai.config import config_hash, load_yaml
from qe.config import FrozenModel

FusionMode = Literal["AI_DISABLED", "AI_ADVISORY", "AI_WEIGHTED", "AI_EXPERIMENTAL"]
FusionContext = Literal["study", "shadow"]

WEIGHT_CEILING: dict[str, float] = {
    "AI_DISABLED": 0.0,
    "AI_ADVISORY": 0.0,
    "AI_WEIGHTED": 0.20,
    "AI_EXPERIMENTAL": 0.50,
}
STUDY_ONLY_MODES = frozenset({"AI_WEIGHTED", "AI_EXPERIMENTAL"})


class HardRiskConfig(FrozenModel):
    """Deterministic vetoes. A flagged symbol is REJECT whatever the AI says."""

    max_data_age_days: int = Field(5, ge=0)
    max_vol_63d: float | None = Field(None, gt=0)  # annualized; None = off
    max_drawdown_252d: float | None = Field(None, lt=0)  # e.g. -0.5; None = off


class FusionConfig(FrozenModel):
    schema_version: Literal["qe_ai_fusion/1"] = "qe_ai_fusion/1"
    mode: FusionMode = "AI_ADVISORY"
    ai_weight: float = Field(0.0, ge=0.0, le=1.0)
    allowed_contexts: tuple[FusionContext, ...] = ("study", "shadow")
    recommendation_dead_zone: float = Field(0.20, ge=0.0, lt=1.0)
    hard_risk: HardRiskConfig = HardRiskConfig()

    @model_validator(mode="after")
    def _ceiling(self) -> "FusionConfig":
        cap = WEIGHT_CEILING[self.mode]
        if self.ai_weight > cap:
            raise ValueError(f"ai_weight {self.ai_weight} exceeds the {self.mode} ceiling {cap}")
        return self

    def config_hash(self) -> str:
        return config_hash(self)

    @classmethod
    def from_yaml(cls, path) -> "FusionConfig":
        return cls.model_validate(load_yaml(path))
