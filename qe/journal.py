"""Append-only event journal — the engine's single source of observability.

One JSONL file per session. The first record is always SESSION_START carrying
config hash + full config echo + code SHA + data snapshot id; the last is
SESSION_END. Every record has a monotonic ``seq`` so truncation and gaps are
detectable. Session reports, monitoring, replay, and the advisory layer are all
readers of this file — nothing else is ground truth.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any


class JournalError(RuntimeError):
    pass


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class JournalWriter:
    """Writes one session journal. The file must not already exist (a journal
    is never appended to across processes — that would break seq integrity)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fh = open(self.path, "x", encoding="utf-8")
        except FileExistsError as exc:
            raise JournalError(f"journal already exists: {self.path}") from exc
        self._seq = 0
        self._closed = False

    def write(self, event_type: str, data: dict[str, Any]) -> int:
        if self._closed:
            raise JournalError("journal is closed")
        record = {"seq": self._seq, "ts": _utc_now_iso(), "type": event_type, "data": data}
        self._fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        self._fh.flush()
        self._seq += 1
        return record["seq"]

    def session_start(
        self,
        *,
        session_id: str,
        mode: str,
        config_hash: str,
        config: dict[str, Any],
        code_sha: str,
        data_snapshot_id: str | None,
    ) -> None:
        if self._seq != 0:
            raise JournalError("SESSION_START must be the first record")
        self.write(
            "SESSION_START",
            {
                "session_id": session_id,
                "mode": mode,
                "config_hash": config_hash,
                "config": config,
                "code_sha": code_sha,
                "data_snapshot_id": data_snapshot_id,
            },
        )

    def session_end(self, status: str, summary: dict[str, Any]) -> None:
        self.write("SESSION_END", {"status": status, **summary})
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._fh.close()
            self._closed = True

    def __enter__(self) -> "JournalWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # On error, stamp the failure into the journal instead of dying silently.
        if exc_type is not None and not self._closed:
            self.write("SESSION_ABORT", {"error": f"{exc_type.__name__}: {exc}"})
        self.close()


def read_journal(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield records, enforcing monotonic seq (fail-closed on corruption)."""
    expected = 0
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise JournalError(f"{path}:{line_no}: invalid JSON") from exc
            if record.get("seq") != expected:
                raise JournalError(
                    f"{path}:{line_no}: seq {record.get('seq')} != expected {expected}"
                )
            expected += 1
            yield record


def read_header(path: str | Path) -> dict[str, Any]:
    """Return the SESSION_START record's data payload."""
    for record in read_journal(path):
        if record["type"] != "SESSION_START":
            raise JournalError(f"{path}: first record is {record['type']}, not SESSION_START")
        return record["data"]
    raise JournalError(f"{path}: empty journal")
