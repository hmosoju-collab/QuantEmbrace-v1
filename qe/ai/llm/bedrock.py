"""Claude on Amazon Bedrock via the official SDK's Mantle client (ADR-043 P10).

Messages API on Bedrock's Messages endpoint (``anthropic.<model>`` IDs, SigV4 auth
from the normal AWS credential chain, no API key). The SDK is imported lazily so
the fake backend and every test run without the optional dependency
(requirements-ai.txt). All request/response logic and the Opus 5.x rules are in
``qe.ai.llm.messages``. Requires the operator's explicit ``--allow-llm-spend``.
"""

from typing import Any

from qe.ai.llm.messages import MessagesAPILLM


class BedrockLLM(MessagesAPILLM):
    name = "bedrock"

    def __init__(self, *, region: str, timeout_s: float, runtime_client: Any = None):
        super().__init__(timeout_s=timeout_s, runtime_client=runtime_client)
        self._region = region

    def _make_client(self) -> Any:
        from anthropic import AnthropicBedrockMantle  # optional dependency, lazy

        return AnthropicBedrockMantle(
            aws_region=self._region, timeout=self._timeout_s, max_retries=0
        )
