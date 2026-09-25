"""Cross-sectional universe construction: liquidity ranking + hygiene filters.

Ported verbatim from `scripts/backtest/run_factor_study` (`_is_fund`,
`_drop_funds`, `_drop_corp_action_dislocations`) — these filters are part of
the validated factor-study semantics and parity-critical for the forward books.
"""

import pandas as pd

# NSE ETFs/liquid funds trade in the EQ segment with trivially ~100% delivery%,
# so factor ranks would pick them up — but they are cash/index/commodity
# instruments, not stock conviction. Ticker-pattern filter (lake ISIN column
# is unpopulated; INF-prefix ISIN matching would be cleaner).
FUND_TOKENS = (
    "BEES",
    "ETF",
    "LIQUID",
    "SETF",
    "NIFTY",
    "SENSEX",
    "GSEC",
    "BHARATBOND",
    "IETF",
    "MAFANG",
    "MON100",
    "HNGSNG",
)


def is_fund(symbol: str) -> bool:
    s = str(symbol).upper()
    if any(t in s for t in FUND_TOKENS):
        return True
    return s.endswith("GOLD") or s.endswith("SILVER")


def drop_funds(s: pd.Series) -> pd.Series:
    return s[~s.index.to_series().map(is_fund).values]


def drop_corp_action_dislocations(
    s: pd.Series,
    close: pd.DataFrame,
    i: int,
    lookback: int = 63,
    week_threshold: float = -0.35,
) -> pd.Series:
    """Drop symbols with a ≥35% single-week drop in the past lookback days —
    that is a demerger/split/special-dividend dislocation, not a market return."""
    syms = [sym for sym in s.index if sym in close.columns]
    if not syms or i < lookback + 5:
        return s
    px = close[syms].iloc[max(0, i - lookback) : i + 1]
    weekly_ret = px.pct_change(5)
    dislocated = weekly_ret.min() < week_threshold
    clean = dislocated[~dislocated].index.tolist()
    return s[s.index.isin(clean)]


def liquid_universe(close: pd.DataFrame, turnover: pd.DataFrame, i: int, top_n: int) -> list[str]:
    """Top-N by 60d median turnover at row i, fund- and dislocation-filtered.

    Windows are [i-60, i) — strictly before the rebalance row, matching the
    validated factor-study/paper-book semantics.
    """
    liq = turnover.iloc[i - 60 : i].median().dropna()
    valid = close.iloc[i].dropna().index
    liq = drop_funds(liq[liq.index.isin(valid)])
    liq = drop_corp_action_dislocations(liq, close, i, lookback=126)
    return liq.sort_values(ascending=False).head(top_n).index.tolist()
