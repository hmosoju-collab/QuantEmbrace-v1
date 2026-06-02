from __future__ import annotations

from types import SimpleNamespace

import pytest

from execution_engine.polling.position_monitor import PositionMonitor


class _RateLimiter:
    async def acquire(self, *args, **kwargs) -> None:
        return None


class _Broker:
    def __init__(self, positions: list[dict]) -> None:
        self._positions = positions

    async def get_positions(self) -> list[dict]:
        return self._positions


class _OrderManager:
    def __init__(self, positions: list[SimpleNamespace]) -> None:
        self._positions = positions

    async def get_all_open_positions(self) -> list[SimpleNamespace]:
        return self._positions


class _KillSwitch:
    def __init__(self) -> None:
        self.activations: list[dict[str, str]] = []

    async def activate(self, *, reason: str, activated_by: str) -> None:
        self.activations.append(
            {"reason": reason, "activated_by": activated_by}
        )


@pytest.mark.asyncio
async def test_position_drift_activates_kill_switch() -> None:
    kill_switch = _KillSwitch()
    monitor = PositionMonitor(
        zerodha=_Broker([{"symbol": "RELIANCE", "quantity": 15}]),
        order_manager=_OrderManager([SimpleNamespace(symbol="RELIANCE", quantity=10)]),
        rate_limiter=_RateLimiter(),
        kill_switch=kill_switch,
        settings=SimpleNamespace(),
    )

    await monitor._poll_cycle()

    assert len(kill_switch.activations) == 1
    assert kill_switch.activations[0]["activated_by"] == "position_monitor"
    assert "POSITION_DRIFT_DETECTED" in kill_switch.activations[0]["reason"]


@pytest.mark.asyncio
async def test_broker_empty_dynamo_open_position_activates_kill_switch() -> None:
    kill_switch = _KillSwitch()
    monitor = PositionMonitor(
        zerodha=_Broker([]),
        order_manager=_OrderManager([SimpleNamespace(symbol="INFY", quantity=25)]),
        rate_limiter=_RateLimiter(),
        kill_switch=kill_switch,
        settings=SimpleNamespace(),
    )

    await monitor._poll_cycle()

    assert len(kill_switch.activations) == 1
    assert "broker_empty_dynamo_open" in kill_switch.activations[0]["reason"]
