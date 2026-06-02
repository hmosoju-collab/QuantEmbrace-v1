"""Service health collector — probes each trading service's /health and /ready.

Every QuantEmbrace service exposes ``GET /health`` (liveness, 200) and
``GET /ready`` (readiness, 200 ready / 503 not-ready). This collector issues
plain HTTP GETs with the stdlib (no extra deps, inherently read-only) and maps
the responses to a coarse status:

    * not reachable at all (TCP refused / timeout) → DOWN  (process not serving)
    * reachable, /health 200 and /ready 200           → OK
    * reachable but /health!=200 or /ready!=200        → DEGRADED

A *non-critical* service that is DOWN only degrades the overall result (it must
not, by itself, paint the whole platform DOWN). The agent never probes itself.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from typing import Any, Optional

from monitoring_agent.collectors.base import Collector
from monitoring_agent.snapshot import CollectorResult, Status, worst

logger = logging.getLogger("monitoring_agent.collectors.services")


def _probe(url: str, timeout: float) -> tuple[bool, Optional[int], Optional[str]]:
    """GET a URL read-only. Returns (reachable, http_status, error)."""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 (fixed http scheme)
            return True, resp.getcode(), None
    except urllib.error.HTTPError as exc:
        # Got an HTTP response with a non-2xx code → service is reachable.
        return True, exc.code, None
    except Exception as exc:  # noqa: BLE001 — connection refused / timeout / DNS
        return False, None, repr(exc)


def decide_service_status(
    reachable: bool, health_code: Optional[int], ready_code: Optional[int]
) -> Status:
    """Map probe outcomes to a coarse component status."""
    if not reachable:
        return Status.DOWN
    if health_code == 200 and ready_code == 200:
        return Status.OK
    return Status.DEGRADED


class ServiceCollector(Collector):
    """Probe HTTP health/readiness endpoints for the configured services."""

    name = "services"

    def _collect_sync(self) -> CollectorResult:
        services = self.rules.services
        if not services:
            return self._result(Status.UNKNOWN, "services: none configured in rules.yaml")

        per_service: list[dict[str, Any]] = []
        effective: list[Status] = []

        for svc in services:
            base = f"http://{svc.host}:{svc.port}"
            health_reachable, health_code, health_err = _probe(base + svc.health_path, svc.timeout_seconds)
            ready_reachable, ready_code, ready_err = _probe(base + svc.ready_path, svc.timeout_seconds)
            reachable = health_reachable or ready_reachable
            status = decide_service_status(reachable, health_code, ready_code)

            per_service.append(
                {
                    "name": svc.name,
                    "status": status.value,
                    "critical": svc.critical,
                    "reachable": reachable,
                    "health_code": health_code,
                    "ready_code": ready_code,
                    "endpoint": base,
                    "error": health_err or ready_err,
                }
            )

            # A non-critical service that is DOWN should only degrade the overall.
            eff = status
            if not svc.critical and status == Status.DOWN:
                eff = Status.DEGRADED
            effective.append(eff)

        overall = worst(effective)
        down = [p["name"] for p in per_service if p["status"] == Status.DOWN.value]
        degraded = [p["name"] for p in per_service if p["status"] == Status.DEGRADED.value]

        summary = f"services: {len(services)} checked"
        if down:
            summary += f"; down={down}"
        if degraded:
            summary += f"; degraded={degraded}"
        if not down and not degraded:
            summary += "; all healthy"

        return self._result(overall, summary, details={"services": per_service})
