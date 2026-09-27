"""P6 hypothesis drafts: code-computed flags, pre-registrable gates, no
governance writes, CANDIDATE-only."""

from datetime import date
import json
from pathlib import Path
import shutil

import pytest

from qe.ai.agents.hypothesis import SPEC
from qe.ai.cli import main
from qe.ai.config import ModelProfile, ResearchRunConfig
from qe.ai.hypotheses import generate_hypotheses, load_eliminated_families, match_eliminated
from qe.ai.llm import FakeLLM
from qe.ai.models import ComponentStatus
from qe.ai.orchestration import run_research
from qe.ai.tools import ResearchData
from qe.config import DataConfig, RunConfig, StrategyConfig, UniverseConfig
from qe.journal import read_journal

REPO = Path(__file__).resolve().parents[3]
BOOK = RunConfig(
    name="book",
    mode="sim",
    start_date=date(2024, 1, 1),
    end_date=date(2025, 6, 1),
    universe=UniverseConfig(symbols=None),
    data=DataConfig(lake_root="unused"),
    strategy=StrategyConfig(factor="delivery", top_n=40, k=10),
)
CFG = ResearchRunConfig(
    name="hyp",
    book_config="unused.yaml",
    quick_model=ModelProfile(model_id="fake-quick", tier="quick"),
    deep_model=ModelProfile(model_id="fake-deep", tier="deep"),
    max_symbols=3,
)
GATE = {"name": "sharpe-min", "metric": "sharpe", "op": ">=", "value": 1.0}


def _hyp(**over) -> dict:
    h = {
        "name": "quality-tilt",
        "family": "quality-factor",
        "hypothesis": "High-ROE names outperform within the liquid universe.",
        "rationale": "Test.",
        "required_data": ["nse_eod_prices"],
        "proposed_study_kind": "walk_forward",
        "proposed_gates": [GATE],
    }
    return h | over


def _reply(*hyps) -> str:
    return json.dumps({"hypotheses": list(hyps), "evidence_ids": []})


@pytest.fixture()
def base(tmp_path, synthetic_panel):
    (tmp_path / "governance").mkdir()
    shutil.copy(REPO / "governance" / "research-eliminated-families.yaml", tmp_path / "governance")
    res = run_research(
        CFG,
        as_of=synthetic_panel.date_at(400),
        base_dir=tmp_path,
        book=BOOK,
        data=ResearchData(synthetic_panel, "ds-test", {}),
        client=FakeLLM(),
    )
    return tmp_path, res.journal_path


def test_prompt_hash_is_pinned():
    # changing the hypothesis prompt text without bumping its version must fail here
    assert (SPEC.prompt_version, SPEC.prompt_hash) == ("hypothesis/1", "75183f0722883263")


def test_drafts_are_candidate_only_and_never_touch_governance(base):
    tmp, journal = base
    gov_before = sorted(p.name for p in (tmp / "governance").iterdir())
    run = generate_hypotheses(journal, base_dir=tmp, client=FakeLLM())
    assert run.status is ComponentStatus.OK and len(run.drafts) == 1
    d = run.drafts[0]
    assert d.status == "CANDIDATE" and d.requires_human_review is True
    assert d.testable_now and d.eliminated_family is None and d.family_experiment_count == 0
    assert run.out_dir == tmp / "reports" / "qe-ai" / "hypotheses" / d.research_id
    assert (run.out_dir / "drafts.md").exists() and (run.out_dir / "drafts.jsonl").exists()
    assert sorted(p.name for p in (tmp / "governance").iterdir()) == gov_before
    header = next(read_journal(run.journal_path))["data"]
    assert header["mode"] == "ai-hypotheses"


def test_re_proposal_of_a_settled_family_is_flagged_by_code(base):
    tmp, journal = base
    hyp = _hyp(family="overnight-edge", hypothesis="Harvest the overnight return on NIFTY futures.")
    run = generate_hypotheses(journal, base_dir=tmp, client=FakeLLM(script=[_reply(hyp)]))
    d = run.drafts[0]
    assert d.eliminated_family == "overnight-index-futures" and d.eliminated_status == "DEAD"
    assert "Re-proposal of settled family" in (run.out_dir / "drafts.md").read_text()


def test_untestable_data_is_flagged(base):
    tmp, journal = base
    hyp = _hyp(required_data=["nse_eod_prices", "fundamentals"])
    d = generate_hypotheses(journal, base_dir=tmp, client=FakeLLM(script=[_reply(hyp)])).drafts[0]
    assert d.testable_now is False and d.missing_data == ("fundamentals",)


@pytest.mark.parametrize(
    "bad",
    [
        _hyp(proposed_gates=[]),  # ungated (F-11)
        _hyp(proposed_gates=[GATE | {"metric": "alpha_vibes"}]),  # not a study metric
        _hyp(required_data=["crystal_ball"]),  # unknown data kind
        _hyp(name="Not A Slug!"),
    ],
)
def test_invalid_drafts_are_malformed_not_written(base, bad):
    tmp, journal = base
    run = generate_hypotheses(journal, base_dir=tmp, client=FakeLLM(script=[_reply(bad)] * 2))
    assert run.status is ComponentStatus.MALFORMED and run.drafts == () and run.out_dir is None


def test_family_budget_comes_from_the_ledger(base):
    tmp, journal = base
    ledger = tmp / "governance" / "experiment-registry.jsonl"
    ledger.write_text(
        "".join(
            json.dumps({"experiment_id": f"exp-{i}", "family": "quality-factor"}) + "\n"
            for i in (1, 2, 2)
        )
    )
    d = generate_hypotheses(journal, base_dir=tmp, client=FakeLLM(script=[_reply(_hyp())])).drafts[
        0
    ]
    assert d.family_experiment_count == 2  # distinct experiments, not runs


@pytest.mark.parametrize(
    "family,text,expected",
    [
        ("calendar-play", "buy the turn of month window", "calendar-turn-of-month"),
        ("vol-harvest", "sell an iron condor monthly", "static-short-index-vol"),
        ("quality-factor", "high ROE outperforms", None),
        ("gapless", "no gap here, just momentum in general", None),  # 'gap' alone is not an alias
    ],
)
def test_eliminated_family_matching(family, text, expected):
    fams = load_eliminated_families(REPO)
    hit = match_eliminated(family, text, fams)
    assert (hit["id"] if hit else None) == expected


def test_cli_hypothesize(base, capsys):
    tmp, journal = base
    rc = main(["hypothesize", "--research", str(journal.relative_to(tmp)), "--base-dir", str(tmp)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CANDIDATE draft(s)" in out and "a human decides" in out
