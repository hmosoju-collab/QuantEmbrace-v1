"""AlphaModel — forecast-centric model interface for the Alpha Engine.

Deliberately NOT a ``BaseStrategy``. ``BaseStrategy.generate_signal()`` returns a
trade-ready ``Signal`` (quantity, stops, sizing) — exactly the trading semantics
the shadow-mode Alpha Engine must not own. An ``AlphaModel`` instead returns
``AlphaForecast`` objects: a directional return forecast over a horizon, with no
order intent. Existing production strategies are mirrored into this interface by
``StrategyAlphaAdapter`` (P3) with zero changes to strategy_engine.

A single model trigger fans out one ``AlphaForecast`` per configured horizon
(15/30/60m) so IC-decay is measurable online (ADR-031 #1). Cost augmentation
(``net_edge_bps``, ``edge_band``) is applied downstream by ``CostModel``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from shared.models.alpha import AlphaForecast

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids hard strategy_engine dep in core lib
    from strategy_engine.strategies.base_strategy import Bar


class AlphaModel(ABC):
    """Abstract forecast generator.

    Concrete models (or ``StrategyAlphaAdapter`` wrapping a production strategy)
    implement ``on_bar`` to emit zero or more ``AlphaForecast`` objects. Models
    are pure forecasters: they never size, never place orders, never touch risk
    or broker state.

    Attributes:
        model_id:      Stable model identity, e.g. ``"alpha_orb_v2"``.
        model_version: Date-stamped version, e.g. ``"2026-06-13"`` (ADR-031 #2).
                       Mandatory — part of every forecast's deterministic id.
        alpha_family:  One of ``shared.models.alpha.ALPHA_FAMILIES``.
        market:        ``"NSE"`` (US frozen).
        timeframe:     Bar interval the model consumes, e.g. ``"1m"`` / ``"15m"``.
    """

    def __init__(
        self,
        *,
        model_id: str,
        model_version: str,
        alpha_family: str,
        market: str,
        timeframe: str,
    ) -> None:
        if not model_version:
            raise ValueError(f"{model_id}: model_version is mandatory (ADR-031 #2)")
        self.model_id = model_id
        self.model_version = model_version
        self.alpha_family = alpha_family
        self.market = market
        self.timeframe = timeframe

    @property
    def model_ref(self) -> str:
        """``model_id@model_version`` — the registry/performance key."""
        return f"{self.model_id}@{self.model_version}"

    async def initialize(self, saved_state: dict[str, Any] | None = None) -> None:
        """Optional warm-up hook (restore indicator buffers, etc.). Default no-op."""
        return None

    @abstractmethod
    async def on_bar(self, bar: Bar) -> list[AlphaForecast]:
        """Process one OHLCV bar and return 0..n forecasts (one per horizon).

        Implementations MUST return raw forecasts (``net_edge_bps``/``edge_band``
        unset); the engine applies the ``CostModel`` before ranking. Returning an
        empty list means "no forecast on this bar".
        """
        raise NotImplementedError

    def get_state(self) -> dict[str, Any]:
        """Serializable model state for restart-safety. Default empty."""
        return {}
