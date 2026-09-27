"""LLM access for qe.ai: protocol, fake + Bedrock + first-party backends, gateway, controls."""

from qe.ai.config import ResearchRunConfig
from qe.ai.llm.anthropic_api import AnthropicLLM
from qe.ai.llm.base import LLMClient, LLMError, LLMRequest, LLMResponse, LLMTimeout
from qe.ai.llm.bedrock import BedrockLLM
from qe.ai.llm.budget import BudgetExhausted, CircuitBreaker, RunBudget
from qe.ai.llm.cache import ResponseCache, cache_key
from qe.ai.llm.fake import FakeLLM
from qe.ai.llm.gateway import CallResult, LLMGateway


class SpendNotAllowed(RuntimeError):
    pass


def build_client(
    cfg: ResearchRunConfig, *, allow_spend: bool, runtime_client: object | None = None
) -> LLMClient:
    """The fake backend is free; any real backend requires the operator's
    explicit ``--allow-llm-spend`` (zero-spend default, ADR-043 §7)."""
    if cfg.backend == "fake":
        return FakeLLM(seed=cfg.seed)
    if not allow_spend:
        raise SpendNotAllowed(
            f"backend={cfg.backend} makes paid LLM calls; re-run with --allow-llm-spend"
        )
    if cfg.backend == "anthropic":
        return AnthropicLLM(timeout_s=cfg.budget.timeout_s, runtime_client=runtime_client)
    return BedrockLLM(
        region=cfg.region, timeout_s=cfg.budget.timeout_s, runtime_client=runtime_client
    )


__all__ = [
    "AnthropicLLM",
    "BedrockLLM",
    "BudgetExhausted",
    "CallResult",
    "CircuitBreaker",
    "FakeLLM",
    "LLMClient",
    "LLMError",
    "LLMGateway",
    "LLMRequest",
    "LLMResponse",
    "LLMTimeout",
    "ResponseCache",
    "RunBudget",
    "SpendNotAllowed",
    "build_client",
    "cache_key",
]
