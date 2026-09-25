"""qe.ai run configuration — its own frozen, hashed models (ADR-043 §6).

Deliberately NOT part of ``qe.config.RunConfig``: any new field there moves
every engine config hash and strands the paper books (ADR-042). These models
hash with ``exclude_none`` so a new optional field (default ``None``) never
moves an existing research config hash; any other schema change must bump
``schema_version``.
"""

from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
import yaml

from qe.config import FrozenModel

ResearchMode = Literal["FAST", "STANDARD", "DEEP"]


def canonical_json(model: BaseModel) -> str:
    return json.dumps(
        model.model_dump(mode="json", exclude_none=True), sort_keys=True, separators=(",", ":")
    )


def config_hash(model: BaseModel) -> str:
    return hashlib.sha256(canonical_json(model).encode("utf-8")).hexdigest()


def load_yaml(path: str | Path) -> dict:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at top level")
    return raw


class ModelProfile(FrozenModel):
    """One LLM. ``knowledge_cutoff`` drives the contamination rule
    (docs/research/lookahead-prevention.md §2); ``None`` means unknown, which
    marks every signal from this model as contaminated."""

    model_id: str
    tier: Literal["quick", "deep"]
    knowledge_cutoff: date | None = None


class BudgetConfig(FrozenModel):
    max_run_tokens: int = Field(200_000, gt=0)
    max_tokens_per_call: int = Field(1024, gt=0, le=8192)
    timeout_s: float = Field(30.0, gt=0, le=300)
    max_retries: int = Field(1, ge=0, le=3)  # retries per agent on timeout/malformed
    breaker_threshold: int = Field(3, ge=1)  # consecutive LLM failures that open the breaker


class ResearchRunConfig(FrozenModel):
    schema_version: Literal["qe_ai_research/1"] = "qe_ai_research/1"
    name: str
    # A qe RunConfig YAML, read (never written) for factor params, market, and
    # lake location — one source of truth with the engine book being annotated.
    book_config: str
    research_mode: ResearchMode = "FAST"
    backend: Literal["fake", "bedrock"] = "fake"
    quick_model: ModelProfile
    deep_model: ModelProfile
    budget: BudgetConfig = BudgetConfig()
    guard_days: int = Field(90, ge=0)
    # Extra symbols to research beyond the engine's own basket at as_of.
    symbols: tuple[str, ...] | None = None
    max_symbols: int = Field(20, ge=1, le=100)
    temperature: float = Field(0.0, ge=0.0, le=1.0)
    region: str = "ap-south-1"
    seed: int = 0
    use_cache: bool = True

    @model_validator(mode="after")
    def _tiers(self) -> "ResearchRunConfig":
        if self.quick_model.tier != "quick" or self.deep_model.tier != "deep":
            raise ValueError("quick_model must be tier=quick and deep_model tier=deep")
        return self

    def config_hash(self) -> str:
        return config_hash(self)

    @property
    def short_hash(self) -> str:
        return self.config_hash()[:12]

    def knowledge_cutoffs(self) -> dict[str, date | None]:
        return {
            self.quick_model.model_id: self.quick_model.knowledge_cutoff,
            self.deep_model.model_id: self.deep_model.knowledge_cutoff,
        }

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ResearchRunConfig":
        return cls.model_validate(load_yaml(path))
