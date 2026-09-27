"""Deterministic research fusion (ADR-043 §5; docs/operations/ai-configuration.md).

Per symbol s at decision date t:

    q   = 2·rank_pct(book factor | liquid universe) - 1          (None if no score)
    a   = ResearchSignal.ai_score (mean of OK directional analysts), c = ai_confidence
    w   = cfg.ai_weight   if the AI view is USABLE, else 0
          USABLE ⇔ mode ∈ {AI_WEIGHTED, AI_EXPERIMENTAL} ∧ context = study
                   ∧ signal present for exactly this decision cutoff
                   ∧ not contaminated ∧ a, c available
    S   = q                     if w == 0   (identity — no arithmetic, no NaN leak)
          (1-w)·q + w·c·a       otherwise

    decision = REJECT        if any hard risk flag            (always wins)
               NO_DECISION   if q is None                     (AI alone never decides)
               DISABLED/ADVISORY:   SELECT ⇔ the engine's own pick
               WEIGHTED/EXPERIMENTAL: SELECT ⇔ top-k by (S, q, symbol)

The AI recommendation (sign of a with a dead zone) is reported NEXT TO the
decision and never merged into it. Nothing here reaches the engine: the output
is a research view in reports/qe-ai/.
"""

from collections.abc import Mapping
from datetime import date, datetime
from typing import Literal

from qe.ai.fusion.config import STUDY_ONLY_MODES, FusionConfig, FusionContext
from qe.ai.fusion.quant import QuantRow
from qe.ai.models import ResearchSignal
from qe.ai.tools import RegimeReading
from qe.config import FrozenModel

Decision = Literal["SELECT", "NOT_SELECTED", "NO_DECISION", "REJECT"]
AIRecommendation = Literal["POSITIVE", "NEGATIVE", "NEUTRAL", "UNAVAILABLE", "DISABLED"]


class FusionRefused(RuntimeError):
    pass


class FusionRow(FrozenModel):
    symbol: str
    market_regime: str
    regime_value: float | None
    q: float | None
    factor_score: float | None
    ai_score: float | None
    ai_confidence: float | None
    contaminated: bool | None
    ai_usable: bool
    ai_excluded_reason: str | None
    weight: float
    fused_score: float | None
    hard_flags: tuple[str, ...]
    quant_decision: Decision  # what QuantEmbrace's engine does on its own
    final_decision: Decision  # fused decision under this config
    ai_recommendation: AIRecommendation
    ai_agrees: bool | None  # AI recommendation vs final decision
    bull_case: str = ""
    bear_case: str = ""
    consensus: str = ""
    conflicting_evidence: tuple[str, ...] = ()


class FusionReport(FrozenModel):
    schema_version: Literal["fusion_report/1"] = "fusion_report/1"
    fusion_config_hash: str
    mode: str
    context: str
    decision_date: date
    information_cutoff: datetime
    research_id: str | None
    rows: tuple[FusionRow, ...]
    ignored_signals: tuple[str, ...] = ()  # signals for a different decision cutoff

    @property
    def selected(self) -> list[str]:
        return [r.symbol for r in self.rows if r.final_decision == "SELECT"]

    @property
    def divergences(self) -> list[str]:
        return [r.symbol for r in self.rows if r.final_decision != r.quant_decision]


def _quant_decision(row: QuantRow) -> Decision:
    if row.hard_flags:
        return "REJECT"
    if row.q is None:
        return "NO_DECISION"
    return "SELECT" if row.selected else "NOT_SELECTED"


def _usable(sig: ResearchSignal | None, cfg: FusionConfig, context: str) -> tuple[bool, str | None]:
    if cfg.mode not in STUDY_ONLY_MODES:
        return False, f"{cfg.mode}: AI carries no weight"
    if context != "study":
        return False, "AI weight is allowed only in the study context"
    if sig is None:
        return False, "no research signal"
    if sig.contamination_risk:
        return False, "contaminated: decision date within model knowledge cutoff + guard"
    if sig.ai_score is None or sig.ai_confidence is None:
        return False, "ai_score / ai_confidence unavailable"
    return True, None


