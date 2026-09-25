"""LLM layer: fake determinism, Bedrock adapter (fake runtime), spend guard,
gateway cache / budget / breaker / retries / events."""

import json

import pytest

from qe.ai.config import ModelProfile, ResearchRunConfig
from qe.ai.llm import (
    BedrockLLM,
    CircuitBreaker,
    FakeLLM,
    LLMError,
    LLMGateway,
    LLMRequest,
    LLMTimeout,
    ResponseCache,
    RunBudget,
    SpendNotAllowed,
    build_client,
)
from qe.ai.llm.fake import default_payload
from qe.ai.models import ComponentStatus


def _req(prompt: str = "RESPONSE_SCHEMA: analyst/1\nALLOWED_EVIDENCE_IDS: tech.a,tech.b\n", mt=100):
    return LLMRequest(model_id="m", system="sys", prompt=prompt, max_tokens=mt)


def _gw(client, *, budget=10_000, threshold=3, retries=1, cache=True, events=None):
    return LLMGateway(
        client,
        budget=RunBudget(budget),
        breaker=CircuitBreaker(threshold),
        cache=ResponseCache() if cache else None,
        max_retries=retries,
        sink=(lambda t, d: events.append((t, d))) if events is not None else None,
    )


def _call(gw, req=None):
    return gw.call(
        req or _req(), agent_id="technical", symbol="AAA", prompt_version="v", prompt_hash="h"
    )


@pytest.mark.parametrize(
    "schema", ["analyst/1", "risk_analyst/1", "debate/1", "critic/1", "synthesis/1"]
)
def test_fake_default_payload_is_deterministic(schema):
    r = _req(f"RESPONSE_SCHEMA: {schema}\nALLOWED_EVIDENCE_IDS: tech.a,tech.b\n")
    a, b = FakeLLM(seed=3).complete(r), FakeLLM(seed=3).complete(r)
    assert a.text == b.text
    assert json.loads(a.text) == default_payload(r, 3)
    assert FakeLLM(seed=4).complete(r).text != a.text or schema in ("critic/1",)


def test_fake_script_simulates_failures():
    fake = FakeLLM(script=["not json", LLMTimeout("t")])
    assert fake.complete(_req()).text == "not json"
    with pytest.raises(LLMTimeout):
        fake.complete(_req())
    assert json.loads(fake.complete(_req()).text)["evidence_ids"] == ["tech.a", "tech.b"]


