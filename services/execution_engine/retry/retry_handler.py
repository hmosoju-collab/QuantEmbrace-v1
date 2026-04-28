"""
Retry Handler — exponential backoff with circuit breaker for broker API calls.

Provides resilient communication with external broker APIs. When a broker
endpoint is consistently failing, the circuit breaker opens to prevent
wasting resources on calls that will likely fail, and allows the broker
time to recover.
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Any, Callable, Coroutine, Optional, TypeVar

from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="execution_engine")

T = TypeVar("T")


class CircuitState(str, Enum):
    """Circuit breaker states."""

    CLOSED = "CLOSED"  # Normal operation — requests flow through
    OPEN = "OPEN"  # Failing — requests are rejected immediately
    HALF_OPEN = "HALF_OPEN"  # Testing — one request allowed to probe recovery


class CircuitBreakerOpen(Exception):
    """Raised when a call is attempted while the circuit breaker is open."""

    def __init__(self, broker: str, cooldown_remaining: float) -> None:
        self.broker = broker
        self.cooldown_remaining = cooldown_remaining
        super().__init__(
            f"Circuit breaker OPEN for {broker} — "
            f"{cooldown_remaining:.1f}s remaining before probe"
        )


class RetryHandler:
    """
    Retry handler with exponential backoff and circuit breaker.

    The circuit breaker pattern prevents cascading failures when a broker
    API is down. After ``failure_threshold`` consecutive failures, the
    circuit opens and all subsequent calls are rejected immediately for
    ``cooldown_seconds``. After the cooldown, a single probe request is
    allowed (half-open state). If it succeeds, the circuit closes; if it
    fails, the circuit re-opens.

    Configuration:
        max_retries: Maximum retry attempts per call.
        base_delay: Initial delay between retries (seconds).
        max_delay: Maximum delay cap (seconds).
        backoff_factor: Multiplier for exponential backoff.
        failure_threshold: Consecutive failures before circuit opens.
        cooldown_seconds: How long the circuit stays open before probing.
    """

    def __init__(
        self,
        broker_name: str = "unknown",
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        backoff_factor: float = 2.0,
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
        retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    ) -> None:
        self._broker_name = broker_name
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._backoff_factor = backoff_factor
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._retryable_exceptions = retryable_exceptions

        # Circuit breaker state
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._last_failure_time: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """Current circuit breaker state."""
        return self._state

    @property
    def consecutive_failures(self) -> int:
        """Number of consecutive failures."""
        return self._consecutive_failures

    async def execute(
        self,
        func: Callable[..., Coroutine[Any, Any, T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """
        Execute an async function with retry and circuit breaker protection.

        Args:
            func: The async function to call.
            *args: Positional arguments for the function.
            **kwargs: Keyword arguments for the function.

        Returns:
            The return value of the function.

        Raises:
            CircuitBreakerOpen: If the circuit breaker is open.
            Exception: The last exception if all retries are exhausted.
        """
        await self._check_circuit()

        last_exception: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 1):
            try:
                result = await func(*args, **kwargs)
                await self._record_success()
                return result

            except self._retryable_exceptions as exc:
                last_exception = exc
                await self._record_failure()

                if attempt == self._max_retries:
                    logger.error(
                        "All %d retries exhausted for %s [%s]: %s",
                        self._max_retries,
                        func.__name__,
                        self._broker_name,
                        str(exc),
                    )
                    raise

                delay = min(
                    self._base_delay * (self._backoff_factor ** (attempt - 1)),
                    self._max_delay,
                )

                logger.warning(
                    "Retry %d/%d for %s [%s] failed (%s), retrying in %.1fs",
                    attempt,
                    self._max_retries,
                    func.__name__,
                    self._broker_name,
                    str(exc),
                    delay,
                )

                await asyncio.sleep(delay)

                # Re-check circuit before next attempt
                await self._check_circuit()

        # Should not reach here, but satisfy type checker
        raise last_exception  # type: ignore[misc]

    async def reset(self) -> None:
        """Manually reset the circuit breaker to closed state."""
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._last_failure_time = 0.0
            logger.info("Circuit breaker for %s manually reset to CLOSED", self._broker_name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _check_circuit(self) -> None:
        """Check the circuit breaker state and raise if open."""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return

            if self._state == CircuitState.HALF_OPEN:
                # Allow one probe request through
                return

            # State is OPEN — check if cooldown has elapsed
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
                logger.info(
                    "Circuit breaker for %s transitioning to HALF_OPEN — "
                    "allowing probe request",
                    self._broker_name,
                )
                return

            remaining = self._cooldown_seconds - elapsed
            raise CircuitBreakerOpen(self._broker_name, remaining)

    async def _record_success(self) -> None:
        """Record a successful call and close the circuit if needed."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.info(
                    "Circuit breaker for %s closing — probe request succeeded",
                    self._broker_name,
                )
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0

    async def _record_failure(self) -> None:
        """Record a failed call and potentially open the circuit."""
        async with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning(
                    "Circuit breaker for %s re-opening — probe request failed",
                    self._broker_name,
                )
                return

            if self._consecutive_failures >= self._failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    "Circuit breaker for %s OPENED after %d consecutive failures — "
                    "cooldown %.0fs",
                    self._broker_name,
                    self._consecutive_failures,
                    self._cooldown_seconds,
                )
