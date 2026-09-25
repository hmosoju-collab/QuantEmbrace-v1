"""Fusion: ADVISORY invariance (property-based), hard-risk veto, mode
ceilings and context gating, contamination exclusion, quant fallback."""

from datetime import date, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st
import pydantic
import pytest

from qe.ai.fusion import FusionConfig, FusionRefused, HardRiskConfig, fuse, quant_rows
from qe.ai.models import COMPONENTS, ComponentStatus, ResearchSignal
from qe.ai.tools import QuantSpec, ResearchDataAPI, regime

POS = 400
SPEC = QuantSpec("delivery", 40, 10)
OK, SKIPPED = ComponentStatus.OK, ComponentStatus.SKIPPED


@pytest.fixture(scope="module")
def env(synthetic_panel):
    api = ResearchDataAPI(synthetic_panel, POS, "NSE")
    rows = quant_rows(api, SPEC, HardRiskConfig())
    return api, rows, regime(api)[1]


def signal(sym, a, c, *, contaminated=False, cutoff=None, api=None) -> ResearchSignal:
    cutoff = cutoff or api.cutoff
    ok = a is not None
    status = dict.fromkeys(COMPONENTS, SKIPPED) | {
        "technical": OK if ok else ComponentStatus.TIMEOUT
    }
    return ResearchSignal(
        research_id="r",
        trace_id="t",
        symbol=sym,
        market="NSE",
        timestamp=cutoff,
        information_cutoff=cutoff,
        research_mode="FAST",
        market_regime="RISK_ON",
        technical_score=a,
        component_status=status,
        ai_score=a,
        ai_confidence=c,
        model_version="m",
        prompt_version="p",
        knowledge_cutoffs={"m": None} if contaminated else {"m": date(2020, 1, 1)},
        guard_days=90,
        contamination_risk=contaminated,
    )


def _fuse(env, signals, cfg, context="shadow", rows=None):
    api, base_rows, reading = env
    return fuse(
        rows or base_rows,
        signals,
        cfg,
        context=context,
        k=SPEC.k,
        decision_date=api.decision_date,
        information_cutoff=api.cutoff,
        regime=reading,
    )


def _decisions(report):
    return {r.symbol: (r.final_decision, r.fused_score) for r in report.rows}


ai_view = st.tuples(
    st.floats(-1, 1), st.floats(0, 1), st.booleans()
)  # (ai_score, ai_confidence, contaminated)


@settings(max_examples=60, deadline=None)
@given(views=st.lists(ai_view, min_size=1, max_size=40))
@pytest.mark.parametrize("mode", ["AI_ADVISORY", "AI_DISABLED"])
def test_advisory_and_disabled_are_independent_of_every_ai_input(env, mode, views):
    api, rows, _ = env
    syms = list(rows)[: len(views)]
    signals = {
        s: signal(s, a, c, contaminated=x, api=api)
        for s, (a, c, x) in zip(syms, views, strict=False)
    }
    cfg = FusionConfig(mode=mode)
    for context in ("shadow", "study"):
        with_ai = _fuse(env, signals, cfg, context)
        without = _fuse(env, {}, cfg, context)
        assert _decisions(with_ai) == _decisions(without)
        assert with_ai.divergences == [] and all(r.weight == 0.0 for r in with_ai.rows)


def test_advisory_selection_equals_the_engine_pick(env):
    _, rows, _ = env
    rep = _fuse(env, {}, FusionConfig())
    assert set(rep.selected) == {s for s, r in rows.items() if r.selected}
    assert all(r.fused_score == r.q for r in rep.rows)


@settings(max_examples=40, deadline=None)
@given(a=st.floats(-1, 1), c=st.floats(0, 1))
@pytest.mark.parametrize(
    "mode,w", [("AI_ADVISORY", 0.0), ("AI_WEIGHTED", 0.2), ("AI_EXPERIMENTAL", 0.5)]
)
def test_hard_risk_reject_always_wins(env, mode, w, a, c):
    api, _, _reading = env
    flagged = quant_rows(api, SPEC, HardRiskConfig(max_vol_63d=1e-6))  # everything breaches
    signals = {s: signal(s, a, c, api=api) for s in flagged}
    rep = _fuse(env, signals, FusionConfig(mode=mode, ai_weight=w), "study", rows=flagged)
    assert {r.final_decision for r in rep.rows} == {"REJECT"}


@pytest.mark.parametrize(
    "mode,w",
    [("AI_ADVISORY", 0.01), ("AI_DISABLED", 0.1), ("AI_WEIGHTED", 0.21), ("AI_EXPERIMENTAL", 0.51)],
)
def test_weight_ceilings_are_enforced(mode, w):
    with pytest.raises(pydantic.ValidationError, match="ceiling"):
        FusionConfig(mode=mode, ai_weight=w)


