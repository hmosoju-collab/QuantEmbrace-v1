"""Declarative walk-forward study: engine leg + v1 cross-check + gates + registry.

Two models, honestly separated:

* **engine** — the go-forward model: one continuous share-based sim through
  `qe.engine.sim` (integer shares, actual delta-notional statutory costs,
  dislocation filter — identical semantics to the live forward books).
* **v1 cross-check** — the registered study's returns-space model reproduced
  verbatim (`qe.research.wf_v1`) so legacy numbers stay auditable.

Differences between the columns are MODEL differences (shares vs returns,
cost heuristic vs actual costs, universe filter set), not bugs; the report
prints both. Gates evaluate against both; the registry records both verdicts.

Advisory only. Backtesting can recommend; it cannot promote. Live stays BLOCKED.
"""

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path

import pandas as pd

from qe.config import RunConfig, WalkForwardConfig
from qe.data.panel import Panel, load_panel, resolve_panel_files
from qe.data.snapshot import create_snapshot
from qe.engine.sim import SimRunResult, run_sim
from qe.research import wf_v1
from qe.research.gates import all_passed, evaluate_gates
from qe.research.metrics import (
    monthly_metrics,
    nav_monthly_returns,
    per_year,
    with_walk_forward_stats,
)
from qe.research.regime import pit_regime_series
from qe.research.registry import register_run


@dataclass(frozen=True)
class WalkForwardResult:
    sim: SimRunResult
    engine_metrics: dict
    engine_per_year: pd.DataFrame
    engine_gates: list[dict]
    engine_pass: bool
    v1_variants: dict[str, dict] | None  # label -> {"metrics": ..., "cash_months": ...}
    v1_per_year: pd.DataFrame | None  # delivery (no overlay)
    v1_gates: list[dict] | None
    v1_pass: bool | None
    experiment_record: dict | None
    report_path: Path
    summary_path: Path


