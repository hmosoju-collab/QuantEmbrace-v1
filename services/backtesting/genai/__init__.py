"""Serverless GenAI analysis layer for the QuantEmbrace backtesting lab.

Advisory only. The layer may observe, analyze, summarize, and recommend over
backtest outputs; it MUST NOT place trades, enable live trading, change capital,
mutate config/tables, or promote strategies. Guardrails enforce this.
"""

from __future__ import annotations

from backtesting.genai.analyst import AnalysisContext, AnalysisResult, GenAIAnalyst
from backtesting.genai.bedrock_client import (
    AnthropicProvider,
    BedrockProvider,
    StubProvider,
    get_provider,
)
from backtesting.genai.guardrails import (
    CitationError,
    ForbiddenActionError,
    SecretLeakError,
    assert_no_forbidden_action,
    assert_no_secrets,
    evaluate_evidence,
    redact_secrets,
    scan_for_secrets,
)

__all__ = [
    "GenAIAnalyst", "AnalysisContext", "AnalysisResult",
    "StubProvider", "BedrockProvider", "AnthropicProvider", "get_provider",
    "scan_for_secrets", "redact_secrets", "assert_no_secrets",
    "assert_no_forbidden_action", "evaluate_evidence",
    "SecretLeakError", "ForbiddenActionError", "CitationError",
]
