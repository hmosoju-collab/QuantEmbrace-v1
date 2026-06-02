from __future__ import annotations

import asyncio

import pytest

from shared.zerodha.rate_limiter import (
    EndpointClass,
    Priority,
    ZerodhaRateLimitExceeded,
    ZerodhaRateLimiter,
)


@pytest.mark.asyncio
async def test_quote_endpoint_is_limited_to_one_immediate_request() -> None:
    limiter = ZerodhaRateLimiter()

    await limiter.acquire(Priority.MEDIUM, EndpointClass.QUOTE)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            limiter.acquire(Priority.MEDIUM, EndpointClass.QUOTE),
            timeout=0.02,
        )


@pytest.mark.asyncio
async def test_non_critical_traffic_preserves_reserved_control_capacity() -> None:
    limiter = ZerodhaRateLimiter(critical_reserved_tokens=2)

    for _ in range(8):
        await limiter.acquire(Priority.MEDIUM, EndpointClass.OTHER)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            limiter.acquire(Priority.MEDIUM, EndpointClass.OTHER),
            timeout=0.02,
        )

    await limiter.acquire(Priority.CRITICAL, EndpointClass.ORDER_CONTROL)


@pytest.mark.asyncio
async def test_order_daily_cap_fails_closed() -> None:
    limiter = ZerodhaRateLimiter(max_orders_per_day=1)

    await limiter.acquire(Priority.HIGH, EndpointClass.ORDER_PLACE)

    with pytest.raises(ZerodhaRateLimitExceeded):
        await limiter.acquire(Priority.HIGH, EndpointClass.ORDER_PLACE)
