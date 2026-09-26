"""LLM client protocol for qe.ai — request/response only.

No streaming, no tool use, no system-level actions: an LLM here can only return
text, which agents parse into a bounded pydantic schema. Pattern ported (not
imported) from ``services/backtesting/genai/bedrock_client.py``.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LLMRequest:
    model_id: str
    system: str
    prompt: str
    max_tokens: int
    temperature: float = 0.0  # ignored by backends whose models removed sampling params
    effort: str | None = None  # thinking/effort level for models that require one


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    stop_reason: str
    cached: bool = False


class LLMError(RuntimeError):
    """A provider call failed (message is sanitized: exception type only)."""


class LLMTimeout(LLMError):
    pass


class LLMClient(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...