class _FakeRuntime:
    def __init__(self, exc=None):
        self.exc, self.calls = exc, []

    def converse(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return {
            "output": {"message": {"content": [{"text": '{"a": 1}'}]}},
            "usage": {"inputTokens": 12, "outputTokens": 3},
            "metrics": {"latencyMs": 42},
            "stopReason": "end_turn",
        }


def test_bedrock_converse_request_and_response_mapping():
    rt = _FakeRuntime()
    resp = BedrockLLM(region="ap-south-1", timeout_s=5, runtime_client=rt).complete(_req())
    assert (resp.text, resp.input_tokens, resp.output_tokens, resp.latency_ms) == (
        '{"a": 1}',
        12,
        3,
        42.0,
    )
    call = rt.calls[0]
    assert call["modelId"] == "m" and call["system"] == [{"text": "sys"}]
    assert call["inferenceConfig"] == {"maxTokens": 100, "temperature": 0.0}
    assert "toolConfig" not in call  # the model is never given tools


def test_bedrock_errors_are_sanitized():
    class ReadTimeoutError(Exception):
        pass

    with pytest.raises(LLMTimeout, match=r"^ReadTimeoutError$"):
        BedrockLLM(
            region="r", timeout_s=5, runtime_client=_FakeRuntime(ReadTimeoutError("arn:aws:secret"))
        ).complete(_req())
    with pytest.raises(LLMError) as ei:
        BedrockLLM(
            region="r", timeout_s=5, runtime_client=_FakeRuntime(ValueError("req-id 123 arn:x"))
        ).complete(_req())
    assert str(ei.value) == "ValueError"  # provider message (ids/ARNs) never crosses


def test_bedrock_without_injected_client_cannot_reach_aws_in_tests():
    # conftest patches boto3.client to raise; the adapter must surface an LLMError.
    with pytest.raises(LLMError):
        BedrockLLM(region="r", timeout_s=5).complete(_req())


def _cfg(backend: str) -> ResearchRunConfig:
    return ResearchRunConfig(
        name="t",
        book_config="b.yaml",
        backend=backend,
        quick_model=ModelProfile(model_id="q", tier="quick"),
        deep_model=ModelProfile(model_id="d", tier="deep"),
    )


def test_real_backend_requires_spend_flag():
    assert isinstance(build_client(_cfg("fake"), allow_spend=False), FakeLLM)
    with pytest.raises(SpendNotAllowed, match="--allow-llm-spend"):
        build_client(_cfg("bedrock"), allow_spend=False)
    assert isinstance(
        build_client(_cfg("bedrock"), allow_spend=True, runtime_client=_FakeRuntime()), BedrockLLM
    )


def test_gateway_cache_dedupes_identical_calls():
    fake = FakeLLM()
    gw = _gw(fake)
    a, b = _call(gw), _call(gw)
    assert a.status == b.status == ComponentStatus.OK
    assert b.response.cached and not a.response.cached
    assert len(fake.requests) == 1 and gw.stats.cache_hits == 1


def test_gateway_retries_timeout_then_succeeds():
    fake = FakeLLM(script=[LLMTimeout("t")])
    r = _call(_gw(fake, retries=1, cache=False))
    assert r.status == ComponentStatus.OK and r.attempts == 2


def test_gateway_timeout_exhausts_retries():
    fake = FakeLLM(script=[LLMTimeout("t"), LLMTimeout("t")])
    r = _call(_gw(fake, retries=1, cache=False))
    assert r.status == ComponentStatus.TIMEOUT and r.response is None and r.attempts == 2


def test_budget_exhaustion_is_unavailable_not_a_crash():
    fake = FakeLLM()
    r = _call(_gw(fake, budget=50), _req(mt=100))
    assert r.status == ComponentStatus.UNAVAILABLE and "budget" in r.error
    assert fake.requests == []


def test_breaker_opens_and_stops_calling_provider():
    fake = FakeLLM(script=[LLMError("x")] * 10)
    gw = _gw(fake, threshold=2, retries=0, cache=False)
    assert _call(gw).status == ComponentStatus.ERROR
    assert _call(gw).status == ComponentStatus.ERROR
    n = len(fake.requests)
    r = _call(gw)
    assert r.status == ComponentStatus.UNAVAILABLE and "breaker" in r.error
    assert len(fake.requests) == n  # no further provider calls


def test_prompt_with_secret_is_refused_before_any_call():
    fake = FakeLLM()
    r = _call(_gw(fake), _req("RESPONSE_SCHEMA: analyst/1\napi_key=abcd1234efgh5678\n"))
    assert r.status == ComponentStatus.ERROR and fake.requests == []


def test_gateway_emits_llm_call_events():
    events = []
    _call(_gw(FakeLLM(), events=events))
    kind, data = events[0]
    assert kind == "LLM_CALL"
    for k in ("agent_id", "model_id", "prompt_version", "prompt_hash", "status", "latency_ms",
              "input_tokens", "output_tokens", "cached", "attempts", "error"):  # fmt: skip
        assert k in data


def test_disk_cache_writes_only_under_ai_cache(tmp_path):
    cache = ResponseCache(tmp_path)
    gw = LLMGateway(
        FakeLLM(), budget=RunBudget(10_000), breaker=CircuitBreaker(3), cache=cache, max_retries=0
    )
    _call(gw)
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written and all("backtest-data/ai_cache/llm" in str(p) for p in written)
    # a fresh cache over the same dir replays the stored response
    assert ResponseCache(tmp_path).get(next(iter(cache._mem))).cached
