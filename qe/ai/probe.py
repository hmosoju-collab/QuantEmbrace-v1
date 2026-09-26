"""Cheap access probe for the configured LLM backend (ADR-043 P10).

One tiny request (a few tokens, a fraction of a cent) that answers "can this
account actually call this model, here?" BEFORE a research run, and turns the
provider's status into an actionable hint. Real backends still need
``--allow-llm-spend``; the fake backend is reported as nothing to probe.
"""

from dataclasses import dataclass

from qe.ai.config import ResearchRunConfig
from qe.ai.llm import LLMClient, LLMError, LLMRequest, LLMTimeout

_HINTS = {
    "403": (
        "The account is not entitled to this model on this endpoint/region (Bedrock returns "
        "'not available for this account'). `aws bedrock get-foundation-model-availability` can "
        "still say AUTHORIZED. Request/enable Anthropic model access for the account in the "
        "Bedrock console (use-case details / model access) or contact AWS Sales; then re-probe."
    ),
    "404": (
        "The Messages endpoint in this region does not serve this model ID (or the ID is wrong). "
        "Check the ID against the models overview and try a region where the endpoint is live "
        "(for example us-east-1); ap-south-1 returned 404 for every model on 2026-09-26."
    ),
    "400": "The request was rejected: check effort / max_tokens for this model (Opus 5.x rejects "
    "sampling parameters and cannot disable thinking).",
    "401": "Credentials were rejected: refresh the AWS credentials/profile in use.",
    "429": "Throttled: retry later or request a quota increase.",
}


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    backend: str
    model_id: str
    detail: str
    hint: str = ""


def run_probe(cfg: ResearchRunConfig, client: LLMClient) -> ProbeResult:
    model = cfg.quick_model.model_id
    request = LLMRequest(
        model_id=model,
        system="Reply with the single word: ok",
        prompt="ping",
        max_tokens=256,  # room for always-on thinking on Opus 5.x; still a few hundred tokens at most
        effort=cfg.effort,
    )
    try:
        resp = client.complete(request)
    except LLMTimeout as exc:
        return ProbeResult(False, client.name, model, f"timeout: {exc}", "The call timed out.")
    except LLMError as exc:
        label = str(exc)
        status = label.rsplit(":", 1)[-1]
        return ProbeResult(False, client.name, model, label, _HINTS.get(status, ""))
    return ProbeResult(
        True,
        client.name,
        model,
        f"stop={resp.stop_reason} tokens in/out={resp.input_tokens}/{resp.output_tokens} "
        f"latency={resp.latency_ms:.0f}ms",
    )


def llm_health(
    llm_calls: int, failures: int, input_tokens: int, errors: dict[str, int]
) -> str | None:
    """A loud, honest summary when the backend failed; None when it is healthy."""
    if not failures:
        return None
    seen = ", ".join(f"{k} x{v}" for k, v in sorted(errors.items())) or "unknown"
    if input_tokens == 0:
        return (
            f"the LLM backend produced NO usable output ({failures} failed call(s): {seen}). "
            "Every AI component is UNAVAILABLE; the quant view is unaffected. "
            "Run `python -m qe.ai probe` to diagnose."
        )
    return f"{failures} of {llm_calls} LLM call(s) failed ({seen}); affected components are UNAVAILABLE."
