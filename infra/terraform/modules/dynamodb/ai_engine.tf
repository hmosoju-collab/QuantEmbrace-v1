###############################################################################
# Phase 6 — AI Engine Tables
#
# Two new tables support the ML enrichment and advisory layers:
#
#   1. regime-log           — per-session regime classification log
#   2. strategy-recommendations — post-market StrategySelector output
#
# NOTE: strategy-config already exists in main.tf (Phase 3).
#       EnrichmentWatchdog reads ENRICHMENT_CONFIG / GLOBAL from that table.
#       No new strategy-config table needed here.
###############################################################################

# ---------------------------------------------------------------------------
# Regime Log Table (Phase 6)
#
#   Written by:  ai_engine/enrichment/signal_enricher.py (_write_regime_log)
#                — non-blocking, via asyncio.create_task, one row per signal
#   Read by:     operator dashboards, Grafana queries, offline analysis
#
#   Key schema:
#     PK = "REGIME#{market}#{symbol}"      e.g. REGIME#NSE#RELIANCE
#     SK = "SESSION#{date}T{signal_time}"  e.g. SESSION#2026-05-11T09:15:00
#
#   Attributes written per row:
#     regime          (S)  — trending | ranging | volatile | crash | unknown
#     confidence      (N)  — 0.0–1.0
#     quality_score   (N)  — 0.0–1.0
#     filtered        (BOOL)
#     strategy_id     (S)
#     enrichment_ms   (N)  — enrichment latency in milliseconds
#     schema_version  (S)  — "4.0"
#     ttl             (N)  — epoch seconds, 30 days
#
#   Access patterns:
#     - Query PK=REGIME#NSE#RELIANCE, SK begins_with SESSION#2026-05-11
#       → today's regime sequence for a symbol
#     - Query PK=REGIME#NSE#RELIANCE, SK between SESSION#... AND SESSION#...
#       → historical regime window for a symbol
#
#   No PITR: this is analytics/advisory data, not trading state.
#   TTL: 30 days — keeps rolling month of regime history for model evaluation.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "regime_log" {
  name         = "${local.table_prefix}-regime-log"
  billing_mode = local.billing_mode
  hash_key     = "PK"
  range_key    = "SK"

  read_capacity  = var.use_provisioned_capacity ? local.default_read_capacity : null
  write_capacity = var.use_provisioned_capacity ? local.default_write_capacity : null

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false # Advisory data — PITR cost not justified
  }

  tags = merge(local.common_tags, {
    Name    = "${local.table_prefix}-regime-log"
    Table   = "regime_log"
    Phase   = "6"
    Service = "ai_engine"
    Purpose = "Per-signal regime classification log — written by SignalEnricher, read by dashboards"
  })
}

# Auto-scaling for regime-log write path (provisioned capacity mode only)
# Write rate tracks signal rate. At NSE peak ~200 signals/min → ~4 WCU average.
# Default_write_capacity=5 covers burst; auto-scaling handles sustained peaks.
resource "aws_appautoscaling_target" "regime_log_write" {
  count              = var.use_provisioned_capacity ? 1 : 0
  max_capacity       = var.autoscaling_max_capacity
  min_capacity       = local.default_write_capacity
  resource_id        = "table/${aws_dynamodb_table.regime_log.name}"
  scalable_dimension = "dynamodb:table:WriteCapacityUnits"
  service_namespace  = "dynamodb"
}

resource "aws_appautoscaling_policy" "regime_log_write" {
  count              = var.use_provisioned_capacity ? 1 : 0
  name               = "${local.table_prefix}-regime-log-write-autoscale"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.regime_log_write[0].resource_id
  scalable_dimension = aws_appautoscaling_target.regime_log_write[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.regime_log_write[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "DynamoDBWriteCapacityUtilization"
    }
    target_value = 70.0
  }
}

# ---------------------------------------------------------------------------
# Strategy Recommendations Table (Phase 6)
#
#   Written by:  ai_engine/agents/strategy_selector.py (StrategySelector)
#                — once per trading session at ~10:15 UTC (post NSE+US close)
#   Read by:     operators, Grafana, ops dashboard
#
#   Key schema:
#     PK = "DATE#{yyyy-mm-dd}"              e.g. DATE#2026-05-11
#     SK = "STRATEGY#{strategy_name}"       e.g. STRATEGY#momentum_nse_v2
#
#   Attributes:
#     recommended_action  (S)  — KEEP | DISABLE | PAPER_ONLY | INCREASE_ALLOCATION
#     reasoning           (S)  — Claude Haiku narrative (≤ 500 chars)
#     affected_strategies (SS) — set of strategy names impacted
#     session_regime      (S)  — dominant regime label for the session
#     generated_by        (S)  — "claude-3-5-haiku-20241022"
#     generated_at        (S)  — ISO timestamp
#     ttl                 (N)  — epoch seconds, 30 days
#
#   Access patterns:
#     - GetItem PK=DATE#today, SK=STRATEGY#momentum_nse_v2 → today's recommendation
#     - Query PK=DATE#today → all strategy recommendations for today
#     - Query with filter SK begins_with "STRATEGY#" → all strategies for a date
#
#   No PITR: advisory output, not trading state. TTL: 30 days.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "strategy_recommendations" {
  name         = "${local.table_prefix}-strategy-recommendations"
  billing_mode = local.billing_mode
  hash_key     = "PK"
  range_key    = "SK"

  read_capacity  = var.use_provisioned_capacity ? local.default_read_capacity : null
  write_capacity = var.use_provisioned_capacity ? local.default_write_capacity : null

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false # Advisory data — PITR cost not justified
  }

  tags = merge(local.common_tags, {
    Name    = "${local.table_prefix}-strategy-recommendations"
    Table   = "strategy_recommendations"
    Phase   = "6"
    Service = "ai_engine"
    Purpose = "Post-market StrategySelector output (Claude Haiku advisory) — one row per strategy per day"
  })
}
