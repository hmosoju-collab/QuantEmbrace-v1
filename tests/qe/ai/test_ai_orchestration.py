"""Research graph: modes, symbol selection, failure isolation, degradation,
cache replay, contamination, fail-closed look-ahead."""

from collections import Counter
from datetime import date, datetime, timedelta
import json

import pytest

from qe.ai.config import BudgetConfig, ModelProfile, ResearchRunConfig
from qe.ai.llm import FakeLLM, LLMError
from qe.ai.models import ComponentStatus, Evidence
from qe.ai.orchestration import graph, run_research
from qe.ai.tools import QuantSpec, ResearchData, ResearchDataAPI, ToolResult, quant_view
from qe.config import DataConfig, RunConfig, StrategyConfig, UniverseConfig
from qe.journal import read_journal

POS = 400
BOOK = RunConfig(
    name="book",
    mode="sim",
    start_date=date(2024, 1, 1),
    end_date=date(2025, 6, 1),
    universe=UniverseConfig(symbols=None),
    data=DataConfig(lake_root="unused"),
    strategy=StrategyConfig(factor="delivery", top_n=40, k=10),
)


def _cfg(mode: str = "FAST", cutoff: date | None = date(2023, 1, 31), **over) -> ResearchRunConfig:
    return ResearchRunConfig(
        name="t",
        book_config="unused.yaml",
        research_mode=mode,
        quick_model=ModelProfile(model_id="fake-quick", tier="quick", knowledge_cutoff=cutoff),
        deep_model=ModelProfile(model_id="fake-deep", tier="deep", knowledge_cutoff=cutoff),
        max_symbols=3,
        **over,
    )


def _run(tmp_path, panel, cfg, client=None):
    client = client or FakeLLM()
    res = run_research(
        cfg,
        as_of=panel.date_at(POS),
        base_dir=tmp_path,
        book=BOOK,
        data=ResearchData(panel, "ds-test", {}),
        client=client,
    )
    return res, client


def _events(res, kind):
    return [r["data"] for r in read_journal(res.journal_path) if r["type"] == kind]


def test_researches_the_engine_basket_first(tmp_path, synthetic_panel):
    res, _ = _run(tmp_path, synthetic_panel, _cfg())
    view = quant_view(ResearchDataAPI(synthetic_panel, POS, "NSE"), QuantSpec("delivery", 40, 10))
    assert [s.symbol for s in res.report.signals] == list(view.basket[:3])


@pytest.mark.parametrize(
    "mode,calls,agents",
    [
        ("FAST", 1 + 3 * 2, {"regime", "technical", "risk"}),
        (
            "STANDARD",
            1 + 3 * 6,
            {"regime", "technical", "risk", "bull", "bear", "critic", "synthesizer"},
        ),
        (
            "DEEP",
            1 + 3 * 8,
            {"regime", "technical", "risk", "bull", "bear", "critic", "synthesizer"},
        ),
    ],
)
def test_mode_controls_agent_participation_and_cost(tmp_path, synthetic_panel, mode, calls, agents):
    res, fake = _run(tmp_path, synthetic_panel, _cfg(mode))
    llm = _events(res, "LLM_CALL")
    assert len(fake.requests) == calls == len(llm)
    assert {e["agent_id"] for e in llm} == agents  # no-data agents never call the LLM
    sig = res.report.signals[0]
    if mode == "FAST":
        assert sig.component_status["debate"] == sig.component_status["synthesis"] == "SKIPPED"
        assert sig.component_status["news"] == "SKIPPED"
    else:
        assert sig.component_status["news"] == "UNAVAILABLE"
        assert sig.component_status["debate"] == sig.component_status["synthesis"] == "OK"
        assert sig.bull_case and sig.bear_case and sig.consensus
    synth_models = {e["model_id"] for e in llm if e["agent_id"] == "synthesizer"}
    assert synth_models == (
        {"fake-deep"} if mode == "DEEP" else {"fake-quick"} if mode == "STANDARD" else set()
    )
    if mode == "DEEP":
        assert Counter(e["agent_id"] for e in llm)["bull"] == 3 * 2  # two rounds


def test_ai_score_is_computed_from_analysts_not_the_synthesizer(tmp_path, synthetic_panel):
    res, _ = _run(tmp_path, synthetic_panel, _cfg("STANDARD"))
    for s in res.report.signals:
        assert s.ai_score == s.technical_score  # the only OK directional analyst today


