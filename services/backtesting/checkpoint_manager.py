"""Checkpoint manager for resumable QuantEmbrace backtests (per-shard, fleet-safe).

A long (10-15-year, multi-symbol) backtest is sharded into ``partition`` units
(e.g. ``symbol#year``). Each worker checkpoints **its own** partition as a
distinct DynamoDB item, so concurrent fleet workers never contend on a shared
item — no read-modify-write clobber and no hot key.

Key schema (``qe-bt-checkpoints``): composite ``PK = run_id``, ``SK = partition_id``.
A run-level meta item uses the sentinel ``SK = "#RUN"`` (resumable command, etc.).
Resume reads every shard via ``Query(run_id)``; completed = shards marked DONE.

**DynamoDB stores metadata only** — per-partition cursors, small partial metrics,
failure reasons, retry counts, and a pointer to large partial output in S3
(``partial_output_s3_path``). Large partial outputs live in S3, never inline.

Backtest-only. The table-name guard (shared with the run registry) rejects any
live/paper trading table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from backtesting.run_registry import assert_backtest_table

# Sentinel sort key for the run-level meta item (resumable command, etc.).
_RUN_META_SK = "#RUN"

# Per-shard status markers.
_STATUS_DONE = "DONE"
_STATUS_FAILED = "FAILED"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CheckpointRecord:
    """Aggregate read-model assembled from all per-shard items of a run.

    Returned by ``get_checkpoint`` for back-compatible reads. Writes always go to
    individual shard / meta items — never to this aggregate — so concurrent
    workers cannot clobber one another.
    """

    run_id: str
    completed_partitions: list[str] = field(default_factory=list)
    failed_partitions: dict[str, Any] = field(default_factory=dict)
    partition_cursors: dict[str, str] = field(default_factory=dict)
    partition_outputs: dict[str, str] = field(default_factory=dict)
    partial_metrics: dict[str, Any] = field(default_factory=dict)
    last_processed_timestamp: str | None = None
    partial_output_s3_path: str | None = None
    resumable_command: str | None = None
    retry_count: int = 0
    updated_at: str | None = None


class CheckpointManager:
    """Per-shard checkpoints over the composite-key ``qe-bt-checkpoints`` table.

    Schema: ``PK = run_id``, ``SK = partition_id`` (``"#RUN"`` for run meta).
    """

    def __init__(self, table: Any) -> None:
        assert_backtest_table(getattr(table, "name", ""))
        self._table = table

    @classmethod
    def from_aws(cls, table_name: str, *, dynamodb: Any = None) -> CheckpointManager:
        assert_backtest_table(table_name)
        if dynamodb is None:
            from shared.aws.clients import get_dynamodb_resource

            dynamodb = get_dynamodb_resource()
        return cls(dynamodb.Table(table_name))

    # ── reads ─────────────────────────────────────────────────────────────────
    def _query_items(self, run_id: str) -> list[dict[str, Any]]:
        """All items for a run (paginated). String key-condition: no boto3 import."""
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "run_id = :rid",
            "ExpressionAttributeValues": {":rid": run_id},
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        return items

    def _get_shard(self, run_id: str, partition_id: str) -> dict[str, Any] | None:
        resp = self._table.get_item(Key={"run_id": run_id, "partition_id": partition_id})
        return resp.get("Item")

    def get_checkpoint(self, run_id: str) -> CheckpointRecord | None:
        """Aggregate read-model assembled from all of a run's shard/meta items."""
        items = self._query_items(run_id)
        if not items:
            return None
        rec = CheckpointRecord(run_id=run_id)
        latest_updated: str | None = None
        for it in items:
            sk = it.get("partition_id")
            updated = it.get("updated_at")
            if updated and (latest_updated is None or updated > latest_updated):
                latest_updated = updated
            if sk == _RUN_META_SK:
                rec.resumable_command = it.get("resumable_command")
                continue

            cursor = it.get("last_processed_timestamp")
            if cursor:
                rec.partition_cursors[sk] = cursor
                if rec.last_processed_timestamp is None or cursor > rec.last_processed_timestamp:
                    rec.last_processed_timestamp = cursor

            raw_metrics = it.get("partial_metrics")
            if raw_metrics:
                parsed = json.loads(raw_metrics) if isinstance(raw_metrics, str) else raw_metrics
                if parsed:
                    rec.partial_metrics.update(parsed)

            out = it.get("partial_output_s3_path")
            if out:
                rec.partition_outputs[sk] = out
                rec.partial_output_s3_path = out  # back-compat scalar (any shard's output)

            rec.retry_count += int(it.get("retry_count") or 0)

            status = it.get("status")
            if status == _STATUS_DONE:
                rec.completed_partitions.append(sk)
            elif status == _STATUS_FAILED:
                rec.failed_partitions[sk] = {
                    "reason": it.get("reason"),
                    "retry_count": int(it.get("retry_count") or 0),
                    "at": updated,
                }
        rec.completed_partitions.sort()
        rec.updated_at = latest_updated
        return rec

    def completed_partitions(self, run_id: str) -> set[str]:
        rec = self.get_checkpoint(run_id)
        return set(rec.completed_partitions) if rec else set()

    def pending_partitions(self, run_id: str, all_partitions: list[str]) -> list[str]:
        """Partitions still to run — the resume work list."""
        done = self.completed_partitions(run_id)
        return [p for p in all_partitions if p not in done]

    # ── writes (each targets a single-owner item → no cross-worker contention) ──
    def init_checkpoint(
        self, run_id: str, *, resumable_command: str | None = None
    ) -> CheckpointRecord:
        """Create the run-meta item if absent (idempotent)."""
        if self._get_shard(run_id, _RUN_META_SK) is None:
            self._table.put_item(
                Item={
                    "run_id": run_id,
                    "partition_id": _RUN_META_SK,
                    "resumable_command": resumable_command,
                    "updated_at": _utcnow(),
                }
            )
        return self.get_checkpoint(run_id)

    def checkpoint_partition(
        self,
        run_id: str,
        partition_id: str,
        *,
        last_processed_timestamp: str,
        partial_metrics: dict[str, Any] | None = None,
        partial_output_s3_path: str | None = None,
    ) -> CheckpointRecord:
        """Mark one partition DONE by writing **its own** item (no shared RMW).

        Idempotent by ``(run_id, partition_id)``: re-completing a partition simply
        re-writes its DONE item. A success also clears any prior FAILED state for
        the partition (the item is replaced).
        """
        if partial_output_s3_path is not None and not _looks_like_path(partial_output_s3_path):
            raise ValueError("partial_output_s3_path must be an S3 URI/path (no inline blobs).")
        self._table.put_item(
            Item={
                "run_id": run_id,
                "partition_id": partition_id,
                "status": _STATUS_DONE,
                "last_processed_timestamp": last_processed_timestamp,
                "partial_output_s3_path": partial_output_s3_path,
                "partial_metrics": json.dumps(partial_metrics or {}),
                "retry_count": 0,
                "reason": None,
                "updated_at": _utcnow(),
            }
        )
        return self.get_checkpoint(run_id)

    def fail_partition(
        self, run_id: str, partition_id: str, reason: str, *, increment_retry: bool = True
    ) -> CheckpointRecord:
        """Mark one partition FAILED on its own item; retry_count is per-shard.

        A partition has a single owning worker at a time, so the read-then-write of
        the shard's own item is race-free with respect to other shards.
        """
        prev = self._get_shard(run_id, partition_id) or {}
        prev_retries = int(prev.get("retry_count") or 0)
        self._table.put_item(
            Item={
                "run_id": run_id,
                "partition_id": partition_id,
                "status": _STATUS_FAILED,
                "last_processed_timestamp": prev.get("last_processed_timestamp"),
                "partial_output_s3_path": prev.get("partial_output_s3_path"),
                "partial_metrics": prev.get("partial_metrics") or json.dumps({}),
                "retry_count": prev_retries + (1 if increment_retry else 0),
                "reason": reason,
                "updated_at": _utcnow(),
            }
        )
        return self.get_checkpoint(run_id)

    def set_resumable_command(self, run_id: str, command: str) -> CheckpointRecord:
        self._table.put_item(
            Item={
                "run_id": run_id,
                "partition_id": _RUN_META_SK,
                "resumable_command": command,
                "updated_at": _utcnow(),
            }
        )
        return self.get_checkpoint(run_id)


def _looks_like_path(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return value.startswith("s3://") or "/" in value
