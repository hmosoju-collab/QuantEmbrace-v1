"""The research view: fuse a research run and render AI vs QuantEmbrace.

Recomputes the deterministic quant view on the SAME data the research run saw
(the snapshot id must match — a lake that changed since is refused), fuses per
``configs/research_fusion.yaml``, and optionally annotates with the engine
journal's actual RISK / REBALANCE record for that date (read-only). Output:
``reports/qe-ai/<research_run_id>/fusion-<mode>-<context>.{jsonl,md}``.
"""

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
from typing import Any

from qe.ai.config import ResearchRunConfig
from qe.ai.fusion.config import FusionConfig, FusionContext
from qe.ai.fusion.engine import FusionRefused, FusionReport, fuse
from qe.ai.fusion.quant import quant_rows
from qe.ai.paths import AI_REPORT_DIR, safe_write_path
from qe.ai.reporting import ADVISORY_BANNER, load_research_journal
from qe.ai.tools import (
    QuantSpec,
    ResearchData,
    ResearchDataAPI,
    knowledge_ts,
    load_research_data,
    regime,
)
from qe.config import RunConfig
from qe.journal import read_header, read_journal


@dataclass(frozen=True)
class FusionRun:
    report: FusionReport
    engine: dict[str, Any] | None
    out_dir: Path


def engine_record(path: str | Path, decision_date: date) -> dict[str, Any]:
    """What the qe engine actually did on ``decision_date`` (read-only)."""
    header = read_header(path)
    if header.get("mode") == "ai-research":
        raise FusionRefused(f"{path} is a research journal, not an engine journal")
    out: dict[str, Any] = {
        "session_id": header.get("session_id"),
        "mode": header.get("mode"),
        "config_hash": str(header.get("config_hash", ""))[:12],
        "risk": None,
        "rebalance": None,
        "skipped": None,
        "kill_blocked": None,
    }
    day = decision_date.isoformat()
    for rec in read_journal(path):
        d = rec["data"]
        if d.get("date") != day:
            continue
        if rec["type"] == "RISK":
            out["risk"] = {
                "approved": d["approved"],
                "rejections": [c["check"] for c in d["checks"] if not c["ok"]],
            }
        elif rec["type"] == "REBALANCE":
            out["rebalance"] = {
                "buys": sorted(o["symbol"] for o in d["orders"] if o["side"] == "BUY"),
                "sells": sorted(o["symbol"] for o in d["orders"] if o["side"] == "SELL"),
                "nav": d["nav"],
            }
        elif rec["type"] == "REBALANCE_SKIPPED":
            out["skipped"] = d.get("reason")
        elif rec["type"] == "KILL_BLOCKED":
            out["kill_blocked"] = d.get("reason")
    return out


def run_fusion(
    research_journal: str | Path,
    cfg: FusionConfig,
    *,
    context: FusionContext = "shadow",
    base_dir: str | Path = ".",
    engine_journal: str | Path | None = None,
    book: RunConfig | None = None,
    data: ResearchData | None = None,
) -> FusionRun:
    base_dir = Path(base_dir)
    view = load_research_journal(research_journal)
    ai_cfg = ResearchRunConfig.model_validate(view.header["config"])
    book = book or RunConfig.from_yaml(base_dir / ai_cfg.book_config)
    as_of = date.fromisoformat(view.manifest["as_of"])
    if data is None:
        data = load_research_data(book, as_of, base_dir)
    if data.snapshot_id != view.header.get("data_snapshot_id"):
        raise FusionRefused(
            f"lake changed since the research run: snapshot {data.snapshot_id} != "
            f"{view.header.get('data_snapshot_id')} — re-run research first"
        )
    market = book.universe.market
    api = ResearchDataAPI.at(data.panel, knowledge_ts(as_of, market), market, data.index_bars)
    spec = QuantSpec.from_book(book)
    if spec is None:
        raise FusionRefused("the annotated book has no cross-sectional factor to fuse with")
    rows = quant_rows(api, spec, cfg.hard_risk, extra_symbols=[s.symbol for s in view.signals])
    report = fuse(
        rows,
        {s.symbol: s for s in view.signals},
        cfg,
        context=context,
        k=spec.k,
        decision_date=api.decision_date,
        information_cutoff=api.cutoff,
        regime=regime(api)[1],
        research_id=view.header.get("session_id"),
    )
    engine = engine_record(base_dir / engine_journal, api.decision_date) if engine_journal else None

    stem = f"fusion-{cfg.mode.lower()}-{context}"
    run_id = view.header["session_id"]
    md = safe_write_path(base_dir, AI_REPORT_DIR / run_id / f"{stem}.md")
    md.parent.mkdir(parents=True, exist_ok=True)
    jl = safe_write_path(base_dir, AI_REPORT_DIR / run_id / f"{stem}.jsonl")
    jl.write_text(
        "".join(json.dumps(r.model_dump(mode="json"), sort_keys=True) + "\n" for r in report.rows)
    )
    md.write_text(render_fusion(report, cfg, engine))
    return FusionRun(report, engine, md.parent)


