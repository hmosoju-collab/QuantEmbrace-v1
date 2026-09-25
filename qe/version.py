"""Code-version stamping for journals and run registries."""

from pathlib import Path
import subprocess


def code_version(repo_root: Path | None = None) -> str:
    """Return the git SHA of HEAD (short, 12 chars), with a ``-dirty`` suffix
    when the working tree has uncommitted changes.

    Returns ``"unknown"`` when git is unavailable — callers must still journal
    the value, never omit it.
    """
    cwd = str(repo_root) if repo_root else None
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
            timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
            timeout=10,
        ).stdout.strip()
        return f"{sha}-dirty" if status else sha
    except (OSError, subprocess.SubprocessError):
        return "unknown"
