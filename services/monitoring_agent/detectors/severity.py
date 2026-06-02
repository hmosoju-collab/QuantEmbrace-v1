"""Fine-grained severity model — Phase 2.

Phase 1 collectors emit only a coarse :class:`~monitoring_agent.snapshot.Status`
(ok / degraded / down / unknown). Phase 2 refines those measurements into an
operator-facing severity ladder:

    INFO  <  WARNING  <  CRITICAL  <  BLOCKER

The ladder is *additive* — it never replaces the coarse ``Status`` and never
changes when an alert fires (that stays driven by the proven Status-based
incident log). Severity exists to tell an operator *how bad* and *how urgently*,
not to take any action. Phase 2 remains strictly observe-only.

Mapping intent (capital-protection ordering — a critical component being blind or
down is always worse than a non-critical one):

    * critical  DOWN              → BLOCKER  (must not trade until resolved)
    * critical  DEGRADED          → CRITICAL (impaired; page someone)
    * critical  UNKNOWN           → CRITICAL (we are blind on something critical)
    * non-critical DOWN/DEGRADED  → WARNING  (degrades the platform, not fatal)
    * non-critical UNKNOWN        → INFO     (blind on something we can live without)
    * OK                          → INFO

Detectors may also raise severity directly from numeric measurements (consumer
lag, restart counts, feed staleness, log error rates) — see ``engine.py``.
"""

from __future__ import annotations

import enum

from monitoring_agent.snapshot import Status


class Severity(str, enum.Enum):
    """Phase 2 severity ladder. ``str`` subclass so it serialises as its value."""

    INFO = "info"          # Healthy or purely informational.
    WARNING = "warning"    # Degraded / non-critical impairment — watch it.
    CRITICAL = "critical"  # Critical component impaired or blind — page someone.
    BLOCKER = "blocker"    # Critical component down — trading must not proceed.


# Ordering used to roll many findings up into one overall severity.
_SEVERITY_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.CRITICAL: 2,
    Severity.BLOCKER: 3,
}


def severity_rank(severity: Severity) -> int:
    """Numeric rank for comparisons/sorting. Higher is worse."""
    return _SEVERITY_RANK[severity]


def worst_severity(severities: list[Severity]) -> Severity:
    """Return the worst (highest-rank) severity, INFO if the list is empty."""
    if not severities:
        return Severity.INFO
    return max(severities, key=severity_rank)


def severity_for_status(status: Status, *, critical: bool) -> Severity:
    """Map a coarse :class:`Status` + criticality to a base :class:`Severity`.

    This is the default escalation used when a detector has no finer numeric
    signal to go on (e.g. a service is simply unreachable). Detectors that *do*
    have a measurement (lag, restarts, staleness) compute severity directly and
    use this only as a floor.
    """
    if status is Status.OK:
        return Severity.INFO
    if status is Status.DOWN:
        return Severity.BLOCKER if critical else Severity.WARNING
    if status is Status.DEGRADED:
        return Severity.CRITICAL if critical else Severity.WARNING
    if status is Status.UNKNOWN:
        return Severity.CRITICAL if critical else Severity.INFO
    return Severity.INFO
