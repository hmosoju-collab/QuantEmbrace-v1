"""DynamoDB write handlers for Phase 4 safe actions.

Phase 4 implements exactly two write actions:

    BLOCK_NEW_ENTRIES     → ENTRY_BLOCK / GLOBAL  in the risk-state table
    ACTIVATE_KILL_SWITCH  → KILLSWITCH  / GLOBAL  in the risk-state table

Every other action type remains read-only, alert-only, paper-only, manual
runbook, or forbidden. This is by design: keeping the write surface minimal
keeps the blast radius minimal.

Safety invariants enforced here:
    * Never deletes any record.
    * Never clears the kill switch, the reconciliation flag, or exit locks.
    * Never writes live_trading_enabled or any capital-limit field.
    * BLOCK_NEW_ENTRIES affects entry-signal production only — it does NOT
      touch orders, positions, TEE config, MIS config, or ExitOrderRouter
      state. Exit management continues unchanged.
    * ACTIVATE_KILL_SWITCH uses the canonical shared/risk_state schema so
      the existing KillSwitch reader in risk_engine sees the correct item.
    * Both writes are idempotent: calling put_item with the same payload
      twice leaves DynamoDB in the same state.

Reading side for BLOCK_NEW_ENTRIES is Phase 5: strategy_engine /
risk_engine will read ENTRY_BLOCK/GLOBAL and stop producing/approving
new entry signals when blocked=True. Until Phase 5 wires the reader
this write is advisory (visible in DynamoDB but not yet enforced by
the trading pipeline).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from shared.risk_state import (
    ENTRY_BLOCK_PK,
    ENTRY_BLOCK_SK,
    KILL_SWITCH_PK,
    KILL_SWITCH_SK,
    entry_block_item,
    kill_switch_item,
)

logger = logging.getLogger("safe_actions.dynamo_writer")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WriteResult:
    """Outcome of a single DynamoDB write attempt."""

    success: bool
    table: str
    pk: str
    sk: str
    error_type: Optional[str] = None
    was_noop: bool = False
    """True when the write succeeded but a newer/equal record already existed
    (read-before-write idempotency check, if performed)."""


class SafeActionDynamoWriter:
    """Synchronous DynamoDB write handlers for BLOCK_NEW_ENTRIES and ACTIVATE_KILL_SWITCH.

    Uses the same low-level boto3 DynamoDB client pattern as KillSwitch so
    the risk-state table reader sees a consistent schema.

    Args:
        dynamo_client:    boto3 DynamoDB client (``boto3.client("dynamodb")``).
                          Accept any object with ``put_item`` / ``get_item``
                          so tests can inject a fake without moto.
        risk_state_table: Fully-qualified DynamoDB table name, e.g.
                          ``"quantembrace-prod-risk-state"``.
    """

    def __init__(self, dynamo_client: Any, risk_state_table: str) -> None:
        self._dynamo = dynamo_client
        self._table = risk_state_table

    # ── BLOCK_NEW_ENTRIES ─────────────────────────────────────────────────────

    def write_block_new_entries(
        self,
        *,
        action_id: str,
        idempotency_key: str,
        reason: str,
        source: str = "safe_actions",
    ) -> WriteResult:
        """Write ENTRY_BLOCK/GLOBAL to the risk-state table.

        Effect: sets ``blocked=True`` globally. This prevents new entry
        signals from being produced by strategy_engine (Phase 5 reader).
        Exit management (TEE, MIS, ExitOrderRouter) is unaffected.

        Idempotent: calling this multiple times with the same
        ``idempotency_key`` writes the same item — no secondary effects.
        """
        now = _utc_iso()
        item = entry_block_item(
            blocked=True,
            reason=reason,
            source=source,
            action_id=action_id,
            idempotency_key=idempotency_key,
            created_at=now,
        )
        return self._put(item, pk=ENTRY_BLOCK_PK, sk=ENTRY_BLOCK_SK)

    # ── ACTIVATE_KILL_SWITCH ─────────────────────────────────────────────────

    def write_kill_switch_active(
        self,
        *,
        action_id: str,
        idempotency_key: str,
        reason: str,
        activated_by: str = "safe_actions",
    ) -> WriteResult:
        """Write KILLSWITCH/GLOBAL active=True to the risk-state table.

        Uses the canonical ``kill_switch_item()`` schema from
        ``shared/risk_state`` so the existing ``KillSwitch._load_state()``
        reader in risk_engine sees exactly the same item shape it already
        knows how to parse.

        The existing risk_engine kill-switch Kafka listener (KafkaKillSwitchListener)
        also polls DynamoDB; after this write takes effect the next read
        will see ``active=True`` and halt new signal approvals.

        This write NEVER:
            * Deactivates the kill switch.
            * Modifies exit management state.
            * Changes capital limits.
            * Sets live_trading_enabled.

        Idempotent: put_item unconditionally overwrites with active=True;
        calling this twice leaves the table in the same state.
        """
        now = _utc_iso()
        item = kill_switch_item(
            active=True,
            reason=reason,
            activated_by=activated_by,
            activated_at=now,
            updated_at=now,
            detail=f"action_id={action_id} idempotency_key={idempotency_key} source=safe_actions",
        )
        return self._put(item, pk=KILL_SWITCH_PK, sk=KILL_SWITCH_SK)

    # ── internal ──────────────────────────────────────────────────────────────

    def _put(
        self,
        item: dict[str, Any],
        *,
        pk: str,
        sk: str,
    ) -> WriteResult:
        """Call put_item; catch every exception; return WriteResult."""
        try:
            self._dynamo.put_item(TableName=self._table, Item=item)
            logger.info(
                "safe_actions.dynamo_writer.put_ok table=%s pk=%s sk=%s",
                self._table,
                pk,
                sk,
            )
            return WriteResult(success=True, table=self._table, pk=pk, sk=sk)
        except Exception as exc:  # noqa: BLE001
            error_type = type(exc).__name__
            logger.error(
                "safe_actions.dynamo_writer.put_failed table=%s pk=%s sk=%s error_type=%s",
                self._table,
                pk,
                sk,
                error_type,
            )
            return WriteResult(
                success=False,
                table=self._table,
                pk=pk,
                sk=sk,
                error_type=error_type,
            )