def run_walk_forward_study(
    config: RunConfig,
    *,
    base_dir: str | Path = ".",
    panel: Panel | None = None,
    report_dir: str | Path | None = None,
) -> WalkForwardResult:
    base_dir = Path(base_dir)
    wf = config.walk_forward or WalkForwardConfig()
    strat = config.strategy
    assert strat is not None  # guaranteed by RunConfig validation

    # Panel over the STUDY window (warmup rows are inside it, as in v1).
    if panel is None:
        lake_root = base_dir / config.data.lake_root
        files = resolve_panel_files(
            lake_root,
            config.start_date,
            config.end_date,
            market=config.universe.market,
            segment=config.universe.segment,
            interval=config.data.interval,
        )
        manifest = create_snapshot(
            lake_root,
            files,
            {
                "study": "walk_forward",
                "market": config.universe.market,
                "segment": config.universe.segment,
                "interval": config.data.interval,
                "start_date": str(config.start_date),
                "end_date": str(config.end_date),
            },
        )
        snapshot_id = manifest["snapshot_id"]
        panel = load_panel(files, config.start_date, config.end_date)
    else:
        snapshot_id = "ds-injected"

    # Engine leg: continuous sim from the first tradeable month-end (>= warmup).
    rb_rows = [
        r
        for r in wf_v1.rebalance_rows(panel.index)
        if r >= wf.warmup_rows and r < len(panel.index) - 1
    ]
    if len(rb_rows) < 2:
        raise ValueError("panel too short for a walk-forward (need >=2 post-warmup month-ends)")
    engine_config = config.model_copy(
        update={"start_date": panel.date_at(rb_rows[0]), "study_kind": "forward_book"}
    )
    sim = run_sim(engine_config, base_dir=base_dir, panel=panel, panel_snapshot_id=snapshot_id)

    engine_monthly = nav_monthly_returns(sim.nav_history)
    engine_py = per_year(engine_monthly)
    engine_metrics = with_walk_forward_stats(monthly_metrics(engine_monthly), engine_py)

    # v1 cross-check: the registered returns-space model, verbatim.
    v1_variants = v1_py = v1_gates = None
    v1_pass = None
    if wf.v1_cross_check:
        regime = wf_v1.regime_series(panel.close, panel.turnover, wf.overlay_sma)
        # F-10: the verbatim v1 overlay selects its market proxy from total-period
        # turnover (look-ahead). The PIT variant is reported alongside it.
        regime_pit = pit_regime_series(panel.close, panel.turnover, wf.overlay_sma)
        legs = {
            "delivery": wf_v1.run_delivery(
                panel.close,
                panel.turnover,
                panel.delivery,
                None,
                strat.top_n,
                strat.k,
                wf.warmup_rows,
            ),
            "delivery+overlay": wf_v1.run_delivery(
                panel.close,
                panel.turnover,
                panel.delivery,
                regime,
                strat.top_n,
                strat.k,
                wf.warmup_rows,
            ),
            "benchmark": wf_v1.run_benchmark(
                panel.close, panel.turnover, None, strat.top_n, wf.warmup_rows
            ),
            "benchmark+overlay": wf_v1.run_benchmark(
                panel.close, panel.turnover, regime, strat.top_n, wf.warmup_rows
            ),
            "delivery+overlay_pit": wf_v1.run_delivery(
                panel.close,
                panel.turnover,
                panel.delivery,
                regime_pit,
                strat.top_n,
                strat.k,
                wf.warmup_rows,
            ),
            "benchmark+overlay_pit": wf_v1.run_benchmark(
                panel.close, panel.turnover, regime_pit, strat.top_n, wf.warmup_rows
            ),
        }
        v1_py = per_year(legs["delivery"].monthly_net)
        v1_variants = {
            label: {
                "metrics": monthly_metrics(leg.monthly_net),
                "cash_months": leg.cash_months,
            }
            for label, leg in legs.items()
        }
        v1_delivery_metrics = with_walk_forward_stats(v1_variants["delivery"]["metrics"], v1_py)
        v1_variants["delivery"]["metrics"] = v1_delivery_metrics

    gates = config.experiment.gates if config.experiment else ()
    engine_gates = evaluate_gates(gates, engine_metrics)
    # No pre-registered gates means nothing was tested: FAIL, never a vacuous
    # pass (F-11; all_passed([]) is False by design, see qe/research/gates.py).
    engine_pass = all_passed(engine_gates)
    if wf.v1_cross_check and v1_variants is not None:
        v1_gates = evaluate_gates(gates, v1_variants["delivery"]["metrics"])
        v1_pass = all_passed(v1_gates)

    out_dir = Path(report_dir) if report_dir else base_dir / "reports" / "qe" / sim.session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "session_id": sim.session_id,
        "study_kind": "walk_forward",
        "config_hash": config.config_hash(),
        "engine_config_hash": sim.config_hash,
        "code_sha": sim.code_sha,
        "data_snapshot_id": sim.data_snapshot_id,
        "engine": {
            "metrics": engine_metrics,
            "per_year": engine_py.reset_index().to_dict(orient="records"),
            "gates": engine_gates,
            "all_gates_passed": engine_pass,
        },
        "v1_cross_check": (
            None
            if v1_variants is None
            else {
                "variants": v1_variants,
                "per_year_delivery": v1_py.reset_index().to_dict(orient="records"),
                "gates": v1_gates,
                "all_gates_passed": v1_pass,
            }
        ),
    }

    experiment_record = None
    if config.experiment:
        experiment_record = register_run(
            base_dir,
            config.experiment,
            {
                "session_id": sim.session_id,
                "config_hash": config.config_hash(),
                "data_snapshot_id": sim.data_snapshot_id,
                "code_sha": sim.code_sha,
                "engine_pass": engine_pass,
                "v1_pass": v1_pass,
                "engine_metrics": engine_metrics,
            },
        )
        summary["experiment"] = {
            "experiment_id": experiment_record["experiment_id"],
            "family": config.experiment.family,
            "family_experiment_count": experiment_record["family_experiment_count"],
        }

    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    report_path = out_dir / "report.md"
    report_path.write_text(_render_report(config, sim, summary, engine_py, v1_py))

    return WalkForwardResult(
        sim=sim,
        engine_metrics=engine_metrics,
        engine_per_year=engine_py,
        engine_gates=engine_gates,
        engine_pass=engine_pass,
        v1_variants=v1_variants,
        v1_per_year=v1_py,
        v1_gates=v1_gates,
        v1_pass=v1_pass,
        experiment_record=experiment_record,
        report_path=report_path,
        summary_path=summary_path,
    )


def _fmt_gates(results: list[dict]) -> list[str]:
    if not results:
        return ["_(no gates declared — verdict is FAIL: an ungated study proves nothing)_"]
    lines = ["| Gate | Metric | Threshold | Actual | Verdict |", "|---|---|---|---:|---|"]
    for g in results:
        actual = "—" if g["actual"] is None else f"{g['actual']:.4f}"
        lines.append(
            f"| {g['name']} | `{g['metric']}` | {g['op']} {g['value']} | {actual} | "
            f"{'✅ PASS' if g['passed'] else '❌ FAIL'} |"
        )
    return lines


