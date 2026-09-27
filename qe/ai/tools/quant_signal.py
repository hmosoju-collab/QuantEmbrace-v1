"""The quant view: the engine's own factor score, rank, and basket at as_of.

``factor_scores`` repeats the two score lines of
``FactorBookStrategy.select_basket`` (qe/strategy/factor_book.py) instead of
importing the strategy object, so qe.ai never touches trading-path code. The
equality is enforced at every rebalance row by
``tests/qe/ai/test_ai_tools_pit.py::test_engine_basket_parity`` — if the engine
formula changes, that test fails before research can drift from the book.
"""

from dataclasses import dataclass

import pandas as pd

from qe.ai.models import ComponentStatus
from qe.ai.tools.pit import ResearchDataAPI, ToolResult, evidence, unavailable
from qe.config import RunConfig
from qe.strategy.base import Context
from qe.universe import liquid_universe

TOOL = "quant"


@dataclass(frozen=True)
class QuantSpec:
    factor: str
    top_n: int
    k: int

    @classmethod
    def from_book(cls, book: RunConfig) -> "QuantSpec | None":
        s = book.strategy
        if s is None or s.kind != "factor_book":
            return None  # only cross-sectional factor books have a rank to annotate
        return cls(factor=s.factor, top_n=s.top_n, k=s.k)


def factor_scores(ctx: Context, factor: str, top_n: int) -> pd.Series:
    i = ctx.now_pos
    univ = liquid_universe(ctx.close, ctx.turnover, i, top_n)
    if factor == "momentum":
        if i < 252:
            return pd.Series(dtype=float)
        return (ctx.close[univ].iloc[i - 21] / ctx.close[univ].iloc[i - 252] - 1.0).dropna()
    return ctx.delivery[univ].iloc[i - 21 : i].mean().dropna()


def engine_basket(scores: pd.Series, k: int) -> list[str]:
    return scores.sort_values(ascending=False).head(k).index.tolist()


def quant_scores(scores: pd.Series) -> pd.Series:
    """q = 2·rank_pct - 1 in (-1, 1]: the cross-sectional position of each
    liquid-universe name on the book's own factor."""
    return 2.0 * scores.rank(pct=True) - 1.0


@dataclass(frozen=True)
class QuantView:
    scores: pd.Series
    q: pd.Series
    basket: tuple[str, ...]


def quant_view(api: ResearchDataAPI, spec: QuantSpec) -> QuantView:
    scores = factor_scores(api.context(), spec.factor, spec.top_n)
    return QuantView(scores, quant_scores(scores), tuple(engine_basket(scores, spec.k)))


def quant(
    api: ResearchDataAPI, symbol: str, spec: QuantSpec | None, view: QuantView | None
) -> ToolResult:
    if spec is None or view is None:
        return unavailable(TOOL, symbol, "book strategy has no cross-sectional factor")
    if symbol not in view.scores.index:
        return unavailable(TOOL, symbol, "not in the liquid universe / no factor score")
    facts = (
        ("factor_score", float(view.scores[symbol]), f"{spec.factor} factor raw score"),
        ("q_score", float(view.q[symbol]), "factor rank mapped to [-1, 1] (1 = best)"),
        ("in_basket", symbol in view.basket, f"in the book's top-{spec.k} basket"),
        ("universe_size", len(view.scores), "names with a factor score"),
    )
    return ToolResult(
        TOOL,
        symbol,
        ComponentStatus.OK,
        tuple(evidence(api, TOOL, n, v, s, symbol) for n, v, s in facts),
    )
