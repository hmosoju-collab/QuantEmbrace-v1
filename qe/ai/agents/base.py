"""Agent runner: evidence in, one validated AgentObservation out.

An agent is a spec (role, task, versioned prompt, output schema), not code with
side effects. ``run_agent``:

  1. no evidence ⇒ UNAVAILABLE with zero LLM calls (never "analyze" from memory);
  2. renders a prompt from evidence *values and meanings only* — no dates, no
     run/trace ids, and (by default) no ticker — inside untrusted-data blocks;
  3. calls the gateway (cache / budget / breaker / retries / journaling);
  4. parses strictly: forbidden content ⇒ BLOCKED (no retry); bad JSON, schema
     violation, or an evidence id not in the allowlist ⇒ one corrective retry,
     then MALFORMED.

The observation carries evidence *ids*; the orchestrator attaches the real
Evidence records, so a model can never invent a source or a timestamp.
"""

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from qe.ai.agents.prompts import SCHEMA_FIELDS, SCHEMAS, SYSTEM_PROMPT
from qe.ai.config import ModelProfile
from qe.ai.guardrails import ForbiddenOutputError, assert_no_forbidden, data_block, redact
from qe.ai.llm import LLMGateway, LLMRequest
from qe.ai.models import AgentObservation, ComponentStatus, Evidence
from qe.ai.tools.pit import ToolResult

Tier = Literal["quick", "deep"]


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    prompt_version: str  # "<agent>/<n>" — bump when role/task/schema text changes
    schema_id: str
    role: str
    task: str
    tier: Tier = "quick"

    @property
    def prompt_hash(self) -> str:
        text = "\n".join(
            (
                SYSTEM_PROMPT,
                self.role,
                self.task,
                self.schema_id,
                SCHEMA_FIELDS[self.schema_id],
                self.prompt_version,
            )
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class AgentContext:
    """What every agent call needs from the orchestrator."""

    gateway: LLMGateway
    quick: ModelProfile
    deep: ModelProfile
    market: str
    max_tokens: int
    temperature: float
    max_retries: int
    mask_identifiers: bool = True


@dataclass(frozen=True)
class AgentRun:
    observation: AgentObservation
    output: BaseModel | None = None


def subject_label(symbol: str | None, market: str, mask: bool) -> str:
    if symbol is None:
        return f"the {market} equity market as a whole"
    if mask:
        return f"one {market}-listed equity (identifier withheld)"
    return f"{market}:{re.sub(r'[^A-Z0-9&_.-]', '', symbol.upper())}"


def evidence_payload(evidence: Sequence[Evidence]) -> list[dict[str, Any]]:
    return [{"id": e.evidence_id, "value": e.value, "meaning": e.summary} for e in evidence]


def render_prompt(
    spec: AgentSpec,
    *,
    subject: str,
    blocks: Sequence[tuple[str, Any]],
    allowed_ids: Sequence[str],
    correction: str | None = None,
) -> str:
    lines = [
        f"ROLE: {spec.role}",
        f"TASK: {spec.task}",
        f"SUBJECT: {subject}",
        f"RESPONSE_SCHEMA: {spec.schema_id}",
        f"SCHEMA_FIELDS: {SCHEMA_FIELDS[spec.schema_id]}",
        f"ALLOWED_EVIDENCE_IDS: {','.join(allowed_ids)}",
        "",
        *(data_block(label, payload) for label, payload in blocks),
    ]
    if correction:
        lines += [
            "",
            f"Your previous response was rejected ({correction}). Return only the corrected JSON object.",
        ]
    return "\n".join(lines)


def parse_output(
    text: str, schema_id: str, allowed_ids: set[str]
) -> tuple[BaseModel | None, ComponentStatus, str | None]:
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", body)
    try:
        assert_no_forbidden(body)
    except ForbiddenOutputError as exc:
        return None, ComponentStatus.BLOCKED, str(exc)[:200]
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        return None, ComponentStatus.MALFORMED, "response is not valid JSON"
    try:
        out = SCHEMAS[schema_id].model_validate(raw)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "root" for e in exc.errors()})
        return None, ComponentStatus.MALFORMED, f"schema violation in: {', '.join(fields)[:160]}"
    cited = {
        i
        for name in ("evidence_ids", "supporting_evidence_ids", "contradicting_evidence_ids")
        for i in getattr(out, name, ())
    }
    unknown = cited - allowed_ids
    if unknown:
        return None, ComponentStatus.MALFORMED, f"unknown evidence ids: {sorted(unknown)[:5]}"
    return out, ComponentStatus.OK, None