def test_breaker_degrades_to_unavailable_and_run_completes(tmp_path, synthetic_panel):
    cfg = _cfg(budget=BudgetConfig(breaker_threshold=2, max_retries=0))
    res, fake = _run(tmp_path, synthetic_panel, cfg, FakeLLM(script=[LLMError("down")] * 100))
    assert len(res.report.signals) == 3 and not res.report.failed_symbols
    assert all(s.ai_score is None for s in res.report.signals)
    assert len(fake.requests) == 2  # breaker opened; no further provider calls
    statuses = {v for s in res.report.signals for v in s.component_status.values()}
    assert ComponentStatus.UNAVAILABLE in statuses
    assert _events(res, "SESSION_END")[0]["status"] == "OK"


def test_budget_exhaustion_degrades_without_crashing(tmp_path, synthetic_panel):
    cfg = _cfg(budget=BudgetConfig(max_run_tokens=1500, max_tokens_per_call=1024))
    res, _ = _run(tmp_path, synthetic_panel, cfg)
    errs = [e["error"] or "" for e in _events(res, "LLM_CALL")]
    assert any("budget" in e for e in errs)
    assert len(res.report.signals) == 3


def test_replay_from_cache_is_identical(tmp_path, synthetic_panel):
    cfg = _cfg("STANDARD")
    first, _ = _run(tmp_path, synthetic_panel, cfg)
    second, fake2 = _run(tmp_path, synthetic_panel, cfg)
    assert fake2.requests == []  # every call served from the content-addressed cache

    def strip(s):
        return s.model_dump(mode="json", exclude={"research_id", "trace_id"})

    assert [strip(s) for s in first.report.signals] == [strip(s) for s in second.report.signals]


def test_symbol_failure_is_isolated(tmp_path, synthetic_panel, monkeypatch):
    real = graph.technical
    victim = quant_view(
        ResearchDataAPI(synthetic_panel, POS, "NSE"), QuantSpec("delivery", 40, 10)
    ).basket[1]

    def flaky(api, symbol):
        if symbol == victim:
            raise RuntimeError("tool bug")
        return real(api, symbol)

    monkeypatch.setattr(graph, "technical", flaky)
    res, _ = _run(tmp_path, synthetic_panel, _cfg())
    assert res.report.failed_symbols == (victim,)
    assert len(res.report.signals) == 2
    assert _events(res, "SYMBOL_FAILED")[0]["error"].startswith("RuntimeError")


def test_lookahead_evidence_fails_the_symbol_before_any_llm_call(
    tmp_path, synthetic_panel, monkeypatch
):
    real = graph.technical

    def leaky(api, symbol):
        good = real(api, symbol)
        future = Evidence(
            evidence_id="tech.aaa_future",
            tool="tech",
            symbol=symbol,
            knowledge_ts=api.cutoff + timedelta(days=1),
            value=1.0,
            summary="tomorrow's close",
        )
        return ToolResult("tech", symbol, ComponentStatus.OK, (future, *good.evidence))

    monkeypatch.setattr(graph, "technical", leaky)
    res, fake = _run(tmp_path, synthetic_panel, _cfg())
    assert res.report.signals == () and len(res.report.failed_symbols) == 3
    assert all("after information_cutoff" in e["error"] for e in _events(res, "SYMBOL_FAILED"))
    assert not any("aaa_future" in r.prompt for r in fake.requests)  # the model never saw it


@pytest.mark.parametrize(
    "cutoff,expected",
    [(None, True), (date(2024, 11, 1), True), (date(2023, 1, 31), False)],
)
def test_contamination_follows_model_knowledge_cutoff(tmp_path, synthetic_panel, cutoff, expected):
    res, _ = _run(tmp_path, synthetic_panel, _cfg(cutoff=cutoff))  # decision date 2024-12-12
    assert {s.contamination_risk for s in res.report.signals} == {expected}


def test_run_writes_only_ai_locations(tmp_path, synthetic_panel):
    _run(tmp_path, synthetic_panel, _cfg("STANDARD"))
    written = {p.relative_to(tmp_path).parts[:2] for p in tmp_path.rglob("*") if p.is_file()}
    assert written <= {("journals", "ai"), ("backtest-data", "ai_cache")}


def test_signals_round_trip_through_json(tmp_path, synthetic_panel):
    res, _ = _run(tmp_path, synthetic_panel, _cfg("DEEP"))
    recs = _events(res, "RESEARCH_SIGNAL")
    assert len(recs) == 3
    for rec in recs:
        json.dumps(rec)
        assert datetime.fromisoformat(rec["information_cutoff"]).hour == 15
