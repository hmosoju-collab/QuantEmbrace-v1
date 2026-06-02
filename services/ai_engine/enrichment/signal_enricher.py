"""
SignalEnricher — orchestrates the full enrichment pipeline per signal.

Receives a parsed SignalEvent from signals.pending, runs:
  1. FeatureReader.get_latest         → live FeatureSet from DynamoDB   ~3-5ms
  2. RegimeClassifier.classify        → market regime                   ~1ms
  3. SignalQualityScorer.score        → quality score + soft filter      ~1ms
  4. Quality filter threshold lookup  → DynamoDB strategy-config        cached
  5. Build EnrichedSignal                                                <0.1ms
  6. Publish to signals.enriched                                         ~1ms
                                                                        --------
  Total enrichment budget:                                              ~6-8ms

Latency target: P99 < 15ms (as measured by CloudWatch EnrichmentLatencyMs).

Non-fatal design:
  Every step is individually wrapped.  FeatureReader failure → regime
  "unknown", quality_score 0.5, filtered False.  Signal always flows
  through.  No step can prevent a signal from reaching risk_engine.

Phase 6 design (ADR-014 §5.5).

Regime log:
  Every enriched signal is written to DynamoDB regime-log table for
  model evaluation and strategy selector agent input.  Failures here
  are non-fatal (logged, not propagated).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

from shared.logging.logger import get_logger
from shared.models.enriched_signal import (
    EnrichedSignal,
    RegimeLabel,
    degraded_enrichment,
)
from ai_engine.classifiers.regime_classifier import RegimeClassifier
from ai_engine.scorers.signal_quality_scorer import SignalQualityScorer

logger = get_logger(__name__, service_name="ai_engine")

_DEFAULT_QUALITY_THRESHOLD: float = 0.0  # no filtering until operator raises it


class SignalEnricher:
    """
    Per-signal enrichment orchestrator.

    Args:
        regime_classifier:  Loaded RegimeClassifier.
        quality_scorer:     Loaded SignalQualityScorer.
        dynamo_client:      boto3 DynamoDB client for threshold + regime-log writes.
        strategy_config_table: DynamoDB table for quality_filter_threshold.
        regime_log_table:   DynamoDB table for regime audit log (may be None).
        default_interval:   Candle interval used for feature lookup (default "1m").
    """

    def __init__(
        self,
        regime_classifier:    RegimeClassifier,
        quality_scorer:       SignalQualityScorer,
        dynamo_client:        Optional[Any] = None,
        strategy_config_table: Optional[str] = None,
        regime_log_table:     Optional[str] = None,
        default_interval:     str = "1m",
    ) -> None:
        self._classifier    = regime_classifier
        self._scorer        = quality_scorer
        self._dynamo        = dynamo_client
        self._config_table  = strategy_config_table
        self._regime_table  = regime_log_table
        self._interval      = default_interval

        # Cached threshold per strategy.  Refreshed when a new strategy arrives
        # (or cleared on each signal to ensure freshness — DynamoDB read is ~1ms).
        self._threshold_cache: dict[str, float] = {}

    async def enrich(
        self,
        signal_event: Any,  # shared.risk_engine.consumers.kafka_signal_consumer.SignalEvent
    ) -> EnrichedSignal:
        """
        Enrich a signal with regime + quality score.

        Always returns an EnrichedSignal — never raises.  Falls back to
        degraded defaults on any sub-step failure.

        Args:
            signal_event: SignalEvent from ai_engine's KafkaSignalConsumer.

        Returns:
            EnrichedSignal ready for publishing to signals.enriched.
        """
        t_start = time.perf_counter()

        signal        = signal_event.signal
        trace_id      = signal_event.trace_id
        strategy_id   = signal_event.strategy_id
        product_type  = signal_event.product_type
        expires_at    = signal_event.expires_at

        market = signal.market
        symbol = signal.symbol

        try:
            # Step 2 — regime classification
            regime_output = await self._classifier.classify(
                market=market, symbol=symbol, interval=self._interval
            )

            # Step 3 — signal quality scoring + soft filter
            threshold = await self._get_threshold(signal.strategy_name)
            quality_score, filtered = await self._scorer.score(
                market=market,
                symbol=symbol,
                direction=signal.direction,
                interval=self._interval,
                threshold=threshold,
            )

            enrichment_ms = (time.perf_counter() - t_start) * 1000.0

            model_versions = {
                **self._classifier._registry.loaded_versions(),
            }

            enriched = EnrichedSignal.from_signal(
                signal,
                trace_id              = trace_id,
                strategy_id           = strategy_id,
                product_type          = product_type,
                expires_at            = expires_at,
                regime                = regime_output.regime,
                regime_confidence     = regime_output.confidence,
                quality_score         = quality_score,
                filtered              = filtered,
                enrichment_latency_ms = enrichment_ms,
                model_versions        = model_versions,
            )

            logger.info(
                "signal.enriched",
                signal_id             = enriched.signal_id,
                symbol                = enriched.symbol,
                regime                = enriched.regime,
                regime_confidence     = round(enriched.regime_confidence, 4),
                quality_score         = round(enriched.quality_score, 4),
                filtered              = enriched.filtered,
                enrichment_latency_ms = round(enrichment_ms, 2),
                model_versions        = enriched.model_versions,
            )

            # Async regime-log write (non-blocking, non-fatal)
            asyncio.create_task(
                self._write_regime_log(enriched),
                name=f"regime_log.{signal.signal_id[:8]}",
            )

            return enriched

        except Exception as exc:
            enrichment_ms = (time.perf_counter() - t_start) * 1000.0
            logger.error(
                "signal_enricher.pipeline_error",
                signal_id=signal.signal_id,
                symbol=symbol,
                error=str(exc),
                enrichment_latency_ms=round(enrichment_ms, 2),
            )
            return degraded_enrichment(
                signal,
                trace_id              = trace_id,
                strategy_id           = strategy_id,
                product_type          = product_type,
                expires_at            = expires_at,
                enrichment_latency_ms = enrichment_ms,
            )

    # ── Threshold resolution ───────────────────────────────────────────────────

    async def _get_threshold(self, strategy_name: str) -> float:
        """
        Read quality_filter_threshold from DynamoDB strategy-config.

        Returns cached value if available (per-call caching — DynamoDB read
        is ~1ms but avoids re-reading within a tight loop).

        Falls back to 0.0 (no filtering) on any error.
        """
        if strategy_name in self._threshold_cache:
            return self._threshold_cache[strategy_name]

        threshold = await self._read_threshold_from_dynamo(strategy_name)
        self._threshold_cache[strategy_name] = threshold
        return threshold

    def clear_threshold_cache(self) -> None:
        """Clear the threshold cache (call periodically to pick up config changes)."""
        self._threshold_cache.clear()

    async def _read_threshold_from_dynamo(self, strategy_name: str) -> float:
        """Read quality_filter_threshold from strategy-config DynamoDB table."""
        if not self._dynamo or not self._config_table:
            return _DEFAULT_QUALITY_THRESHOLD

        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._config_table,
                Key={
                    "PK": {"S": f"STRATEGY#{strategy_name}"},
                    "SK": {"S": "CONFIG"},
                },
            )
            item = response.get("Item", {})
            raw = item.get("quality_filter_threshold", {}).get("N")
            if raw is not None:
                value = float(raw)
                # Clamp to [0.0, 1.0] — never trust unchecked DynamoDB data
                return max(0.0, min(1.0, value))
        except Exception as exc:
            logger.warning(
                "signal_enricher.threshold_read_failed",
                strategy=strategy_name,
                error=str(exc),
            )

        return _DEFAULT_QUALITY_THRESHOLD

    # ── Regime log ─────────────────────────────────────────────────────────────

    async def _write_regime_log(self, enriched: EnrichedSignal) -> None:
        """
        Write regime classification to DynamoDB regime-log table.

        Non-fatal: failures are logged but do not affect signal flow.
        TTL: 30 days (matches Phase 6 design §7.3).
        """
        if not self._dynamo or not self._regime_table:
            return

        try:
            now = datetime.now(timezone.utc)
            ttl_seconds = int(now.timestamp()) + (30 * 24 * 3600)  # 30 days

            pk = f"REGIME#{enriched.market}#{enriched.symbol}"
            sk = f"SESSION#{enriched.enriched_at.date().isoformat()}T{enriched.generated_at.isoformat()}"

            item: dict[str, Any] = {
                "PK":                     {"S": pk},
                "SK":                     {"S": sk},
                "signal_id":              {"S": enriched.signal_id},
                "strategy_name":          {"S": enriched.strategy_name},
                "regime":                 {"S": enriched.regime},
                "regime_confidence":      {"N": str(round(enriched.regime_confidence, 6))},
                "quality_score":          {"N": str(round(enriched.quality_score, 6))},
                "filtered":               {"BOOL": enriched.filtered},
                "enrichment_latency_ms":  {"N": str(round(enriched.enrichment_latency_ms, 3))},
                "enriched_at":            {"S": enriched.enriched_at.isoformat()},
                "ttl":                    {"N": str(ttl_seconds)},
            }

            await asyncio.to_thread(
                self._dynamo.put_item,
                TableName=self._regime_table,
                Item=item,
            )

        except Exception as exc:
            logger.warning(
                "signal_enricher.regime_log_write_failed",
                signal_id=enriched.signal_id,
                error=str(exc),
            )
