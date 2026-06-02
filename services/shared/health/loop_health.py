"""Loop health tracking — Phase 7.1 (B-002).

Provides a lightweight wrapper and tracker for long-running async loops so
that crashes are never silent.

Usage — wrapping a coroutine:

    tracker = LoopHealthTracker("candle_loop", metrics_client=_metrics)
    await tracker.run(self._candle_processing_loop())

Usage — inline heartbeat inside a loop body:

    tracker = LoopHealthTracker("kafka_loop", metrics_client=_metrics)
    while self._running:
        try:
            await self._process_tick()
            tracker.record_success()
        except Exception as exc:
            tracker.record_crash(exc)
            raise

Metrics emitted (namespace inherited from metrics_client):
    service.loop_running                 gauge: 1 while running, 0 after crash
    service.loop_crash_total             counter: increments on each unhandled exception
    service.loop_last_success_timestamp  gauge: epoch seconds of last successful cycle

All metric emissions are best-effort (never raise).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Optional

logger = logging.getLogger("shared.health.loop_health")


class LoopHealthTracker:
    """Track health of a long-running async loop and emit standard metrics.

    Args:
        loop_name:      Short identifier used as the ``Loop`` CloudWatch dimension.
        metrics_client: An object with ``record_count(name, value, dimensions)``
                        and optionally ``record_gauge(name, value, dimensions)``.
                        If None, metrics are skipped but logging still works.
        service_name:   Added as ``Service`` dimension if provided.
    """

    def __init__(
        self,
        loop_name: str,
        metrics_client: Optional[Any] = None,
        service_name: str = "",
    ) -> None:
        self.loop_name = loop_name
        self._metrics = metrics_client
        self._service = service_name
        self._running: bool = False
        self._crash_count: int = 0
        self._last_success_at: float = 0.0
        self._started_at: float = 0.0

    # ── public API ─────────────────────────────────────────────────────────────

    async def run(self, coro: Awaitable[Any]) -> Any:
        """Run *coro*, emitting health metrics and logging any crash as CRITICAL.

        Crashes are re-raised after being logged so the caller's gather or
        task-runner can propagate them correctly.

        Usage:
            await health.run(self._candle_processing_loop())
        """
        self._running = True
        self._started_at = time.time()
        self._emit_running(1)
        try:
            result = await coro
            return result
        except asyncio.CancelledError:
            # CancelledError is not a crash — it's a graceful shutdown signal.
            self._running = False
            self._emit_running(0)
            raise
        except Exception as exc:
            self.record_crash(exc)
            raise
        finally:
            self._running = False
            self._emit_running(0)

    def record_success(self) -> None:
        """Call at the end of each successful loop cycle to update the heartbeat."""
        self._last_success_at = time.time()
        self._emit_gauge("service.loop_last_success_timestamp", self._last_success_at)

    def record_crash(self, exc: BaseException) -> None:
        """Call when an unhandled exception is caught in the loop."""
        self._crash_count += 1
        self._running = False
        logger.critical(
            "loop_health.crash loop=%s crash_count=%d error_type=%s error=%s — "
            "service restart required",
            self.loop_name, self._crash_count, type(exc).__name__, repr(exc),
        )
        self._emit_count("service.loop_crash_total", 1)
        self._emit_running(0)

    # ── properties ─────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def crash_count(self) -> int:
        return self._crash_count

    @property
    def last_success_at(self) -> float:
        return self._last_success_at

    # ── internal ───────────────────────────────────────────────────────────────

    def _dims(self) -> dict[str, str]:
        d: dict[str, str] = {"Loop": self.loop_name}
        if self._service:
            d["Service"] = self._service
        return d

    def _emit_running(self, value: float) -> None:
        self._emit_gauge("service.loop_running", value)

    def _emit_count(self, name: str, value: float = 1.0) -> None:
        if self._metrics is None:
            return
        try:
            self._metrics.record_count(name, value=value, dimensions=self._dims())
        except Exception:  # noqa: BLE001
            pass  # metrics must never crash the caller

    def _emit_gauge(self, name: str, value: float) -> None:
        if self._metrics is None:
            return
        try:
            # Fall back to record_count if record_gauge is not available.
            if hasattr(self._metrics, "record_gauge"):
                self._metrics.record_gauge(name, value=value, dimensions=self._dims())
            else:
                self._metrics.record_count(name, value=value, dimensions=self._dims())
        except Exception:  # noqa: BLE001
            pass
