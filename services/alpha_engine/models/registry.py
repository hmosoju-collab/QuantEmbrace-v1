"""AlphaModelRegistry — instantiates the configured alpha models for a session.

Wraps the three production strategies (ORB v2 / VWAP v2 / trend_15m) as
``StrategyAlphaAdapter`` instances. For each model it resolves which version(s) to
shadow from the registry store: the champion always, plus the challenger when one
is set (champion-challenger A/B, ADR-031 #9). On a fresh stack with no registry
record, it bootstrap-registers the boot version as a SHADOW champion so every
forecast is versioned and the lineage exists from day one.

The strategy classes are imported across the service boundary (sanctioned by the
monorepo Dockerfile) and are never modified.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from alpha_engine.models.strategy_alpha_adapter import StrategyAlphaAdapter
from alpha_engine.store.registry_store import AlphaRegistryStore, RegistryError
from shared.logging.logger import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from alpha_engine.models.alpha_model import AlphaModel
    from strategy_engine.strategies.base_strategy import BaseStrategy

logger = get_logger(__name__, service_name="alpha_engine")


def _orb(name: str, symbols: list[str]) -> BaseStrategy:
    from strategy_engine.strategies.orb_strategy import ORBStrategy

    return ORBStrategy(name=name, symbols=symbols, market="NSE", paper_trade=True)


def _vwap(name: str, symbols: list[str]) -> BaseStrategy:
    from strategy_engine.strategies.vwap_reversion_strategy import VWAPReversionStrategy

    return VWAPReversionStrategy(name=name, symbols=symbols, market="NSE", paper_trade=True)


def _trend(name: str, symbols: list[str]) -> BaseStrategy:
    from strategy_engine.strategies.intraday_trend_15m_strategy import IntradayTrend15mStrategy

    return IntradayTrend15mStrategy(name=name, symbols=symbols, market="NSE", paper_trade=True)


@dataclass(frozen=True)
class _ModelSpec:
    factory: Callable[[str, list[str]], BaseStrategy]
    alpha_family: str
    timeframe: str
    feature_keys: list[str]


# The shadow mirror of the live ADR-030 strategy set.
MODEL_SPECS: dict[str, _ModelSpec] = {
    "alpha_orb_v2": _ModelSpec(_orb, "momentum", "1m", ["vol_ratio", "or_range"]),
    "alpha_vwap_rev_v2": _ModelSpec(_vwap, "reversal", "1m", ["reward_risk", "band_std", "atr"]),
    "alpha_trend_15m": _ModelSpec(_trend, "momentum", "15m", ["adx", "atr"]),
}


class AlphaModelRegistry:
    def __init__(
        self,
        registry_store: AlphaRegistryStore,
        *,
        symbols: list[str],
        horizons_minutes: list[int],
        universe_resolver: Callable[[str], str],
    ) -> None:
        self._registry = registry_store
        self._symbols = symbols
        self._horizons = horizons_minutes
        self._resolve_universe = universe_resolver

    def build_models(self, model_ids: list[str], boot_version: str) -> list[AlphaModel]:
        models: list[AlphaModel] = []
        for model_id in model_ids:
            spec = MODEL_SPECS.get(model_id)
            if spec is None:
                logger.warning("alpha_engine.unknown_model id=%s — skipped", model_id)
                continue
            for version in self._resolve_versions(model_id, boot_version, spec):
                strategy = spec.factory(model_id, self._symbols)
                models.append(
                    StrategyAlphaAdapter(
                        strategy,
                        model_id=model_id,
                        model_version=version,
                        alpha_family=spec.alpha_family,
                        timeframe=spec.timeframe,
                        horizons_minutes=self._horizons,
                        feature_keys=spec.feature_keys,
                        universe_resolver=self._resolve_universe,
                    )
                )
                logger.info("alpha_engine.model_loaded %s@%s", model_id, version)
        return models

    def _resolve_versions(self, model_id: str, boot_version: str, spec: _ModelSpec) -> list[str]:
        try:
            meta = self._registry.get_meta(model_id)
        except Exception:
            logger.exception("alpha_engine.registry_read_failed id=%s — using boot version", model_id)
            return [boot_version]

        if meta is None:
            self._bootstrap_register(model_id, boot_version, spec)
            return [boot_version]

        versions: list[str] = []
        champion = meta.get("champion_model_version")
        challenger = meta.get("challenger_model_version")
        if champion:
            versions.append(champion)
        if challenger and challenger != champion:
            versions.append(challenger)
        return versions or [boot_version]

    def _bootstrap_register(self, model_id: str, version: str, spec: _ModelSpec) -> None:
        try:
            self._registry.register_version(
                model_id=model_id,
                model_version=version,
                alpha_family=spec.alpha_family,
                hypothesis="auto baseline shadow registration at service boot",
                experiment_id=f"EXP-BOOT-{version}",
                change_summary="initial shadow registration (mirrors live ADR-030 strategy)",
            )
            logger.info("alpha_engine.bootstrap_registered %s@%s", model_id, version)
        except RegistryError:
            # Already registered concurrently, or write blocked — proceed in shadow.
            logger.info("alpha_engine.bootstrap_register_skipped %s@%s", model_id, version)
        except Exception:
            logger.exception("alpha_engine.bootstrap_register_failed %s@%s", model_id, version)
