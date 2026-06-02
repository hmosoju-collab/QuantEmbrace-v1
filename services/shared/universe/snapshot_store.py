"""
UniverseSnapshotStore — persist and retrieve UniverseSnapshot objects.

Two backends:
  InMemorySnapshotStore — process-local, no persistence (default for paper mode / tests).
  DynamoSnapshotStore   — DynamoDB-backed, survives restarts (production).

Key design:
  - Paper and live snapshots are stored under different DynamoDB key namespaces.
  - Once stored, a snapshot for a given (mode, date) is never overwritten.
  - Snapshots expire after snapshot.ttl_days (DynamoDB TTL attribute).
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import date
from typing import Optional

from shared.universe.models import UniverseSnapshot
from shared.universe.modes import UniverseMode

logger = logging.getLogger(__name__)


# ── Abstract store ────────────────────────────────────────────────────────────

class SnapshotStore(ABC):
    """Protocol for snapshot persistence backends."""

    @abstractmethod
    def save(self, snapshot: UniverseSnapshot) -> None:
        """Persist a snapshot. No-op if already exists for (mode, trading_date)."""

    @abstractmethod
    def get(self, mode: UniverseMode, trading_date: date) -> Optional[UniverseSnapshot]:
        """Retrieve snapshot for a given mode and date. None if not stored."""

    @abstractmethod
    def exists(self, mode: UniverseMode, trading_date: date) -> bool:
        """True if a snapshot exists for (mode, trading_date)."""


# ── In-memory store ───────────────────────────────────────────────────────────

class InMemorySnapshotStore(SnapshotStore):
    """
    Simple in-process dictionary store. Snapshots survive for the lifetime of the process.

    Safe for paper trading and tests. Not suitable for production multi-instance deployments.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, date], UniverseSnapshot] = {}

    def _key(self, mode: UniverseMode, trading_date: date) -> tuple[str, date]:
        return (mode.value, trading_date)

    def save(self, snapshot: UniverseSnapshot) -> None:
        key = self._key(snapshot.mode, snapshot.trading_date)
        if key in self._store:
            logger.debug(
                "snapshot_store.already_exists mode=%s date=%s — not overwriting",
                snapshot.mode.value, snapshot.trading_date,
            )
            return
        self._store[key] = snapshot
        logger.info(
            "snapshot_store.saved mode=%s date=%s approved=%d",
            snapshot.mode.value, snapshot.trading_date, snapshot.size,
        )

    def get(self, mode: UniverseMode, trading_date: date) -> Optional[UniverseSnapshot]:
        return self._store.get(self._key(mode, trading_date))

    def exists(self, mode: UniverseMode, trading_date: date) -> bool:
        return self._key(mode, trading_date) in self._store

    def clear(self) -> None:
        """Clear all stored snapshots (test utility)."""
        self._store.clear()


# ── DynamoDB store ────────────────────────────────────────────────────────────

