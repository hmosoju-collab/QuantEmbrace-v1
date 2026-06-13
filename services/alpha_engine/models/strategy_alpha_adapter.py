"""StrategyAlphaAdapter — wraps a production BaseStrategy as an AlphaModel.

Mirrors ORB v2 / VWAP v2 / trend_15m into the forecast world with ZERO changes to
strategy_engine (``prefer_refactor_over_rewrite``). On each bar it drives the
wrapped strategy and, if it emits a Signal, fans out one ``AlphaForecast`` per
configured horizon (ADR-031 #1). The forecast's raw return is the strategy's own
viability-passed take-profit distance in bps; quantity/stops/sizing are discarded
(shadow mode owns no trade semantics).

``top_features`` (ADR-031 #16) is extracted from the strategy's own decision
metadata using a per-model key list — heuristic-v1 explainability.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from alpha_engine.models.alpha_model import AlphaModel
from shared.logging.logger import get_logger
from shared.models.alpha import AlphaForecast, FeatureContribution
from shared.models.signal import Signal

if TYPE_CHECKING:  # pragma: no cover
    from strategy_engine.strategies.base_strategy import Bar, BaseStrategy

logger = get_logger(__name__, service_name="alpha_engine")

# Metadata keys, per strategy, that are the genuine decision drivers (explainability).
_DEFAULT_FEATURE_KEYS: dict[str, list[str]] = {
    "momentum": ["vol_ratio", "or_range", "adx"],
    "reversal": ["reward_risk", "band_std", "atr"],
    "quality": ["adx", "atr"],
    "regime_filter": ["adx", "atr"],
}


class StrategyAlphaAdapter(AlphaModel):
    def __init__(
        self,
        strategy: BaseStrategy,
        *,
        model_id: str,
        model_version: str,
        alpha_family: str,
        timeframe: str,
        horizons_minutes: list[int],
        feature_keys: list[str] | None = None,
        universe_resolver: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(
            model_id=model_id,
            model_version=model_version,
            alpha_family=alpha_family,
            market=getattr(strategy, "market", "NSE"),
            timeframe=timeframe,
        )
        self._strategy = strategy
        self._horizons = list(horizons_minutes)
        self._feature_keys = feature_keys or _DEFAULT_FEATURE_KEYS.get(alpha_family, [])
        self._resolve_universe = universe_resolver or (lambda _s: "UNKNOWN")

    async def initialize(self, saved_state=None) -> None:
        init = getattr(self._strategy, "initialize", None)
        if init is not None:
            await init(saved_state)

    async def on_bar(self, bar: Bar) -> list[AlphaForecast]:
        await self._strategy.on_bar(bar)
        signal = await self._strategy.generate_signal()
        if signal is None:
            return []
        return self._to_forecasts(signal, bar)

    def _to_forecasts(self, signal: Signal, bar: Bar) -> list[AlphaForecast]:
        price = signal.price_at_signal
        if not price or signal.take_profit is None:
            return []
        raw_bps = abs(signal.take_profit - price) / price * 10_000.0
        top_features = tuple(
            FeatureContribution(k, float(signal.metadata[k]))
            for k in self._feature_keys
            if isinstance(signal.metadata.get(k), (int, float))
        )
        universe = self._resolve_universe(signal.symbol)
        # decision_ts is the bar close time (deterministic across replays), NOT
        # the Signal's wall-clock generated_at — this keeps forecast_id idempotent.
        decision_ts = bar.timestamp
        carried = {
            "strategy_version": signal.metadata.get("strategy_version"),
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            **{k: v for k, v in signal.metadata.items() if k.startswith("viability_")},
        }

        forecasts: list[AlphaForecast] = []
        for horizon in self._horizons:
            forecasts.append(
                AlphaForecast(
                    model_id=self.model_id,
                    model_version=self.model_version,
                    alpha_family=self.alpha_family,
                    symbol=signal.symbol,
                    market=signal.market,
                    universe=universe,
                    direction=signal.direction,
                    timeframe=self.timeframe,
                    horizon_minutes=horizon,
                    forecast_return_bps=raw_bps,
                    confidence=signal.confidence,
                    decision_price=price,
                    decision_ts=decision_ts,
                    trace_id="",  # filled by the service from the candle trace_id
                    top_features=top_features,
                    metadata=carried,
                )
            )
        return forecasts
