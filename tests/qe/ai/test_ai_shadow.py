"""P6 forward AI shadow gate: pre-registration discipline, fail-closed
accrual rules, incremental-IC math, verdict logic."""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pydantic
import pytest
import yaml

from qe.ai.cli import main
from qe.ai.config import ResearchRunConfig
from qe.ai.llm import FakeLLM
from qe.ai.orchestration import run_research
from qe.ai.shadow import ShadowGateConfig, evaluate, incremental_ic, run_shadow
from qe.ai.tools import ResearchData
from qe.config import RunConfig

REPO = Path(__file__).resolve().parents[3]
BOOK = {
    "name": "book",
    "mode": "sim",
    "start_date": "2024-01-01",
    "end_date": "2025-06-01",
    "universe": {"market": "NSE", "segment": "EQ", "symbols": None},
    "data": {"lake_root": "unused"},
    "strategy": {"kind": "factor_book", "factor": "delivery", "top_n": 40, "k": 10},
}


def _ai_cfg(cutoff: str | None = "2020-01-01", name: str = "shadow") -> dict:
    return {
        "name": name,
        "book_config": "configs/book.yaml",
        "research_mode": "FAST",
        "quick_model": {"model_id": "fake-quick", "tier": "quick", "knowledge_cutoff": cutoff},
        "deep_model": {"model_id": "fake-deep", "tier": "deep", "knowledge_cutoff": cutoff},
        "max_symbols": 10,
    }


def _gate(**over) -> dict:
    g = {
        "status": "SIGNED_OFF",
        "signed_off_by": "Hari",
        "signed_off_on": None,
        "research_config": "configs/ai.yaml",
        "research_config_hash": None,
        "model_id": "fake-quick",
        "horizon_days": 21,
        "min_names_per_month": 8,
    }
    return g | over


def _month_ends(panel) -> list[int]:
    idx = pd.DatetimeIndex(panel.index)
    s = pd.Series(range(len(idx)), index=idx)
    return list(s.groupby(idx.to_period("M")).max())


@pytest.fixture()
def env(tmp_path, synthetic_panel):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "book.yaml").write_text(yaml.safe_dump(BOOK))
    (tmp_path / "configs" / "ai.yaml").write_text(yaml.safe_dump(_ai_cfg()))
    cfg = ResearchRunConfig.from_yaml(tmp_path / "configs" / "ai.yaml")
    book = RunConfig.model_validate(BOOK)
    data = ResearchData(synthetic_panel, "ds-test", {})

    def research(pos, config=cfg):
        return run_research(
            config,
            as_of=synthetic_panel.date_at(pos),
            base_dir=tmp_path,
            book=book,
            data=data,
            client=FakeLLM(),
        )

    return tmp_path, cfg, book, data, research


def _write_gate(base, **over):
    (base / "configs" / "gate.yaml").write_text(
        yaml.safe_dump(_gate(**over), default_flow_style=False)
    )


def test_committed_gate_is_an_unsigned_draft_mirroring_the_forward_gate():
    g = ShadowGateConfig.from_yaml(REPO / "configs" / "qe_ai_shadow_gate.yaml")
    assert g.status == "DRAFT" and g.signed_off_by is None and g.research_config_hash is None
    t = g.thresholds
    assert (t.min_months, t.min_ic_ir, t.min_positive_frac, t.max_single_month_share) == (
        12, 0.50, 0.58, 0.50
    )  # fmt: skip


def test_signed_off_gate_requires_its_binding():
    with pytest.raises(pydantic.ValidationError, match="binding"):
        ShadowGateConfig.model_validate(_gate(signed_off_on=None, research_config_hash=None))


def test_draft_never_yields_a_verdict():
    g = ShadowGateConfig.model_validate(_gate(status="DRAFT"))
    assert evaluate([0.3] * 24, g).verdict == "NOT_EVALUABLE"


def _signed() -> ShadowGateConfig:
    return ShadowGateConfig.model_validate(
        _gate(signed_off_on="2026-01-01", research_config_hash="h")
    )


@pytest.mark.parametrize(
    "ics,verdict",
    [
        ([0.1] * 11, "IN_PROGRESS"),  # < 12 months is never a pass
        ([0.08, 0.12, 0.05, 0.1, -0.02, 0.09, 0.07, 0.11, 0.06, -0.01, 0.1, 0.08], "PASS"),
        ([-0.05] * 12, "FAIL"),  # negative mean
        ([0.9] + [0.01, -0.01] * 5 + [0.01], "FAIL"),  # one month dominates
        ([0.2, -0.19] * 6, "FAIL"),  # IR too low / 50% positive
    ],
)
def test_gate_verdicts(ics, verdict):
    assert evaluate(ics, _signed()).verdict == verdict


