"""qe.costs must never drift from the v1 single source of truth."""

from pathlib import Path
import sys

import pytest

from qe.costs import EquityDeliveryCosts

_REPO = Path(__file__).resolve().parents[2]
for _p in (_REPO / "scripts" / "backtest", _REPO / "scripts" / "paper"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def test_leg_fracs_match_v1_factor_study():
    v1 = pytest.importorskip("run_factor_study")
    costs = EquityDeliveryCosts()
    assert costs.leg_cost_frac("BUY") == pytest.approx(v1._BUY_FRAC, abs=1e-15)
    assert costs.leg_cost_frac("SELL") == pytest.approx(v1._SELL_FRAC, abs=1e-15)
    assert costs.round_trip_frac == pytest.approx(v1._ROUND_TRIP, abs=1e-15)


def test_statutory_fields_match_v1_delivery_model():
    from strategy_engine.backtesting.backtester import IndianCostModel

    v1 = IndianCostModel.delivery()
    costs = EquityDeliveryCosts()
    assert costs.stt_buy_pct == v1.stt_buy_pct
    assert costs.stt_sell_pct == v1.stt_sell_pct
    assert costs.exchange_txn_pct == v1.exchange_txn_pct
    assert costs.sebi_turnover_pct == v1.sebi_turnover_pct
    assert costs.stamp_buy_pct == v1.stamp_buy_pct
    assert costs.gst_pct == v1.gst_pct
