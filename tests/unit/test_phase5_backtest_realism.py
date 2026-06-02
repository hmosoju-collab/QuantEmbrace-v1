from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SERVICES = _REPO / "services"
if str(_SERVICES) not in sys.path:
    sys.path.insert(0, str(_SERVICES))

from shared.models.signal import Direction, Signal
from strategy_engine.backtesting.backtester import Backtester, IndianCostModel
from strategy_engine.strategies.base_strategy import Bar, BaseStrategy


class ScriptedStrategy(BaseStrategy):
    def __init__(self, signals_by_bar: dict[int, Signal]) -> None:
        super().__init__(name="phase5_scripted", symbols=["TEST"], market="NSE")
        self._signals_by_bar = signals_by_bar
        self._bar_index = -1

    async def on_tick(self, symbol: str, price: float, volume: int, timestamp: datetime) -> None:
        return None

    async def on_bar(self, bar: Bar) -> None:
        self._bar_index += 1

    async def generate_signal(self) -> Optional[Signal]:
        return self._signals_by_bar.get(self._bar_index)


def _signal(direction: Direction = Direction.BUY, quantity: int = 10) -> Signal:
    return Signal(
        symbol="TEST",
        market="NSE",
        direction=direction,
        quantity=quantity,
        confidence=1.0,
        strategy_name="phase5_scripted",
        price_at_signal=100.0,
        stop_loss=95.0 if direction == Direction.BUY else None,
    )


def _bar(index: int, close: float, *, open_: float | None = None, low: float | None = None, high: float | None = None, volume: int = 10_000) -> Bar:
    ts = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    return Bar(
        symbol="TEST",
        market="NSE",
        open=open_ if open_ is not None else close,
        high=high if high is not None else close + 1.0,
        low=low if low is not None else close - 1.0,
        close=close,
        volume=volume,
        timestamp=ts,
        interval="minute",
    )


@pytest.mark.asyncio
async def test_backtest_applies_slippage_costs_and_reports_them() -> None:
    strategy = ScriptedStrategy({0: _signal(Direction.BUY, quantity=10)})
    bt = Backtester(
        strategy,
        initial_capital=100_000,
        commission_pct=0.03,
        slippage_bps=10.0,
        spread_bps=20.0,
    )

    result = await bt.run([_bar(0, 100), _bar(1, 100), _bar(2, 110)])

    assert result.total_trades == 1
    assert result.trades[0].entry_price > 100.0
    assert result.total_costs > 0.0
    assert result.total_slippage > 0.0
    assert result.rejected_orders == 0
    assert result.lookahead_violations == 0
    assert "stress_2x_pct" in result.drawdown_stress


@pytest.mark.asyncio
async def test_backtest_rejects_order_above_liquidity_cap() -> None:
    strategy = ScriptedStrategy({0: _signal(Direction.BUY, quantity=1000)})
    bt = Backtester(
        strategy,
        initial_capital=100_000,
        commission_pct=0.0,
        indian_cost_model=IndianCostModel(enabled=False),
        max_order_bar_volume_pct=1.0,
    )

    result = await bt.run([_bar(0, 100, volume=10_000), _bar(1, 100, volume=100)])

    assert result.total_trades == 0
    assert result.rejected_orders == 1


@pytest.mark.asyncio
async def test_gap_stop_fills_at_worse_open_not_stop_price() -> None:
    strategy = ScriptedStrategy({0: _signal(Direction.BUY, quantity=10)})
    bt = Backtester(
        strategy,
        initial_capital=100_000,
        commission_pct=0.0,
        indian_cost_model=IndianCostModel(enabled=False),
        gap_stop_behavior=True,
    )

    bars = [
        _bar(0, 100),
        _bar(1, 100),
        _bar(2, 91, open_=90, low=89, high=92),
    ]
    result = await bt.run(bars)

    assert result.total_trades == 1
    assert result.trades[0].exit_reason == "stop_loss_gap"
    assert result.trades[0].exit_price == 90


@pytest.mark.asyncio
async def test_signal_fills_on_next_bar_without_lookahead() -> None:
    strategy = ScriptedStrategy({0: _signal(Direction.BUY, quantity=10)})
    bt = Backtester(
        strategy,
        initial_capital=100_000,
        commission_pct=0.0,
        indian_cost_model=IndianCostModel(enabled=False),
    )

    bars = [_bar(0, 100), _bar(1, 101), _bar(2, 102)]
    result = await bt.run(bars)

    assert result.total_trades == 1
    assert result.trades[0].entry_time == bars[1].timestamp
    assert result.lookahead_violations == 0
