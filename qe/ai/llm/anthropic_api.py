"""Claude via the first-party Anthropic API (ADR-043 P10, second backend).

For operators whose AWS account cannot call Claude on Bedrock: the same
``LLMClient`` protocol, the same Messages API and Opus 5.x rules
(``qe.ai.llm.messages``), the same spend flag, budget, probe and contamination
rules. Model IDs are first-party IDs (``claude-opus-5-5``, no ``anthropic.``
prefix).

Credentials are resolved BY THE SDK from its standard chain (``ANTHROPIC_API_KEY``,
``ANTHROPIC_AUTH_TOKEN``, or an ``ant auth login`` profile). qe.ai never reads the
environment or handles a key: it cannot log one, and the secret scanner would
refuse any prompt containing one. The SDK is imported lazily (requirements-ai.txt).
"""

from typing import Any

from qe.ai.llm.messages import MessagesAPILLM


class AnthropicLLM(MessagesAPILLM):
    name = "anthropic"

    def _make_client(self) -> Any:
        from anthropic import Anthropic  # optional dependency, lazy

        return Anthropic(timeout=self._timeout_s, max_retries=0)
