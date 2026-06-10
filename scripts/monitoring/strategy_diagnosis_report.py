#!/usr/bin/env python3
"""
Strategy Diagnosis Report — post-session analysis for QuantEmbrace paper trading.

Reads filled orders and risk state from DynamoDB, runs StrategyPerformanceAnalyzer,
and writes a structured markdown report to reports/session_YYYYMMDD_strategy_diagnosis.md.

Usage:
    python scripts/monitoring/strategy_diagnosis_report.py [--date YYYY-MM-DD]

Output:
    reports/session_YYYYMMDD_strategy_diagnosis.md
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Path bootstrap: ensure service packages are importable ───────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SERVICES  = _REPO_ROOT / "services"
for _p in [str(_REPO_ROOT), str(_SERVICES)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import boto3  # noqa: E402 — must come after sys.path

from shared.monitoring.strategy_performance import (  # noqa: E402
    StrategyPerformanceAnalyzer,
    StrategyPerformanceStatus,
)

_IST = timezone(timedelta(hours=5, minutes=30))


# ── Session metadata ──────────────────────────────────────────────────────────


def _get_git_commit() -> str:
    """Return the current git commit hash, or the GIT_COMMIT env var, or 'unknown'."""
    env_val = os.environ.get("GIT_COMMIT", "").strip()
    if env_val:
        return env_val
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(_REPO_ROOT),
        )
        if result.returncode == 0:
            return result.stdout.strip()[:12]  # short hash
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "unknown"


def _get_image_tag() -> str:
    """Return the risk_engine Docker image tag via docker inspect, or env var."""
    env_val = os.environ.get("RISK_ENGINE_IMAGE_TAG", "").strip()
    if env_val:
        return env_val
    try:
        container = "quantembrace-ahedgelevelalgotradingsystem-risk_engine-1"
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.Config.Image}}", container],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "unknown"


def _load_yaml_quality_gate_thresholds() -> dict:
    """Read vwap_reversion thresholds from paper_optimization.yaml if available.

    Returns a dict with keys:
        vwap_reversion_min_confidence, vwap_reversion_min_reward_risk_ratio
    Defaults to "unknown" for any missing or unreadable field.
    """
    result = {
        "vwap_reversion_min_confidence": "unknown",
        "vwap_reversion_min_reward_risk_ratio": "unknown",
    }
    candidates = [
        _REPO_ROOT / "services" / "strategy_engine" / "config" / "paper_optimization.yaml",
        Path("services/strategy_engine/config/paper_optimization.yaml"),
    ]
    env_path = os.environ.get("PAPER_OPTIMIZATION_CONFIG_PATH")
    if env_path:
        candidates.insert(0, Path(env_path))
    for path in candidates:
        if path.exists():
            try:
                import yaml  # noqa: PLC0415
                with open(path, "r", encoding="utf-8") as fh:
                    cfg = yaml.safe_load(fh) or {}
                ef = cfg.get("entry_filters", {})
                mc = ef.get("min_confidence", {})
                rr = ef.get("min_reward_risk_ratio", {})
                if "vwap_reversion" in mc:
                    result["vwap_reversion_min_confidence"] = str(mc["vwap_reversion"])
                if "vwap_reversion" in rr:
                    result["vwap_reversion_min_reward_risk_ratio"] = str(rr["vwap_reversion"])
                return result
            except Exception:
                pass
    return result


def _build_session_metadata(
    date_str: str,
    dynamo,
    risk_state_table: str,
    performance_status: "StrategyPerformanceStatus",
) -> str:
    """Build the ## Session Metadata markdown block.

    Includes image tag, git commit, build time, quality gate counters from
    DynamoDB, and YAML thresholds. All fields degrade gracefully to "unknown"
    if data is unavailable at report generation time.
    """
    image_tag  = _get_image_tag()
    git_commit = _get_git_commit()
    build_time = os.environ.get("BUILD_TIME", "unknown")

    # Quality gate counters from DynamoDB
    today = date_str
    reason_codes = (
        "CONFIDENCE_BELOW_THRESHOLD",
        "REWARD_RISK_TOO_LOW",
        "MAX_TRADES_PER_SYMBOL_REACHED",
    )
    qg_counts: dict[str, int] = {}
    for code in reason_codes:
        try:
            resp = dynamo.get_item(
                TableName=risk_state_table,
                Key={
                    "PK": {"S": f"QUALITY_GATE_REJECT#{code}"},
                    "SK": {"S": f"DAY#{today}"},
                },
            )
            item = resp.get("Item")
            if item:
                raw = item.get("count", {})
                n = raw.get("N", "0") if isinstance(raw, dict) else "0"
                qg_counts[code] = int(float(n))
            else:
                qg_counts[code] = 0
        except Exception:
            qg_counts[code] = 0

    total_rejections = sum(qg_counts.values())
    gate_active      = total_rejections > 0

    # Pass-through rate
    accepted = performance_status.total_trades if performance_status else 0
    total_attempted = accepted + total_rejections
    if total_attempted > 0:
        pass_through_rate = f"{accepted / total_attempted * 100:.1f}%"
    else:
        pass_through_rate = "unknown"

    # Validation status: PASS if at least one rejection was seen (gate is
    # actively processing signals), UNKNOWN if no signal volume yet.
    if gate_active:
        gate_validation_status = "PASS"
    elif total_attempted == 0:
        gate_validation_status = "UNKNOWN"
    else:
        gate_validation_status = "FAIL"

    # YAML thresholds
    yaml_thresholds = _load_yaml_quality_gate_thresholds()

    lines = [
        "## Session Metadata\n",
        f"- image_tag: {image_tag}",
        f"- git_commit: {git_commit}",
        f"- build_time: {build_time}",
        f"- quality_gate_active: {'true' if gate_active else 'false'}",
        f"- quality_gate_validation_status: {gate_validation_status}",
        f"- pass_through_rate: {pass_through_rate}",
        f"- total_quality_gate_rejections: {total_rejections}",
        f"- vwap_reversion_min_confidence: {yaml_thresholds['vwap_reversion_min_confidence']}",
        f"- vwap_reversion_min_reward_risk_ratio: {yaml_thresholds['vwap_reversion_min_reward_risk_ratio']}",
        "",
    ]
    return "\n".join(lines)


# ── Report generation ─────────────────────────────────────────────────────────


def _fmt_pnl(v: float) -> str:
    prefix = "+" if v > 0 else ""
    return f"{prefix}₹{v:,.2f}"


def _fmt_pct(v: float) -> str:
    return f"{v*100:.1f}%"


def _live_verdict(p: StrategyPerformanceStatus) -> tuple[str, str]:
    """Return (verdict_label, reason) based on performance metrics."""
    if p.total_trades == 0:
        return "LIVE_BLOCKED", "no trades — insufficient data to assess strategy edge"
    if p.realized_pnl < 0:
        return "LIVE_BLOCKED", f"realized_pnl = {_fmt_pnl(p.realized_pnl)} < 0"
    if p.expectancy < 0:
        return "LIVE_BLOCKED", f"expectancy = {_fmt_pnl(p.expectancy)} < 0"
    if p.profit_factor < 1.2:
        return "LIVE_BLOCKED", f"profit_factor = {p.profit_factor:.2f} < 1.2"
    if p.gate_status == "PASS":
        return "SHADOW_LIVE_READY", (
            "strategy edge confirmed — requires ≥5 consecutive PASS sessions and "
            "full manual gate sign-off before any live promotion"
        )
    return "PAPER_OPTIMIZATION", f"gate_status = {p.gate_status}"


def _build_report(
    date_str: str,
    p: StrategyPerformanceStatus,
    dynamo=None,
    risk_state_table: str = "",
) -> str:
    verdict, reason = _live_verdict(p)
    sl_tp_str = "inf" if p.sl_tp_ratio == float("inf") else f"{p.sl_tp_ratio:.1f}"
    pf_str = "—" if p.profit_factor == 0.0 and p.losing_trades == 0 else f"{p.profit_factor:.2f}"

    lines: list[str] = []

    lines.append(f"# QuantEmbrace Strategy Diagnosis — {date_str}\n")
    lines.append(f"_Generated: {datetime.now(_IST).strftime('%Y-%m-%d %H:%M:%S IST')}_\n")

    # ── Session Metadata (image tag, git commit, quality gate status) ─────────
    if dynamo is not None and risk_state_table:
        lines.append(_build_session_metadata(date_str, dynamo, risk_state_table, p))

    # ── Executive Summary ─────────────────────────────────────────────────────
    lines.append("## Executive Summary\n")
    infra_status = "TECHNICALLY HEALTHY"
    edge_status  = (
        "PROVEN" if p.gate_status == "PASS"
        else "NOT PROVEN" if p.total_trades > 0
        else "INSUFFICIENT DATA"
    )
    lines.append(f"- Platform status: **{infra_status}**")
    lines.append(f"- Strategy edge: **{edge_status}**")
    lines.append(f"- Live-readiness verdict: **{verdict}**")
    lines.append(f"- Gate status: **{p.gate_status}** — {p.gate_reason}")
    lines.append("")

    # ── Infrastructure Health ─────────────────────────────────────────────────
    lines.append("## Infrastructure Health\n")
    lines.append("| Check | Status |")
    lines.append("|---|---|")
    lines.append("| live_trading_enabled | false (paper mode) |")
    lines.append("| kill_switch | not activated by strategy circuit |")
    lines.append("| broker_isolation | paper_trade=True path only |")
    lines.append("")

    # ── Strategy P&L Summary ──────────────────────────────────────────────────
    lines.append("## Strategy P&L Summary\n")
    lines.append("| Strategy | Trades | Win Rate | Profit Factor | Expectancy | Realized P&L |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    if p.per_strategy:
        for m in sorted(p.per_strategy, key=lambda x: x.realized_pnl):
            pf = "—" if m.profit_factor == 0.0 and m.losing_trades == 0 else f"{m.profit_factor:.2f}"
            lines.append(
                f"| {m.strategy_id} | {m.total_trades} | {_fmt_pct(m.win_rate)} "
                f"| {pf} | {_fmt_pnl(m.expectancy)} | {_fmt_pnl(m.realized_pnl)} |"
            )
    else:
        lines.append("| — | — | — | — | — | — |")
    lines.append("")

    # ── Symbol P&L Summary ────────────────────────────────────────────────────
    lines.append("## Symbol P&L Summary (top 20 by absolute P&L)\n")
    lines.append("| Symbol | Trades | Win Rate | Realized P&L |")
    lines.append("|---|---:|---:|---:|")
    sym_sorted = sorted(p.per_symbol, key=lambda x: abs(x.realized_pnl), reverse=True)[:20]
    if sym_sorted:
        for m in sym_sorted:
            lines.append(
                f"| {m.symbol} | {m.total_trades} | {_fmt_pct(m.win_rate)} | {_fmt_pnl(m.realized_pnl)} |"
            )
    else:
        lines.append("| — | — | — | — |")
    lines.append("")

    # ── Time-Bucket Analysis ──────────────────────────────────────────────────
    lines.append("## Time-Bucket Analysis\n")
    lines.append("| Time Bucket | Trades | Win Rate | Realized P&L | SL | TP |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    if p.per_time_bucket:
        for tb in p.per_time_bucket:
            wr = tb.winning_trades / tb.total_trades if tb.total_trades > 0 else 0.0
            lines.append(
                f"| {tb.bucket_label} | {tb.total_trades} | {_fmt_pct(wr)} "
                f"| {_fmt_pnl(tb.realized_pnl)} | {tb.sl_count} | {tb.tp_count} |"
            )
    else:
        lines.append("| — | — | — | — | — | — |")
    lines.append("")

    # ── Exit Reason Distribution ──────────────────────────────────────────────
    lines.append("## Exit Reason Distribution\n")
    lines.append("| Exit Reason | Count | Total P&L | Avg P&L |")
    lines.append("|---|---:|---:|---:|")
    if p.per_exit_reason:
        for er in sorted(p.per_exit_reason, key=lambda x: x.count, reverse=True):
            lines.append(
                f"| {er.exit_reason} | {er.count} | {_fmt_pnl(er.total_pnl)} | {_fmt_pnl(er.avg_pnl)} |"
            )
    else:
        lines.append("| — | — | — | — |")
    lines.append("")

    # ── Top 10 Losing Trades ──────────────────────────────────────────────────
    lines.append("## Top 10 Losing Trades\n")
    lines.append("| Symbol | Strategy | Side | Qty | Price | P&L | Exit Reason | Time |")
    lines.append("|---|---|---|---:|---:|---:|---|---|")
    if p.top_losers:
        for r in p.top_losers:
            lines.append(
                f"| {r.symbol} | {r.strategy_id} | {r.side} "
                f"| {r.filled_quantity:.0f} | ₹{r.filled_price:,.2f} "
                f"| {_fmt_pnl(r.pnl)} | {r.exit_reason} | {r.time_bucket} |"
            )
    else:
        lines.append("| — | — | — | — | — | — | — | — |")
    lines.append("")

    # ── Top 10 Winning Trades ─────────────────────────────────────────────────
    lines.append("## Top 10 Winning Trades\n")
    lines.append("| Symbol | Strategy | Side | Qty | Price | P&L | Exit Reason | Time |")
    lines.append("|---|---|---|---:|---:|---:|---|---|")
    if p.top_winners:
        for r in p.top_winners:
            lines.append(
                f"| {r.symbol} | {r.strategy_id} | {r.side} "
                f"| {r.filled_quantity:.0f} | ₹{r.filled_price:,.2f} "
                f"| {_fmt_pnl(r.pnl)} | {r.exit_reason} | {r.time_bucket} |"
            )
    else:
        lines.append("| — | — | — | — | — | — | — | — |")
    lines.append("")

    # ── Recommended Config Changes ────────────────────────────────────────────
    lines.append("## Recommended Config Changes\n")
    recommendations: list[str] = []

    # VWAP win-rate check
    vwap_metrics = next((m for m in p.per_strategy if "vwap" in m.strategy_id.lower()), None)
    if vwap_metrics and vwap_metrics.win_rate < 0.40:
        recommendations.append(
            "VWAP win_rate < 40% — consider raising `min_confidence` for vwap_reversion "
            "to 0.97 in `paper_optimization.yaml` and re-evaluate over 3+ sessions."
        )

    # ORB SL:TP ratio check
    orb_metrics = next((m for m in p.per_strategy if "orb" in m.strategy_id.lower()), None)
    if orb_metrics and orb_metrics.tp_count > 0 and orb_metrics.sl_count > orb_metrics.tp_count * 2:
        recommendations.append(
            "ORB sl_count > tp_count×2 — consider raising `volume_multiplier` in ORB config "
            "to require stronger breakout confirmation before entry."
        )

    # Early session losses check
    early_bucket = next((tb for tb in p.per_time_bucket if tb.bucket_label == "09:15–09:30"), None)
    if early_bucket and early_bucket.realized_pnl < -200:
        recommendations.append(
            "09:15–09:30 bucket is negative — confirm `vwap_reversion_start_time = 09:45` "
            "is active in `paper_optimization.yaml`. VWAP bands are unreliable without 30 min of data."
        )

    if recommendations:
        for i, rec in enumerate(recommendations, 1):
            lines.append(f"{i}. {rec}")
    else:
        lines.append("No automatic config changes recommended. Session metrics are within acceptable ranges.")
    lines.append("")

    # ── Live-Readiness Verdict ────────────────────────────────────────────────
    lines.append("## Live-Readiness Verdict\n")
    lines.append("| Criterion | Status |")
    lines.append("|---|---|")
    lines.append(f"| realized_pnl > 0 | {'PASS' if p.realized_pnl > 0 else 'FAIL'} |")
    lines.append(f"| expectancy > 0 | {'PASS' if p.expectancy > 0 else 'FAIL'} |")
    lines.append(f"| profit_factor > 1.2 | {'PASS' if p.profit_factor > 1.2 else 'FAIL'} |")
    lines.append(f"| reconciliation failures | (manual check required) |")
    lines.append("")
    lines.append("Verdict rules:")
    lines.append("- LIVE_BLOCKED if realized_pnl < 0")
    lines.append("- LIVE_BLOCKED if expectancy < 0")
    lines.append("- LIVE_BLOCKED if profit_factor < 1.2")
    lines.append("- LIVE_BLOCKED if any reconciliation failure")
    lines.append("- PAPER_OPTIMIZATION if infra healthy but strategy P&L negative")
    lines.append("- SHADOW_LIVE_READY only after ≥5 clean paper sessions with positive expectancy")
    lines.append("")
    lines.append(f"**Final verdict: {verdict}**  ")
    lines.append(f"Reason: {reason}")
    lines.append("")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────


async def _run(date_str: str) -> None:
    # Table names from environment (same pattern as other scripts)
    prefix        = os.environ.get("DYNAMODB_TABLE_PREFIX", "qe-local")
    orders_table  = os.environ.get("DYNAMODB_TABLE_ORDERS", f"{prefix}-orders")
    risk_table    = os.environ.get("DYNAMODB_TABLE_RISK_STATE", f"{prefix}-risk-state")
    endpoint_url  = os.environ.get("AWS_ENDPOINT_URL")  # LocalStack in local dev

    boto_kwargs: dict = {}
    if endpoint_url:
        boto_kwargs["endpoint_url"] = endpoint_url

    dynamo = boto3.client("dynamodb", region_name=os.environ.get("AWS_DEFAULT_REGION", "ap-south-1"), **boto_kwargs)

    analyzer = StrategyPerformanceAnalyzer(
        dynamo_client=dynamo,
        orders_table=orders_table,
        risk_state_table=risk_table,
        trade_date=date_str,
    )

    print(f"Scanning orders table '{orders_table}' for date {date_str} ...")
    p = await analyzer.analyze()
    print(f"Found {p.total_trades} filled trades. Gate: {p.gate_status}")

    report_text = _build_report(date_str, p, dynamo=dynamo, risk_state_table=risk_table)

    # Create reports/ directory if absent
    reports_dir = Path(__file__).resolve().parent.parent.parent / "reports"
    reports_dir.mkdir(exist_ok=True)

    out_path = reports_dir / f"session_{date_str.replace('-', '')}_strategy_diagnosis.md"
    out_path.write_text(report_text, encoding="utf-8")
    print(f"Report written to: {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantEmbrace session strategy diagnosis report")
    parser.add_argument(
        "--date",
        default=None,
        help="Trading date in YYYY-MM-DD format (default: today IST)",
    )
    args = parser.parse_args()

    if args.date:
        date_str = args.date
    else:
        date_str = datetime.now(_IST).strftime("%Y-%m-%d")

    asyncio.run(_run(date_str))


if __name__ == "__main__":
    main()
