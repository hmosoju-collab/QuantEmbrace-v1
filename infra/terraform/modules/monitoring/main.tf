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
  common_tags    = merge(var.tags, { Module = "monitoring" })
  cluster_name   = var.ecs_cluster_name != "" ? var.ecs_cluster_name : "${var.project}-${var.environment}"
  namespace      = "${var.project}/${var.environment}"
  alarm_prefix   = "${var.project}-${var.environment}"
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
  treat_missing_data  = "breaching"

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
          period = 300, stat = "Average", view = "timeSeries"
          yAxis = { left = { min = 0, max = 100 } }
          annotations = { horizontal = [{ value = var.ecs_cpu_threshold_pct, color = "#ff6961", label = "Threshold" }] }
          region = data.aws_region.current.name
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
          period = 300, stat = "Average", view = "timeSeries"
          yAxis = { left = { min = 0, max = 100 } }
          annotations = { horizontal = [{ value = var.ecs_memory_threshold_pct, color = "#ff6961", label = "Threshold" }] }
          region = data.aws_region.current.name
        }
      },
      # Row 2 — Trading activity
      {
        type = "metric", x = 0, y = 6, width = 8, height = 6
        properties = {
          title = "Daily P&L"
          metrics = [[local.namespace, "DailyPnL", "ServiceName", "risk_engine", { stat = "Minimum", label = "Daily P&L" }]]
          period = 300, view = "timeSeries"
          annotations = {
            horizontal = [
              { value = -var.daily_pnl_loss_alert_threshold, color = "#ffad33", label = "Alert" },
              { value = -var.daily_pnl_loss_halt_threshold,  color = "#ff6961", label = "Halt" }
            ]
          }
          region = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 8, y = 6, width = 8, height = 6
        properties = {
          title = "Order Rejection Rate (%)"
          metrics = [[local.namespace, "OrderRejectionRatePct", "ServiceName", "execution_engine"]]
          period = 300, stat = "Average", view = "timeSeries"
          annotations = { horizontal = [{ value = var.order_rejection_rate_threshold_pct, color = "#ff6961", label = "Threshold" }] }
          region = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 16, y = 6, width = 8, height = 6
        properties = {
          title = "Orders Submitted"
          metrics = [[local.namespace, "OrdersSubmitted", "ServiceName", "execution_engine", { stat = "Sum" }]]
          period = 300, view = "timeSeries"
          region = data.aws_region.current.name
        }
      },
      # Row 3 — Latency + DynamoDB
      {
        type = "metric", x = 0, y = 12, width = 12, height = 6
        properties = {
          title = "Order Placement Latency (ms)"
          metrics = [
            [local.namespace, "OrderPlacementLatencyMs", "ServiceName", "execution_engine", { stat = "p50", label = "P50" }],
            [local.namespace, "OrderPlacementLatencyMs", "ServiceName", "execution_engine", { stat = "p99", label = "P99" }]
          ]
          period = 60, view = "timeSeries"
          annotations = { horizontal = [{ value = var.execution_latency_threshold_ms, color = "#ff6961", label = "P99 threshold" }] }
          region = data.aws_region.current.name
        }
      },
      {
        type = "metric", x = 12, y = 12, width = 12, height = 6
        properties = {
          title = "DynamoDB Consumed Capacity"
          metrics = [
            ["AWS/DynamoDB", "ConsumedReadCapacityUnits",  "TableName", "${local.alarm_prefix}-orders",     { stat = "Sum", label = "Orders RCU" }],
            ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", "${local.alarm_prefix}-orders",     { stat = "Sum", label = "Orders WCU" }],
            ["AWS/DynamoDB", "ConsumedReadCapacityUnits",  "TableName", "${local.alarm_prefix}-risk-state", { stat = "Sum", label = "Risk RCU" }],
            ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", "${local.alarm_prefix}-risk-state", { stat = "Sum", label = "Risk WCU" }]
          ]
          period = 300, view = "timeSeries"
          region = data.aws_region.current.name
        }
      },
      # Row 4 — Error summary
      {
        type = "metric", x = 0, y = 18, width = 24, height = 6
        properties = {
          title = "Error Counts by Service"
          metrics = [for svc in var.service_names : [
            local.namespace, "ErrorCount", "ServiceName", svc, { stat = "Sum" }
          ]]
          period = 300, view = "timeSeries"
          region = data.aws_region.current.name
        }
      }
    ]
  })
}
