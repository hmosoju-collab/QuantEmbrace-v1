"""Point-in-time market regime for research overlays (F-10 correction).

``wf_v1.regime_series`` — a verbatim v1 parity anchor, kept unchanged — picks
its top-100 market proxy from TOTAL-period turnover (``turn.sum()``), so future
liquidity leaks into every past date. This version re-selects the proxy at each
month-end from trailing ``[i-60, i)`` median turnover among names with a price
at ``i`` (funds dropped, same as v1), and holds it for the following month, so
the regime on any date uses only information available on that date.
"""

import pandas as pd

from qe.research.wf_v1 import rebalance_rows
from qe.universe import drop_funds

LOOKBACK = 60  # same window as qe.universe.liquid_universe


def pit_regime_series(
    close: pd.DataFrame, turn: pd.DataFrame, sma: int, top_n: int = 100
) -> pd.Series:
    """PIT market proxy (EW top-N by trailing turnover) above its SMA?"""
    month_ends = [i for i in rebalance_rows(close.index) if i >= LOOKBACK]
    member = pd.DataFrame(False, index=close.index, columns=close.columns)
    for k, i in enumerate(month_ends):
        liq = turn.iloc[i - LOOKBACK : i].median().dropna()
        priced = close.iloc[i].dropna().index
        top = drop_funds(liq[liq.index.isin(priced)]).nlargest(top_n).index
        end = month_ends[k + 1] if k + 1 < len(month_ends) else len(close.index) - 1
        # chosen with data through day i; applies to returns from day i+1 on
        member.iloc[i + 1 : end + 1, member.columns.get_indexer(top)] = True
    mkt_ret = close.pct_change().where(member).mean(axis=1)
    mkt_idx = (1.0 + mkt_ret.fillna(0.0)).cumprod()
    sma_line = mkt_idx.rolling(sma).mean()
    return (mkt_idx > sma_line) & sma_line.notna()
