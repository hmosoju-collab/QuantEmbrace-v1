"""
Unit tests for the Week-1 entry-economics rebuild (committee report 2026-06-10).

Covers:
  1. Universal viability gate (strategies/_viability.py) — Standing Rules R1/R2.
  2. ORB v2 — wider stops with pct floor, breakout buffer, volume reject,
     09:30–11:30 window, min OR range pct, discriminating confidence,
     strategy-wide daily signal budget, 5m aggregation.
  3. VWAP reversion v2 — RR ≥ 1.5 gate, stop pct floor, direction-aware
     cooldown, per-(symbol, direction) daily budget, strategy-wide budget,
     10:15 IST start.
  4. PaperQualityGateValidator — market-prefixed strategy-name lookup
     (nse_vwap_reversion must match the unprefixed YAML key).
  5. SymbolTradeCountValidator — GLOBAL_DAILY_ENTRY_LIMIT_REACHED budget.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure services/ is on the path for imports
_ROOT = Path(__file__).parent
while not (_ROOT / "services").exists() and _ROOT != _ROOT.parent:
    _ROOT = _ROOT.parent
sys.path.insert(0, str(_ROOT / "services"))

from shared.models.signal import Direction, Signal
from strategy_engine.strategies._viability import (
    MIN_STOP_PCT,
    MIN_TARGET_COST_MULTIPLE,
    ROUND_TRIP_COST_PCT,
    check_signal_viability,
)
from strategy_engine.strategies.base_strategy import Bar
from strategy_engine.strategies.orb_strategy import ORBStrategy
from strategy_engine.strategies.vwap_reversion_strategy import VWAPReversionStrategy

from risk_engine.validators.paper_quality_gate_validator import (
    PaperQualityGateValidator,
    _threshold_for,
)
from risk_engine.validators.symbol_trade_count_validator import (
    SymbolTradeCountValidator,
)

_IST = timezone(timedelta(hours=5, minutes=30))


def _utc_for_ist(hour: int, minute: int) -> datetime:
    """UTC datetime whose IST wall-clock is hour:minute on a fixed date."""
    return datetime(2026, 6, 10, hour, minute, tzinfo=_IST).astimezone(timezone.utc)


def _bar(symbol: str, ist_h: int, ist_m: int, o: float, h: float, l: float,
         c: float, vol: int = 1000) -> Bar:
    return Bar(
        symbol=symbol, market="NSE", open=o, high=h, low=l, close=c,
        volume=vol, timestamp=_utc_for_ist(ist_h, ist_m),
    )


def _run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Universal viability gate
# ══════════════════════════════════════════════════════════════════════════════

def test_viability_accepts_cost_viable_trade():
    # price 1000, stop 5 (0.5%), tp 10 (1.0%) — clears both floors
    r = check_signal_viability(1000.0, 5.0, 10.0)
    assert r.viable
    assert r.reason == ""
    assert r.stop_pct == pytest.approx(0.5)
    assert r.target_pct == pytest.approx(1.0)
    assert r.net_edge_pct == pytest.approx(1.0 - ROUND_TRIP_COST_PCT)


def test_viability_rejects_stop_below_floor():
    # Session-15 ORB median stop was 0.163% — must be rejected
    r = check_signal_viability(1000.0, 1.63, 3.26)
    assert not r.viable
    assert r.reason.startswith("stop_below_floor")


def test_viability_rejects_target_below_cost_floor():
    # stop OK (0.5%) but target 0.45% < 2.5 × 0.20% cost
    r = check_signal_viability(1000.0, 5.0, 4.5)
    assert not r.viable
    assert r.reason.startswith("target_below_cost_floor")


def test_viability_boundary_values_pass():
    price = 1000.0
    stop = price * MIN_STOP_PCT / 100.0
    target = price * (MIN_TARGET_COST_MULTIPLE * ROUND_TRIP_COST_PCT) / 100.0
    r = check_signal_viability(price, stop, target)
    assert r.viable


def test_viability_rejects_invalid_inputs():
    assert not check_signal_viability(0.0, 5.0, 10.0).viable
    assert not check_signal_viability(1000.0, 0.0, 10.0).viable
    assert not check_signal_viability(1000.0, 5.0, -1.0).viable


def test_viability_metadata_fields():
    md = check_signal_viability(1000.0, 5.0, 10.0).to_metadata()
    assert set(md) == {
        "viability_stop_pct", "viability_target_pct", "viability_net_edge_pct",
    }


# ══════════════════════════════════════════════════════════════════════════════
# 2. ORB v2
# ══════════════════════════════════════════════════════════════════════════════

def _orb(**kwargs) -> ORBStrategy:
    defaults = dict(name="nse_orb_15m", symbols=["TEST"], market="NSE")
    defaults.update(kwargs)
    return ORBStrategy(**defaults)


async def _form_or(s: ORBStrategy, symbol: str = "TEST", lo: float = 1000.0,
                   hi: float = 1010.0, vol: int = 1000) -> None:
    """Feed 09:15–09:29 OR bars oscillating lo↔hi, then the 09:30 confirm bar."""
    for i in range(15):
        await s.on_bar(_bar(symbol, 9, 15 + i, lo, hi, lo, lo if i % 2 else hi, vol))
    await s.on_bar(_bar(symbol, 9, 30, lo, hi, lo, hi, vol))  # confirmation bar


def test_orb_emits_on_buffered_breakout_with_wide_stop():
    s = _orb()

    async def run():
        await _form_or(s)
        # buffer = 0.15 × 10 = 1.5 → BUY needs close > 1011.5; volume 3× avg
        await s.on_bar(_bar("TEST", 9, 35, 1011, 1012.5, 1011, 1012.0, 3000))
        return await s.generate_signal()

    sig = _run(run())
    assert sig is not None
    assert sig.direction == Direction.BUY
    # stop = max(|price − or_mid|=7, atr5=0 early session, 0.45% floor≈4.55) = 7
    assert sig.stop_loss == pytest.approx(1012.0 - 7.0, abs=0.01)
    # tp = 2R
    assert sig.take_profit == pytest.approx(1012.0 + 14.0, abs=0.01)
    assert (sig.stop_loss / sig.price_at_signal) < 1.0
    stop_pct = (sig.price_at_signal - sig.stop_loss) / sig.price_at_signal * 100
    assert stop_pct >= MIN_STOP_PCT


def test_orb_no_signal_inside_buffer():
    s = _orb()

    async def run():
        await _form_or(s)
        # close 1011.0 pokes the OR high but is inside the 1.5 buffer
        await s.on_bar(_bar("TEST", 9, 35, 1010, 1011.2, 1010, 1011.0, 3000))
        return await s.generate_signal()

    assert _run(run()) is None


def test_orb_rejects_when_volume_history_unavailable():
    s = _orb()

    async def run():
        await _form_or(s, vol=0)  # no volume history → avg_vol_10 = 0
        await s.on_bar(_bar("TEST", 9, 35, 1011, 1013, 1011, 1012.5, 0))
        return await s.generate_signal()

    assert _run(run()) is None  # old code waved this through (vol_ok=True)


def test_orb_no_signal_after_window_end():
    s = _orb()

    async def run():
        await _form_or(s)
        await s.on_bar(_bar("TEST", 12, 0, 1011, 1013, 1011, 1012.5, 3000))
        return await s.generate_signal()

    assert _run(run()) is None  # 12:00 IST > 11:30 window end


def test_orb_min_or_range_pct_blocks_dead_open():
    s = _orb()

    async def run():
        # OR range 2 on price ~1000 = 0.2% < 0.5% floor → never confirmed
        await _form_or(s, lo=1000.0, hi=1002.0)
        await s.on_bar(_bar("TEST", 9, 35, 1002, 1004, 1002, 1003.5, 3000))
        return await s.generate_signal()

    assert _run(run()) is None


def test_orb_chase_entry_rejected_by_confidence():
    s = _orb()

    async def run():
        await _form_or(s)
        # close 1018 = overshoot 0.8 × OR range → proximity 0, conf ≈ 0.575 < 0.65
        await s.on_bar(_bar("TEST", 9, 35, 1011, 1018.5, 1011, 1018.0, 3000))
        return await s.generate_signal()

    assert _run(run()) is None  # old formula scored this chase 1.0 (max size)


def test_orb_stop_floor_binds_when_or_mid_too_close():
    # OR 1993–2005 (range 12 = 0.6% of close, passes min range); or_mid = 1999.
    # Breakout close 2007 → |price − or_mid| = 8 = 0.398% < 0.45% floor →
    # the pct floor must carry the stop.
    s = _orb()

    async def run():
        await _form_or(s, lo=1993.0, hi=2005.0)
        await s.on_bar(_bar("TEST", 9, 35, 2006, 2007.5, 2006, 2007.0, 3000))
        return await s.generate_signal()

    sig = _run(run())
    assert sig is not None
    stop_pct = (sig.price_at_signal - sig.stop_loss) / sig.price_at_signal * 100
    assert stop_pct == pytest.approx(0.45, abs=0.01)


def test_orb_daily_signal_budget():
    s = _orb(max_signals_per_day=1, symbols=["AAA", "BBB"])

    async def run():
        await _form_or(s, symbol="AAA")
        await _form_or(s, symbol="BBB")
        await s.on_bar(_bar("AAA", 9, 35, 1011, 1013, 1011, 1012.5, 3000))
        first = await s.generate_signal()
        await s.on_bar(_bar("BBB", 9, 36, 1011, 1013, 1011, 1012.5, 3000))
        second = await s.generate_signal()
        return first, second

    first, second = _run(run())
    assert first is not None
    assert second is None  # budget of 1 spent


def test_orb_reset_daily_clears_budget_and_5m_state():
    s = _orb(max_signals_per_day=1)

    async def run():
        await _form_or(s)
        await s.on_bar(_bar("TEST", 9, 35, 1011, 1013, 1011, 1012.5, 3000))
        return await s.generate_signal()

    assert _run(run()) is not None
    s.reset_daily()
    assert s._signals_today == 0
    assert len(s._h5["TEST"]) == 0


def test_orb_5m_aggregation_folds_buckets():
    s = _orb()

    async def run():
        # 09:15–09:29 = 3 complete 5m buckets; closing each requires the next bucket's bar
        await _form_or(s)

    _run(run())
    # buckets 09:15–19 and 09:20–24 are closed by later bars; 09:25–29 closed by the 09:30 bar
    assert len(s._h5["TEST"]) == 3
    assert s._h5["TEST"][0] == pytest.approx(1010.0)
    assert s._l5["TEST"][0] == pytest.approx(1000.0)


# ══════════════════════════════════════════════════════════════════════════════
# 3. VWAP reversion v2
# ══════════════════════════════════════════════════════════════════════════════

def _vwap(**kwargs) -> VWAPReversionStrategy:
    defaults = dict(name="nse_vwap_reversion", symbols=["TEST"], market="NSE")
    defaults.update(kwargs)
    return VWAPReversionStrategy(**defaults)


def _emit_args(price: float, vwap_val: float, lower: float, upper: float,
               atr: float, ist_h: int = 11, ist_m: int = 0) -> tuple:
    bar = _bar("TEST", ist_h, ist_m, price + 1, price + 2, price - 1, price, 2000)
    return bar, Direction.BUY, vwap_val, upper, lower, atr, 0.9


def test_vwap_rr_gate_rejects_shallow_entry():
    s = _vwap()
    s._ensure_buffers("TEST")
    # reward = 1005 − 1000 = 5; stop = max(atr=4, half-band=3, floor=4) = 4 → RR 1.25 < 1.5
    bar, d, v, ub, lb, atr, conf = _emit_args(1000.0, 1005.0, 1002.0, 1008.0, 4.0)
    s._emit(bar, d, v, ub, lb, atr, conf)
    assert s._pending_signal is None


def test_vwap_emits_when_rr_and_viability_pass():
    s = _vwap()
    s._ensure_buffers("TEST")
    # vwap 1010, bands 1005/1015 (half-band 5), price 1002 → reward 8, stop 5 → RR 1.6
    bar, d, v, ub, lb, atr, conf = _emit_args(1002.0, 1010.0, 1005.0, 1015.0, 3.0)
    s._emit(bar, d, v, ub, lb, atr, conf)
    sig = s._pending_signal
    assert sig is not None
    stop_pct = (sig.price_at_signal - sig.stop_loss) / sig.price_at_signal * 100
    assert stop_pct >= MIN_STOP_PCT
    assert sig.take_profit == pytest.approx(1010.0)
    assert sig.metadata["reward_risk"] >= 1.5
    assert s._signals_today == 1


def test_vwap_stop_floor_binds():
    s = _vwap()
    s._ensure_buffers("TEST")
    # atr and half-band both tiny → floor 0.4% must carry the stop,
    # which then fails the RR gate for a shallow reward
    bar, d, v, ub, lb, atr, conf = _emit_args(1000.0, 1005.0, 1004.0, 1006.0, 0.5)
    s._emit(bar, d, v, ub, lb, atr, conf)
    assert s._pending_signal is None  # reward 5 < 1.5 × 4.0 floor stop


def test_vwap_direction_aware_cooldown():
    s = _vwap(signal_cooldown_bars=15)
    s._ensure_buffers("TEST")
    s._bar_index["TEST"] = 100
    s._last_signal_idx[("TEST", "BUY")] = 95     # 5 bars ago → BUY blocked
    assert not s._can_fire("TEST", Direction.BUY)
    assert s._can_fire("TEST", Direction.SELL)   # opposite direction free
    s._bar_index["TEST"] = 111                   # 16 bars later → BUY free again
    assert s._can_fire("TEST", Direction.BUY)


def test_vwap_per_symbol_direction_daily_budget():
    s = _vwap(max_signals_per_symbol_direction=2)
    s._ensure_buffers("TEST")
    s._sym_dir_count[("TEST", "SELL")] = 2
    assert not s._can_fire("TEST", Direction.SELL)  # no 3rd knife-catch
    assert s._can_fire("TEST", Direction.BUY)


def test_vwap_strategy_daily_budget_blocks_on_bar():
    s = _vwap(max_signals_per_day=0)

    async def run():
        # budget 0 → even a perfect setup must not be evaluated
        for i in range(70):
            await s.on_bar(_bar("TEST", 9 + (15 + i) // 60, (15 + i) % 60,
                                1000, 1001, 999, 1000, 2000))
        await s.on_bar(_bar("TEST", 11, 0, 995, 996, 990, 991, 2000))
        return await s.generate_signal()

    assert _run(run()) is None


def test_vwap_start_time_gate_is_1015():
    from strategy_engine.strategies.vwap_reversion_strategy import _IST_VWAP_START
    assert _IST_VWAP_START == 10 * 60 + 15


def test_vwap_reset_daily_clears_budgets():
    s = _vwap()
    s._signals_today = 4
    s._sym_dir_count[("TEST", "BUY")] = 2
    s._last_signal_idx[("TEST", "BUY")] = 50
    s.reset_daily()
    assert s._signals_today == 0
    assert s._sym_dir_count == {}
    assert s._last_signal_idx == {}


# ══════════════════════════════════════════════════════════════════════════════
# 4. PaperQualityGateValidator — prefixed-name lookup
# ══════════════════════════════════════════════════════════════════════════════

def _entry_signal(strategy: str, confidence: float = 0.95,
                  stop: float = 995.0, tp: float = 1010.0) -> Signal:
    return Signal(
        signal_id="sig-1",
        symbol="RELIANCE",
        market="NSE",
        direction=Direction.BUY,
        quantity=10,
        confidence=confidence,
        strategy_name=strategy,
        generated_at=datetime.now(timezone.utc),
        price_at_signal=1000.0,
        stop_loss=stop,
        take_profit=tp,
        metadata={},
    )


def test_threshold_lookup_handles_market_prefix():
    thresholds = {"vwap_reversion": 0.90, "orb_15m": 0.65}
    assert _threshold_for(thresholds, "nse_vwap_reversion") == 0.90
    assert _threshold_for(thresholds, "us_orb_15m") == 0.65
    assert _threshold_for(thresholds, "vwap_reversion") == 0.90
    assert _threshold_for(thresholds, "nse_unknown") == 0.0


def test_quality_gate_now_rejects_prefixed_strategy_low_confidence():
    v = PaperQualityGateValidator(
        min_confidence_by_strategy={"vwap_reversion": 0.90},
        min_rr_by_strategy={},
    )
    # Sessions 12–15: this signal passed because the lookup returned 0.0
    result = v.validate(_entry_signal("nse_vwap_reversion", confidence=0.70))
    assert not result.approved
    assert "CONFIDENCE_BELOW_THRESHOLD" in result.reason


def test_quality_gate_rejects_prefixed_strategy_low_rr():
    v = PaperQualityGateValidator(
        min_confidence_by_strategy={},
        min_rr_by_strategy={"vwap_reversion": 1.40},
    )
    # reward 5, risk 5 → RR 1.0 < 1.40
    result = v.validate(_entry_signal("nse_vwap_reversion", stop=995.0, tp=1005.0))
    assert not result.approved
    assert "REWARD_RISK_TOO_LOW" in result.reason


def test_quality_gate_passes_good_signal():
    v = PaperQualityGateValidator(
        min_confidence_by_strategy={"vwap_reversion": 0.90},
        min_rr_by_strategy={"vwap_reversion": 1.40},
    )
    result = v.validate(_entry_signal("nse_vwap_reversion", confidence=0.95,
                                      stop=995.0, tp=1010.0))
    assert result.approved


# ══════════════════════════════════════════════════════════════════════════════
# 5. SymbolTradeCountValidator — global daily entry budget
# ══════════════════════════════════════════════════════════════════════════════

def _count_validator(max_entries_per_day: int = 15,
                     max_trades_per_symbol: int = 1) -> SymbolTradeCountValidator:
    v = SymbolTradeCountValidator(
        dynamo_client=None,
        orders_table="test-orders",
        max_trades_per_symbol=max_trades_per_symbol,
        max_entries_per_day=max_entries_per_day,
        risk_profile="paper",
    )
    # Pre-rehydrate so validate() does not hit DynamoDB
    v._rehydrated = True
    v._trade_date = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")
    return v


def test_global_budget_rejects_when_spent():
    v = _count_validator(max_entries_per_day=15)
    v._counts = {f"SYM{i}": 1 for i in range(15)}  # 15 entries today
    result = _run(v.validate(_entry_signal("nse_orb_15m")))
    assert not result.approved
    assert "GLOBAL_DAILY_ENTRY_LIMIT_REACHED" in result.reason


def test_global_budget_allows_under_limit():
    v = _count_validator(max_entries_per_day=15)
    v._counts = {f"SYM{i}": 1 for i in range(14)}
    result = _run(v.validate(_entry_signal("nse_orb_15m")))
    assert result.approved


def test_global_budget_exempts_exits():
    v = _count_validator(max_entries_per_day=15)
    v._counts = {f"SYM{i}": 1 for i in range(20)}
    sig = _entry_signal("nse_orb_15m")
    sig.signal_id = "EXIT-RELIANCE-NSE-STOP_LOSS-2026-06-10"
    result = _run(v.validate(sig))
    assert result.approved
    assert result.reason == "exit_signal_exempt"


def test_global_budget_disabled_when_zero():
    v = _count_validator(max_entries_per_day=0)
    v._counts = {f"SYM{i}": 1 for i in range(50)}
    result = _run(v.validate(_entry_signal("nse_orb_15m")))
    assert result.approved  # only the per-symbol limit applies (RELIANCE has 0)


def test_per_symbol_limit_still_enforced_under_budget():
    v = _count_validator(max_entries_per_day=15)
    v._counts = {"RELIANCE": 1}
    result = _run(v.validate(_entry_signal("nse_orb_15m")))
    assert not result.approved
    assert "MAX_TRADES_PER_SYMBOL_REACHED" in result.reason