def _fmt_per_year(py: pd.DataFrame) -> list[str]:
    lines = ["| Year | Return | Sharpe | MaxDD | Months |", "|---|---:|---:|---:|---:|"]
    for y, row in py.iterrows():
        lines.append(
            f"| {y} | {row['return'] * 100:.1f}% | {row['sharpe']:.2f} "
            f"| {row['maxdd'] * 100:.1f}% | {int(row['months'])} |"
        )
    return lines


def _render_report(
    config: RunConfig,
    sim: SimRunResult,
    summary: dict,
    engine_py: pd.DataFrame,
    v1_py: pd.DataFrame | None,
) -> str:
    strat = config.strategy
    em = summary["engine"]["metrics"]
    lines = [
        f"# Walk-Forward Study — {strat.factor} (qe engine, ADR-037 M3)",
        "",
        "**Status:** advisory · backtest-only · live trading remains BLOCKED.",
        f"**Date:** {date.today()}   **Window:** {config.start_date} → {config.end_date}   "
        f"**Book:** top-{strat.k} of top-{strat.top_n} liquid, monthly.",
        "",
        "## Provenance",
        "",
        f"- session `{sim.session_id}` · study config hash `{summary['config_hash']}`",
        f"- code `{sim.code_sha}` · data snapshot `{sim.data_snapshot_id}`",
        f"- journal `{sim.journal_path}`",
    ]
    if "experiment" in summary:
        e = summary["experiment"]
        lines += [
            f"- experiment `{e['experiment_id']}` · family `{e['family']}` — "
            f"**experiment #{e['family_experiment_count']} in this family** "
            "(multiple-testing note: interpret marginal passes accordingly)",
        ]
    lines += [
        "",
        "## Engine model (share-based, actual costs — the go-forward model)",
        "",
        f"CAGR **{em['cagr'] * 100:.1f}%** · Sharpe **{em['sharpe']:.2f}** · "
        f"MaxDD **{em['maxdd'] * 100:.1f}%** · hit {em['hit'] * 100:.0f}% · "
        f"{em['months']} months · positive years **{em['positive_years']}/{em['n_years']}**",
        "",
        *_fmt_per_year(engine_py),
        "",
        "### Gates (engine model)",
        "",
        *_fmt_gates(summary["engine"]["gates"]),
        "",
        f"**Engine verdict: {'ALL GATES PASSED' if summary['engine']['all_gates_passed'] else 'GATE FAILURE'}**",
    ]
    v1 = summary.get("v1_cross_check")
    if v1:
        lines += [
            "",
            "## v1 cross-check (returns-space model of the registered study, verbatim)",
            "",
            "| Variant | CAGR | Sharpe | MaxDD | Hit% |",
            "|---|---:|---:|---:|---:|",
        ]
        labels = {
            "delivery": "delivery",
            "delivery+overlay": "delivery+overlay ⚠️ look-ahead proxy (F-10)",
            "delivery+overlay_pit": "delivery+overlay (point-in-time proxy)",
            "benchmark": "benchmark",
            "benchmark+overlay": "benchmark+overlay ⚠️ look-ahead proxy (F-10)",
            "benchmark+overlay_pit": "benchmark+overlay (point-in-time proxy)",
        }
        for label, shown in labels.items():
            if label not in v1["variants"]:
                continue
            m = v1["variants"][label]["metrics"]
            lines.append(
                f"| {shown} | {m['cagr'] * 100:.1f}% | {m['sharpe']:.2f} "
                f"| {m['maxdd'] * 100:.1f}% | {m['hit'] * 100:.0f}% |"
            )
        lines += [
            "",
            "⚠️ The verbatim v1 overlay picks its market proxy from total-period turnover",
            "(look-ahead, current-state F-10); it is kept only for parity with the registered",
            "study. Use the point-in-time overlay rows as evidence.",
        ]
        lines += [
            "",
            "Per-year OOS (v1 delivery, no overlay):",
            "",
            *_fmt_per_year(v1_py),
            "",
            "### Gates (v1 model)",
            "",
            *_fmt_gates(v1["gates"] or []),
            "",
            "**Model-difference note:** engine vs v1 columns differ by construction — integer",
            "shares on real NAV vs weight returns, actual delta-notional statutory costs vs a",
            "name-turnover heuristic, and the engine's corp-action dislocation filter (absent",
            "in the v1 study). Divergence between columns is a model statement, not a bug.",
        ]
    lines += [
        "",
        "> Backtesting can recommend. It cannot promote. A human approves all production",
        "> changes. Live trading remains BLOCKED.",
        "",
    ]
    return "\n".join(lines)
