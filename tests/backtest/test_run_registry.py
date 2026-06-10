"""Unit tests for the backtest run registry + checkpoint manager (Phase AWS-BT-3).

Covers: create run · mark running · checkpoint partition · resume from checkpoint ·
mark failed · mark completed · idempotent creation · metadata→S3 pointers ·
live-table prefix rejection.

Backtest-only: an in-memory fake DynamoDB table — no AWS, no broker APIs.

Run:  python -m pytest tests/backtest/test_run_registry.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.checkpoint_manager import CheckpointManager  # noqa: E402
from backtesting.run_registry import (  # noqa: E402
    RUN_FIELDS,
    IllegalTransition,
    RunRegistry,
    RunSpec,
    RunStatus,
)


# ── in-memory fake DynamoDB table (single run_id PK) ────────────────────────────


class ConditionalCheckFailedException(Exception):
    """Mimics boto3's conditional-check failure for the in-memory fake."""


class FakeTable:
    def __init__(self, name: str = "qe-bt-runs") -> None:
        self.name = name
        self._store: dict[str, dict] = {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None, **kwargs):  # noqa: N803
        if ConditionExpression is not None and not self._condition_ok(
            Item["run_id"], ConditionExpression, ExpressionAttributeValues or {}
        ):
            raise ConditionalCheckFailedException(ConditionExpression)
        self._store[Item["run_id"]] = dict(Item)
        return {}

    def get_item(self, Key, **kwargs):  # noqa: N803
        item = self._store.get(Key["run_id"])
        return {"Item": dict(item)} if item is not None else {}

    def scan(self, **kwargs):
        return {"Items": [dict(v) for v in self._store.values()]}

    # Minimal evaluator for exactly the ConditionExpressions the registry emits.
    def _condition_ok(self, key, expr, values):  # noqa: N803
        existing = self._store.get(key)
        if expr == "attribute_not_exists(run_id)":
            return existing is None
        if "record_version" in expr:  # optimistic-lock guard
            if existing is None or "record_version" not in existing:
                return True
            return int(existing.get("record_version", 0)) == int(values.get(":v"))
        raise NotImplementedError(f"FakeTable cannot evaluate: {expr!r}")


class FakeCheckpointTable:
    """Composite-key (run_id, partition_id) fake supporting query-by-run_id."""

    def __init__(self, name: str = "qe-bt-checkpoints") -> None:
        self.name = name
        self._store: dict[tuple, dict] = {}

    def put_item(self, Item, **kwargs):  # noqa: N803
        self._store[(Item["run_id"], Item["partition_id"])] = dict(Item)
        return {}

    def get_item(self, Key, **kwargs):  # noqa: N803
        it = self._store.get((Key["run_id"], Key["partition_id"]))
        return {"Item": dict(it)} if it is not None else {}

    def query(self, KeyConditionExpression=None, ExpressionAttributeValues=None, **kwargs):  # noqa: N803
        rid = (ExpressionAttributeValues or {}).get(":rid")
        return {"Items": [dict(v) for (pk, _sk), v in self._store.items() if pk == rid]}


def make_spec(**over) -> RunSpec:
    base = dict(
        strategy="momentum",
        symbols=["RELIANCE", "TCS"],
        timeframe="1d",
        start_date="2010-01-01",
        end_date="2024-12-31",
        config_s3_path="s3://quantembrace-backtest-results/runs/cfg.json",
        code_version="abc123",
        data_version="snap-2024-12",
        operator="hari",
        trust_level="HIGH",
        cost_model_version="v1",
        exit_policy_version="v2",
    )
    base.update(over)
    return RunSpec(**base)


def _registry() -> RunRegistry:
    return RunRegistry(FakeTable("qe-bt-runs"))


def _checkpoints() -> CheckpointManager:
    return CheckpointManager(FakeCheckpointTable("qe-bt-checkpoints"))


# ── tests ──────────────────────────────────────────────────────────────────────


def test_create_run():
    reg = _registry()
    rec = reg.create_run(make_spec())
    assert rec.status == RunStatus.CREATED.value
    assert rec.run_id.startswith("bt_")
    assert reg.get_run(rec.run_id) is not None
    assert rec.strategy == "momentum"
    assert rec.trust_level == "HIGH"


def test_mark_running():
    reg = _registry()
    rid = reg.create_run(make_spec()).run_id
    rec = reg.mark_running(rid)
    assert rec.status == RunStatus.RUNNING.value
    assert rec.started_at is not None


