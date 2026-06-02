"""Durable idempotency store for safe_actions — Phase 6.

Persists idempotency keys to DynamoDB so a SafeActionExecutor restart cannot
re-execute an action that already ran in a prior session.

Schema (risk-state table):
    PK  = "SAFE_ACTION_IDEMPOTENCY"
    SK  = <idempotency_key>          (e.g. "block_new_entries-feed-20260531")
    action_type   S — ActionType value
    mode          S — TradingMode value
    status        S — "executed" | "skipped"
    action_id     S — UUID4 of the SafeAction that succeeded
    created_at    S — ISO-8601 UTC
    result        S — JSON-serialised ExecutionResult.to_dict() (truncated)
    audit_ref     S — same as action_id (links to audit log)

The store uses a conditional write (``attribute_not_exists(SK)``) so two
concurrent executor instances cannot both claim the same key.

Idempotency keys are NOT automatically deleted.  A future phase may add a
``expires_at`` TTL attribute if key accumulation becomes a concern.

Thread-safety: this class is synchronous and safe for single-threaded use
inside asyncio.to_thread().  Concurrent use requires external locking.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("safe_actions.idempotency_store")

_PK = "SAFE_ACTION_IDEMPOTENCY"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DurableIdempotencyStore:
    """DynamoDB-backed idempotency store.  Survives process restarts.

    Args:
        dynamo_client:    boto3 low-level DynamoDB client.
        risk_state_table: Table name (same risk-state table used by kill switch).
    """

    def __init__(self, dynamo_client: Any, risk_state_table: str) -> None:
        self._dynamo = dynamo_client
        self._table = risk_state_table

    # ── public API ────────────────────────────────────────────────────────────

    def check(self, idempotency_key: str) -> tuple[bool, Optional[dict[str, Any]]]:
        """Return (found, existing_item_or_None).

        ``found=True`` means this key was already executed; the caller should
        skip and audit as idempotency_skip.
        """
        try:
            resp = self._dynamo.get_item(
                TableName=self._table,
                Key={"PK": {"S": _PK}, "SK": {"S": idempotency_key}},
                ProjectionExpression=(
                    "action_type, mode, #st, action_id, created_at, audit_ref"
                ),
                ExpressionAttributeNames={"#st": "status"},
            )
            item = resp.get("Item")
            if not item:
                return False, None
            return True, item
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "safe_actions.idempotency_store.check_failed key=%s error_type=%s "
                "— treating as not-found (will allow execution attempt)",
                idempotency_key, type(exc).__name__,
            )
            return False, None

    def mark(
        self,
        idempotency_key: str,
        *,
        action_id: str,
        action_type: str,
        mode: str,
        status: str = "executed",
        result_dict: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Write the idempotency key with a conditional-write guard.

        Returns True if written successfully, False if the key already existed
        or if DynamoDB was unavailable.
        """
        try:
            item: dict[str, Any] = {
                "PK": {"S": _PK},
                "SK": {"S": idempotency_key},
                "action_type": {"S": action_type},
                "mode": {"S": mode},
                "status": {"S": status},
                "action_id": {"S": action_id},
                "created_at": {"S": _utc_iso()},
                "audit_ref": {"S": action_id},
            }
            # Truncate result to 256 chars to stay well inside DynamoDB item limits.
            if result_dict is not None:
                item["result"] = {"S": json.dumps(result_dict, default=str)[:256]}

            self._dynamo.put_item(
                TableName=self._table,
                Item=item,
                ConditionExpression="attribute_not_exists(SK)",
            )
            logger.debug(
                "safe_actions.idempotency_store.marked key=%s action_type=%s",
                idempotency_key, action_type,
            )
            return True

        except Exception as exc:  # noqa: BLE001
            # ConditionalCheckFailedException means another process already marked it.
            error_type = type(exc).__name__
            if "ConditionalCheckFailed" in error_type or "ConditionalCheckFailed" in str(exc):
                logger.info(
                    "safe_actions.idempotency_store.already_marked key=%s "
                    "— concurrent execution race, treating as duplicate",
                    idempotency_key,
                )
            else:
                logger.warning(
                    "safe_actions.idempotency_store.mark_failed key=%s error_type=%s",
                    idempotency_key, error_type,
                )
            return False
