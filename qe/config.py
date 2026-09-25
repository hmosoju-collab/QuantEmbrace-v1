"""Typed, frozen, content-hashed run configuration.

One config tree per session/run. It is frozen at construction and identified by
the SHA-256 of its canonical JSON form. The hash is stamped into every journal
record header and every report, which makes session validity a mechanical check
(journal hash == approved hash) instead of a runtime-validation script.
"""

from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
import yaml


class FrozenModel(BaseModel):
    """Base for all qe config models: immutable, no silent extra keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class UniverseConfig(FrozenModel):
    market: str = "NSE"
    segment: str = "EQ"
    # None means "the whole segment" (cross-sectional strategies select their
    # own universe at each rebalance, e.g. top-N by liquidity).
    symbols: tuple[str, ...] | None = None

    @field_validator("symbols")
    @classmethod
    def _non_empty(cls, v: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if v is not None:
            if not v:
                raise ValueError("universe.symbols must not be empty (use null for whole-segment)")
            if len(set(v)) != len(v):
                raise ValueError("universe.symbols contains duplicates")
        return v


class StrategyConfig(FrozenModel):
    """Strategy parameters — ``factor_book`` (ADR-034/035, NSE cross-sectional)
    or ``risk_parity_lite`` (ADR-041 P4, the US RPLITE static book). Fields
    below the ``kind`` line are grouped by which kind uses them; unused fields
    for a given kind simply take their default."""

    kind: Literal["factor_book", "risk_parity_lite"] = "factor_book"
    # factor_book fields
    factor: Literal["delivery", "momentum"] = "delivery"
    top_n: int = 200
    k: int = 20
    max_weight: float = 0.08
    cash_buffer: float = 0.02
    rebalance: Literal["monthly"] = "monthly"
    # risk_parity_lite fields
    assets: tuple[str, ...] = ("SPY", "TLT", "GLD")
    vol_lookback: int = 63


class RiskConfig(FrozenModel):
    """Optional pre-trade limits beyond the always-on checks (None = off)."""

    max_positions: int | None = None
    max_turnover_frac: float | None = None  # traded notional / NAV per rebalance
    # Paper/live auto-halt triggers (None = off; backtest never uses these).
    max_drawdown_frac: float | None = None  # trip kill switch if NAV drops this far from peak
    max_data_age_days: float | None = None  # trip kill switch if data older than this


class GateSpec(FrozenModel):
    """One pre-registered gate: metric OP value."""

    name: str
    metric: str
    op: Literal[">=", "<=", ">", "<"]
    value: float


class ExperimentConfig(FrozenModel):
    """Registers the study in the experiment tracker (multiple-testing budget)."""

    name: str
    family: str
    hypothesis: str
    gates: tuple[GateSpec, ...] = ()


class WalkForwardConfig(FrozenModel):
    """Walk-forward study options."""

    warmup_rows: int = 252  # first rebalance at/after this row, matching the v1 study
    overlay_sma: int = 200
    v1_cross_check: bool = True  # also run the v1 returns-space model verbatim


class DataConfig(FrozenModel):
    lake_root: str = "backtest-data/lake"
    interval: str = "1d"
    # Pin an existing data snapshot; None means "create one at run start".
    snapshot_id: str | None = None


class RunConfig(FrozenModel):
    """Top-level config for one engine run (backtest/null now; paper/live later)."""

    name: str
    mode: Literal["null", "sim", "paper"] = "null"
    start_date: date
    end_date: date
    universe: UniverseConfig
    data: DataConfig = DataConfig()
    journal_dir: str = "journals"
    seed_nav: float = 1_000_000.0
    strategy: StrategyConfig | None = None
    risk: RiskConfig = RiskConfig()
    study_kind: Literal["forward_book", "walk_forward"] = "forward_book"
    experiment: ExperimentConfig | None = None
    walk_forward: WalkForwardConfig | None = None

    @model_validator(mode="after")
    def _date_order(self) -> "RunConfig":
        if self.end_date < self.start_date:
            raise ValueError(f"end_date {self.end_date} before start_date {self.start_date}")
        if self.mode in ("sim", "paper") and self.strategy is None:
            raise ValueError(f"mode={self.mode} requires a strategy section")
        if self.mode == "null" and self.universe.symbols is None:
            raise ValueError("mode=null requires explicit universe.symbols")
        if self.study_kind == "walk_forward" and self.mode != "sim":
            raise ValueError("study_kind=walk_forward requires mode=sim")
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def config_hash(self) -> str:
        """Full SHA-256 hex digest of the canonical JSON form."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def short_hash(self) -> str:
        return self.config_hash()[:12]

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RunConfig":
        raw = yaml.safe_load(Path(path).read_text())
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: expected a mapping at top level")
        return cls.model_validate(raw)
