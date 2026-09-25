import pytest

from qe import risk
from qe.costs import EquityDeliveryCosts
from qe.execution import Book, Holding, SimBroker, reconcile


def _proposal(**overrides) -> risk.Proposal:
    kwargs = {
        "weights": {"A": 0.05, "B": 0.05},
        "target_qty": {"A": 10, "B": 5},
        "prices": {"A": 100.0, "B": 200.0},
        "nav": 100_000.0,
        "projected_cash": 5_000.0,
        "max_weight": 0.08,
    }
    kwargs.update(overrides)
    return risk.Proposal(**kwargs)


def test_risk_approves_clean_proposal():
    verdict = risk.evaluate(_proposal())
    assert verdict.approved
    assert not verdict.rejections


@pytest.mark.parametrize(
    ("override", "failing_check"),
    [
        ({"target_qty": {"A": -5}}, "long_only"),
        ({"weights": {"A": 0.10}}, "weight_cap"),
        ({"weights": {"A": 0.6, "B": 0.6}}, "gross_exposure"),
        ({"projected_cash": -1.0}, "cash_non_negative"),
    ],
)
def test_risk_rejects(override, failing_check):
    verdict = risk.evaluate(_proposal(**override))
    assert not verdict.approved
    assert failing_check in [r.check for r in verdict.rejections]


def test_risk_fails_closed_on_crashing_check():
    def bad_check(p):
        raise RuntimeError("boom")

    verdict = risk.evaluate(_proposal(), checks=(bad_check,))
    assert not verdict.approved
    assert "boom" in verdict.results[0].detail


def test_reconcile_deltas_and_skips_unpriced():
    orders = reconcile(
        current_qty={"A": 10, "C": 7, "SUSPENDED": 3},
        target_qty={"A": 4, "B": 5},
        prices={"A": 100.0, "B": 50.0, "C": 20.0},
    )
    by_symbol = {o.symbol: o for o in orders}
    assert by_symbol["A"].side == "SELL" and by_symbol["A"].qty == 6
    assert by_symbol["B"].side == "BUY" and by_symbol["B"].qty == 5
    assert by_symbol["C"].side == "SELL" and by_symbol["C"].qty == 7
    assert "SUSPENDED" not in by_symbol  # no price → no order, same as v1


def test_sim_broker_cash_and_cost_math():
    costs = EquityDeliveryCosts()
    book = Book(cash=1_000.0, holdings={"A": Holding(qty=10, avg_price=50.0)})
    prices = {"A": 60.0, "B": 100.0}

    fill = SimBroker(costs).rebalance_fill(book, {"B": 5}, prices)

    sell_notional, buy_notional = 10 * 60.0, 5 * 100.0
    expected_cost = buy_notional * costs.leg_cost_frac("BUY") + sell_notional * costs.leg_cost_frac(
        "SELL"
    )
    assert fill.total_cost == pytest.approx(expected_cost, abs=0.01)
    assert book.cash == pytest.approx(
        1_000.0 + sell_notional - buy_notional - expected_cost, abs=0.01
    )
    assert book.qty() == {"B": 5}
    assert book.mark_to_market(prices) == pytest.approx(book.cash + 500.0)
