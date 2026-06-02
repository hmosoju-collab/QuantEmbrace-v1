###############################################################################
# QuantEmbrace — Monitoring Module
#
# Resources
# ─────────
#   SNS topics     alerts (general) + kill-switch (dedicated)
#   Log groups     per-service CloudWatch log groups (30-day retention)
#   ECS alarms     task running count, CPU %, memory % — per service
#   Trading alarms daily P&L loss (alert + halt), order rejection rate,
#                  no-orders sentinel, WebSocket gap, data-feed staleness
#   Infra alarms   execution latency p99, DynamoDB throttles, cost anomaly
#   Dashboard      ECS + latency + DynamoDB + error summary
###############################################################################

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  common_tags  = merge(var.tags, { Module = "monitoring" })
  cluster_name = var.ecs_cluster_name != "" ? var.ecs_cluster_name : "${var.project}-${var.environment}"
  namespace    = "${var.project}/${var.environment}"
  alarm_prefix = "${var.project}-${var.environment}"
}

# =============================================================================
# SNS Topics
# =============================================================================

resource "aws_sns_topic" "alerts" {
  name = "${local.alarm_prefix}-system-alerts"
  tags = merge(local.common_tags, { Name = "${local.alarm_prefix}-system-alerts" })
}

# Dedicated kill-switch topic — subscribed to by all services for sub-5s halt
resource "aws_sns_topic" "kill_switch" {
  name = "${local.alarm_prefix}-kill-switch"
  tags = merge(local.common_tags, { Name = "${local.alarm_prefix}-kill-switch" })
}

resource "aws_sns_topic_subscription" "alert_email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_sns_topic_subscription" "kill_switch_email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.kill_switch.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# =============================================================================
# CloudWatch Log Groups — per service
# =============================================================================

resource "aws_cloudwatch_log_group" "service" {
  for_each = toset(var.service_names)

  name              = "/quantembrace/${var.environment}/${each.key}"
  retention_in_days = var.log_retention_days

  tags = merge(local.common_tags, {
    Name    = "${local.alarm_prefix}-${each.key}-logs"
    Service = each.key
  })
}

# =============================================================================
# ECS Service Health Alarms
# =============================================================================

# Task running count — fires if a service crashes to 0 tasks
resource "aws_cloudwatch_metric_alarm" "ecs_task_count" {
  for_each = toset(var.service_names)

  alarm_name          = "${local.alarm_prefix}-${each.key}-task-count-low"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "RunningTaskCount"
  namespace           = "AWS/ECS"
  period              = 60
  statistic           = "Minimum"
  threshold           = var.ecs_min_running_tasks
  alarm_description   = "${each.key}: running task count below minimum (${var.ecs_min_running_tasks})"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  # notBreaching: platform is EC2 ASG, not ECS — this namespace has no data.
  # Keeping the alarm for future ECS-restore compatibility; breaching=false avoids
  # 5 permanent false-positive alarms that cause operator alert fatigue.
  treat_missing_data = "notBreaching"

  dimensions = {
    ClusterName = local.cluster_name
    ServiceName = "${local.alarm_prefix}-${replace(each.key, "_", "-")}"
  }

  tags = merge(local.common_tags, { Service = each.key, AlarmType = "ecs-health" })
}

# CPU utilization — fires if a service is CPU-saturated (runaway loop)
resource "aws_cloudwatch_metric_alarm" "ecs_cpu" {
  for_each = toset(var.service_names)

  alarm_name          = "${local.alarm_prefix}-${each.key}-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = 300
  statistic           = "Average"
  threshold           = var.ecs_cpu_threshold_pct
  alarm_description   = "${each.key}: CPU utilisation > ${var.ecs_cpu_threshold_pct}%"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    ClusterName = local.cluster_name
    ServiceName = "${local.alarm_prefix}-${replace(each.key, "_", "-")}"
  }

  tags = merge(local.common_tags, { Service = each.key, AlarmType = "ecs-health" })
}

# Memory utilization — fires before OOM kills the container
resource "aws_cloudwatch_metric_alarm" "ecs_memory" {
  for_each = toset(var.service_names)

  alarm_name          = "${local.alarm_prefix}-${each.key}-memory-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "MemoryUtilization"
  namespace           = "AWS/ECS"
  period              = 300
  statistic           = "Average"
  threshold           = var.ecs_memory_threshold_pct
  alarm_description   = "${each.key}: memory utilisation > ${var.ecs_memory_threshold_pct}%"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    ClusterName = local.cluster_name
    ServiceName = "${local.alarm_prefix}-${replace(each.key, "_", "-")}"
  }

  tags = merge(local.common_tags, { Service = each.key, AlarmType = "ecs-health" })
}

# Application error rate — per service, custom metric published by services
resource "aws_cloudwatch_metric_alarm" "service_errors" {
  for_each = toset(var.service_names)

  alarm_name          = "${local.alarm_prefix}-${each.key}-high-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "ErrorCount"
  namespace           = local.namespace
  period              = 300
  statistic           = "Sum"
  threshold           = var.error_rate_threshold
  alarm_description   = "${each.key}: error count > ${var.error_rate_threshold} in 5 min"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { ServiceName = each.key }

  tags = merge(local.common_tags, { Service = each.key, AlarmType = "error-rate" })
}

# =============================================================================
# Trading-Specific Alarms
# =============================================================================

# Daily P&L loss — investigation threshold (softer, fires first)
resource "aws_cloudwatch_metric_alarm" "daily_pnl_loss_alert" {
  alarm_name          = "${local.alarm_prefix}-daily-pnl-loss-alert"
  comparison_operator = "LessThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "DailyPnL"
  namespace           = local.namespace
  period              = 300
  statistic           = "Minimum"
  threshold           = -var.daily_pnl_loss_alert_threshold
  alarm_description   = "Daily P&L loss > ${var.daily_pnl_loss_alert_threshold} — investigation required"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { ServiceName = "risk_engine" }

  tags = merge(local.common_tags, { AlarmType = "trading-pnl" })
}