def _recommendation(sig: ResearchSignal | None, cfg: FusionConfig) -> AIRecommendation:
    if cfg.mode == "AI_DISABLED":
        return "DISABLED"
    if sig is None or sig.ai_score is None:
        return "UNAVAILABLE"
    if sig.ai_score > cfg.recommendation_dead_zone:
        return "POSITIVE"
    if sig.ai_score < -cfg.recommendation_dead_zone:
        return "NEGATIVE"
    return "NEUTRAL"


def _agrees(rec: AIRecommendation, decision: Decision) -> bool | None:
    if rec in ("UNAVAILABLE", "DISABLED", "NEUTRAL") or decision in ("NO_DECISION",):
        return None
    positive = rec == "POSITIVE"
    return positive == (decision == "SELECT")


def fuse(
    rows: Mapping[str, QuantRow],
    signals: Mapping[str, ResearchSignal],
    cfg: FusionConfig,
    *,
    context: FusionContext,
    k: int,
    decision_date: date,
    information_cutoff: datetime,
    regime: RegimeReading,
    research_id: str | None = None,
) -> FusionReport:
    if context not in cfg.allowed_contexts:
        raise FusionRefused(
            f"context {context!r} is not in allowed_contexts {cfg.allowed_contexts}"
        )
    if cfg.mode in STUDY_ONLY_MODES and context != "study":
        raise FusionRefused(f"{cfg.mode} may run only in the study context (got {context!r})")

    # A signal researched for a different decision cutoff (especially a LATER
    # one) must never annotate this decision — ignore it outright.
    matched = {s: sig for s, sig in signals.items() if sig.information_cutoff == information_cutoff}
    ignored = tuple(sorted(set(signals) - set(matched)))
    use_ai = cfg.mode != "AI_DISABLED"

    staged = []
    for sym, row in rows.items():
        sig = matched.get(sym) if use_ai else None
        usable, reason = _usable(sig, cfg, context)
        w = cfg.ai_weight if usable else 0.0
        if row.q is None:
            fused = None
        elif w == 0.0:
            fused = row.q
        else:
            fused = (1.0 - w) * row.q + w * sig.ai_confidence * sig.ai_score
        staged.append((row, sig, usable, reason, w, fused))

    weighted_pick: set[str] = set()
    if cfg.mode in STUDY_ONLY_MODES:
        ranked = sorted(
            (
                (f, r.q, r.symbol)
                for r, _s, _u, _re, _w, f in staged
                if f is not None and not r.hard_flags
            ),
            key=lambda t: (-t[0], -t[1], t[2]),
        )
        weighted_pick = {sym for _f, _q, sym in ranked[:k]}

    out = []
    for row, sig, usable, reason, w, fused in staged:
        quant_dec = _quant_decision(row)
        if quant_dec in ("REJECT", "NO_DECISION"):
            final: Decision = quant_dec
        elif cfg.mode in STUDY_ONLY_MODES:
            final = "SELECT" if row.symbol in weighted_pick else "NOT_SELECTED"
        else:
            final = quant_dec  # DISABLED / ADVISORY: identical to the engine
        rec = _recommendation(sig, cfg)
        out.append(
            FusionRow(
                symbol=row.symbol,
                market_regime=regime.label,
                regime_value=regime.value,
                q=row.q,
                factor_score=row.factor_score,
                ai_score=sig.ai_score if sig else None,
                ai_confidence=sig.ai_confidence if sig else None,
                contaminated=sig.contamination_risk if sig else None,
                ai_usable=usable,
                ai_excluded_reason=reason,
                weight=w,
                fused_score=fused,
                hard_flags=row.hard_flags,
                quant_decision=quant_dec,
                final_decision=final,
                ai_recommendation=rec,
                ai_agrees=_agrees(rec, final),
                bull_case=sig.bull_case if sig else "",
                bear_case=sig.bear_case if sig else "",
                consensus=sig.consensus if sig else "",
                conflicting_evidence=tuple(e.evidence_id for e in sig.contradicting_evidence)
                if sig
                else (),
            )
        )
    return FusionReport(
        fusion_config_hash=cfg.config_hash(),
        mode=cfg.mode,
        context=context,
        decision_date=decision_date,
        information_cutoff=information_cutoff,
        research_id=research_id,
        rows=tuple(out),
        ignored_signals=ignored,
    )
