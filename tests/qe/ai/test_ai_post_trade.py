"""P7 post-trade analyst: exact episode reconstruction, deterministic
classifications, read-only engine journals, knowledge-time lessons."""

from datetime import date, datetime, time
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from qe.ai.agents.post_trade import SPEC
from qe.ai.config import ModelProfile, ResearchRunConfig
from qe.ai.hypotheses import generate_hypotheses
from qe.ai.llm import FakeLLM
from qe.ai.models import ComponentStatus
from qe.ai.orchestration import run_research
from qe.ai.post_trade import (
    PostTradeReview,
    completed_trades,
    lessons_known_at,
    open_positions,
    run_post_trade,
)
from qe.ai.tools import ResearchData
from qe.clock import IST
from qe.config import DataConfig, RunConfig, StrategyConfig, UniverseConfig
from qe.engine import run_sim
from qe.journal import JournalWriter

CFG = ResearchRunConfig(
    name="pt",
    book_config="unused.yaml",
    quick_model=ModelProfile(model_id="fake-quick", tier="quick"),
    deep_model=ModelProfile(model_id="fake-deep", tier="deep"),
    max_symbols=3,
)


def _order(sym, side, qty, px):
    return {"symbol": sym, "side": side, "qty": qty, "price": px, "notional": qty * px}


def test_episode_reconstruction_is_exact(tmp_path):
    path = tmp_path / "journals" / "sim-x.jsonl"
    with JournalWriter(path) as w:
        w.session_start(session_id="sim-x", mode="sim", config_hash="h", config={},
                        code_sha="c", data_snapshot_id="ds")  # fmt: skip
        w.write("REBALANCE", {"date": "2025-01-31", "cost": 3.0, "nav": 0,
                              "orders": [_order("A", "BUY", 10, 100.0), _order("B", "BUY", 5, 200.0)]})  # fmt: skip
        w.write("REBALANCE", {"date": "2025-02-28", "cost": 2.0, "nav": 0,
                              "orders": [_order("A", "SELL", 4, 110.0), _order("B", "BUY", 5, 210.0)]})  # fmt: skip
        w.write("REBALANCE", {"date": "2025-03-31", "cost": 2.0, "nav": 0,
                              "orders": [_order("A", "SELL", 6, 120.0), _order("C", "BUY", 1, 50.0)]})  # fmt: skip
    trades = completed_trades(path)
    assert [t.symbol for t in trades] == ["A"]
    a = trades[0]
    assert (a.open_date, a.close_date) == (date(2025, 1, 31), date(2025, 3, 31))
    assert (a.qty_bought, a.qty_sold) == (10, 10)
    assert a.buy_notional == 1000.0 and a.sell_notional == 440.0 + 720.0
    expected_cost = 3.0 * 1000 / 2000 + 2.0 * 440 / (440 + 1050) + 2.0 * 720 / (720 + 50)
    assert a.costs == pytest.approx(expected_cost)
    assert a.net_return == pytest.approx((1160 - 1000 - expected_cost) / 1000)
    assert open_positions(path) == {"B": 10, "C": 1}  # still open ⇒ not reviewed


@pytest.fixture()
def sim(tmp_path, synthetic_panel):
    book = RunConfig(
        name="pt-book",
        mode="sim",
        start_date=date(2024, 9, 30),
        end_date=synthetic_panel.date_at(len(synthetic_panel.index) - 1),
        universe=UniverseConfig(symbols=None),
        data=DataConfig(lake_root="unused"),
        journal_dir=str(tmp_path / "journals"),
        strategy=StrategyConfig(factor="delivery", top_n=40, k=10),
    )
    return run_sim(book, base_dir=tmp_path, panel=synthetic_panel)


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_reviews_are_deterministic_read_only_and_knowledge_stamped(tmp_path, synthetic_panel, sim):
    before = _sha(sim.journal_path)
    run = run_post_trade(sim.journal_path, CFG, base_dir=tmp_path, client=FakeLLM(),
                         data=ResearchData(synthetic_panel, "ds-test", {}), max_trades=8)  # fmt: skip
    assert _sha(sim.journal_path) == before  # the trade record is never rewritten
    assert 1 <= len(run.reviews) <= 8
    for r in run.reviews:
        assert r.knowledge_ts == datetime.combine(r.close_date, time(15, 30), tzinfo=IST)
        assert r.quant_thesis == ("CONFIRMED" if r.excess_return > 0 else "REFUTED")
        assert r.ai_thesis == "NO_SIGNAL"  # no research journal at those entries
        assert r.entry_quality in ("GOOD", "NEUTRAL", "POOR", "UNKNOWN")
        assert r.lesson_status is ComponentStatus.OK and r.lesson
        assert r.mae <= 0 <= r.mfe or r.mfe < 0
    assert run.out_dir.is_relative_to(tmp_path / "reports" / "qe-ai" / "post_trade")
    assert "Classifications are computed by code" in (run.out_dir / "report.md").read_text()
    # determinism: a second run gives identical classifications
    again = run_post_trade(sim.journal_path, CFG, base_dir=tmp_path, client=FakeLLM(),
                           data=ResearchData(synthetic_panel, "ds-test", {}), max_trades=8)  # fmt: skip
    strip = {"lesson_status", "model_id"}
    assert [r.model_dump(exclude=strip) for r in again.reviews] == [
        r.model_dump(exclude=strip) for r in run.reviews
    ]