def _f(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}" if signed else f"{v:.2f}"


def render_fusion(report: FusionReport, cfg: FusionConfig, engine: dict[str, Any] | None) -> str:
    rows = report.rows
    regime = rows[0] if rows else None
    lines = [
        f"# Research view — {report.decision_date} ({report.mode}, {report.context})",
        "",
        ADVISORY_BANNER,
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Fusion mode / AI weight | {cfg.mode} / {cfg.ai_weight} (ceiling enforced in code) |",
        f"| Fusion config hash | `{report.fusion_config_hash[:12]}` |",
        f"| Research run | `{report.research_id}` |",
        f"| Information cutoff | {report.information_cutoff.isoformat()} |",
        f"| Market regime | {regime.market_regime if regime else '—'} "
        f"(R {_f(regime.regime_value) if regime else '—'}) |",
    ]
    if engine:
        risk = engine.get("risk") or {}
        reb = engine.get("rebalance")
        acted = (
            f"rebalanced — buys {len(reb['buys'])}, sells {len(reb['sells'])}, NAV {reb['nav']}"
            if reb
            else f"not executed ({engine.get('skipped') or engine.get('kill_blocked') or 'no rebalance due'})"
        )
        lines.append(
            f"| Engine (`{engine['session_id']}`) | risk "
            f"{'APPROVED' if risk.get('approved') else 'REJECTED' if risk else '—'}; {acted} |"
        )
    if report.ignored_signals:
        lines.append(f"| Ignored signals (other cutoff) | {', '.join(report.ignored_signals)} |")
    lines += [
        "",
        "## AI recommendation vs QuantEmbrace decision",
        "",
        "| Symbol | Quant score | Strategy signal | AI score | AI conf | AI recommendation | "
        "Risk flags | **QuantEmbrace decision** | AI agrees | Contaminated |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    shown = [
        r
        for r in rows
        if r.ai_score is not None or r.quant_decision == "SELECT" or r.final_decision == "SELECT"
    ]
    for r in sorted(shown, key=lambda r: (r.q is None, -(r.q or 0.0), r.symbol)):
        agree = "—" if r.ai_agrees is None else ("yes" if r.ai_agrees else "**no**")
        lines.append(
            f"| {r.symbol} | {_f(r.q)} | {r.quant_decision} | {_f(r.ai_score)} | "
            f"{_f(r.ai_confidence, False)} | {r.ai_recommendation} | "
            f"{', '.join(r.hard_flags) or '—'} | **{r.final_decision}** | {agree} | "
            f"{'—' if r.contaminated is None else 'yes' if r.contaminated else 'no'} |"
        )
    div = report.divergences
    lines += [
        "",
        f"**Divergences from the engine's own decision:** {', '.join(div) if div else 'none'}"
        + (
            " (expected none in AI_DISABLED / AI_ADVISORY)"
            if cfg.mode in ("AI_DISABLED", "AI_ADVISORY")
            else ""
        ),
        "",
        "## Bull / bear / consensus / conflicting evidence",
        "",
    ]
    for r in shown:
        if not (r.bull_case or r.bear_case or r.consensus or r.conflicting_evidence):
            continue
        lines.append(f"### {r.symbol}")
        for label, text in (
            ("Bull", r.bull_case),
            ("Bear", r.bear_case),
            ("Consensus", r.consensus),
        ):
            if text:
                lines.append(f"- **{label}:** {text}")
        if r.conflicting_evidence:
            lines.append(f"- **Conflicting evidence:** {', '.join(r.conflicting_evidence)}")
        lines.append("")
    return "\n".join(lines) + "\n"
