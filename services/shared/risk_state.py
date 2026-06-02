"""Canonical DynamoDB key schema for trading risk state.

This module is intentionally small and dependency-light so every service can
share the exact same keys and field names without importing risk_engine code.
It uses low-level DynamoDB attribute maps by default because most hot-path
services already use boto3 clients rather than resource Table objects.
"""

from __future__ import annotations

from typing import Any, Optional

RISK_STATE_SCHEMA_VERSION = "1.0"

KILL_SWITCH_PK = "KILLSWITCH"
KILL_SWITCH_SK = "GLOBAL"

POSITION_SK = "CURRENT"

NAV_PK = "NAV#CURRENT"
NAV_SK = "STATE"

RISK_DECISION_SK = "DECISION"

DATA_OUTBOX_PENDING_PK = "DATA_OUTBOX#PENDING"
DATA_OUTBOX_SK_PREFIX = "EVENT#"


def s(value: Any) -> dict[str, str]:
    """Return a DynamoDB string attribute."""
    return {"S": str(value)}


def n(value: Any) -> dict[str, str]:
    """Return a DynamoDB number attribute."""
    return {"N": str(value)}


def b(value: bool) -> dict[str, bool]:
    """Return a DynamoDB bool attribute."""
    return {"BOOL": bool(value)}


def kill_switch_key() -> dict[str, dict[str, str]]:
    """Low-level DynamoDB key for the global kill-switch row."""
    return {"PK": s(KILL_SWITCH_PK), "SK": s(KILL_SWITCH_SK)}


def kill_switch_resource_key() -> dict[str, str]:
    """Resource Table key for the global kill-switch row."""
    return {"PK": KILL_SWITCH_PK, "SK": KILL_SWITCH_SK}


def kill_switch_item(
    *,
    active: bool,
    reason: str,
    activated_by: str,
    updated_at: str,
    activated_at: Optional[str] = None,
    deactivated_at: Optional[str] = None,
    detail: str = "",
) -> dict[str, dict[str, Any]]:
    """Canonical low-level DynamoDB item for kill-switch state."""
    item: dict[str, dict[str, Any]] = {
        **kill_switch_key(),
        "active": b(active),
        "status": s("ACTIVE" if active else "INACTIVE"),
        "scope": s(KILL_SWITCH_SK),
        "reason": s(reason),
        "activated_by": s(activated_by),
        "updated_at": s(updated_at),
        "schema_version": s(RISK_STATE_SCHEMA_VERSION),
    }
    if active:
        item["activated_at"] = s(activated_at or updated_at)
    else:
        item["deactivated_at"] = s(deactivated_at or updated_at)
    if detail:
        item["detail"] = s(detail)
    return item


def position_key(symbol: str) -> dict[str, dict[str, str]]:
    """Low-level DynamoDB key for the current position of a symbol."""
    return {"PK": s(f"POSITION#{symbol}"), "SK": s(POSITION_SK)}


def nav_key() -> dict[str, dict[str, str]]:
    """Low-level DynamoDB key for the current NAV row."""
    return {"PK": s(NAV_PK), "SK": s(NAV_SK)}


def risk_decision_key(signal_id: str) -> dict[str, dict[str, str]]:
    """Low-level DynamoDB key for the risk decision reservation of a signal."""
    return {"PK": s(f"RISK_DECISION#{signal_id}"), "SK": s(RISK_DECISION_SK)}


def data_outbox_key(event_id: str) -> dict[str, dict[str, str]]:
    """Low-level DynamoDB key for a pending market-data outbox event."""
    return {"PK": s(DATA_OUTBOX_PENDING_PK), "SK": s(f"{DATA_OUTBOX_SK_PREFIX}{event_id}")}


RECONCILIATION_PK = "RECONCILIATION#STATE"
RECONCILIATION_SK = "GLOBAL"


def reconciliation_key() -> dict[str, dict[str, str]]:
    """Low-level DynamoDB key for the global reconciliation-required flag."""
    return {"PK": s(RECONCILIATION_PK), "SK": s(RECONCILIATION_SK)}


# ── safe-actions entry block ───────────────────────────────────────────────────
# Written by SafeActionDynamoWriter when BLOCK_NEW_ENTRIES executes.
# Reading side (strategy_engine / risk_engine) is wired in Phase 5.

ENTRY_BLOCK_PK = "ENTRY_BLOCK"
ENTRY_BLOCK_SK = "GLOBAL"


def entry_block_key() -> dict[str, dict[str, str]]:
    """Low-level DynamoDB key for the global entry-block flag."""
    return {"PK": s(ENTRY_BLOCK_PK), "SK": s(ENTRY_BLOCK_SK)}


def entry_block_item(
    *,
    blocked: bool,
    reason: str,
    source: str,
    action_id: str,
    idempotency_key: str,
    created_at: str,
) -> dict[str, dict[str, Any]]:
    """Canonical low-level DynamoDB item for the global entry-block state.

    ``blocked=True`` means: do not accept new entry signals.
    Exit management (TEE, MIS, ExitOrderRouter) is not affected — this
    flag governs only strategy_engine entry-signal production.
    """
    return {
        **entry_block_key(),
        "blocked": b(blocked),
        "status": s("BLOCKED" if blocked else "CLEAR"),
        "reason": s(reason),
        "source": s(source),
        "action_id": s(action_id),
        "idempotency_key": s(idempotency_key),
        "created_at": s(created_at),
        "schema_version": s(RISK_STATE_SCHEMA_VERSION),
    }


def attr_bool(item: dict[str, Any], name: str, default: bool = False) -> bool:
    """Read a bool from either low-level or resource-style DynamoDB items."""
    raw = item.get(name)
    if isinstance(raw, dict):
        if "BOOL" in raw:
            return bool(raw["BOOL"])
        if "S" in raw:
            return str(raw["S"]).upper() in {"ACTIVE", "TRUE", "1", "YES"}
    if isinstance(raw, str):
        return raw.upper() in {"ACTIVE", "TRUE", "1", "YES"}
    if raw is None and name == "active":
        status = attr_string(item, "status", "")
        if status:
            return status.upper() == "ACTIVE"
    return bool(raw) if raw is not None else default


def attr_string(item: dict[str, Any], name: str, default: str = "") -> str:
    """Read a string from either low-level or resource-style DynamoDB items."""
    raw = item.get(name)
    if isinstance(raw, dict):
        if "S" in raw:
            return str(raw["S"])
        if "N" in raw:
            return str(raw["N"])
        if "BOOL" in raw:
            return str(raw["BOOL"])
    if raw is None:
        return default
    return str(raw)


def attr_number(item: dict[str, Any], name: str, default: float = 0.0) -> float:
    """Read a number from either low-level or resource-style DynamoDB items."""
    raw = item.get(name)
    if isinstance(raw, dict):
        raw = raw.get("N")
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default
