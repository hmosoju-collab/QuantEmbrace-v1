"""Session report — derived purely from the event journal (RA-1 §2.4).

The journal is the single source of truth, so monitoring, the post-session
report, trade replay, and decision replay are all just readers of it. This
replaces the v1 LiveCounters JSON + monitor scripts: there is nothing to keep
in sync because there is only one artifact.
"""

from pathlib import Path
from typing import Any

from qe.journal import read_journal

# NAV currency symbol by market (ADR-041 P3 — USD NAV for the US book).
_MARKET_CURRENCY = {"NSE": "₹", "US": "$"}


def session_report(journal_path: str | Path) -> dict[str, Any]:
    """Reconstruct a session's outcome from its journal records."""
    records = list(read_journal(journal_path))
    by_type: dict[str, list[dict]] = {}
    for r in records:
        by_type.setdefault(r["type"], []).append(r["data"])

    start = by_type.get("SESSION_START", [{}])[0]
    end = by_type.get("SESSION_END", [{}])
    end = end[0] if end else {}

    orders = [o for rb in by_type.get("REBALANCE", []) for o in rb.get("orders", [])]
    return {
        "session_id": start.get("session_id"),
        "mode": start.get("mode"),
        "config": start.get("config", {}),
        "config_hash": start.get("config_hash"),
        "code_sha": start.get("code_sha"),
        "data_snapshot_id": start.get("data_snapshot_id"),
        "status": end.get("status"),
        "n_records": len(records),
        "rebalances": len(by_type.get("REBALANCE", [])),
        "risk_rejections": sum(1 for d in by_type.get("RISK", []) if not d.get("approved")),
        "kill_triggered": len(by_type.get("KILL_TRIGGERED", [])),
        "kill_blocked": len(by_type.get("KILL_BLOCKED", [])),
        "orders": len(orders),
        "buys": sum(1 for o in orders if o.get("side") == "BUY"),
        "sells": sum(1 for o in orders if o.get("side") == "SELL"),
        "final": end,
        "complete": bool(end),  # a session with no SESSION_END aborted mid-flight
    }


def render_session_report(journal_path: str | Path) -> str:
    r = session_report(journal_path)
    final = r["final"]
    lines = [
        f"QuantEmbrace session report — {r['session_id']}",
        f"  mode              {r['mode']}   status {r['status'] or 'INCOMPLETE'}",
        f"  config hash       {r['config_hash']}",
        f"  code / snapshot   {r['code_sha']} / {r['data_snapshot_id']}",
        f"  records           {r['n_records']} (complete={r['complete']})",
        f"  rebalances        {r['rebalances']}   orders {r['orders']} "
        f"(buys {r['buys']} / sells {r['sells']})",
        f"  risk rejections   {r['risk_rejections']}",
        f"  kill triggered    {r['kill_triggered']}   kill-blocked emissions {r['kill_blocked']}",
    ]
    if final:
        nav = final.get("nav") or final.get("final_nav")
        if nav is not None:
            market = r.get("config", {}).get("universe", {}).get("market", "NSE")
            symbol = _MARKET_CURRENCY.get(market, "")
            lines.append(f"  NAV               {symbol}{nav:,.2f}")
        if "rebalanced" in final:
            lines.append(f"  rebalanced today  {final['rebalanced']}   due {final.get('due')}")
    lines.append("  (advisory · derived from journal · live trading BLOCKED)")
    return "\n".join(lines)
