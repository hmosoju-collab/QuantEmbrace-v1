"""Deterministic risk evidence per symbol (volatility, drawdown, liquidity,
data freshness). The same numbers drive fusion's hard flags; an LLM's risk
narrative never does."""

from dataclasses import dataclass
import math

from qe.ai.models import ComponentStatus
from qe.ai.tools.pit import ResearchDataAPI, ToolResult, evidence, unavailable
from qe.universe import liquid_universe

TOOL = "risk"


@dataclass(frozen=True)
class RiskMetrics:
    has_price: bool
    data_age_days: int | None
    vol_63d: float | None
    max_dd_252d: float | None
    turnover_60d: float | None


def risk_metrics(api: ResearchDataAPI, symbol: str) -> RiskMetrics:
    ctx = api.context()
    if symbol not in ctx.close.columns:
        return RiskMetrics(False, None, None, None, None)
    px = ctx.close[symbol].dropna()
    if px.empty:
        return RiskMetrics(False, None, None, None, None)
    age = (api.decision_date - px.index[-1].date()).days
    rets = px.pct_change().iloc[-63:]
    vol = float(rets.std() * math.sqrt(252)) if len(px) > 63 else None
    win = px.iloc[-252:]
    max_dd = float((win / win.cummax() - 1.0).min()) if len(px) >= 20 else None
    turn = ctx.turnover[symbol].iloc[-60:].dropna() if symbol in ctx.turnover.columns else None
    t60 = float(turn.median()) if turn is not None and len(turn) else None
    return RiskMetrics(True, age, vol, max_dd, t60)


def risk(api: ResearchDataAPI, symbol: str, top_n: int = 200) -> ToolResult:
    m = risk_metrics(api, symbol)
    if not m.has_price:
        return unavailable(TOOL, symbol, "no price history")
    ctx = api.context()
    in_univ = symbol in liquid_universe(ctx.close, ctx.turnover, ctx.now_pos, top_n)
    facts = [
        ("data_age_days", m.data_age_days, "calendar days since the last valid price"),
        ("in_liquid_universe", in_univ, f"in the PIT liquid top-{top_n}"),
        ("vol_63d", m.vol_63d, "annualized 63-day volatility"),
        ("max_dd_252d", m.max_dd_252d, "max drawdown over the last 252 days"),
        ("turnover_60d", m.turnover_60d, "median daily traded value, 60 days"),
    ]
    return ToolResult(
        TOOL,
        symbol,
        ComponentStatus.OK,
        tuple(evidence(api, TOOL, n, v, s, symbol) for n, v, s in facts if v is not None),
    )
