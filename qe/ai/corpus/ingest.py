"""Raw NSE announcements → curated corpus (ADR-043 P9 quarantine → promote).

    backtest-data/raw/nse_announcements/ingest=<UTC>/announcements.jsonl   (scripts/, immutable, READ-ONLY here)
        │  parse → sanitize → screen → validate
        ├─► PROMOTED    → backtest-data/ai_corpus/curated/announcements.jsonl (append-only, deduped)
        ├─► QUARANTINED → backtest-data/ai_corpus/held/<ingest>.jsonl   (injection / secret / forbidden language: HUMAN review)
        └─► REJECTED    → backtest-data/ai_corpus/rejected/<ingest>.jsonl (schema / time / symbol problems, with reason)

Nothing is silently dropped: every raw record ends in exactly one bucket and the
counts are reported. Held/rejected files keep a short SANITISED excerpt (secrets
redacted) plus the raw content hash — never the raw bytes.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from qe.ai.corpus.models import Category, Document
from qe.ai.corpus.sanitize import sanitize_text, screen_raw
from qe.ai.guardrails import redact
from qe.ai.paths import AI_CORPUS_DIR, safe_write_path

RAW_RELDIR = Path("backtest-data") / "raw" / "nse_announcements"
IST = ZoneInfo("Asia/Kolkata")
MIN_DATE = datetime(2010, 1, 1, tzinfo=IST)
FUTURE_TOLERANCE = timedelta(minutes=5)
MAX_RAW_TEXT = 20_000
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&-]{0,19}$")
_FORMATS = (
    "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d",
)  # fmt: skip
_DATE_ONLY = {"%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"}

# Every NSE announcement text opens "<Company> has informed the Exchange (regarding|about|that) ...".
# That boilerplate carries no information and eats the headline budget, so it is removed.
_BOILERPLATE = re.compile(
    r"^.{0,120}?\bhas (?:informed|intimated|submitted to)(?: the)? Exchange\b\s*"
    r"(?:regarding|about|that|on|with respect to|of)?\s*",
    re.IGNORECASE,
)

# First match wins; keywords are matched on the sanitised NSE `desc` (category) text.
_CATEGORY_RULES: tuple[tuple[Category, tuple[str, ...]], ...] = (
    ("ROUTINE_FILING", ("newspaper publication", "trading window", "share certificate",
                        "esop", "esos", "esps",
                        "certificate under regulation", "analysts/institutional investor",
                        "loss of share", "duplicate")),
    ("RESULTS", ("financial result", "results", "earnings")),
    ("BOARD_MEETING", ("board meeting", "outcome of board", "intimation of board")),
    ("DIVIDEND", ("dividend",)),
    ("CORPORATE_ACTION", ("bonus", "split", "buyback", "rights issue", "record date", "demerger", "allotment")),
    ("ACQUISITION_OR_ORDER", ("acquisition", "amalgamation", "merger", "order", "contract", "award")),
    ("RATING", ("credit rating", "rating")),
    ("GOVERNANCE", ("appointment", "resignation", "cessation", "auditor", "director",
                    "shareholders meeting", "annual general meeting", "postal ballot")),
    ("REGULATORY", ("sebi", "regulation 30", "regulation 29", "clarification", "disclosure")),
)  # fmt: skip


def _category(desc: str) -> Category:
    low = desc.lower()
    for name, keys in _CATEGORY_RULES:
        if any(k in low for k in keys):
            return name
    return "OTHER"


def parse_ts(value: object) -> tuple[datetime, bool] | None:
    """(timestamp in IST, date_only) or None. A date-only value becomes 23:59:59."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    for fmt in _FORMATS:
        try:
            dt = datetime.strptime(v, fmt)
        except ValueError:
            continue
        date_only = fmt in _DATE_ONLY
        if date_only:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.replace(tzinfo=IST), date_only
    return None


@dataclass
class IngestReport:
    ingests: list[str] = field(default_factory=list)
    seen: int = 0
    promoted: int = 0
    duplicates: int = 0
    held: Counter = field(default_factory=Counter)
    rejected: Counter = field(default_factory=Counter)
    curated_total: int = 0


