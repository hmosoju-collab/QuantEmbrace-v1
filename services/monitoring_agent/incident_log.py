"""Append-only incident log + state-change detection.

The agent writes its OWN observability artifacts (this incident log and the
health snapshot). That does not violate the read-only contract — the contract
forbids mutating the *trading platform* (Kafka, DynamoDB, the broker, Docker),
not writing the agent's own files.

:class:`IncidentLog` remembers the last-known status of every component and, each
cycle, appends a JSON line for every component whose status *changed*. This makes
alerting edge-triggered (alert on transition, not on every cycle) and lets the
agent be **restart-safe**: on startup it replays the existing log to restore the
last-known status per component, so a restart does not re-alert on a still-broken
component.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from monitoring_agent.snapshot import ALERTING_STATUSES, HealthSnapshot, Status

logger = logging.getLogger("monitoring_agent.incident_log")

# Local severity ranking (kept independent of snapshot internals on purpose).
_RANK: dict[Status, int] = {Status.OK: 0, Status.UNKNOWN: 1, Status.DEGRADED: 2, Status.DOWN: 3}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Transition:
    """A change in a single component's status between two cycles."""

    component: str
    previous: Optional[str]
    current: str
    summary: str
    is_recovery: bool
    alertworthy: bool


class IncidentLog:
    """Edge-triggered incident recorder backed by an append-only JSONL file."""

    def __init__(self, path: str, *, min_status: str = "degraded") -> None:
        self.path = path
        self._last: dict[str, str] = {}
        self._min_rank = _RANK.get(self._coerce_status(min_status), _RANK[Status.DEGRADED])
        self._restore()

    @staticmethod
    def _coerce_status(value: str) -> Status:
        try:
            return Status(str(value).strip().lower())
        except ValueError:
            return Status.DEGRADED

    def _restore(self) -> None:
        """Replay the existing log so a restart doesn't re-alert old problems."""
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    component = record.get("component")
                    to_status = record.get("to")
                    if component and to_status:
                        self._last[component] = to_status
            logger.info("incident_log: restored last-known status for %d component(s)", len(self._last))
        except OSError as exc:
            logger.warning("incident_log: could not replay %s: %s", self.path, exc)

    def _alertworthy(self, previous: Optional[str], current: str) -> bool:
        current_status = self._coerce_status(current)
        current_rank = _RANK.get(current_status, 1)
        if current_status in ALERTING_STATUSES and current_rank >= self._min_rank:
            return True
        # Recovery to OK from a state that would itself have alerted.
        if current_status == Status.OK and previous is not None:
            prev_status = self._coerce_status(previous)
            if prev_status in ALERTING_STATUSES and _RANK.get(prev_status, 0) >= self._min_rank:
                return True
        return False

    def record(self, snapshot: HealthSnapshot) -> list[Transition]:
        """Detect and persist status transitions; return them (all of them)."""
        transitions: list[Transition] = []
        for result in snapshot.results:
            previous = self._last.get(result.name)
            current = result.status.value
            if previous == current:
                continue
            transition = Transition(
                component=result.name,
                previous=previous,
                current=current,
                summary=result.summary,
                is_recovery=(current == Status.OK.value),
                alertworthy=self._alertworthy(previous, current),
            )
            transitions.append(transition)
            self._append(snapshot, transition)
            self._last[result.name] = current
        return transitions

    def _append(self, snapshot: HealthSnapshot, transition: Transition) -> None:
        record = {
            "ts": _utc_now_iso(),
            "component": transition.component,
            "from": transition.previous,
            "to": transition.current,
            "summary": transition.summary,
            "alertworthy": transition.alertworthy,
            "overall_status": snapshot.overall_status.value,
            "phase": snapshot.phase,
        }
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("incident_log: could not append to %s: %s", self.path, exc)
