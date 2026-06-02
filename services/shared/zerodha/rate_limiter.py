"""
Zerodha API Token Bucket Rate Limiter with Priority Queues.

Replaces ``asyncio.Semaphore(8)`` in the execution engine.  A semaphore
limits *concurrency*, not *rate* — with 8 concurrent 50ms calls you get
160 req/sec, not 10.  This module implements a true token bucket that
enforces exactly 10 req/sec regardless of call duration or concurrency.

Design (ADR-012 / architecture/zerodha_rate_capacity_design.md):

    Endpoint buckets:
        order_place   10 req/sec + 10/sec, 400/min, 5000/day order caps
        order_control 10 req/sec, with reserved CRITICAL capacity
        quote          1 req/sec
        historical     3 req/sec
        other         10 req/sec

    Priority tiers:
        CRITICAL  Emergency cancel, kill-switch force-cancel.  Preempts queue.
        HIGH      place_order, bulk_fill_poll.
        MEDIUM    get_positions, get_margins.
        LOW       historical data, analytics, non-urgent reads.

    Waiting:
        Requests that cannot immediately acquire a token are enqueued.
        They are served in priority order when tokens become available.
        Requests are NEVER dropped — CRITICAL calls always get served next.

    Market phase awareness:
        ``set_market_phase()`` is called by ``MarketPhaseGovernor`` when the
        IST-time phase changes.  Phase-aware budget information is surfaced via
        ``get_utilization()`` for the CloudWatch metrics exporter; it does NOT
        affect the fundamental token bucket mechanics (the budget table is advisory
        — the limiter never *blocks* a call because it is over budget for its
        category, it only applies the token cost).

Usage::

    limiter = ZerodhaRateLimiter()

    # Acquire before every Kite Connect API call:
    await limiter.acquire(Priority.HIGH)
    result = kite.orders()

    # Emergency cancel — preempts all queued LOW/MEDIUM/HIGH work:
    await limiter.acquire(Priority.CRITICAL)
    kite.cancel_order(...)

    # Observe utilization:
    util = limiter.get_utilization()
    # {"tokens_available": 8.2, "burst_remaining": 6.8, "queue_depth": 0, ...}
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import suppress
from datetime import datetime
from enum import Enum, IntEnum

from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="execution_engine")

# ── Public enums ──────────────────────────────────────────────────────────────


class Priority(IntEnum):
    """
    Request priority for the Zerodha rate limiter queue.

    Lower integer = higher priority.  CRITICAL always jumps to the front.
    """

    CRITICAL = 0  # emergency cancel, kill-switch force-cancel
    HIGH = 1  # place_order, bulk_fill_poll, cancel_order
    MEDIUM = 2  # get_positions, get_margins, get_quotes (batch)
    LOW = 3  # historical data, analytics, non-urgent reads


class EndpointClass(str, Enum):
    """Zerodha endpoint rate-limit class."""

    ORDER_PLACE = "order_place"       # kite.place_order(), capped by orders/sec/min/day
    ORDER_CONTROL = "order_control"   # cancel/modify and emergency order controls
    QUOTE = "quote"                   # kite.quote(), 1 req/sec
    HISTORICAL = "historical"         # kite.historical_data(), 3 req/sec
    OTHER = "other"                   # positions, orders, margins, instruments, etc.


class ZerodhaRateLimitExceeded(RuntimeError):
    """Raised when a non-waitable Zerodha limit would be breached."""


class _EndpointBucket:
    """Token bucket for one Zerodha endpoint class."""

    __slots__ = ("rate", "burst", "tokens", "last_refill")

    def __init__(self, rate: float, burst: float) -> None:
        self.rate = float(rate)
        self.burst = float(burst)
        self.tokens = float(burst)
        self.last_refill = time.monotonic()

    def refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last_refill = now

    def consume(self, units: float = 1.0) -> None:
        self.tokens -= units


# ── Internal waiter ───────────────────────────────────────────────────────────


class _Waiter:
    """A single request waiting for a token."""

    __slots__ = ("priority", "endpoint", "units", "future", "enqueued_at")

    def __init__(
        self,
        priority: Priority,
        endpoint: EndpointClass,
        units: float,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.priority = priority
        self.endpoint = endpoint
        self.units = units
        self.future = loop.create_future()
        self.enqueued_at = time.monotonic()

    def __lt__(self, other: "_Waiter") -> bool:
        # Lower priority value = higher urgency; break ties by FIFO
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.enqueued_at < other.enqueued_at


# ── Rate limiter ─────────────────────────────────────────────────────────────


class ZerodhaRateLimiter:
    """
    Endpoint-aware limiter for Zerodha Kite Connect REST limits.

    Thread-safe via asyncio Lock.  All methods are coroutine-safe.

    Args:
        capacity:       Sustained token refill rate in tokens/second.
                        Default 10 — matches Zerodha's published limit.
        burst_capacity: General endpoint burst ceiling. Default 10 so the
                        client does not exceed per-endpoint Kite request caps.
    """

    def __init__(
        self,
        capacity: int = 10,
        burst_capacity: int = 10,
        quote_capacity: int = 1,
        historical_capacity: int = 3,
        order_capacity: int = 10,
        other_capacity: int = 10,
        critical_reserved_tokens: int = 2,
        max_orders_per_second: int = 10,
        max_orders_per_minute: int = 400,
        max_orders_per_day: int = 5000,
    ) -> None:
        self._capacity = float(capacity)
        self._burst = float(min(burst_capacity, capacity))
        self._tokens = self._burst  # legacy snapshot for OTHER bucket
        self._last_refill = time.monotonic()
        self._critical_reserved_tokens = float(max(0, critical_reserved_tokens))
        self._buckets: dict[EndpointClass, _EndpointBucket] = {
            EndpointClass.ORDER_PLACE: _EndpointBucket(
                rate=float(order_capacity),
                burst=float(min(order_capacity, max_orders_per_second)),
            ),
            EndpointClass.ORDER_CONTROL: _EndpointBucket(
                rate=float(other_capacity),
                burst=float(min(other_capacity, capacity)),
            ),
            EndpointClass.QUOTE: _EndpointBucket(
                rate=float(quote_capacity),
                burst=float(max(1, quote_capacity)),
            ),
            EndpointClass.HISTORICAL: _EndpointBucket(
                rate=float(historical_capacity),
                burst=float(max(1, historical_capacity)),
            ),
            EndpointClass.OTHER: _EndpointBucket(
                rate=float(other_capacity),
                burst=float(min(other_capacity, capacity)),
            ),
        }
        self._lock = asyncio.Lock()
        self._waiters: list[_Waiter] = []

        self._max_orders_per_second = int(max_orders_per_second)
        self._max_orders_per_minute = int(max_orders_per_minute)
        self._max_orders_per_day = int(max_orders_per_day)
        self._order_second_window: deque[float] = deque()
        self._order_minute_window: deque[float] = deque()
        self._order_day_window: deque[float] = deque()
        self._order_day_key = datetime.utcnow().date()

        # Metrics
        self._total_acquired = 0
        self._total_waited = 0
        self._peak_queue_depth = 0
        self._last_phase = "UNKNOWN"
        self._drain_task: asyncio.Task[None] | None = None

        # Per-priority acquire counts (for utilization reporting)
        self._priority_counts: dict[Priority, int] = {p: 0 for p in Priority}
        self._endpoint_counts: dict[EndpointClass, int] = {e: 0 for e in EndpointClass}

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """True when the background drain loop is active."""
        return self._drain_task is not None and not self._drain_task.done()

    async def acquire(
        self,
        priority: Priority = Priority.HIGH,
        endpoint: EndpointClass | str = EndpointClass.OTHER,
        units: float = 1.0,
    ) -> None:
        """
        Acquire one rate-limit token before making a Kite Connect API call.

        Blocks until a token is available.  CRITICAL requests skip the queue
        and are served before any pending HIGH/MEDIUM/LOW requests when tokens
        become available.

        Args:
            priority: Request priority (default HIGH for most API calls).

        Example::

            await rate_limiter.acquire(Priority.CRITICAL)
            kite.cancel_order(order_id=emergency_order)
        """
        endpoint_class = self._normalise_endpoint(endpoint)
        if units <= 0:
            return

        async with self._lock:
            self._refill()
            self._raise_if_daily_order_cap_exhausted(endpoint_class)
            if self._can_acquire(endpoint_class, priority, units):
                # Fast path: token available immediately
                self._consume(endpoint_class, priority, units)
                return

            # Slow path: enqueue and wait
            loop = asyncio.get_event_loop()
            waiter = _Waiter(
                priority=priority,
                endpoint=endpoint_class,
                units=units,
                loop=loop,
            )
            self._waiters.append(waiter)
            self._waiters.sort()  # O(N log N) — queue is rarely >10 items

            depth = len(self._waiters)
            if depth > self._peak_queue_depth:
                self._peak_queue_depth = depth

            if depth > 5:
                logger.warning(
                    "zerodha_rate_limiter.queue_building",
                    queue_depth=depth,
                    priority=priority.name,
                    endpoint=endpoint_class.value,
                    tokens_available=round(self._tokens, 2),
                )

        self._total_waited += 1
        # Wait outside the lock so other coroutines can acquire
        await waiter.future

    def set_market_phase(self, phase: str) -> None:
        """
        Notify the limiter of a market phase change.

        Called by ``MarketPhaseGovernor`` when the IST clock crosses a phase
        boundary. The phase is recorded for utilization reporting — the token
        budget mechanics do not change per phase (CRITICAL always preempts).

        Args:
            phase: Phase name string from ``MarketPhase`` enum.
        """
        self._last_phase = phase
        logger.info(
            "zerodha_rate_limiter.phase_changed",
            phase=phase,
            tokens_at_transition=round(self._tokens, 2),
        )

    def get_utilization(self) -> dict:
        """
        Return current rate limiter utilization snapshot.

        Used by CloudWatch metrics exporter and ``scripts/zerodha/rate_monitor.py``.

        Returns:
            Dict with keys: tokens_available, burst_remaining, queue_depth,
            total_acquired, total_waited, peak_queue_depth, market_phase,
            priority_breakdown.
        """
        self._refill()
        return {
            "tokens_available": round(self._tokens, 2),
            "burst_remaining": round(self._burst - self._tokens, 2),
            "queue_depth": len(self._waiters),
            "total_acquired": self._total_acquired,
            "total_waited": self._total_waited,
            "peak_queue_depth": self._peak_queue_depth,
            "market_phase": self._last_phase,
            "priority_breakdown": {p.name: self._priority_counts[p] for p in Priority},
            "endpoint_breakdown": {
                e.value: self._endpoint_counts[e] for e in EndpointClass
            },
            "endpoint_tokens": {
                e.value: round(bucket.tokens, 2)
                for e, bucket in self._buckets.items()
            },
            "order_limits": {
                "orders_last_second": len(self._order_second_window),
                "orders_last_minute": len(self._order_minute_window),
                "orders_today": len(self._order_day_window),
                "max_orders_per_second": self._max_orders_per_second,
                "max_orders_per_minute": self._max_orders_per_minute,
                "max_orders_per_day": self._max_orders_per_day,
            },
        }

    def get_token_count(self) -> float:
        """Return current number of available tokens (0.0 – burst_capacity)."""
        self._refill()
        return round(self._buckets[EndpointClass.OTHER].tokens, 3)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _refill(self) -> None:
        """
        Replenish endpoint tokens and wake any waiters that can now proceed.

        Called inside the lock before every acquire attempt and from the
        background drain loop.  Uses monotonic clock to avoid drift.
        """
        for bucket in self._buckets.values():
            bucket.refill()
        self._sync_legacy_token_snapshot()
        self._prune_order_windows()

        made_progress = True
        while made_progress:
            made_progress = False
            self._waiters.sort()
            for waiter in list(self._waiters):
                if waiter.future.done():
                    self._waiters.remove(waiter)
                    made_progress = True
                    break
                if self._can_acquire(waiter.endpoint, waiter.priority, waiter.units):
                    self._waiters.remove(waiter)
                    self._consume(waiter.endpoint, waiter.priority, waiter.units)
                    waiter.future.set_result(None)
                    made_progress = True
                    break

    @staticmethod
    def _normalise_endpoint(endpoint: EndpointClass | str) -> EndpointClass:
        if isinstance(endpoint, EndpointClass):
            return endpoint
        raw = str(endpoint).strip().lower().replace("-", "_")
        aliases = {
            "order": EndpointClass.ORDER_PLACE,
            "place_order": EndpointClass.ORDER_PLACE,
            "order_place": EndpointClass.ORDER_PLACE,
            "cancel": EndpointClass.ORDER_CONTROL,
            "cancel_order": EndpointClass.ORDER_CONTROL,
            "modify_order": EndpointClass.ORDER_CONTROL,
            "order_control": EndpointClass.ORDER_CONTROL,
            "quotes": EndpointClass.QUOTE,
            "quote": EndpointClass.QUOTE,
            "historical": EndpointClass.HISTORICAL,
            "historical_data": EndpointClass.HISTORICAL,
            "other": EndpointClass.OTHER,
        }
        try:
            return aliases[raw]
        except KeyError:
            return EndpointClass(raw)

    def _sync_legacy_token_snapshot(self) -> None:
        bucket = self._buckets[EndpointClass.OTHER]
        self._tokens = bucket.tokens
        self._capacity = bucket.rate
        self._burst = bucket.burst
        self._last_refill = bucket.last_refill

    def _reserved_floor(self, endpoint: EndpointClass, priority: Priority) -> float:
        if priority == Priority.CRITICAL:
            return 0.0
        if endpoint in {
            EndpointClass.ORDER_PLACE,
            EndpointClass.ORDER_CONTROL,
            EndpointClass.OTHER,
        }:
            return min(
                self._critical_reserved_tokens,
                max(0.0, self._buckets[endpoint].burst - 1.0),
            )
        return 0.0

    def _can_acquire(
        self,
        endpoint: EndpointClass,
        priority: Priority,
        units: float,
    ) -> bool:
        bucket = self._buckets[endpoint]
        floor = self._reserved_floor(endpoint, priority)
        if bucket.tokens - units < floor:
            return False
        if endpoint == EndpointClass.ORDER_PLACE and not self._order_caps_available():
            return False
        return True

    def _consume(
        self,
        endpoint: EndpointClass,
        priority: Priority,
        units: float,
    ) -> None:
        self._buckets[endpoint].consume(units)
        if endpoint == EndpointClass.ORDER_PLACE:
            now = time.monotonic()
            for _ in range(int(units)):
                self._order_second_window.append(now)
                self._order_minute_window.append(now)
                self._order_day_window.append(now)
        self._total_acquired += 1
        self._priority_counts[priority] += 1
        self._endpoint_counts[endpoint] += 1
        self._sync_legacy_token_snapshot()

    def _prune_order_windows(self) -> None:
        today = datetime.utcnow().date()
        if today != self._order_day_key:
            self._order_day_key = today
            self._order_day_window.clear()

        now = time.monotonic()
        while self._order_second_window and now - self._order_second_window[0] >= 1.0:
            self._order_second_window.popleft()
        while self._order_minute_window and now - self._order_minute_window[0] >= 60.0:
            self._order_minute_window.popleft()

    def _order_caps_available(self) -> bool:
        self._prune_order_windows()
        return (
            len(self._order_second_window) < self._max_orders_per_second
            and len(self._order_minute_window) < self._max_orders_per_minute
            and len(self._order_day_window) < self._max_orders_per_day
        )

    def _raise_if_daily_order_cap_exhausted(self, endpoint: EndpointClass) -> None:
        if endpoint != EndpointClass.ORDER_PLACE:
            return
        self._prune_order_windows()
        if len(self._order_day_window) >= self._max_orders_per_day:
            raise ZerodhaRateLimitExceeded(
                "Zerodha daily order placement cap exhausted "
                f"({self._max_orders_per_day}/day). Trading must halt until reset."
            )

    async def _drain_loop(self) -> None:
        """
        Background coroutine that periodically refills tokens and wakes waiters.

        Without this loop, waiters would only be woken when a new ``acquire()``
        call triggers ``_refill()``.  The drain loop ensures that even if no
        new calls arrive, queued waiters are served as soon as tokens refill.

        Runs every 50ms (20 Hz refill check — overkill but cheap).
        """
        while True:
            await asyncio.sleep(0.05)
            async with self._lock:
                self._refill()

    async def start(self) -> None:
        """Start the background drain loop.  Call once from service startup."""
        if self.is_running:
            return
        self._drain_task = asyncio.create_task(
            self._drain_loop(),
            name="zerodha_rate_limiter_drain",
        )
        await asyncio.sleep(0)
        logger.info(
            "zerodha_rate_limiter.started",
            capacity=int(self._capacity),
            burst=int(self._burst),
        )

    async def stop(self) -> None:
        """Stop the background drain loop."""
        if self._drain_task is None:
            return
        self._drain_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._drain_task
        self._drain_task = None
        logger.info("zerodha_rate_limiter.stopped")

    # ── Context manager support ───────────────────────────────────────────────

    async def __aenter__(self) -> "ZerodhaRateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *_: object) -> None:
        pass  # Token already consumed on enter
