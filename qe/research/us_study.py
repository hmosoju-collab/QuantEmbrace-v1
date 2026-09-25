"""US risk-parity-lite book study: sim run + SPY buy-and-hold benchmark +
report (ADR-041 P4).

Parallel to `qe.research.study.run_factor_book_study` (NSE, EW-liquid-universe
benchmark, ₹ NAV) — kept as a separate module rather than branching that one,
since the two benchmarks and currencies are genuinely different concepts and
the NSE forward-book cadence must not risk any regression from this addition.

The ``summary.json`` shape (session_id/config_hash/.../nav_history/final_mtm/
months/book_cum/bench_cum/n_full) is intentionally identical to the NSE
study's, so `scripts/paper/check_us_forward_gate.py` can read it with the same
adapter logic as the NSE Forward Factor Gate reads `run_factor_book_study`
summaries.

Advisory only. Backtesting can recommend; it cannot promote. Live stays BLOCKED.
"""

from dataclasses import dataclass
from datetime import date
from itertools import pairwise
import json
import math
from pathlib import Path

import pandas as pd

from qe.config import RunConfig
from qe.data.panel import Panel
from qe.engine.sim import SimRunResult, run_sim
from qe.research.benchmark import buy_hold_benchmark_return
from qe.research.metrics import (
    monthly_metrics,
    nav_monthly_returns,
    per_year,
    with_walk_forward_stats,
)


@dataclass(frozen=True)
class UsStudyResult:
    sim: SimRunResult
    months: list[dict]  # {to, book, bench, alpha} per leg (last is partial)
    book_cum: float
    bench_cum: float
    n_full: int
    report_path: Path
    summary_path: Path


def run_risk_parity_study(
    config: RunConfig,
    *,
    base_dir: str | Path = ".",
    panel: Panel | None = None,
    report_dir: str | Path | None = None,
    benchmark_symbol: str = "SPY",
) -> UsStudyResult:
    base_dir = Path(base_dir)
    sim = run_sim(config, base_dir=base_dir, panel=panel)
    if len(sim.nav_history) != len(sim.rebal_positions):
        raise RuntimeError(
            "study requires every scheduled rebalance to execute "
            f"({len(sim.nav_history)}/{len(sim.rebal_positions)} ran — see journal RISK records)"
        )

    legs = [*sim.rebal_positions, sim.final_pos]
    navs = [*(nav for _, nav in sim.nav_history), sim.final_mtm[1]]
    leg_dates = [*(d for d, _ in sim.nav_history), sim.final_mtm[0]]

    bench_rets = [
        buy_hold_benchmark_return(sim.panel, benchmark_symbol, a, b) for a, b in pairwise(legs)
    ]
    book_rets = [navs[i + 1] / navs[i] - 1.0 for i in range(len(navs) - 1)]
    months = [
        {
            "to": leg_dates[i + 1].isoformat(),
            "book": book_rets[i],
            "bench": bench_rets[i],
            "alpha": book_rets[i] - bench_rets[i],
        }
        for i in range(len(book_rets))
    ]
    book_cum = navs[-1] / config.seed_nav - 1.0
    bench_cum = math.prod(1.0 + b for b in bench_rets) - 1.0
    n_full = max(len(book_rets) - 1, 0)  # final leg is a partial month (MTM)

    out_dir = Path(report_dir) if report_dir else base_dir / "reports" / "qe" / sim.session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "session_id": sim.session_id,
        "config_hash": sim.config_hash,
        "code_sha": sim.code_sha,
        "data_snapshot_id": sim.data_snapshot_id,
        "factor": config.strategy.kind if config.strategy else None,
        "benchmark_symbol": benchmark_symbol,
        "inception": config.start_date.isoformat(),
        "seed_nav": config.seed_nav,
        "nav_history": [{"date": d.isoformat(), "nav": n} for d, n in sim.nav_history],
        "final_mtm": {"date": sim.final_mtm[0].isoformat(), "nav": sim.final_mtm[1]},
        "months": months,
        "book_cum": book_cum,
        "bench_cum": bench_cum,
        "n_full": n_full,
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    report_path = out_dir / "report.md"
    report_path.write_text(
        _render_report(config, sim, months, book_cum, bench_cum, n_full, benchmark_symbol)
    )

    return UsStudyResult(
        sim=sim,
        months=months,
        book_cum=book_cum,
        bench_cum=bench_cum,
        n_full=n_full,
        report_path=report_path,
        summary_path=summary_path,
    )


