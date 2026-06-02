"""
KillSwitchCache — in-memory kill switch state with background DynamoDB refresh.

Wraps the existing ``KillSwitch`` with a 1-second background polling loop so
that the kill switch state is always current even if the Kafka/SNS propagation
path fails.  ``is_active()`` returns the in-memory state synchronously (0ms,
no I/O) so the validator pipeline pays zero latency cost for the safety check.

Problem solved:
    The existing ``KillSwitch.is_active()`` is already O(0) — it reads an
    in-memory bool set by ``load_state()`` at startup.  The gap is that
    **after startup** the in-memory state only updates via:
        1. Kafka kill-switch-listener topic
        2. SNS subscription (if wired)
    If either path fails, the kill switch activated via the HTTP API or CLI
    will NOT reach the risk engine's in-memory state until restart.

    ``KillSwitchCache`` adds a third path: a background asyncio task that
    reads the DynamoDB record every ``poll_interval_seconds`` (default 1s).
    This ensures that even with Kafka and SNS both down, the kill switch
    activates within 1s of a DynamoDB write.

Force-cancel on activation (Zerodha alignment):
    When the poll detects a NEW activation (transitioned from inactive →
    active), ``KillSwitchCache`` fires an ``on_activated`` callback.  The
    risk engine wires this callback to trigger ``cancel_all_orders()`` on the
    execution engine with ``Priority.CRITICAL`` (as required by ADR-014 /
    Misalignment 4).

Usage:
    cache = KillSwitchCache(kill_switch=existing_kill_switch)
    await cache.start()          # starts background poll loop
    ...
    if cache.is_active():        # synchronous, 0ms
        reject(signal)
    ...
    await cache.stop()
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine, Optional

from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="risk_engine")

# Callback type: async function called when kill switch transitions inactive → active.
OnActivatedCallback = Callable[[], Coroutine[Any, Any, None]]


class KillSwitchCache:
    """
    In-memory kill switch state with 1-second background DynamoDB refresh.

    Attributes:
        _kill_switch:       Underlying KillSwitch for DynamoDB reads/writes.
        _poll_interval:     Seconds between DynamoDB state refreshes (default 1).
        _on_activated:      Optional async callback fired when activation is detected
                            for the first time (inactive → active transition).  Used
                            to trigger Priority.CRITICAL force-cancel of open orders.
        _cached_active:     In-memory kill switch state.  Updated by the poll loop.
        _poll_task:         The background asyncio task running the poll loop.
        _running:           Set to False by stop() to exit the poll loop cleanly.
    """

    def __init__(
        self,
        kill_switch: Any,
        poll_interval_seconds: float = 1.0,
        on_activated: Optional[OnActivatedCallback] = None,
    ) -> None:
        """
        Args:
            kill_switch: Existing ``KillSwitch`` instance that owns the DynamoDB
                state.  This cache reads state from it; all writes (activate /
                deactivate) still go through ``kill_switch`` directly.
            poll_interval_seconds: How often to refresh state from DynamoDB.
                Default 1s.  Set higher in tests to reduce noise.
            on_activated: Optional async callback invoked once per
                inactive → active transition.  The callback should trigger
                Priority.CRITICAL force-cancel of all open orders.
        """
        self._kill_switch = kill_switch
        self._poll_interval = poll_interval_seconds
        self._on_activated = on_activated
        self._cached_active: bool = False
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._running: bool = False

    # ── Public API ────────────────────────────────────────────────────────────

    def is_active(self) -> bool:
        """
        Return cached kill switch state — synchronous, 0ms, no I/O.

        Always reflects the most recent DynamoDB poll result (at most
        ``poll_interval_seconds`` stale) or the startup ``load_state()`` value.

        Returns:
            True if trading is halted.
        """
        return self._cached_active

    @property
    def reason(self) -> str:
        """Delegate to underlying KillSwitch for human-readable activation reason."""
        return self._kill_switch.reason  # type: ignore[no-any-return]

    async def start(self) -> None:
        """
        Start the background DynamoDB poll loop.

        Immediately performs one synchronous state load so the cache is
        populated before the first signal validation.  The background poll
        loop then keeps it current.
        """
        # Seed the cache with the current DynamoDB state before starting the loop.
        # This is the same call made by RiskEngineService.start() via load_state()
        # but we do it explicitly here so KillSwitchCache is self-contained.
        try:
            await self._kill_switch.load_state()
            self._cached_active = self._kill_switch.active
            logger.info(
                "kill_switch_cache.started (initial_state=%s, poll_interval=%.1fs)",
                self._cached_active,
                self._poll_interval,
            )
        except Exception:
            logger.exception("kill_switch_cache.initial_load_failed — defaulting to inactive")
            self._cached_active = False

        self._running = True
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="kill-switch-cache-poll"
        )

    async def stop(self) -> None:
        """
        Stop the background poll loop cleanly.

        Waits for the poll task to finish (it will exit on the next iteration
        once ``_running`` is False).
        """
        self._running = False
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("kill_switch_cache.stopped")

    # ── Background poll loop ──────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """
        Background asyncio task: refresh kill switch state from DynamoDB.

        Loop:
            1. Sleep for poll_interval_seconds.
            2. Call kill_switch.load_state() to refresh the underlying bool.
            3. Compare new state to cached state.
            4. If inactive → active transition: fire on_activated callback.
            5. Update _cached_active.

        Errors:
            DynamoDB transient errors are logged and skipped.  The cached state
            remains at its last known value — a brief polling gap does NOT
            inadvertently deactivate an active kill switch.
        """
        logger.info("kill_switch_cache.poll_loop.started")

        while self._running:
            await asyncio.sleep(self._poll_interval)

            if not self._running:
                break

            try:
                await self._kill_switch.load_state()
                new_active = self._kill_switch.active

                if new_active != self._cached_active:
                    if new_active:
                        # Transition: inactive → active
                        logger.warning(
                            "kill_switch_cache.activation_detected "
                            "(reason=%r) — triggering force-cancel",
                            self._kill_switch.reason,
                        )
                        self._cached_active = True
                        if self._on_activated is not None:
                            try:
                                await self._on_activated()
                            except Exception:
                                logger.exception(
                                    "kill_switch_cache.on_activated_callback_failed"
                                )
                    else:
                        # Transition: active → inactive
                        logger.info(
                            "kill_switch_cache.deactivation_detected — resuming"
                        )
                        self._cached_active = False

            except Exception:
                logger.exception(
                    "kill_switch_cache.poll_error — retaining cached state (active=%s)",
                    self._cached_active,
                )

        logger.info("kill_switch_cache.poll_loop.stopped")
