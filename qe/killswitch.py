"""Kill switch v2 — one in-process state machine, one persisted flag.

RA-1 F-8 / M4: the v1 kill switch was a Kafka topic + a DynamoDB flag + a
per-service listener in every service + auto-trigger monitors, and it self-refired
four separate ways. Here it is a single object the engine consults before every
order emission, backed by one JSON file. Activation is idempotent — re-activating
an already-active switch returns the existing state and does NOT re-fire — which is
what structurally retires the self-refire bug class.

Auto-triggers are pure predicates evaluated by the engine loop; tripping one just
calls ``activate`` (idempotently). No topics, no listeners, no background tasks.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path


@dataclass(frozen=True)
class KillState:
    active: bool
    reason: str | None = None
    activated_by: str | None = None
    activated_at: str | None = None

    @classmethod
    def inactive(cls) -> "KillState":
        return cls(active=False)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class KillSwitch:
    """Persisted, idempotent halt flag. ``path`` is the single source of truth."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def state(self) -> KillState:
        if not self.path.exists():
            return KillState.inactive()
        return KillState(**json.loads(self.path.read_text()))

    def is_active(self) -> bool:
        return self.state().active

    def activate(self, reason: str, by: str) -> KillState:
        """Halt trading. Idempotent: if already active, the ORIGINAL activation
        (reason/by/time) is preserved and returned — no re-fire, no overwrite."""
        current = self.state()
        if current.active:
            return current
        new = KillState(active=True, reason=reason, activated_by=by, activated_at=_now())
        self._write(new)
        return new

    def deactivate(self, by: str) -> KillState:
        """Resume trading. Idempotent: deactivating an inactive switch is a no-op."""
        new = KillState.inactive()
        self._write(new)
        return new

    def _write(self, state: KillState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(state), sort_keys=True))


# ── auto-trigger predicates (pure; return a reason string or None) ─────────────


def drawdown_trigger(nav: float, peak_nav: float, max_dd_frac: float | None) -> str | None:
    """Trip if drawdown from peak NAV breaches ``max_dd_frac`` (e.g. 0.25)."""
    if max_dd_frac is None or peak_nav <= 0:
        return None
    dd = nav / peak_nav - 1.0
    if dd <= -abs(max_dd_frac):
        return f"drawdown {dd:.1%} breached limit -{abs(max_dd_frac):.0%} (nav {nav:,.0f} / peak {peak_nav:,.0f})"
    return None


def staleness_trigger(data_age_days: float, max_age_days: float | None) -> str | None:
    """Trip if the freshest available data is older than ``max_age_days`` — the
    fail-closed guard against trading on stale prices (paper/live)."""
    if max_age_days is None:
        return None
    if data_age_days > max_age_days:
        return f"data stale: {data_age_days:.0f}d old > limit {max_age_days:.0f}d"
    return None
