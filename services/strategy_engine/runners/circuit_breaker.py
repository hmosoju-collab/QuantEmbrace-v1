"""
CircuitBreaker — dual-threshold circuit breaker for per-strategy failure isolation.

Implements the CLOSED / OPEN / HALF_OPEN state machine defined in ADR-013 §6.3.

Thresholds (both configurable per strategy via DynamoDB strategy-config):
    consecutive_threshold:  Open if N consecutive errors.   Default: 5
    rate_threshold:         Open if N errors in 5 minutes.  Default: 10

Either threshold triggers OPEN — whichever is hit first.

Recovery:
    OPEN stays for at least reset_seconds (default: 300s = 5 minutes).
    After reset_seconds, state transitions to HALF_OPEN.
    HALF_OPEN: next dispatch is a test call. On success → CLOSED. On failure → OPEN again.
    3 consecutive successes in HALF_OPEN → CLOSED (configurable via success_threshold).

Manual reset:
    Operator sets circuit_breaker_reset=True in DynamoDB strategy-config.
    StrategyConfigLoader reads this flag and calls reset() on the CircuitBreaker.
    reset() transitions directly to CLOSED regardless of current state.

CloudWatch:
    Callers (StrategyRunner) are responsible for emitting the CircuitBreakerState metric.
    This class exposes state and open_at for metric construction.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class CircuitState(str, Enum):
    """Circuit breaker state."""
    CLOSED    = "CLOSED"
    OPEN      = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class CircuitBreaker:
    """
    Dual-threshold circuit breaker.

    Thread-safety:
        Not thread-safe. Designed for single-threaded async use within a single
        StrategyRunner. Each strategy has its own CircuitBreaker instance.

    Args:
        strategy_name:         Strategy name — used for logging/metrics dimensions.
        consecutive_threshold: Open after N consecutive errors.
        rate_threshold:        Open after N errors in rate_window_seconds.
        rate_window_seconds:   Rolling error rate window.
        reset_seconds:         Minimum OPEN duration before transitioning to HALF_OPEN.
        success_threshold:     Consecutive successes in HALF_OPEN before CLOSED.
    """

    strategy_name:          str
    consecutive_threshold:  int   = 5
    rate_threshold:         int   = 10
    rate_window_seconds:    float = 300.0   # 5 minutes
    reset_seconds:          float = 300.0   # 5 minutes open before HALF_OPEN
    success_threshold:      int   = 3       # HALF_OPEN successes needed → CLOSED

    # ── Runtime state (not part of config) ───────────────────────────────────
    _state:               CircuitState = field(default=CircuitState.CLOSED, init=False)
    _consecutive_errors:  int          = field(default=0, init=False)
    _half_open_successes: int          = field(default=0, init=False)
    _open_at:             Optional[float] = field(default=None, init=False)
    # Rolling window: deque of error timestamps (epoch float)
    _error_times:         deque         = field(default_factory=deque, init=False)

    def __post_init__(self) -> None:
        self._error_times = deque()

    # ── Public state accessors ────────────────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        """Current circuit breaker state. Transitions OPEN → HALF_OPEN lazily on read."""
        self._check_half_open_transition()
        return self._state

    @property
    def is_open(self) -> bool:
        """True if the circuit is OPEN or transitioning (should not dispatch)."""
        return self.state == CircuitState.OPEN

    @property
    def is_closed(self) -> bool:
        """True if the circuit is CLOSED (normal operation)."""
        return self.state == CircuitState.CLOSED

    @property
    def open_at(self) -> Optional[float]:
        """Epoch timestamp when the circuit last opened. None if never opened."""
        return self._open_at

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    @property
    def errors_in_window(self) -> int:
        """Count of errors within the last rate_window_seconds."""
        self._evict_old_errors()
        return len(self._error_times)

    # ── State machine ─────────────────────────────────────────────────────────

    def record_success(self) -> None:
        """
        Record a successful dispatch outcome.

        CLOSED:    Reset consecutive error counter.
        HALF_OPEN: Increment success counter; if success_threshold reached → CLOSED.
        OPEN:      No-op (callers shouldn't dispatch while open, but guard anyway).
        """
        self._evict_old_errors()

        if self._state == CircuitState.CLOSED:
            self._consecutive_errors = 0
            # Note: we do NOT clear the rate window on success — isolated failures
            # should still count even if interspersed with successes.

        elif self._state == CircuitState.HALF_OPEN:
            self._half_open_successes += 1
            if self._half_open_successes >= self.success_threshold:
                self._transition_to_closed()

    def record_failure(self) -> bool:
        """
        Record a failed dispatch outcome.

        Returns:
            True if this failure caused the circuit to OPEN (for caller to emit alarm).
            False if the circuit was already OPEN or not yet at threshold.
        """
        now = time.monotonic()
        self._consecutive_errors += 1
        self._error_times.append(now)
        self._evict_old_errors()

        if self._state == CircuitState.OPEN:
            return False  # already open

        if self._state == CircuitState.HALF_OPEN:
            # Single failure in HALF_OPEN → re-open immediately
            self._transition_to_open()
            return True

        # CLOSED: check both thresholds
        rate_exceeded        = len(self._error_times) >= self.rate_threshold
        consecutive_exceeded = self._consecutive_errors >= self.consecutive_threshold

        if rate_exceeded or consecutive_exceeded:
            self._transition_to_open()
            return True

        return False

    def reset(self) -> None:
        """
        Manual reset — transition directly to CLOSED.

        Called by StrategyConfigLoader when operator sets circuit_breaker_reset=True
        in DynamoDB. Takes effect within one config refresh cycle (≤60s).
        """
        self._transition_to_closed()

    def update_thresholds(
        self,
        consecutive_threshold: Optional[int] = None,
        rate_threshold: Optional[int] = None,
    ) -> None:
        """
        Update thresholds from DynamoDB strategy-config hot-reload.

        Does not affect current state — threshold changes apply to future errors only.
        """
        if consecutive_threshold is not None:
            self.consecutive_threshold = consecutive_threshold
        if rate_threshold is not None:
            self.rate_threshold = rate_threshold

    # ── Internal transitions ──────────────────────────────────────────────────

    def _transition_to_open(self) -> None:
        self._state   = CircuitState.OPEN
        self._open_at = time.monotonic()
        self._half_open_successes = 0

    def _transition_to_half_open(self) -> None:
        self._state = CircuitState.HALF_OPEN
        self._half_open_successes = 0

    def _transition_to_closed(self) -> None:
        self._state               = CircuitState.CLOSED
        self._consecutive_errors  = 0
        self._half_open_successes = 0
        self._open_at             = None
        self._error_times.clear()

    def _check_half_open_transition(self) -> None:
        """Lazily transition OPEN → HALF_OPEN when reset_seconds have elapsed."""
        if (
            self._state == CircuitState.OPEN
            and self._open_at is not None
            and (time.monotonic() - self._open_at) >= self.reset_seconds
        ):
            self._transition_to_half_open()

    def _evict_old_errors(self) -> None:
        """Remove error timestamps outside the rolling rate window."""
        cutoff = time.monotonic() - self.rate_window_seconds
        while self._error_times and self._error_times[0] < cutoff:
            self._error_times.popleft()
