"""Claude on Amazon Bedrock via the official SDK's Mantle client (ADR-043 P10).

The only qe.ai module allowed to import ``anthropic`` (lazily, so the fake
backend and every test run without the optional dependency — see
requirements-ai.txt). Uses the Messages API on Bedrock's Messages endpoint
(``anthropic.<model>`` IDs, SigV4 auth from the normal AWS credential chain, no
API key). Building this client at all requires the operator's explicit
``--allow-llm-spend`` (``qe.ai.llm.build_client``).

Model-specific facts this adapter encodes (Claude Opus 5.x, verified against the
official docs 2026-09-26):
  * sampling parameters (temperature / top_p / top_k) were removed — sending one
    is a 400, so ``LLMRequest.temperature`` is deliberately NOT forwarded;
  * thinking is always on and cannot be disabled — control depth with an explicit
    ``output_config.effort`` (the default on Opus 5.5 is ``medium``); thinking
    tokens are billed as output and count against ``max_tokens``, so callers give
    real headroom (the research config uses 4096);
  * no tools are ever declared — the model has no action surface.
SDK retries are disabled (``max_retries=0``); the gateway owns bounded retries.
"""

import time
from typing import Any

from qe.ai.llm.base import LLMError, LLMRequest, LLMResponse, LLMTimeout


class BedrockLLM:
    name = "bedrock"

    def __init__(self, *, region: str, timeout_s: float, runtime_client: Any = None):
        self._region = region
        self._timeout_s = timeout_s
        self._client = runtime_client

    def _runtime(self) -> Any:
        if self._client is None:
            from anthropic import AnthropicBedrockMantle  # optional dependency, lazy

            self._client = AnthropicBedrockMantle(
                aws_region=self._region, timeout=self._timeout_s, max_retries=0
            )
        return self._client

    def complete(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": request.model_id,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.effort:
            kwargs["output_config"] = {"effort": request.effort}
        try:
            client = self._runtime()
        except ImportError:
            raise LLMError(
                "optional dependency missing: pip install -r requirements-ai.txt"
            ) from None
        t0 = time.perf_counter()
        try:
            resp = client.messages.create(**kwargs)
        except Exception as exc:
            # Only the exception type and HTTP status cross this boundary: provider
            # messages can carry request ids / ARNs that do not belong in journals.
            kind = type(exc).__name__
            status = getattr(exc, "status_code", None)
            label = f"{kind}:{status}" if isinstance(status, int) else kind
            if "Timeout" in kind:
                raise LLMTimeout(label) from None
            raise LLMError(label) from None
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        usage = resp.usage
        return LLMResponse(
            text=text,
            model_id=request.model_id,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0)
            + int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
            + int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            latency_ms=(time.perf_counter() - t0) * 1000,
            stop_reason=str(getattr(resp, "stop_reason", "") or ""),
        )
