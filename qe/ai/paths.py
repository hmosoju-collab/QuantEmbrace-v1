"""The only places qe.ai may write (ADR-043 §9).

Everything qe.ai persists goes through ``safe_write_path``. The allowlist keeps
research output away from the files the governance layer treats as evidence:
``reports/qe/*/summary.json`` (read by ``qe.live_gate`` and
``check_forward_gate.py``) and ``journals/paper-*.jsonl`` (clean paper sessions
counted by ``qe.live_gate``).
"""

from pathlib import Path

AI_JOURNAL_DIR = Path("journals") / "ai"
AI_REPORT_DIR = Path("reports") / "qe-ai"
AI_CACHE_DIR = Path("backtest-data") / "ai_cache"
_ALLOWED_ROOTS = (AI_JOURNAL_DIR, AI_REPORT_DIR, AI_CACHE_DIR)


class UnsafeWritePath(RuntimeError):
    pass


def safe_write_path(base_dir: str | Path, rel: str | Path) -> Path:
    """Resolve ``base_dir / rel`` and refuse anything outside the AI roots.

    The target is fully resolved but the allowed roots are NOT, so a symlink
    planted at (or inside) an allowed root that points at gate evidence resolves
    outside the literal root and is refused.
    """
    base = Path(base_dir).resolve()
    target = (base / rel).resolve()
    if target.name.startswith("paper-"):
        raise UnsafeWritePath(f"qe.ai may not write a paper-* file: {target}")
    for root in _ALLOWED_ROOTS:
        if (base / root) in target.parents:
            return target
    allowed = ", ".join(str(r) for r in _ALLOWED_ROOTS)
    raise UnsafeWritePath(f"qe.ai may not write {target} (allowed under: {allowed})")
