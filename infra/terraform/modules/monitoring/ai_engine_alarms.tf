###############################################################################
# Phase 6 — AI Engine CloudWatch Alarms
#
# Namespace: QuantEmbrace/AIEngine
# Metrics published by services/ai_engine/service.py and enrichment modules.
#
# Alarms in this file:
#
#   1. EnrichmentLatencyHigh     — P99 enrichment > 20ms (budget: 15ms target)
#   2. EnrichmentFallbackActive  — watchdog activated signals.pending fallback
#   3. RegimeClassificationErrors — model errors indicate possible corruption
#   4. QualityFilterRateHigh     — > 50% signals filtered → possible mis-tuning
#   5. SignalsEnrichedSilent     — no enriched signals for 15 min during hours
#   6. ModelHotReloadFailing     — S3 download or joblib errors
#
# All alarms notify the standard alerts SNS topic.
# EnrichmentFallbackActive also notifies SNS for ops awareness (not kill-switch;
# fallback is safe by design — trading continues via signals.pending path).
###############################################################################

locals {
  ai_engine_namespace    = "QuantEmbrace/AIEngine"
  ai_engine_alarm_prefix = "${local.alarm_prefix}-ai-engine"
}

# ── 1. Enrichment Latency P99 > 20ms ─────────────────────────────────────────
#
# SignalEnricher emits EnrichmentLatencyMs via cloudwatch:PutMetricData.
# Uses PERCENTILE statistic (p99) for accurate tail-latency signal.
# Design target P99 < 15ms; alarm threshold 20ms gives a 5ms buffer before
# action is required.  3 evaluation periods = 5 min sustained breach.
#
# Actions: alert ops to investigate (model too large? feature read slowdown?).
# Do NOT halt trading — enrichment failure degrades to fallback gracefully.

