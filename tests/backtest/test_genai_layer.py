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


def test_redact_secrets_transforms_patterns():
    """redact_secrets removes all secret patterns; the result passes assert_no_secrets."""
    from backtesting.genai.guardrails import redact_secrets

    redacted = redact_secrets(SECRET_BLOB)
    assert "[REDACTED]" in redacted
    assert "wJalrXUtnFEMI" not in redacted
    assert "sk-abcdefghijklmnop" not in redacted
    assert_no_secrets(redacted)  # should not raise after redaction


def test_analyst_strategy_tee_dataset_methods():
    """analyze_strategy, analyze_tee, and explain_dataset all require sources and return AnalysisResult."""
    from backtesting.genai.analyst import AnalysisResult

    provider = StubProvider(response="Advisory analysis only.")
    analyst = GenAIAnalyst(provider)
    ctx = AnalysisContext(run_id="bt_demo", sources=_src(), metrics={"net_pnl": 50})

    for method, kind in [
        (analyst.analyze_strategy, "strategy_analysis"),
        (analyst.analyze_tee, "tee_analysis"),
        (analyst.explain_dataset, "model_dataset"),
    ]:
        res = method(ctx)
        assert isinstance(res, AnalysisResult)
        assert res.kind == kind
        assert "## Sources" in res.text
        assert res.model == "stub"

    empty_ctx = AnalysisContext(run_id="bt_demo", sources=[])
    for method in (analyst.analyze_strategy, analyst.analyze_tee, analyst.explain_dataset):
        with pytest.raises(CitationError):
            method(empty_ctx)


def test_evaluate_evidence_boundary_conditions():
    """Boundary: exactly 5 valid sessions passes; profit_factor=1.2 blocks; recommend_promotion always False."""
    from backtesting.genai.guardrails import evaluate_evidence

    # Exactly MIN_VALID_SESSIONS (5) is sufficient — condition is < 5, not <= 5.
    at_boundary = evaluate_evidence({
        "valid_sessions": 5, "oos_gates_pass": True, "expectancy": 0.01,
        "profit_factor": 1.21, "realized_pnl": 0.01, "reconciliation_mismatches": 0,
    })
    assert at_boundary["verdict"] == "ADVISORY_OK"
    assert at_boundary["recommend_promotion"] is False  # never True

    # profit_factor exactly 1.2 blocks (condition is <= 1.2, not < 1.2).
    exact_pf = evaluate_evidence({
        "valid_sessions": 5, "oos_gates_pass": True, "expectancy": 0.01,
        "profit_factor": 1.2, "realized_pnl": 0.01, "reconciliation_mismatches": 0,
    })
    assert exact_pf["verdict"] == "BLOCK"
    assert "profit_factor" in " ".join(exact_pf["reasons"])
    assert exact_pf["recommend_promotion"] is False  # also never True on BLOCK


def test_get_provider_factory():
    """`get_provider` returns the right type; raises ValueError for unknown names."""
    from backtesting.genai.bedrock_client import get_provider

    p = get_provider("stub", response="hi")
    assert isinstance(p, StubProvider)
    assert p.invoke("x") == "hi"

    with pytest.raises(ValueError, match="Unknown provider"):
        get_provider("unknown_backend")


def test_athena_queries_render_and_guard():
    """`render()` substitutes params; unknown query raises KeyError; all queries are read-only."""
    from backtesting.genai.athena_queries import ATHENA_QUERIES, render

    sql = render("exit_reason_distribution", db="bt_prod", run_id="run_001")
    assert "bt_prod" in sql and "run_001" in sql

    sql2 = render("top_runs_by_expectancy", db="bt_prod", limit=10)
    assert "10" in sql2

    with pytest.raises(KeyError, match="Unknown query"):
        render("nonexistent_query")

    # All Athena templates must be read-only — no write DDL/DML.
    write_keywords = {"INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE"}
    for name, template in ATHENA_QUERIES.items():
        upper = template.upper()
        hits = [w for w in write_keywords if w in upper]
        assert hits == [], f"Query {name!r} contains write keyword(s): {hits}"


def test_no_broker_calls_in_genai_modules():
    """All genai modules must not import or call broker APIs.

    Note: guardrails.py legitimately names 'place_order' / 'submit_order' as
    regex strings it blocks — so we check for broker *imports*, not occurrences
    of any string that could appear in the forbidden-action pattern list.
    """
    import backtesting.genai.analyst as a_mod
    import backtesting.genai.athena_queries as aq_mod
    import backtesting.genai.bedrock_client as bc_mod
    import backtesting.genai.guardrails as g_mod
    import backtesting.genai.prompts as pr_mod

    import_forbidden = ["import kiteconnect", "import alpaca", "from zerodha", "import zerodha"]
    for mod in (a_mod, g_mod, bc_mod, pr_mod, aq_mod):
        src = Path(mod.__file__).read_text().lower()
        present = [t for t in import_forbidden if t in src]
        assert present == [], f"{mod.__name__} must not import broker libraries: {present}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