def _render_report(
    config: RunConfig,
    sim: SimRunResult,
    months: list[dict],
    book_cum: float,
    bench_cum: float,
    n_full: int,
    benchmark_symbol: str,
) -> str:
    kind = config.strategy.kind if config.strategy else "?"
    lines = [
        f"# US Risk-Parity Book Study — {kind} (qe engine, ADR-041 P4)",
        "",
        "**Status:** advisory · isolated study · live trading remains BLOCKED.",
        f"**Date:** {date.today()}   **Inception:** {config.start_date}   "
        f"**Latest MTM:** {sim.final_mtm[0]}",
        "",
        "## Provenance",
        "",
        f"- session `{sim.session_id}`",
        f"- config hash `{sim.config_hash}`",
        f"- code `{sim.code_sha}` · data snapshot `{sim.data_snapshot_id}`",
        f"- journal `{sim.journal_path}`",
        "",
        f"> **Sample-size honesty:** {n_full} complete monthly returns (+1 partial) — far too",
        "> few for Sharpe/t-stat claims. Judge alpha vs the benchmark on the same months.",
        "",
        f"## Monthly returns (net of US equity costs + slippage, benchmark = buy-and-hold {benchmark_symbol})",
        "",
        f"| Month-end | Book | Benchmark ({benchmark_symbol}) | Alpha |",
        "|---|---:|---:|---:|",
    ]
    for i, r in enumerate(months):
        tag = " (partial)" if i == len(months) - 1 else ""
        lines.append(
            f"| {r['to']}{tag} | {r['book'] * 100:+.2f}% | {r['bench'] * 100:+.2f}% "
            f"| {r['alpha'] * 100:+.2f}% |"
        )
    lines += [
        "",
        f"**Cumulative:** book **{book_cum * 100:+.2f}%** vs benchmark "
        f"**{bench_cum * 100:+.2f}%** → spread **{(book_cum - bench_cum) * 100:+.2f} pts**. "
        f"NAV ${sim.final_mtm[1]:,.0f} (seed ${config.seed_nav:,.0f}).",
        "",
        "> Backtesting can recommend. It cannot promote. A human approves all production",
        "> changes. Live trading remains BLOCKED.",
        "",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class WalkForwardHalf:
    label: str
    start: date
    end: date
    sim: SimRunResult
    metrics: dict
    per_year_df: object  # pd.DataFrame; typed loosely to avoid a pandas import here


def _slice_panel(
    panel: Panel, *, before: date | None = None, at_or_after: date | None = None
) -> Panel:
    """Row-slice a Panel by its own (already market-tz-normalized) index —
    needed because `run_sim` only applies `config.end_date` via lake-loading
    (`load_panel`'s date filter); an *injected* panel is used as-is, so a
    walk-forward split over an injected panel must slice it here itself."""
    idx = panel.index
    mask = pd.Series(True, index=idx)
    if before is not None:
        mask &= idx.normalize() < pd.Timestamp(before, tz=idx.tz)
    if at_or_after is not None:
        mask &= idx.normalize() >= pd.Timestamp(at_or_after, tz=idx.tz)
    return Panel(
        close=panel.close.loc[mask],
        turnover=panel.turnover.loc[mask],
        delivery=panel.delivery.loc[mask],
    )


def run_risk_parity_walkforward(
    config: RunConfig,
    *,
    split_date: date,
    base_dir: str | Path = ".",
    panel: Panel | None = None,
) -> tuple[WalkForwardHalf, WalkForwardHalf]:
    """In-sample / out-of-sample split at ``split_date`` (ADR-041 P4).

    Runs the SAME ported qe RiskParityLiteStrategy independently over each
    half via ``run_sim`` and reports full-period + per-year stats with the
    generic ``qe.research.metrics`` helpers (no v1 cross-check — there is no
    v1 US model; NSE's `run_walk_forward_study` is untouched and unaffected).
    This checks that the strategy's historical edge holds up across separate
    multi-year sub-periods of the qe-ported engine, not just in aggregate —
    RPLITE has no fitted parameters beyond the pre-registered 63-day lookback
    (already robustness-checked at ±25% in the Phase 2 screen), so this is a
    sustained-performance check rather than a train/test split in the ML sense.

    A ``panel`` (when injected, e.g. in tests) is sliced per half — a real
    lake-backed run (``panel=None``) instead relies on `run_sim`'s own
    `config.end_date` file/date filtering for each half's config."""
    is_config = config.model_copy(update={"name": f"{config.name}-is", "end_date": split_date})
    oos_config = config.model_copy(update={"name": f"{config.name}-oos", "start_date": split_date})
    halves = []
    for label, cfg, lo, hi in (
        ("in-sample", is_config, config.start_date, split_date),
        ("out-of-sample", oos_config, split_date, config.end_date),
    ):
        half_panel = None
        if panel is not None:
            half_panel = (
                _slice_panel(panel, before=split_date)
                if label == "in-sample"
                else _slice_panel(panel, at_or_after=split_date)
            )
        sim = run_sim(cfg, base_dir=base_dir, panel=half_panel)
        monthly = nav_monthly_returns(sim.nav_history)
        py = per_year(monthly)
        m = with_walk_forward_stats(monthly_metrics(monthly), py)
        halves.append(WalkForwardHalf(label, lo, hi, sim, m, py))
    return tuple(halves)


def render_walkforward_report(halves: tuple[WalkForwardHalf, WalkForwardHalf]) -> str:
    lines = [
        "# US Risk-Parity Walk-Forward — in-sample vs out-of-sample (qe engine, ADR-041 P4)",
        "",
        "**Status:** advisory · backtest-only · live trading remains BLOCKED.",
        "",
    ]
    for h in halves:
        m = h.metrics
        lines += [
            f"## {h.label.title()} ({h.start} → {h.end})",
            "",
            f"CAGR **{m['cagr'] * 100:.1f}%** · Sharpe **{m['sharpe']:.2f}** · "
            f"MaxDD **{m['maxdd'] * 100:.1f}%** · hit {m['hit'] * 100:.0f}% · "
            f"{m['months']} months · positive years **{m['positive_years']}/{m['n_years']}**",
            "",
        ]
    lines += [
        "> Backtesting can recommend. It cannot promote. A human approves all production",
        "> changes. Live trading remains BLOCKED.",
        "",
    ]
    return "\n".join(lines)
