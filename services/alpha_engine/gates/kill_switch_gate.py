"""KillSwitchGate — read-only kill-switch awareness for the Alpha Engine.

A faithful mirror of strategy_engine's ``_is_kill_switch_active`` (1s TTL cache,
fail-safe to last-known state on read error). The Alpha Engine NEVER writes the
kill switch — when active, it merely pauses publishing and store writes so the
shadow stream and research tables don't accumulate output during a halt. This is
advisory-only behaviour; nothing here can stop or start trading.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from shared.logging.logger import get_logger
from shared.risk_state import attr_bool, kill_switch_resource_key

logger = get_logger(__name__, service_name="alpha_engine")


class KillSwitchGate:
    """Polls the global kill-switch row with a short TTL cache.

    Args:
        table:  A DynamoDB ``Table`` resource for ``{prefix}-risk-state`` (or any
                object exposing ``get_item(Key=...)``). Injected for testability.
        poll_interval_seconds: Minimum seconds between DynamoDB reads.
    """

    def __init__(self, *, table: Any, poll_interval_seconds: float = 1.0) -> None:
        self._table = table
        self._poll = poll_interval_seconds
        self._active = False
        self._checked_at = 0.0  # monotonic; 0 forces a read on first call

    async def is_active(self) -> bool:
        now = time.monotonic()
        if self._checked_at and (now - self._checked_at) < self._poll:
            return self._active

        try:
            response = await asyncio.to_thread(
                self._table.get_item,
                Key=kill_switch_resource_key(),
                ProjectionExpression="active, #status",
                ExpressionAttributeNames={"#status": "status"},
            )
            item = response.get("Item")
            active = bool(item and attr_bool(item, "active", False))
            if active != self._active:
                logger.warning(
                    "alpha_engine.kill_switch_state_changed %s -> %s",
                    "ACTIVE" if self._active else "INACTIVE",
                    "ACTIVE" if active else "INACTIVE",
                )
            self._active = active
            self._checked_at = now
        except Exception:
            # Fail-safe: keep the last known state and retry next call (do not
            # advance _checked_at, so a transient error doesn't cache a stale read).
            logger.exception(
                "alpha_engine.kill_switch_read_failed using last known state: %s",
                "ACTIVE" if self._active else "INACTIVE",
            )
        return self._active
