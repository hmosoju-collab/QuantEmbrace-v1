"""Shared Messages-API client logic for the real backends (ADR-043 P10).

Both real backends (Amazon Bedrock and the first-party Anthropic API) speak the
same Messages API through the official SDK, so the request/response mapping — and
the model-family rules it encodes — live here exactly once. This module imports
NO SDK: subclasses supply ``_runtime()`` and import the SDK lazily in their own
module, which is the only place ``anthropic`` may be imported.

Claude Opus 5.x rules encoded (verified against the official docs 2026-09-26):
  * sampling parameters (temperature / top_p / top_k) were removed — sending one
    is a 400, so ``LLMRequest.temperature`` is deliberately NOT forwarded;
  * thinking is always on and cannot be disabled — depth is controlled with an
    explicit ``output_config.effort`` (default on Opus 5.5 is ``medium``); thinking
    tokens are billed as output and count against ``max_tokens`` (callers leave
    headroom — the research configs use 4096);
  * no tools are ever declared — the model has no action surface.
SDK retries are disabled (``max_retries=0``); the gateway owns bounded retries.
"""

import time
from typing import Any

from qe.ai.llm.base import LLMError, LLMRequest, LLMResponse, LLMTimeout

MISSING_SDK = "optional dependency missing: pip install -r requirements-ai.txt"


def _label(exc: Exception) -> str:
    """Exception type and HTTP status only. Provider messages can carry request ids /
    ARNs / org details that do not belong in journals."""
    kind = type(exc).__name__
    status = getattr(exc, "status_code", None)
    return f"{kind}:{status}" if isinstance(status, int) else kind


class MessagesAPILLM:
    name = "messages"

    def __init__(self, *, timeout_s: float, runtime_client: Any = None):
        self._timeout_s = timeout_s
        self._client = runtime_client

    def _make_client(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def _runtime(self) -> Any:
        if self._client is None:
            self._client = self._make_client()
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
            raise LLMError(MISSING_SDK) from None
        except Exception as exc:  # e.g. the SDK refusing to build a client without credentials
            raise LLMError(_label(exc)) from None
        t0 = time.perf_counter()
        try:
            resp = client.messages.create(**kwargs)
        except Exception as exc:
            label = _label(exc)
            if "Timeout" in type(exc).__name__:
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
