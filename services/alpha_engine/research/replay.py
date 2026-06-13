"""AlphaReplayHarness — offline forecast generation + labeling for research.

Feeds historical Bars through the production-strategy AlphaModels (the exact same
adapters the live shadow service uses) to produce a forecasts DataFrame, then
labels them against a price history. Reuses the leakage-safe
``model_dataset_builder.forward_return`` so realized returns use ONLY future bars.
No new fill model is reimplemented.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Callable

import pandas as pd

from alpha_engine.cost.cost_model import CostModel
from alpha_engine.models.registry import MODEL_SPECS
from alpha_engine.models.strategy_alpha_adapter import StrategyAlphaAdapter

if TYPE_CHECKING:  # pragma: no cover
    from alpha_engine.models.alpha_model import AlphaModel
    from strategy_engine.strategies.base_strategy import Bar

_TIMEFRAME_TO_INTERVAL = {"1m": "minute", "5m": "5minute", "15m": "15minute"}


def build_offline_models(
    model_ids: list[str],
    model_version: str,
    *,
    symbols: list[str],
    horizons_minutes: list[int],
    universe_resolver: Callable[[str], str] | None = None,
) -> list["AlphaModel"]:
    """Build adapters for offline replay (no registry / champion-challenger)."""
    resolver = universe_resolver or (lambda _s: "UNKNOWN")
    models: list[AlphaModel] = []
    for model_id in model_ids:
        spec = MODEL_SPECS.get(model_id)
        if spec is None:
            continue
        strategy = spec.factory(model_id, symbols)
        models.append(
            StrategyAlphaAdapter(
                strategy,
                model_id=model_id,
                model_version=model_version,
                alpha_family=spec.alpha_family,
                timeframe=spec.timeframe,
                horizons_minutes=horizons_minutes,
                feature_keys=spec.feature_keys,
                universe_resolver=resolver,
            )
        )
    return models


async def replay_models(
    bars: list["Bar"], models: list["AlphaModel"], *, cost_model: CostModel | None = None
) -> pd.DataFrame:
    """Run bars (chronological) through models -> cost-applied forecasts DataFrame."""
    cost = cost_model or CostModel()
    records: list[dict] = []
    for bar in bars:
        for model in models:
            if _TIMEFRAME_TO_INTERVAL.get(model.timeframe) != bar.interval:
                continue
            for forecast in await model.on_bar(bar):
                records.append(cost.apply(forecast).to_dict())
    return pd.DataFrame(records)


def label_forecasts(forecasts: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Add ``realized_fwd_return_bps`` (RAW, unsigned) to each forecast.

    ``prices`` must have columns ``symbol``, ``timestamp``, ``price``. Realized
    return uses ONLY future bars (``forward_return``) — no lookahead.
    """
    from backtesting.model_dataset_builder import forward_return

    if forecasts.empty:
        out = forecasts.copy()
        out["realized_fwd_return_bps"] = pd.Series(dtype=float)
        return out

    price_lookup: dict[str, pd.Series] = {}
    for symbol, grp in prices.groupby("symbol"):
        series = grp.set_index(pd.to_datetime(grp["timestamp"]))["price"].sort_index()
        price_lookup[str(symbol)] = series

    realized: list[float | None] = []
    for _, row in forecasts.iterrows():
        series = price_lookup.get(str(row["symbol"]))
        ts = pd.to_datetime(row["decision_ts"])
        ret = forward_return(series, ts, timedelta(minutes=int(row["horizon_minutes"])))
        realized.append(None if ret is None else ret * 10_000.0)

    out = forecasts.copy()
    out["realized_fwd_return_bps"] = realized
    return out
