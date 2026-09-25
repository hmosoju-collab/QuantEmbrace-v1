"""Amazon Bedrock (Converse API) client — the only qe.ai module allowed boto3.

IAM-native (no API key to leak). boto3 is imported lazily, and only when a
real call is made; building this client at all requires the operator's
explicit ``--allow-llm-spend`` (see ``qe.ai.llm.build_client``). botocore's own
retries are disabled — the gateway owns the bounded retry policy.
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
            import boto3
            from botocore.config import Config

            self._client = boto3.client(  # noqa: TID251 — Bedrock runtime, not SQS
                "bedrock-runtime",
                region_name=self._region,
                config=Config(
                    connect_timeout=5,
                    read_timeout=self._timeout_s,
                    retries={"max_attempts": 0},
                ),
            )
        return self._client

    def complete(self, request: LLMRequest) -> LLMResponse:
        t0 = time.perf_counter()
        try:
            resp = self._runtime().converse(
                modelId=request.model_id,
                system=[{"text": request.system}],
                messages=[{"role": "user", "content": [{"text": request.prompt}]}],
                inferenceConfig={
                    "maxTokens": request.max_tokens,
                    "temperature": request.temperature,
                },
            )
        except Exception as exc:
            # Only the exception type crosses this boundary: provider messages
            # can carry request ids / ARNs that do not belong in journals.
            kind = type(exc).__name__
            if "Timeout" in kind:
                raise LLMTimeout(kind) from None
            raise LLMError(kind) from None
        content = resp.get("output", {}).get("message", {}).get("content", [])
        usage = resp.get("usage", {})
        return LLMResponse(
            text="".join(part.get("text", "") for part in content),
            model_id=request.model_id,
            input_tokens=int(usage.get("inputTokens", 0)),
            output_tokens=int(usage.get("outputTokens", 0)),
            latency_ms=float(
                resp.get("metrics", {}).get("latencyMs", (time.perf_counter() - t0) * 1000)
            ),
            stop_reason=str(resp.get("stopReason", "")),
        )
