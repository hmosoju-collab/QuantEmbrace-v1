"""Factor-book study: sim run + EW benchmark per leg + report.

This is the v2 home of the forward-book monthly cadence (ADR-037 M2): the same
numbers `replay_delivery_book_forward.py` produces, computed by the one engine,
with journal + snapshot + config-hash provenance and a machine-readable summary.

Advisory only. Backtesting can recommend; it cannot promote. Live stays BLOCKED.
"""

from dataclasses import dataclass
from datetime import date
from itertools import pairwise
import json
import math
from pathlib import Path

from qe.config import RunConfig
from qe.data.panel import Panel
from qe.engine.sim import SimRunResult, run_sim
from qe.research.benchmark import ew_benchmark_return


@dataclass(frozen=True)
class StudyResult:
    sim: SimRunResult
    months: list[dict]  # {to, book, bench, alpha} per leg (last is partial)
    book_cum: float
    bench_cum: float
    n_full: int
    report_path: Path
    summary_path: Path


def run_factor_book_study(
    config: RunConfig,
    *,
    base_dir: str | Path = ".",
    panel: Panel | None = None,
    report_dir: str | Path | None = None,
) -> StudyResult:
    base_dir = Path(base_dir)
    sim = run_sim(config, base_dir=base_dir, panel=panel)
    if len(sim.nav_history) != len(sim.rebal_positions):
        raise RuntimeError(
            "study requires every scheduled rebalance to execute "
            f"({len(sim.nav_history)}/{len(sim.rebal_positions)} ran — see journal RISK records)"
        )

    top_n = config.strategy.top_n if config.strategy else 200
    legs = [*sim.rebal_positions, sim.final_pos]
    navs = [*(nav for _, nav in sim.nav_history), sim.final_mtm[1]]
    leg_dates = [*(d for d, _ in sim.nav_history), sim.final_mtm[0]]

    bench_rets = [ew_benchmark_return(sim.panel, a, b, top_n) for a, b in pairwise(legs)]
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
        "factor": config.strategy.factor if config.strategy else None,
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
    report_path.write_text(_render_report(config, sim, months, book_cum, bench_cum, n_full))

    return StudyResult(
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
) -> str:
    factor = config.strategy.factor if config.strategy else "?"
    lines = [
        f"# Factor Book Study — {factor} (qe engine, ADR-037)",
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
        "> few for Sharpe/t-stat claims. Judge alpha vs the EW benchmark on the same months.",
        "",
        "## Monthly returns (net of NSE delivery costs + slippage)",
        "",
        "| Month-end | Book | Benchmark (EW univ) | Alpha |",
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
        f"NAV ₹{sim.final_mtm[1]:,.0f} (seed ₹{config.seed_nav:,.0f}).",
        "",
        "> Backtesting can recommend. It cannot promote. A human approves all production",
        "> changes. Live trading remains BLOCKED.",
        "",
    ]
    return "\n".join(lines)
