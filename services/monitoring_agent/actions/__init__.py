"""Action layer — Phase 3 design + framework COMPLETE; Phase 4 wiring pending.

SAFETY CONTRACT (binds every action ever added here):
    Actions may only ever *reduce* risk. They must NEVER place trades, increase
    exposure, override a risk rejection, change NAV, or disable the kill switch.

Phase 3 (COMPLETE — 2026-05-31):
    The safe_actions framework lives at
    ``services/execution_engine/safe_actions/``. It provides:
        SafeActionClassifier  — maps Phase 2 findings → ClassifiedObservation
        SafeActionPolicy      — mode-based allow/deny (PAPER/LIVE_DISABLED/…)
        SafeActionExecutor    — policy + idempotency + preconditions + audit
        SafeActionAudit       — append-only JSONL audit log (never raises)
    Three action types are fully implemented:
        SEND_ALERT, GENERATE_RUNBOOK_COMMAND, READ_RUNTIME_STATE
    All DynamoDB/Kafka write actions are framework-complete stubs (Phase 4).
    Docker restart is explicitly forbidden and never proposed by the classifier.

Phase 4 (PLANNED — not yet started):
    Wire SafeActionExecutor into MonitoringAgent.run_once() behind
    MONITORING_ACTION_MODE=safe_actions. Implement DynamoDB/Kafka write handlers
    for BLOCK_NEW_ENTRIES, ACTIVATE_KILL_SWITCH, paper repair actions, etc.
    See docs/architecture/safe-actions-design.md §11 for the full Phase 4 plan.

The monitoring agent remains in notify_only mode until the operator explicitly
sets ACTION_MODE=safe_actions. No autonomous action occurs by default.
"""

from __future__ import annotations

# Phase 3 public API re-exported from execution_engine.safe_actions
# (pythonpath=["services"] makes this importable from monitoring_agent context)
try:
    from execution_engine.safe_actions import (  # noqa: F401
        ActionScope,
        ActionType,
        ClassificationResult,
        ClassifiedObservation,
        ExecutionResult,
        RiskLevel,
        SafeAction,
        SafeActionAudit,
        SafeActionClassifier,
        SafeActionExecutor,
        SafeActionPolicy,
        TradingMode,
    )
    __all__ = [
        "ActionType", "ActionScope", "RiskLevel", "TradingMode",
        "ClassificationResult", "SafeAction", "ExecutionResult",
        "ClassifiedObservation", "SafeActionPolicy", "SafeActionClassifier",
        "SafeActionAudit", "SafeActionExecutor",
    ]
except ImportError:
    # Graceful fallback if execution_engine is not on the path (e.g. isolated test env)
    __all__: list[str] = []
