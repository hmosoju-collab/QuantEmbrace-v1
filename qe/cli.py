"""qe command-line entry point.

Usage:
    python -m qe null   --config path/to/run.yaml [--base-dir DIR]
    python -m qe study  --config path/to/run.yaml [--base-dir DIR]
    python -m qe paper  --config path/to/run.yaml [--as-of YYYY-MM-DD] [--base-dir DIR]
    python -m qe kill   status|activate|deactivate [--reason ...] [--path FILE]
    python -m qe report --journal path/to/journal.jsonl
"""

import argparse
from datetime import date
from pathlib import Path
import sys

from qe.config import RunConfig
from qe.engine import run_null


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qe", description="QuantEmbrace v2 engine (ADR-037)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_null = sub.add_parser("null", help="run the M1 null engine (data spine acceptance)")
    p_null.add_argument("--config", required=True, help="RunConfig YAML")
    p_null.add_argument("--base-dir", default=".", help="anchor for relative lake/journal paths")

    p_study = sub.add_parser("study", help="run a factor-book study (sim + benchmark + report)")
    p_study.add_argument("--config", required=True, help="RunConfig YAML (mode: sim)")
    p_study.add_argument("--base-dir", default=".", help="anchor for relative lake/journal paths")

    p_paper = sub.add_parser("paper", help="run one real-time paper session (WallClock)")
    p_paper.add_argument("--config", required=True, help="RunConfig YAML (with strategy)")
    p_paper.add_argument("--as-of", help="trading date YYYY-MM-DD (default: latest lake date)")
    p_paper.add_argument("--base-dir", default=".", help="anchor for relative lake/journal paths")

    p_kill = sub.add_parser("kill", help="operator kill switch")
    p_kill.add_argument("action", choices=["status", "activate", "deactivate"])
    p_kill.add_argument("--reason", default="operator request")
    p_kill.add_argument("--by", default="operator")
    p_kill.add_argument(
        "--path", default="backtest-data/paper_book/qe_kill_switch.json", help="flag file"
    )

    p_report = sub.add_parser("report", help="render a session report from a journal")
    p_report.add_argument("--journal", required=True, help="journal .jsonl path")

    p_live = sub.add_parser("live", help="attempt to open live trading (evidence-gated; refuses)")
    p_live.add_argument("--config", required=True, help="RunConfig YAML (with strategy)")
    p_live.add_argument("--approved-by", default="operator")
    p_live.add_argument("--base-dir", default=".")

    p_drill = sub.add_parser("drill", help="run fail-closed pre-live safety drills")
    p_drill.add_argument("--base-dir", default=".")

    args = parser.parse_args(argv)

    if args.command == "null":
        config = RunConfig.from_yaml(args.config)
        result = run_null(config, base_dir=Path(args.base_dir))
        print(f"session_id        {result.session_id}")
        print(f"status            {result.status}")
        print(f"config_hash       {result.config_hash}")
        print(f"code_sha          {result.code_sha}")
        print(f"data_snapshot_id  {result.data_snapshot_id}")
        print(f"bars/days/symbols {result.n_bars}/{result.n_days}/{result.n_symbols}")
        print(f"journal           {result.journal_path}")
        return 0

    if args.command == "study":
        from qe.research import run_factor_book_study, run_walk_forward_study

        config = RunConfig.from_yaml(args.config)

        is_risk_parity = config.strategy is not None and config.strategy.kind == "risk_parity_lite"
        if is_risk_parity and config.study_kind == "forward_book":
            from qe.research.us_study import run_risk_parity_study

            study = run_risk_parity_study(config, base_dir=Path(args.base_dir))
            sim = study.sim
            print(f"session_id        {sim.session_id}")
            print(f"config_hash       {sim.config_hash}")
            print(f"code_sha          {sim.code_sha}")
            print(f"data_snapshot_id  {sim.data_snapshot_id}")
            print(f"rebalances        {len(sim.nav_history)}")
            for d, nav in sim.nav_history:
                print(f"  {d}  NAV ${nav:,.2f}")
            print(f"final MTM         {sim.final_mtm[0]}  NAV ${sim.final_mtm[1]:,.2f}")
            print(
                f"cumulative        book {study.book_cum * 100:+.2f}% vs bench "
                f"{study.bench_cum * 100:+.2f}% ({study.n_full} full months)"
            )
            print(f"report            {study.report_path}")
            print(f"journal           {sim.journal_path}")
            print("Advisory only. Backtesting can recommend; it cannot promote. Live BLOCKED.")
            return 0
        if is_risk_parity and config.study_kind == "walk_forward":
            print(
                "study_kind=walk_forward for kind=risk_parity_lite is not CLI-wired yet — "
                "call qe.research.us_study.run_risk_parity_walkforward(config, split_date=...) "
                "directly (ADR-041 P4; see docs/strategy/us-qe-phase4-report.md)."
            )
            return 2

        if config.study_kind == "walk_forward":
            wf = run_walk_forward_study(config, base_dir=Path(args.base_dir))
            em = wf.engine_metrics
            print(f"session_id        {wf.sim.session_id}")
            print(f"config_hash       {config.config_hash()}")
            print(f"code_sha          {wf.sim.code_sha}")
            print(f"data_snapshot_id  {wf.sim.data_snapshot_id}")
            print(
                f"engine            CAGR {em['cagr'] * 100:+.1f}%  Sharpe {em['sharpe']:.2f}  "
                f"MaxDD {em['maxdd'] * 100:.1f}%  years {em['positive_years']}/{em['n_years']} positive"
            )
            print(f"engine gates      {'ALL PASS' if wf.engine_pass else 'FAIL'}")
            if wf.v1_variants is not None:
                m = wf.v1_variants["delivery"]["metrics"]
                print(
                    f"v1 cross-check    CAGR {m['cagr'] * 100:+.1f}%  Sharpe {m['sharpe']:.2f}  "
                    f"MaxDD {m['maxdd'] * 100:.1f}%  years {m['positive_years']}/{m['n_years']} positive"
                )
                print(f"v1 gates          {'ALL PASS' if wf.v1_pass else 'FAIL'}")
            if wf.experiment_record:
                print(
                    f"experiment        {wf.experiment_record['experiment_id']} "
                    f"(#{wf.experiment_record['family_experiment_count']} in family "
                    f"'{config.experiment.family}')"
                )
            print(f"report            {wf.report_path}")
            print(f"journal           {wf.sim.journal_path}")
            print("Advisory only. Backtesting can recommend; it cannot promote. Live BLOCKED.")
            return 0

        study = run_factor_book_study(config, base_dir=Path(args.base_dir))
        sim = study.sim
        print(f"session_id        {sim.session_id}")
        print(f"config_hash       {sim.config_hash}")
        print(f"code_sha          {sim.code_sha}")
        print(f"data_snapshot_id  {sim.data_snapshot_id}")
        print(f"rebalances        {len(sim.nav_history)}")
        for d, nav in sim.nav_history:
            print(f"  {d}  NAV ₹{nav:,.2f}")
        print(f"final MTM         {sim.final_mtm[0]}  NAV ₹{sim.final_mtm[1]:,.2f}")
        print(
            f"cumulative        book {study.book_cum * 100:+.2f}% vs bench "
            f"{study.bench_cum * 100:+.2f}% ({study.n_full} full months)"
        )
        print(f"report            {study.report_path}")
        print(f"journal           {sim.journal_path}")
        print("Advisory only. Backtesting can recommend; it cannot promote. Live BLOCKED.")
        return 0

    if args.command == "paper":
        from qe.engine import run_paper
        from qe.reporting import render_session_report

        config = RunConfig.from_yaml(args.config)
        as_of = date.fromisoformat(args.as_of) if args.as_of else None
        result = run_paper(config, base_dir=Path(args.base_dir), as_of=as_of)
        print(render_session_report(result.journal_path))
        print(f"  state             {result.state_path}")
        print(f"  journal           {result.journal_path}")
        if result.kill_active:
            print("  ⚠ KILL SWITCH ACTIVE — no orders emitted this session.")
        print("Advisory only. Paper mode. Live trading remains BLOCKED.")
        return 0

    if args.command == "kill":
        from qe.killswitch import KillSwitch

        ks = KillSwitch(args.path)
        if args.action == "status":
            st = ks.state()
            print(f"kill switch: {'ACTIVE' if st.active else 'inactive'}  ({args.path})")
            if st.active:
                print(f"  reason      {st.reason}")
                print(f"  activated   {st.activated_at} by {st.activated_by}")
        elif args.action == "activate":
            st = ks.activate(args.reason, by=args.by)
            print(f"kill switch ACTIVE — {st.reason} (by {st.activated_by} at {st.activated_at})")
        else:
            ks.deactivate(by=args.by)
            print("kill switch deactivated. Trading may resume (subject to all other gates).")
        return 0

    if args.command == "report":
        from qe.reporting import render_session_report

        print(render_session_report(args.journal))
        return 0

    if args.command == "live":
        from qe.data.feed import LiveLakeFeed
        from qe.killswitch import KillSwitch
        from qe.live_gate import LiveGateRefused, mint_live_gate_token

        config = RunConfig.from_yaml(args.config)
        base_dir = Path(args.base_dir)
        try:
            age = (date.today() - LiveLakeFeed(base_dir / config.data.lake_root).latest_date()).days
        except Exception:
            age = None
        kill_active = KillSwitch(
            base_dir / "backtest-data" / "paper_book" / "qe_kill_switch.json"
        ).is_active()
        factor = config.strategy.factor if config.strategy else ""
        print("QuantEmbrace — LIVE GATE CEREMONY")
        print("Backtesting recommends; a human promotes. Evaluating pre-registered evidence...\n")
        try:
            token = mint_live_gate_token(
                base_dir=base_dir,
                config_hash=config.config_hash(),
                factor=factor,
                lake_age_days=age,
                kill_active=kill_active,
                approved_by=args.approved_by,
            )
            print(f"  ✅ GATE PASSED — token issued (expires {token.expires_utc}).")
            print(
                "  Live trading is now UNLOCKED for this config, but this CLI does NOT auto-trade."
            )
            print("  Proceed via the pre-live runbook with an operator-supplied broker client.")
            return 0
        except LiveGateRefused as refused:
            print("  ⛔ LIVE TRADING BLOCKED — the evidence gate refused. No token minted.\n")
            for c in refused.checks:
                print(f"    [{'PASS' if c.passed else 'FAIL'}] {c.name:22} {c.detail}")
            print("\n  This is by design. Live unblocks only when ALL checks pass — evidence, not")
            print(
                "  authorization. See docs/live-readiness/pre-live-runbook.md. Live remains BLOCKED."
            )
            return 1

    if args.command == "drill":
        from qe.livecheck import run_fail_closed_drills

        print("QuantEmbrace — fail-closed pre-live drills\n")
        results = run_fail_closed_drills()
        for r in results:
            print(f"  [{'PASS' if r.passed else 'FAIL'}] {r.name:14} {r.detail}")
        all_pass = all(r.passed for r in results)
        print(
            f"\n  {'ALL DRILLS PASSED' if all_pass else 'DRILL FAILURE'} — "
            "the fail-closed machinery " + ("works." if all_pass else "MUST be fixed before live.")
        )
        return 0 if all_pass else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
