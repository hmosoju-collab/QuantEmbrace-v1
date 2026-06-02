"""Log collector — scans recent log files for error patterns, read-only.

Opt-in via ``rules.logs.enabled`` (paths are deployment-specific). For each
configured path/glob it tails up to ``max_bytes_per_file`` from the END of files
modified within ``window_seconds`` and counts substring matches for each pattern
in ``error_patterns``. Files are opened read-only; nothing is ever written.

Coarse status: any matches → DEGRADED; clean → OK; nothing readable → UNKNOWN.
Sample lines are truncated and capped, and are kept only in ``details`` (the Slack
notifier sends summaries, never raw ``details``) so log contents never leak to an
alert channel.
"""

from __future__ import annotations

import glob
import logging
import os
import time
from typing import Any

from monitoring_agent.collectors.base import Collector
from monitoring_agent.snapshot import CollectorResult, Status

logger = logging.getLogger("monitoring_agent.collectors.logs")

_MAX_SAMPLES = 5
_SAMPLE_TRUNCATE = 300


class LogCollector(Collector):
    """Scan recent log files for error patterns (opt-in, read-only)."""

    name = "logs"

    def enabled(self) -> bool:
        return self.rules.logs.enabled

    def _tail(self, path: str, max_bytes: int) -> str:
        """Read the last ``max_bytes`` of a file (read-only)."""
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            data = fh.read()
        return data.decode("utf-8", errors="replace")

    def _collect_sync(self) -> CollectorResult:
        rule = self.rules.logs
        if not rule.enabled:
            return self._result(Status.OK, "logs: scanning disabled", details={"enabled": False})
        if not rule.paths:
            return self._result(Status.UNKNOWN, "logs: enabled but no paths configured")

        now = time.time()
        cutoff = now - rule.window_seconds
        per_pattern: dict[str, int] = {p: 0 for p in rule.error_patterns}
        samples: list[str] = []
        scanned: list[str] = []
        skipped_stale = 0
        unreadable: list[str] = []

        expanded: list[str] = []
        for pattern_path in rule.paths:
            matches = glob.glob(pattern_path)
            expanded.extend(matches if matches else [pattern_path])

        for path in expanded:
            try:
                if os.path.getmtime(path) < cutoff:
                    skipped_stale += 1
                    continue
                content = self._tail(path, rule.max_bytes_per_file)
            except OSError as exc:
                unreadable.append(f"{path}: {exc.__class__.__name__}")
                continue

            scanned.append(path)
            for line in content.splitlines():
                for pat in rule.error_patterns:
                    if pat in line:
                        per_pattern[pat] += 1
                        if len(samples) < _MAX_SAMPLES:
                            samples.append(line[:_SAMPLE_TRUNCATE])
                        break

        total_matches = sum(per_pattern.values())
        details: dict[str, Any] = {
            "files_scanned": scanned,
            "files_unreadable": unreadable,
            "files_skipped_stale": skipped_stale,
            "window_seconds": rule.window_seconds,
            "matches_by_pattern": per_pattern,
            "total_matches": total_matches,
            "samples": samples,
        }

        if not scanned and unreadable:
            return self._result(Status.UNKNOWN, f"logs: no readable files ({len(unreadable)} unreadable)", details=details)
        if total_matches > 0:
            return self._result(
                Status.DEGRADED,
                f"logs: {total_matches} error-pattern match(es) across {len(scanned)} file(s)",
                details=details,
            )
        return self._result(Status.OK, f"logs: clean across {len(scanned)} file(s)", details=details)
