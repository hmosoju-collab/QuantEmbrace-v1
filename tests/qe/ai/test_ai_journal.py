"""Research journal: complete event stream, provenance header, redaction,
sanitized aborts, and derived reports."""

from datetime import date
import json

import pytest

from qe.ai.config import ModelProfile, ResearchRunConfig
from qe.ai.llm import FakeLLM
from qe.ai.llm.fake import default_payload
from qe.ai.orchestration import graph, run_research
from qe.ai.reporting import load_research_journal, write_research_report
from qe.ai.tools import ResearchData
from qe.config import DataConfig, RunConfig, StrategyConfig, UniverseConfig
from qe.journal import JournalWriter, read_journal

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
    name="j",
    book_config="unused.yaml",
    research_mode="STANDARD",
    quick_model=ModelProfile(model_id="fake-quick", tier="quick"),
    deep_model=ModelProfile(model_id="fake-deep", tier="deep"),
    max_symbols=2,
)
SECRET = "sk-ant-api03-SECRETSECRETSECRET123"


def _run(tmp_path, panel, client=None):
    return run_research(
        CFG,
        as_of=panel.date_at(400),
        base_dir=tmp_path,
        book=BOOK,
        data=ResearchData(panel, "ds-test", {}),
        client=client or FakeLLM(),
    )


def test_event_stream_is_complete_and_provenanced(tmp_path, synthetic_panel):
    res = _run(tmp_path, synthetic_panel)
    recs = list(read_journal(res.journal_path))
    kinds = [r["type"] for r in recs]
    assert kinds[0] == "SESSION_START" and kinds[1] == "RUN_MANIFEST" and kinds[-1] == "SESSION_END"
    for k in ("TOOL_CALL", "LLM_CALL", "AGENT_OBSERVATION", "RESEARCH_SIGNAL"):
        assert k in kinds
    head = recs[0]["data"]
    assert head["mode"] == "ai-research" and head["config_hash"] == CFG.config_hash()
    assert head["data_snapshot_id"] == "ds-test" and head["code_sha"]
    man = recs[1]["data"]
    for k in (
        "as_of",
        "information_cutoff",
        "book_config_hash",
        "prompts",
        "knowledge_cutoffs_used",
        "seed",
    ):
        assert k in man
    assert res.journal_path.parent == tmp_path / "journals" / "ai"
    assert not res.journal_path.name.startswith("paper-")


def test_llm_text_secrets_are_redacted_in_the_journal(tmp_path, synthetic_panel):
    def leaky(req):
        payload = default_payload(req)
        if "summary" in payload:
            payload["summary"] = f"analysis mentions {SECRET}"
        return json.dumps(payload)

    res = _run(tmp_path, synthetic_panel, FakeLLM(leaky))
    text = res.journal_path.read_text()
    assert SECRET not in text and "[REDACTED]" in text


def test_abort_is_journaled_and_sanitized(tmp_path, synthetic_panel, monkeypatch):
    def boom(api):
        raise RuntimeError("regime failed password=hunter2hunter2")

    monkeypatch.setattr(graph, "regime", boom)
    with pytest.raises(RuntimeError):
        _run(tmp_path, synthetic_panel)
    journal = next((tmp_path / "journals" / "ai").glob("*.jsonl"))
    last = list(read_journal(journal))[-1]
    assert last["type"] == "SESSION_ABORT" and "[REDACTED]" in last["data"]["error"]
    assert "hunter2hunter2" not in journal.read_text()


def test_report_is_derived_from_the_journal(tmp_path, synthetic_panel):
    res = _run(tmp_path, synthetic_panel)
    out = write_research_report(res.journal_path, tmp_path)
    assert out == tmp_path / "reports" / "qe-ai" / res.run_id
    lines = (out / "signals.jsonl").read_text().splitlines()
    assert len(lines) == len(res.report.signals) == 2
    summary = (out / "summary.md").read_text()
    assert "Advisory research only" in summary and "Contamination:** 2/2" in summary
    view = load_research_journal(res.journal_path)
    assert [s.symbol for s in view.signals] == [s.symbol for s in res.report.signals]


def test_report_refuses_non_research_journals(tmp_path):
    path = tmp_path / "journals" / "paper-x.jsonl"
    with JournalWriter(path) as w:
        w.session_start(
            session_id="x",
            mode="paper",
            config_hash="h",
            config={},
            code_sha="c",
            data_snapshot_id=None,
        )
    with pytest.raises(ValueError, match=r"not a qe\.ai research journal"):
        load_research_journal(path)
