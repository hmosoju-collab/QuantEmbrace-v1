"""Live gate — the evidence ceremony that unblocks live trading, by construction.

RA-1 §2.4 / M6: paper/live isolation is a property of the type system. The live
broker is unconstructible without a ``LiveGateToken``, and a token can only be
minted by ``mint_live_gate_token`` when EVERY pre-registered precondition passes.
The preconditions read real evidence (forward-factor months, clean paper
sessions, a signed human approval, kill-switch state, lake freshness) and fail
closed. Today they do not pass — so the ceremony correctly REFUSES, and live
trading is blocked not by policy but because no valid token can exist.

Nothing here places an order. Minting a token is necessary but not sufficient:
`LiveBroker` also requires an explicitly-supplied broker client, so no code path
can trade on the strength of the token alone.

Governance invariant (verbatim): backtesting recommends; a human promotes. The
gate must never be relaxed to force a pass.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

# Where the human-produced evidence artifacts live (version-controlled, reviewable).
GATE_DIR = Path("governance") / "live-gate"
OPERATOR_APPROVAL = "operator-approval.json"  # explicit human sign-off
FORWARD_GATE_PASS = "forward-gate-pass.json"  # recorded after check_forward_gate PASSES

# Pre-registered thresholds (mirror the Forward Factor Gate; DO NOT relax).
MIN_FORWARD_MONTHS = 12
MIN_CLEAN_PAPER_SESSIONS = 3
MAX_LAKE_AGE_DAYS = 7
TOKEN_TTL_HOURS = 24


class LiveGateRefused(RuntimeError):
    """Raised by the ceremony when any precondition fails. Carries the checks."""

    def __init__(self, checks: list["GateCheck"]):
        self.checks = checks
        failed = [c.name for c in checks if not c.passed]
        super().__init__(f"live gate REFUSED — failed: {', '.join(failed) or 'none?'}")


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class LiveGateToken:
    issued_utc: str
    expires_utc: str
    config_hash: str  # the token authorizes exactly ONE config
    evidence_digest: str  # sha256 over the passing checks
    approved_by: str

    def is_valid(self, config_hash: str, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        try:
            expires = datetime.fromisoformat(self.expires_utc)
        except ValueError:
            return False
        return self.config_hash == config_hash and now < expires


# ── precondition checks (pure, fail-closed) ───────────────────────────────────


def _count_clean_paper_sessions(journal_dir: Path) -> int:
    """Distinct month-ends with a clean, rebalanced paper session (status OK)."""
    from qe.reporting import session_report

    if not journal_dir.is_dir():
        return 0
    done: set[str] = set()
    for jp in journal_dir.glob("paper-*.jsonl"):
        try:
            rep = session_report(jp)
        except Exception:
            continue
        final = rep.get("final") or {}
        if rep.get("status") == "OK" and final.get("rebalanced") and final.get("as_of"):
            done.add(final["as_of"])
    return len(done)


def _forward_months(base_dir: Path, factor: str) -> int:
    """Most complete-month count across this factor's qe study summaries."""
    reports = base_dir / "reports" / "qe"
    if not reports.is_dir():
        return 0
    best = 0
    for summ in reports.glob("*/summary.json"):
        try:
            data = json.loads(summ.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("factor") == factor:
            best = max(best, int(data.get("n_full", 0)))
    return best


def evaluate_live_preconditions(
    *,
    base_dir: str | Path,
    config_hash: str,
    factor: str,
    lake_age_days: float | None,
    kill_active: bool,
) -> list[GateCheck]:
    base_dir = Path(base_dir)
    gate_dir = base_dir / GATE_DIR
    checks: list[GateCheck] = []

    # 1. Forward-factor gate recorded PASS (human artifact, bound to this config).
    fg = gate_dir / FORWARD_GATE_PASS
    if not fg.exists():
        checks.append(
            GateCheck("forward_gate_pass", False, f"missing {fg} (run check_forward_gate first)")
        )
    else:
        try:
            rec = json.loads(fg.read_text())
            ok = rec.get("result") == "PASS" and rec.get("config_hash") == config_hash
            checks.append(
                GateCheck(
                    "forward_gate_pass",
                    ok,
                    "recorded PASS" if ok else "present but not a PASS for this config",
                )
            )
        except (OSError, json.JSONDecodeError) as e:
            checks.append(GateCheck("forward_gate_pass", False, f"unreadable: {e}"))

    # 2. Independent re-verification: >=12 complete forward months in qe studies.
    months = _forward_months(base_dir, factor)
    checks.append(
        GateCheck(
            "forward_months",
            months >= MIN_FORWARD_MONTHS,
            f"{months}/{MIN_FORWARD_MONTHS} complete months",
        )
    )

    # 3. Clean paper sessions accumulated.
    n = _count_clean_paper_sessions(base_dir / "journals")
    checks.append(
        GateCheck(
            "clean_paper_sessions",
            n >= MIN_CLEAN_PAPER_SESSIONS,
            f"{n}/{MIN_CLEAN_PAPER_SESSIONS} clean sessions",
        )
    )

    # 4. Explicit human approval bound to this exact config.
    ap = gate_dir / OPERATOR_APPROVAL
    if not ap.exists():
        checks.append(GateCheck("operator_approval", False, f"missing {ap}"))
    else:
        try:
            rec = json.loads(ap.read_text())
            ok = (
                rec.get("approved") is True
                and rec.get("config_hash") == config_hash
                and bool(rec.get("operator"))
            )
            checks.append(
                GateCheck(
                    "operator_approval",
                    ok,
                    (
                        f"approved by {rec.get('operator')}"
                        if ok
                        else "not a valid approval for this config"
                    ),
                )
            )
        except (OSError, json.JSONDecodeError) as e:
            checks.append(GateCheck("operator_approval", False, f"unreadable: {e}"))

    # 5. Kill switch clear.
    checks.append(
        GateCheck("kill_switch_clear", not kill_active, "active" if kill_active else "clear")
    )

    # 6. Lake fresh (fail-closed if unknown).
    fresh = lake_age_days is not None and lake_age_days <= MAX_LAKE_AGE_DAYS
    checks.append(
        GateCheck(
            "lake_fresh",
            fresh,
            f"{lake_age_days}d old" if lake_age_days is not None else "unknown age",
        )
    )
    return checks


def mint_live_gate_token(
    *,
    base_dir: str | Path,
    config_hash: str,
    factor: str,
    lake_age_days: float | None,
    kill_active: bool,
    approved_by: str,
) -> LiveGateToken:
    """Mint a token IFF every precondition passes; otherwise raise LiveGateRefused."""
    checks = evaluate_live_preconditions(
        base_dir=base_dir,
        config_hash=config_hash,
        factor=factor,
        lake_age_days=lake_age_days,
        kill_active=kill_active,
    )
    if not all(c.passed for c in checks):
        raise LiveGateRefused(checks)

    now = datetime.now(UTC)
    digest = hashlib.sha256(
        json.dumps([asdict(c) for c in checks], sort_keys=True).encode()
    ).hexdigest()
    token = LiveGateToken(
        issued_utc=now.isoformat(timespec="seconds"),
        expires_utc=(now + timedelta(hours=TOKEN_TTL_HOURS)).isoformat(timespec="seconds"),
        config_hash=config_hash,
        evidence_digest=digest,
        approved_by=approved_by,
    )
    out = Path(base_dir) / GATE_DIR / f"token-{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(asdict(token), indent=2, sort_keys=True))
    return token
