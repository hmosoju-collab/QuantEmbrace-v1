"""python -m qe.ai — offline, advisory AI research (ADR-043). Never trades.

    python -m qe.ai research --config configs/qe_ai_research.yaml --as-of 2026-07-14
    python -m qe.ai report   --journal journals/ai/<run_id>.jsonl
    python -m qe.ai fuse     --research journals/ai/<run_id>.jsonl [--engine-journal ...]

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
    return 2
