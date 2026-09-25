"""Paper/live isolation is a property of the type system, not a config flag."""

import pytest

from qe.costs import EquityDeliveryCosts
from qe.execution import Book, Holding, LiveBroker, LiveBrokerLocked, PaperBroker, SimBroker


def test_paper_broker_shares_sim_fill_logic_exactly():
    prices = {"A": 60.0, "B": 100.0}
    target = {"B": 5}
    costs = EquityDeliveryCosts()

    sim_book = Book(cash=1_000.0, holdings={"A": Holding(10, 50.0)})
    paper_book = Book(cash=1_000.0, holdings={"A": Holding(10, 50.0)})
    sim_fill = SimBroker(costs).rebalance_fill(sim_book, target, prices)
    paper_fill = PaperBroker(costs).rebalance_fill(paper_book, target, prices)

    assert sim_fill.total_cost == paper_fill.total_cost
    assert sim_book.cash == paper_book.cash
    assert sim_book.qty() == paper_book.qty()


def test_paper_broker_is_a_sim_broker_and_cannot_reach_a_venue():
    pb = PaperBroker()
    assert isinstance(pb, SimBroker)
    assert pb.venue == "paper-sim"
    assert not hasattr(pb, "place_order")  # no real-venue surface at all


def test_live_broker_unconstructible_without_token():
    with pytest.raises(LiveBrokerLocked, match="LiveGateToken"):
        LiveBroker(token=None, config_hash="a" * 64, client=object())
    # Full token+client construction is exercised in test_live_gate.py; here we
    # only assert the barrier fires with no token (structural, at construction).
