"""Mode-based policy: what SafeAction types and scopes are allowed.

Rules are static (no I/O) and checked before any executor dispatch. The policy
is the first line of defence; the executor's forbidden-type guard is the second.

Design principles:
    * Deny by default: if a mode has no entry for a scope, it is denied.
    * Fail closed: any uncertainty → deny.
    * Explicit forbidden list: operations that must never run are enumerated here
      rather than relying on the absence of a permit.
    * Phase 3 only: LIVE_HUMAN_GATED scope is always read-only advisory (no live
      mutation). Phase 4 may relax specific entries after operator sign-off.
"""

from __future__ import annotations

from execution_engine.safe_actions.safe_action_models import (
    ActionScope,
    ActionType,
    TradingMode,
)

# ── always-forbidden (regardless of mode or scope) ────────────────────────────

_ALWAYS_FORBIDDEN_TYPES: frozenset[ActionType] = frozenset(
    {
        ActionType.FORBIDDEN,
    }
)

# Context strings for the forbidden-action audit log.
# These are actions that have no ActionType mapping but must be explicitly
# rejected if someone tries to manufacture them via a novel code path.
FORBIDDEN_CONTEXT_LABELS: frozenset[str] = frozenset(
    {
        "enable_live_trading",
        "set_trading_mode_live",
        "change_capital_limits",
        "place_real_broker_order",
        "restart_live_execution_engine",
        "restart_live_risk_engine",
        "restart_live_ai_engine",
        "restart_docker_container",
        "mutate_strategy_parameters",
        "clear_kill_switch",
        "clear_reconciliation_required",
        "clear_exit_order_id",
        "delete_dynamodb_records",
        "auto_promote_strategy_to_live",
        "modify_allowed_symbols",
        "modify_allowed_strategies",
        "silence_alerts",
    }
)

# ── paper-repair action types (forbidden in all live modes) ──────────────────

_PAPER_ONLY_TYPES: frozenset[ActionType] = frozenset(
    {
        ActionType.ATTACH_EXIT_POLICY_PAPER,
        ActionType.REPAIR_PAPER_ZERO_QTY_OPEN,
        ActionType.UPDATE_PAPER_DIRECTION_FROM_QUANTITY,
        ActionType.FORCE_PAPER_SQUARE_OFF,
    }
)

# ── per-mode allowed scopes ────────────────────────────────────────────────────

_MODE_ALLOWED_SCOPES: dict[TradingMode, frozenset[ActionScope]] = {
    TradingMode.PAPER: frozenset(
        {
            ActionScope.READ_ONLY,
            ActionScope.PAPER_ONLY,
            ActionScope.RISK_REDUCTION,
            ActionScope.LIVE_HUMAN_GATED,
        }
    ),
    TradingMode.BACKTEST: frozenset(
        {
            ActionScope.READ_ONLY,
        }
    ),
    TradingMode.LIVE_DISABLED: frozenset(
        {
            ActionScope.READ_ONLY,
            ActionScope.RISK_REDUCTION,
            ActionScope.LIVE_HUMAN_GATED,
        }
    ),
    TradingMode.LIVE_STAGE_1: frozenset(
        {
            ActionScope.READ_ONLY,
            ActionScope.RISK_REDUCTION,
            ActionScope.LIVE_HUMAN_GATED,
        }
    ),
    TradingMode.LIVE_FULL_BLOCKED: frozenset(
        {
            ActionScope.READ_ONLY,
        }
    ),
}

# ── per-mode additional type-level denials ────────────────────────────────────
# These apply on top of the scope gate for extra precision.

_MODE_DENIED_TYPES: dict[TradingMode, frozenset[ActionType]] = {
    TradingMode.PAPER: frozenset(),
    TradingMode.BACKTEST: frozenset(
        {
            ActionType.ACTIVATE_KILL_SWITCH,
            ActionType.BLOCK_NEW_ENTRIES,
            ActionType.PAUSE_STRATEGY_ENTRIES,
            ActionType.MARK_LIVE_READINESS_BLOCKED,
            ActionType.FORCE_PAPER_SQUARE_OFF,
            ActionType.RUN_RECONCILIATION,
        }
    ),
    TradingMode.LIVE_DISABLED: _PAPER_ONLY_TYPES,
    TradingMode.LIVE_STAGE_1: _PAPER_ONLY_TYPES,
    TradingMode.LIVE_FULL_BLOCKED: _PAPER_ONLY_TYPES
    | frozenset(
        {
            # In a fully-blocked state only read-only + human-gated are safe.
            # Kill switch ON is still allowed (reduces risk), but only via
            # READ_ONLY advisory path — actual write deferred to Phase 4.
            ActionType.BLOCK_NEW_ENTRIES,
            ActionType.PAUSE_STRATEGY_ENTRIES,
        }
    ),
}

