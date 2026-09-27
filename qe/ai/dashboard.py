"""Research dashboard (ADR-043 P8) — one static, self-contained HTML page.

Built only from files the other qe.ai commands already wrote (research
journal, fusion view, shadow-gate summary, post-trade reviews, hypothesis
drafts) plus the lifecycle ledger, read as plain JSON. It shows the AI
recommendation NEXT TO QuantEmbrace's final decision and never computes a
decision itself.

Safety: much of the text on this page is LLM output, i.e. untrusted. Every
value is HTML-escaped, and a Content-Security-Policy (``default-src 'none'``)
forbids scripts and any network load — the page has no JavaScript at all.
"""

from datetime import UTC, datetime
from html import escape
import json
from pathlib import Path
from typing import Any

from qe.ai.paths import AI_JOURNAL_DIR, AI_REPORT_DIR, safe_write_path
from qe.ai.reporting import load_research_journal
from qe.journal import JournalError

CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--good:#1f7a4d;
--bad:#b3261e;--warn:#8a5a00;--accent:#2f5bd3}
@media (prefers-color-scheme:dark){:root{--bg:#131312;--card:#1c1c1a;--fg:#ecebe6;--muted:#9d9c95;
--line:#34332f;--good:#5fc48e;--bad:#f08a83;--warn:#e5b45c;--accent:#8aa8ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:28px 0 10px}.muted{color:var(--muted)}.banner{border-left:3px solid var(--warn);
padding:8px 12px;background:var(--card);margin:12px 0 20px}.grid{display:grid;
grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.card{background:var(--card);
border:1px solid var(--line);border-radius:8px;padding:12px 14px}.card b{display:block;font-size:20px}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:7px 10px;border-bottom:1px solid var(--line);
text-align:left;white-space:nowrap}th{color:var(--muted);font-weight:600}td.wrap{white-space:normal;min-width:240px}
.num{text-align:right;font-variant-numeric:tabular-nums}.pos{color:var(--good)}.neg{color:var(--bad)}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;border:1px solid var(--line);font-size:12px}
.decision{font-weight:700;color:var(--accent)}details{background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:8px 12px;margin:6px 0}summary{cursor:pointer;font-weight:600}
"""


def _e(v: Any) -> str:
    return escape("" if v is None else str(v), quote=True)


def _num(v: float | None, pct: bool = False, signed: bool = True) -> str:
    if v is None:
        return '<td class="num muted">—</td>'
    txt = f"{v:+.1%}" if pct else (f"{v:+.2f}" if signed else f"{v:.2f}")
    cls = "pos" if v > 0 else "neg" if v < 0 else ""
    return f'<td class="num {cls if signed else ""}">{_e(txt)}</td>'


def _latest(paths: list[Path]) -> Path | None:
    return sorted(paths)[-1] if paths else None


def _jsonl(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def collect(base_dir: Path) -> dict[str, Any]:
    reports = base_dir / AI_REPORT_DIR
    research = None
    for path in sorted((base_dir / AI_JOURNAL_DIR).glob("ai-*.jsonl"), reverse=True):
        try:
            research = load_research_journal(path)
            break
        except (ValueError, JournalError, KeyError):
            continue
    fusion_rows: list[dict] = []
    if research is not None:
        rid = research.header["session_id"]
        fusion_rows = _jsonl(
            (reports / rid / "fusion-ai_advisory-shadow.jsonl")
            if (reports / rid / "fusion-ai_advisory-shadow.jsonl").exists()
            else _latest(list((reports / rid).glob("fusion-*.jsonl")))
        )
    shadow_path = _latest(list((reports / "shadow").glob("*/summary.json")))
    lifecycle: dict[str, dict] = {}
    for rec in _jsonl(base_dir / "governance" / "strategy-lifecycle.jsonl"):
        lifecycle[rec["strategy_id"]] = rec
    return {
        "research": research,
        "fusion": fusion_rows,
        "shadow": json.loads(shadow_path.read_text()) if shadow_path else None,
        "post_trade": _jsonl(_latest(list((reports / "post_trade").glob("*/reviews.jsonl")))),
        "hypotheses": _jsonl(_latest(list((reports / "hypotheses").glob("*/drafts.jsonl")))),
        "lifecycle": lifecycle,
    }


def render(data: dict[str, Any], generated: datetime) -> str:
    r = data["research"]
    sig = {s.symbol: s for s in r.signals} if r else {}
    parts = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">',
        '<meta name=viewport content="width=device-width,initial-scale=1">',
        "<title>QuantEmbrace Research View</title>",
        f"<style>{CSS}</style></head><body><main>",
        "<h1>QuantEmbrace research view</h1>",
        f"<div class=muted>Generated {_e(generated.strftime('%Y-%m-%d %H:%M UTC'))} from local "
        "qe.ai outputs</div>",
        "<div class=banner><b>Advisory research only (ADR-043).</b> The AI recommendation is "
        "shown next to QuantEmbrace's decision and never replaces it. Fusion AI weight is 0 by "
        "default; no trading path reads this page.</div>",
    ]
    if r is None:
        parts.append(
            "<p class=muted>No research run yet — run <code>python -m qe.ai research</code>.</p>"
        )
    else:
        m = r.manifest
        regime = r.signals[0] if r.signals else None
        n_cont = sum(s.contamination_risk for s in r.signals)
        parts += [
            "<div class=grid>",
            f"<div class=card><span class=muted>Market regime</span><b>{_e(regime.market_regime if regime else '—')}</b>"
            f"<span class=muted>confidence {_e(f'{regime.regime_confidence:.2f}' if regime and regime.regime_confidence is not None else '—')}</span></div>",
            f"<div class=card><span class=muted>Decision date</span><b>{_e(m.get('decision_date'))}</b>"
            f"<span class=muted>cutoff {_e(m.get('information_cutoff'))}</span></div>",
            f"<div class=card><span class=muted>Research run</span><b>{_e(m.get('research_mode'))} · {_e(m.get('backend'))}</b>"
            f"<span class=muted>{len(r.signals)} signals · {n_cont} contaminated</span></div>",
            "</div>",
            "<h2>AI recommendation vs QuantEmbrace decision</h2>",
            "<div class=scroll><table><thead><tr><th>Symbol</th><th class=num>Quant score</th>"
            "<th>Strategy signal</th><th class=num>AI research score</th><th class=num>AI conf</th>"
            "<th class=num>Risk score</th><th>Risk flags</th><th>AI recommendation</th>"
            "<th>QuantEmbrace final decision</th><th>AI agrees</th><th>Contaminated</th></tr></thead><tbody>",
        ]
        rows = data["fusion"] or [
            {"symbol": s.symbol, "q": None, "quant_decision": "—", "ai_score": s.ai_score,
             "ai_confidence": s.ai_confidence, "hard_flags": [], "ai_recommendation": "—",
             "final_decision": "run qe.ai fuse", "ai_agrees": None,
             "contaminated": s.contamination_risk}
            for s in r.signals
        ]  # fmt: skip
        shown = [
            row
            for row in rows
            if row.get("ai_score") is not None or row.get("final_decision") == "SELECT"
        ]
        for row in sorted(shown, key=lambda x: -(x.get("q") or -9)):
            s = sig.get(row["symbol"])
            agree = row.get("ai_agrees")
            parts.append(
                f"<tr><td>{_e(row['symbol'])}</td>{_num(row.get('q'))}<td>{_e(row.get('quant_decision'))}</td>"
                f"{_num(row.get('ai_score'))}{_num(row.get('ai_confidence'), signed=False)}"
                f"{_num(s.risk_score if s else None, signed=False)}"
                f"<td>{_e(', '.join(row.get('hard_flags') or []) or '—')}</td>"
                f"<td><span class=tag>{_e(row.get('ai_recommendation'))}</span></td>"
                f"<td class=decision>{_e(row.get('final_decision'))}</td>"
                f"<td>{_e('—' if agree is None else 'yes' if agree else 'no')}</td>"
                f"<td>{_e('—' if row.get('contaminated') is None else 'yes' if row.get('contaminated') else 'no')}</td></tr>"
            )
        parts.append(
            "</tbody></table></div><h2>Bull / bear / consensus / conflicting evidence</h2>"
        )
        for s in r.signals:
            if not (s.bull_case or s.bear_case or s.consensus or s.contradicting_evidence):
                continue
            conflicts = ", ".join(e.evidence_id for e in s.contradicting_evidence) or "—"
            parts.append(
                f"<details><summary>{_e(s.symbol)}</summary>"
                f"<p><b>Bull:</b> {_e(s.bull_case)}</p><p><b>Bear:</b> {_e(s.bear_case)}</p>"
                f"<p><b>Consensus:</b> {_e(s.consensus)}</p><p><b>Conflicting evidence:</b> {_e(conflicts)}</p>"
                f"<p class=muted>Risks: {_e('; '.join(s.risks) or '—')}</p></details>"
            )
    sh = data["shadow"]
    parts.append("<h2>Forward AI shadow gate</h2>")
    if sh is None:
        parts.append("<p class=muted>Not evaluated yet — <code>python -m qe.ai shadow</code>.</p>")
    else:
        parts.append(
            f"<div class=grid><div class=card><span class=muted>Verdict</span><b>{_e(sh['verdict'])}</b>"
            f"<span class=muted>{_e(sh['reason'])}</span></div><div class=card><span class=muted>Gate</span>"
            f"<b>{_e(sh['gate_status'])}</b><span class=muted>{_e(sh['months'])} scored months</span></div></div>"
        )
    pt = data["post_trade"]
    parts.append("<h2>Post-trade reviews</h2>")
    if not pt:
        parts.append("<p class=muted>None yet — <code>python -m qe.ai post-trade</code>.</p>")
    else:
        parts.append(
            "<div class=scroll><table><thead><tr><th>Symbol</th><th>Held</th><th class=num>Net</th>"
            "<th class=num>Excess</th><th>Factor thesis</th><th>Entry</th><th>Exit</th><th>Regime call</th>"
            "<th>Lesson (model narrative)</th></tr></thead><tbody>"
        )
        for x in pt[-20:]:
            parts.append(
                f"<tr><td>{_e(x['symbol'])}</td><td>{_e(x['open_date'])} → {_e(x['close_date'])}</td>"
                f"{_num(x['net_return'], pct=True)}{_num(x.get('excess_return'), pct=True)}"
                f"<td>{_e(x['quant_thesis'])}</td><td>{_e(x['entry_quality'])}</td><td>{_e(x['exit_quality'])}</td>"
                f"<td>{_e(x['regime_accuracy'])}</td><td class=wrap>{_e(x.get('lesson') or '—')}</td></tr>"
            )
        parts.append("</tbody></table></div>")
    parts.append("<h2>Hypothesis drafts (CANDIDATE, for human review)</h2>")
    if not data["hypotheses"]:
        parts.append("<p class=muted>None yet — <code>python -m qe.ai hypothesize</code>.</p>")
    for h in data["hypotheses"]:
        flags = []
        if h.get("eliminated_family"):
            flags.append(f"re-proposes settled family {h['eliminated_family']}")
        if not h.get("testable_now"):
            flags.append("needs " + ", ".join(h.get("missing_data") or []))
        parts.append(
            f"<details><summary>{_e(h['name'])} <span class=tag>{_e(h['family'])}</span></summary>"
            f"<p>{_e(h['hypothesis'])}</p><p class=muted>{_e('; '.join(flags) or 'testable now')} · "
            f"family budget {_e(h.get('family_experiment_count'))}</p></details>"
        )
    parts.append("<h2>Strategy lifecycle (human-approved)</h2>")
    if not data["lifecycle"]:
        parts.append(
            "<p class=muted>No strategies registered — <code>python -m qe lifecycle</code>.</p>"
        )
    else:
        parts.append("<div class=scroll><table><thead><tr><th>Strategy</th><th>State</th><th>Approved by</th>"
                     "<th>Recorded</th></tr></thead><tbody>")  # fmt: skip
        for sid, rec in sorted(data["lifecycle"].items()):
            parts.append(
                f"<tr><td>{_e(sid)}</td><td class=decision>{_e(rec['to_state'])}</td>"
                f"<td>{_e(rec['approved_by'])}</td><td>{_e(rec['recorded_utc'])}</td></tr>"
            )
        parts.append("</tbody></table></div>")
    parts.append("</main></body></html>")
    return "\n".join(parts)


def build_dashboard(base_dir: str | Path = ".") -> Path:
    base_dir = Path(base_dir)
    out = safe_write_path(base_dir, AI_REPORT_DIR / "dashboard" / "index.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(collect(base_dir), datetime.now(UTC)))
    return out
