"""Unit tests for the strategy adapters (Phase AWS-BT-5).

Covers: VWAP setup-gating · ORB opening-range-only · momentum 5m · trend_15m 15m ·
preclose window · scalp low-edge rejection · no future data · scalp paper-only.

Backtest-only: reuses the production strategy classes in fast mode (no broker, no
Kafka, no AWS).

Run:  python -m pytest tests/backtest/test_strategy_adapter.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.replay_engine import Candle  # noqa: E402
from backtesting.strategy_adapter import get_adapter, list_adapters  # noqa: E402

IST = "Asia/Kolkata"


# ── helpers ──────────────────────────────────────────────────────────────────


def _candle(symbol: str, ts: pd.Timestamp, price: float, interval: str) -> Candle:
    return Candle(symbol, "NSE", "EQ", interval, ts, price, price + 1, price - 1, price, 5000)


def _series(start: str, n: int, step_min: int, fn, *, interval: str, symbol: str = "RELIANCE"):
    base = pd.Timestamp(start, tz=IST)
    return [_candle(symbol, base + pd.Timedelta(minutes=step_min * i), fn(i), interval) for i in range(n)]


def _run(coro):
    return asyncio.run(coro)


# ── tests ──────────────────────────────────────────────────────────────────────


def test_vwap_emits_only_with_valid_setup():
    # Flat series → price == VWAP → no mean-reversion deviation → no signal.
    flat = _series("2020-06-01 10:00", 40, 1, lambda i: 100.0, interval="1m")
    sigs = _run(get_adapter("vwap_reversion").collect_signals(flat, ["RELIANCE"], data_version="s"))
    assert sigs == []


def test_orb_uses_first_15m_opening_range_only():
    # Bars only within the 09:15–09:30 opening-range window → ORB forms the range,
    # it does not trade during/while forming it.
    orb_window = _series("2020-06-01 09:15", 15, 1, lambda i: 100.0, interval="1m")
    sigs = _run(get_adapter("orb").collect_signals(orb_window, ["RELIANCE"], data_version="s"))
    assert sigs == []


def test_momentum_uses_5m_candles():
    a = get_adapter("momentum")
    assert a.replay_interval == "5m"
    # V-shape on 5m candles → golden cross → BUY (long_window ≥ atr_period to warm up).
    prices = [120 - i for i in range(20)] + [101 + 3 * i for i in range(25)]
    cs = _series("2020-06-01 09:20", len(prices), 5, lambda i: prices[i], interval="5m")
    sigs = _run(
        a.collect_signals(cs, ["RELIANCE"], data_version="snap1", short_window=3, long_window=14, min_confidence=0.0)
    )
    assert sigs, "momentum should emit on a clean 5m crossover"
    s = sigs[0]
    assert s["direction"] == "BUY"
    assert s["strategy_version"] == "momentum@2.0"
    assert s["data_version"] == "snap1"
    assert s["metadata"]["tee"]["product_type"] == "MIS"
    assert s["metadata"]["tee"]["stop_loss"] is not None


def test_trend_15m_uses_15m_candles():
    a = get_adapter("trend_15m")
    assert a.replay_interval == "15m"
    bars = _series("2020-06-01 09:15", 12, 15, lambda i: 100.0, interval="15m")
    sigs = _run(a.collect_signals(bars, ["RELIANCE"], data_version="s"))
    assert isinstance(sigs, list)  # runs on 15m candles without error


def test_preclose_respects_window():
    # Production pre-close fire window is 14:45–15:10 IST. A 10:00 bar is outside it.
    outside = _series("2020-06-01 10:00", 8, 5, lambda i: 100.0 + i, interval="5m")
    sigs = _run(get_adapter("preclose").collect_signals(outside, ["RELIANCE"], data_version="s"))
    assert sigs == []


def test_scalp_v2_rejects_low_edge_trades():
    a = get_adapter("scalp_1m")
    assert a.strategy_version == "scalp_1m@2.0"  # hardened v2
    # Flat, zero-edge 1m series → fails edge floors → rejected (no signal).
    flat = _series("2020-06-01 10:00", 40, 1, lambda i: 100.0, interval="1m")
    sigs = _run(a.collect_signals(flat, ["RELIANCE"], data_version="s"))
    assert sigs == []


def test_no_strategy_sees_future_data():
    # Concrete: momentum emits, and its signal is stamped to a past bar.
    prices = [120 - i for i in range(20)] + [101 + 3 * i for i in range(25)]
    cs = _series("2020-06-01 09:20", len(prices), 5, lambda i: prices[i], interval="5m")
    last_ts = cs[-1].timestamp.isoformat()
    sigs = _run(
        get_adapter("momentum").collect_signals(
            cs, ["RELIANCE"], data_version="s", short_window=3, long_window=14, min_confidence=0.0
        )
    )
    assert sigs
    assert all(s["generated_at"] <= last_ts for s in sigs)
    # Generic: no adapter ever emits a signal stamped after the last fed candle.
    for name in list_adapters():
        a = get_adapter(name)
        bars = _series("2020-06-01 10:00", 40, 1, lambda i: 100.0, interval=a.replay_interval)
        out = _run(a.collect_signals(bars, ["RELIANCE"], data_version="s"))
        assert all(s["generated_at"] <= bars[-1].timestamp.isoformat() for s in out)


def test_scalp_remains_paper_only():
    from shared.models.signal import Direction, Signal

    scalp = get_adapter("scalp_1m")
    assert scalp.paper_only is True
    assert get_adapter("momentum").paper_only is False
    # Enriched scalp signals are always paper_trade=True, even from a non-paper Signal.
    raw = Signal(
        symbol="RELIANCE", market="NSE", direction=Direction.BUY, quantity=1, confidence=0.9,
        strategy_name="scalp", price_at_signal=100.0, stop_loss=99.0, take_profit=102.0,
        paper_trade=False,
    )
    enriched = scalp.enrich(raw, data_version="s")
    assert enriched["paper_trade"] is True
    assert enriched["metadata"]["paper_trade"] is True


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
