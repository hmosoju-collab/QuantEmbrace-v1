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
import pytest

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


def test_get_adapter_raises_for_unknown_name():
    with pytest.raises(ValueError, match="Unknown strategy"):
        get_adapter("doesnotexist")


def test_list_adapters_returns_all_six():
    names = list_adapters()
    assert set(names) == {"vwap_reversion", "momentum", "orb", "trend_15m", "preclose", "scalp_1m"}
    assert len(names) == 6


def test_paper_only_adapters_filterable():
    """Only scalp_1m is paper_only; 5 adapters are eligible for live comparison."""
    live_eligible = [n for n in list_adapters() if not get_adapter(n).paper_only]
    paper_only_names = [n for n in list_adapters() if get_adapter(n).paper_only]
    assert paper_only_names == ["scalp_1m"]
    assert len(live_eligible) == 5
    assert "scalp_1m" not in live_eligible


def test_enrich_non_scalp_preserves_paper_trade_flag():
    """A non-paper-only adapter's enrich() does not override paper_trade."""
    from shared.models.signal import Direction, Signal

    mom = get_adapter("momentum")
    assert mom.paper_only is False
    raw = Signal(
        symbol="INFY", market="NSE", direction=Direction.BUY, quantity=1, confidence=0.85,
        strategy_name="momentum", price_at_signal=1500.0, stop_loss=1485.0, take_profit=1530.0,
        paper_trade=False,
    )
    enriched = mom.enrich(raw, data_version="snap-test")
    assert enriched.get("paper_trade") is not True
    assert enriched["strategy_version"] == "momentum@2.0"
    assert enriched["data_version"] == "snap-test"
    assert enriched["metadata"]["backtest"] is True


def test_enrich_tee_metadata_completeness():
    """enrich() includes all TEE fields the Trade Exit Engine needs."""
    from shared.models.signal import Direction, Signal

    a = get_adapter("momentum")
    raw = Signal(
        symbol="TCS", market="NSE", direction=Direction.BUY, quantity=5, confidence=0.8,
        strategy_name="momentum", price_at_signal=3500.0, stop_loss=3465.0, take_profit=3570.0,
        paper_trade=True,
    )
    e = a.enrich(raw, data_version="v2")
    tee = e["metadata"]["tee"]
    assert tee["strategy"] == "momentum"
    assert tee["entry_price"] == 3500.0
    assert tee["stop_loss"] == 3465.0
    assert tee["take_profit"] == 3570.0
    assert abs(tee["risk_per_unit"] - 35.0) < 1e-6   # 3500 - 3465
    assert abs(tee["rr_target"] - 2.0) < 0.1         # 70 / 35
    assert tee["product_type"] == "MIS"
    assert tee["exit_policy_version"] == "tee@1.0"
    assert e["metadata"]["backtest"] is True


def test_no_broker_calls_in_strategy_adapter():
    """strategy_adapter.py must not reference broker APIs."""
    import backtesting.strategy_adapter as sa_mod

    forbidden = ["kiteconnect", "alpaca", "place_order", "zerodhabroker", "submit_order"]
    src = Path(sa_mod.__file__).read_text().lower()
    present = [t for t in forbidden if t in src]
    assert present == [], f"strategy_adapter.py must not reference brokers: {present}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
