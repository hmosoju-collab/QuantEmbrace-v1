"""Monthly-return metrics — exact ports of the v1 factor-study functions
(`run_factor_study._metrics`, `run_delivery_walkforward._per_year`), so v2
studies report the same numbers the registered studies reported.
"""

from datetime import date

import numpy as np
import pandas as pd


def monthly_metrics(monthly: pd.Series) -> dict:
    """CAGR / vol / Sharpe / MaxDD / hit-rate over a monthly net-return series."""
    if monthly.empty:
        return {"cagr": 0.0, "vol": 0.0, "sharpe": 0.0, "maxdd": 0.0, "hit": 0.0, "months": 0}
    eq = (1.0 + monthly).cumprod()
    years = len(monthly) / 12.0
    cagr = eq.iloc[-1] ** (1.0 / years) - 1.0 if years > 0 and eq.iloc[-1] > 0 else -1.0
    vol = monthly.std() * np.sqrt(12)
    sharpe = (monthly.mean() * 12) / vol if vol else 0.0
    dd = (eq / eq.cummax() - 1.0).min()
    hit = (monthly > 0).mean()
    return {
        "cagr": float(cagr),
        "vol": float(vol),
        "sharpe": float(sharpe),
        "maxdd": float(dd),
        "hit": float(hit),
        "months": len(monthly),
    }


def per_year(monthly: pd.Series) -> pd.DataFrame:
    """Per-calendar-year OOS breakdown (return, vol, maxdd, sharpe, months)."""
    df = pd.DataFrame({"r": monthly})
    df["year"] = df.index.year
    rows = []
    for y, g in df.groupby("year"):
        eq = (1.0 + g["r"]).cumprod()
        ann = eq.iloc[-1] - 1.0
        vol = g["r"].std() * np.sqrt(12)
        dd = (eq / eq.cummax() - 1.0).min()
        rows.append(
            {
                "year": y,
                "return": ann,
                "vol": vol,
                "maxdd": dd,
                "sharpe": (g["r"].mean() * 12 / vol) if vol else 0.0,
                "months": len(g),
            }
        )
    return pd.DataFrame(rows).set_index("year")


def with_walk_forward_stats(metrics: dict, py: pd.DataFrame) -> dict:
    """Augment full-period metrics with the walk-forward gate metrics."""
    n_years = len(py)
    pos_years = int((py["return"] > 0).sum())
    return {
        **metrics,
        "n_years": n_years,
        "positive_years": pos_years,
        "per_year_positive_frac": (pos_years / n_years) if n_years else 0.0,
        "worst_year_return": float(py["return"].min()) if n_years else 0.0,
    }


def nav_monthly_returns(nav_history: list[tuple[date, float]]) -> pd.Series:
    """Complete-month net returns from consecutive rebalance NAVs, indexed by
    the later rebalance date (the final partial-month MTM leg is not included
    here — pass only rebalance NAVs)."""
    if len(nav_history) < 2:
        return pd.Series(dtype=float)
    dates = [pd.Timestamp(d) for d, _ in nav_history[1:]]
    rets = [nav_history[i + 1][1] / nav_history[i][1] - 1.0 for i in range(len(nav_history) - 1)]
    return pd.Series(rets, index=pd.DatetimeIndex(dates))
