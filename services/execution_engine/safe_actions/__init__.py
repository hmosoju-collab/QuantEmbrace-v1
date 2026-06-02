"""Phase 3 safe_actions layer — conservative autonomous operations.

SAFETY CONTRACT (binds every action added here):
    A safe action REDUCES risk, improves observability, or repairs paper/runtime
    state. It NEVER:
        * places broker orders or increases market exposure
        * enables live trading or changes capital limits
        * restarts Docker containers or live services autonomously
        * bypasses human approval for irreversible operations
        * clears the kill switch, exit locks, or reconciliation flags
        * silences alerts

    The executor fails closed: an unrecognised action type, a policy violation,
    a precondition failure, or any unexpected error produces a blocked/failed
    ExecutionResult with a full audit record — never a silent pass-through.

Phases:
    Phase 3 (this module): design + models + policy + classifier + executor stub.
        Only SEND_ALERT and GENERATE_RUNBOOK_COMMAND are fully executed.
        All DynamoDB/Kafka write actions are routed through the framework
        (policy + idempotency + audit) but return NotImplementedError stubs
        until Phase 4 wires the real writers.
    Phase 4: wires the executor into the monitoring agent's action loop behind
        ACTION_MODE=safe_actions, implements the DynamoDB/Kafka write handlers.

See docs/architecture/safe-actions-design.md for the full design.
"""

from __future__ import annotations

from execution_engine.safe_actions.safe_action_audit import SafeActionAudit
from execution_engine.safe_actions.safe_action_classifier import SafeActionClassifier
from execution_engine.safe_actions.safe_action_dynamo_writer import (
    SafeActionDynamoWriter,
    WriteResult,
)
from execution_engine.safe_actions.safe_action_executor import SafeActionExecutor
from execution_engine.safe_actions.safe_action_models import (
    ActionScope,
    ActionType,
    ClassificationResult,
    ClassifiedObservation,
    ExecutionResult,
    RiskLevel,
    SafeAction,
    TradingMode,
)
from execution_engine.safe_actions.safe_action_policy import SafeActionPolicy

__all__ = [
    # models
    "ActionType",
    "ActionScope",
    "RiskLevel",
    "TradingMode",
    "ClassificationResult",
    "SafeAction",
    "ExecutionResult",
    "ClassifiedObservation",
    # policy
    "SafeActionPolicy",
    # classifier
    "SafeActionClassifier",
    # audit
    "SafeActionAudit",
    # dynamo writer (Phase 4)
    "SafeActionDynamoWriter",
    "WriteResult",
    # executor
    "SafeActionExecutor",
]
