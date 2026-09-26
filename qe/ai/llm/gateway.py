"""LLMGateway — every qe.ai model call goes through here.

Order of checks per call: secret scan of the prompt (refuse) → breaker (open ⇒
UNAVAILABLE) → cache (hit ⇒ free) → budget reservation (exhausted ⇒
UNAVAILABLE) → provider call with bounded retries on timeout/error. Each call is
reported to an event sink (the research journal) with model, prompt version +
hash, latency, tokens, cache flag, status. A failure never raises past the
gateway: it becomes a component status and the deterministic path continues.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
import time
from typing import Any

from qe.ai.guardrails import SecretLeakError, assert_no_secrets
from qe.ai.llm.base import LLMClient, LLMError, LLMRequest, LLMResponse, LLMTimeout
from qe.ai.llm.budget import BudgetExhausted, CircuitBreaker, RunBudget
from qe.ai.llm.cache import ResponseCache, cache_key
from qe.ai.models.common import ComponentStatus

EventSink = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class CallResult:
    response: LLMResponse | None
    status: ComponentStatus  # OK | TIMEOUT | ERROR | UNAVAILABLE
    error: str | None = None
    attempts: int = 0


@dataclass
class GatewayStats:
    calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    failures: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)  # sanitized provider error labels


class LLMGateway:
    def __init__(
        self,
        client: LLMClient,
        *,
        budget: RunBudget,
        breaker: CircuitBreaker,
        cache: ResponseCache | None,
        max_retries: int,
        sink: EventSink | None = None,
        backoff_s: float = 0.0,
    ):
        self.client = client
        self.budget = budget
        self.breaker = breaker
        self.cache = cache
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.sink = sink or (lambda _t, _d: None)
        self.stats = GatewayStats()

    def call(
        self,
        request: LLMRequest,
        *,
        agent_id: str,
        symbol: str | None,
        prompt_version: str,
        prompt_hash: str,
    ) -> CallResult:
        result = self._call(request, prompt_version)
        resp = result.response
        self.stats.by_status[result.status] = self.stats.by_status.get(result.status, 0) + 1
        self.sink(
            "LLM_CALL",
            {
                "agent_id": agent_id,
                "symbol": symbol,
                "backend": self.client.name,
                "model_id": request.model_id,
                "prompt_version": prompt_version,
                "prompt_hash": prompt_hash,
                "status": result.status,
                "attempts": result.attempts,
                "cached": bool(resp and resp.cached),
                "latency_ms": resp.latency_ms if resp else None,
                "input_tokens": resp.input_tokens if resp else 0,
                "output_tokens": resp.output_tokens if resp else 0,
                "stop_reason": resp.stop_reason if resp else None,
                "error": result.error,
            },
        )
        return result

    def _call(self, request: LLMRequest, prompt_version: str) -> CallResult:
        try:
            assert_no_secrets(request.system + "\n" + request.prompt)
        except SecretLeakError as exc:
            return CallResult(None, ComponentStatus.ERROR, str(exc))
        if self.breaker.is_open:
            return CallResult(None, ComponentStatus.UNAVAILABLE, "circuit breaker open")

        key = cache_key(request, prompt_version)
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                self.stats.cache_hits += 1
                return CallResult(hit, ComponentStatus.OK, None, 0)

        attempts = 0
        last_status, last_error = ComponentStatus.ERROR, "not attempted"
        while attempts <= self.max_retries:
            try:
                self.budget.reserve(request.max_tokens)
            except BudgetExhausted as exc:
                return CallResult(None, ComponentStatus.UNAVAILABLE, str(exc), attempts)
            attempts += 1
            self.stats.calls += 1
            try:
                resp = self.client.complete(request)
            except LLMTimeout as exc:
                last_status, last_error = ComponentStatus.TIMEOUT, f"timeout: {exc}"
                self.stats.errors[str(exc)] = self.stats.errors.get(str(exc), 0) + 1
            except LLMError as exc:
                last_status, last_error = ComponentStatus.ERROR, f"provider error: {exc}"
                self.stats.errors[str(exc)] = self.stats.errors.get(str(exc), 0) + 1
            else:
                self.budget.record(resp.input_tokens, resp.output_tokens)
                self.stats.input_tokens += resp.input_tokens
                self.stats.output_tokens += resp.output_tokens
                self.breaker.record_success()
                if self.cache is not None:
                    self.cache.put(key, resp)
                return CallResult(resp, ComponentStatus.OK, None, attempts)
            self.stats.failures += 1
            self.breaker.record_failure()
            if self.breaker.is_open:
                break
            if self.backoff_s and attempts <= self.max_retries:
                time.sleep(self.backoff_s * attempts)  # linear backoff before the retry
        return CallResult(None, last_status, last_error, attempts)
