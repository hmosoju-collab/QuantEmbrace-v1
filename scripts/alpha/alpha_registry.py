#!/usr/bin/env python
"""alpha_registry — operator CLI for the Alpha Engine model registry (ADR-031).

Institutional memory for alpha models: lifecycle status, lineage, champion/
challenger, and statistical-gate recommendations. Promotion is ALWAYS manual —
this tool records human decisions and (for APPROVED) refuses to skip the
statistical gate unless an explicit ``--override-stats`` reason is given.

Commands:
    list                      List all models + their champion/challenger.
    show MODEL                Show every version of MODEL with status + lineage.
    register-version MODEL    Register a new SHADOW version (lineage mandatory).
    set-status MODEL          Transition a version's lifecycle status.
    set-champion MODEL        Set the live champion version.
    set-challenger MODEL      Set the challenger version (shadowed alongside).
    validate-promotion MODEL  Run the statistical promotion gate (advisory).

Examples:
    python scripts/alpha/alpha_registry.py register-version alpha_orb_v3 \\
        --version 2026-07-01 --parent alpha_orb_v2@2026-06-13 \\
        --experiment-id EXP-2026-081 --alpha-family momentum \\
        --hypothesis "ATR normalization improves IC in high-vol regimes" \\
        --change-summary "ATR-normalized breakout threshold"
    python scripts/alpha/alpha_registry.py set-status alpha_orb_v3 \\
        --version 2026-07-01 --status APPROVED --reason "..." --decided-by operator
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from alpha_engine.store.registry_store import (  # noqa: E402
    AlphaRegistryStore,
    RegistryError,
)
from shared.aws.clients import get_dynamodb_resource  # noqa: E402
from shared.config.settings import get_settings  # noqa: E402


def _store(prefix: str) -> AlphaRegistryStore:
    resource = get_dynamodb_resource()
    return AlphaRegistryStore(table=resource.Table(f"{prefix}-alpha-registry"))


def _split_parent(parent: str | None) -> tuple[str | None, str | None]:
    if not parent:
        return None, None
    if "@" in parent:
        pid, pver = parent.split("@", 1)
        return pid, pver
    return parent, None


def _cmd_list(store: AlphaRegistryStore, args: argparse.Namespace) -> int:
    # The registry has no global index in v1; the operator passes --models or the
    # configured set is used. (A full table scan is intentionally avoided.)
    settings = get_settings()
    models = args.models.split(",") if args.models else settings.alpha.models
    for model_id in models:
        meta = store.get_meta(model_id)
        if meta is None:
            print(f"{model_id}: (not registered)")
            continue
        print(
            f"{model_id}: champion={meta.get('champion_model_version')} "
            f"challenger={meta.get('challenger_model_version')}"
        )
    return 0


def _cmd_show(store: AlphaRegistryStore, args: argparse.Namespace) -> int:
    meta = store.get_meta(args.model)
    if meta is None:
        print(f"{args.model}: not registered")
        return 1
    print(json.dumps(meta, indent=2, default=str))
    for v in store.list_versions(args.model):
        print(json.dumps(v, indent=2, default=str))
    return 0


def _cmd_register(store: AlphaRegistryStore, args: argparse.Namespace) -> int:
    parent_id, parent_ver = _split_parent(args.parent)
    try:
        store.register_version(
            model_id=args.model,
            model_version=args.version,
            alpha_family=args.alpha_family,
            hypothesis=args.hypothesis,
            experiment_id=args.experiment_id,
            change_summary=args.change_summary,
            parent_model_id=parent_id,
            parent_model_version=parent_ver,
            notes=args.notes,
        )
    except RegistryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"registered {args.model}@{args.version} (status=SHADOW)")
    return 0


def _cmd_set_status(store: AlphaRegistryStore, args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.status.upper() == "APPROVED" and not args.override_stats:
        version = store.get_version(args.model, args.version)
        if version is None:
            print(f"ERROR: {args.model}@{args.version} not registered", file=sys.stderr)
            return 1
        eligible, reasons = _promotion_eligible(version, settings.alpha)
        if not eligible:
            print(
                "REFUSED: statistical promotion gate not satisfied:\n  - "
                + "\n  - ".join(reasons)
                + "\nRe-run with --override-stats \"<reason>\" to record a manual override.",
                file=sys.stderr,
            )
            return 2
    try:
        store.set_status(
            model_id=args.model,
            model_version=args.version,
            status=args.status,
            reason=args.reason if not args.override_stats else f"{args.reason} [override: {args.override_stats}]",
            decided_by=args.decided_by,
        )
    except RegistryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"{args.model}@{args.version} -> {args.status.upper()}")
    return 0


def _cmd_set_champion(store: AlphaRegistryStore, args: argparse.Namespace) -> int:
    try:
        store.set_champion(
            model_id=args.model, model_version=args.version,
            reason=args.reason, decided_by=args.decided_by,
        )
    except RegistryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"{args.model}: champion = {args.version}")
    return 0


def _cmd_set_challenger(store: AlphaRegistryStore, args: argparse.Namespace) -> int:
    try:
        store.set_challenger(
            model_id=args.model, model_version=args.version,
            reason=args.reason, decided_by=args.decided_by,
        )
    except RegistryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"{args.model}: challenger = {args.version}")
    return 0


def _cmd_validate(store: AlphaRegistryStore, args: argparse.Namespace) -> int:
    settings = get_settings()
    version = store.get_version(args.model, args.version)
    if version is None:
        print(f"ERROR: {args.model}@{args.version} not registered", file=sys.stderr)
        return 1
    eligible, reasons = _promotion_eligible(version, settings.alpha)
    print(f"{args.model}@{args.version} promotion-eligible: {eligible}")
    for r in reasons:
        print(f"  - {r}")
    print("\nNOTE: This is advisory only. A human approves all promotions.")
    return 0 if eligible else 2


def _promotion_eligible(version: dict, alpha_cfg) -> tuple[bool, list[str]]:
    """Statistical promotion gate (ADR-031 P5). Full DSR/PBO/FDR computation lives
    in research/statistics.py; this reads the stats already stored on the version
    by ``evaluate_alpha --stats`` and applies the configured thresholds.
    """
    dsr = version.get("deflated_sharpe")
    pbo = version.get("pbo_score")
    adj_p = version.get("adjusted_p_value")
    if dsr is None or pbo is None or adj_p is None:
        return False, [
            "no statistical validation on record — run "
            "`evaluate_alpha --stats` and persist results before promotion"
        ]
    reasons: list[str] = []
    if dsr <= alpha_cfg.stats_dsr_threshold:
        reasons.append(f"deflated_sharpe {dsr:.3f} <= threshold {alpha_cfg.stats_dsr_threshold}")
    if pbo >= alpha_cfg.stats_pbo_threshold:
        reasons.append(f"pbo {pbo:.3f} >= threshold {alpha_cfg.stats_pbo_threshold}")
    if adj_p >= alpha_cfg.stats_alpha:
        reasons.append(f"adjusted_p {adj_p:.3f} >= alpha {alpha_cfg.stats_alpha}")
    if reasons:
        return False, reasons
    return True, [
        f"deflated_sharpe {dsr:.3f} > {alpha_cfg.stats_dsr_threshold}",
        f"pbo {pbo:.3f} < {alpha_cfg.stats_pbo_threshold}",
        f"adjusted_p {adj_p:.3f} < {alpha_cfg.stats_alpha}",
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Alpha Engine model registry CLI (ADR-031).")
    parser.add_argument(
        "--prefix",
        default=os.environ.get("DYNAMODB_TABLE_PREFIX", "quantembrace-development"),
        help="DynamoDB table prefix (default: env DYNAMODB_TABLE_PREFIX).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List models + champion/challenger.")
    p_list.add_argument("--models", default="", help="Comma-separated model ids (default: config).")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="Show all versions of a model.")
    p_show.add_argument("model")
    p_show.set_defaults(func=_cmd_show)

    p_reg = sub.add_parser("register-version", help="Register a new SHADOW version.")
    p_reg.add_argument("model")
    p_reg.add_argument("--version", required=True)
    p_reg.add_argument("--alpha-family", required=True)
    p_reg.add_argument("--hypothesis", required=True)
    p_reg.add_argument("--experiment-id", required=True)
    p_reg.add_argument("--change-summary", required=True)
    p_reg.add_argument("--parent", default=None, help="parent as model@version or version.")
    p_reg.add_argument("--notes", default="")
    p_reg.set_defaults(func=_cmd_register)

    p_status = sub.add_parser("set-status", help="Transition a version's status.")
    p_status.add_argument("model")
    p_status.add_argument("--version", required=True)
    p_status.add_argument("--status", required=True)
    p_status.add_argument("--reason", required=True)
    p_status.add_argument("--decided-by", required=True)
    p_status.add_argument("--override-stats", default="", help="Reason to bypass the stats gate.")
    p_status.set_defaults(func=_cmd_set_status)

    p_champ = sub.add_parser("set-champion", help="Set the live champion version.")
    p_champ.add_argument("model")
    p_champ.add_argument("--version", required=True)
    p_champ.add_argument("--reason", required=True)
    p_champ.add_argument("--decided-by", required=True)
    p_champ.set_defaults(func=_cmd_set_champion)

    p_chal = sub.add_parser("set-challenger", help="Set the challenger version.")
    p_chal.add_argument("model")
    p_chal.add_argument("--version", required=True)
    p_chal.add_argument("--reason", required=True)
    p_chal.add_argument("--decided-by", required=True)
    p_chal.set_defaults(func=_cmd_set_challenger)

    p_val = sub.add_parser("validate-promotion", help="Run the statistical promotion gate.")
    p_val.add_argument("model")
    p_val.add_argument("--version", required=True)
    p_val.set_defaults(func=_cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = _store(args.prefix)
    return args.func(store, args)


if __name__ == "__main__":
    raise SystemExit(main())