def test_incremental_ic_measures_information_beyond_the_factor_rank():
    rng = np.random.default_rng(3)
    q = rng.uniform(-1, 1, 40)
    fwd = rng.normal(0, 0.05, 40)
    assert incremental_ic(q + 10 * fwd, q, fwd) > 0.9  # AI adds the future-return signal
    # AI == (linear function of) the factor rank, or constant: zero incremental info,
    # counted as 0.0 — never dropped (dropping would inflate the IC ratio)
    assert incremental_ic(2 * q + 0.3, q, fwd) == 0.0
    assert incremental_ic(np.full(40, 0.5), q, fwd) == 0.0
    assert incremental_ic(q, q, np.zeros(40)) is None  # outcome unmeasurable
    assert abs(incremental_ic(q + rng.normal(0, 1, 40), q, fwd)) < 0.5  # noise


def test_accrual_rules_fail_closed(env, synthetic_panel):
    base, cfg, book, data, research = env
    me = [p for p in _month_ends(synthetic_panel) if 300 <= p]  # past warmup
    d0, d1, d2, dlast = me[0], me[1], me[2], me[-1]  # dlast: outcome not knowable yet
    research(d0)  # before sign-off → excluded
    research(d1)
    research(d1)  # re-run of the same date → first run counts
    research(d2)
    research(d2, cfg.model_copy(update={"name": "other"}))  # unbound config hash
    research(dlast)
    _write_gate(
        base,
        signed_off_on=synthetic_panel.date_at(d0).isoformat(),
        research_config_hash=cfg.config_hash(),
    )
    rep = run_shadow("configs/gate.yaml", as_of=synthetic_panel.date_at(len(synthetic_panel.index) - 1),
                     base_dir=base, book=book, data=data)  # fmt: skip
    ign = rep.collected.ignored
    assert ign["run_before_sign_off"] == 1
    assert ign["rerun_same_decision_date"] == 1
    assert ign["run_config_not_bound"] == 1
    assert ign["month_outcome_not_yet_known"] == 1
    assert [m["decision_date"] for m in rep.monthly] == [
        synthetic_panel.date_at(d1).isoformat(), synthetic_panel.date_at(d2).isoformat()
    ]  # fmt: skip
    assert all(m["n"] >= 8 for m in rep.monthly)
    assert rep.result.verdict == "IN_PROGRESS"  # 2 months << 12
    assert (rep.out_dir / "report.md").exists() and (rep.out_dir / "summary.json").exists()
    assert rep.out_dir.is_relative_to(base / "reports" / "qe-ai" / "shadow")


def test_contaminated_signals_never_accrue(env, synthetic_panel):
    base, _cfg, book, data, research = env
    (base / "configs" / "ai.yaml").write_text(yaml.safe_dump(_ai_cfg(cutoff=None)))
    cfg = ResearchRunConfig.from_yaml(base / "configs" / "ai.yaml")
    me = [p for p in _month_ends(synthetic_panel) if 300 <= p]
    research(me[1], cfg)
    _write_gate(base, signed_off_on="2020-01-01", research_config_hash=cfg.config_hash())
    rep = run_shadow("configs/gate.yaml", as_of=synthetic_panel.date_at(len(synthetic_panel.index) - 1),
                     base_dir=base, book=book, data=data)  # fmt: skip
    assert rep.collected.observations == [] and rep.collected.ignored["signal_contaminated"] == 10


def test_cli_show_binding_and_draft(env, capsys):
    base, cfg, *_ = env
    _write_gate(base, status="DRAFT", signed_off_by=None, model_id=None)
    assert (
        main(["shadow", "--gate", "configs/gate.yaml", "--show-binding", "--base-dir", str(base)])
        == 0
    )
    shown = capsys.readouterr().out
    assert cfg.config_hash() in shown
    assert "test-double" in shown and "contaminated" not in shown  # fake backend, real cutoff
    (base / "configs" / "ai.yaml").write_text(yaml.safe_dump(_ai_cfg(cutoff=None)))
    main(["shadow", "--gate", "configs/gate.yaml", "--show-binding", "--base-dir", str(base)])
    assert "every signal would be contaminated" in capsys.readouterr().out
    assert (
        main(
            [
                "shadow",
                "--gate",
                "configs/gate.yaml",
                "--as-of",
                "2025-05-01",
                "--base-dir",
                str(base),
            ]
        )
        == 0
    )
    assert "NOT_EVALUABLE" in capsys.readouterr().out
    assert date  # (imported for readability of dates above)
