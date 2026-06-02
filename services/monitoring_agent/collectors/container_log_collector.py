"""Container log collector — scans recent Docker container stdout/stderr, read-only.

Uses the Docker SDK (already a dependency for docker_collector) to stream the
last ``lookback_seconds`` of stdout/stderr from each service container and scans
the raw text for configurable pattern groups:

    blocker_patterns   → coarse Status.DOWN   (e.g. kill_switch.activated)
    critical_patterns  → coarse Status.DEGRADED + CRITICAL severity
    warning_patterns   → coarse Status.DEGRADED + WARNING severity

Config block (rules.yaml ``container_logs:``):

    container_logs:
      enabled: true
      lookback_seconds: 90          # overlap with 30s poll; catches bursts
      name_contains:                 # filter by container name fragment
        - risk_engine
        - execution_engine
        - ...
      blocker_patterns: [...]
      critical_patterns: [...]
      warning_patterns: [...]
      max_lines_per_container: 2000  # cap to avoid slow cycles on chatty containers
      sample_lines: 3               # how many matching lines to surface per container

Opt-in: disabled when section absent or ``enabled: false``.
Read-only: container.logs() is a non-mutating Docker API call.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from monitoring_agent.collectors.base import Collector
from monitoring_agent.snapshot import CollectorResult, Status

logger = logging.getLogger("monitoring_agent.collectors.container_logs")

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "lookback_seconds": 90,
    "name_contains": [],
    "blocker_patterns": [],
    "critical_patterns": [],
    "warning_patterns": [],
    "max_lines_per_container": 2000,
    "sample_lines": 3,
}


def _cfg(rules: Any) -> dict[str, Any]:
    raw = rules.raw.get("container_logs") if isinstance(rules.raw, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    merged = dict(_DEFAULTS)
    for key, default in _DEFAULTS.items():
        if key not in raw:
            continue
        val = raw[key]
        if isinstance(default, bool):
            merged[key] = bool(val)
        elif isinstance(default, int):
            try:
                merged[key] = int(val)
            except (TypeError, ValueError):
                pass
        elif isinstance(default, list):
            merged[key] = list(val) if isinstance(val, list) else ([val] if val else [])
        else:
            merged[key] = val
    return merged


def _scan(text: str, patterns: list[str]) -> tuple[int, list[str]]:
    """Count lines matching any pattern; return (count, sample_lines)."""
    count = 0
    samples: list[str] = []
    for line in text.splitlines():
        for pat in patterns:
            if pat in line:
                count += 1
                samples.append(line.strip()[:200])
                break
    return count, samples


class ContainerLogCollector(Collector):
    """Fetch recent container stdout/stderr via Docker SDK and scan for anomaly patterns."""

    name = "container_logs"

    def enabled(self) -> bool:
        return bool(_cfg(self.rules).get("enabled", False))

    def _collect_sync(self) -> CollectorResult:
        try:
            import docker
        except Exception as exc:  # noqa: BLE001
            return self._result(Status.UNKNOWN, "container_logs: docker SDK not installed", error=repr(exc))

        cfg = _cfg(self.rules)
        lookback = int(cfg["lookback_seconds"])
        name_filters: list[str] = cfg["name_contains"]
        blocker_pats: list[str] = cfg["blocker_patterns"]
        critical_pats: list[str] = cfg["critical_patterns"]
        warning_pats: list[str] = cfg["warning_patterns"]
        max_lines: int = int(cfg["max_lines_per_container"])
        sample_cap: int = int(cfg["sample_lines"])

        try:
            client = docker.from_env()
            containers = client.containers.list()
        except Exception as exc:  # noqa: BLE001
            return self._result(
                Status.UNKNOWN,
                "container_logs: cannot reach Docker daemon",
                error=repr(exc),
            )

        since_dt = datetime.now(timezone.utc) - timedelta(seconds=lookback)

        per_container: list[dict[str, Any]] = []
        worst_status = Status.OK
        all_blocker_samples: list[str] = []
        all_critical_samples: list[str] = []
        all_warning_samples: list[str] = []

        for container in containers:
            name = getattr(container, "name", "?")
            if name_filters and not any(f in name for f in name_filters):
                continue

            try:
                raw_bytes: bytes = container.logs(since=since_dt, stream=False, timestamps=False)
                text = raw_bytes.decode("utf-8", errors="replace")
                # Tail to cap so a very chatty container doesn't slow the cycle.
                lines = text.splitlines()
                if len(lines) > max_lines:
                    lines = lines[-max_lines:]
                text = "\n".join(lines)
            except Exception as exc:  # noqa: BLE001
                per_container.append({"name": name, "status": "unreadable", "error": str(exc)[:120]})
                continue

            b_count, b_samples = _scan(text, blocker_pats)
            c_count, c_samples = _scan(text, critical_pats)
            w_count, w_samples = _scan(text, warning_pats)

            entry: dict[str, Any] = {
                "name": name,
                "lines_scanned": len(lines),
                "blocker_hits": b_count,
                "critical_hits": c_count,
                "warning_hits": w_count,
                "status": "ok",
            }

            if b_count:
                entry["status"] = "blocker"
                worst_status = Status.DOWN
                all_blocker_samples.extend(b_samples[:sample_cap])
            elif c_count:
                entry["status"] = "critical"
                if worst_status == Status.OK:
                    worst_status = Status.DEGRADED
                all_critical_samples.extend(c_samples[:sample_cap])
            elif w_count:
                entry["status"] = "warning"
                if worst_status == Status.OK:
                    worst_status = Status.DEGRADED
                all_warning_samples.extend(w_samples[:sample_cap])

            per_container.append(entry)

        total_blocker = sum(e.get("blocker_hits", 0) for e in per_container)
        total_critical = sum(e.get("critical_hits", 0) for e in per_container)
        total_warning = sum(e.get("warning_hits", 0) for e in per_container)

        details: dict[str, Any] = {
            "lookback_seconds": lookback,
            "containers_scanned": len(per_container),
            "total_blocker_hits": total_blocker,
            "total_critical_hits": total_critical,
            "total_warning_hits": total_warning,
            "blocker_samples": all_blocker_samples[:sample_cap],
            "critical_samples": all_critical_samples[:sample_cap],
            "warning_samples": all_warning_samples[:sample_cap],
            "per_container": per_container,
        }

        if not per_container:
            return self._result(Status.UNKNOWN, "container_logs: no matching containers found", details=details)

        if worst_status == Status.DOWN:
            summary = f"container_logs: BLOCKER patterns hit — {total_blocker} match(es)"
        elif worst_status == Status.DEGRADED:
            summary = (
                f"container_logs: {total_critical} critical + {total_warning} warning match(es) "
                f"across {len(per_container)} container(s)"
            )
        else:
            summary = f"container_logs: clean across {len(per_container)} container(s) ({lookback}s window)"

        return self._result(worst_status, summary, details=details)