# ── human-approval required regardless of mode ────────────────────────────────

_ALWAYS_HUMAN_GATED_SCOPES: frozenset[ActionScope] = frozenset(
    {ActionScope.LIVE_HUMAN_GATED}
)

_ALWAYS_HUMAN_GATED_TYPES: frozenset[ActionType] = frozenset(
    {ActionType.GENERATE_RUNBOOK_COMMAND}
)


# ── policy class ──────────────────────────────────────────────────────────────


class SafeActionPolicy:
    """Stateless policy gate — must be consulted before every executor dispatch.

    Usage::

        policy = SafeActionPolicy(TradingMode.PAPER)
        allowed, reason = policy.is_allowed(ActionType.BLOCK_NEW_ENTRIES,
                                             ActionScope.RISK_REDUCTION)
        if not allowed:
            ...
    """

    def __init__(self, mode: TradingMode) -> None:
        self._mode = mode

    @property
    def mode(self) -> TradingMode:
        return self._mode

    def is_allowed(
        self,
        action_type: ActionType,
        scope: ActionScope,
    ) -> tuple[bool, str]:
        """Return ``(allowed, reason_if_denied)``.

        Checks in order:
            1. Always-forbidden type
            2. Always-forbidden scope
            3. Scope not in mode's allowed set
            4. Type in mode's deny list
        """
        # 1. Unconditionally forbidden action type
        if action_type in _ALWAYS_FORBIDDEN_TYPES or action_type is ActionType.FORBIDDEN:
            return False, f"{action_type.value} is always forbidden"

        # 2. Unconditionally forbidden scope
        if scope is ActionScope.FORBIDDEN:
            return False, "ActionScope.FORBIDDEN is never executable"

        # 3. Scope not permitted in this mode
        allowed_scopes = _MODE_ALLOWED_SCOPES.get(self._mode, frozenset())
        if scope not in allowed_scopes:
            return (
                False,
                f"scope {scope.value} not permitted in mode {self._mode.value}",
            )

        # 4. Type explicitly denied in this mode (despite scope being allowed)
        denied_types = _MODE_DENIED_TYPES.get(self._mode, frozenset())
        if action_type in denied_types:
            return (
                False,
                f"{action_type.value} is denied in mode {self._mode.value}",
            )

        return True, ""

    def requires_human_approval(
        self,
        action_type: ActionType,
        scope: ActionScope,
    ) -> bool:
        """True when the action must be approved by a human before execution."""
        return (
            scope in _ALWAYS_HUMAN_GATED_SCOPES
            or action_type in _ALWAYS_HUMAN_GATED_TYPES
        )

    def is_context_forbidden(self, context_label: str) -> bool:
        """True when a raw context label (not an ActionType) is in the forbidden set."""
        return context_label in FORBIDDEN_CONTEXT_LABELS

    @classmethod
    def from_env(cls) -> "SafeActionPolicy":
        """Resolve TradingMode from environment variables (safe default: LIVE_DISABLED).

        Mapping:
            RISK_PROFILE=paper  + LIVE_FLAG absent/false → PAPER
            RISK_PROFILE=*      + LIVE_FLAG absent/false → LIVE_DISABLED
            RISK_PROFILE=tiny-live + LIVE_FLAG=true      → LIVE_STAGE_1
            anything else                                 → LIVE_FULL_BLOCKED
        """
        import os

        risk_profile = os.environ.get("RISK_PROFILE", "").lower()
        live_flag = os.environ.get("QE_EXECUTION_LIVE_TRADING_ENABLED", "").lower()
        live_enabled = live_flag in ("1", "true", "yes")

        if not live_enabled:
            mode = (
                TradingMode.PAPER
                if risk_profile == "paper"
                else TradingMode.LIVE_DISABLED
            )
        elif risk_profile == "tiny-live":
            mode = TradingMode.LIVE_STAGE_1
        else:
            mode = TradingMode.LIVE_FULL_BLOCKED

        return cls(mode)
