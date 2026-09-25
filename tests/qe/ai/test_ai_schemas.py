"""ResearchSignal v1 / config schemas: versioned, bounded, frozen, self-consistent."""

from datetime import date, datetime, timedelta

import pydantic
import pytest

from qe.ai.config import ModelProfile, ResearchRunConfig
from qe.ai.models import (
    COMPONENTS,
    AgentObservation,
    ComponentStatus,
    Evidence,
    LookaheadViolation,
    ResearchSignal,
    is_contaminated,
)
from qe.clock import IST

CUTOFF = datetime(2026, 7, 31, 15, 30, tzinfo=IST)
OK = ComponentStatus.OK
UNAV = ComponentStatus.UNAVAILABLE


def _ev(ts: datetime = CUTOFF, eid: str = "tech.ret_21d") -> Evidence:
    return Evidence(
        evidence_id=eid, tool="tech", symbol="AAA", knowledge_ts=ts, value=0.1, summary="x"
    )


def _signal(**over) -> ResearchSignal:
    status = dict.fromkeys(COMPONENTS, UNAV) | {"technical": OK, "risk": OK}
    kw = {
        "research_id": "ai-run",
        "trace_id": "t-1",
        "symbol": "AAA",
        "market": "NSE",
        "timestamp": CUTOFF,
        "information_cutoff": CUTOFF,
        "research_mode": "FAST",
        "market_regime": "RISK_ON",
        "regime_confidence": 0.4,
        "technical_score": 0.5,
        "risk_score": 0.3,
        "component_status": status,
        "ai_score": 0.5,
        "ai_confidence": 0.6,
        "supporting_evidence": (_ev(),),
        "model_version": "quick=m1;deep=m2",
        "prompt_version": "technical/1",
        "knowledge_cutoffs": {"m1": date(2025, 1, 31), "m2": date(2025, 1, 31)},
        "guard_days": 90,
        "contamination_risk": False,
    }
    kw.update(over)
    return ResearchSignal(**kw)


def test_valid_signal_and_version():
    s = _signal()
    assert s.schema_version == "research_signal/1"
    assert s.ai_score == 0.5 and not s.contamination_risk


def test_signal_is_frozen_and_forbids_extra_fields():
    s = _signal()
    with pytest.raises(pydantic.ValidationError):
        s.ai_score = 0.9
    with pytest.raises(pydantic.ValidationError):
        _signal(buy_now=True)


@pytest.mark.parametrize(
    "field,value", [("technical_score", 1.5), ("risk_score", -0.1), ("ai_confidence", 2.0)]
)
def test_scores_are_bounded(field, value):
    with pytest.raises(pydantic.ValidationError):
        _signal(**{field: value})


def test_evidence_after_cutoff_fails_closed():
    late = _ev(CUTOFF + timedelta(minutes=1))
    with pytest.raises(pydantic.ValidationError, match="after information_cutoff"):
        _signal(supporting_evidence=(late,))
    assert issubclass(LookaheadViolation, ValueError)


def test_ai_score_is_computed_not_claimed():
    with pytest.raises(pydantic.ValidationError, match="ai_score"):
        _signal(ai_score=0.9)


def test_status_and_score_must_agree():
    with pytest.raises(pydantic.ValidationError, match="technical"):
        _signal(technical_score=None)  # status OK but no score
    status = dict.fromkeys(COMPONENTS, UNAV) | {"risk": OK}
    with pytest.raises(pydantic.ValidationError):
        _signal(component_status=status)  # technical UNAVAILABLE but score present


def test_contamination_is_computed_not_claimed():
    # decision 2026-07-31 vs cutoff 2026-06-30 (+90d guard) ⇒ contaminated
    with pytest.raises(pydantic.ValidationError, match="contamination_risk"):
        _signal(knowledge_cutoffs={"m1": date(2026, 6, 30)}, contamination_risk=False)
    assert _signal(knowledge_cutoffs={"m1": date(2026, 6, 30)}, contamination_risk=True)


def test_contamination_rule():
    d = date(2026, 7, 31)
    assert is_contaminated(d, {"m": None}, 90)  # unknown cutoff
    assert is_contaminated(d, {}, 90)  # no models declared
    assert is_contaminated(d, {"m": date(2026, 5, 2)}, 90)  # inside guard
    assert not is_contaminated(d, {"m": date(2026, 5, 1)}, 90)  # 91 days after cutoff
    assert is_contaminated(d, {"a": date(2020, 1, 1), "b": date(2026, 7, 1)}, 90)  # any model


def test_naive_timestamps_rejected():
    with pytest.raises(pydantic.ValidationError):
        _ev(datetime(2026, 7, 31, 15, 30))
    with pytest.raises(pydantic.ValidationError, match="timezone-aware"):
        _signal(timestamp=datetime(2026, 7, 31, 15, 30))


def test_observation_non_ok_carries_no_values():
    with pytest.raises(pydantic.ValidationError):
        AgentObservation(agent_id="news", symbol="AAA", status=UNAV, score=0.2)
    obs = AgentObservation(agent_id="news", symbol="AAA", status=UNAV)
    assert obs.score is None and obs.llm_calls == 0


def test_research_config_hash_is_stable_under_new_optional_fields():
    cfg = ResearchRunConfig.from_yaml("configs/qe_ai_research.yaml")
    assert cfg.backend == "fake" and cfg.research_mode == "FAST"
    # exclude_none: an unset optional field is absent from the canonical form,
    # so adding one later (default None) cannot move this hash (ADR-042 lesson).
    assert '"symbols"' not in __import__("qe.ai.config", fromlist=["x"]).canonical_json(cfg)
    assert (
        cfg.config_hash()
        == ResearchRunConfig.from_yaml("configs/qe_ai_research.yaml").config_hash()
    )


def test_model_tiers_enforced():
    with pytest.raises(pydantic.ValidationError, match="tier"):
        ResearchRunConfig(
            name="x",
            book_config="b.yaml",
            quick_model=ModelProfile(model_id="d", tier="deep"),
            deep_model=ModelProfile(model_id="d", tier="deep"),
        )
