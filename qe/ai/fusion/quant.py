"""The deterministic side of fusion: quant score, engine pick, hard flags."""

from collections.abc import Iterable
from dataclasses import dataclass

from qe.ai.fusion.config import HardRiskConfig
from qe.ai.tools import QuantSpec, ResearchDataAPI, quant_view, risk_metrics
from qe.universe import liquid_universe


@dataclass(frozen=True)
class QuantRow:
    symbol: str
    q: float | None  # 2·rank_pct - 1 of the book's factor; None = no factor score
    factor_score: float | None
    selected: bool  # in the engine's own basket at this date
    hard_flags: tuple[str, ...]


def hard_flags(
    api: ResearchDataAPI, symbol: str, universe: set[str], cfg: HardRiskConfig
) -> tuple[str, ...]:
    m = risk_metrics(api, symbol)
    flags = []
    if not m.has_price:
        flags.append("no_price")
    elif m.data_age_days is not None and m.data_age_days > cfg.max_data_age_days:
        flags.append(f"stale_data_{m.data_age_days}d")
    if symbol not in universe:
        flags.append("outside_liquid_universe")
    if cfg.max_vol_63d is not None and m.vol_63d is not None and m.vol_63d > cfg.max_vol_63d:
        flags.append("vol_above_cap")
    if (
        cfg.max_drawdown_252d is not None
        and m.max_dd_252d is not None
        and m.max_dd_252d < cfg.max_drawdown_252d
    ):
        flags.append("drawdown_beyond_cap")
    return tuple(flags)


def quant_rows(
    api: ResearchDataAPI, spec: QuantSpec, cfg: HardRiskConfig, extra_symbols: Iterable[str] = ()
) -> dict[str, QuantRow]:
    """One row per liquid-universe name with a factor score, plus any extra
    symbols (e.g. researched names outside the universe, which get flagged)."""
    view = quant_view(api, spec)
    ctx = api.context()
    universe = set(liquid_universe(ctx.close, ctx.turnover, ctx.now_pos, spec.top_n))
    basket = set(view.basket)
    rows = {}
    for sym in dict.fromkeys([*view.scores.index, *extra_symbols]):
        has = sym in view.scores.index
        rows[sym] = QuantRow(
            symbol=sym,
            q=float(view.q[sym]) if has else None,
            factor_score=float(view.scores[sym]) if has else None,
            selected=sym in basket,
            hard_flags=hard_flags(api, sym, universe, cfg),
        )
    return rows