def test_checkpoint_partition():
    cm = _checkpoints()
    rid = "bt_demo"
    cm.init_checkpoint(rid, resumable_command="python ... --resume bt_demo")
    cm.checkpoint_partition(
        rid,
        "RELIANCE#2020",
        last_processed_timestamp="2020-12-31T15:30:00+05:30",
        partial_metrics={"trades": 12, "pnl": 1500.5},
        partial_output_s3_path="s3://quantembrace-backtest-results/runs/bt_demo/checkpoints/RELIANCE_2020.parquet",
    )
    rec = cm.get_checkpoint(rid)
    assert rec is not None
    assert "RELIANCE#2020" in rec.completed_partitions
    assert rec.partition_cursors["RELIANCE#2020"] == "2020-12-31T15:30:00+05:30"
    assert rec.last_processed_timestamp == "2020-12-31T15:30:00+05:30"
    assert rec.partial_metrics["pnl"] == 1500.5
    assert rec.partial_output_s3_path.startswith("s3://")


def test_resume_from_checkpoint():
    cm = _checkpoints()
    rid = "bt_resume"
    all_parts = ["A#2020", "A#2021", "B#2020", "B#2021"]
    cm.init_checkpoint(rid)
    cm.checkpoint_partition(rid, "A#2020", last_processed_timestamp="2020-12-31T15:30:00+05:30")
    cm.checkpoint_partition(rid, "A#2021", last_processed_timestamp="2021-12-31T15:30:00+05:30")
    pending = cm.pending_partitions(rid, all_parts)
    assert pending == ["B#2020", "B#2021"]
    assert cm.completed_partitions(rid) == {"A#2020", "A#2021"}


def test_two_workers_same_run_distinct_shards_no_clobber():
    # Per-shard items: two workers completing different shards both persist.
    # The old single-item design would have lost one to last-writer-wins.
    table = FakeCheckpointTable("qe-bt-checkpoints")
    worker_a = CheckpointManager(table)
    worker_b = CheckpointManager(table)
    worker_a.init_checkpoint("r")
    worker_a.checkpoint_partition("r", "A#2020", last_processed_timestamp="2020-12-31T15:30:00+05:30")
    worker_b.checkpoint_partition("r", "B#2020", last_processed_timestamp="2020-12-31T15:30:00+05:30")
    assert worker_a.completed_partitions("r") == {"A#2020", "B#2020"}


def test_many_distinct_shard_completions_all_recorded():
    cm = _checkpoints()
    rid = "bt_fleet"
    cm.init_checkpoint(rid)
    shards = [f"SYM{i}#2020" for i in range(25)]
    for s in shards:
        cm.checkpoint_partition(rid, s, last_processed_timestamp="2020-12-31T15:30:00+05:30")
    assert cm.completed_partitions(rid) == set(shards)


def test_fail_then_succeed_clears_failure():
    cm = _checkpoints()
    rid = "bt_retry"
    cm.init_checkpoint(rid)
    cm.fail_partition(rid, "A#2020", "worker OOM")
    rec = cm.get_checkpoint(rid)
    assert "A#2020" in rec.failed_partitions
    assert "A#2020" not in rec.completed_partitions
    cm.checkpoint_partition(rid, "A#2020", last_processed_timestamp="2020-12-31T15:30:00+05:30")
    rec2 = cm.get_checkpoint(rid)
    assert "A#2020" in rec2.completed_partitions
    assert "A#2020" not in rec2.failed_partitions


def test_mark_failed():
    reg = _registry()
    rid = reg.create_run(make_spec()).run_id
    reg.mark_running(rid)
    rec = reg.mark_failed(rid, "worker OOM on B#2018")
    assert rec.status == RunStatus.FAILED.value
    assert rec.error_reason == "worker OOM on B#2018"
    assert rec.completed_at is not None


def test_mark_completed():
    reg = _registry()
    rid = reg.create_run(make_spec()).run_id
    reg.mark_running(rid)
    rec = reg.mark_completed(
        rid, result_s3_path="s3://quantembrace-backtest-results/runs/x/metrics.json"
    )
    assert rec.status == RunStatus.COMPLETED.value
    assert rec.completed_at is not None
    assert rec.result_s3_path.startswith("s3://")


def test_idempotent_run_creation():
    table = FakeTable("qe-bt-runs")
    reg = RunRegistry(table)
    spec = make_spec()
    r1 = reg.create_run(spec)
    r2 = reg.create_run(spec)  # same config ⇒ same run, no duplicate
    assert r1.run_id == r2.run_id
    assert len(table.scan()["Items"]) == 1


