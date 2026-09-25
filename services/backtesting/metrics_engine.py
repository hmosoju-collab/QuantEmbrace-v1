"""Metrics engine for the QuantEmbrace backtesting lab.

Computes the full `docs/backtesting/metrics-catalog.md` set from a canonical
trades DataFrame and maps the result to the **live-readiness gates**
(expectancy > 0, profit factor > 1.2, realized/net P&L > 0). Pure computation —
no I/O, no broker APIs. Backtest-only; results are advisory.

Canonical trades DataFrame columns (extras ignored):
    symbol, strategy, direction, entry_time, exit_time, entry_price, exit_price,
    quantity, gross_pnl, costs, slippage, net_pnl, exit_reason,
    mfe_r, mae_r, r_multiple, mis_dependent
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

# Live-readiness gate thresholds (mirrors CLAUDE.md § Strategy Performance rule).
GATE_EXPECTANCY_MIN = 0.0
GATE_PROFIT_FACTOR_MIN = 1.2
GATE_NET_PNL_MIN = 0.0

# Annualised risk-free rate assumption (NSE / Indian equity context).
_RF_ANNUAL = 0.06          # 6%
_TRADING_DAYS_PER_YEAR = 252
_RF_DAILY = _RF_ANNUAL / _TRADING_DAYS_PER_YEAR


@dataclass
class RunMeta:
    run_id: str
    strategy: str = ""
    symbols: tuple[str, ...] = ()
    start_date: str = ""
    end_date: str = ""
    code_version: str = "unknown"
    data_version: str = "unknown"
    cost_model_version: str = "unknown"
    exit_policy_version: str = "unknown"
    status: str = "COMPLETED"
    error_reason: str | None = None
    initial_capital: float = 1_000_000.0


def _safe_mean(series: pd.Series) -> float:
    return float(series.mean()) if len(series) else 0.0


def compute_metrics(
    trades: pd.DataFrame,
    *,
    equity_curve: pd.DataFrame | None = None,
    initial_capital: float = 1_000_000.0,
    period_seconds: float | None = None,
) -> dict:
    """Compute the full metrics catalog from a trades DataFrame."""
    m: dict = {
        "number_of_trades": int(len(trades)),
        "gross_pnl": 0.0,
        "net_pnl": 0.0,
        "total_return_pct": 0.0,
        "annualised_return_pct": 0.0,
        "cost_impact": 0.0,
        "total_slippage": 0.0,
        "win_rate": 0.0,
        "avg_winner": 0.0,
        "avg_loser": 0.0,
        "largest_win": 0.0,
        "largest_loss": 0.0,
        "payoff_ratio": 0.0,
        "profit_factor": 0.0,
        "expectancy": 0.0,
        "max_consecutive_losses": 0,
        "sharpe_ratio": 0.0,
        "sortino_ratio": 0.0,
        "max_drawdown_pct": 0.0,
        "max_drawdown_abs": 0.0,
        "daily_drawdown_pct": 0.0,
        "monthly_pnl": {},
        "turnover": 0.0,
        "exposure_seconds": 0.0,
        "exposure_pct": 0.0,
        "mis_dependency": 0.0,
        "avg_mfe_r": 0.0,
        "avg_mae_r": 0.0,
        "profit_capture_ratio": 0.0,
        "lookahead_violations": 0,   # enforced by replay_engine; always 0
    }
    if trades.empty:
        m["gates"] = evaluate_gates(m)
        return m

    net = trades["net_pnl"].astype(float)
    gross = trades["gross_pnl"].astype(float) if "gross_pnl" in trades else net
    costs = trades["costs"].astype(float) if "costs" in trades else pd.Series([0.0] * len(trades))
    slip = trades["slippage"].astype(float) if "slippage" in trades else pd.Series([0.0] * len(trades))

    wins = net[net > 0]
    losses = net[net <= 0]

    m["gross_pnl"] = float(gross.sum())
    m["net_pnl"] = float(net.sum())
    m["total_return_pct"] = m["net_pnl"] / initial_capital * 100.0 if initial_capital else 0.0
    m["cost_impact"] = float(costs.sum())
    m["total_slippage"] = float(slip.sum())
    m["win_rate"] = len(wins) / len(net) * 100.0
    m["avg_winner"] = _safe_mean(wins)
    m["avg_loser"] = _safe_mean(losses)
    m["largest_win"] = float(wins.max()) if len(wins) else 0.0
    m["largest_loss"] = float(losses.min()) if len(losses) else 0.0
    m["payoff_ratio"] = (m["avg_winner"] / abs(m["avg_loser"])) if m["avg_loser"] < 0 else float("inf") if m["avg_winner"] > 0 else 0.0
    gross_profit = float(wins.sum())
    gross_loss = float(abs(losses.sum()))
    m["profit_factor"] = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    m["expectancy"] = float(net.mean())
    m["max_consecutive_losses"] = _max_consecutive_losses(net)

    # Turnover.
    if {"entry_price", "exit_price", "quantity"} <= set(trades.columns):
        q = trades["quantity"].abs().astype(float)
        m["turnover"] = float((trades["entry_price"].astype(float) * q).sum()
                              + (trades["exit_price"].astype(float) * q).sum())

    # Exposure time.
    if {"entry_time", "exit_time"} <= set(trades.columns):
        durations = (pd.to_datetime(trades["exit_time"]) - pd.to_datetime(trades["entry_time"]))
        m["exposure_seconds"] = float(durations.dt.total_seconds().clip(lower=0).sum())
        if period_seconds:
            m["exposure_pct"] = min(100.0, m["exposure_seconds"] / period_seconds * 100.0)

    # MIS dependency.
    if "mis_dependent" in trades:
        m["mis_dependency"] = float(trades["mis_dependent"].astype(bool).mean())

    # MFE / MAE / capture.
    if "mfe_r" in trades:
        m["avg_mfe_r"] = _safe_mean(trades["mfe_r"].astype(float))
    if "mae_r" in trades:
        m["avg_mae_r"] = _safe_mean(trades["mae_r"].astype(float))
    if "r_multiple" in trades and m["avg_mfe_r"] > 0:
        m["profit_capture_ratio"] = _safe_mean(trades["r_multiple"].astype(float)) / m["avg_mfe_r"]

    # Monthly P&L (by exit month).
    if "exit_time" in trades:
        em = pd.to_datetime(trades["exit_time"]).dt.strftime("%Y-%m")
        m["monthly_pnl"] = {k: float(v) for k, v in net.groupby(em).sum().items()}

    # Drawdown + risk-adjusted metrics (from equity curve, or built from cumulative net P&L).
    eq = equity_curve
    if eq is None:
        ordered = trades.sort_values("exit_time") if "exit_time" in trades else trades
        eq = pd.DataFrame({
            "timestamp": pd.to_datetime(ordered["exit_time"]) if "exit_time" in ordered else range(len(ordered)),
            "equity": initial_capital + ordered["net_pnl"].astype(float).cumsum().values,
        })
    dd_pct, dd_abs = _max_drawdown(eq["equity"].astype(float))
    m["max_drawdown_pct"] = dd_pct
    m["max_drawdown_abs"] = dd_abs
    m["daily_drawdown_pct"] = _daily_drawdown(eq)

    # CAGR — requires a known period length.
    if period_seconds and period_seconds > 0:
        calendar_years = period_seconds / (365.25 * 24 * 3600)
        final_equity = float(eq["equity"].iloc[-1]) if not eq.empty else initial_capital
        if calendar_years > 0 and initial_capital > 0:
            m["annualised_return_pct"] = (
                (final_equity / initial_capital) ** (1.0 / calendar_years) - 1.0
            ) * 100.0

    # Sharpe and Sortino from daily equity returns.
    m["sharpe_ratio"], m["sortino_ratio"] = _sharpe_sortino(eq)

    m["gates"] = evaluate_gates(m)
    return m


def _max_drawdown(equity: pd.Series) -> tuple[float, float]:
    if equity.empty:
        return 0.0, 0.0
    peak = equity.cummax()
    dd_abs_series = peak - equity
    dd_pct_series = dd_abs_series / peak.replace(0, math.nan) * 100.0
    return float(dd_pct_series.max() or 0.0), float(dd_abs_series.max() or 0.0)


def _daily_drawdown(eq: pd.DataFrame) -> float:
    if "timestamp" not in eq or eq.empty:
        return 0.0
    try:
        days = pd.to_datetime(eq["timestamp"]).dt.date
    except (TypeError, ValueError):
        return 0.0
    worst = 0.0
    for _, grp in eq.groupby(days):
        ddp, _ = _max_drawdown(grp["equity"].astype(float))
        worst = max(worst, ddp)
    return worst


def _max_consecutive_losses(net: pd.Series) -> int:
    """Count the longest losing streak (consecutive net_pnl <= 0 trades)."""
    best, current = 0, 0
    for v in net:
        if v <= 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _sharpe_sortino(eq: pd.DataFrame) -> tuple[float, float]:
    """Annualised Sharpe and Sortino from a daily-resampled equity curve.

    Returns (0.0, 0.0) when there are fewer than 2 distinct equity days.
    """
    if eq.empty or "timestamp" not in eq or "equity" not in eq:
        return 0.0, 0.0
    try:
        ts = pd.to_datetime(eq["timestamp"])
        daily = eq.copy()
        daily["date"] = ts.dt.date
        daily_eq = daily.groupby("date")["equity"].last()
    except Exception:
        return 0.0, 0.0

    if len(daily_eq) < 2:
        return 0.0, 0.0

    rets = daily_eq.pct_change().dropna()
    if rets.empty or rets.std() == 0:
        return 0.0, 0.0

    excess = rets - _RF_DAILY
    sharpe = float(excess.mean() / rets.std() * math.sqrt(_TRADING_DAYS_PER_YEAR))

    downside = rets[rets < 0]
    if downside.empty or downside.std() == 0:
        sortino = float("inf") if excess.mean() > 0 else 0.0
    else:
        sortino = float(excess.mean() / downside.std() * math.sqrt(_TRADING_DAYS_PER_YEAR))

    return sharpe, sortino


def evaluate_gates(metrics: dict) -> dict:
    """Map metrics to the live-readiness gates (advisory only)."""
    expectancy_pass = metrics.get("expectancy", 0.0) > GATE_EXPECTANCY_MIN
    pf = metrics.get("profit_factor", 0.0)
    pf_pass = pf > GATE_PROFIT_FACTOR_MIN
    net_pass = metrics.get("net_pnl", 0.0) > GATE_NET_PNL_MIN
    return {
        "expectancy_gt_0": expectancy_pass,
        "profit_factor_gt_1_2": pf_pass,
        "net_pnl_gt_0": net_pass,
        "overall_pass": bool(expectancy_pass and pf_pass and net_pass),
        "note": "Advisory. A passing backtest is necessary but not sufficient for live — "
                "promotion still requires ≥5 valid paper sessions + operator sign-off.",
    }


def breakdowns(trades: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Return per-strategy, per-symbol, and per-exit-reason breakdown frames."""
    return {
        "strategy": _group_breakdown(trades, "strategy"),
        "symbol": _group_breakdown(trades, "symbol"),
        "exit_reason": _exit_reason_breakdown(trades),
    }


