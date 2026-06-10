"""GenAI analyst orchestrator for the backtesting lab (advisory only).

Wraps an ``LLMProvider`` behind the guardrails: redacts secrets, blocks forbidden
actions in both the request and the response, enforces citations on reports, and
computes risk-governance verdicts **in code** (the LLM only explains them).

This class has **no method** to place orders, enable live, mutate config/capital,
or promote a strategy — there is no action channel, only text analysis.
Backtest-only; no broker APIs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from backtesting.genai.bedrock_client import LLMProvider
from backtesting.genai.guardrails import (
    assert_cited,
    assert_no_forbidden_action,
    assert_no_secrets,
    ensure_sources_section,
    evaluate_evidence,
    redact_secrets,
)
from backtesting.genai.prompts import (
    PROMPT_TEMPLATE_VERSION,
    model_dataset_prompt,
    report_prompt,
    risk_governance_prompt,
    strategy_analysis_prompt,
    tee_analysis_prompt,
)


@dataclass
class AnalysisContext:
    run_id: str = ""
    summary_text: str = ""
    metrics: dict = field(default_factory=dict)
    sources: list[dict] = field(default_factory=list)
    extra_text: str = ""

    def as_context_text(self) -> str:
        parts = []
        if self.run_id:
            parts.append(f"run_id: {self.run_id}")
        if self.summary_text:
            parts.append(self.summary_text)
        if self.metrics:
            parts.append("metrics: " + json.dumps(self.metrics, default=str))
        if self.extra_text:
            parts.append(self.extra_text)
        return "\n".join(parts)


@dataclass
class AnalysisResult:
    kind: str
    text: str
    sources: list[dict]
    model: str
    template_version: str = PROMPT_TEMPLATE_VERSION


class GenAIAnalyst:
    """Advisory analysis over backtest outputs. No action channel."""

    def __init__(self, provider: LLMProvider, *, max_tokens: int = 1024) -> None:
        self._p = provider
        self._max_tokens = max_tokens

    # ── guarded LLM round-trip ──────────────────────────────────────────────────
    def _guarded_invoke(self, prompt: str) -> str:
        # NOTE: the prompt embeds a trusted system preamble that legitimately *names*
        # forbidden actions (to prohibit them), so we do not scan the assembled prompt
        # for forbidden actions — only untrusted context (in _report_like) and the
        # model response are scanned.
        assert_no_secrets(prompt)            # never send secrets to the model
        response = self._p.invoke(prompt, max_tokens=self._max_tokens)
        response = redact_secrets(response)  # defensive: scrub any echoed secret
        assert_no_forbidden_action(response)  # model must not emit a forbidden directive
        return response

    # ── report-style analyses (require cited sources) ──────────────────────────
    def _report_like(self, kind: str, prompt_fn, ctx: AnalysisContext) -> AnalysisResult:
        if not ctx.sources:
            from backtesting.genai.guardrails import CitationError

            raise CitationError(f"{kind} requires at least one cited source.")
        context_text = redact_secrets(ctx.as_context_text())
        assert_no_forbidden_action(context_text)  # untrusted context must not request a forbidden action
        body = self._guarded_invoke(prompt_fn(context_text, ctx.sources))
        text = ensure_sources_section(body, ctx.sources)
        assert_cited(text, ctx.sources)
        return AnalysisResult(kind, text, ctx.sources, self._p.name)

    def generate_report(self, ctx: AnalysisContext) -> AnalysisResult:
        return self._report_like("report", report_prompt, ctx)

    def analyze_strategy(self, ctx: AnalysisContext) -> AnalysisResult:
        return self._report_like("strategy_analysis", strategy_analysis_prompt, ctx)

    def analyze_tee(self, ctx: AnalysisContext) -> AnalysisResult:
        return self._report_like("tee_analysis", tee_analysis_prompt, ctx)

    def explain_dataset(self, ctx: AnalysisContext) -> AnalysisResult:
        return self._report_like("model_dataset", model_dataset_prompt, ctx)

    # ── risk governance (verdict computed in code) ──────────────────────────────
    def risk_governance(self, evidence: dict) -> dict:
        """Return an advisory governance verdict. NEVER authorises promotion.

        The verdict is computed deterministically; the LLM is used only to produce
        a plain-language explanation (and is fully optional/safe to fail)."""
        verdict = evaluate_evidence(evidence)
        explanation: str | None = None
        try:
            explanation = self._guarded_invoke(risk_governance_prompt(evidence, verdict))
        except Exception:
            explanation = None  # explanation is advisory; the verdict stands regardless
        return {**verdict, "explanation": explanation, "model": getattr(self._p, "name", None)}
