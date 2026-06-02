"""
EnrichedSignal — schema v4.0 enriched signal after AI engine processing.

The AI engine (``ai_engine``) consumes ``signals.pending`` (v3.0), runs
regime classification and signal quality scoring, then publishes
``signals.enriched`` (v4.0) using this model.

The risk engine reads from ``signals.enriched`` and parses both v3.0
(fallback, when ai_engine is lagging) and v4.0 (primary).

Signal flow (Phase 6):
    strategy_engine
        └──▶  signals.pending  (v3.0, schema_version="3.0")
                  └──▶  ai_engine (aiengine-v1)
                            └──▶  signals.enriched  (v4.0, schema_version="4.0")
                                      └──▶  risk_engine (risk-v1)

Enrichment fields are advisory — they do not gate orders.  The risk engine
remains the sole gatekeeper.  However, ``filtered=True`` is respected by
the risk engine as a soft-filter rejection when the quality filter is
enabled (threshold > 0.0 in strategy-config DynamoDB).

Degradation contract:
    - FeatureReader failure  →  regime="unknown", quality_score=0.5, filtered=False
    - Model unavailable       →  same defaults as above
    - Enrichment always completes — signals always flow through to risk

Schema version: 4.0 (extends Signal v3.0 schema)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from shared.models.signal import Direction, Signal, SignalStatus


ENRICHED_SCHEMA_VERSION = "4.0"

# Regime labels — "unknown" is the safe degradation value
RegimeLabel = Literal["trending", "ranging", "volatile", "crash", "unknown"]

# Default values used when enrichment cannot be computed
_DEFAULT_REGIME: RegimeLabel = "unknown"
_DEFAULT_QUALITY_SCORE: float = 0.5
_DEFAULT_FILTERED: bool = False


@dataclass(frozen=True)
class EnrichedSignal:
    """
    Immutable enriched signal — a Signal with AI engine annotations.

    Carries all original signal fields verbatim plus enrichment fields.
    Consumers should never modify enrichment fields after construction.

    Attributes:
        signal_id:             Unique signal identifier (deduplication key).
        strategy_name:         Strategy that generated this signal.
        symbol:                Trading symbol (e.g. "RELIANCE", "AAPL").
        market:                Market identifier ("NSE" or "US").
        direction:             BUY or SELL.
        quantity:              Shares/units.
        confidence:            Strategy conviction score 0.0–1.0.
        price_at_signal:       Market price when signal was generated.
        generated_at:          UTC timestamp from strategy_engine.
        expires_at:            Hard expiry (generated_at + 30s from strategy_engine).
        stop_loss:             Optional stop-loss price.
        take_profit:           Optional take-profit price.
        paper_trade:           Routes to paper endpoint when True.
        strategy_id:           Stable strategy identifier for limits/audit.
        product_type:          Broker product type (CNC, MIS, NRML).
        trace_id:              Correlation ID propagated from originating tick.
        metadata:              Pass-through strategy metadata.

        regime:                Market regime at enrichment time.
                               One of: trending, ranging, volatile, crash, unknown.
        regime_confidence:     HMM posterior confidence 0.0–1.0.
                               0.0 means degradation — regime is not reliable.
        quality_score:         GBT signal quality score 0.0–1.0.
                               0.5 = degraded/unknown (feature read failed).
        filtered:              True if quality_score < quality_filter_threshold.
                               Risk engine rejects filtered=True signals when
                               the quality filter is enabled.
        enriched_at:           UTC timestamp when ai_engine processed this signal.
        enrichment_latency_ms: Wall-clock time of the enrichment step (ms).
                               Used for observability; P99 target < 15ms.
        model_versions:        Dict of model name → version used for enrichment.
                               e.g. {"regime": "v1", "quality": "v1"}
        schema_version:        Always "4.0" for this class.
    """

    # ── Original signal fields (pass-through from v3.0) ───────────────────────
    signal_id:       str
    strategy_name:   str
    symbol:          str
    market:          str
    direction:       Direction
    quantity:        int
    confidence:      float
    price_at_signal: float
    generated_at:    datetime
    expires_at:      datetime
    stop_loss:       Optional[float]
    take_profit:     Optional[float]
    paper_trade:     bool
    strategy_id:     str
    product_type:    str
    trace_id:        str
    metadata:        dict[str, Any]

    # ── Enrichment fields (new in Phase 6 / v4.0) ────────────────────────────
    regime:                RegimeLabel
    regime_confidence:     float
    quality_score:         float
    filtered:              bool
    enriched_at:           datetime
    enrichment_latency_ms: float
    model_versions:        dict[str, str]

    schema_version: str = ENRICHED_SCHEMA_VERSION

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def instrument_id(self) -> str:
        """Canonical ``{MARKET}:{SYMBOL}`` identifier."""
        return f"{self.market}:{self.symbol}"

    @property
    def is_enriched(self) -> bool:
        """True when ML enrichment completed successfully (regime != unknown)."""
        return self.regime != "unknown"

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a dict for Kafka JSON transport (v4.0 schema)."""
        return {
            # Envelope
            "schema_version":          self.schema_version,
            # Signal fields
            "signal_id":               self.signal_id,
            "strategy_name":           self.strategy_name,
            "strategy_id":             self.strategy_id,
            "instrument_id":           self.instrument_id,
            "symbol":                  self.symbol,
            "market":                  self.market,
            "direction":               self.direction.value,
            "quantity":                self.quantity,
            "confidence":              self.confidence,
            "price_at_signal":         self.price_at_signal,
            "generated_at":            self.generated_at.isoformat(),
            "signal_time":             self.generated_at.isoformat(),  # v3.0 compat alias
            "expires_at":              self.expires_at.isoformat(),
            "stop_loss":               self.stop_loss,
            "take_profit":             self.take_profit,
            "paper_trade":             self.paper_trade,
            "product_type":            self.product_type,
            "trace_id":                self.trace_id,
            "metadata":                self.metadata,
            # Enrichment fields
            "regime":                  self.regime,
            "regime_confidence":       self.regime_confidence,
            "quality_score":           self.quality_score,
            "filtered":                self.filtered,
            "enriched_at":             self.enriched_at.isoformat(),
            "enrichment_latency_ms":   self.enrichment_latency_ms,
            "model_versions":          self.model_versions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EnrichedSignal":
        """
        Deserialise an EnrichedSignal from a v4.0 dict.

        Raises:
            KeyError: if required fields are missing.
            ValueError: if field values are invalid.
        """
        instrument_id: str = data["instrument_id"]
        market, symbol = instrument_id.split(":", 1)

        generated_at = _parse_dt(data.get("generated_at") or data.get("signal_time"))
        expires_at   = _parse_dt(data["expires_at"])
        enriched_at  = _parse_dt(data["enriched_at"])

        return cls(
            signal_id               = data["signal_id"],
            strategy_name           = data["strategy_name"],
            strategy_id             = data.get("strategy_id", data["strategy_name"]),
            symbol                  = symbol,
            market                  = market,
            direction               = Direction(data["direction"]),
            quantity                = int(data["quantity"]),
            confidence              = float(data.get("confidence", 1.0)),
            price_at_signal         = float(data.get("price_at_signal", 0.0)),
            generated_at            = generated_at,
            expires_at              = expires_at,
            stop_loss               = data.get("stop_loss"),
            take_profit             = data.get("take_profit"),
            paper_trade             = bool(data.get("paper_trade", False)),
            product_type            = data.get("product_type", "MIS"),
            trace_id                = data.get("trace_id", ""),
            metadata                = dict(data.get("metadata") or {}),
            regime                  = data.get("regime", _DEFAULT_REGIME),
            regime_confidence       = float(data.get("regime_confidence", 0.0)),
            quality_score           = float(data.get("quality_score", _DEFAULT_QUALITY_SCORE)),
            filtered                = bool(data.get("filtered", _DEFAULT_FILTERED)),
            enriched_at             = enriched_at,
            enrichment_latency_ms   = float(data.get("enrichment_latency_ms", 0.0)),
            model_versions          = dict(data.get("model_versions") or {}),
            schema_version          = data.get("schema_version", ENRICHED_SCHEMA_VERSION),
        )

    @classmethod
    def from_signal(
        cls,
        signal: Signal,
        *,
        trace_id:              str = "",
        strategy_id:           str = "",
        product_type:          str = "MIS",
        expires_at:            Optional[datetime] = None,
        regime:                RegimeLabel = _DEFAULT_REGIME,
        regime_confidence:     float = 0.0,
        quality_score:         float = _DEFAULT_QUALITY_SCORE,
        filtered:              bool = _DEFAULT_FILTERED,
        enriched_at:           Optional[datetime] = None,
        enrichment_latency_ms: float = 0.0,
        model_versions:        Optional[dict[str, str]] = None,
    ) -> "EnrichedSignal":
        """
        Build an EnrichedSignal from a Signal + enrichment fields.

        Used by SignalEnricher after computing regime and quality score.

        Args:
            signal:                Original Signal from signals.pending.
            trace_id:              Kafka trace_id from SignalEvent.
            strategy_id:           Kafka strategy_id from SignalEvent.
            product_type:          Kafka product_type from SignalEvent.
            expires_at:            Kafka expires_at from SignalEvent.
            regime:                Classified market regime.
            regime_confidence:     HMM posterior confidence.
            quality_score:         GBT quality score.
            filtered:              Whether quality filter tripped.
            enriched_at:           When enrichment completed.
            enrichment_latency_ms: Wall-clock enrichment time.
            model_versions:        Map of model names to active versions.
        """
        from shared.utils.helpers import utc_now

        _now = utc_now()
        _expires = expires_at or _now

        return cls(
            signal_id               = signal.signal_id,
            strategy_name           = signal.strategy_name,
            strategy_id             = strategy_id or signal.strategy_name,
            symbol                  = signal.symbol,
            market                  = signal.market,
            direction               = signal.direction,
            quantity                = signal.quantity,
            confidence              = signal.confidence,
            price_at_signal         = signal.price_at_signal,
            generated_at            = signal.generated_at,
            expires_at              = _expires,
            stop_loss               = signal.stop_loss,
            take_profit             = signal.take_profit,
            paper_trade             = signal.paper_trade,
            product_type            = product_type,
            trace_id                = trace_id,
            metadata                = dict(signal.metadata),
            regime                  = regime,
            regime_confidence       = regime_confidence,
            quality_score           = quality_score,
            filtered                = filtered,
            enriched_at             = enriched_at or _now,
            enrichment_latency_ms   = enrichment_latency_ms,
            model_versions          = model_versions or {},
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_dt(value: Any) -> datetime:
    """Parse an ISO-8601 string to timezone-aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def degraded_enrichment(
    signal: Signal,
    *,
    trace_id:    str = "",
    strategy_id: str = "",
    product_type: str = "MIS",
    expires_at:   Optional[datetime] = None,
    enrichment_latency_ms: float = 0.0,
) -> EnrichedSignal:
    """
    Build an EnrichedSignal with degraded defaults.

    Used when the enrichment pipeline fails completely.  Signal still flows
    through — regime='unknown', quality_score=0.5, filtered=False.
    """
    return EnrichedSignal.from_signal(
        signal,
        trace_id              = trace_id,
        strategy_id           = strategy_id,
        product_type          = product_type,
        expires_at            = expires_at,
        regime                = "unknown",
        regime_confidence     = 0.0,
        quality_score         = _DEFAULT_QUALITY_SCORE,
        filtered              = False,
        enrichment_latency_ms = enrichment_latency_ms,
        model_versions        = {},
    )
