"""Research journal: qe.journal.JournalWriter + redaction + sanitized aborts.

Same append-only, seq-checked JSONL format as engine journals (so the same
readers work), written only under ``journals/ai/`` — never ``journals/paper-*``
which ``qe.live_gate`` counts as clean-session evidence. Every payload is
recursively redacted, and an exception is sanitized before it is journaled
(``JournalWriter.__exit__`` would otherwise write the raw message).
"""

from pathlib import Path
from typing import Any

from qe.ai.config import ResearchRunConfig
from qe.ai.guardrails import redact, redact_obj
from qe.ai.paths import AI_JOURNAL_DIR, safe_write_path
from qe.journal import JournalWriter

JOURNAL_MODE = "ai-research"


class ResearchJournal:
    def __init__(self, base_dir: str | Path, run_id: str):
        self.path = safe_write_path(base_dir, AI_JOURNAL_DIR / f"{run_id}.jsonl")
        self._writer = JournalWriter(self.path)
        self._open = True

    def start(
        self, *, run_id: str, config: ResearchRunConfig, code_sha: str, snapshot_id: str
    ) -> None:
        self._writer.session_start(
            session_id=run_id,
            mode=JOURNAL_MODE,
            config_hash=config.config_hash(),
            config=redact_obj(config.model_dump(mode="json", exclude_none=True)),
            code_sha=code_sha,
            data_snapshot_id=snapshot_id,
        )

    def event(self, event_type: str, data: dict[str, Any]) -> None:
        self._writer.write(event_type, redact_obj(data))

    def end(self, status: str, summary: dict[str, Any]) -> None:
        self._writer.session_end(status, redact_obj(summary))
        self._open = False

    def __enter__(self) -> "ResearchJournal":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None and self._open:
            self._writer.write(
                "SESSION_ABORT", {"error": redact(f"{exc_type.__name__}: {exc}")[:300]}
            )
        self._writer.close()
        self._open = False
