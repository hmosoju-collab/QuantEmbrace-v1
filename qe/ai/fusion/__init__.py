"""Deterministic research fusion — AI_ADVISORY by default (AI weight 0)."""

from qe.ai.fusion.config import (
    STUDY_ONLY_MODES,
    WEIGHT_CEILING,
    FusionConfig,
    HardRiskConfig,
)
from qe.ai.fusion.engine import FusionRefused, FusionReport, FusionRow, fuse
from qe.ai.fusion.quant import QuantRow, hard_flags, quant_rows
from qe.ai.fusion.view import FusionRun, engine_record, render_fusion, run_fusion

__all__ = [
    "STUDY_ONLY_MODES",
    "WEIGHT_CEILING",
    "FusionConfig",
    "FusionRefused",
    "FusionReport",
    "FusionRow",
    "FusionRun",
    "HardRiskConfig",
    "QuantRow",
    "engine_record",
    "fuse",
    "hard_flags",
    "quant_rows",
    "render_fusion",
    "run_fusion",
]
