"""Point-in-time tools: no future data, knowledge-time stamping, engine parity."""

from datetime import date, datetime, time
import inspect

import pandas as pd
import pytest

from qe.ai import tools
from qe.ai.models import ComponentStatus
from qe.ai.tools import (
    QuantSpec,
    ResearchDataAPI,
    factor_scores,
    load_research_data,
    quant,
    quant_view,
    regime,
    risk,
    technical,
)
from qe.ai.tools.quant_signal import engine_basket
from qe.clock import IST
from qe.config import DataConfig, RunConfig, StrategyConfig, UniverseConfig
from qe.data.panel import Panel
from qe.strategy import Context, FactorBookStrategy

POS = 400


def _all_tool_outputs(api: ResearchDataAPI, spec: QuantSpec) -> dict:
    view = quant_view(api, spec)
    out = {"regime": regime(api)}
    for sym in ("S000", "S007", "S031"):
        out[sym] = (technical(api, sym), risk(api, sym), quant(api, sym, spec, view))
    return out


def _future_mutated(panel: Panel, pos: int) -> Panel:
    def bump(df):
        df = df.copy()
        df.iloc[pos + 1 :] = df.iloc[pos + 1 :] * 10.0
        return df

    return Panel(bump(panel.close), bump(panel.turnover), bump(panel.delivery))


def _hash(panel: Panel) -> int:
    return sum(
        int(pd.util.hash_pandas_object(f, index=True).sum())
        for f in (panel.close, panel.turnover, panel.delivery)
    )


@pytest.mark.parametrize("factor", ["delivery", "momentum"])
def test_tools_never_see_future_rows(synthetic_panel, factor):
    spec = QuantSpec(factor=factor, top_n=40, k=10)
    before = _all_tool_outputs(ResearchDataAPI(synthetic_panel, POS, "NSE"), spec)
    after = _all_tool_outputs(
        ResearchDataAPI(_future_mutated(synthetic_panel, POS), POS, "NSE"), spec
    )
    assert before == after


def test_every_evidence_is_stamped_at_the_decision_close(synthetic_panel):
    api = ResearchDataAPI(synthetic_panel, POS, "NSE")
    expected = datetime.combine(api.decision_date, time(15, 30), tzinfo=IST)
    assert api.cutoff == expected
    outs = _all_tool_outputs(api, QuantSpec("delivery", 40, 10))
    results = [outs["regime"][0]] + [r for k, v in outs.items() if k != "regime" for r in v]
    evs = [e for r in results for e in r.evidence]
    assert evs and all(e.knowledge_ts == expected for e in evs)


def test_same_day_close_is_invisible_before_the_close(synthetic_panel):
    d = synthetic_panel.date_at(POS)
    before_close = ResearchDataAPI.at(
        synthetic_panel, datetime.combine(d, time(11, 0), tzinfo=IST), "NSE"
    )
    at_close = ResearchDataAPI.at(
        synthetic_panel, datetime.combine(d, time(15, 30), tzinfo=IST), "NSE"
    )
    assert before_close.pos == POS - 1 and at_close.pos == POS
    with pytest.raises(ValueError, match="timezone-aware"):
        ResearchDataAPI.at(synthetic_panel, datetime.combine(d, time(16, 0)), "NSE")


def test_tools_do_not_mutate_the_panel(synthetic_panel):
    h = _hash(synthetic_panel)
    for factor in ("delivery", "momentum"):
        _all_tool_outputs(ResearchDataAPI(synthetic_panel, POS, "NSE"), QuantSpec(factor, 40, 10))
    assert _hash(synthetic_panel) == h


@pytest.mark.parametrize("factor", ["delivery", "momentum"])
def test_engine_basket_parity(synthetic_panel, factor):
    """quant_signal repeats FactorBookStrategy's score lines; prove equality at
    every row the engine could rebalance on (and a sweep of others)."""
    strat = FactorBookStrategy(factor=factor, top_n=40, k=10)
    for pos in range(100, len(synthetic_panel.index), 7):
        ctx = Context.at(synthetic_panel, pos)
        assert engine_basket(factor_scores(ctx, factor, 40), 10) == strat.select_basket(ctx), pos


