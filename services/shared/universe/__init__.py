"""
Universe management — shared library for trading universe control.

Key exports:
    UniverseMode            — PAPER_SAFE_START | PAPER_EXPAND | LIVE_ADVANCED
    UniverseSnapshot        — immutable approved-symbol set for a mode + date
    UniverseBuilder         — builds a snapshot by applying filter chain
    UniverseOrderValidator  — validates orders against an approved snapshot
    ValidationResult        — result of order validation (approved + reason)
    PromotionGateEvaluator  — evaluates promotion gate criteria

Quick start:
    from shared.universe import UniverseMode, UniverseBuilder, UniverseOrderValidator

    builder = UniverseBuilder.from_yaml_config()
    snapshot = builder.build(UniverseMode.PAPER_SAFE_START, date.today())
    validator = UniverseOrderValidator(snapshot)

    result = validator.validate("RELIANCE", "NSE")
    if not result.approved:
        raise ValueError(result.reason)
"""

from shared.universe.builder import UniverseBuilder, UniverseSnapshotError
from shared.universe.models import (
    CorporateActionEvent,
    CorporateActionType,
    ExclusionReason,
    LiquidityMetrics,
    RiskMetrics,
    SnapshotFailureMode,
    SymbolMaster,
    UniverseAuditLog,
    UniverseDecision,
    UniverseSnapshot,
    ValidationResult,
)
from shared.universe.modes import UniverseMode
from shared.universe.order_validator import UniverseOrderValidator, build_validator_for_today
from shared.universe.promotion import PromotionGateEvaluator, PromotionGateResult
from shared.universe.snapshot_store import (
    DynamoSnapshotStore,
    InMemorySnapshotStore,
    SnapshotStore,
    build_snapshot_store,
)

__all__ = [
    # Modes
    "UniverseMode",
    # Models
    "UniverseSnapshot",
    "UniverseDecision",
    "ValidationResult",
    "ExclusionReason",
    "SnapshotFailureMode",
    "SymbolMaster",
    "LiquidityMetrics",
    "RiskMetrics",
    "CorporateActionEvent",
    "CorporateActionType",
    "UniverseAuditLog",
    # Builder
    "UniverseBuilder",
    "UniverseSnapshotError",
    # Validator
    "UniverseOrderValidator",
    "build_validator_for_today",
    # Store
    "SnapshotStore",
    "InMemorySnapshotStore",
    "DynamoSnapshotStore",
    "build_snapshot_store",
    # Promotion
    "PromotionGateEvaluator",
    "PromotionGateResult",
]
