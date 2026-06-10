"""LLM provider abstraction for the backtesting GenAI layer.

Resolves the Phase-0 §9 open question (Bedrock vs the existing Anthropic SDK) with
an ``LLMProvider`` interface so either backend works behind the same guardrails:

    * ``BedrockProvider``  — Amazon Bedrock (IAM-native, no API key) — the design default.
    * ``AnthropicProvider``— Anthropic SDK (matches the repo's existing StrategySelector).
    * ``StubProvider``     — deterministic, offline; used by tests.

Providers are request/response only — **no streaming, no polling**. Backtest-only;
no broker APIs. Bedrock/Anthropic clients are injectable so tests never touch a
network or AWS.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

# Design default — Amazon Bedrock Claude Haiku (IAM-native).
DEFAULT_BEDROCK_MODEL = "anthropic.claude-3-5-haiku-20241022-v1:0"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


class LLMProvider(Protocol):
    name: str

    def invoke(self, prompt: str, *, max_tokens: int = 1024, temperature: float = 0.0) -> str: ...


class StubProvider:
    """Deterministic offline provider for tests/dev. Records the last prompt."""

    name = "stub"

    def __init__(self, response: str | Callable[[str], str] = "Stub analysis.") -> None:
        self._response = response
        self.last_prompt: str | None = None
        self.calls = 0

    def invoke(self, prompt: str, *, max_tokens: int = 1024, temperature: float = 0.0) -> str:
        self.last_prompt = prompt
        self.calls += 1
        return self._response(prompt) if callable(self._response) else self._response


class BedrockProvider:
    """Amazon Bedrock runtime provider (request/response). Client is injectable."""

    name = "bedrock"

    def __init__(self, model_id: str = DEFAULT_BEDROCK_MODEL, *, runtime_client: Any = None,
                 region: str = "ap-south-1") -> None:
        self._model_id = model_id
        self._client = runtime_client
        self._region = region

    def _runtime(self) -> Any:
        if self._client is None:
            import boto3  # lazy — never imported in tests (StubProvider is used)

            # Bedrock has no getter in shared.aws.clients yet; add one before prod use.
            self._client = boto3.client("bedrock-runtime", region_name=self._region)  # noqa: TID251
        return self._client

    def invoke(self, prompt: str, *, max_tokens: int = 1024, temperature: float = 0.0) -> str:
        import json

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        })
        resp = self._runtime().invoke_model(modelId=self._model_id, body=body)
        payload = json.loads(resp["body"].read())
        return "".join(part.get("text", "") for part in payload.get("content", []))


class AnthropicProvider:
    """Anthropic SDK provider (matches the repo's existing StrategySelector pattern)."""

    name = "anthropic"

    def __init__(self, model: str = DEFAULT_ANTHROPIC_MODEL, *, api_key: str | None = None,
                 client: Any = None) -> None:
        self._model = model
        self._api_key = api_key
        self._client = client

    def _anthropic(self) -> Any:
        if self._client is None:
            import os

            import anthropic  # lazy

            self._client = anthropic.Anthropic(api_key=self._api_key or os.environ.get("ANTHROPIC_API_KEY", ""))
        return self._client

    def invoke(self, prompt: str, *, max_tokens: int = 1024, temperature: float = 0.0) -> str:
        msg = self._anthropic().messages.create(
            model=self._model, max_tokens=max_tokens, temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")


def get_provider(name: str = "bedrock", **kwargs: Any) -> LLMProvider:
    name = name.lower()
    if name == "stub":
        return StubProvider(**kwargs)
    if name == "anthropic":
        return AnthropicProvider(**kwargs)
    if name == "bedrock":
        return BedrockProvider(**kwargs)
    raise ValueError(f"Unknown provider {name!r}. Use bedrock | anthropic | stub.")
