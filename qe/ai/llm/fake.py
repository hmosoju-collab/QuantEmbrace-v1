"""Deterministic offline LLM — the only backend tests ever use.

The default responder reads the ``RESPONSE_SCHEMA:`` and
``ALLOWED_EVIDENCE_IDS:`` lines every qe.ai prompt carries and returns valid
JSON for that schema. Scores are a pure function of (seed, prompt), so a run is
reproducible byte-for-byte. A ``script`` of texts / exceptions / callables is
consumed first, to simulate malformed output, timeouts, and errors.
"""

from collections.abc import Callable, Iterable
import hashlib
import json
import re

from qe.ai.llm.base import LLMRequest, LLMResponse

_SCHEMA_RE = re.compile(r"^RESPONSE_SCHEMA: (\S+)$", re.MULTILINE)
_IDS_RE = re.compile(r"^ALLOWED_EVIDENCE_IDS: (.*)$", re.MULTILINE)

ScriptItem = str | BaseException | Callable[[LLMRequest], str]


def _unit(seed: int, text: str, salt: str) -> float:
    """Deterministic pseudo-random number in [0, 1)."""
    h = hashlib.sha256(f"{seed}|{salt}|{text}".encode()).hexdigest()
    return int(h[:8], 16) / 0x1_0000_0000


def default_payload(request: LLMRequest, seed: int = 0) -> dict:
    schema_m = _SCHEMA_RE.search(request.prompt)
    schema = schema_m.group(1) if schema_m else "analyst/1"
    ids_m = _IDS_RE.search(request.prompt)
    ids = [i for i in (ids_m.group(1).split(",") if ids_m else []) if i.strip()][:2]
    score = round(2 * _unit(seed, request.prompt, "score") - 1, 3)
    conf = round(0.3 + 0.6 * _unit(seed, request.prompt, "conf"), 3)
    if schema == "analyst/1":
        return {
            "score": score,
            "confidence": conf,
            "summary": "Fake analysis (deterministic test double).",
            "points": ["fake point"],
            "evidence_ids": ids,
        }
    if schema == "risk_analyst/1":
        return {
            "risk_score": round((score + 1) / 2, 3),
            "confidence": conf,
            "summary": "Fake risk review.",
            "risks": ["fake risk"],
            "evidence_ids": ids,
        }
    if schema == "debate/1":
        return {
            "argument": "Fake debate argument.",
            "points": ["fake point"],
            "conviction": conf,
            "evidence_ids": ids,
        }
    if schema == "critic/1":
        return {
            "summary": "Fake critique.",
            "contradictions": ["fake contradiction"],
            "unsupported_claims": [],
            "evidence_ids": ids,
        }
    if schema == "synthesis/1":
        return {
            "bull_case": "Fake bull case.",
            "bear_case": "Fake bear case.",
            "consensus": "Fake consensus.",
            "ai_confidence": conf,
            "risks": ["fake risk"],
            "supporting_evidence_ids": ids[:1],
            "contradicting_evidence_ids": ids[1:2],
        }
    raise ValueError(f"FakeLLM has no default payload for schema {schema!r}")


class FakeLLM:
    name = "fake"

    def __init__(
        self,
        responder: Callable[[LLMRequest], str] | None = None,
        *,
        script: Iterable[ScriptItem] = (),
        seed: int = 0,
    ):
        self._responder = responder or (lambda r: json.dumps(default_payload(r, seed)))
        self._script = list(script)
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, BaseException):
                raise item
            text = item(request) if callable(item) else item
        else:
            text = self._responder(request)
        return LLMResponse(
            text=text,
            model_id=request.model_id,
            input_tokens=max(1, len(request.system + request.prompt) // 4),
            output_tokens=max(1, len(text) // 4),
            latency_ms=0.0,
            stop_reason="end_turn",
        )
