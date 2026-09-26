"""P10 access probe and loud LLM-failure reporting."""

import pytest
import yaml

from qe.ai.cli import main
from qe.ai.config import ModelProfile, ResearchRunConfig
from qe.ai.llm import BedrockLLM, FakeLLM, LLMError, LLMGateway, LLMTimeout
from qe.ai.probe import llm_health, run_probe

CFG = ResearchRunConfig(
    name="p",
    book_config="b.yaml",
    backend="bedrock",
    effort="low",
    quick_model=ModelProfile(model_id="anthropic.claude-opus-5-5", tier="quick"),
    deep_model=ModelProfile(model_id="anthropic.claude-opus-5-5", tier="deep"),
)


class _Runtime:
    """Stands in for AnthropicBedrockMantle."""

    def __init__(self, exc=None):
        self.exc = exc
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc

        class R:
            usage = type("U", (), {"input_tokens": 9, "output_tokens": 4})()
            stop_reason = "end_turn"

            def __init__(self):
                self.content = [type("B", (), {"type": "text", "text": "ok"})()]

        return R()


def _bedrock(exc=None):
    rt = _Runtime(exc)
    return BedrockLLM(region="ap-south-1", timeout_s=5, runtime_client=rt), rt


def _status_error(code):
    return type("PermissionDeniedError", (Exception,), {"status_code": code})("provider text")


def test_probe_success_is_tiny_and_uses_the_configured_effort():
    client, rt = _bedrock()
    res = run_probe(CFG, client)
    assert res.ok and "tokens in/out=9/4" in res.detail
    call = rt.calls[0]
    assert call["max_tokens"] == 256 and call["output_config"] == {"effort": "low"}
    assert call["messages"][0]["content"] == "ping"  # nothing but a ping ever leaves


@pytest.mark.parametrize(
    "code,needle",
    [
        (403, "not entitled"),
        (404, "does not serve this model"),
        (400, "rejected"),
        (429, "Throttled"),
    ],
)
def test_probe_failures_carry_actionable_hints(code, needle):
    client, _ = _bedrock(_status_error(code))
    res = run_probe(CFG, client)
    assert not res.ok and res.detail.endswith(f":{code}") and needle in res.hint
    assert "provider text" not in res.detail  # provider messages never surface


def test_probe_timeout():
    class Timeout(LLMTimeout):
        pass

    class C:
        name = "bedrock"

        def complete(self, req):
            raise Timeout("APITimeoutError")

    res = run_probe(CFG, C())
    assert not res.ok and "timeout" in res.detail


def test_cli_probe_refuses_without_the_spend_flag_and_fake_has_nothing_to_probe(tmp_path, capsys):
    (tmp_path / "ai.yaml").write_text(yaml.safe_dump(CFG.model_dump(mode="json")))
    assert main(["probe", "--config", "ai.yaml", "--base-dir", str(tmp_path)]) == 2
    assert "--allow-llm-spend" in capsys.readouterr().err
    fake = CFG.model_copy(update={"backend": "fake"})
    (tmp_path / "fake.yaml").write_text(yaml.safe_dump(fake.model_dump(mode="json")))
    assert main(["probe", "--config", "fake.yaml", "--base-dir", str(tmp_path)]) == 0
    assert "nothing to probe" in capsys.readouterr().out


def test_health_summary_is_loud_when_every_call_failed_and_quiet_when_healthy():
    assert llm_health(7, 0, 100, {}) is None
    total = llm_health(3, 3, 0, {"NotFoundError:404": 3})
    assert "NO usable output" in total and "NotFoundError:404 x3" in total and "probe" in total
    partial = llm_health(10, 2, 500, {"APITimeoutError": 2})
    assert "2 of 10" in partial and "NO usable" not in partial


def test_gateway_records_sanitized_error_labels():
    from qe.ai.llm import CircuitBreaker, LLMRequest, RunBudget

    fake = FakeLLM(script=[LLMError("NotFoundError:404"), LLMError("NotFoundError:404")])
    gw = LLMGateway(
        fake, budget=RunBudget(1000), breaker=CircuitBreaker(5), cache=None, max_retries=1
    )
    gw.call(LLMRequest("m", "s", "RESPONSE_SCHEMA: analyst/1\n", 10),
            agent_id="a", symbol=None, prompt_version="v", prompt_hash="h")  # fmt: skip
    assert gw.stats.errors == {"NotFoundError:404": 2} and gw.stats.failures == 2
