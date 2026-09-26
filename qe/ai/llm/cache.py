"""Content-addressed LLM response cache (dedupe + deterministic replay).

The key covers everything that determines a response: model, prompt version,
system + user text, max_tokens, temperature. Prompts never embed run ids,
trace ids, or wall-clock time, so identical research is never paid for twice
and a rerun replays byte-identically from cache.
"""

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

from qe.ai.llm.base import LLMRequest, LLMResponse
from qe.ai.paths import AI_CACHE_DIR, safe_write_path


def cache_key(request: LLMRequest, prompt_version: str) -> str:
    payload = json.dumps(
        {
            "model_id": request.model_id,
            "prompt_version": prompt_version,
            "system": request.system,
            "prompt": request.prompt,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "effort": request.effort,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ResponseCache:
    """On-disk under ``backtest-data/ai_cache/llm`` when ``base_dir`` is given,
    otherwise in-memory (tests)."""

    def __init__(self, base_dir: str | Path | None = None):
        self._dir = safe_write_path(base_dir, AI_CACHE_DIR / "llm") if base_dir else None
        self._mem: dict[str, dict] = {}

    def get(self, key: str) -> LLMResponse | None:
        raw = self._mem.get(key)
        if raw is None and self._dir is not None:
            path = self._dir / key[:2] / f"{key}.json"
            if path.exists():
                raw = json.loads(path.read_text())
        if raw is None:
            return None
        return LLMResponse(**{**raw, "cached": True, "latency_ms": 0.0})

    def put(self, key: str, response: LLMResponse) -> None:
        raw = {k: v for k, v in asdict(response).items() if k != "cached"}
        self._mem[key] = raw
        if self._dir is not None:
            path = self._dir / key[:2] / f"{key}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(raw, sort_keys=True))
            os.replace(tmp, path)