def _group_breakdown(trades: pd.DataFrame, key: str) -> pd.DataFrame:
    if trades.empty or key not in trades:
        return pd.DataFrame(columns=[key, "trades", "net_pnl", "win_rate", "profit_factor"])
    rows = []
    for val, g in trades.groupby(key):
        net = g["net_pnl"].astype(float)
        wins, losses = net[net > 0], net[net <= 0]
        gp, gl = float(wins.sum()), float(abs(losses.sum()))
        rows.append({
            key: val,
            "trades": int(len(g)),
            "net_pnl": float(net.sum()),
            "win_rate": len(wins) / len(net) * 100.0 if len(net) else 0.0,
            "profit_factor": (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0),
        })
    return pd.DataFrame(rows)


def _exit_reason_breakdown(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty or "exit_reason" not in trades:
        return pd.DataFrame(columns=["exit_reason", "count", "net_pnl", "share_pct"])
    rows = []
    total = len(trades)
    for val, g in trades.groupby("exit_reason"):
        rows.append({
            "exit_reason": val,
            "count": int(len(g)),
            "net_pnl": float(g["net_pnl"].astype(float).sum()),
            "share_pct": len(g) / total * 100.0,
        })
    return pd.DataFrame(rows).sort_values("count", ascending=False).reset_index(drop=True)
