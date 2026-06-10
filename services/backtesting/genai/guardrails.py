"""Guardrails for the QuantEmbrace backtesting GenAI layer.

The GenAI layer is **advisory only**. These guardrails enforce the hard rules:

    * **No secrets** ever enter a prompt (scan + redact).
    * **No forbidden action** (enable live, place orders, mutate config/capital,
      auto-promote) may appear in a request or be emitted in a response.
    * **Reports must cite sources.**
    * **Risk governance blocks** when evidence is insufficient — it can never
      authorise promotion (that is always a human decision).

Pure functions — no I/O, no broker APIs, no LLM calls.
"""

from __future__ import annotations

import re

# ── secrets ──────────────────────────────────────────────────────────────────

_SECRET_PATTERNS: list[re.Pattern] = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                                   # AWS access key id
    re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*\S+"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),                             # Anthropic/OpenAI-style key
    re.compile(r"(?i)\b(api[_-]?key|secret|password|access[_-]?token|bearer)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)zerodha[_-]?access[_-]?token\s*[:=]\s*\S+"),
]

_REDACTION = "[REDACTED]"


class SecretLeakError(Exception):
    """Raised when a secret would enter a prompt."""


class ForbiddenActionError(Exception):
    """Raised when a forbidden action appears in a request or response."""


class CitationError(Exception):
    """Raised when a report lacks cited sources."""


def scan_for_secrets(text: str) -> list[str]:
    found: list[str] = []
    for pat in _SECRET_PATTERNS:
        found.extend(m.group(0) for m in pat.finditer(text or ""))
    return found


def redact_secrets(text: str) -> str:
    out = text or ""
    for pat in _SECRET_PATTERNS:
        out = pat.sub(_REDACTION, out)
    return out


def assert_no_secrets(text: str) -> None:
    leaks = scan_for_secrets(text)
    if leaks:
        raise SecretLeakError(f"Refusing to send {len(leaks)} secret(s) to the LLM.")


# ── forbidden actions ────────────────────────────────────────────────────────

_FORBIDDEN_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?i)enable\s+live"),
    re.compile(r"(?i)go[\s_-]?live"),
    re.compile(r"(?i)live_trading_enabled\s*[:=]\s*true"),
    re.compile(r"(?i)QE_EXECUTION_LIVE_TRADING_ENABLED\s*[:=]\s*true"),
    re.compile(r"(?i)place\s+(an?\s+)?order"),
    re.compile(r"(?i)\bsubmit_order\b|\bplace_order\b"),
    re.compile(r"(?i)(auto[\s_-]?promote|promote\s+.*\blive\b)"),
    re.compile(r"(?i)(set|change|increase|decrease|adjust)\s+capital"),
    re.compile(r"(?i)mutate\s+(config|capital|table)"),
]


def detect_forbidden_actions(text: str) -> list[str]:
    hits: list[str] = []
    for pat in _FORBIDDEN_PATTERNS:
        hits.extend(m.group(0) for m in pat.finditer(text or ""))
    return hits


def assert_no_forbidden_action(text: str) -> None:
    hits = detect_forbidden_actions(text)
    if hits:
        raise ForbiddenActionError(
            f"Forbidden action(s) detected (advisory layer cannot act): {hits}"
        )


# ── citations ────────────────────────────────────────────────────────────────


def ensure_sources_section(report_text: str, sources: list[dict]) -> str:
    """Append a Sources section (if missing) listing the cited source ids/refs."""
    if not sources:
        raise CitationError("A report must be generated from at least one cited source.")
    if "## Sources" in report_text or "Sources:" in report_text:
        return report_text
    lines = ["", "## Sources", ""]
    for s in sources:
        sid = s.get("id", "?")
        ref = s.get("ref", "")
        lines.append(f"- `{sid}`{(' — ' + ref) if ref else ''}")
    return report_text.rstrip() + "\n" + "\n".join(lines) + "\n"


def assert_cited(report_text: str, sources: list[dict]) -> None:
    if not sources:
        raise CitationError("No sources provided — report is uncited.")
    has_section = "## Sources" in report_text or "Sources:" in report_text
    has_id = any(str(s.get("id", "")) in report_text for s in sources)
    if not (has_section or has_id):
        raise CitationError("Report does not cite any of the provided sources.")


# ── risk-governance evidence sufficiency ─────────────────────────────────────

# Mirrors CLAUDE.md § Strategy Performance Live-Readiness Rule.
MIN_VALID_SESSIONS = 5


def evaluate_evidence(evidence: dict) -> dict:
    """Return an advisory governance verdict. NEVER authorises promotion.

    verdict ∈ {BLOCK, ADVISORY_OK}. ADVISORY_OK means evidence *supports continued
    evaluation* — promotion remains a manual operator decision.
    """
    reasons: list[str] = []
    valid_sessions = int(evidence.get("valid_sessions", 0))
    if valid_sessions < MIN_VALID_SESSIONS:
        reasons.append(f"valid_sessions {valid_sessions} < {MIN_VALID_SESSIONS}")
    if not evidence.get("oos_gates_pass", False):
        reasons.append("OOS gates not passed")
    if float(evidence.get("expectancy", 0.0)) <= 0:
        reasons.append("expectancy <= 0")
    if float(evidence.get("profit_factor", 0.0)) <= 1.2:
        reasons.append("profit_factor <= 1.2")
    if float(evidence.get("realized_pnl", 0.0)) <= 0:
        reasons.append("realized_pnl <= 0")
    if int(evidence.get("reconciliation_mismatches", 1)) != 0:
        reasons.append("reconciliation mismatches present")

    if reasons:
        return {
            "verdict": "BLOCK",
            "recommend_promotion": False,
            "reasons": reasons,
            "note": "Insufficient evidence. Promotion is blocked and remains a manual operator decision.",
        }
    return {
        "verdict": "ADVISORY_OK",
        "recommend_promotion": False,  # advisory layer NEVER recommends auto-promotion
        "reasons": [],
        "note": "Evidence supports continued evaluation. Promotion still requires human operator sign-off.",
    }
