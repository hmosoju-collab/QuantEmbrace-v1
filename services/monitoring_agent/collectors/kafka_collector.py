"""Kafka health collector — strictly read-only.

Read-only strategy (this is the whole point — the agent must never disturb the
trading event stream):

    * **Cluster + topics:** ``AdminClient.list_topics(timeout)`` returns broker
      and topic metadata only. No topic is created or altered.
    * **Consumer-group lag for OTHER services' groups:** we measure lag *without
      joining* any group. Committed offsets come from
      ``AdminClient.list_consumer_group_offsets`` (an admin read), and high
      watermarks come from a throwaway ``Consumer`` on which we NEVER call
      ``subscribe``/``poll``/``commit`` — only ``get_watermark_offsets``. The
      consumer is created with ``enable.auto.commit=false`` and closed each cycle.
      This is the same mechanism the platform's own lag monitor uses.

Phase 1 reports *coarse liveness* (reachable / a critical group has no committed
offsets / a configured topic is missing). The raw per-group lag is recorded in
``details`` for the Phase 2 severity engine, which owns the warn/critical lag
thresholds parsed in :mod:`monitoring_agent.rules`.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from monitoring_agent.collectors.base import Collector
from monitoring_agent.snapshot import CollectorResult, Status, worst

logger = logging.getLogger("monitoring_agent.collectors.kafka")

# Throwaway, never-subscribed group id used only so a Consumer can be constructed
# for watermark reads. It never joins a real group and never commits.
_READONLY_GROUP = "monitoring-agent-readonly-watermark"
_DEFAULT_TIMEOUT = 5.0


def compute_group_lag(
    committed: dict[tuple[str, int], int],
    highwater: dict[tuple[str, int], int],
) -> dict[str, Any]:
    """Pure lag math: combine committed offsets with high watermarks.

    A negative committed offset (the confluent sentinel for "no offset stored
    yet") or a missing/negative high watermark is skipped rather than turned into
    a spurious huge lag. Returns total/max lag plus per-partition detail.
    """
    partitions: list[dict[str, Any]] = []
    total_lag = 0
    max_lag = 0
    for tp, committed_offset in committed.items():
        if committed_offset is None or committed_offset < 0:
            continue
        high = highwater.get(tp)
        if high is None or high < 0:
            continue
        lag = max(0, int(high) - int(committed_offset))
        total_lag += lag
        max_lag = max(max_lag, lag)
        partitions.append(
            {
                "topic": tp[0],
                "partition": tp[1],
                "committed": int(committed_offset),
                "high": int(high),
                "lag": lag,
            }
        )
    return {"total_lag": total_lag, "max_lag": max_lag, "partitions": partitions}


class KafkaCollector(Collector):
    """Observe Kafka cluster reachability, topic presence, and consumer lag."""

    name = "kafka"

    def __init__(self, config: Any, rules: Any) -> None:
        super().__init__(config, rules)
        self._timeout = _DEFAULT_TIMEOUT
        self._admin: Any = None  # cached across cycles

    # ── result assembly (pure; unit-testable without confluent-kafka) ──────────

    def _build_result(self, findings: dict[str, Any]) -> CollectorResult:
        if not findings.get("reachable"):
            return self._result(
                Status.DOWN,
                "kafka: cluster unreachable",
                details=findings,
                error=findings.get("error"),
            )

        groups: dict[str, Any] = findings.get("groups", {})
        missing_critical_topics = findings.get("missing_critical_topics") or []
        idle_critical_groups = [
            gid for gid, info in groups.items() if info.get("critical") and not info.get("found")
        ]
        group_errors = [gid for gid, info in groups.items() if info.get("error")]

        status = Status.OK
        notes: list[str] = []
        if missing_critical_topics:
            status = worst([status, Status.DEGRADED])
            notes.append(f"missing topics={missing_critical_topics}")
        if idle_critical_groups:
            status = worst([status, Status.DEGRADED])
            notes.append(f"no committed offsets for critical groups={idle_critical_groups}")
        if group_errors and status == Status.OK:
            status = Status.UNKNOWN
            notes.append(f"offset read errors for groups={group_errors}")

        broker_count = findings.get("broker_count", 0)
        summary = f"kafka: {broker_count} broker(s)"
        summary += "; " + ("; ".join(notes) if notes else "topics + consumer groups healthy")
        return self._result(status, summary, details=findings)

    # ── confluent-kafka access (isolated so it degrades to UNKNOWN cleanly) ────

    def _auth_config(self) -> dict[str, Any]:
        """Reuse the platform's Kafka security config (IAM for MSK, PLAINTEXT local)."""
        from shared.kafka.config import get_kafka_auth_config

        return get_kafka_auth_config(self.config.aws_region)

    def _get_admin(self, admin_cls: Any, auth: dict[str, Any]) -> Any:
        if self._admin is None:
            self._admin = admin_cls({**auth, "bootstrap.servers": self.config.kafka_bootstrap_servers})
        return self._admin

    def _committed_offsets(
        self, admin: Any, group_id: str, cgtp_cls: Any
    ) -> dict[tuple[str, int], int]:
        """Read a consumer group's committed offsets via the admin API (no join)."""
        futmap = admin.list_consumer_group_offsets([cgtp_cls(group_id)])
        out: dict[tuple[str, int], int] = {}
        for _gid, fut in futmap.items():
            res = fut.result(timeout=self._timeout)
            for tp in getattr(res, "topic_partitions", None) or []:
                out[(tp.topic, tp.partition)] = int(getattr(tp, "offset", -1))
        return out

    def _watermarks(
        self, consumer: Any, tps: Iterable[tuple[str, int]], tp_cls: Any
    ) -> dict[tuple[str, int], int]:
        """Read high watermarks per partition (broker query, no assignment/poll)."""
        out: dict[tuple[str, int], int] = {}
        for topic, partition in tps:
            try:
                _low, high = consumer.get_watermark_offsets(
                    tp_cls(topic, partition), timeout=self._timeout, cached=False
                )
                out[(topic, partition)] = int(high)
            except Exception:  # noqa: BLE001 — one bad partition shouldn't fail the group
                out[(topic, partition)] = -1
        return out

    def _collect_sync(self) -> CollectorResult:
        try:
            from confluent_kafka import Consumer, ConsumerGroupTopicPartitions, TopicPartition
            from confluent_kafka.admin import AdminClient
        except Exception as exc:  # noqa: BLE001
            return self._result(Status.UNKNOWN, "kafka: confluent-kafka not available", error=repr(exc))

        if not self.config.kafka_bootstrap_servers:
            return self._result(Status.UNKNOWN, "kafka: no bootstrap servers configured")

        try:
            auth = self._auth_config()
        except Exception as exc:  # noqa: BLE001 — IAM signer missing, etc.
            return self._result(Status.UNKNOWN, "kafka: auth config unavailable", error=repr(exc))

        admin = self._get_admin(AdminClient, auth)
        findings: dict[str, Any] = {
            "reachable": False,
            "broker_count": 0,
            "groups": {},
            "missing_topics": [],
            "missing_critical_topics": [],
        }

        # 1) cluster + topic metadata
        try:
            metadata = admin.list_topics(timeout=self._timeout)
            findings["reachable"] = True
            findings["broker_count"] = len(getattr(metadata, "brokers", {}) or {})
            present = set((getattr(metadata, "topics", {}) or {}).keys())
        except Exception as exc:  # noqa: BLE001
            findings["error"] = repr(exc)
            return self._build_result(findings)

        for topic_rule in self.rules.topics:
            if topic_rule.name not in present:
                findings["missing_topics"].append(topic_rule.name)
                findings["missing_critical_topics"].append(topic_rule.name)

        # 2) consumer-group lag (read-only; never joins a group)
        consumer = None
        try:
            consumer = Consumer(
                {
                    **auth,
                    "bootstrap.servers": self.config.kafka_bootstrap_servers,
                    "group.id": _READONLY_GROUP,
                    "enable.auto.commit": False,
                }
            )
            for grp in self.rules.consumer_groups:
                info: dict[str, Any] = {
                    "critical": grp.critical,
                    "found": False,
                    "total_lag": 0,
                    "max_lag": 0,
                    "partitions": 0,
                    "warn_lag": grp.warn_lag,
                    "critical_lag": grp.critical_lag,
                }
                try:
                    committed = self._committed_offsets(admin, grp.group_id, ConsumerGroupTopicPartitions)
                    if committed:
                        info["found"] = True
                        highwater = self._watermarks(consumer, committed.keys(), TopicPartition)
                        lag = compute_group_lag(committed, highwater)
                        info["total_lag"] = lag["total_lag"]
                        info["max_lag"] = lag["max_lag"]
                        info["partitions"] = len(lag["partitions"])
                        info["partition_detail"] = lag["partitions"]
                    else:
                        # No committed offsets yet — consumer may be running but topics are empty.
                        # Probe group membership so we don't false-alarm before first message.
                        try:
                            gd_futures = admin.describe_consumer_groups([grp.group_id])
                            gd = gd_futures[grp.group_id].result(timeout=self._timeout)
                            state_name = getattr(getattr(gd, "state", None), "name", "Unknown")
                            member_count = len(getattr(gd, "members", []))
                            if state_name in {"Stable", "PreparingRebalance", "CompletingRebalance"} and member_count > 0:
                                info["found"] = True
                                info["group_state"] = state_name
                                info["member_count"] = member_count
                        except Exception:  # noqa: BLE001
                            pass  # stay found=False; original liveness behavior
                except Exception as exc:  # noqa: BLE001
                    info["error"] = repr(exc)
                findings["groups"][grp.group_id] = info
        finally:
            if consumer is not None:
                try:
                    consumer.close()
                except Exception:  # noqa: BLE001
                    pass

        return self._build_result(findings)
