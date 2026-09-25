"""python -m qe.ai — offline, advisory AI research (ADR-043). Never trades.

    python -m qe.ai research --config configs/qe_ai_research.yaml --as-of 2026-07-14
    python -m qe.ai report   --journal journals/ai/<run_id>.jsonl
    python -m qe.ai fuse     --research journals/ai/<run_id>.jsonl [--engine-journal ...]
    python -m qe.ai hypothesize --research journals/ai/<run_id>.jsonl
    python -m qe.ai shadow   --gate configs/qe_ai_shadow_gate.yaml [--as-of D] [--show-binding]
    python -m qe.ai post-trade --engine-journal journals/<sim-or-paper>.jsonl [--max-trades N]
    python -m qe.ai dashboard   # static research view -> reports/qe-ai/dashboard/index.html

A separate entry point from ``python -m qe`` on purpose: the engine CLI imports
the paper engine at load, and qe.ai must never share a process path with it.
Any non-fake backend additionally requires ``--allow-llm-spend``.
"""

import argparse
from datetime import date
from pathlib import Path
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m qe.ai",
        description="Offline, advisory AI research over the qe lake (ADR-043). Never trades.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("research", help="run one research pass at an as-of date")
    r.add_argument("--config", required=True, help="qe.ai research config YAML")
    r.add_argument("--as-of", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    r.add_argument("--symbols", default="", help="extra comma-separated symbols to research")
    r.add_argument(
        "--allow-llm-spend",
        action="store_true",
        help="required for any paid backend (bedrock); the fake backend never spends",
    )
    r.add_argument("--base-dir", default=".")

    rp = sub.add_parser("report", help="rebuild reports/qe-ai/<run_id>/ from a research journal")
    rp.add_argument("--journal", required=True)
    rp.add_argument("--base-dir", default=".")

    f = sub.add_parser("fuse", help="AI view vs QuantEmbrace decision (deterministic fusion)")
    f.add_argument("--research", required=True, help="research journal (journals/ai/...)")
    f.add_argument("--config", default="configs/research_fusion.yaml", help="fusion config YAML")
    f.add_argument("--context", choices=("shadow", "study"), default="shadow")
    f.add_argument("--engine-journal", default=None, help="optional qe engine journal (read-only)")
    f.add_argument("--base-dir", default=".")

    h = sub.add_parser("hypothesize", help="draft testable hypotheses for human review")
    h.add_argument("--research", required=True, help="research journal (journals/ai/...)")
    h.add_argument("--allow-llm-spend", action="store_true")
    h.add_argument("--base-dir", default=".")

    pt = sub.add_parser("post-trade", help="review completed trades from an engine journal")
    pt.add_argument("--engine-journal", required=True, help="qe sim/paper journal (read-only)")
    pt.add_argument("--config", default="configs/qe_ai_research.yaml")
    pt.add_argument("--max-trades", type=int, default=20, help="most recent completed trades")
    pt.add_argument("--allow-llm-spend", action="store_true")
    pt.add_argument("--base-dir", default=".")

    db = sub.add_parser("dashboard", help="render the static research view (no JavaScript)")
    db.add_argument("--base-dir", default=".")

    sh = sub.add_parser("shadow", help="evaluate the pre-registered forward AI shadow gate")
    sh.add_argument("--gate", default="configs/qe_ai_shadow_gate.yaml")
    sh.add_argument("--as-of", type=date.fromisoformat, help="default: latest lake date")
    sh.add_argument(
        "--show-binding", action="store_true", help="print the values a human signs off"
    )
    sh.add_argument("--base-dir", default=".")

    args = parser.parse_args(argv)
    base = Path(args.base_dir)

    if args.cmd == "research":
        from qe.ai.config import ResearchRunConfig
        from qe.ai.llm import SpendNotAllowed
        from qe.ai.orchestration import run_research
        from qe.ai.reporting import write_research_report

        cfg = ResearchRunConfig.from_yaml(base / args.config)
        extra = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
        try:
            result = run_research(
                cfg,
                as_of=args.as_of,
                base_dir=base,
                allow_spend=args.allow_llm_spend,
                extra_symbols=extra,
            )
        except SpendNotAllowed as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        out = write_research_report(result.journal_path, base)
        rep = result.report
        print(f"run      : {result.run_id}")
        print(f"journal  : {result.journal_path}")
        print(f"report   : {out}")
        print(
            f"signals  : {len(rep.signals)} ({sum(s.contamination_risk for s in rep.signals)} "
            f"contaminated), failed: {list(rep.failed_symbols) or 'none'}"
        )
        print(f"llm      : {rep.llm_calls} calls, {rep.cache_hits} cache hits")
        print("advisory : research only; AI weight is 0 unless a study config says otherwise")
        return 0

    if args.cmd == "report":
        from qe.ai.reporting import write_research_report

        print(write_research_report(base / args.journal, base))
        return 0

    if args.cmd == "fuse":
        from qe.ai.fusion import FusionConfig, FusionRefused, run_fusion

        cfg = FusionConfig.from_yaml(base / args.config)
        try:
            run = run_fusion(
                base / args.research,
                cfg,
                context=args.context,
                base_dir=base,
                engine_journal=args.engine_journal,
            )
        except FusionRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        rep = run.report
        print(f"view     : {run.out_dir}")
        print(f"mode     : {rep.mode} ({rep.context}), decision date {rep.decision_date}")
        print(
            f"selected : {len(rep.selected)}; divergences from engine: {rep.divergences or 'none'}"
        )
        return 0

    if args.cmd == "hypothesize":
        from qe.ai.hypotheses import generate_hypotheses
        from qe.ai.llm import SpendNotAllowed

        try:
            run = generate_hypotheses(
                base / args.research, base_dir=base, allow_spend=args.allow_llm_spend
            )
        except SpendNotAllowed as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        print(f"journal  : {run.journal_path}")
        print(f"drafts   : {len(run.drafts)} CANDIDATE draft(s) -> {run.out_dir or '(none)'}")
        for d in run.drafts:
            flags = [f"re-proposes {d.eliminated_family}"] if d.eliminated_family else []
            flags += [] if d.testable_now else [f"needs {', '.join(d.missing_data)}"]
            print(f"  {d.draft_id} {d.name} [{d.family}] {'; '.join(flags) or 'testable now'}")
        print("advisory : drafts only; a human decides via `python -m qe lifecycle`")
        return 0 if run.status == "OK" else 1

    if args.cmd == "dashboard":
        from qe.ai.dashboard import build_dashboard

        print(build_dashboard(base))
        return 0

    if args.cmd == "post-trade":
        from qe.ai.config import ResearchRunConfig
        from qe.ai.llm import SpendNotAllowed
        from qe.ai.post_trade import run_post_trade

        cfg = ResearchRunConfig.from_yaml(base / args.config)
        try:
            run = run_post_trade(
                base / args.engine_journal,
                cfg,
                base_dir=base,
                allow_spend=args.allow_llm_spend,
                max_trades=args.max_trades,
            )
        except SpendNotAllowed as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        confirmed = sum(r.quant_thesis == "CONFIRMED" for r in run.reviews)
        print(f"journal  : {run.journal_path}")
        print(
            f"reviews  : {len(run.reviews)} completed trade(s); open positions {run.open_positions}"
        )
        print(f"thesis   : factor pick beat benchmark in {confirmed}/{len(run.reviews)}")
        print(f"report   : {run.out_dir or '(none)'}")
        return 0

    if args.cmd == "shadow":
        from qe.ai.config import ResearchRunConfig
        from qe.ai.shadow import ShadowGateConfig, run_shadow

        gate = ShadowGateConfig.from_yaml(base / args.gate)
        if args.show_binding:
            cfg = ResearchRunConfig.from_yaml(base / gate.research_config)
            print("Fill these into the gate, set status: SIGNED_OFF, commit BEFORE accrual:")
            print(f"  research_config_hash: {cfg.config_hash()}")
            print(f"  model_id: {cfg.quick_model.model_id}   # analyst model")
            print(
                f"  (knowledge_cutoff in {gate.research_config}: {cfg.quick_model.knowledge_cutoff})"
            )
            print("  signed_off_by: <your name>   signed_off_on: <today, YYYY-MM-DD>")
            if cfg.quick_model.knowledge_cutoff is None:
                print(
                    "WARNING: do NOT sign off yet - knowledge_cutoff is unknown, so every "
                    "signal would be contaminated and the series could never accrue."
                )
            if cfg.backend == "fake":
                print(
                    "WARNING: do NOT sign off yet - backend=fake produces deterministic "
                    "test-double scores, not research. Configure the real model first (P10)."
                )
            return 0
        as_of = args.as_of
        if as_of is None:
            from qe.data.feed import LiveLakeFeed, reference_symbol_for_market

            cfg = ResearchRunConfig.from_yaml(base / gate.research_config)
            from qe.config import RunConfig

            book = RunConfig.from_yaml(base / cfg.book_config)
            m = book.universe.market
            as_of = LiveLakeFeed(
                base / book.data.lake_root,
                market=m,
                reference_symbol=reference_symbol_for_market(m),
            ).latest_date()
        rep = run_shadow(args.gate, as_of=as_of, base_dir=base)
        r = rep.result
        print(f"gate     : {rep.gate_status} ({rep.gate_hash[:12]})")
        print(f"verdict  : {r.verdict} - {r.reason}")
        print(f"months   : {r.months}; observations: {len(rep.collected.observations)}")
        print(f"report   : {rep.out_dir}")
        return 0
    return 2
