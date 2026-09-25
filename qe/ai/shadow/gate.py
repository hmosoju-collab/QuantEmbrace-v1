"""Forward AI shadow gate: config + pure evaluation math.

The metric mirrors the Forward Factor Gate on a monthly series, but the
series is the AI score's *incremental* information coefficient: each month
the researched names' ai_score is residualized on the factor rank q (OLS) and
the Spearman correlation of that residual with the forward return is taken.
"""

from dataclasses import dataclass
from datetime import date
import math
from typing import Literal

import numpy as np
from pydantic import Field, model_validator

from qe.ai.config import config_hash, load_yaml
from qe.config import FrozenModel


class ShadowThresholds(FrozenModel):
    min_months: int = Field(12, ge=1)
    min_mean_ic: float = 0.0
    min_ic_ir: float = 0.50
    min_positive_frac: float = Field(0.58, ge=0, le=1)
    max_single_month_share: float = Field(0.50, gt=0, le=1)


class ShadowGateConfig(FrozenModel):
    schema_version: Literal["qe_ai_shadow_gate/1"] = "qe_ai_shadow_gate/1"
    status: Literal["DRAFT", "SIGNED_OFF"] = "DRAFT"
    signed_off_by: str | None = None
    signed_off_on: date | None = None
    research_config: str
    research_config_hash: str | None = None
    model_id: str | None = None
    horizon_days: int = Field(21, ge=1)
    min_names_per_month: int = Field(8, ge=3)
    thresholds: ShadowThresholds = ShadowThresholds()

    @model_validator(mode="after")
    def _binding(self) -> "ShadowGateConfig":
        if self.status == "SIGNED_OFF":
            missing = [
                f
                for f in ("signed_off_by", "signed_off_on", "research_config_hash", "model_id")
                if not getattr(self, f)
            ]
            if missing:
                raise ValueError(f"SIGNED_OFF gate is missing its binding: {missing}")
        return self

    def config_hash(self) -> str:
        return config_hash(self)

    @classmethod
    def from_yaml(cls, path) -> "ShadowGateConfig":
        return cls.model_validate(load_yaml(path))


def _rank(x: np.ndarray) -> np.ndarray:
    order = x.argsort(kind="mergesort")
    ranks = np.empty(len(x))
    ranks[order] = np.arange(len(x))
    # average ties
    for v in np.unique(x):
        idx = np.where(x == v)[0]
        if len(idx) > 1:
            ranks[idx] = ranks[idx].mean()
    return ranks


def incremental_ic(ai: np.ndarray, q: np.ndarray, fwd: np.ndarray) -> float | None:
    """Spearman IC of (ai_score residualized on q) vs forward return.

    An AI score that is constant, or an exact linear function of the factor
    rank, carries NO incremental information: that month scores 0.0 (it is
    counted, not dropped — dropping it would inflate the IC ratio). Only an
    unmeasurable month (too few names, constant outcome) returns None.
    """
    if len(ai) < 3 or np.ptp(fwd) == 0:
        return None
    if np.ptp(ai) == 0:
        return 0.0
    if np.ptp(q) > 0:
        slope, intercept = np.polyfit(q, ai, 1)
        resid = ai - (slope * q + intercept)
    else:
        resid = ai - ai.mean()
    if np.std(resid) <= 1e-9 * np.std(ai):  # numerically nothing left beyond q
        return 0.0
    c = np.corrcoef(_rank(resid), _rank(fwd))[0, 1]
    return None if math.isnan(c) else float(c)


@dataclass(frozen=True)
class GateResult:
    verdict: str  # NOT_EVALUABLE | IN_PROGRESS | PASS | FAIL
    months: int
    mean_ic: float | None
    ic_ir: float | None
    positive_frac: float | None
    max_single_share: float | None
    checks: dict[str, bool]
    reason: str


def evaluate(monthly_ic: list[float], gate: ShadowGateConfig) -> GateResult:
    """Fail-closed: a DRAFT gate never yields a verdict; too few months is
    IN_PROGRESS, never a pass."""
    t = gate.thresholds
    n = len(monthly_ic)
    if gate.status != "SIGNED_OFF":
        return GateResult(
            "NOT_EVALUABLE", n, None, None, None, None, {}, "gate is a DRAFT — not signed off"
        )
    if n == 0:
        return GateResult("IN_PROGRESS", 0, None, None, None, None, {}, "no scored months yet")
    arr = np.array(monthly_ic, dtype=float)
    mean = float(arr.mean())
    sd = float(arr.std(ddof=1)) if n > 1 else float("nan")
    ir = mean / sd * math.sqrt(12) if n > 1 and sd > 0 else None
    pos = float((arr > 0).mean())
    total = float(arr.sum())
    share = float(arr.max() / total) if total > 0 else 1.0
    checks = {
        f"months>={t.min_months}": n >= t.min_months,
        f"mean_ic>{t.min_mean_ic}": mean > t.min_mean_ic,
        f"ic_ir>={t.min_ic_ir}": ir is not None and ir >= t.min_ic_ir,
        f"positive_months>={t.min_positive_frac:.0%}": pos >= t.min_positive_frac,
        f"single_month_share<={t.max_single_month_share:.0%}": share <= t.max_single_month_share,
    }
    if n < t.min_months:
        verdict, reason = "IN_PROGRESS", f"{n}/{t.min_months} months"
    elif all(checks.values()):
        verdict, reason = (
            "PASS",
            "eligible for HUMAN REVIEW only — any AI weight change needs a new ADR",
        )
    else:
        failed = [k for k, v in checks.items() if not v]
        verdict, reason = "FAIL", f"failed: {', '.join(failed)}"
    return GateResult(verdict, n, mean, ir, pos, share, checks, reason)
