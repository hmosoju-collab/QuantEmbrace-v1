"""Curated announcement document (ADR-043 P9).

``knowledge_ts`` is when the item became PUBLIC: the later of the exchange
dissemination time and the announcement time; a date-only item is treated as
known at 23:59:59 IST that day (conservative — never usable before the next
session's close). ``fetched_at`` is recorded for audit and must not precede it.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator

from qe.config import FrozenModel

Category = Literal[
    "RESULTS", "BOARD_MEETING", "DIVIDEND", "CORPORATE_ACTION", "ACQUISITION_OR_ORDER",
    "RATING", "GOVERNANCE", "REGULATORY", "ROUTINE_FILING", "OTHER",
]  # fmt: skip
Symbol = Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9&-]{0,19}$")]


class Document(FrozenModel):
    schema_version: Literal["corpus_document/1"] = "corpus_document/1"
    doc_id: Annotated[str, Field(pattern=r"^doc-[0-9a-f]{16}$")]
    source: Literal["NSE_ANNOUNCEMENTS"]
    symbol: Symbol
    category: Category
    subject: Annotated[str, Field(max_length=120)] = ""  # NSE's own subject label (desc)
    headline: Annotated[str, Field(min_length=1, max_length=200)]
    body: Annotated[str, Field(max_length=600)] = ""  # stored for humans; NOT put in prompts
    knowledge_ts: datetime
    fetched_at: datetime
    ingested_at: datetime
    # Official exchange feed ⇒ HIGH provenance trust (like bhavcopy), but the
    # TEXT is issuer-authored and is always treated as untrusted content.
    trust_level: Literal["HIGH"] = "HIGH"
    content_trust: Literal["untrusted"] = "untrusted"
    raw_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("knowledge_ts", "fetched_at", "ingested_at")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return v
