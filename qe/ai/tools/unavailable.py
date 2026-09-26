"""Fundamentals / sentiment — no source exists (news moved to the P9 corpus, tools/documents.py).

These tools always report UNAVAILABLE, and the matching agents then make zero
LLM calls, so a model is never asked to "analyze" a company's fundamentals or
news from its own memory (hallucination + contamination). A real source must
first pass the data-lake quarantine/trust-tier process — [PLANNED, P9].
"""

from qe.ai.tools.pit import ToolResult, unavailable

_REASON = "no {kind} source in the lake (prices only); see ADR-043 / P9"


def fundamentals(symbol: str) -> ToolResult:
    return unavailable("fundamentals", symbol, _REASON.format(kind="fundamentals"))


def sentiment(symbol: str) -> ToolResult:
    return unavailable("sentiment", symbol, _REASON.format(kind="sentiment"))
