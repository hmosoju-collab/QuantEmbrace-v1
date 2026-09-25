"""Technical evidence from the PIT close/turnover/delivery panel.

Close-only indicators (the qe panel carries no open/high/low, so no ATR). All
windows end at the decision row; nothing here can see a later bar.
"""

import math

from qe.ai.models import ComponentStatus
from qe.ai.tools.pit import ResearchDataAPI, ToolResult, evidence, unavailable

TOOL = "tech"


def _ret(px, n: int) -> float | None:
    return float(px.iloc[-1] / px.iloc[-1 - n] - 1.0) if len(px) > n else None


def _rsi(px, n: int = 14) -> float | None:
    """Simple-average RSI (Cutler) over the last n daily changes."""
    if len(px) <= n:
        return None
    d = px.diff().iloc[-n:]
    gain, loss = d.clip(lower=0).mean(), (-d.clip(upper=0)).mean()
    if loss == 0:
        return 100.0
    return float(100 - 100 / (1 + gain / loss))


def technical(api: ResearchDataAPI, symbol: str) -> ToolResult:
    ctx = api.context()
    if symbol not in ctx.close.columns:
        return unavailable(TOOL, symbol, "symbol not in panel")
    px = ctx.close[symbol].dropna()
    if px.empty or px.index[-1] != ctx.close.index[-1]:
        return unavailable(TOOL, symbol, "no price at the decision date")

    facts: list[tuple[str, float | None, str]] = []
    for n in (21, 63, 252):
        r = _ret(px, n)
        facts.append((f"ret_{n}d", r, f"{n}-trading-day return"))
    for n in (50, 200):
        gap = float(px.iloc[-1] / px.iloc[-n:].mean() - 1.0) if len(px) >= n else None
        facts.append((f"sma{n}_gap", gap, f"price vs {n}-day simple moving average"))
    facts.append(("rsi14", _rsi(px), "14-day RSI (0-100)"))
    rets = px.pct_change().iloc[-63:]
    vol = float(rets.std() * math.sqrt(252)) if len(px) > 63 else None
    facts.append(("vol_63d", vol, "annualized 63-day volatility"))
    dd = float(px.iloc[-1] / px.iloc[-252:].max() - 1.0) if len(px) >= 252 else None
    facts.append(("dd_252d", dd, "drawdown from 252-day high"))
    if symbol in ctx.delivery.columns:
        dv = ctx.delivery[symbol].iloc[-21:].dropna()
        facts.append(
            ("delivery_21d", float(dv.mean()) if len(dv) else None, "21-day mean delivery %")
        )

    ev = tuple(
        evidence(api, TOOL, name, value, summary, symbol)
        for name, value, summary in facts
        if value is not None and not math.isnan(value)
    )
    if not ev:
        return unavailable(TOOL, symbol, "insufficient history")
    return ToolResult(TOOL, symbol, ComponentStatus.OK, ev)
