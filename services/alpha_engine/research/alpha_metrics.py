"""alpha_metrics — pure-pandas evaluation of alpha forecasts (ADR-031).

Operates on a "forecasts" DataFrame with (at least) these columns:
    model_id, model_version, symbol, universe, direction, horizon_minutes,
    forecast_return_bps, net_edge_bps, confidence, decision_ts,
    realized_fwd_return_bps   (the labeled forward return, in bps)
and optionally ``regime``, ``edge_band``, ``conflict_group_id``, ``alpha_family``.

No scipy dependency: Spearman rank-IC is Pearson correlation on ranks.
``direction`` is "BUY"/"SELL"; signed realized return = realized * dir_sign.
"""

from __future__ import annotations

import math

import pandas as pd

REALIZED_COL = "realized_fwd_return_bps"
DEFAULT_COST_BPS = 20.0


def dir_sign(direction: pd.Series) -> pd.Series:
    """+1 for BUY, -1 for SELL (case-insensitive)."""
    return direction.astype(str).str.upper().map({"BUY": 1.0, "SELL": -1.0}).fillna(0.0)


def signed_realized_bps(df: pd.DataFrame) -> pd.Series:
    """Realized return in the forecast's intended direction."""
    return df[REALIZED_COL].astype(float) * dir_sign(df["direction"])


def _labeled(df: pd.DataFrame) -> pd.DataFrame:
    return df[df[REALIZED_COL].notna()].copy()


def _spearman(a: pd.Series, b: pd.Series) -> float | None:
    if len(a) < 2:
        return None
    ra, rb = a.rank(), b.rank()
    if ra.nunique() < 2 or rb.nunique() < 2:
        return None
    return float(ra.corr(rb))


def rank_ic(
    df: pd.DataFrame,
    *,
    value_col: str = "net_edge_bps",
    group_by_day: bool = True,
) -> dict:
    """Cross-sectional Spearman rank-IC of ``value_col`` vs realized return.

    Computes per-group (per IST day by default) IC, then averages across groups
    and returns a t-stat for the mean. ``signed`` realized is used so a SELL with
    a negative price move counts as a correct forecast.
    """
    data = _labeled(df)
    if data.empty:
        return {"ic": None, "t_stat": None, "n_groups": 0, "n": 0}
    data = data.assign(_signed=signed_realized_bps(data))
    if group_by_day:
        data = data.assign(_grp=pd.to_datetime(data["decision_ts"]).dt.date)
    else:
        data = data.assign(_grp=0)

    ics: list[float] = []
    for _, grp in data.groupby("_grp"):
        ic = _spearman(grp[value_col], grp["_signed"])
        if ic is not None:
            ics.append(ic)
    if not ics:
        return {"ic": None, "t_stat": None, "n_groups": 0, "n": int(len(data))}

    s = pd.Series(ics)
    mean_ic = float(s.mean())
    t_stat = None
    if len(s) > 1 and s.std(ddof=1) > 0:
        t_stat = float(mean_ic / (s.std(ddof=1) / math.sqrt(len(s))))
    return {"ic": mean_ic, "t_stat": t_stat, "n_groups": int(len(s)), "n": int(len(data))}


def ic_decay_curve(df: pd.DataFrame, *, value_col: str = "net_edge_bps") -> pd.DataFrame:
    """Rank-IC per horizon — the online/offline IC-decay measurement (ADR-031 #1)."""
    rows = []
    for horizon, grp in df.groupby("horizon_minutes"):
        res = rank_ic(grp, value_col=value_col)
        rows.append({"horizon_minutes": int(horizon), "ic": res["ic"], "n": res["n"]})
    return pd.DataFrame(rows).sort_values("horizon_minutes").reset_index(drop=True)


def hit_rate(df: pd.DataFrame) -> float | None:
    data = _labeled(df)
    if data.empty:
        return None
    signed = signed_realized_bps(data)
    return float((signed > 0).mean())


def cost_adjusted_expectancy_bps(df: pd.DataFrame, *, cost_bps: float = DEFAULT_COST_BPS) -> float | None:
    data = _labeled(df)
    if data.empty:
        return None
    return float(signed_realized_bps(data).mean() - cost_bps)