# Daily P&L loss — halt threshold (harder, triggers kill switch investigation)
resource "aws_cloudwatch_metric_alarm" "daily_pnl_loss_halt" {
  alarm_name          = "${local.alarm_prefix}-daily-pnl-loss-halt"
  comparison_operator = "LessThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "DailyPnL"
  namespace           = local.namespace
  period              = 300
  statistic           = "Minimum"
  threshold           = -var.daily_pnl_loss_halt_threshold
  alarm_description   = "CRITICAL: daily P&L loss > ${var.daily_pnl_loss_halt_threshold} — automatic halt threshold"
  alarm_actions       = [aws_sns_topic.alerts.arn, aws_sns_topic.kill_switch.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { ServiceName = "risk_engine" }

  tags = merge(local.common_tags, { AlarmType = "trading-halt" })
}

# Order rejection rate — possible misconfigured risk limits or broker issue
resource "aws_cloudwatch_metric_alarm" "order_rejection_rate" {
  alarm_name          = "${local.alarm_prefix}-order-rejection-rate-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "OrderRejectionRatePct"
  namespace           = local.namespace
  period              = 300
  statistic           = "Average"
  threshold           = var.order_rejection_rate_threshold_pct
  alarm_description   = "Order rejection rate > ${var.order_rejection_rate_threshold_pct}% in 5 min — check risk limits or broker connectivity"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { ServiceName = "execution_engine" }

  tags = merge(local.common_tags, { AlarmType = "trading-orders" })
}

# No orders submitted during expected trading hours — strategy may be stuck
# (Custom metric: OrdersSubmitted — should be > 0 during market hours)
resource "aws_cloudwatch_metric_alarm" "no_orders_sentinel" {
  alarm_name          = "${local.alarm_prefix}-no-orders-during-market-hours"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  metric_name         = "OrdersSubmitted"
  namespace           = local.namespace
  period              = 1800 # 30-minute window
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "No orders submitted in 30 min during expected market hours — strategy may be stuck"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching" # silence overnight / weekends

  dimensions = { ServiceName = "execution_engine" }

  tags = merge(local.common_tags, { AlarmType = "trading-sentinel" })
}

# WebSocket connectivity gap — broker feed disconnected
resource "aws_cloudwatch_metric_alarm" "websocket_gap" {
  alarm_name          = "${local.alarm_prefix}-websocket-disconnected"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "WebSocketGapSeconds"
  namespace           = local.namespace
  period              = 60
  statistic           = "Maximum"
  threshold           = var.websocket_gap_seconds
  alarm_description   = "WebSocket silent for > ${var.websocket_gap_seconds}s — broker feed may be down"
  alarm_actions       = [aws_sns_topic.alerts.arn, aws_sns_topic.kill_switch.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { ServiceName = "data_ingestion" }

  tags = merge(local.common_tags, { AlarmType = "trading-connectivity" })
}

# Data feed staleness — no ticks received per instrument during market hours
resource "aws_cloudwatch_metric_alarm" "data_feed_stale" {
  alarm_name          = "${local.alarm_prefix}-data-feed-stale"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "DataFeedStalenessSeconds"
  namespace           = local.namespace
  period              = 60
  statistic           = "Maximum"
  threshold           = var.data_staleness_seconds
  alarm_description   = "Data feed stale > ${var.data_staleness_seconds}s during market hours — possible data ingestion failure"
  alarm_actions       = [aws_sns_topic.alerts.arn, aws_sns_topic.kill_switch.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { ServiceName = "data_ingestion" }

  tags = merge(local.common_tags, { AlarmType = "trading-data-quality" })
}

# Risk engine health — if the risk engine is unhealthy, trading must halt
resource "aws_cloudwatch_metric_alarm" "risk_engine_health" {
  alarm_name          = "${local.alarm_prefix}-risk-engine-unhealthy"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HealthCheckSuccess"
  namespace           = local.namespace
  period              = 60
  statistic           = "Average"
  threshold           = 1
  alarm_description   = "CRITICAL: risk engine health check failing — trading must halt"
  alarm_actions       = [aws_sns_topic.alerts.arn, aws_sns_topic.kill_switch.arn]
  treat_missing_data  = "breaching"

  dimensions = { ServiceName = "risk_engine" }

  tags = merge(local.common_tags, { AlarmType = "trading-halt" })
}

# Position concentration — single symbol > threshold % of portfolio
#
# Metric: PositionConcentrationPct (custom, emitted by risk_engine)
# Semantics: maximum single-symbol weight across all live positions.
# Why: a concentrated position amplifies single-name risk; many institutional
# risk frameworks hard-limit single names to 5–20% of NAV.
resource "aws_cloudwatch_metric_alarm" "position_concentration" {
  alarm_name          = "${local.alarm_prefix}-position-concentration-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "PositionConcentrationPct"
  namespace           = "QuantEmbrace/Trading"
  period              = 300
  statistic           = "Maximum"
  threshold           = var.max_position_concentration_pct
  alarm_description   = "Single-symbol position concentration > ${var.max_position_concentration_pct}% of portfolio — review position sizing"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { ServiceName = "risk_engine" }

  tags = merge(local.common_tags, { AlarmType = "risk-portfolio" })
}

# Total exposure approaching limit — gross notional as % of configured cap
#
# Metric: TotalExposurePct (custom, emitted by risk_engine every cycle)
# Semantics: (long_notional + short_notional) / exposure_cap * 100.
# Fires as a warning BEFORE the hard cap is hit and new signals start being
# rejected — gives the operator time to reduce positions gracefully.
resource "aws_cloudwatch_metric_alarm" "total_exposure_warning" {
  alarm_name          = "${local.alarm_prefix}-total-exposure-approaching-limit"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "TotalExposurePct"
  namespace           = "QuantEmbrace/Trading"
  period              = 300
  statistic           = "Maximum"
  threshold           = var.total_exposure_warning_pct
  alarm_description   = "Total gross exposure > ${var.total_exposure_warning_pct}% of configured cap — approaching hard limit"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { ServiceName = "risk_engine" }

  tags = merge(local.common_tags, { AlarmType = "risk-portfolio" })
}

# Kill-switch activation — any activation must be immediately visible
#
# Metric: KillSwitchActivations (custom counter, emitted by risk_engine on
# every activation event including auto-triggers (signal age, loss limit),
# WebSocket disconnect, and manual operator activations).
#
# Why separate from SNS notifications: the alarm appears on the dashboard,
# surfaces in the alarm history, and routes to the kill-switch SNS topic so
# *any* activation wakes up the on-call engineer regardless of channel.
resource "aws_cloudwatch_metric_alarm" "kill_switch_activated" {
  alarm_name          = "${local.alarm_prefix}-kill-switch-activated"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "KillSwitchActivations"
  namespace           = "QuantEmbrace/Trading"
  period              = var.kill_switch_activation_alarm_period
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "CRITICAL: trading kill switch activated — all new order placement is halted"
  alarm_actions       = [aws_sns_topic.alerts.arn, aws_sns_topic.kill_switch.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { ServiceName = "risk_engine" }

  tags = merge(local.common_tags, { AlarmType = "trading-halt" })
}

# =============================================================================
# Infrastructure Alarms
# =============================================================================

# Execution engine order placement latency p99
resource "aws_cloudwatch_metric_alarm" "execution_latency" {
  alarm_name          = "${local.alarm_prefix}-execution-latency-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "OrderPlacementLatencyMs"
  namespace           = local.namespace
  period              = 60
  extended_statistic  = "p99"
  threshold           = var.execution_latency_threshold_ms
  alarm_description   = "P99 order placement latency > ${var.execution_latency_threshold_ms}ms"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { ServiceName = "execution_engine" }

  tags = merge(local.common_tags, { AlarmType = "latency" })
}

# DynamoDB throttled requests
resource "aws_cloudwatch_metric_alarm" "dynamodb_throttles" {
  alarm_name          = "${local.alarm_prefix}-dynamodb-throttles"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "ThrottledRequests"
  namespace           = "AWS/DynamoDB"
  period              = 300
  statistic           = "Sum"
  threshold           = 10
  alarm_description   = "DynamoDB throttled requests — consider provisioned capacity scaling"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  tags = merge(local.common_tags, { AlarmType = "dynamodb" })
}

# AWS estimated daily charges anomaly
resource "aws_cloudwatch_metric_alarm" "daily_cost" {
  alarm_name          = "${local.alarm_prefix}-daily-cost-anomaly"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "EstimatedCharges"
  namespace           = "AWS/Billing"
  period              = 86400
  statistic           = "Maximum"
  threshold           = var.daily_cost_threshold_usd
  alarm_description   = "Estimated daily charges > $${var.daily_cost_threshold_usd}"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { Currency = "USD" }

  tags = merge(local.common_tags, { AlarmType = "cost" })
}

# =============================================================================
# Auto Kill-Switch — WebSocket Disconnect → DynamoDB Write
#
# When the WebSocket gap alarm fires, we need the kill switch activated within
# seconds — not just an SNS notification that a human might not see immediately.
#
# Architecture:
#   CloudWatch Alarm (ALARM state)
#     → EventBridge rule
#     → Lambda (inline Python — writes kill_switch=ACTIVE to DynamoDB)
#
# The Lambda is only created if var.kill_switch_lambda_role_arn is set.
# The alarm still fires SNS in all cases; Lambda makes the DDB write automatic.
# =============================================================================

resource "aws_lambda_function" "auto_kill_switch" {
  count = var.kill_switch_lambda_role_arn != "" ? 1 : 0

  function_name = "${local.alarm_prefix}-auto-kill-switch"
  role          = var.kill_switch_lambda_role_arn
  runtime       = "python3.11"
  handler       = "index.handler"
  timeout       = 10

  environment {
    variables = {
      DYNAMODB_TABLE = var.kill_switch_dynamodb_table != "" ? var.kill_switch_dynamodb_table : "${local.alarm_prefix}-risk-state"
      REGION         = data.aws_region.current.name
    }
  }

  # Inline deployment package — minimal Python, no external deps
  filename         = data.archive_file.kill_switch_lambda[0].output_path
  source_code_hash = data.archive_file.kill_switch_lambda[0].output_base64sha256

  tags = merge(local.common_tags, { Name = "${local.alarm_prefix}-auto-kill-switch" })
}

data "archive_file" "kill_switch_lambda" {
  count       = var.kill_switch_lambda_role_arn != "" ? 1 : 0
  type        = "zip"
  output_path = "${path.module}/auto_kill_switch.zip"

  source {
    filename = "index.py"
    content  = <<-PYTHON
      """
      Auto kill-switch Lambda.

      Triggered by EventBridge when a WebSocket-gap or data-staleness
      CloudWatch alarm transitions to ALARM state.

      Writes kill_switch = ACTIVE to DynamoDB risk-state table so the
      risk engine detects it within 1 second (its DynamoDB poll interval).
      """
      import os, json, boto3
      from datetime import datetime, timezone

      DYNAMODB_TABLE = os.environ["DYNAMODB_TABLE"]
      ddb = boto3.client("dynamodb", region_name=os.environ["REGION"])

      def handler(event, context):
          alarm_name = event.get("detail", {}).get("alarmName", "unknown")
          new_state  = event.get("detail", {}).get("state", {}).get("value", "")

          if new_state != "ALARM":
              # Only act on ALARM transitions, not OK or INSUFFICIENT_DATA
              return {"status": "skipped", "reason": f"state={new_state}"}

          now = datetime.now(timezone.utc).isoformat()
          ddb.put_item(
              TableName=DYNAMODB_TABLE,
              Item={
                  "PK":             {"S": "KILLSWITCH"},
                  "SK":             {"S": "GLOBAL"},
                  "active":         {"BOOL": True},
                  "status":         {"S": "ACTIVE"},
                  "scope":          {"S": "GLOBAL"},
                  "reason":         {"S": f"Auto-activated by alarm: {alarm_name}"},
                  "activated_at":   {"S": now},
                  "activated_by":   {"S": "cloudwatch-auto-kill-switch-lambda"},
                  "updated_at":     {"S": now},
                  "schema_version": {"S": "1.0"},
              },
          )
          print(json.dumps({
              "event": "kill_switch_activated",
              "alarm": alarm_name,
              "activated_at": now,
          }))
          return {"status": "activated", "alarm": alarm_name}
    PYTHON
  }
}

resource "aws_lambda_permission" "allow_eventbridge" {
  count = var.kill_switch_lambda_role_arn != "" ? 1 : 0

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.auto_kill_switch[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.alarm_to_kill_switch[0].arn
}

# EventBridge rule: CloudWatch alarm state change → Lambda
resource "aws_cloudwatch_event_rule" "alarm_to_kill_switch" {
  count = var.kill_switch_lambda_role_arn != "" ? 1 : 0

  name        = "${local.alarm_prefix}-ws-disconnect-kill-switch"
  description = "Triggers auto kill-switch Lambda when WebSocket or data-feed alarm fires"

  event_pattern = jsonencode({
    source      = ["aws.cloudwatch"],
    detail-type = ["CloudWatch Alarm State Change"],
    detail = {
      alarmName = [
        "${local.alarm_prefix}-websocket-disconnected",
        "${local.alarm_prefix}-data-feed-stale",
        "${local.alarm_prefix}-risk-engine-unhealthy",
      ],
      state = { value = ["ALARM"] }
    }
  })

  tags = merge(local.common_tags, { Name = "${local.alarm_prefix}-ws-disconnect-kill-switch" })
}

resource "aws_cloudwatch_event_target" "kill_switch_lambda" {
  count = var.kill_switch_lambda_role_arn != "" ? 1 : 0

  rule      = aws_cloudwatch_event_rule.alarm_to_kill_switch[0].name
  target_id = "AutoKillSwitchLambda"
  arn       = aws_lambda_function.auto_kill_switch[0].arn
}

# CloudWatch log group for the Lambda
resource "aws_cloudwatch_log_group" "kill_switch_lambda" {
  count = var.kill_switch_lambda_role_arn != "" ? 1 : 0

  name              = "/aws/lambda/${local.alarm_prefix}-auto-kill-switch"
  retention_in_days = var.log_retention_days

  tags = local.common_tags
}

# =============================================================================
# CloudWatch Dashboard
# =============================================================================

# =============================================================================
# Zerodha Rate-Limit Alarms — QuantEmbrace/ZerodhaRateLimit namespace
# (ADR-012: Zerodha Full-Capacity Rate Limit Architecture)
#
# Namespace:  QuantEmbrace/ZerodhaRateLimit
# Dimensions: Service=execution_engine  (all 6 metrics emitted by BulkOrderPoller
#             and ZerodhaRateLimiter._drain_loop via boto3 put_metric_data)
#
# Metric catalogue
# ────────────────
#   ZerodhaAPICallsPerSecond      Smoothed req/sec measured by rate_limiter.py
#   ZerodhaTokenBucketLevel       Current token count in the bucket (0–15)
#   ZerodhaRateLimitErrors        Count of HTTP 429 or acquire-timeout events
#   ZerodhaFillDetectionLatencyMs Time (ms) from broker fill to DynamoDB write
#   ZerodhaOpenOrderCount         Current open order count (drives poll interval)
#   ZerodhaQuoteSpreadBps         Per-instrument bid/ask spread from LiveQuotePoller
#
# Priority mapping to SNS topics
# ───────────────────────────────
#   P0  → aws_sns_topic.alerts  + aws_sns_topic.kill_switch  (paging severity)
#   P1  → aws_sns_topic.alerts  only                         (investigation)
# =============================================================================

locals {
  zerodha_namespace = "QuantEmbrace/ZerodhaRateLimit"
}

# ── P0 Alarm: Rate-limit 429 errors ──────────────────────────────────────────
#
# Any sustained 429 from Zerodha means we have exceeded 10 req/sec.  This is
# a critical defect — the token bucket has failed to prevent over-use, or a
# new code path bypassed the rate limiter entirely.  Immediate investigation
# required: active positions may have undetected fills during the error window.
#
# SNS: alerts + kill_switch (P0 — pages on-call)
# eval: 1 × 60s window (first occurrence triggers)

resource "aws_cloudwatch_metric_alarm" "zerodha_rate_limit_errors" {
  alarm_name          = "${local.alarm_prefix}-zerodha-rate-limit-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = var.zerodha_rate_limit_error_evaluation_periods
  metric_name         = "ZerodhaRateLimitErrors"
  namespace           = local.zerodha_namespace
  period              = 60
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = <<-EOT
    P0: Zerodha API returned HTTP 429 (rate limit exceeded).
    Token bucket enforcement has failed or a code path bypassed ZerodhaRateLimiter.
    Active positions may have undetected fills during this error window.
    Action: inspect rate_limiter utilization metrics and recently deployed code paths.
  EOT
  alarm_actions       = [aws_sns_topic.alerts.arn, aws_sns_topic.kill_switch.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { Service = "execution_engine" }

  tags = merge(local.common_tags, { AlarmType = "zerodha-rate-limit", Priority = "P0" })
}

# ── P1 Alarm: Token bucket sustained depletion ────────────────────────────────
#
# When the token bucket falls below 1 token for ≥30s, every caller is
# queuing behind the 10 req/sec refill cadence.  CRITICAL-priority cancels
# still preempt the queue, but MEDIUM/LOW callers (position monitor, candle
# stream) begin accumulating latency.  30-second window eliminates noise from
# normal burst absorption (e.g. MIS square-off sends 8 cancels in <1 second).
#
# SNS: alerts only (P1 — investigation, not immediate halt)
# eval: 3 × 10s = 30s minimum sustained depletion

resource "aws_cloudwatch_metric_alarm" "zerodha_token_bucket_depletion" {
  alarm_name          = "${local.alarm_prefix}-zerodha-token-bucket-depleted"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  metric_name         = "ZerodhaTokenBucketLevel"
  namespace           = local.zerodha_namespace
  period              = 10
  statistic           = "Minimum"
  threshold           = var.zerodha_token_depletion_threshold
  alarm_description   = <<-EOT
    P1: Zerodha token bucket sustained below ${var.zerodha_token_depletion_threshold} token for 30s.
    All callers are queuing behind the 10 req/sec refill rate.
    Action: review PHASE_BUDGET allocation and open order count to identify
    which priority tier is consuming the budget.  Consider reducing poll frequency
    or deferring LOW-priority calls during high-activity phases.
  EOT
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { Service = "execution_engine" }

  tags = merge(local.common_tags, { AlarmType = "zerodha-rate-limit", Priority = "P1" })
}

# ── P1 Alarm: Fill detection latency P95 ─────────────────────────────────────
#
# BulkOrderPoller must detect fills within 1 second (P95) in NORMAL phase.
# Exceeding 1000ms P95 sustained for 2 minutes indicates the poller is behind
# schedule — likely rate-limit contention, DynamoDB write latency spike, or
# the poll interval adaptive logic has selected an interval that is too slow
# for the current open order count.
#
# SNS: alerts only (P1 — investigation)
# eval: 2 × 60s = 2 minutes of sustained degradation

resource "aws_cloudwatch_metric_alarm" "zerodha_fill_detection_latency" {
  alarm_name          = "${local.alarm_prefix}-zerodha-fill-latency-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "ZerodhaFillDetectionLatencyMs"
  namespace           = local.zerodha_namespace
  period              = 60
  extended_statistic  = "p95"
  threshold           = var.zerodha_fill_latency_p95_threshold_ms
  alarm_description   = <<-EOT
    P1: Zerodha fill detection P95 latency > ${var.zerodha_fill_latency_p95_threshold_ms}ms sustained for 2 min.
    BulkOrderPoller is behind schedule — fills are taking longer than 1 second to appear in DynamoDB.
    Action: check open order count (may need to reduce min_poll_interval_ms), inspect DynamoDB
    write latency, and verify rate limiter is not queuing HIGH-priority tokens behind other callers.
  EOT
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { Service = "execution_engine" }

  tags = merge(local.common_tags, { AlarmType = "zerodha-fill-detection", Priority = "P1" })
}

# =============================================================================
# CloudWatch Dashboard
# =============================================================================

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${local.alarm_prefix}-overview"

  dashboard_body = jsonencode({
    widgets = [
      # Row 1 — ECS health
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title = "ECS CPU Utilization (%)"
          metrics = [for svc in var.service_names : [
            "AWS/ECS", "CPUUtilization",
            "ClusterName", local.cluster_name,
            "ServiceName", "${local.alarm_prefix}-${replace(svc, "_", "-")}"
          ]]
          period      = 300, stat = "Average", view = "timeSeries"
          yAxis       = { left = { min = 0, max = 100 } }
          annotations = { horizontal = [{ value = var.ecs_cpu_threshold_pct, color = "#ff6961", label = "Threshold" }] }
          region      = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title = "ECS Memory Utilization (%)"
          metrics = [for svc in var.service_names : [
            "AWS/ECS", "MemoryUtilization",
            "ClusterName", local.cluster_name,
            "ServiceName", "${local.alarm_prefix}-${replace(svc, "_", "-")}"
          ]]
          period      = 300, stat = "Average", view = "timeSeries"
          yAxis       = { left = { min = 0, max = 100 } }
          annotations = { horizontal = [{ value = var.ecs_memory_threshold_pct, color = "#ff6961", label = "Threshold" }] }
          region      = data.aws_region.current.name
        }
      },
      # Row 2 — Trading activity
      {
        type = "metric", x = 0, y = 6, width = 8, height = 6
        properties = {
          title   = "Daily P&L"
          metrics = [[local.namespace, "DailyPnL", "ServiceName", "risk_engine", { stat = "Minimum", label = "Daily P&L" }]]
          period  = 300, view = "timeSeries"
          annotations = {
            horizontal = [
              { value = -var.daily_pnl_loss_alert_threshold, color = "#ffad33", label = "Alert" },
              { value = -var.daily_pnl_loss_halt_threshold, color = "#ff6961", label = "Halt" }
            ]
          }
          region = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 8, y = 6, width = 8, height = 6
        properties = {
          title       = "Order Rejection Rate (%)"
          metrics     = [[local.namespace, "OrderRejectionRatePct", "ServiceName", "execution_engine"]]
          period      = 300, stat = "Average", view = "timeSeries"
          annotations = { horizontal = [{ value = var.order_rejection_rate_threshold_pct, color = "#ff6961", label = "Threshold" }] }
          region      = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 16, y = 6, width = 8, height = 6
        properties = {
          title   = "Orders Submitted"
          metrics = [[local.namespace, "OrdersSubmitted", "ServiceName", "execution_engine", { stat = "Sum" }]]
          period  = 300, view = "timeSeries"
          region  = data.aws_region.current.name
        }
      },
      # Row 3 — Risk portfolio (concentration, exposure, kill-switch)
      {
        type = "metric", x = 0, y = 12, width = 8, height = 6
        properties = {
          title       = "Position Concentration (%)"
          metrics     = [["QuantEmbrace/Trading", "PositionConcentrationPct", "ServiceName", "risk_engine", { stat = "Maximum", label = "Max single-symbol %" }]]
          period      = 300, view = "timeSeries"
          yAxis       = { left = { min = 0, max = 100 } }
          annotations = { horizontal = [{ value = var.max_position_concentration_pct, color = "#ff6961", label = "Threshold" }] }
          region      = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 8, y = 12, width = 8, height = 6
        properties = {
          title   = "Total Exposure (% of cap)"
          metrics = [["QuantEmbrace/Trading", "TotalExposurePct", "ServiceName", "risk_engine", { stat = "Maximum", label = "Gross exposure %" }]]
          period  = 300, view = "timeSeries"
          yAxis   = { left = { min = 0, max = 100 } }
          annotations = {
            horizontal = [
              { value = var.total_exposure_warning_pct, color = "#ffad33", label = "Warning" },
              { value = 100, color = "#ff6961", label = "Hard cap" }
            ]
          }
          region = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 16, y = 12, width = 8, height = 6
        properties = {
          title       = "Kill Switch Activations"
          metrics     = [["QuantEmbrace/Trading", "KillSwitchActivations", "ServiceName", "risk_engine", { stat = "Sum", label = "Activations" }]]
          period      = 300, view = "timeSeries"
          annotations = { horizontal = [{ value = 1, color = "#ff6961", label = "Any activation" }] }
          region      = data.aws_region.current.name
        }
      },
      # Row 4 — Latency + DynamoDB
      {
        type = "metric", x = 0, y = 18, width = 12, height = 6
        properties = {
          title = "Order Placement Latency (ms)"
          metrics = [
            [local.namespace, "OrderPlacementLatencyMs", "ServiceName", "execution_engine", { stat = "p50", label = "P50" }],
            [local.namespace, "OrderPlacementLatencyMs", "ServiceName", "execution_engine", { stat = "p99", label = "P99" }]
          ]
          period      = 60, view = "timeSeries"
          annotations = { horizontal = [{ value = var.execution_latency_threshold_ms, color = "#ff6961", label = "P99 threshold" }] }
          region      = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 12, y = 18, width = 12, height = 6
        properties = {
          title = "DynamoDB Consumed Capacity"
          metrics = [
            ["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", "${local.alarm_prefix}-orders", { stat = "Sum", label = "Orders RCU" }],
            ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", "${local.alarm_prefix}-orders", { stat = "Sum", label = "Orders WCU" }],
            ["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", "${local.alarm_prefix}-risk-state", { stat = "Sum", label = "Risk RCU" }],
            ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", "${local.alarm_prefix}-risk-state", { stat = "Sum", label = "Risk WCU" }]
          ]
          period = 300, view = "timeSeries"
          region = data.aws_region.current.name
        }
      },
      # Row 5 — Error summary
      {
        type = "metric", x = 0, y = 24, width = 24, height = 6
        properties = {
          title = "Error Counts by Service"
          metrics = [for svc in var.service_names : [
            local.namespace, "ErrorCount", "ServiceName", svc, { stat = "Sum" }
          ]]
          period = 300, view = "timeSeries"
          region = data.aws_region.current.name
        }
      },
      # Row 6 — Zerodha rate-limit health (ADR-012)
      {
        type = "metric", x = 0, y = 30, width = 6, height = 6
        properties = {
          title = "Zerodha API calls/sec (actual vs 10 req/sec limit)"
          metrics = [
            [local.zerodha_namespace, "ZerodhaAPICallsPerSecond", "Service", "execution_engine",
            { stat = "Average", label = "Actual req/sec", color = "#1f77b4" }]
          ]
          period      = 10, view = "timeSeries"
          yAxis       = { left = { min = 0, max = 12 } }
          annotations = { horizontal = [{ value = 10, color = "#ff6961", label = "Hard limit" }] }
          region      = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 6, y = 30, width = 6, height = 6
        properties = {
          title = "Token Bucket Level (0–15)"
          metrics = [
            [local.zerodha_namespace, "ZerodhaTokenBucketLevel", "Service", "execution_engine",
            { stat = "Minimum", label = "Min tokens", color = "#2ca02c" }],
            [local.zerodha_namespace, "ZerodhaTokenBucketLevel", "Service", "execution_engine",
            { stat = "Average", label = "Avg tokens", color = "#1f77b4" }]
          ]
          period = 10, view = "timeSeries"
          yAxis  = { left = { min = 0, max = 15 } }
          annotations = {
            horizontal = [
              { value = var.zerodha_token_depletion_threshold, color = "#ff6961",
              label = "Depletion alarm threshold" }
            ]
          }
          region = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 12, y = 30, width = 6, height = 6
        properties = {
          title = "Fill Detection Latency (ms)"
          metrics = [
            [local.zerodha_namespace, "ZerodhaFillDetectionLatencyMs", "Service", "execution_engine",
            { stat = "p50", label = "P50", color = "#2ca02c" }],
            [local.zerodha_namespace, "ZerodhaFillDetectionLatencyMs", "Service", "execution_engine",
            { stat = "p95", label = "P95", color = "#ff7f0e" }],
            [local.zerodha_namespace, "ZerodhaFillDetectionLatencyMs", "Service", "execution_engine",
            { stat = "p99", label = "P99", color = "#d62728" }]
          ]
          period = 60, view = "timeSeries"
          annotations = {
            horizontal = [
              { value = var.zerodha_fill_latency_p95_threshold_ms, color = "#ff6961",
              label = "P95 alarm threshold" }
            ]
          }
          region = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 18, y = 30, width = 6, height = 6
        properties = {
          title = "Zerodha Rate-Limit Errors (429 / timeouts)"
          metrics = [
            [local.zerodha_namespace, "ZerodhaRateLimitErrors", "Service", "execution_engine",
            { stat = "Sum", label = "Errors (sum/min)", color = "#d62728" }]
          ]
          period      = 60, view = "timeSeries"
          annotations = { horizontal = [{ value = 0, color = "#ff6961", label = "Any error = P0 alarm" }] }
          region      = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 0, y = 36, width = 12, height = 6
        properties = {
          title = "Open Order Count (drives BulkOrderPoller interval)"
          metrics = [
            [local.zerodha_namespace, "ZerodhaOpenOrderCount", "Service", "execution_engine",
            { stat = "Maximum", label = "Open orders", color = "#9467bd" }]
          ]
          period = 10, view = "timeSeries"
          annotations = {
            horizontal = [
              { value = 2, color = "#aec7e8", label = "1s interval" },
              { value = 5, color = "#ffbb78", label = "500ms interval" },
              { value = 6, color = "#ff9896", label = "300ms interval" }
            ]
          }
          region = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 12, y = 36, width = 12, height = 6
        properties = {
          title = "Quote Spread (bps) — LiveQuotePoller (spread gate threshold)"
          metrics = [
            [local.zerodha_namespace, "ZerodhaQuoteSpreadBps", "Service", "execution_engine",
            { stat = "p95", label = "P95 spread (bps)", color = "#e377c2" }],
            [local.zerodha_namespace, "ZerodhaQuoteSpreadBps", "Service", "execution_engine",
            { stat = "Maximum", label = "Max spread (bps)", color = "#d62728" }]
          ]
          period = 60, view = "timeSeries"
          annotations = {
            horizontal = [{ value = 50, color = "#ff6961", label = "Spread gate (50 bps default)" }]
          }
          region = data.aws_region.current.name
        }
      },
      # Row 8 — Phase 5 operations: lifecycle, lag, stale data, drift
      {
        type = "metric", x = 0, y = 42, width = 8, height = 6
        properties = {
          title = "Order Lifecycle"
          metrics = [
            [local.namespace, "OrdersSubmitted", "ServiceName", "execution_engine", { stat = "Sum", label = "Submitted" }],
            [local.namespace, "OrderPlacementErrors", "ServiceName", "execution_engine", { stat = "Sum", label = "Placement errors" }],
            [local.namespace, "OrderRejectionRatePct", "ServiceName", "execution_engine", { stat = "Average", label = "Reject rate %" }],
            [local.namespace, "PaperOrdersSimulated", "ServiceName", "execution_engine", { stat = "Sum", label = "Paper simulated" }]
          ]
          period = 60, view = "timeSeries"
          region = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 8, y = 42, width = 8, height = 6
        properties = {
          title = "Kafka Lag by Consumer Group"
          metrics = [
            [local.namespace, "KafkaConsumerLag", "ConsumerGroup", "strategy-v1", { stat = "Maximum", label = "strategy-v1" }],
            [local.namespace, "KafkaConsumerLag", "ConsumerGroup", "risk-v1", { stat = "Maximum", label = "risk-v1" }],
            [local.namespace, "KafkaConsumerLag", "ConsumerGroup", "execution-v1", { stat = "Maximum", label = "execution-v1" }],
            [local.namespace, "KafkaRetryMessages", "SourceService", "execution_engine", { stat = "Sum", label = "execution retry" }],
            [local.namespace, "KafkaDLQMessages", "SourceService", "execution_engine", { stat = "Sum", label = "execution DLQ" }]
          ]
          period      = 60, view = "timeSeries"
          annotations = { horizontal = [{ value = 0, color = "#ff6961", label = "Target before live" }] }
          region      = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 16, y = 42, width = 8, height = 6
        properties = {
          title = "Stale Ticks and Candles"
          metrics = [
            [local.namespace, "DataFeedStalenessSeconds", "ServiceName", "data_ingestion", { stat = "Maximum", label = "Tick stale seconds" }],
            [local.namespace, "CandleCacheStalenessSeconds", "ServiceName", "data_ingestion", { stat = "Maximum", label = "Candle stale seconds" }],
            [local.namespace, "WebSocketGapSeconds", "ServiceName", "data_ingestion", { stat = "Maximum", label = "WebSocket gap" }]
          ]
          period      = 60, view = "timeSeries"
          annotations = { horizontal = [{ value = var.data_staleness_seconds, color = "#ff6961", label = "Stale data threshold" }] }
          region      = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 0, y = 48, width = 8, height = 6
        properties = {
          title = "Position Drift"
          metrics = [
            [local.namespace, "PositionDriftQuantity", "ServiceName", "execution_engine", { stat = "Maximum", label = "Drift qty" }],
            [local.namespace, "PositionMismatchCount", "ServiceName", "execution_engine", { stat = "Sum", label = "Mismatches" }]
          ]
          period      = 60, view = "timeSeries"
          annotations = { horizontal = [{ value = 0, color = "#ff6961", label = "Must be zero" }] }
          region      = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 8, y = 48, width = 8, height = 6
        properties = {
          title = "PnL and NAV"
          metrics = [
            [local.namespace, "DailyPnL", "ServiceName", "risk_engine", { stat = "Minimum", label = "Daily PnL" }],
            [local.namespace, "NAV", "ServiceName", "risk_engine", { stat = "Minimum", label = "NAV" }],
            [local.namespace, "DailyLossPct", "ServiceName", "risk_engine", { stat = "Maximum", label = "Daily loss %" }]
          ]
          period = 300, view = "timeSeries"
          region = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 16, y = 48, width = 8, height = 6
        properties = {
          title = "Broker Latency and Rejects"
          metrics = [
            [local.namespace, "OrderPlacementLatencyMs", "ServiceName", "execution_engine", { stat = "p99", label = "Order p99 ms" }],
            [local.zerodha_namespace, "ZerodhaFillDetectionLatencyMs", "Service", "execution_engine", { stat = "p99", label = "Fill p99 ms" }],
            [local.zerodha_namespace, "ZerodhaRateLimitErrors", "Service", "execution_engine", { stat = "Sum", label = "429/timeouts" }],
            [local.namespace, "OrderRejectionRatePct", "ServiceName", "execution_engine", { stat = "Average", label = "Reject %" }]
          ]
          period = 60, view = "timeSeries"
          region = data.aws_region.current.name
        }
      }
    ]
  })
}

# =============================================================================
# Phase 8 (ADR-015) Alarms
# =============================================================================

# ── RiskV1LagHigh ─────────────────────────────────────────────────────────────
#
# Metric: KafkaConsumerLag (custom, emitted by KafkaLagWatchdog in risk_engine)
# Namespace: QuantEmbrace/ExecutionEngine  (risk_engine emits under execution ns
#            for uniformity; adjust if separated)
# Fires when the risk-v1 consumer group is > 500 messages behind the head of
# signals.pending. At normal throughput (~10 signals/s) this represents ~50s of
# unprocessed backlog — enough to indicate the risk engine is CPU-starved or
# stuck. The KafkaLagWatchdog will auto-activate the kill switch after 3
# consecutive checks above threshold, so this alarm gives earlier visibility.
#
# Why evaluation_periods=2: a single slow GC pause can briefly spike lag.
# Two consecutive 30s windows (1 min total) confirms a sustained backlog.
#
# SNS: alerts only (P1) — the KafkaLagWatchdog activates kill switch directly.
# =============================================================================

resource "aws_cloudwatch_metric_alarm" "risk_v1_lag_high" {
  alarm_name          = "${local.alarm_prefix}-risk-v1-consumer-lag-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "KafkaConsumerLag"
  namespace           = "QuantEmbrace/RiskEngine"
  period              = 30
  statistic           = "Maximum"
  threshold           = 500
  alarm_description   = "risk-v1 Kafka consumer lag > 500 messages — risk engine may be overloaded or stuck"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    ServiceName   = "risk_engine"
    ConsumerGroup = "risk-v1"
  }

  tags = merge(local.common_tags, {
    AlarmType = "phase8-kafka-lag"
    Phase     = "8"
  })
}

# ── OrphanPositionDetected ────────────────────────────────────────────────────
#
# Metric: OrphanPositionDetected (custom Count, emitted by OrphanDetector in
#         execution_engine every 30s cycle when at least one orphan is found)
# Namespace: QuantEmbrace/ExecutionEngine
#
# An orphan is a FILLED entry order with no active protective stop-loss child.
# The OrphanDetector is alert-only (ADR-015 §5.4 — no auto-flatten on orphan).
# This alarm ensures the on-call engineer is paged within 1 minute of detection.
#
# Why evaluation_periods=1: an orphan at any point requires immediate attention.
# treat_missing_data=notBreaching: silence when market is closed and no fills occur.
#
# SNS: alerts + kill_switch (P0 — unprotected position is a live risk event)
# =============================================================================

resource "aws_cloudwatch_metric_alarm" "orphan_position_detected" {
  alarm_name          = "${local.alarm_prefix}-orphan-position-detected"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "OrphanPositionDetected"
  namespace           = "QuantEmbrace/ExecutionEngine"
  period              = 60
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "CRITICAL: FILLED position detected without active protective stop-loss — manual review required (no auto-flatten per ADR-015)"
  alarm_actions       = [aws_sns_topic.alerts.arn, aws_sns_topic.kill_switch.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { Service = "execution_engine" }

  tags = merge(local.common_tags, {
    AlarmType = "phase8-orphan-position"
    Phase     = "8"
  })
}

# ── ReconciliationHaltActive ──────────────────────────────────────────────────
#
# Metric: ReconciliationHaltActive (custom Gauge 0/1, emitted by risk_engine
#         every signal-validation cycle when reconciliation_required=True)
# Namespace: QuantEmbrace/RiskEngine
#
# When the reconciliation_required flag is set, the risk engine rejects all
# non-closeout signals. This alarm fires if the halt remains active for > 5
# minutes, which indicates the operator has not yet cleared it after setting it
# (possibly forgot, or the reconcile.py script has a bug).
#
# evaluation_periods=1, period=300: fires after a single 5-minute window where
# the gauge is non-zero, giving the operator time to run reconcile.py before
# the alarm rings.
#
# SNS: alerts only (P1) — the halt itself is the safety mechanism; this alarm
#      is a reminder to clear it promptly.
# =============================================================================

resource "aws_cloudwatch_metric_alarm" "reconciliation_halt_active" {
  alarm_name          = "${local.alarm_prefix}-reconciliation-halt-active"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "ReconciliationHaltActive"
  namespace           = "QuantEmbrace/RiskEngine"
  period              = 300
  statistic           = "Maximum"
  threshold           = 1
  alarm_description   = "Reconciliation halt has been active for > 5 minutes — run scripts/ops/reconcile.py --clear after verifying position accuracy"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { ServiceName = "risk_engine" }

  tags = merge(local.common_tags, {
    AlarmType = "phase8-reconciliation-halt"
    Phase     = "8"
  })
}
