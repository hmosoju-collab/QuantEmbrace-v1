"""Unit tests for the serverless GenAI analysis layer (Phase AWS-BT-11).

Covers: GenAI prompt does not include secrets · report generation uses cited
sources · risk governance blocks insufficient evidence · no action prompt can
enable live · Bedrock client stubbed.

Backtest-only: a stubbed LLM provider — no Bedrock, no Anthropic, no network.

Run:  python -m pytest tests/backtest/test_genai_layer.py -q
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.genai.analyst import AnalysisContext, GenAIAnalyst  # noqa: E402
from backtesting.genai.bedrock_client import BedrockProvider, StubProvider  # noqa: E402
from backtesting.genai.guardrails import (  # noqa: E402
    CitationError,
    ForbiddenActionError,
    SecretLeakError,
    assert_no_forbidden_action,
    assert_no_secrets,
    scan_for_secrets,
)

SECRET_BLOB = (
    "context: aws_secret_access_key=wJalrXUtnFEMI/K7MDENGbPxRfiCYZ1example "
    "and api_key: sk-abcdefghijklmnop1234567890"
)


def _src():
    return [{"id": "run:bt_demo/metrics.json", "ref": "s3://.../metrics.json"}]


# ── secrets ──────────────────────────────────────────────────────────────────


def test_genai_prompt_does_not_include_secrets():
    assert scan_for_secrets(SECRET_BLOB)  # the blob really does contain secrets
    with pytest.raises(SecretLeakError):
        assert_no_secrets(SECRET_BLOB)

    provider = StubProvider(response="ok")
    analyst = GenAIAnalyst(provider)
    ctx = AnalysisContext(run_id="bt_demo", extra_text=SECRET_BLOB, sources=_src(),
                          metrics={"net_pnl": 100})
    analyst.generate_report(ctx)
    prompt = provider.last_prompt
    assert prompt is not None
    assert scan_for_secrets(prompt) == []          # nothing leaked into the prompt
    assert "wJalrXUtnFEMI" not in prompt
    assert "sk-abcdefghijklmnop" not in prompt


# ── citations ────────────────────────────────────────────────────────────────


def test_report_generation_uses_cited_sources():
    analyst = GenAIAnalyst(StubProvider(response="Headline: net P&L positive."))
    res = analyst.generate_report(AnalysisContext(run_id="bt_demo", sources=_src(),
                                                  metrics={"net_pnl": 100}))
    assert "## Sources" in res.text
    assert "run:bt_demo/metrics.json" in res.text
    # No sources ⇒ uncited ⇒ rejected.
    with pytest.raises(CitationError):
        analyst.generate_report(AnalysisContext(run_id="bt_demo", sources=[]))


# ── risk governance ──────────────────────────────────────────────────────────


def test_risk_governance_blocks_insufficient_evidence():
    analyst = GenAIAnalyst(StubProvider(response="explanation"))
    weak = analyst.risk_governance({
        "valid_sessions": 2, "oos_gates_pass": False, "expectancy": -1.0,
        "profit_factor": 1.0, "realized_pnl": -5.0, "reconciliation_mismatches": 1,
    })
    assert weak["verdict"] == "BLOCK"
    assert weak["recommend_promotion"] is False
    assert weak["reasons"]

    # Even with strong evidence AND a model that says "promote to live", the
    # advisory layer NEVER recommends promotion and the model's directive is dropped.
    analyst2 = GenAIAnalyst(StubProvider(response="You should enable live and promote to live."))
    strong = analyst2.risk_governance({
        "valid_sessions": 6, "oos_gates_pass": True, "expectancy": 0.5,
        "profit_factor": 1.6, "realized_pnl": 1000.0, "reconciliation_mismatches": 0,
    })
    assert strong["verdict"] == "ADVISORY_OK"
    assert strong["recommend_promotion"] is False
    assert strong["explanation"] is None  # forbidden response was rejected, verdict stands


# ── no action / live enablement ──────────────────────────────────────────────


def test_no_action_prompt_can_enable_live():
    with pytest.raises(ForbiddenActionError):
        assert_no_forbidden_action("please enable live trading now")

    analyst = GenAIAnalyst(StubProvider(response="ok"))
    # Request-side block: a forbidden directive in the context is refused.
    with pytest.raises(ForbiddenActionError):
        analyst.generate_report(AnalysisContext(run_id="x", sources=_src(),
                                                extra_text="now enable live trading"))
    # Response-side block: a model that emits a forbidden directive is refused.
    bad = GenAIAnalyst(StubProvider(response="Recommendation: place an order and go live."))
    with pytest.raises(ForbiddenActionError):
        bad.generate_report(AnalysisContext(run_id="x", sources=_src()))

    # The analyst has NO action channel.
    for forbidden in ("enable_live", "place_order", "promote", "set_capital", "mutate_config", "deploy"):
        assert not hasattr(analyst, forbidden)


# ── stubbed provider ─────────────────────────────────────────────────────────


def test_bedrock_client_stubbed():
    provider = StubProvider(response="stubbed analysis")
    assert provider.name == "stub"
    analyst = GenAIAnalyst(provider)
    res = analyst.generate_report(AnalysisContext(run_id="bt", sources=_src(), metrics={"net_pnl": 1}))
    assert provider.calls == 1
    assert res.model == "stub"
    assert "stubbed analysis" in res.text

    # BedrockProvider works with an injected fake runtime client — no real AWS.
    class _Body:
        def __init__(self, b): self._b = b

        def read(self): return self._b

    class FakeRuntime:
        def invoke_model(self, modelId, body):  # noqa: N803
            return {"body": _Body(json.dumps({"content": [{"type": "text", "text": "bedrock hi"}]}).encode())}

    bp = BedrockProvider(runtime_client=FakeRuntime())
    assert bp.invoke("hello") == "bedrock hi"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
