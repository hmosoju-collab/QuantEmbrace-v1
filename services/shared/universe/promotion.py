"""
Promotion gate evaluation.

Promotion gates are the criteria that must pass before an operator can promote
the active universe mode to the next stage.

Promotion path:
    PAPER_SAFE_START  →  PAPER_EXPAND  →  LIVE_ADVANCED

IMPORTANT: Promotion is ALWAYS a manual operator decision.
The gate evaluator REPORTS pass/fail — it never automatically changes the mode.

Usage:
    evaluator = PromotionGateEvaluator.from_yaml_config()
    result = evaluator.evaluate(
        gate="PAPER_SAFE_START_TO_EXPAND",
        metrics=collected_metrics_dict,
    )
    print(result.summary())

    # Operator then manually sets UNIVERSE_MODE=PAPER_EXPAND in the environment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml

from shared.universe.modes import UniverseMode

logger = logging.getLogger(__name__)

_DEFAULT_GATES_YAML = Path("configs/promotion_gates.yaml")


@dataclass
class GateCriterionResult:
    """Pass/fail result for a single gate criterion."""
    criterion: str
    required: Any
    actual: Optional[Any]
    passed: bool
    notes: str = ""


@dataclass
class PromotionGateResult:
    """Full evaluation result for a promotion gate."""
    gate_name: str
    from_mode: UniverseMode
    to_mode: UniverseMode
    evaluated_at: date
    overall_passed: bool
    criteria_results: list[GateCriterionResult] = field(default_factory=list)

    @property
    def failed_criteria(self) -> list[GateCriterionResult]:
        return [c for c in self.criteria_results if not c.passed]

    @property
    def passed_criteria(self) -> list[GateCriterionResult]:
        return [c for c in self.criteria_results if c.passed]

    def summary(self) -> str:
        lines = [
            f"Promotion Gate: {self.gate_name}",
            f"Route: {self.from_mode.value} → {self.to_mode.value}",
            f"Evaluated: {self.evaluated_at}",
            f"Overall: {'PASSED ✓' if self.overall_passed else 'FAILED ✗'}",
            f"Criteria: {len(self.passed_criteria)} passed / {len(self.failed_criteria)} failed",
        ]
        if self.failed_criteria:
            lines.append("\nFailed criteria:")
            for c in self.failed_criteria:
                lines.append(
                    f"  ✗ {c.criterion}: required={c.required}, actual={c.actual}"
                    + (f" — {c.notes}" if c.notes else "")
                )
        if self.passed_criteria:
            lines.append("\nPassed criteria:")
            for c in self.passed_criteria:
                lines.append(f"  ✓ {c.criterion}: {c.actual}")
        return "\n".join(lines)


class PromotionGateEvaluator:
    """
    Evaluates promotion gate criteria against provided metrics.

    Metrics are collected externally (from CloudWatch, DynamoDB, Prometheus, etc.)
    and passed in as a flat dict. The evaluator checks each criterion from the YAML.
    """

    _GATE_TO_MODES = {
        "PAPER_SAFE_START_TO_EXPAND": (UniverseMode.PAPER_SAFE_START, UniverseMode.PAPER_EXPAND),
        "PAPER_EXPAND_TO_LIVE": (UniverseMode.PAPER_EXPAND, UniverseMode.LIVE_ADVANCED),
    }

    def __init__(self, gates_config_path: str | Path | None = None) -> None:
        self._cfg_path = Path(gates_config_path or _DEFAULT_GATES_YAML)
        self._cfg: dict[str, Any] = {}

    def _load(self) -> None:
        if self._cfg:
            return
        if not self._cfg_path.exists():
            logger.warning("promotion_gate.config_not_found path=%s", self._cfg_path)
            return
        with open(self._cfg_path, "r", encoding="utf-8") as fh:
            self._cfg = yaml.safe_load(fh) or {}

    def evaluate(self, gate: str, metrics: dict[str, Any]) -> PromotionGateResult:
        """
        Evaluate a promotion gate against collected metrics.

        Args:
            gate: Gate name, e.g. "PAPER_SAFE_START_TO_EXPAND"
            metrics: Flat dict of metric_name → value collected from the system.
                     Keys match the dotted paths in promotion_gates.yaml.
                     Example: {
                       "data_quality.min_tick_data_pass_rate_pct": 97.5,
                       "signal_stability.max_signal_flip_rate_pct": 12.0,
                       ...
                     }

        Returns:
            PromotionGateResult with pass/fail per criterion and overall result.
        """
        self._load()

        from_mode, to_mode = self._GATE_TO_MODES.get(gate, (None, None))
        if from_mode is None:
            raise ValueError(
                f"Unknown promotion gate '{gate}'. "
                f"Valid gates: {list(self._GATE_TO_MODES)}"
            )

        gate_cfg = self._cfg.get(gate, {})
        if not gate_cfg:
            logger.warning("promotion_gate.config_empty gate=%s — cannot evaluate", gate)
            return PromotionGateResult(
                gate_name=gate,
                from_mode=from_mode,
                to_mode=to_mode,
                evaluated_at=date.today(),
                overall_passed=False,
                criteria_results=[],
            )

        criteria_results: list[GateCriterionResult] = []

        for section_name, section_cfg in gate_cfg.items():
            if not isinstance(section_cfg, dict):
                continue
            for criterion, required_value in section_cfg.items():
                if criterion.startswith("_") or criterion == "description":
                    continue
                full_key = f"{section_name}.{criterion}"
                actual = metrics.get(full_key)

                passed, notes = self._check_criterion(criterion, required_value, actual)
                criteria_results.append(GateCriterionResult(
                    criterion=full_key,
                    required=required_value,
                    actual=actual,
                    passed=passed,
                    notes=notes,
                ))

        overall = all(c.passed for c in criteria_results)

        result = PromotionGateResult(
            gate_name=gate,
            from_mode=from_mode,
            to_mode=to_mode,
            evaluated_at=date.today(),
            overall_passed=overall,
            criteria_results=criteria_results,
        )

        logger.info(
            "promotion_gate.evaluated gate=%s overall=%s passed=%d failed=%d",
            gate, "PASS" if overall else "FAIL",
            len(result.passed_criteria), len(result.failed_criteria),
        )

        return result

    def _check_criterion(
        self, criterion: str, required: Any, actual: Optional[Any]
    ) -> tuple[bool, str]:
        """Return (passed, notes) for a single criterion."""

        if actual is None:
            return False, "Metric not provided — cannot evaluate"

        # Boolean criteria (must be exactly True)
        if isinstance(required, bool):
            if not isinstance(actual, bool):
                return False, f"Expected bool, got {type(actual).__name__}"
            if required is True and actual is not True:
                return False, "Required to be True"
            return True, ""

        # Numeric criteria — determine direction from criterion name
        if isinstance(required, (int, float)):
            try:
                actual_num = float(actual)
                required_num = float(required)
            except (TypeError, ValueError):
                return False, f"Cannot compare {actual!r} to {required!r}"

            # "max_*" criteria: actual must be ≤ required
            if criterion.startswith("max_"):
                if actual_num <= required_num:
                    return True, f"{actual_num:.2f} ≤ {required_num:.2f}"
                return False, f"{actual_num:.2f} exceeds max {required_num:.2f}"

            # "min_*" criteria: actual must be ≥ required
            if criterion.startswith("min_"):
                if actual_num >= required_num:
                    return True, f"{actual_num:.2f} ≥ {required_num:.2f}"
                return False, f"{actual_num:.2f} below min {required_num:.2f}"

            # Exact numeric match for other numeric criteria
            return actual_num == required_num, f"{actual_num} vs {required_num}"

        return True, "No comparison rule"

    @classmethod
    def from_yaml_config(
        cls, gates_config_path: str | Path | None = None
    ) -> "PromotionGateEvaluator":
        return cls(gates_config_path=gates_config_path)
