"""Reads the global entry-block state from DynamoDB.

The entry-block flag is written by SafeActionDynamoWriter.write_block_new_entries()
and stored at PK=ENTRY_BLOCK / SK=GLOBAL in the risk-state table.

Design mirrors the kill-switch reader pattern in strategy_engine/service.py:
    * Short configurable TTL cache (default 5s) avoids per-signal DynamoDB reads.
    * Thread-safe for use with asyncio.to_thread().
    * Fail behavior is mode-aware:
          PAPER  — on read failure, WARN and allow (configurable via fail_closed_on_error).
          LIVE   — on read failure, fail closed (block new entries) to prevent
                   trading on stale safety state.

This module is intentionally dependency-light (only shared/risk_state imports)
so any service can add it without pulling in execution_engine or monitoring_agent.

Reading side only — never writes, never deletes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from shared.risk_state import ENTRY_BLOCK_PK, ENTRY_BLOCK_SK, attr_bool, attr_string

logger = logging.getLogger("shared.entry_block_reader")


def _utc_now() -> float:
    return time.monotonic()


@dataclass(frozen=True)
class EntryBlockState:
    """Snapshot of the current entry-block state."""

    blocked: bool
    reason: str = ""
    source: str = ""
    action_id: str = ""
    idempotency_key: str = ""
    created_at: str = ""
    schema_version: str = ""
    read_ok: bool = True
    """False when the DynamoDB read failed (response is last-known or default)."""

    @classmethod
    def allow(cls) -> "EntryBlockState":
        """Entry is allowed (flag absent or blocked=False)."""
        return cls(blocked=False)

    @classmethod
    def blocked_from_item(cls, item: dict[str, Any]) -> "EntryBlockState":
        """Parse a DynamoDB low-level item into EntryBlockState."""
        return cls(
            blocked=attr_bool(item, "blocked", False),
            reason=attr_string(item, "reason"),
            source=attr_string(item, "source"),
            action_id=attr_string(item, "action_id"),
            idempotency_key=attr_string(item, "idempotency_key"),
            created_at=attr_string(item, "created_at"),
            schema_version=attr_string(item, "schema_version"),
            read_ok=True,
        )

    @classmethod
    def fail_closed(cls) -> "EntryBlockState":
        """Fail-closed state: block entries because read failed in live mode."""
        return cls(blocked=True, reason="entry_block_read_failure", read_ok=False)

    @classmethod
    def fail_open(cls) -> "EntryBlockState":
        """Fail-open state: allow entries despite read failure (paper mode)."""
        return cls(blocked=False, reason="entry_block_read_failure", read_ok=False)


class EntryBlockReader:
    """Synchronous DynamoDB reader for the ENTRY_BLOCK/GLOBAL flag.

    Args:
        dynamo_client:     boto3 DynamoDB low-level client.
        risk_state_table:  Fully-qualified DynamoDB table name.
        cache_ttl_seconds: How long to cache the last-read state (default 5s).
        fail_closed_on_error: If True, a DynamoDB read failure blocks entries.
                              If False (paper-safe), read failure warns and allows.
    """

    def __init__(
        self,
        dynamo_client: Any,
        risk_state_table: str,
        cache_ttl_seconds: float = 5.0,
        fail_closed_on_error: bool = False,
    ) -> None:
        self._dynamo = dynamo_client
        self._table = risk_state_table
        self._ttl = cache_ttl_seconds
        self._fail_closed = fail_closed_on_error
        self._cached: EntryBlockState = EntryBlockState.allow()
        self._cached_at: float = 0.0

    def read(self, *, force: bool = False) -> EntryBlockState:
        """Read the entry-block state, using the cache if within TTL.

        Args:
            force: Bypass the cache and always read from DynamoDB.

        Returns:
            EntryBlockState with ``blocked=True`` if new entries should be halted.
        """
        now = _utc_now()
        if not force and (now - self._cached_at) < self._ttl:
            return self._cached

        try:
            response = self._dynamo.get_item(
                TableName=self._table,
                Key={"PK": {"S": ENTRY_BLOCK_PK}, "SK": {"S": ENTRY_BLOCK_SK}},
                ProjectionExpression=(
                    "blocked, #st, reason, source, action_id, "
                    "idempotency_key, created_at, schema_version"
                ),
                ExpressionAttributeNames={"#st": "status"},
            )
            item = response.get("Item")
            if not item:
                # Flag absent → entries allowed
                state = EntryBlockState.allow()
            else:
                state = EntryBlockState.blocked_from_item(item)

            if state.blocked and state != self._cached:
                logger.warning(
                    "entry_block.active reason=%s source=%s action_id=%s created_at=%s",
                    state.reason, state.source, state.action_id, state.created_at,
                )
            elif not state.blocked and self._cached.blocked:
                logger.info("entry_block.cleared — new entries allowed again")

            self._cached = state
            self._cached_at = now
            return state

        except Exception as exc:  # noqa: BLE001
            error_type = type(exc).__name__
            if self._fail_closed:
                logger.error(
                    "entry_block_reader.read_failed error_type=%s — fail closed (live mode)",
                    error_type,
                )
                # Do NOT update cache so we keep returning fail_closed on repeated failures.
                return EntryBlockState.fail_closed()
            else:
                logger.warning(
                    "entry_block_reader.read_failed error_type=%s — fail open (paper mode)",
                    error_type,
                )
                return EntryBlockState.fail_open()

    def invalidate(self) -> None:
        """Force the next read to bypass the cache."""
        self._cached_at = 0.0

    @property
    def cached_state(self) -> EntryBlockState:
        return self._cached
