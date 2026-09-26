"""Point-in-time reader over the curated corpus.

Curated files are NOT trusted either: every line is re-validated against the
``Document`` schema and re-screened on load, and anything that fails is dropped
and counted (tamper / schema-drift defense). ``visible(cutoff)`` returns only
documents whose ``knowledge_ts <= cutoff``, so the corpus can never leak the
future into a research prompt.
"""

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pydantic

from qe.ai.corpus.ingest import curated_path
from qe.ai.corpus.models import Document
from qe.ai.corpus.sanitize import screen_raw


@dataclass(frozen=True)
class Corpus:
    documents: dict[str, tuple[Document, ...]] = field(default_factory=dict)  # by symbol
    corpus_hash: str | None = None
    dropped_on_load: int = 0

    @property
    def loaded(self) -> bool:
        return bool(self.documents)

    def visible(self, cutoff: datetime) -> "Corpus":
        kept = {
            s: tuple(d for d in docs if d.knowledge_ts <= cutoff)
            for s, docs in self.documents.items()
        }
        return Corpus({s: d for s, d in kept.items() if d}, self.corpus_hash, self.dropped_on_load)


def load_corpus(base_dir: str | Path = ".") -> Corpus:
    path = curated_path(Path(base_dir))
    if not path.exists():
        return Corpus()
    raw = path.read_bytes()
    by_symbol: dict[str, list[Document]] = {}
    dropped = 0
    seen: set[str] = set()
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            doc = Document.model_validate(json.loads(line))
        except (json.JSONDecodeError, pydantic.ValidationError):
            dropped += 1
            continue
        if doc.doc_id in seen or screen_raw(f"{doc.headline} {doc.body}"):
            dropped += 1  # duplicate, or text no longer passes the screen
            continue
        seen.add(doc.doc_id)
        by_symbol.setdefault(doc.symbol, []).append(doc)
    docs = {s: tuple(sorted(v, key=lambda d: d.knowledge_ts)) for s, v in by_symbol.items()}
    return Corpus(docs, hashlib.sha256(raw).hexdigest()[:16], dropped)