@pytest.mark.parametrize("mode", ["AI_WEIGHTED", "AI_EXPERIMENTAL"])
def test_weighted_modes_refused_outside_study(env, mode):
    with pytest.raises(FusionRefused, match="study"):
        _fuse(env, {}, FusionConfig(mode=mode, ai_weight=0.2), "shadow")
    with pytest.raises(FusionRefused, match="allowed_contexts"):
        _fuse(env, {}, FusionConfig(allowed_contexts=("study",)), "shadow")


def _swap_case(env):
    """A basket name the AI hates and a near-miss name the AI loves."""
    _api, rows, _ = env
    ranked = sorted((r for r in rows.values() if r.q is not None), key=lambda r: -r.q)
    inside = [r for r in ranked if r.selected][-1]  # weakest basket member
    outside = next(r for r in ranked if not r.selected)  # strongest non-member
    return inside.symbol, outside.symbol


def test_weighted_study_can_change_selection_with_clean_signals(env):
    api, _rows, _ = env
    inside, outside = _swap_case(env)
    signals = {
        inside: signal(inside, -1.0, 1.0, api=api),
        outside: signal(outside, 1.0, 1.0, api=api),
    }
    rep = _fuse(env, signals, FusionConfig(mode="AI_WEIGHTED", ai_weight=0.2), "study")
    assert outside in rep.selected and inside not in rep.selected and len(rep.selected) == SPEC.k
    assert set(rep.divergences) == {inside, outside}
    row = next(r for r in rep.rows if r.symbol == outside)
    assert row.fused_score == pytest.approx(0.8 * row.q + 0.2 * 1.0 * 1.0)


def test_contaminated_signals_never_carry_weight(env):
    api, rows, _ = env
    inside, outside = _swap_case(env)
    signals = {
        inside: signal(inside, -1.0, 1.0, contaminated=True, api=api),
        outside: signal(outside, 1.0, 1.0, contaminated=True, api=api),
    }
    rep = _fuse(env, signals, FusionConfig(mode="AI_EXPERIMENTAL", ai_weight=0.5), "study")
    assert set(rep.selected) == {s for s, r in rows.items() if r.selected}
    row = next(r for r in rep.rows if r.symbol == outside)
    assert not row.ai_usable and "contaminated" in row.ai_excluded_reason and row.weight == 0.0


def test_unavailable_ai_falls_back_to_quant(env):
    api, rows, _ = env
    signals = {s: signal(s, None, None, api=api) for s in list(rows)[:15]}
    rep = _fuse(env, signals, FusionConfig(mode="AI_WEIGHTED", ai_weight=0.2), "study")
    assert set(rep.selected) == {s for s, r in rows.items() if r.selected}
    assert all(r.ai_recommendation in ("UNAVAILABLE",) for r in rep.rows if r.symbol in signals)


def test_ai_alone_never_decides(env):
    api, _, _reading = env
    rows = quant_rows(api, SPEC, HardRiskConfig(), extra_symbols=["NOT_IN_PANEL"])
    sig = {"NOT_IN_PANEL": signal("NOT_IN_PANEL", 1.0, 1.0, api=api)}
    rep = _fuse(env, sig, FusionConfig(mode="AI_EXPERIMENTAL", ai_weight=0.5), "study", rows=rows)
    row = next(r for r in rep.rows if r.symbol == "NOT_IN_PANEL")
    assert row.final_decision == "REJECT" and "no_price" in row.hard_flags
    assert row.ai_recommendation == "POSITIVE"  # shown, but it decides nothing


def test_signals_for_another_cutoff_are_ignored(env):
    api, rows, _ = env
    sym = next(iter(rows))
    later = signal(sym, 1.0, 1.0, cutoff=api.cutoff + timedelta(days=1))
    rep = _fuse(env, {sym: later}, FusionConfig(mode="AI_WEIGHTED", ai_weight=0.2), "study")
    assert rep.ignored_signals == (sym,)
    assert next(r for r in rep.rows if r.symbol == sym).ai_score is None


@pytest.mark.parametrize(
    "a,rec", [(0.5, "POSITIVE"), (-0.5, "NEGATIVE"), (0.1, "NEUTRAL"), (None, "UNAVAILABLE")]
)
def test_ai_recommendation_is_reported_next_to_the_decision(env, a, rec):
    api, rows, _ = env
    sym = next(s for s, r in rows.items() if r.selected)
    rep = _fuse(env, {sym: signal(sym, a, 0.9 if a is not None else None, api=api)}, FusionConfig())
    row = next(r for r in rep.rows if r.symbol == sym)
    assert row.ai_recommendation == rec and row.final_decision == "SELECT"
    assert row.ai_agrees == {"POSITIVE": True, "NEGATIVE": False}.get(rec)
    disabled = _fuse(env, {sym: signal(sym, 0.5, 0.9, api=api)}, FusionConfig(mode="AI_DISABLED"))
    assert next(r for r in disabled.rows if r.symbol == sym).ai_recommendation == "DISABLED"


def test_committed_fusion_config_is_advisory_zero_weight():
    cfg = FusionConfig.from_yaml("configs/research_fusion.yaml")
    assert cfg.mode == "AI_ADVISORY" and cfg.ai_weight == 0.0
