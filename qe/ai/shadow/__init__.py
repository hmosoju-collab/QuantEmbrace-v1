"""Forward AI shadow gate (ADR-043 P6) — the only route to AI weight > 0.

Pre-registered in configs/qe_ai_shadow_gate.yaml (DRAFT until a human signs
off); evaluated by ``python -m qe.ai shadow``. A PASS means human review, never
an automatic change.
"""

from qe.ai.shadow.gate import (
    GateResult,
    ShadowGateConfig,
    ShadowThresholds,
    evaluate,
    incremental_ic,
)
from qe.ai.shadow.ledger import Collected, ShadowObservation, ShadowReport, collect, run_shadow

__all__ = [
    "Collected",
    "GateResult",
    "ShadowGateConfig",
    "ShadowObservation",
    "ShadowReport",
    "ShadowThresholds",
    "collect",
    "evaluate",
    "incremental_ic",
    "run_shadow",
]