def _digest(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def evaluate(wrapper: dict, ingested_at: datetime) -> tuple[str, str, Document | dict]:
    """Return (bucket, reason, Document | excerpt). bucket ∈ promote|hold|reject."""
    raw_json = json.dumps(wrapper.get("record"), sort_keys=True, default=str)
    raw_sha = _digest(raw_json)
    rec = wrapper.get("record")
    if not isinstance(rec, dict):
        return "reject", "record_not_an_object", {"raw_sha256": raw_sha}
    if len(raw_json) > MAX_RAW_TEXT:
        return "reject", "oversized_record", {"raw_sha256": raw_sha}

    symbol = str(rec.get("symbol", "")).strip().upper()
    if not _SYMBOL.match(symbol):
        return "reject", "bad_or_missing_symbol", {"raw_sha256": raw_sha}
    try:
        fetched_at = datetime.fromisoformat(str(wrapper.get("fetched_at")))
    except ValueError:
        return "reject", "bad_fetched_at", {"raw_sha256": raw_sha}
    if fetched_at.tzinfo is None:
        return "reject", "bad_fetched_at", {"raw_sha256": raw_sha}

    parsed = [parse_ts(rec.get(k)) for k in ("exchdisstime", "an_dt")]
    parsed = [p for p in parsed if p is not None]
    if not parsed:
        return "reject", "missing_or_unparseable_timestamp", {"raw_sha256": raw_sha}
    knowledge_ts = max(p[0] for p in parsed)  # the LATER time is when it was surely public
    if knowledge_ts < MIN_DATE:
        return "reject", "timestamp_before_2010", {"raw_sha256": raw_sha}
    if knowledge_ts > fetched_at + FUTURE_TOLERANCE:
        return "reject", "future_dated_vs_fetch_time", {"raw_sha256": raw_sha}

    desc = sanitize_text(rec.get("desc"), 120)
    text = sanitize_text(rec.get("attchmntText") or rec.get("desc"), 600)
    stripped = _BOILERPLATE.sub("", text).strip(" .,:;-'\"")
    headline = (stripped or text)[:200]
    body = sanitize_text(rec.get("attchmntText"), 600)
    if not headline:
        return "reject", "empty_text_after_sanitizing", {"raw_sha256": raw_sha}

    reasons = screen_raw(f"{rec.get('desc')} {rec.get('attchmntText')}")
    if reasons:
        excerpt = {
            "symbol": symbol,
            "excerpt": redact(headline)[:160],
            "raw_sha256": raw_sha,
            "reasons": reasons,
        }
        return "hold", ",".join(reasons), excerpt

    doc = Document(
        doc_id=f"doc-{_digest('NSE', symbol, knowledge_ts.isoformat(), headline)[:16]}",
        source="NSE_ANNOUNCEMENTS",
        symbol=symbol,
        category=_category(f"{desc} {headline}"),
        subject=desc,
        headline=headline,
        body=body if body != headline else "",
        knowledge_ts=knowledge_ts,
        fetched_at=fetched_at,
        ingested_at=ingested_at,
        raw_sha256=raw_sha,
    )
    return "promote", "", doc


def _raw_files(base_dir: Path) -> list[Path]:
    root = base_dir / RAW_RELDIR
    return sorted(root.glob("ingest=*/announcements.jsonl")) if root.exists() else []


def curated_path(base_dir: Path) -> Path:
    return safe_write_path(base_dir, AI_CORPUS_DIR / "curated" / "announcements.jsonl")


def ingest(base_dir: str | Path = ".", *, now: datetime | None = None) -> IngestReport:
    base = Path(base_dir)
    now = now or datetime.now(UTC)
    curated = curated_path(base)
    known: set[str] = set()
    if curated.exists():
        known = {
            json.loads(line)["doc_id"] for line in curated.read_text().splitlines() if line.strip()
        }
    report = IngestReport(curated_total=len(known))
    for raw in _raw_files(base):
        name = raw.parent.name
        held_lines, rejected_lines, new_docs = [], [], []
        for line in raw.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            report.seen += 1
            try:
                wrapper = json.loads(line)
            except json.JSONDecodeError:
                report.rejected["invalid_json_line"] += 1
                rejected_lines.append({"reason": "invalid_json_line"})
                continue
            bucket, reason, payload = evaluate(wrapper if isinstance(wrapper, dict) else {}, now)
            if bucket == "promote":
                if payload.doc_id in known:
                    report.duplicates += 1
                    continue
                known.add(payload.doc_id)
                new_docs.append(payload)
                report.promoted += 1
            elif bucket == "hold":
                report.held[reason.split(",")[0]] += 1
                held_lines.append({"reason": reason, **payload})
            else:
                report.rejected[reason] += 1
                rejected_lines.append({"reason": reason, **payload})
        report.ingests.append(name)
        if new_docs:
            curated.parent.mkdir(parents=True, exist_ok=True)
            with open(curated, "a", encoding="utf-8") as fh:
                for d in new_docs:
                    fh.write(json.dumps(d.model_dump(mode="json"), sort_keys=True) + "\n")
        for label, lines in (("held", held_lines), ("rejected", rejected_lines)):
            if lines:
                out = safe_write_path(base, AI_CORPUS_DIR / label / f"{name}.jsonl")
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in lines))
    report.curated_total = len(known)
    return report