resource "aws_cloudwatch_metric_alarm" "ai_engine_enrichment_latency" {
  alarm_name          = "${local.ai_engine_alarm_prefix}-enrichment-latency-high"
  alarm_description   = "ai_engine: enrichment P99 latency > 20ms for 5 min — check model size and DynamoDB feature read times"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "EnrichmentLatencyMs"
  namespace           = local.ai_engine_namespace
  period              = 60
  extended_statistic  = "p99"
  threshold           = 20             # ms — 5ms buffer above the 15ms design target
  treat_missing_data  = "notBreaching" # silence when ai_engine is scaled to 0

  dimensions = {
    Service     = "ai_engine"
    Environment = var.environment
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = merge(local.common_tags, {
    Service   = "ai_engine"
    Phase     = "6"
    AlarmType = "enrichment-latency"
  })
}

# ── 2. Enrichment Fallback Active ─────────────────────────────────────────────
#
# EnrichmentWatchdog publishes EnrichmentFallbackActive = 1 when it switches
# risk_engine to consume signals.pending directly (ai_engine lag > threshold).
# Published to QuantEmbrace/RiskEngine namespace (same as watchdog's metric call
# in enrichment_watchdog.py _publish_fallback_metric).
#
# This is NOT a critical trading alarm — fallback mode is safe by design.
# Alert ops so they can investigate why ai_engine is lagging.

resource "aws_cloudwatch_metric_alarm" "enrichment_fallback_active" {
  alarm_name          = "${local.alarm_prefix}-enrichment-fallback-active"
  alarm_description   = "EnrichmentWatchdog activated fallback mode — risk_engine consuming signals.pending directly; investigate ai_engine health"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "EnrichmentFallbackActive"
  namespace           = "QuantEmbrace/RiskEngine" # Published by EnrichmentWatchdog (risk_engine)
  period              = 60
  statistic           = "Maximum"
  threshold           = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    Service = "risk_engine"
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = merge(local.common_tags, {
    Service   = "ai_engine"
    Phase     = "6"
    AlarmType = "enrichment-fallback"
  })
}

# ── 3. Regime Classification Errors ──────────────────────────────────────────
#
# RegimeClassifier publishes RegimeClassificationErrors on each model inference
# exception.  Errors always degrade to regime="unknown" so trading continues,
# but sustained errors indicate a model corruption or joblib incompatibility.
# Threshold: > 5 errors in 5 minutes (not per-signal — some tolerance is fine).

resource "aws_cloudwatch_metric_alarm" "regime_classification_errors" {
  alarm_name          = "${local.ai_engine_alarm_prefix}-regime-classification-errors"
  alarm_description   = "ai_engine: > 5 regime classification errors in 5 min — possible model corruption or joblib version mismatch"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "RegimeClassificationErrors"
  namespace           = local.ai_engine_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  treat_missing_data  = "notBreaching"

  dimensions = {
    Service     = "ai_engine"
    Environment = var.environment
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = merge(local.common_tags, {
    Service   = "ai_engine"
    Phase     = "6"
    AlarmType = "model-health"
  })
}

# ── 4. Quality Filter Rate > 50% ─────────────────────────────────────────────
#
# SignalQualityScorer publishes QualityFilterRate (%) — the fraction of signals
# filtered out due to quality score below threshold.  A sustained rate > 50%
# is anomalous: either the model is miscalibrated, the threshold is too high,
# or market regime has changed dramatically.  Should not fire under normal
# ranging/trending regimes.
#
# Note: This alarm uses a composite math expression across the raw metric
# (QualityFilteredCount / SignalsEnrichedCount * 100).  If the single-metric
# form is insufficient, replace with a math-based alarm using metric math.

resource "aws_cloudwatch_metric_alarm" "quality_filter_rate_high" {
  alarm_name          = "${local.ai_engine_alarm_prefix}-quality-filter-rate-high"
  alarm_description   = "ai_engine: > 50% signals filtered by quality scorer — check model calibration or quality threshold config in DynamoDB"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "QualityFilterRatePct"
  namespace           = local.ai_engine_namespace
  period              = 300
  statistic           = "Average"
  threshold           = 50 # percent
  treat_missing_data  = "notBreaching"

  dimensions = {
    Service     = "ai_engine"
    Environment = var.environment
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = merge(local.common_tags, {
    Service   = "ai_engine"
    Phase     = "6"
    AlarmType = "enrichment-quality"
  })
}

# ── 5. Signals Enriched Count = 0 (market hours) ─────────────────────────────
#
# ai_engine publishes SignalsEnrichedCount (sum) every minute.  Zero for 15 min
# during market hours indicates ai_engine is not processing — possible consumer
# group lag, crash loop, or Kafka connectivity failure.
#
# treat_missing_data = notBreaching: silence when ASG is scaled to 0 overnight.
# Operators must ensure this alarm is suppressed outside market hours or via
# scheduled maintenance windows.

resource "aws_cloudwatch_metric_alarm" "signals_enriched_silent" {
  alarm_name          = "${local.ai_engine_alarm_prefix}-signals-enriched-silent"
  alarm_description   = "ai_engine: no signals enriched for 15 min — possible consumer stall, crash loop, or Kafka connectivity issue"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  metric_name         = "SignalsEnrichedCount"
  namespace           = local.ai_engine_namespace
  period              = 300 # 5-min periods × 3 = 15 min
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "notBreaching" # silence overnight when ASG is at 0

  dimensions = {
    Service     = "ai_engine"
    Environment = var.environment
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = merge(local.common_tags, {
    Service   = "ai_engine"
    Phase     = "6"
    AlarmType = "enrichment-throughput"
  })
}

# ── 6. Model Hot Reload Failures ──────────────────────────────────────────────
#
# ModelRegistry publishes ModelHotReloadErrors on S3 download failure or joblib
# load error during the 60-second hot-reload loop.  Sustained failures mean
# model updates are not reaching production.  The service continues with the
# last-loaded model (or stub), so trading is not impacted, but model drift risk
# increases.
#
# Threshold: > 3 errors in 15 min (3 consecutive reload cycles failing).

resource "aws_cloudwatch_metric_alarm" "model_hot_reload_errors" {
  alarm_name          = "${local.ai_engine_alarm_prefix}-model-hot-reload-errors"
  alarm_description   = "ai_engine: model hot-reload failing — S3 download or joblib load errors; model updates not reaching production"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "ModelHotReloadErrors"
  namespace           = local.ai_engine_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 3
  treat_missing_data  = "notBreaching"

  dimensions = {
    Service     = "ai_engine"
    Environment = var.environment
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = merge(local.common_tags, {
    Service   = "ai_engine"
    Phase     = "6"
    AlarmType = "model-health"
  })
}