def test_quant_score_bounds_and_membership(synthetic_panel):
    api = ResearchDataAPI(synthetic_panel, POS, "NSE")
    spec = QuantSpec("delivery", 40, 10)
    view = quant_view(api, spec)
    assert view.q.between(-1, 1).all() and len(view.basket) == 10
    top = view.basket[0]
    assert view.q[top] == view.q.max()
    res = quant(api, "NOT_A_SYMBOL", spec, view)
    assert res.status is ComponentStatus.UNAVAILABLE
    assert quant(api, top, None, None).status is ComponentStatus.UNAVAILABLE


def test_regime_is_unavailable_without_history(synthetic_panel):
    res, reading = regime(ResearchDataAPI(synthetic_panel, 100, "NSE"))
    assert res.status is ComponentStatus.UNAVAILABLE and reading.label == "UNKNOWN"
    res, reading = regime(ResearchDataAPI(synthetic_panel, POS, "NSE"))
    assert res.ok and reading.label in ("RISK_ON", "RISK_OFF") and -1 <= reading.value <= 1


def test_index_series_is_pit_and_never_forward_filled(synthetic_panel):
    d = synthetic_panel.date_at(POS)
    days = [synthetic_panel.date_at(p) for p in range(POS - 300, POS + 20)]
    vix = pd.Series(range(len(days)), index=days, dtype=float)
    api = ResearchDataAPI(synthetic_panel, POS, "NSE", {"INDIAVIX": vix})
    s = api.index_series("INDIAVIX")
    assert s.index[-1] == d and s.index.max() <= d  # future values truncated
    ev = {e.evidence_id: e.value for e in regime(api)[0].evidence}
    assert "regime.indiavix" in ev and ev["regime.indiavix"] == float(vix[d])

    stale = vix[vix.index < d]  # series ends before the decision date
    api2 = ResearchDataAPI(synthetic_panel, POS, "NSE", {"INDIAVIX": stale})
    assert api2.index_series("INDIAVIX") is None
    assert "regime.indiavix" not in {e.evidence_id for e in regime(api2)[0].evidence}


def test_no_data_tools_are_unavailable(synthetic_panel):
    for fn in (tools.fundamentals, tools.sentiment):
        res = fn("S000")
        assert (
            res.status is ComponentStatus.UNAVAILABLE
            and not res.evidence
            and "prices only" in res.reason
        )
    res = tools.news(ResearchDataAPI(synthetic_panel, 400, "NSE"), "S000")  # empty corpus
    assert res.status is ComponentStatus.UNAVAILABLE and "no curated corpus" in res.reason


def test_tool_functions_take_no_path_url_or_code_arguments():
    banned = {"path", "url", "uri", "file", "filename", "command", "cmd", "sql", "query", "code"}
    public = [getattr(tools, n) for n in tools.__all__ if callable(getattr(tools, n))]
    for fn in public:
        if inspect.isclass(fn) or fn is load_research_data:
            continue  # data classes; the loader is called by the orchestrator, not agents
        assert not banned & set(inspect.signature(fn).parameters), fn.__name__


def test_load_research_data_pins_a_snapshot(fixture_lake, tmp_path):
    book = RunConfig(
        name="b",
        mode="sim",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        universe=UniverseConfig(symbols=None),
        data=DataConfig(lake_root=str(fixture_lake)),
        strategy=StrategyConfig(),
    )
    data = load_research_data(book, date(2024, 1, 12), base_dir="/")
    assert data.snapshot_id.startswith("ds-")
    assert list(data.panel.close.columns) == ["TESTA", "TESTB"]
    assert data.panel.date_at(len(data.panel.index) - 1) <= date(2024, 1, 12)
    assert data.index_bars == {}  # fixture lake has no INDICES segment
    assert (fixture_lake / "_snapshots" / f"{data.snapshot_id}.json").exists()
