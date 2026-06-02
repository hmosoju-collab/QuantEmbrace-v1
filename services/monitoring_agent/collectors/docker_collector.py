"""Docker collector — container liveness + restart counts, read-only.

Uses the Docker SDK against the daemon socket (mounted **read-only** into the
agent container). It only ever *lists* and *inspects* containers — it never
starts, stops, restarts, kills, or removes anything. Restarting containers is a
Phase 3 ``safe_action`` and is explicitly out of scope here.

Coarse status per watched container:

    * ``running`` (and not health=unhealthy)  → OK
    * ``running`` + health=unhealthy            → DEGRADED
    * ``restarting``                            → DEGRADED
    * anything else (exited/dead/created/paused)→ DOWN

Restart counts are recorded in ``details`` (the Phase 2 detector compares them
against ``restart_warn`` / ``restart_critical`` and against the previous
snapshot). The collector is opt-in via ``rules.docker.enabled``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from monitoring_agent.collectors.base import Collector
from monitoring_agent.snapshot import CollectorResult, Status, worst

logger = logging.getLogger("monitoring_agent.collectors.docker")


def decide_container_status(state: Optional[str], health: Optional[str]) -> Status:
    """Map a container state + health string to a coarse status."""
    state = (state or "").lower()
    health = (health or "").lower()
    if state != "running":
        if state == "restarting":
            return Status.DEGRADED
        return Status.DOWN
    if health == "unhealthy":
        return Status.DEGRADED
    return Status.OK


class DockerCollector(Collector):
    """Observe Docker container liveness + restart counts via the local daemon."""

    name = "docker"

    def enabled(self) -> bool:
        return self.rules.docker.enabled

    def _collect_sync(self) -> CollectorResult:
        try:
            import docker  # lazy: optional dependency
        except Exception as exc:  # noqa: BLE001
            return self._result(Status.UNKNOWN, "docker: SDK not installed", error=repr(exc))

        try:
            client = docker.from_env()
            containers = client.containers.list(all=True)  # read-only listing
        except Exception as exc:  # noqa: BLE001 — socket not mounted / daemon down
            return self._result(
                Status.UNKNOWN,
                "docker: cannot reach Docker daemon (is the socket mounted read-only?)",
                error=repr(exc),
            )

        name_filters = self.rules.docker.name_contains
        watched: list[dict[str, Any]] = []
        statuses: list[Status] = []

        for container in containers:
            name = getattr(container, "name", "?")
            if name_filters and not any(token in name for token in name_filters):
                continue
            try:
                container.reload()  # GET inspect — read-only, populates full attrs
            except Exception:  # noqa: BLE001
                pass
            attrs = getattr(container, "attrs", {}) or {}
            state_obj = attrs.get("State", {}) or {}
            state = state_obj.get("Status") or getattr(container, "status", None)
            health = (state_obj.get("Health") or {}).get("Status")
            restart_count = attrs.get("RestartCount", 0)

            status = decide_container_status(state, health)
            statuses.append(status)
            watched.append(
                {
                    "name": name,
                    "state": state,
                    "health": health,
                    "restart_count": restart_count,
                    "status": status.value,
                    "restart_warn": self.rules.docker.restart_warn,
                    "restart_critical": self.rules.docker.restart_critical,
                }
            )

        if not watched:
            if name_filters:
                return self._result(
                    Status.UNKNOWN,
                    f"docker: no containers match name_contains={list(name_filters)}",
                    details={"containers": []},
                )
            return self._result(Status.OK, "docker: no containers present", details={"containers": []})

        overall = worst(statuses)
        problems = [w["name"] for w in watched if w["status"] in (Status.DOWN.value, Status.DEGRADED.value)]
        summary = f"docker: {len(watched)} watched"
        summary += f"; issues={problems}" if problems else "; all running"
        return self._result(overall, summary, details={"containers": watched})
