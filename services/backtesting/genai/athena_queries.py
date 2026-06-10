"""Athena query templates over backtest outputs (Glue-cataloged Parquet / registry).

Read-only analytical queries the GenAI layer (or an operator) runs to ground its
analysis in actual results. Parameterised with ``{db}`` and ``{run_id}`` etc.
Backtest-only; no writes, no DDL beyond catalog reads.
"""

from __future__ import annotations

ATHENA_QUERIES: dict[str, str] = {
    "top_runs_by_expectancy": (
        "SELECT run_id, strategy, expectancy, profit_factor, net_pnl "
        "FROM {db}.bt_run_metrics "
        "WHERE status = 'COMPLETED' "
        "ORDER BY expectancy DESC LIMIT {limit}"
    ),
    "gate_passing_runs": (
        "SELECT run_id, strategy, expectancy, profit_factor, net_pnl "
        "FROM {db}.bt_run_metrics "
        "WHERE expectancy > 0 AND profit_factor > 1.2 AND net_pnl > 0"
    ),
    "exit_reason_distribution": (
        "SELECT exit_reason, COUNT(*) AS n, SUM(net_pnl) AS net_pnl "
        "FROM {db}.bt_trades WHERE run_id = '{run_id}' "
        "GROUP BY exit_reason ORDER BY n DESC"
    ),
    "mis_dependency_by_strategy": (
        "SELECT strategy, AVG(CAST(mis_dependent AS double)) AS mis_dependency "
        "FROM {db}.bt_trades GROUP BY strategy ORDER BY mis_dependency DESC"
    ),
    "lookahead_violations_check": (
        "SELECT run_id, lookahead_violations FROM {db}.bt_run_metrics "
        "WHERE lookahead_violations > 0"
    ),
}


def render(name: str, **params: object) -> str:
    if name not in ATHENA_QUERIES:
        raise KeyError(f"Unknown query {name!r}. Available: {sorted(ATHENA_QUERIES)}")
    return ATHENA_QUERIES[name].format(**params)