class DynamoSnapshotStore(SnapshotStore):
    """
    DynamoDB-backed snapshot store.

    Table schema:
        PK: "UNIVERSE#{namespace}#{mode}"   e.g. "UNIVERSE#PAPER#PAPER_SAFE_START"
        SK: "DATE#{trading_date}"           e.g. "DATE#2026-05-26"
        Attributes:
            approved_symbols: list[str]  (stored as JSON string to fit DynamoDB 400KB limit)
            generated_at:     str (ISO-8601 UTC)
            failure_mode:     str | null
            failure_details:  str
            checksum:         str
            data_sources:     list[str]
            ttl:              int (epoch seconds — DynamoDB TTL)

    Paper/live isolation: namespace is "PAPER" for paper modes, "LIVE" for live mode.

    This is a STUB implementation. Replace the boto3 calls once DynamoDB client
    is available in the deployment environment.
    """

    # TTL: 30 days from snapshot creation
    _TTL_DAYS = 30

    def __init__(
        self,
        dynamo_client: object,           # boto3 DynamoDB client
        table_name: str = "qe_universe_snapshot",
    ) -> None:
        self._dynamo = dynamo_client
        self._table = table_name

    def _pk(self, mode: UniverseMode) -> str:
        return f"UNIVERSE#{mode.dynamo_namespace}#{mode.value}"

    def _sk(self, trading_date: date) -> str:
        return f"DATE#{trading_date.isoformat()}"

    def save(self, snapshot: UniverseSnapshot) -> None:
        if self.exists(snapshot.mode, snapshot.trading_date):
            logger.debug(
                "dynamo_snapshot_store.already_exists mode=%s date=%s — not overwriting",
                snapshot.mode.value, snapshot.trading_date,
            )
            return

        import time
        ttl_epoch = int(time.time()) + self._TTL_DAYS * 86400

        item = {
            "PK": {"S": self._pk(snapshot.mode)},
            "SK": {"S": self._sk(snapshot.trading_date)},
            "approved_symbols": {"S": json.dumps(sorted(snapshot.approved_symbols))},
            "generated_at": {"S": snapshot.generated_at.isoformat()},
            "failure_mode": {"S": snapshot.failure_mode.value if snapshot.failure_mode else ""},
            "failure_details": {"S": snapshot.failure_details},
            "checksum": {"S": snapshot.checksum},
            "data_sources": {"S": json.dumps(sorted(snapshot.data_sources_used))},
            "ttl": {"N": str(ttl_epoch)},
        }

        try:
            self._dynamo.put_item(  # type: ignore[union-attr]
                TableName=self._table,
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
            logger.info(
                "dynamo_snapshot_store.saved mode=%s date=%s approved=%d",
                snapshot.mode.value, snapshot.trading_date, snapshot.size,
            )
        except Exception as exc:
            if "ConditionalCheckFailedException" in str(type(exc).__name__):
                logger.debug("dynamo_snapshot_store.race_condition_ok mode=%s date=%s",
                             snapshot.mode.value, snapshot.trading_date)
            else:
                logger.error(
                    "dynamo_snapshot_store.save_failed mode=%s date=%s error=%s",
                    snapshot.mode.value, snapshot.trading_date, exc,
                )
                raise

    def get(self, mode: UniverseMode, trading_date: date) -> Optional[UniverseSnapshot]:
        try:
            response = self._dynamo.get_item(  # type: ignore[union-attr]
                TableName=self._table,
                Key={
                    "PK": {"S": self._pk(mode)},
                    "SK": {"S": self._sk(trading_date)},
                },
            )
        except Exception as exc:
            logger.error(
                "dynamo_snapshot_store.get_failed mode=%s date=%s error=%s",
                mode.value, trading_date, exc,
            )
            return None

        item = response.get("Item")
        if not item:
            return None

        from datetime import datetime, timezone
        from shared.universe.models import SnapshotFailureMode

        approved = frozenset(json.loads(item["approved_symbols"]["S"]))
        generated_at = datetime.fromisoformat(item["generated_at"]["S"])
        fm_str = item.get("failure_mode", {}).get("S", "")
        failure_mode = SnapshotFailureMode(fm_str) if fm_str else None

        return UniverseSnapshot(
            mode=mode,
            trading_date=trading_date,
            approved_symbols=approved,
            decisions=(),       # decisions are not persisted to DynamoDB (too large)
            generated_at=generated_at,
            data_sources_used=frozenset(json.loads(item.get("data_sources", {}).get("S", "[]"))),
            failure_mode=failure_mode,
            failure_details=item.get("failure_details", {}).get("S", ""),
        )

    def exists(self, mode: UniverseMode, trading_date: date) -> bool:
        return self.get(mode, trading_date) is not None


# ── Factory ───────────────────────────────────────────────────────────────────

def build_snapshot_store(
    dynamo_client: object | None = None,
    table_name: str = "qe_universe_snapshot",
) -> SnapshotStore:
    """
    Build the appropriate snapshot store based on available infrastructure.

    Uses DynamoDB if a client is provided; falls back to in-memory otherwise.
    """
    if dynamo_client is not None:
        return DynamoSnapshotStore(dynamo_client=dynamo_client, table_name=table_name)
    logger.warning(
        "snapshot_store.no_dynamo_client — using in-memory store; "
        "snapshots will not survive restart"
    )
    return InMemorySnapshotStore()
