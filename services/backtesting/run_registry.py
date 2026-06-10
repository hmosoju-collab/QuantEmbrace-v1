"""Durable backtest run registry (DynamoDB metadata) for the QuantEmbrace lab.

Tracks every backtest run's lifecycle so 10–15-year runs can be resumed, audited,
and reproduced. **DynamoDB stores metadata only**; large outputs (trades, equity
curves, partial results) live in S3 and are referenced here by path.

Hard safety (backtest-only):
    * Table name MUST be a backtest table (``qe-bt-*`` / contains ``backtest``).
      Live/paper trading tables (orders, positions, risk-state, …) are **rejected**
      at construction — this registry can never mutate a trading-runtime table.
    * No broker APIs, no live trading.

DynamoDB access goes through the sanctioned ``shared.aws.clients`` factory or an
injected table (tests / LocalStack) — never a raw ``boto3.client`` (TID251).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

# ── live-table guard ────────────────────────────────────────────────────────────

# Trading-runtime tables (suffixes) the lab must never touch. Mirrors the 13
# live/paper tables in infra/terraform/modules/dynamodb.
_FORBIDDEN_LIVE_TABLES: frozenset[str] = frozenset(
    {
        "orders",
        "positions",
        "latest-prices",
        "risk-state",
        "sessions",
        "candle-cache",
        "strategy-config",
        "strategy-state",
        "signal-inbox",
        "signal-outbox",
        "features",
        "regime-log",
        "strategy-recommendations",
        "kill-switch",
        "instrument-registry",
    }
)

# A backtest table must carry one of these markers.
_BACKTEST_MARKERS: tuple[str, ...] = ("qe-bt-", "backtest")


def assert_backtest_table(name: str) -> None:
    """Raise ``ValueError`` unless ``name`` is a backtest table (not a live one)."""
    if not name:
        raise ValueError("Table name is required.")
    n = name.strip().lower()
    for live in _FORBIDDEN_LIVE_TABLES:
        if n == live or n.endswith(f"-{live}") or n.endswith(f"_{live}"):
            raise ValueError(
                f"Refusing to use live/paper trading table {name!r}. "
                "The backtest lab may only use qe-bt-* tables (metadata only)."
            )
    if not any(marker in n for marker in _BACKTEST_MARKERS):
        raise ValueError(
            f"Table {name!r} is not a recognised backtest table. "
            "Name must start with 'qe-bt-' or contain 'backtest'."
        )


# ── model ────────────────────────────────────────────────────────────────────


class RunStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


# Registry fields persisted to DynamoDB (all metadata; no large payloads).
RUN_FIELDS: tuple[str, ...] = (
    "run_id",
    "status",
    "strategy",
    "symbols",
    "timeframe",
    "start_date",
    "end_date",
    "config_s3_path",
    "result_s3_path",
    "checkpoint_s3_path",
    "code_version",
    "data_version",
    "started_at",
    "updated_at",
    "completed_at",
    "error_reason",
    "operator",
    "trust_level",
    "cost_model_version",
    "exit_policy_version",
    "config_hash",
    "record_version",
)

DEFAULT_RESULTS_BASE = "s3://quantembrace-backtest-results"


@dataclass
class RunSpec:
    """Inputs that define a run (and its deterministic ``run_id``)."""

    strategy: str
    symbols: list[str]
    timeframe: str
    start_date: date | str
    end_date: date | str
    config_s3_path: str
    code_version: str = "unknown"
    data_version: str = "unknown"
    operator: str = "unknown"
    trust_level: str = "HIGH"
    cost_model_version: str = "unknown"
    exit_policy_version: str = "unknown"
    result_s3_path: str | None = None
    checkpoint_s3_path: str | None = None

    def config_hash(self) -> str:
        payload = json.dumps(
            {
                "strategy": self.strategy,
                "symbols": sorted(self.symbols),
                "timeframe": self.timeframe,
                "start_date": str(self.start_date),
                "end_date": str(self.end_date),
                "code_version": self.code_version,
                "data_version": self.data_version,
                "cost_model_version": self.cost_model_version,
                "exit_policy_version": self.exit_policy_version,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def run_id(self) -> str:
        return f"bt_{self.config_hash()}"


@dataclass
class RunRecord:
    run_id: str
    status: str
    strategy: str = ""
    symbols: list[str] = field(default_factory=list)
    timeframe: str = ""
    start_date: str = ""
    end_date: str = ""
    config_s3_path: str = ""
    result_s3_path: str = ""
    checkpoint_s3_path: str = ""
    code_version: str = ""
    data_version: str = ""
    started_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
    error_reason: str | None = None
    operator: str = ""
    trust_level: str = ""
    cost_model_version: str = ""
    exit_policy_version: str = ""
    config_hash: str = ""
    record_version: int = 0

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> RunRecord:
        return cls(**{k: item.get(k) for k in RUN_FIELDS if k in item})

    def to_item(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in RUN_FIELDS}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── concurrency control ───────────────────────────────────────────────────────

# Terminal lifecycle states. A non-terminal update (e.g. mark_running) must never
# resurrect a run that has already finished.
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}
)

# Bounded optimistic-concurrency retries for read-modify-write under fleet workers.
_MAX_UPDATE_RETRIES = 6


class OptimisticLockError(RuntimeError):
    """A run record kept losing the optimistic-version race after max retries."""


class IllegalTransition(ValueError):
    """Refused a lifecycle transition that would regress a terminal run."""


def _is_conditional_failure(exc: Exception) -> bool:
    """True for a DynamoDB ConditionalCheckFailed (boto3 ClientError or test fake)."""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        if resp.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return True
    return type(exc).__name__ == "ConditionalCheckFailedException"


# ── registry ──────────────────────────────────────────────────────────────────


class RunRegistry:
    """CRUD + lifecycle over the ``qe-bt-runs`` DynamoDB table (metadata only)."""

    def __init__(self, table: Any, *, results_base: str = DEFAULT_RESULTS_BASE) -> None:
        name = getattr(table, "name", "")
        assert_backtest_table(name)
        self._table = table
        self._results_base = results_base.rstrip("/")

    @classmethod
    def from_aws(cls, table_name: str, *, dynamodb: Any = None, **kw: Any) -> RunRegistry:
        assert_backtest_table(table_name)  # fail before touching AWS
        if dynamodb is None:
            from shared.aws.clients import get_dynamodb_resource

            dynamodb = get_dynamodb_resource()
        return cls(dynamodb.Table(table_name), **kw)

    # ── reads ─────────────────────────────────────────────────────────────────
    def get_run(self, run_id: str, *, consistent: bool = False) -> RunRecord | None:
        resp = self._table.get_item(Key={"run_id": run_id}, ConsistentRead=consistent)
        item = resp.get("Item")
        return RunRecord.from_item(item) if item else None

    def list_runs(self, *, status: str | None = None) -> list[RunRecord]:
        items = self._table.scan().get("Items", [])
        records = [RunRecord.from_item(i) for i in items]
        if status:
            records = [r for r in records if r.status == status]
        return records

    # ── create (idempotent) ─────────────────────────────────────────────────────
    def create_run(self, spec: RunSpec, *, run_id: str | None = None) -> RunRecord:
        """Create a run, or return the existing one for the same config (idempotent)."""
        if not _looks_like_path(spec.config_s3_path):
            raise ValueError(
                f"config_s3_path must be an S3 URI or path (metadata only), got {spec.config_s3_path!r}"
            )
        rid = run_id or spec.run_id()
        existing = self.get_run(rid)
        if existing is not None:
            return existing  # idempotent: same config ⇒ same run

        now = _utcnow()
        rec = RunRecord(
            run_id=rid,
            status=RunStatus.CREATED.value,
            strategy=spec.strategy,
            symbols=list(spec.symbols),
            timeframe=spec.timeframe,
            start_date=str(spec.start_date),
            end_date=str(spec.end_date),
            config_s3_path=spec.config_s3_path,
            result_s3_path=spec.result_s3_path or f"{self._results_base}/runs/{rid}/",
            checkpoint_s3_path=spec.checkpoint_s3_path
            or f"{self._results_base}/runs/{rid}/checkpoints/",
            code_version=spec.code_version,
            data_version=spec.data_version,
            started_at=None,
            updated_at=now,
            completed_at=None,
            error_reason=None,
            operator=spec.operator,
            trust_level=spec.trust_level,
            cost_model_version=spec.cost_model_version,
            exit_policy_version=spec.exit_policy_version,
            config_hash=spec.config_hash(),
        )
        # ConditionExpression guards against a concurrent create on real AWS;
        # the get-first check above makes it idempotent for both AWS and tests.
        try:
            self._table.put_item(
                Item=rec.to_item(),
                ConditionExpression="attribute_not_exists(run_id)",
            )
        except Exception as exc:  # ConditionalCheckFailed on a race → return the winner
            if not _is_conditional_failure(exc):
                raise
            existing = self.get_run(rid, consistent=True)
            if existing is not None:
                return existing
            raise
        return rec

    # ── lifecycle transitions ───────────────────────────────────────────────────
    def mark_running(self, run_id: str) -> RunRecord:
        changes: dict[str, Any] = {"status": RunStatus.RUNNING.value}
        rec = self.get_run(run_id)
        if rec is not None and not rec.started_at:
            changes["started_at"] = _utcnow()
        return self._update(run_id, changes)

    def mark_completed(
        self,
        run_id: str,
        *,
        result_s3_path: str | None = None,
    ) -> RunRecord:
        changes: dict[str, Any] = {
            "status": RunStatus.COMPLETED.value,
            "completed_at": _utcnow(),
        }
        if result_s3_path is not None:
            if not _looks_like_path(result_s3_path):
                raise ValueError("result_s3_path must be an S3 URI/path (metadata only).")
            changes["result_s3_path"] = result_s3_path
        return self._update(run_id, changes)

    def mark_failed(self, run_id: str, reason: str) -> RunRecord:
        return self._update(
            run_id,
            {
                "status": RunStatus.FAILED.value,
                "error_reason": reason,
                "completed_at": _utcnow(),
            },
        )

    def mark_cancelled(self, run_id: str, reason: str | None = None) -> RunRecord:
        return self._update(
            run_id,
            {
                "status": RunStatus.CANCELLED.value,
                "error_reason": reason,
                "completed_at": _utcnow(),
            },
        )

    def set_result_paths(
        self, run_id: str, *, result_s3_path: str | None = None, checkpoint_s3_path: str | None = None
    ) -> RunRecord:
        changes: dict[str, Any] = {}
        for key, val in (("result_s3_path", result_s3_path), ("checkpoint_s3_path", checkpoint_s3_path)):
            if val is not None:
                if not _looks_like_path(val):
                    raise ValueError(f"{key} must be an S3 URI/path (metadata only).")
                changes[key] = val
        return self._update(run_id, changes)

    # ── internal read-modify-write (optimistic concurrency; items are tiny) ───────
    def _update(self, run_id: str, changes: dict[str, Any]) -> RunRecord:
        """Apply ``changes`` under an optimistic ``record_version`` lock.

        A conditional ``put_item`` guards against concurrent fleet writers: on a
        version conflict we re-read the latest record and re-apply the changes, so
        no concurrent update is silently lost (no stale full-item clobber). A
        non-terminal status change can never overwrite a terminal run.
        """
        last_exc: Exception | None = None
        for _ in range(_MAX_UPDATE_RETRIES):
            rec = self.get_run(run_id, consistent=True)
            if rec is None:
                raise KeyError(f"Run not found: {run_id}")

            new_status = changes.get("status")
            if (
                new_status is not None
                and rec.status in _TERMINAL_STATUSES
                and new_status not in _TERMINAL_STATUSES
            ):
                raise IllegalTransition(
                    f"Refusing to move run {run_id} from terminal {rec.status!r} "
                    f"to {new_status!r}."
                )

            expected = int(rec.record_version or 0)
            for k, v in changes.items():
                setattr(rec, k, v)
            rec.updated_at = _utcnow()
            rec.record_version = expected + 1
            try:
                self._table.put_item(
                    Item=rec.to_item(),
                    ConditionExpression=(
                        "attribute_not_exists(run_id) "
                        "OR attribute_not_exists(record_version) "
                        "OR record_version = :v"
                    ),
                    ExpressionAttributeValues={":v": expected},
                )
                return rec
            except Exception as exc:  # version conflict → re-read latest and retry
                if not _is_conditional_failure(exc):
                    raise
                last_exc = exc
        raise OptimisticLockError(
            f"run {run_id!r} lost {_MAX_UPDATE_RETRIES} optimistic-version races"
        ) from last_exc


def _looks_like_path(value: str) -> bool:
    """Accept an S3 URI or a filesystem-style path; reject inline blobs."""
    if not isinstance(value, str) or not value:
        return False
    return value.startswith("s3://") or "/" in value
