"""Alpha — canonical shared models for the Alpha Engine (ADR-031, shadow mode).

This is the SINGLE source of truth for the ``AlphaForecast`` and
``AlphaOpportunity`` types, mirroring the precedent set by
``shared/models/signal.py``. The Alpha Engine and (at the future cutover) any
consumer of alpha research import from here — no service redefines these locally.

An ``AlphaForecast`` is forecast-centric, NOT a trade-ready order. It deliberately
carries no quantity, no broker product type, and no sizing — the Alpha Engine
generates and ranks alpha; it never trades (see ``CLAUDE.md`` governance and
``architecture/phase9_design_review.md`` §3 for the vocabulary this aligns with).

Vocabulary (phase9 §3): ``forecast_return_bps``, ``net_edge_bps``, ``alpha_family``.

forecast_id is deterministic — the same model@version producing the same forecast
for the same symbol/direction/horizon at the same decision time always yields the
same id, so a replayed bar produces an idempotent row (mirrors
``_make_deterministic_signal_id`` in the strategy_engine publisher).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
from typing import Any

from shared.models.signal import Direction

# ── Allowed enumerations (kept as plain constants to avoid over-modelling) ──────

ALPHA_FAMILIES: frozenset[str] = frozenset(
    {"momentum", "quality", "reversal", "regime_filter"}
)
"""Alpha family taxonomy (phase9 §3.1)."""

UNIVERSES: frozenset[str] = frozenset(
    {"NIFTY50", "NIFTYNEXT50", "MIDCAP100", "SMALLCAP", "UNKNOWN"}
)
"""Universe attribution buckets (ADR-031 rev 2). UNKNOWN when unresolvable."""

LABEL_PENDING = "PENDING"
LABEL_LABELED = "LABELED"
LABEL_UNLABELABLE = "UNLABELABLE"


@dataclass(frozen=True)
class FeatureContribution:
    """One decision driver behind a forecast (explainability snapshot, ADR-031 #16).

    Heuristic in v1 (``explainability_version="heuristic-v1"``): the adapter
    extracts these from each strategy's own decision metadata (e.g. ORB breakout
    strength, VWAP sigma-deviation). SHAP-based attribution is future work — the
    field *contract* is what matters now, so that when an alpha degrades we can
    later answer "which drivers disappeared?".
    """

    feature: str
    value: float

    def to_dict(self) -> dict[str, Any]:
        return {"feature": self.feature, "value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeatureContribution:
        return cls(feature=str(data["feature"]), value=float(data["value"]))


def compute_forecast_id(
    model_id: str,
    model_version: str,
    symbol: str,
    direction: Direction | str,
    horizon_minutes: int,
    decision_ts: datetime,
) -> str:
    """Deterministic 32-char forecast id.

    Formula: ``sha256(model_id|model_version|symbol|direction|horizon|decision_ts_iso)[:32]``.

    model_version is part of the identity (ADR-031 #2): a forecast from
    ``alpha_orb_v2@2026-06-13`` is a different observation from the same model
    re-tuned on a later date, even for the same symbol/time — their IC must
    never blend.
    """
    if decision_ts.tzinfo is None:
        decision_ts = decision_ts.replace(tzinfo=UTC)
    dir_value = direction.value if isinstance(direction, Direction) else str(direction)
    raw = "|".join(
        [
            model_id,
            model_version,
            symbol,
            dir_value,
            str(int(horizon_minutes)),
            decision_ts.isoformat(),
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class AlphaForecast:
    """A single-horizon alpha forecast produced by an ``AlphaModel``.

    Cost fields (``expected_spread_bps``, ``expected_slippage_bps``,
    ``fees_taxes_bps``, ``net_edge_bps``, ``edge_band``) are populated by the
    ``CostModel`` via ``dataclasses.replace`` after the model emits the raw
    forecast; ``forecast_id`` stays stable across that replace because it is
    derived only from identity fields.
    """

    model_id: str
    model_version: str           # mandatory — never blank (validated below)
    alpha_family: str
    symbol: str
    market: str
    universe: str
    direction: Direction
    timeframe: str
    horizon_minutes: int
    forecast_return_bps: float
    confidence: float
    decision_price: float
    decision_ts: datetime
    trace_id: str

    # Cost-augmented fields (set by CostModel.apply; safe defaults pre-costing)
    expected_spread_bps: float = 0.0
    expected_slippage_bps: float = 0.0
    fees_taxes_bps: float = 0.0
    net_edge_bps: float = 0.0
    edge_band: str = ""

    # Explainability (ADR-031 #16)
    top_features: tuple[FeatureContribution, ...] = field(default_factory=tuple)
    explainability_version: str = "heuristic-v1"

    metadata: dict[str, Any] = field(default_factory=dict)

    # Derived — computed in __post_init__ when not supplied (e.g. from_dict)
    forecast_id: str = ""

    def __post_init__(self) -> None:
        if not self.model_version:
            raise ValueError("AlphaForecast.model_version is mandatory and must be non-empty")
        if not self.forecast_id:
            object.__setattr__(
                self,
                "forecast_id",
                compute_forecast_id(
                    self.model_id,
                    self.model_version,
                    self.symbol,
                    self.direction,
                    self.horizon_minutes,
                    self.decision_ts,
                ),
            )

    @property
    def instrument_id(self) -> str:
        """``{MARKET}:{SYMBOL}`` — the Kafka partition key convention."""
        return f"{self.market}:{self.symbol}"

    @property
    def model_ref(self) -> str:
        """``model_id@model_version`` — the registry/performance key."""
        return f"{self.model_id}@{self.model_version}"

    def to_dict(self) -> dict[str, Any]:
        decision_ts = self.decision_ts
        if decision_ts.tzinfo is None:
            decision_ts = decision_ts.replace(tzinfo=UTC)
        return {
            "forecast_id": self.forecast_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "alpha_family": self.alpha_family,
            "symbol": self.symbol,
            "market": self.market,
            "universe": self.universe,
            "instrument_id": self.instrument_id,
            "direction": self.direction.value,
            "timeframe": self.timeframe,
            "horizon_minutes": self.horizon_minutes,
            "forecast_return_bps": self.forecast_return_bps,
            "expected_spread_bps": self.expected_spread_bps,
            "expected_slippage_bps": self.expected_slippage_bps,
            "fees_taxes_bps": self.fees_taxes_bps,
            "net_edge_bps": self.net_edge_bps,
            "edge_band": self.edge_band,
            "confidence": self.confidence,
            "top_features": [fc.to_dict() for fc in self.top_features],
            "explainability_version": self.explainability_version,
            "decision_price": self.decision_price,
            "decision_ts": decision_ts.isoformat(),
            "trace_id": self.trace_id,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AlphaForecast:
        raw_ts = data["decision_ts"]
        decision_ts = datetime.fromisoformat(raw_ts) if isinstance(raw_ts, str) else raw_ts
        top_features = tuple(
            FeatureContribution.from_dict(fc) for fc in data.get("top_features", [])
        )
        return cls(
            model_id=data["model_id"],
            model_version=data["model_version"],
            alpha_family=data["alpha_family"],
            symbol=data["symbol"],
            market=data["market"],
            universe=data.get("universe", "UNKNOWN"),
            direction=Direction(data["direction"]),
            timeframe=data["timeframe"],
            horizon_minutes=int(data["horizon_minutes"]),
            forecast_return_bps=float(data["forecast_return_bps"]),
            confidence=float(data["confidence"]),
            decision_price=float(data["decision_price"]),
            decision_ts=decision_ts,
            trace_id=data["trace_id"],
            expected_spread_bps=float(data.get("expected_spread_bps", 0.0)),
            expected_slippage_bps=float(data.get("expected_slippage_bps", 0.0)),
            fees_taxes_bps=float(data.get("fees_taxes_bps", 0.0)),
            net_edge_bps=float(data.get("net_edge_bps", 0.0)),
            edge_band=data.get("edge_band", ""),
            top_features=top_features,
            explainability_version=data.get("explainability_version", "heuristic-v1"),
            metadata=data.get("metadata", {}),
            # Preserve the stored id exactly (do not recompute on read)
            forecast_id=data.get("forecast_id", ""),
        )


@dataclass(frozen=True)
class AlphaOpportunity:
    """A ranked forecast emitted by the ``AlphaRanker`` for one cycle.

    Wraps an ``AlphaForecast`` with cross-sectional ranking context. The shadow
    publisher serializes these to ``alpha.opportunities`` (publish-eligible only,
    net_edge_bps ≥ floor); all forecasts — published or suppressed — are still
    persisted to the forecast store for research.

    ``conflict_group_id`` ties together same-symbol opposite-direction forecasts
    in the same cycle. Both sides are kept (ADR-031 #7): shadow mode maximizes
    learning, so research can later compute per-family conflict win rates.
    """

    forecast: AlphaForecast
    rank: int
    score: float
    cycle_id: str
    cycle_ts: datetime
    conflict_group_id: str | None = None
    published: bool = False
    suppressed_by: str = ""   # "" when publish-eligible; e.g. "edge_floor", "kill_switch"

    def to_dict(self) -> dict[str, Any]:
        cycle_ts = self.cycle_ts
        if cycle_ts.tzinfo is None:
            cycle_ts = cycle_ts.replace(tzinfo=UTC)
        return {
            "forecast": self.forecast.to_dict(),
            "rank": self.rank,
            "score": self.score,
            "cycle_id": self.cycle_id,
            "cycle_ts": cycle_ts.isoformat(),
            "conflict_group_id": self.conflict_group_id,
            "published": self.published,
            "suppressed_by": self.suppressed_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AlphaOpportunity:
        raw_ts = data["cycle_ts"]
        cycle_ts = datetime.fromisoformat(raw_ts) if isinstance(raw_ts, str) else raw_ts
        return cls(
            forecast=AlphaForecast.from_dict(data["forecast"]),
            rank=int(data["rank"]),
            score=float(data["score"]),
            cycle_id=data["cycle_id"],
            cycle_ts=cycle_ts,
            conflict_group_id=data.get("conflict_group_id"),
            published=bool(data.get("published", False)),
            suppressed_by=data.get("suppressed_by", ""),
        )
