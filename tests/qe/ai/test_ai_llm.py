"""LLM layer: fake determinism, Bedrock adapter (fake runtime), spend guard,
gateway cache / budget / breaker / retries / events."""

import json

import pytest

from qe.ai.config import ModelProfile, ResearchRunConfig
from qe.ai.llm import (
    AnthropicLLM,
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


class _Block:
    def __init__(self, type_, text=""):
        self.type, self.text = type_, text


class _Usage:
    input_tokens, output_tokens = 12, 3
    cache_creation_input_tokens, cache_read_input_tokens = 0, 5


class _Resp:
    def __init__(self, stop_reason="end_turn"):
        # thinking blocks come back empty (display omitted) and must be ignored
        self.content = [_Block("thinking"), _Block("text", '{"a": 1}')]
        self.usage, self.stop_reason = _Usage(), stop_reason


class _Messages:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kw):
        self.owner.calls.append(kw)
        if self.owner.exc:
            raise self.owner.exc
        return _Resp(self.owner.stop_reason)


class _FakeRuntime:
    """Stands in for the SDK client: ``client.messages.create(**kw)``."""

    def __init__(self, exc=None, stop_reason="end_turn"):
        self.exc, self.calls, self.stop_reason = exc, [], stop_reason
        self.messages = _Messages(self)


def _bedrock(rt, **kw):
    return BedrockLLM(region="ap-south-1", timeout_s=5, runtime_client=rt, **kw)


def _anthropic(rt):
    return AnthropicLLM(timeout_s=5, runtime_client=rt)


# Both real backends speak the same Messages API through one shared implementation
# (llm/messages.py), so every request/response rule is asserted against both.
BACKENDS = pytest.mark.parametrize(
    "make,model", [(_bedrock, "anthropic.claude-opus-5-5"), (_anthropic, "claude-opus-5-5")],
    ids=["bedrock", "anthropic"],
)  # fmt: skip


@BACKENDS
def test_messages_request_and_response_mapping(make, model):
    rt = _FakeRuntime()
    req = LLMRequest(model_id=model, system="sys", prompt="p",
                     max_tokens=100, temperature=0.7, effort="low")  # fmt: skip
    resp = make(rt).complete(req)
    assert resp.text == '{"a": 1}'  # thinking blocks ignored, text joined
    assert (resp.input_tokens, resp.output_tokens) == (12 + 5, 3)  # cache reads are input
    assert resp.stop_reason == "end_turn" and resp.latency_ms >= 0
    call = rt.calls[0]
    assert call["model"] == model and call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "p"}]
    assert call["max_tokens"] == 100 and call["output_config"] == {"effort": "low"}
    # Opus 5.x removed sampling params (400) and the model is never given tools
    for banned in ("temperature", "top_p", "top_k", "tools", "tool_choice", "thinking"):
        assert banned not in call


@BACKENDS
def test_messages_omit_output_config_when_no_effort(make, model):
    rt = _FakeRuntime()
    make(rt).complete(_req())
    assert "output_config" not in rt.calls[0]


@BACKENDS
def test_messages_errors_are_sanitized(make, model):
    class APITimeoutError(Exception):
        pass

    class APIStatusError(Exception):
        status_code = 429

    with pytest.raises(LLMTimeout, match=r"^APITimeoutError$"):
        make(_FakeRuntime(APITimeoutError("arn:aws:secret key-material-123"))).complete(_req())
    with pytest.raises(LLMError) as ei:
        make(_FakeRuntime(APIStatusError("req-id 123 arn:x"))).complete(_req())
    assert str(ei.value) == "APIStatusError:429"  # type + status only; provider text never crosses


@BACKENDS
def test_real_backend_without_injected_client_cannot_reach_a_network_in_tests(make, model):
    # conftest blocks sockets; with no injected client the adapter must surface an
    # LLMError (or the optional SDK being absent), never a real call.
    with pytest.raises(LLMError):
        make(None).complete(_req())


class _RecordingSDK:
    """Stands in for the `anthropic` module to observe how each client is constructed."""

    def __init__(self):
        self.built = []

    def _factory(self, name):
        def make(**kw):
            self.built.append((name, kw))
            return _FakeRuntime()

        return make

    def install(self, monkeypatch):
        import sys
        import types

        mod = types.ModuleType("anthropic")
        mod.Anthropic = self._factory("Anthropic")
        mod.AnthropicBedrockMantle = self._factory("AnthropicBedrockMantle")
        monkeypatch.setitem(sys.modules, "anthropic", mod)


def test_first_party_client_is_built_without_credentials_or_sdk_retries(monkeypatch):
    sdk = _RecordingSDK()
    sdk.install(monkeypatch)
    AnthropicLLM(timeout_s=42).complete(_req())
    # qe.ai never handles a key (the SDK resolves it) and disables SDK retries (the gateway owns them)
    assert sdk.built == [("Anthropic", {"timeout": 42, "max_retries": 0})]


def test_bedrock_client_is_built_for_the_configured_region_without_sdk_retries(monkeypatch):
    sdk = _RecordingSDK()
    sdk.install(monkeypatch)
    BedrockLLM(region="us-east-1", timeout_s=42).complete(_req())
    assert sdk.built == [
        ("AnthropicBedrockMantle", {"aws_region": "us-east-1", "timeout": 42, "max_retries": 0})
    ]


def test_backend_names_are_distinct_for_the_journal():
    assert AnthropicLLM(timeout_s=5).name == "anthropic" and _bedrock(None).name == "bedrock"


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
    with pytest.raises(SpendNotAllowed, match="backend=anthropic"):
        build_client(_cfg("anthropic"), allow_spend=False)
    assert isinstance(
        build_client(_cfg("bedrock"), allow_spend=True, runtime_client=_FakeRuntime()), BedrockLLM
    )
    assert isinstance(
        build_client(_cfg("anthropic"), allow_spend=True, runtime_client=_FakeRuntime()),
        AnthropicLLM,
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


def test_gateway_backoff_sleeps_between_retries_only(monkeypatch):
    slept = []
    monkeypatch.setattr("qe.ai.llm.gateway.time.sleep", slept.append)
    fake = FakeLLM(script=[LLMTimeout("t"), LLMTimeout("t")])
    gw = LLMGateway(fake, budget=RunBudget(10_000), breaker=CircuitBreaker(5), cache=None,
                    max_retries=2, backoff_s=2.0)  # fmt: skip
    assert _call(gw).status == ComponentStatus.OK  # third attempt succeeds
    assert slept == [2.0, 4.0]  # linear backoff before each retry, none after success


def test_effort_is_part_of_the_cache_key_and_reaches_the_request():
    from qe.ai.llm import cache_key

    a = LLMRequest("m", "s", "p", 10, effort="low")
    b = LLMRequest("m", "s", "p", 10, effort="high")
    assert cache_key(a, "v") != cache_key(b, "v") != cache_key(LLMRequest("m", "s", "p", 10), "v")


def test_new_optional_config_fields_do_not_move_existing_hashes():
    base = ResearchRunConfig(name="x", book_config="b.yaml",
                             quick_model=ModelProfile(model_id="q", tier="quick"),
                             deep_model=ModelProfile(model_id="d", tier="deep"))  # fmt: skip
    assert "effort" not in base.model_dump(mode="json", exclude_none=True)
    assert "retry_backoff_s" not in base.model_dump(mode="json", exclude_none=True)["budget"]
    tuned = base.model_copy(update={"effort": "low"})
    assert tuned.config_hash() != base.config_hash()  # setting it is a real change
