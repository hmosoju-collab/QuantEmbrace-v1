"""Forward shadow evaluation: research journals → scored months → gate verdict.

Accrual discipline (all enforced here, each exclusion counted in the report):
  * only research runs of the BOUND research config hash and model;
  * only decision dates strictly AFTER the operator's sign-off;
  * only uncontaminated signals with an ai_score;
  * the FIRST completed run per decision date counts — later re-runs are
    ignored, so re-running research cannot cherry-pick a better score;
  * one decision date per calendar month (the first);
  * a month is scored only once its forward return is knowable (decision row
    + horizon exists in the lake at as_of) and has >= min_names scored names.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
import json
from pathlib import Path

import numpy as np

from qe.ai.config import ResearchRunConfig
from qe.ai.paths import AI_JOURNAL_DIR, AI_REPORT_DIR, safe_write_path
from qe.ai.reporting import ADVISORY_BANNER, ResearchJournalView, load_research_journal
from qe.ai.shadow.gate import GateResult, ShadowGateConfig, evaluate, incremental_ic
from qe.ai.tools import QuantSpec, ResearchData, ResearchDataAPI, load_research_data, quant_view
from qe.config import RunConfig
from qe.journal import JournalError

PANEL_WARMUP_DAYS = 730


@dataclass(frozen=True)
class ShadowObservation:
    decision_date: date
    symbol: str
    ai_score: float
    research_id: str


@dataclass
class Collected:
    observations: list[ShadowObservation] = field(default_factory=list)
    runs_used: dict[str, str] = field(default_factory=dict)  # decision date -> research_id
    ignored: Counter = field(default_factory=Counter)


def collect(journal_dir: Path, gate: ShadowGateConfig) -> Collected:
    out = Collected()
    by_date: dict[date, list[ResearchJournalView]] = {}
    for path in sorted(journal_dir.glob("ai-*.jsonl")):
        try:
            view = load_research_journal(path)
        except (ValueError, JournalError, KeyError):
            out.ignored["unreadable_or_not_research"] += 1
            continue
        if (
            gate.research_config_hash
            and view.header.get("config_hash") != gate.research_config_hash
        ):
            out.ignored["run_config_not_bound"] += 1
            continue
        if view.end.get("type") != "SESSION_END":
            out.ignored["incomplete_run"] += 1
            continue
        d = date.fromisoformat(view.manifest["decision_date"])
        if gate.signed_off_on and d <= gate.signed_off_on:
            out.ignored["run_before_sign_off"] += 1
            continue
        by_date.setdefault(d, []).append(view)

    months_seen: set[tuple[int, int]] = set()
    for d in sorted(by_date):
        runs = sorted(by_date[d], key=lambda v: v.header["session_id"])  # UTC stamp ⇒ chronological
        out.ignored["rerun_same_decision_date"] += len(runs) - 1
        if (d.year, d.month) in months_seen:
            out.ignored["extra_decision_date_same_month"] += 1
            continue
        months_seen.add((d.year, d.month))
        first = runs[0]
        out.runs_used[d.isoformat()] = first.header["session_id"]
        for s in first.signals:
            if gate.model_id and gate.model_id not in s.knowledge_cutoffs:
                out.ignored["signal_other_model"] += 1
            elif s.contamination_risk:
                out.ignored["signal_contaminated"] += 1
            elif s.ai_score is None:
                out.ignored["signal_no_ai_score"] += 1
            else:
                out.observations.append(ShadowObservation(d, s.symbol, s.ai_score, s.research_id))
    return out


@dataclass(frozen=True)
class ShadowReport:
    as_of: date
    gate_hash: str
    gate_status: str
    result: GateResult
    monthly: list[dict]
    collected: Collected
    out_dir: Path | None


def _score_months(
    obs: list[ShadowObservation], data: ResearchData, spec: QuantSpec, market: str, gate, ignored
) -> list[dict]:
    panel = data.panel
    dates = [panel.date_at(i) for i in range(len(panel.index))]
    pos_of = {d: i for i, d in enumerate(dates)}
    months = []
    for d in sorted({o.decision_date for o in obs}):
        pos = pos_of.get(d)
        if pos is None:
            ignored["month_decision_date_not_in_lake"] += 1
            continue
        if pos + gate.horizon_days >= len(dates):
            ignored["month_outcome_not_yet_known"] += 1
            continue
        view = quant_view(ResearchDataAPI(panel, pos, market), spec)
        ai, q, fwd = [], [], []
        for o in (o for o in obs if o.decision_date == d):
            p0 = panel.close[o.symbol].iloc[pos] if o.symbol in panel.close else np.nan
            p1 = (
                panel.close[o.symbol].iloc[pos + gate.horizon_days]
                if o.symbol in panel.close
                else np.nan
            )
            if o.symbol in view.q.index and np.isfinite(p0) and np.isfinite(p1) and p0 > 0:
                ai.append(o.ai_score)
                q.append(float(view.q[o.symbol]))
                fwd.append(p1 / p0 - 1.0)
        if len(ai) < gate.min_names_per_month:
            ignored["month_too_few_names"] += 1
            continue
        ic = incremental_ic(np.array(ai), np.array(q), np.array(fwd))
        if ic is None:
            ignored["month_degenerate_ic"] += 1
            continue
        months.append({"decision_date": d.isoformat(), "n": len(ai), "incremental_ic": ic})
    return months


def run_shadow(
    gate_path: str | Path,
    *,
    as_of: date,
    base_dir: str | Path = ".",
    book: RunConfig | None = None,
    data: ResearchData | None = None,
    write: bool = True,
) -> ShadowReport:
    base_dir = Path(base_dir)
    gate = ShadowGateConfig.from_yaml(base_dir / gate_path)
    cfg = ResearchRunConfig.from_yaml(base_dir / gate.research_config)
    book = book or RunConfig.from_yaml(base_dir / cfg.book_config)
    spec = QuantSpec.from_book(book)
    collected = collect(base_dir / AI_JOURNAL_DIR, gate)
    monthly: list[dict] = []
    if gate.status == "SIGNED_OFF" and collected.observations and spec is not None:
        first = min(o.decision_date for o in collected.observations)
        start = first - timedelta(days=PANEL_WARMUP_DAYS)
        data = data or load_research_data(book, as_of, base_dir, start=start)
        monthly = _score_months(
            collected.observations, data, spec, book.universe.market, gate, collected.ignored
        )
    result = evaluate([m["incremental_ic"] for m in monthly], gate)
    report = ShadowReport(as_of, gate.config_hash(), gate.status, result, monthly, collected, None)
    if not write:
        return report
    md = safe_write_path(base_dir, AI_REPORT_DIR / "shadow" / as_of.isoformat() / "report.md")
    md.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "as_of": as_of.isoformat(),
        "gate_hash": gate.config_hash(),
        "gate_status": gate.status,
        "verdict": result.verdict,
        "reason": result.reason,
        "months": result.months,
        "mean_ic": result.mean_ic,
        "ic_ir": result.ic_ir,
        "positive_frac": result.positive_frac,
        "max_single_share": result.max_single_share,
        "checks": result.checks,
        "monthly": monthly,
        "runs_used": collected.runs_used,
        "ignored": dict(collected.ignored),
        "n_observations": len(collected.observations),
    }
    safe_write_path(
        base_dir, AI_REPORT_DIR / "shadow" / as_of.isoformat() / "summary.json"
    ).write_text(json.dumps(summary, indent=2, sort_keys=True))
    md.write_text(render(summary, gate))
    return ShadowReport(
        as_of, gate.config_hash(), gate.status, result, monthly, collected, md.parent
    )


def render(summary: dict, gate: ShadowGateConfig) -> str:
    t = gate.thresholds
    lines = [
        f"# Forward AI shadow gate — as of {summary['as_of']}",
        "",
        ADVISORY_BANNER,
        "",
        f"**Verdict: {summary['verdict']}** — {summary['reason']}",
        "",
        f"- Gate status: {summary['gate_status']} (hash `{summary['gate_hash'][:12]}`); "
        f"signed off by {gate.signed_off_by or '—'} on {gate.signed_off_on or '—'}",
        f"- Bound model: `{gate.model_id or '—'}`; research config hash "
        f"`{(gate.research_config_hash or '—')[:12]}`; horizon {gate.horizon_days} trading days",
        f"- Pre-registered thresholds: ≥{t.min_months} months · mean IC > {t.min_mean_ic} · "
        f"IC IR ≥ {t.min_ic_ir} · ≥{t.min_positive_frac:.0%} positive months · "
        f"no month > {t.max_single_month_share:.0%} of total",
        f"- Scored months: {summary['months']}; observations collected: {summary['n_observations']}",
        "",
    ]
    if summary["monthly"]:
        lines += ["| Decision date | Names | Incremental IC |", "|---|---:|---:|"]
        lines += [
            f"| {m['decision_date']} | {m['n']} | {m['incremental_ic']:+.3f} |"
            for m in summary["monthly"]
        ]
        lines.append("")
    if summary["ignored"]:
        lines += ["**Excluded (fail-closed accrual rules):**", ""]
        lines += [f"- {k}: {v}" for k, v in sorted(summary["ignored"].items())]
        lines.append("")
    lines.append(
        "> A PASS makes the AI score eligible for HUMAN REVIEW only. Any change to fusion "
        "`ai_weight` needs a new ADR and operator approval (ADR-043)."
    )
    return "\n".join(lines) + "\n"
