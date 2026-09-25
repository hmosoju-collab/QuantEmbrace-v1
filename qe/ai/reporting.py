"""Research reports — derived views rebuilt from a research journal.

The journal is the source of truth; ``reports/qe-ai/<run_id>/`` is a
disposable projection (signals.jsonl + summary.md). Signals are re-validated
on load, so a tampered or schema-drifted record fails loudly.
"""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from qe.ai.models import ComponentStatus, ResearchSignal
from qe.ai.paths import AI_REPORT_DIR, safe_write_path
from qe.journal import read_journal

ADVISORY_BANNER = (
    "> **Advisory research only (ADR-043).** Not a trading signal. AI weight in fusion is 0 by "
    "default; the qe engine and its risk checks are unaffected by anything below."
)


@dataclass(frozen=True)
class ResearchJournalView:
    header: dict[str, Any]
    manifest: dict[str, Any]
    signals: list[ResearchSignal]
    failed: list[dict[str, Any]]
    end: dict[str, Any]


def load_research_journal(path: str | Path) -> ResearchJournalView:
    header: dict = {}
    manifest: dict = {}
    signals: list[ResearchSignal] = []
    failed: list[dict] = []
    end: dict = {}
    for rec in read_journal(path):
        kind, data = rec["type"], rec["data"]
        if kind == "SESSION_START":
            if data.get("mode") != "ai-research":
                raise ValueError(
                    f"{path} is not a qe.ai research journal (mode={data.get('mode')})"
                )
            header = data
        elif kind == "RUN_MANIFEST":
            manifest = data
        elif kind == "RESEARCH_SIGNAL":
            signals.append(ResearchSignal.model_validate(data))
        elif kind == "SYMBOL_FAILED":
            failed.append(data)
        elif kind in ("SESSION_END", "SESSION_ABORT"):
            end = {"type": kind, **data}
    return ResearchJournalView(header, manifest, signals, failed, end)


def _fmt(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}" if signed else f"{v:.2f}"


def render_summary(view: ResearchJournalView) -> str:
    h, m, sigs = view.header, view.manifest, view.signals
    n_cont = sum(s.contamination_risk for s in sigs)
    lines = [
        f"# AI research — {h.get('session_id', '?')}",
        "",
        ADVISORY_BANNER,
        "",
        "| Field | Value |",
        "|---|---|",
        f"| As-of / decision date | {m.get('as_of')} / {m.get('decision_date')} |",
        f"| Information cutoff | {m.get('information_cutoff')} |",
        f"| Research mode / backend | {m.get('research_mode')} / {m.get('backend')} |",
        f"| Book annotated | `{m.get('book_config')}` (hash `{str(m.get('book_config_hash', ''))[:12]}`) |",
        f"| Research config hash | `{str(h.get('config_hash', ''))[:12]}` |",
        f"| Data snapshot / code | `{h.get('data_snapshot_id')}` / `{h.get('code_sha')}` |",
        f"| Model knowledge cutoffs | {m.get('knowledge_cutoffs_used')} |",
        "",
        f"**Contamination:** {n_cont}/{len(sigs)} signals are flagged (decision date within the "
        "models' knowledge cutoff + guard, or cutoff unknown). Flagged signals can never carry "
        "weight — docs/research/lookahead-prevention.md §2.",
        "",
    ]
    if sigs:
        s0 = sigs[0]
        lines += [
            f"## Market regime: {s0.market_regime} (confidence {_fmt(s0.regime_confidence, False)})",
            "",
        ]
        lines += [
            "## Signals",
            "",
            "| Symbol | AI score | AI conf | Technical | Risk score | Non-OK components | Contaminated |",
            "|---|---|---|---|---|---|---|",
        ]
        for s in sigs:
            bad = ", ".join(
                f"{k}={v}" for k, v in sorted(s.component_status.items())
                if v not in (ComponentStatus.OK, ComponentStatus.SKIPPED)
            )  # fmt: skip
            lines.append(
                f"| {s.symbol} | {_fmt(s.ai_score)} | {_fmt(s.ai_confidence, False)} | "
                f"{_fmt(s.technical_score)} | {_fmt(s.risk_score, False)} | {bad or '—'} | "
                f"{'yes' if s.contamination_risk else 'no'} |"
            )
        lines.append("")
        for s in sigs:
            if not (s.bull_case or s.bear_case or s.consensus or s.risks):
                continue
            lines += [f"### {s.symbol}", ""]
            for label, text in (
                ("Bull", s.bull_case),
                ("Bear", s.bear_case),
                ("Consensus", s.consensus),
            ):
                if text:
                    lines.append(f"- **{label}:** {text}")
            if s.risks:
                lines.append(f"- **Risks:** {'; '.join(s.risks)}")
            cited = [e.evidence_id for e in (*s.supporting_evidence, *s.contradicting_evidence)]
            if cited:
                lines.append(f"- **Evidence cited:** {', '.join(cited)}")
            lines.append("")
    end = view.end
    lines += [
        "## Run",
        "",
        f"- Status: {end.get('type', 'INCOMPLETE')} {end.get('status', '')}".rstrip(),
        f"- LLM calls: {end.get('llm_calls', 0)} (cache hits {end.get('cache_hits', 0)}, "
        f"failures {end.get('llm_failures', 0)}), tokens in/out "
        f"{end.get('input_tokens', 0)}/{end.get('output_tokens', 0)}",
    ]
    if view.failed:
        lines.append(f"- Failed symbols: {', '.join(f['symbol'] for f in view.failed)}")
    return "\n".join(lines) + "\n"


def write_research_report(journal_path: str | Path, base_dir: str | Path = ".") -> Path:
    view = load_research_journal(journal_path)
    run_id = view.header["session_id"]
    summary = safe_write_path(base_dir, AI_REPORT_DIR / run_id / "summary.md")
    summary.parent.mkdir(parents=True, exist_ok=True)
    signals_path = safe_write_path(base_dir, AI_REPORT_DIR / run_id / "signals.jsonl")
    signals_path.write_text(
        "".join(json.dumps(s.model_dump(mode="json"), sort_keys=True) + "\n" for s in view.signals)
    )
    summary.write_text(render_summary(view))
    return summary.parent
