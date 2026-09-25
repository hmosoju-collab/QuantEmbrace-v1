"""Verbatim port of the v1 returns-space walk-forward model
(`scripts/backtest/run_delivery_walkforward.py`) — the CROSS-CHECK model.

This is deliberately NOT the engine: the registered walk-forward (ADR-034/036,
Sharpe 1.40, 5/5 OOS years) was computed in returns space with an EW-pick
gross return and a name-turnover cost heuristic, funds filter only (no
dislocation filter), no integer shares. To reproduce and audit those numbers
the model must be preserved exactly. New research runs through the engine
(`qe.research.walkforward`); this module keeps the legacy yardstick honest.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from qe.costs import EquityDeliveryCosts
from qe.universe import drop_funds

ROUND_TRIP = EquityDeliveryCosts().round_trip_frac


def rebalance_rows(index: pd.DatetimeIndex) -> list[int]:
    """Row positions of the last trading day of each month."""
    months = index.to_period("M")
    rows = []
    for m in months.unique():
        mask = months == m
        rows.append(np.where(mask)[0][-1])
    return sorted(rows)


def regime_series(close: pd.DataFrame, turn: pd.DataFrame, sma: int) -> pd.Series:
    """Market proxy (EW top-100 by total-period turnover) above its SMA?"""
    top100 = drop_funds(turn.sum()).nlargest(100).index
    mkt_ret = close[top100].pct_change().mean(axis=1)
    mkt_idx = (1.0 + mkt_ret.fillna(0.0)).cumprod()
    sma_line = mkt_idx.rolling(sma).mean()
    return (mkt_idx > sma_line) & sma_line.notna()


@dataclass
class WFLegResult:
    label: str
    monthly_net: pd.Series  # indexed by rebalance (period-end) date
    cash_months: int


def run_delivery(
    close: pd.DataFrame,
    turn: pd.DataFrame,
    deliv: pd.DataFrame,
    regime: pd.Series | None,
    top_n: int,
    k: int,
    warmup_rows: int = 252,
) -> WFLegResult:
    idx = close.index
    rb = [r for r in rebalance_rows(idx) if r >= warmup_rows and r < len(idx) - 1]
    held: set[str] = set()
    rets, dates, cash_months = [], [], 0

    for j in range(len(rb) - 1):
        i, i_next = rb[j], rb[j + 1]
        risk_on = True if regime is None else bool(regime.iloc[i])

        if risk_on:
            liq = turn.iloc[i - 60 : i].median().dropna()
            valid_now = close.iloc[i].dropna().index
            liq = drop_funds(liq[liq.index.isin(valid_now)])
            univ = liq.sort_values(ascending=False).head(top_n).index.tolist()
            dlv = deliv[univ].iloc[i - 21 : i].mean().dropna()
            picks = set(dlv.sort_values(ascending=False).head(k).index) if len(dlv) >= k else set()
        else:
            picks = set()
            cash_months += 1

        if picks:
            p0 = close[list(picks)].iloc[i]
            p1 = close[list(picks)].ffill().iloc[i_next]
            gross = (p1 / p0 - 1.0).dropna().mean()
        else:
            gross = 0.0  # in cash

        # cost on name turnover between prior holdings and new target
        if held or picks:
            changed = len(picks.symmetric_difference(held))
            cost = (
                (changed / (2.0 * k)) * ROUND_TRIP
                if (held and picks)
                else (len(picks | held) / k) * ROUND_TRIP
            )
        else:
            cost = 0.0
        rets.append(gross - cost)
        dates.append(idx[i_next])
        held = picks

    label = "delivery+overlay" if regime is not None else "delivery"
    return WFLegResult(label, pd.Series(rets, index=dates), cash_months)


def run_benchmark(
    close: pd.DataFrame,
    turn: pd.DataFrame,
    regime: pd.Series | None,
    top_n: int,
    warmup_rows: int = 252,
) -> WFLegResult:
    idx = close.index
    rb = [r for r in rebalance_rows(idx) if r >= warmup_rows and r < len(idx) - 1]
    rets, dates, cash = [], [], 0
    for j in range(len(rb) - 1):
        i, i_next = rb[j], rb[j + 1]
        if regime is not None and not bool(regime.iloc[i]):
            rets.append(0.0)
            dates.append(idx[i_next])
            cash += 1
            continue
        liq = turn.iloc[i - 60 : i].median().dropna()
        valid = close.iloc[i].dropna().index
        liq = drop_funds(liq[liq.index.isin(valid)])
        univ = liq.sort_values(ascending=False).head(top_n).index.tolist()
        p0 = close[univ].iloc[i]
        p1 = close[univ].ffill().iloc[i_next]
        rets.append((p1 / p0 - 1.0).dropna().mean())
        dates.append(idx[i_next])
    label = "benchmark+overlay" if regime is not None else "benchmark"
    return WFLegResult(label, pd.Series(rets, index=dates), cash)
