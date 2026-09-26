"""Untrusted-text hygiene for the external corpus (ADR-043 P9).

Announcement text is written by the ISSUER: the feed is official, the content is
not to be trusted. Two separate jobs, in this order:

  1. ``sanitize_text``  — normalise and strip everything that can hide or smuggle
     content: NFKC (folds full-width/compat homoglyphs), HTML tags/entities, and
     every control / format / private-use / unassigned character (zero-width
     spaces, bidi overrides, tag characters …); collapse whitespace; hard length cap.
  2. ``screen``         — flag prompt-injection phrasing, secret-like strings and
     forbidden-action language on the SANITISED text (so obfuscation is undone
     first). A flagged document is quarantined for human review, never promoted.

The screen is heuristic and is defense in depth: the structural controls (no LLM
tools/actions, untrusted-data blocks, weight-0 fusion) carry the load.
"""

import html
import re
import unicodedata

from qe.ai.guardrails import detect_forbidden, scan_for_secrets

_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")
_BAD_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn"}  # control, format, surrogate, private, unassigned

_INJECTION: tuple[tuple[str, re.Pattern], ...] = tuple(
    (name, re.compile(rx, re.IGNORECASE))
    for name, rx in (
        ("ignore_instructions", r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(instruction|prompt|rule|guideline|polic|context|above|previous|prior)"),
        ("role_change", r"\b(you are now|you are no longer|act as|pretend to be|from now on you|new instructions?)\b"),
        ("system_marker", r"(^|\s)(system|assistant|developer|user)\s*:"),
        ("prompt_reference", r"\b(system prompt|developer message|hidden instruction|jailbreak)\b"),
        ("markup_marker", r"(<<<|>>>|untrusted_data|\[/?inst\]|<\|)"),
        ("tool_or_exec", r"\b(function call|tool call|execute (the )?(following|command)|run (the )?(following|command))\b"),
    )
)  # fmt: skip


_BREAKS = frozenset("\n\r\t" + chr(0x2028) + chr(0x2029))


def sanitize_text(raw: object, max_len: int, *, strip_markup: bool = True) -> str:
    """Return safe single-line text, at most ``max_len`` characters.

    ``strip_markup=False`` keeps ``<...>`` sequences (still NFKC-normalised and
    stripped of hidden characters): the screen inspects THAT form too, so markup
    the tag-stripper would delete (e.g. ``<<<END_UNTRUSTED_DATA>>>``) is still seen.
    """
    text = "" if raw is None else str(raw)
    text = html.unescape(html.unescape(text))  # twice: &amp;lt; tricks
    if strip_markup:
        text = _TAG.sub(" ", text)
    text = unicodedata.normalize("NFKC", text)
    if strip_markup:
        text = _TAG.sub(" ", text)  # tags revealed by NFKC (full-width brackets)
    text = "".join(
        " " if ch in _BREAKS else ch
        for ch in text
        if ch in _BREAKS or unicodedata.category(ch) not in _BAD_CATEGORIES
    )
    return _SPACE.sub(" ", text).strip()[:max_len]


def screen(*texts: str) -> list[str]:
    """Reasons (empty = clean) these texts must be held for human review. Pass the
    sanitised text AND its markup-preserving form; a hit in either counts."""
    reasons: list[str] = []
    for text in texts:
        for name, rx in _INJECTION:
            if rx.search(text) and f"injection:{name}" not in reasons:
                reasons.append(f"injection:{name}")
        if scan_for_secrets(text) and "secret_like" not in reasons:
            reasons.append("secret_like")
        if detect_forbidden(text) and "forbidden_action_language" not in reasons:
            reasons.append("forbidden_action_language")
    return reasons


def screen_raw(raw: object) -> list[str]:
    """The one entry point callers should use: screens BOTH the sanitised text and
    its markup-preserving form, so neither hidden characters nor markup the tag
    stripper would delete can hide a hit."""
    return screen(sanitize_text(raw, 2000), sanitize_text(raw, 2000, strip_markup=False))