def calibration_curve(df: pd.DataFrame, *, n_buckets: int = 5) -> pd.DataFrame:
    """Predicted-confidence bucket vs realized hit rate (ADR-031 #4)."""
    data = _labeled(df)
    if data.empty:
        return pd.DataFrame(columns=["bucket", "n", "mean_confidence", "hit_rate"])
    data = data.assign(_hit=(signed_realized_bps(data) > 0).astype(float))
    edges = [i / n_buckets for i in range(n_buckets + 1)]
    labels = [f"{edges[i]:.1f}-{edges[i + 1]:.1f}" for i in range(n_buckets)]
    data = data.assign(
        _bucket=pd.cut(data["confidence"].clip(0, 1), bins=edges, labels=labels, include_lowest=True)
    )
    rows = []
    for bucket, grp in data.groupby("_bucket", observed=True):
        rows.append(
            {
                "bucket": str(bucket),
                "n": int(len(grp)),
                "mean_confidence": float(grp["confidence"].mean()),
                "hit_rate": float(grp["_hit"].mean()),
            }
        )
    return pd.DataFrame(rows)


def edge_band_study(
    df: pd.DataFrame, *, bands: list[float] | None = None, cost_bps: float = DEFAULT_COST_BPS
) -> pd.DataFrame:
    """Expectancy + hit rate at each net-edge publication threshold — answers
    "is the 50bps floor right?" (ADR-031 rev 2)."""
    bands = bands or [20.0, 30.0, 40.0, 50.0]
    data = _labeled(df)
    rows = []
    for band in bands:
        sub = data[data["net_edge_bps"] >= band]
        rows.append(
            {
                "min_net_edge_bps": band,
                "n": int(len(sub)),
                "hit_rate": hit_rate(sub),
                "expectancy_bps": cost_adjusted_expectancy_bps(sub, cost_bps=cost_bps),
            }
        )
    return pd.DataFrame(rows)


def per_regime_breakdown(df: pd.DataFrame, *, regime_col: str = "regime") -> pd.DataFrame:
    if regime_col not in df.columns:
        return pd.DataFrame(columns=[regime_col, "n", "ic", "hit_rate"])
    rows = []
    for regime, grp in df.groupby(regime_col):
        rows.append(
            {
                regime_col: str(regime),
                "n": int(len(_labeled(grp))),
                "ic": rank_ic(grp)["ic"],
                "hit_rate": hit_rate(grp),
            }
        )
    return pd.DataFrame(rows)


def conflict_win_rates(df: pd.DataFrame) -> pd.DataFrame:
    """Per alpha-family win rate inside same-symbol opposite-direction conflicts.

    For each ``conflict_group_id``, the family whose signed realized return is
    highest "wins" the disagreement. Aggregates wins/appearances per family.
    """
    if "conflict_group_id" not in df.columns or "alpha_family" not in df.columns:
        return pd.DataFrame(columns=["alpha_family", "appearances", "wins", "win_rate"])
    data = _labeled(df)
    data = data[data["conflict_group_id"].notna()]
    if data.empty:
        return pd.DataFrame(columns=["alpha_family", "appearances", "wins", "win_rate"])
    data = data.assign(_signed=signed_realized_bps(data))

    appearances: dict[str, int] = {}
    wins: dict[str, int] = {}
    for _, grp in data.groupby("conflict_group_id"):
        families = grp["alpha_family"].unique()
        if len(families) < 2:
            continue
        for fam in families:
            appearances[fam] = appearances.get(fam, 0) + 1
        winner = grp.loc[grp["_signed"].idxmax(), "alpha_family"]
        wins[winner] = wins.get(winner, 0) + 1

    rows = [
        {
            "alpha_family": fam,
            "appearances": appearances[fam],
            "wins": wins.get(fam, 0),
            "win_rate": wins.get(fam, 0) / appearances[fam] if appearances[fam] else None,
        }
        for fam in sorted(appearances)
    ]
    return pd.DataFrame(rows)


def rolling_regime_ic(
    df: pd.DataFrame, *, regime: str, windows: list[int] | None = None, regime_col: str = "regime"
) -> pd.DataFrame:
    """Rolling IC of one regime over N-day windows — regime persistence (ADR-031 #10).

    A regime edge that decays *within* the regime (e.g. Bull IC 0.18 in Q1 ->
    0.01 in Q2) is a collapsed alpha even if the pooled IC looks fine.
    """
    windows = windows or [30, 60, 90]
    if regime_col not in df.columns:
        return pd.DataFrame(columns=["window_days", "ic", "n_days"])
    sub = _labeled(df[df[regime_col] == regime]).copy()
    if sub.empty:
        return pd.DataFrame(columns=["window_days", "ic", "n_days"])
    sub = sub.assign(_day=pd.to_datetime(sub["decision_ts"]).dt.date)
    last_day = max(sub["_day"])
    rows = []
    for w in windows:
        cutoff = pd.Timestamp(last_day) - pd.Timedelta(days=w)
        window_df = sub[pd.to_datetime(sub["_day"]) >= cutoff]
        res = rank_ic(window_df)
        rows.append({"window_days": w, "ic": res["ic"], "n_days": res["n_groups"]})
    return pd.DataFrame(rows)
