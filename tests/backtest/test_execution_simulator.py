"""Unit tests for the execution simulator + NSE cost/slippage model (Phase AWS-BT-6).

Covers: long P&L · short P&L · costs applied · slippage applied · signed-quantity
invariant · partial-fill accounting · net-edge rejection · no broker calls.
Plus one end-to-end check (adapter → replay → Backtester) asserting
``lookahead_violations == 0`` with costs and slippage applied.

Backtest-only: no broker, no AWS, no Kafka.

Run:  python -m pytest tests/backtest/test_execution_simulator.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

import backtesting.execution_simulator as exec_mod  # noqa: E402
from backtesting.execution_simulator import (  # noqa: E402
    LIQUID,
    ExecutionConfig,
    ExecutionSimulator,
)
from shared.models.signal import Direction  # noqa: E402
from strategy_engine.backtesting.backtester import IndianCostModel  # noqa: E402

FRICTIONLESS = ExecutionConfig(
    enable_statutory_costs=False,
    brokerage_pct=0.0,
    slippage_bps_liquid=0.0,
    slippage_bps_mid=0.0,
    slippage_bps_illiquid=0.0,
    spread_bps=0.0,
    min_net_edge_pct=0.0,
)


# ── P&L ──────────────────────────────────────────────────────────────────────


def test_long_pnl_correct():
    s = ExecutionSimulator(FRICTIONLESS)
    s.submit("RELIANCE", Direction.BUY, 10, 100.0)
    s.submit("RELIANCE", Direction.SELL, 10, 110.0)
    assert round(s.realized_pnl, 6) == 100.0  # (110-100)*10
    assert s.net_signed_quantity("RELIANCE") == 0


def test_short_pnl_correct():
    s = ExecutionSimulator(FRICTIONLESS)
    s.submit("RELIANCE", Direction.SELL, 10, 110.0)  # open short
    s.submit("RELIANCE", Direction.BUY, 10, 100.0)   # cover
    assert round(s.realized_pnl, 6) == 100.0  # (110-100)*10
    assert s.net_signed_quantity("RELIANCE") == 0


def test_costs_applied():
    s = ExecutionSimulator(ExecutionConfig(min_net_edge_pct=0.0))  # full NSE stack on
    s.submit("RELIANCE", Direction.BUY, 10, 100.0)
    s.submit("RELIANCE", Direction.SELL, 10, 110.0)
    assert s.total_costs > 0.0           # brokerage + STT + exchange + SEBI + GST + stamp
    assert s.realized_pnl < 100.0        # gross 100 reduced by costs + slippage
    cb = s.fills[0].costs
    assert cb.brokerage > 0 and cb.exchange_txn > 0 and cb.gst > 0
    assert s.fills[1].costs.stt > 0      # STT on the sell leg


def test_slippage_applied():
    s = ExecutionSimulator(ExecutionConfig(slippage_bps_liquid=5.0, spread_bps=4.0, enable_statutory_costs=False, brokerage_pct=0.0))
    buy = s.submit("RELIANCE", Direction.BUY, 10, 100.0, tier=LIQUID)
    sell = s.submit("RELIANCE", Direction.SELL, 10, 100.0, tier=LIQUID)
    assert buy.fill_price > 100.0        # buyer pays up
    assert sell.fill_price < 100.0       # seller receives less
    assert s.total_slippage > 0.0


def test_signed_quantity_invariant():
    s = ExecutionSimulator(FRICTIONLESS)
    s.submit("RELIANCE", Direction.BUY, 10, 100.0)
    assert s.net_signed_quantity("RELIANCE") == 10
    s.submit("RELIANCE", Direction.SELL, 4, 101.0)
    assert s.net_signed_quantity("RELIANCE") == 6
    s.submit("RELIANCE", Direction.SELL, 6, 102.0)
    assert s.net_signed_quantity("RELIANCE") == 0
    # Invariant: position == net of signed fills.
    net = sum(f.filled_qty if f.side == Direction.BUY else -f.filled_qty for f in s.fills)
    assert net == s.net_signed_quantity("RELIANCE")


def test_partial_fill_accounting():
    cfg = ExecutionConfig(allow_partial_fills=True, enable_statutory_costs=False, brokerage_pct=0.0,
                          slippage_bps_liquid=0.0, spread_bps=0.0)
    s = ExecutionSimulator(cfg)
    fill = s.submit("RELIANCE", Direction.BUY, 10, 100.0, fill_ratio=0.4)
    assert fill.filled_qty == 4 and fill.requested_qty == 10
    assert s.net_signed_quantity("RELIANCE") == 4
    assert round(s.turnover, 6) == 400.0


def test_rejected_if_net_edge_too_small():
    s = ExecutionSimulator(ExecutionConfig(min_net_edge_pct=0.5))
    tight = s.submit("RELIANCE", Direction.BUY, 10, 100.0, target=100.2)  # 0.2% gross
    assert tight.rejected is True and tight.filled_qty == 0
    assert s.net_signed_quantity("RELIANCE") == 0  # no position change
    wide = s.submit("RELIANCE", Direction.BUY, 10, 100.0, target=105.0)   # 5% gross
    assert wide.rejected is False and wide.filled_qty == 10


def test_no_broker_call_possible():
    src = Path(exec_mod.__file__).read_text().lower()
    forbidden = ["kiteconnect", "alpaca", "place_order", "submit_order", "broker_client", "import boto3"]
    present = [t for t in forbidden if t in src]
    assert present == [], f"execution simulator must not reference brokers: {present}"


# ── end-to-end: replay engine + adapter + Backtester ─────────────────────────


def test_end_to_end_costs_and_no_lookahead():
    from backtesting.replay_engine import Candle, CandleReplayEngine, DataFrameBarSource, ReplayConfig
    from backtesting.strategy_adapter import get_adapter

    IST = "Asia/Kolkata"
    base = pd.Timestamp("2020-06-01 09:20", tz=IST)
    prices = [120 - i for i in range(20)] + [101 + 3 * i for i in range(25)]  # V-shape → cross
    candles = [
        Candle("RELIANCE", "NSE", "EQ", "5m", base + pd.Timedelta(minutes=5 * i),
               p, p + 1, p - 1, p, 5000)
        for i, p in enumerate(prices)
    ]
    adapter = get_adapter("momentum")
    eng = CandleReplayEngine(
        DataFrameBarSource(candles),
        ReplayConfig(timeframes=["5m"], partition_by="symbol", market_hours_filter=False),
    )
    results = eng.run_with_backtester(
        lambda: adapter.build_strategy(["RELIANCE"], short_window=3, long_window=14, min_confidence=0.0),
        backtester_kwargs={"slippage_bps": 5.0, "spread_bps": 4.0, "commission_pct": 0.03},
    )
    assert results
    res = next(iter(results.values()))
    assert res.lookahead_violations == 0       # no-lookahead end-to-end
    assert res.total_trades >= 1               # the crossover produced a trade
    assert res.total_costs > 0.0               # NSE costs applied
    assert res.total_slippage > 0.0            # slippage applied


# ── cost-model: exchange-rate fix + segment profiles (audit follow-up) ──────────


def test_exchange_charge_is_nse_oct2024_rate():
    assert IndianCostModel().exchange_txn_pct == 0.00297
    assert IndianCostModel.delivery().exchange_txn_pct == 0.00297


def test_intraday_profile_stt_sell_only_and_version():
    cfg = ExecutionConfig(brokerage_pct=0.0, slippage_bps_liquid=0.0, spread_bps=0.0)
    s = ExecutionSimulator(cfg)
    buy = s.submit("RELIANCE", Direction.BUY, 10, 100.0)
    sell = s.submit("RELIANCE", Direction.SELL, 10, 110.0)
    assert buy.costs.stt == 0.0          # intraday: no STT on the buy leg
    assert sell.costs.stt > 0.0          # STT on the sell leg
    assert cfg.indian_costs.cost_model_version == "in-eq-intraday-2024.10"


def test_delivery_profile_stt_both_legs_and_stamp():
    cfg = ExecutionConfig(
        brokerage_pct=0.0,
        indian_costs=IndianCostModel.delivery(),
        slippage_bps_liquid=0.0,
        spread_bps=0.0,
    )
    s = ExecutionSimulator(cfg)
    buy = s.submit("RELIANCE", Direction.BUY, 10, 100.0)    # value 1000
    sell = s.submit("RELIANCE", Direction.SELL, 10, 110.0)  # value 1100
    assert round(buy.costs.stt, 6) == round(1000.0 * 0.001, 6)    # delivery STT 0.1% on buy
    assert round(sell.costs.stt, 6) == round(1100.0 * 0.001, 6)   # and on sell
    assert round(buy.costs.stamp, 6) == round(1000.0 * 0.00015, 6)  # delivery stamp 0.015%
    assert cfg.indian_costs.cost_model_version == "in-eq-delivery-2024.10"


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
