###############################################################################
# QuantEmbrace — Backtest Monitoring Module
# SNS alert topic + ~5 CloudWatch alarms in QuantEmbrace/Backtest namespace.
# No Lambda (live module carries Lambda; backtest does not need it).
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

locals {
  common_tags = merge(var.tags, { Module = "monitoring_backtest" })
  namespace   = "QuantEmbrace/Backtest"
}

# SNS topic for backtest alerts (email subscription)
resource "aws_sns_topic" "alerts" {
  name = "quantembrace-backtest-alerts"
  tags = merge(local.common_tags, { Name = "quantembrace-backtest-alerts" })
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# CloudWatch Log Group for worker output
resource "aws_cloudwatch_log_group" "worker" {
  name              = "/quantembrace/backtest/worker"
  retention_in_days = 30
  tags              = merge(local.common_tags, { Name = "quantembrace-backtest-worker-logs" })
}

# Alarm: run failed (FAILED status written to qe-bt-runs → CloudWatch custom metric)
resource "aws_cloudwatch_metric_alarm" "run_failed" {
  alarm_name          = "quantembrace-backtest-run-failed"
  alarm_description   = "A backtest run transitioned to FAILED status"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "RunFailed"
  namespace           = local.namespace
  period              = 60
  statistic           = "Sum"
  threshold           = 1
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = []
  treat_missing_data  = "notBreaching"
  tags                = local.common_tags
}

# Alarm: worker stuck (no checkpoint written in >2h during a running run)
resource "aws_cloudwatch_metric_alarm" "checkpoint_stale" {
  alarm_name          = "quantembrace-backtest-checkpoint-stale"
  alarm_description   = "No checkpoint written in 2h — worker may be stuck or interrupted"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "CheckpointWritten"
  namespace           = local.namespace
  period              = 7200
  statistic           = "Sum"
  threshold           = 1
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching" # only fire when a run is actually active
  tags                = local.common_tags
}

# Alarm: data quality gate failure (Phase 1 DQ check emits metric on failure)
resource "aws_cloudwatch_metric_alarm" "dq_gate_failure" {
  alarm_name          = "quantembrace-backtest-dq-gate-failure"
  alarm_description   = "Pre-snapshot data quality gate failed — run was blocked"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "DQGateFailed"
  namespace           = local.namespace
  period              = 60
  statistic           = "Sum"
  threshold           = 1
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"
  tags                = local.common_tags
}

# Alarm: S3 data bucket PUT errors (lake write failures)
resource "aws_cloudwatch_metric_alarm" "s3_put_errors" {
  alarm_name          = "quantembrace-backtest-s3-put-errors"
  alarm_description   = "Elevated S3 PUT errors on quantembrace-backtest-results"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "5xxErrors"
  namespace           = "AWS/S3"
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  dimensions = {
    BucketName  = "quantembrace-backtest-results"
    FilterId    = "EntireBucket"
  }
  alarm_actions      = [aws_sns_topic.alerts.arn]
  treat_missing_data = "notBreaching"
  tags               = local.common_tags
}

# Alarm: run queue depth (too many runs stuck in CREATED/RUNNING without progress)
resource "aws_cloudwatch_metric_alarm" "run_queue_depth" {
  alarm_name          = "quantembrace-backtest-run-queue-depth"
  alarm_description   = "More than 5 runs in CREATED or RUNNING state — possible deadlock"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "ActiveRunCount"
  namespace           = local.namespace
  period              = 300
  statistic           = "Maximum"
  threshold           = 5
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"
  tags               = local.common_tags
}

# Monthly cost budget for the backtesting-lab cost center.
# Filters by the CostCenter=backtesting-lab tag applied to all backtest resources.
# Alert at 80% actual spend and 100% forecasted.
resource "aws_budgets_budget" "backtest_monthly" {
  name              = "quantembrace-backtest-monthly"
  budget_type       = "COST"
  limit_amount      = tostring(var.monthly_budget_usd)
  limit_unit        = "USD"
  time_period_start = "2026-06-01_00:00"
  time_unit         = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:CostCenter$backtesting-lab"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
  }
}