def test_ai_thesis_uses_the_first_research_signal_at_entry(tmp_path, synthetic_panel, sim):
    first = completed_trades(sim.journal_path)[0]
    pos = next(
        i
        for i in range(len(synthetic_panel.index))
        if synthetic_panel.date_at(i) == first.open_date
    )
    run_research(CFG.model_copy(update={"max_symbols": 20}), as_of=synthetic_panel.date_at(pos),
                 base_dir=tmp_path, book=RunConfig.model_validate(json.loads(
                     next(iter(sim.journal_path.read_text().splitlines())))["data"]["config"]),
                 data=ResearchData(synthetic_panel, "ds-test", {}), client=FakeLLM())  # fmt: skip
    run = run_post_trade(sim.journal_path, CFG, base_dir=tmp_path, client=FakeLLM(),
                         data=ResearchData(synthetic_panel, "ds-test", {}), max_trades=500)  # fmt: skip
    r = next(r for r in run.reviews if r.symbol == first.symbol and r.open_date == first.open_date)
    # the engine bought this name because it was in its basket, which is what research covers
    assert r.ai_score_at_entry is not None
    expected = (
        "NEUTRAL" if abs(r.ai_score_at_entry) <= 0.2
        else "CONFIRMED" if (r.ai_score_at_entry > 0) == (r.excess_return > 0) else "REFUTED"
    )  # fmt: skip
    assert r.ai_thesis == expected


def test_lessons_are_only_visible_after_they_were_knowable(tmp_path, synthetic_panel, sim):
    run_post_trade(sim.journal_path, CFG, base_dir=tmp_path, client=FakeLLM(),
                   data=ResearchData(synthetic_panel, "ds-test", {}), max_trades=500)  # fmt: skip
    all_reviews = lessons_known_at(tmp_path, datetime(2100, 1, 1, tzinfo=IST), limit=10_000)
    closes = sorted({r.close_date for r in all_reviews})
    mid = closes[len(closes) // 2]
    cutoff = datetime.combine(mid, time(15, 30), tzinfo=IST)
    known = lessons_known_at(tmp_path, cutoff, limit=10_000)
    assert known and all(r.knowledge_ts <= cutoff for r in known)
    assert len(known) < len(all_reviews)


def test_hypothesis_prompts_see_only_knowable_lessons(tmp_path, synthetic_panel, sim):
    (tmp_path / "governance").mkdir(exist_ok=True)
    shutil.copy(Path(__file__).resolve().parents[3] / "governance" / "research-eliminated-families.yaml",
                tmp_path / "governance")  # fmt: skip
    run_post_trade(sim.journal_path, CFG, base_dir=tmp_path, client=FakeLLM(),
                   data=ResearchData(synthetic_panel, "ds-test", {}), max_trades=500)  # fmt: skip
    closes = sorted(
        {r.close_date for r in lessons_known_at(tmp_path, datetime(2100, 1, 1, tzinfo=IST), 10_000)}
    )
    as_of = closes[1]  # only lessons closed on/before this date may appear
    book = RunConfig.model_validate(
        json.loads(sim.journal_path.read_text().splitlines()[0])["data"]["config"]
    )
    res = run_research(CFG, as_of=as_of, base_dir=tmp_path, book=book,
                       data=ResearchData(synthetic_panel, "ds-test", {}), client=FakeLLM())  # fmt: skip
    fake = FakeLLM()
    generate_hypotheses(res.journal_path, base_dir=tmp_path, client=fake)
    prompt = fake.requests[0].prompt
    n_known = len(lessons_known_at(tmp_path, datetime.combine(as_of, time(15, 30), tzinfo=IST)))
    assert prompt.count("Fake lesson") == n_known > 0


def test_rejects_non_engine_journals(tmp_path, synthetic_panel):
    res = run_research(CFG, as_of=synthetic_panel.date_at(400), base_dir=tmp_path,
                       book=RunConfig(name="b", mode="sim", start_date=date(2024, 1, 1),
                                      end_date=date(2025, 6, 1), universe=UniverseConfig(symbols=None),
                                      data=DataConfig(lake_root="unused"),
                                      strategy=StrategyConfig(top_n=40, k=10)),
                       data=ResearchData(synthetic_panel, "ds-test", {}), client=FakeLLM())  # fmt: skip
    with pytest.raises(ValueError, match="not an engine"):
        run_post_trade(res.journal_path, CFG, base_dir=tmp_path, client=FakeLLM())


def test_prompt_hash_is_pinned():
    # editing the post-trade prompt without bumping its version must fail here
    assert (SPEC.prompt_version, SPEC.prompt_hash) == ("post_trade/1", "3e7b789980f84e36")
    assert isinstance(PostTradeReview.model_json_schema(), dict)
