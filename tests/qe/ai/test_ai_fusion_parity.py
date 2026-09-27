"""ADVISORY fusion reproduces the engine exactly — at every rebalance, against
both the strategy's own targets and the holdings the sim engine journaled.
(Tests may import the engine; qe.ai itself may not.)"""

from collections import defaultdict
from datetime import date
import random

import pytest

from qe.ai.config import ModelProfile, ResearchRunConfig
from qe.ai.fusion import FusionConfig, FusionRefused, HardRiskConfig, fuse, quant_rows, run_fusion
from qe.ai.llm import FakeLLM
from qe.ai.models import COMPONENTS, ComponentStatus, ResearchSignal
from qe.ai.orchestration import run_research
from qe.ai.tools import QuantSpec, ResearchData, ResearchDataAPI, regime
from qe.config import DataConfig, RunConfig, StrategyConfig, UniverseConfig
from qe.engine import run_sim
from qe.engine.sim import rebalance_schedule
from qe.journal import read_journal
from qe.strategy import Context, FactorBookStrategy

SPEC = QuantSpec("delivery", 40, 10)
INCEPTION = date(2024, 9, 30)


def _book(panel, tmp_path) -> RunConfig:
    return RunConfig(
        name="parity-book",
        mode="sim",
        start_date=INCEPTION,
        end_date=panel.date_at(len(panel.index) - 1),
        universe=UniverseConfig(symbols=None),
        data=DataConfig(lake_root="unused"),
        journal_dir=str(tmp_path / "journals"),
        strategy=StrategyConfig(factor="delivery", top_n=40, k=10),
    )


def _random_signals(api, rows, rng) -> dict[str, ResearchSignal]:
    out = {}
    for sym in rng.sample(sorted(rows), 25):
        a, c = rng.uniform(-1, 1), rng.uniform(0, 1)
        status = dict.fromkeys(COMPONENTS, ComponentStatus.SKIPPED) | {
            "technical": ComponentStatus.OK
        }
        out[sym] = ResearchSignal(
            research_id="r",
            trace_id="t",
            symbol=sym,
            market="NSE",
            timestamp=api.cutoff,
            information_cutoff=api.cutoff,
            research_mode="FAST",
            market_regime="RISK_ON",
            technical_score=a,
            component_status=status,
            ai_score=a,
            ai_confidence=c,
            model_version="m",
            prompt_version="p",
            knowledge_cutoffs={"m": date(2020, 1, 1)},  # clean: would be usable if weighted
            guard_days=90,
            contamination_risk=False,
        )
    return out


def _held_after_each_rebalance(journal_path) -> dict[str, set[str]]:
    qty: dict[str, int] = defaultdict(int)
    held = {}
    for rec in read_journal(journal_path):
        if rec["type"] == "REBALANCE":
            for o in rec["data"]["orders"]:
                qty[o["symbol"]] += o["qty"] if o["side"] == "BUY" else -o["qty"]
            held[rec["data"]["date"]] = {s for s, q in qty.items() if q > 0}
    return held


def test_advisory_equals_engine_at_every_rebalance(synthetic_panel, tmp_path):
    rng = random.Random(7)
    sim = run_sim(_book(synthetic_panel, tmp_path), base_dir=tmp_path, panel=synthetic_panel)
    held = _held_after_each_rebalance(sim.journal_path)
    schedule = rebalance_schedule(synthetic_panel, INCEPTION)
    assert len(schedule) >= 5 and len(held) == len(schedule)

    strategy = FactorBookStrategy(factor="delivery", top_n=40, k=10)
    for d, pos in schedule:
        api = ResearchDataAPI(synthetic_panel, pos, "NSE")
        rows = quant_rows(api, SPEC, HardRiskConfig())
        rep = fuse(
            rows,
            _random_signals(api, rows, rng),
            FusionConfig(),  # committed default: AI_ADVISORY, weight 0
            context="shadow",
            k=SPEC.k,
            decision_date=api.decision_date,
            information_cutoff=api.cutoff,
            regime=regime(api)[1],
        )
        targets = set(strategy.rebalance(Context.at(synthetic_panel, pos)))
        assert set(rep.selected) == targets, d
        assert set(rep.selected) == held[d.isoformat()], d  # what the engine actually held
        assert rep.divergences == []


def test_fusion_view_annotates_the_engine_record(synthetic_panel, tmp_path):
    book = _book(synthetic_panel, tmp_path)
    sim = run_sim(book, base_dir=tmp_path, panel=synthetic_panel)
    d, _pos = rebalance_schedule(synthetic_panel, INCEPTION)[-1]
    data = ResearchData(synthetic_panel, "ds-test", {})
    cfg = ResearchRunConfig(
        name="view",
        book_config="unused.yaml",
        research_mode="STANDARD",
        quick_model=ModelProfile(model_id="fake-quick", tier="quick"),
        deep_model=ModelProfile(model_id="fake-deep", tier="deep"),
        max_symbols=4,
    )
    res = run_research(cfg, as_of=d, base_dir=tmp_path, book=book, data=data, client=FakeLLM())
    run = run_fusion(
        res.journal_path,
        FusionConfig(),
        base_dir=tmp_path,
        engine_journal=sim.journal_path.relative_to(tmp_path),
        book=book,
        data=data,
    )
    assert run.report.divergences == []
    assert run.engine["risk"]["approved"] is True and run.engine["rebalance"] is not None
    md = (run.out_dir / "fusion-ai_advisory-shadow.md").read_text()
    for heading in (
        "AI recommendation vs QuantEmbrace decision",
        "QuantEmbrace decision",
        "Market regime",
    ):
        assert heading in md
    assert run.out_dir.parent == tmp_path / "reports" / "qe-ai"

    with pytest.raises(FusionRefused, match="lake changed"):
        run_fusion(res.journal_path, FusionConfig(), base_dir=tmp_path, book=book,
                   data=ResearchData(synthetic_panel, "ds-other", {}))  # fmt: skip
