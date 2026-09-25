"""Pre-trade risk pipeline: ordered pure checks over a rebalance proposal.

The v1 risk_engine's validators as in-process functions (RA-1 §2.4). Every
verdict — approve or reject, with per-check detail — is journaled by the
engine. A rejected proposal means the rebalance does not execute; the book
carries unchanged. Fail-closed: any check crash is a rejection.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Proposal:
    """A fully-sized rebalance the risk pipeline can veto."""

    weights: dict[str, float]
    target_qty: dict[str, int]
    prices: dict[str, float]
    nav: float
    projected_cash: float
    max_weight: float
    turnover_frac: float = 0.0  # traded notional / NAV for this rebalance
    max_positions: int | None = None  # None = check off
    max_turnover_frac: float | None = None  # None = check off


@dataclass(frozen=True)
class CheckResult:
    check: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class Verdict:
    approved: bool
    results: tuple[CheckResult, ...] = field(default=())

    @property
    def rejections(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok]


def check_long_only(p: Proposal) -> CheckResult:
    shorts = [s for s, q in p.target_qty.items() if q < 0]
    return CheckResult("long_only", not shorts, f"short targets: {shorts}" if shorts else "")


def check_weight_cap(p: Proposal) -> CheckResult:
    eps = 1e-9
    over = [s for s, w in p.weights.items() if w > p.max_weight + eps]
    return CheckResult("weight_cap", not over, f"over {p.max_weight:.2%}: {over}" if over else "")


def check_gross_exposure(p: Proposal) -> CheckResult:
    gross = sum(abs(w) for w in p.weights.values())
    return CheckResult(
        "gross_exposure",
        gross <= 1.0 + 1e-9,
        f"gross {gross:.4f} > 1.0" if gross > 1.0 + 1e-9 else "",
    )


def check_cash_non_negative(p: Proposal) -> CheckResult:
    ok = p.projected_cash >= 0.0
    return CheckResult(
        "cash_non_negative", ok, "" if ok else f"projected cash ₹{p.projected_cash:,.2f} < 0"
    )


def check_max_positions(p: Proposal) -> CheckResult:
    if p.max_positions is None:
        return CheckResult("max_positions", True, "off")
    n = sum(1 for q in p.target_qty.values() if q > 0)
    return CheckResult(
        "max_positions",
        n <= p.max_positions,
        f"{n} > {p.max_positions}" if n > p.max_positions else "",
    )


def check_max_turnover(p: Proposal) -> CheckResult:
    if p.max_turnover_frac is None:
        return CheckResult("max_turnover", True, "off")
    ok = p.turnover_frac <= p.max_turnover_frac + 1e-9
    return CheckResult(
        "max_turnover",
        ok,
        "" if ok else f"turnover {p.turnover_frac:.2%} > {p.max_turnover_frac:.2%}",
    )


DEFAULT_CHECKS = (
    check_long_only,
    check_weight_cap,
    check_gross_exposure,
    check_cash_non_negative,
    check_max_positions,
    check_max_turnover,
)


def evaluate(proposal: Proposal, checks=DEFAULT_CHECKS) -> Verdict:
    results = []
    for check in checks:
        try:
            results.append(check(proposal))
        except Exception as exc:  # fail-closed: a crashing check is a rejection
            results.append(CheckResult(check.__name__, False, f"check error: {exc}"))
    return Verdict(approved=all(r.ok for r in results), results=tuple(results))
