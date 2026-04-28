"""
Common utility functions for QuantEmbrace services.

Provides timestamp formatting, order ID generation, and a retry decorator
with exponential backoff for resilient operations on AWS ECS Fargate.
"""

from __future__ import annotations

import asyncio
import functools
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional, TypeVar

from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="shared")

F = TypeVar("F", bound=Callable[..., Any])


def utc_now() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return utc_now().isoformat()


def epoch_ms() -> int:
    """Return the current time as milliseconds since Unix epoch."""
    return int(time.time() * 1000)


def format_timestamp(dt: datetime, fmt: str = "%Y-%m-%dT%H:%M:%S.%fZ") -> str:
    """
    Format a datetime object to a string.

    Args:
        dt: The datetime to format.
        fmt: strftime format string.

    Returns:
        Formatted timestamp string.
    """
    return dt.strftime(fmt)


def generate_order_id(prefix: str = "QE") -> str:
    """
    Generate a unique, idempotent-safe order ID.

    Format: {prefix}-{timestamp_ms}-{uuid4_short}
    Example: QE-1700000000000-a1b2c3d4

    Args:
        prefix: Order ID prefix for identification.

    Returns:
        Unique order identifier string.
    """
    ts = epoch_ms()
    short_uuid = uuid.uuid4().hex[:8]
    return f"{prefix}-{ts}-{short_uuid}"


def generate_correlation_id() -> str:
    """Generate a UUID4 correlation ID for request tracing."""
    return str(uuid.uuid4())


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[F], F]:
    """
    Synchronous retry decorator with exponential backoff.

    Args:
        max_attempts: Maximum number of attempts (including first).
        base_delay: Initial delay in seconds between retries.
        max_delay: Maximum delay cap in seconds.
        exponential_base: Multiplier for exponential backoff.
        retryable_exceptions: Tuple of exception types to retry on.

    Returns:
        Decorated function with retry behavior.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exception = exc
                    if attempt == max_attempts:
                        logger.error(
                            "All %d attempts exhausted for %s: %s",
                            max_attempts,
                            func.__name__,
                            str(exc),
                        )
                        raise
                    delay = min(
                        base_delay * (exponential_base ** (attempt - 1)),
                        max_delay,
                    )
                    logger.warning(
                        "Attempt %d/%d for %s failed (%s), retrying in %.1fs",
                        attempt,
                        max_attempts,
                        func.__name__,
                        str(exc),
                        delay,
                    )
                    time.sleep(delay)
            raise last_exception  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator


def async_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[F], F]:
    """
    Asynchronous retry decorator with exponential backoff.

    Same parameters as `retry` but for async functions.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exception = exc
                    if attempt == max_attempts:
                        logger.error(
                            "All %d async attempts exhausted for %s: %s",
                            max_attempts,
                            func.__name__,
                            str(exc),
                        )
                        raise
                    delay = min(
                        base_delay * (exponential_base ** (attempt - 1)),
                        max_delay,
                    )
                    logger.warning(
                        "Async attempt %d/%d for %s failed (%s), retrying in %.1fs",
                        attempt,
                        max_attempts,
                        func.__name__,
                        str(exc),
                        delay,
                    )
                    await asyncio.sleep(delay)
            raise last_exception  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator


def chunk_list(lst: list[Any], chunk_size: int) -> list[list[Any]]:
    """
    Split a list into chunks of the specified size.

    Useful for batching DynamoDB writes or S3 uploads.

    Args:
        lst: The list to chunk.
        chunk_size: Maximum items per chunk.

    Returns:
        List of list chunks.
    """
    return [lst[i : i + chunk_size] for i in range(0, len(lst), chunk_size)]
