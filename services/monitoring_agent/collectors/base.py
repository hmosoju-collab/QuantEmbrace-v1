"""Base class shared by every Phase 1 collector.

A *collector* answers one question read-only: "is this component reachable and
behaving?" It returns a :class:`~monitoring_agent.snapshot.CollectorResult` with a
coarse :class:`~monitoring_agent.snapshot.Status` plus secret-free ``details``.

Two invariants are enforced here so individual collectors stay simple:

    1. **Read-only.** Collectors only observe. They never produce to Kafka, never
       commit offsets, never write/update/delete DynamoDB, never start/stop Docker
       containers, and never call a broker API. This base class cannot enforce that
       mechanically, but the contract is asserted by the test-suite's API audit.
    2. **Never throw.** A collector that blows up must not take down the agent.
       :meth:`run` wraps the blocking work, offloads it to a thread (so a slow probe
       can't stall the async loop), times it, and converts any escaped exception
       into a ``Status.UNKNOWN`` result.

Subclasses implement the synchronous :meth:`_collect_sync`; the base handles
threading, timing, and the exception safety-net.
"""

from __future__ import annotations

import abc
import asyncio
import time
from typing import Any, Optional

from monitoring_agent.config import AgentConfig
from monitoring_agent.rules import MonitoringRules
from monitoring_agent.snapshot import CollectorResult, Status


class Collector(abc.ABC):
    """Abstract read-only collector.

    Attributes:
        name: Stable identifier used as the :class:`CollectorResult` name and in
            rules/alerts (e.g. ``"kafka"``, ``"services"``, ``"docker"``).
    """

    name: str = "collector"

    def __init__(self, config: AgentConfig, rules: MonitoringRules) -> None:
        self.config = config
        self.rules = rules

    def enabled(self) -> bool:
        """Whether this collector should run given the loaded rules/config.

        Defaults to True. Collectors that are opt-in (Docker, logs) override this.
        """
        return True

    @abc.abstractmethod
    def _collect_sync(self) -> CollectorResult:
        """Do the actual (blocking, read-only) probing and return a result.

        May raise — :meth:`run` converts any exception into ``Status.UNKNOWN``.
        """
        raise NotImplementedError

    async def run(self) -> CollectorResult:
        """Run the collector in a worker thread, timed, never raising."""
        start = time.perf_counter()
        try:
            result = await asyncio.to_thread(self._collect_sync)
        except Exception as exc:  # safety net — a collector must never crash the loop
            result = CollectorResult(
                name=self.name,
                status=Status.UNKNOWN,
                summary=f"{self.name}: collector raised {type(exc).__name__}",
                error=repr(exc),
            )
        result.duration_ms = (time.perf_counter() - start) * 1000.0
        return result

    # ── convenience ──────────────────────────────────────────────────────────

    def _result(
        self,
        status: Status,
        summary: str,
        *,
        details: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> CollectorResult:
        """Build a :class:`CollectorResult` carrying this collector's name."""
        return CollectorResult(
            name=self.name,
            status=status,
            summary=summary,
            details=details or {},
            error=error,
        )
