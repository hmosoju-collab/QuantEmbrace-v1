"""Equal-weight liquid-universe benchmark — ported from
`scripts/paper/replay_delivery_book_forward._ew_benchmark`.

The factor study's core lesson: most raw return is beta, so the honest
question is always alpha vs the equal-weight market over the SAME legs.
"""

from qe.data.panel import Panel
from qe.universe import drop_corp_action_dislocations, drop_funds


def ew_benchmark_return(panel: Panel, pos_a: int, pos_b: int, top_n: int) -> float:
    """EW simple return of the top-N liquid universe from row pos_a → pos_b,
    universe rebuilt at pos_a with the same filters as the book."""
    liq = panel.turnover.iloc[pos_a - 60 : pos_a].median().dropna()
    valid = panel.close.iloc[pos_a].dropna().index
    liq = drop_funds(liq[liq.index.isin(valid)])
    liq = drop_corp_action_dislocations(liq, panel.close, pos_a, lookback=126)
    univ = liq.sort_values(ascending=False).head(top_n).index.tolist()
    p0 = panel.close[univ].iloc[pos_a]
    p1 = panel.close[univ].ffill().iloc[pos_b]
    return float((p1 / p0 - 1.0).dropna().mean())


def buy_hold_benchmark_return(panel: Panel, symbol: str, pos_a: int, pos_b: int) -> float:
    """Simple buy-and-hold return of a single symbol from row pos_a → pos_b
    (ADR-041 P3 — the US book's benchmark is SPY, not a cross-sectional
    liquid-universe construction; NSE's ``ew_benchmark_return`` above is
    unaffected)."""
    p0 = float(panel.close[symbol].iloc[pos_a])
    p1 = float(panel.close[symbol].ffill().iloc[pos_b])
    return p1 / p0 - 1.0