def _observation_fields(out: BaseModel) -> dict[str, Any]:
    d = out.model_dump()
    if "score" in d:  # analyst/1
        return {k: d[k] for k in ("score", "confidence", "summary", "points", "evidence_ids")}
    if "risk_score" in d:
        return {
            "risk_score": d["risk_score"],
            "confidence": d["confidence"],
            "summary": d["summary"],
            "points": d["risks"],
            "evidence_ids": d["evidence_ids"],
        }
    if "argument" in d:
        return {
            "confidence": d["conviction"],
            "summary": d["argument"],
            "points": d["points"],
            "evidence_ids": d["evidence_ids"],
        }
    if "contradictions" in d:
        return {
            "summary": d["summary"],
            "points": (*d["contradictions"], *d["unsupported_claims"])[:6],
            "evidence_ids": d["evidence_ids"],
        }
    return {  # synthesis/1
        "confidence": d["ai_confidence"],
        "summary": d["consensus"],
        "points": d["risks"],
        "evidence_ids": (*d["supporting_evidence_ids"], *d["contradicting_evidence_ids"])[:16],
    }


def run_agent(
    spec: AgentSpec,
    ctx: AgentContext,
    *,
    symbol: str | None,
    evidence: Sequence[Evidence],
    context_blocks: Sequence[tuple[str, Any]] = (),
    tier: Tier | None = None,
    unavailable_reason: str = "no evidence available",
) -> AgentRun:
    model = ctx.deep if (tier or spec.tier) == "deep" else ctx.quick
    if not evidence:
        return AgentRun(
            AgentObservation(
                agent_id=spec.agent_id,
                symbol=symbol,
                status=ComponentStatus.UNAVAILABLE,
                prompt_version=spec.prompt_version,
                error=unavailable_reason[:300],
            )
        )
    allowed = sorted({e.evidence_id for e in evidence})
    blocks = [(f"evidence/{spec.agent_id}", evidence_payload(evidence)), *context_blocks]
    subject = subject_label(symbol, ctx.market, ctx.mask_identifiers)

    correction: str | None = None
    calls = tokens_in = tokens_out = 0
    latency = 0.0
    retries_left = ctx.max_retries
    while True:
        prompt = render_prompt(
            spec, subject=subject, blocks=blocks, allowed_ids=allowed, correction=correction
        )
        request = LLMRequest(model.model_id, SYSTEM_PROMPT, prompt, ctx.max_tokens, ctx.temperature)
        res = ctx.gateway.call(
            request,
            agent_id=spec.agent_id,
            symbol=symbol,
            prompt_version=spec.prompt_version,
            prompt_hash=spec.prompt_hash,
        )
        calls += res.attempts
        if res.response is not None:
            tokens_in += res.response.input_tokens
            tokens_out += res.response.output_tokens
            latency += res.response.latency_ms
        meta = {
            "agent_id": spec.agent_id,
            "symbol": symbol,
            "model_id": model.model_id,
            "prompt_version": spec.prompt_version,
            "prompt_hash": spec.prompt_hash,
            "llm_calls": calls,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "latency_ms": round(latency, 3),
        }
        if res.status is not ComponentStatus.OK:
            obs = AgentObservation(status=res.status, error=redact(res.error or "")[:300], **meta)
            return AgentRun(obs)
        out, status, err = parse_output(res.response.text, spec.schema_id, set(allowed))
        if out is not None:
            return AgentRun(
                AgentObservation(status=ComponentStatus.OK, **meta, **_observation_fields(out)),
                out,
            )
        if status is ComponentStatus.BLOCKED or retries_left == 0:
            return AgentRun(AgentObservation(status=status, error=err, **meta))
        retries_left -= 1
        correction = err


def run_analyst(
    spec: AgentSpec, ctx: AgentContext, *, symbol: str | None, results: Sequence[ToolResult]
) -> AgentRun:
    """Analyst = agent over tool results; UNAVAILABLE tools contribute nothing."""
    evidence = [e for r in results if r.ok for e in r.evidence]
    reasons = "; ".join(f"{r.tool}: {r.reason}" for r in results if not r.ok)
    return run_agent(
        spec, ctx, symbol=symbol, evidence=evidence, unavailable_reason=reasons or "no evidence"
    )
