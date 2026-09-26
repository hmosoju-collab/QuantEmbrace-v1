"""News evidence from the curated announcement corpus (ADR-043 P9).

Only documents public at the decision cutoff (knowledge_ts <= cutoff) inside a 30
day window are used. Each document becomes one Evidence stamped at its OWN
publication time; only the sanitised HEADLINE and category are exposed to the
model (bodies are kept for humans). No documents in the window ⇒ UNAVAILABLE, so
the news agent makes zero LLM calls rather than reasoning about nothing.
"""

from qe.ai.models import ComponentStatus, Evidence
from qe.ai.tools.pit import ResearchDataAPI, ToolResult, evidence, unavailable

TOOL = "news"
LOOKBACK_DAYS = 30
MAX_HEADLINES = 5


def news(api: ResearchDataAPI, symbol: str) -> ToolResult:
    docs = api.documents_for(symbol, LOOKBACK_DAYS)
    if not docs:
        why = "no announcements in the window" if api.corpus.loaded else "no curated corpus loaded"
        return unavailable(TOOL, symbol, f"{why} ({LOOKBACK_DAYS}d, P9 corpus)")
    facts = [
        evidence(api, TOOL, "count_30d", len(docs), "announcements in the last 30 days", symbol),
        evidence(
            api,
            TOOL,
            "results_30d",
            sum(d.category == "RESULTS" for d in docs),
            "results announcements, last 30 days",
            symbol,
        ),
    ]
    for d in sorted(docs, key=lambda d: d.knowledge_ts, reverse=True)[:MAX_HEADLINES]:
        facts.append(
            Evidence(
                evidence_id=f"news.{d.doc_id[4:12]}",
                tool=TOOL,
                symbol=symbol,
                knowledge_ts=d.knowledge_ts,
                value=d.category,
                summary=f"{d.category}: {d.headline}"[:240],
            )
        )
    return ToolResult(TOOL, symbol, ComponentStatus.OK, tuple(facts))
