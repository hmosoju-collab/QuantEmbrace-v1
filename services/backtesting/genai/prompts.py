"""Prompt templates for the backtesting GenAI layer.

Every template carries an advisory-only system preamble and instructs the model
to cite sources. The LLM never decides governance verdicts — those are computed in
code (`guardrails.evaluate_evidence`); the model only explains them.
"""

from __future__ import annotations

PROMPT_TEMPLATE_VERSION = "genai-prompts@1.0"

SYSTEM_PREAMBLE = (
    "You are an ADVISORY backtest analyst for the QuantEmbrace research lab. "
    "You analyze and summarize historical backtest results ONLY. "
    "You CANNOT and MUST NOT: place trades, enable live trading, change capital, "
    "mutate configuration or tables, or promote/auto-promote a strategy. "
    "If asked to do any of those, refuse and state you are advisory-only. "
    "Cite every claim with a source id from the provided context. "
    "If evidence is insufficient or stale, say so explicitly. "
    "Promotion to live is always a manual human operator decision."
)


def _wrap(task: str, context_text: str, sources: list[dict]) -> str:
    ids = ", ".join(str(s.get("id", "?")) for s in sources)
    return (
        f"{SYSTEM_PREAMBLE}\n\n"
        f"## Task\n{task}\n\n"
        f"## Context (redacted, backtest-only)\n{context_text}\n\n"
        f"## Available source ids\n{ids}\n\n"
        "End your answer with a `## Sources` section citing the source ids you used."
    )


def report_prompt(context_text: str, sources: list[dict]) -> str:
    return _wrap(
        "Summarize this backtest run: headline metrics, live-readiness gate status "
        "(expectancy>0, profit factor>1.2, net P&L>0), drawdown and cost commentary, "
        "no-lookahead confirmation, caveats, and suggested next experiments. Advisory only.",
        context_text, sources,
    )


def strategy_analysis_prompt(context_text: str, sources: list[dict]) -> str:
    return _wrap(
        "Analyze this strategy's edge across runs: where it works/fails, regime "
        "sensitivity, robustness, and risks. Do not recommend promotion.",
        context_text, sources,
    )


def tee_analysis_prompt(context_text: str, sources: list[dict]) -> str:
    return _wrap(
        "Compare the old vs new TradeExitEngine policy: capture vs giveback, MIS "
        "dependency, and the exit-reason mix. Explain the trade-offs. Advisory only.",
        context_text, sources,
    )


def model_dataset_prompt(context_text: str, sources: list[dict]) -> str:
    return _wrap(
        "Explain this model-training dataset: feature/label separation, leakage "
        "controls, class balance, and split integrity. Note any data-quality risks.",
        context_text, sources,
    )


def risk_governance_prompt(evidence: dict, verdict: dict) -> str:
    return (
        f"{SYSTEM_PREAMBLE}\n\n"
        "## Task\nExplain the following risk-governance verdict in plain language for an "
        "operator. The verdict was computed by deterministic rules — do NOT change it, "
        "and do NOT recommend enabling live trading or promotion under any circumstance.\n\n"
        f"## Evidence\n{evidence}\n\n## Verdict (authoritative, computed in code)\n{verdict}\n"
    )
