"""Cross-sectional factor book: long-only top-K equal-weight (ADR-034/035).

Semantics ported from `scripts/paper/run_delivery_paper_book._target_basket`:
  delivery  — trailing 21d mean delivery% (rows [i-21, i), excluding today)
  momentum  — 12-1 momentum close[i-21]/close[i-252] - 1 (skip last month)
within the top-N liquid universe, equal weight min((1-cash_buffer)/K, max_weight).
"""

from dataclasses import dataclass
from typing import Literal

from qe.strategy.base import Context
from qe.universe import liquid_universe

Factor = Literal["delivery", "momentum"]


@dataclass(frozen=True)
class FactorBookStrategy:
    factor: Factor = "delivery"
    top_n: int = 200
    k: int = 20
    max_weight: float = 0.08
    cash_buffer: float = 0.02

    @property
    def name(self) -> str:
        return f"factor-book-{self.factor}"

    def select_basket(self, ctx: Context) -> list[str]:
        i = ctx.now_pos
        univ = liquid_universe(ctx.close, ctx.turnover, i, self.top_n)
        if self.factor == "momentum":
            if i < 252:
                return []
            score = (ctx.close[univ].iloc[i - 21] / ctx.close[univ].iloc[i - 252] - 1.0).dropna()
        else:
            score = ctx.delivery[univ].iloc[i - 21 : i].mean().dropna()
        return score.sort_values(ascending=False).head(self.k).index.tolist()

    def rebalance(self, ctx: Context) -> dict[str, float]:
        basket = self.select_basket(ctx)
        w = min((1.0 - self.cash_buffer) / self.k, self.max_weight)
        return dict.fromkeys(basket, w)