def test_metadata_points_to_s3_outputs():
    table = FakeTable("qe-bt-runs")
    reg = RunRegistry(table)
    rec = reg.create_run(make_spec())
    # All large outputs are referenced by S3 path, not stored inline.
    assert rec.config_s3_path.startswith("s3://")
    assert rec.result_s3_path.startswith("s3://")
    assert rec.checkpoint_s3_path.startswith("s3://")
    # Stored item is metadata only — no payload fields beyond the schema.
    stored = table.get_item(Key={"run_id": rec.run_id})["Item"]
    assert set(stored.keys()) <= set(RUN_FIELDS)
    assert "trades" not in stored and "equity_curve" not in stored


def test_live_table_prefix_is_rejected():
    # Constructing against a live/paper table must fail closed.
    with pytest.raises(ValueError):
        RunRegistry(FakeTable("quantembrace-prod-orders"))
    with pytest.raises(ValueError):
        RunRegistry(FakeTable("qe-prod-positions"))
    with pytest.raises(ValueError):
        CheckpointManager(FakeTable("quantembrace-development-risk-state"))
    # from_aws guards before any AWS call.
    with pytest.raises(ValueError):
        RunRegistry.from_aws("quantembrace-prod-orders")
    # A non-backtest, non-live name is also rejected (safe default).
    with pytest.raises(ValueError):
        RunRegistry(FakeTable("some-random-table"))
    # A valid backtest table is accepted.
    assert RunRegistry(FakeTable("qe-bt-runs")) is not None
    assert CheckpointManager(FakeTable("quantembrace-backtest-checkpoints")) is not None


# ── concurrency / optimistic locking (fleet-scale) ──────────────────────────────


def test_record_version_increments_each_update():
    reg = _registry()
    rid = reg.create_run(make_spec()).run_id
    assert reg.get_run(rid).record_version == 0
    assert reg.mark_running(rid).record_version == 1
    assert reg.mark_completed(rid).record_version == 2


def test_fake_table_rejects_stale_version_put():
    # White-box: the fake honors the optimistic-lock condition the registry relies on.
    cond = (
        "attribute_not_exists(run_id) OR attribute_not_exists(record_version) "
        "OR record_version = :v"
    )
    t = FakeTable("qe-bt-runs")
    t.put_item(Item={"run_id": "r", "record_version": 0})
    t.put_item(
        Item={"run_id": "r", "record_version": 1},
        ConditionExpression=cond,
        ExpressionAttributeValues={":v": 0},
    )  # expected 0 ⇒ ok, now at 1
    with pytest.raises(ConditionalCheckFailedException):
        t.put_item(
            Item={"run_id": "r", "record_version": 2},
            ConditionExpression=cond,
            ExpressionAttributeValues={":v": 0},
        )  # stale expected 0 (now 1) ⇒ rejected


def test_terminal_run_cannot_be_resurrected():
    reg = _registry()
    rid = reg.create_run(make_spec()).run_id
    reg.mark_completed(rid)
    with pytest.raises(IllegalTransition):
        reg.mark_running(rid)
    assert reg.get_run(rid).status == RunStatus.COMPLETED.value


def test_set_result_paths_does_not_regress_terminal_status():
    reg = _registry()
    rid = reg.create_run(make_spec()).run_id
    reg.mark_completed(rid)
    rec = reg.set_result_paths(
        rid, result_s3_path="s3://quantembrace-backtest-results/runs/x/"
    )
    assert rec.status == RunStatus.COMPLETED.value
    assert rec.result_s3_path.endswith("/runs/x/")


def test_update_retries_then_succeeds_on_version_conflict():
    # Inject one transient conditional failure on the optimistic-lock put; the
    # registry must re-read and retry, not lose the update.
    class FlakyTable(FakeTable):
        def __init__(self):
            super().__init__("qe-bt-runs")
            self.fail_next_versioned_put = False

        def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None, **kwargs):  # noqa: N803
            if (
                self.fail_next_versioned_put
                and ConditionExpression
                and "record_version" in ConditionExpression
            ):
                self.fail_next_versioned_put = False
                raise ConditionalCheckFailedException("injected conflict")
            return super().put_item(
                Item,
                ConditionExpression=ConditionExpression,
                ExpressionAttributeValues=ExpressionAttributeValues,
                **kwargs,
            )

    t = FlakyTable()
    reg = RunRegistry(t)
    rid = reg.create_run(make_spec()).run_id
    t.fail_next_versioned_put = True
    rec = reg.mark_running(rid)  # 1st versioned put fails → retry → wins
    assert rec.status == RunStatus.RUNNING.value
    assert rec.record_version == 1
    assert t.fail_next_versioned_put is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
