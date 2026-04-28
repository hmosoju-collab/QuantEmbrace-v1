###############################################################################
# QuantEmbrace — Monitoring Module Outputs
###############################################################################

# ── SNS Topics ────────────────────────────────────────────────────────────────

output "alerts_sns_topic_arn" {
  description = "ARN of the general trading alerts SNS topic"
  value       = aws_sns_topic.alerts.arn
}

output "kill_switch_sns_topic_arn" {
  description = "ARN of the dedicated kill-switch SNS topic (auto-halt triggers publish here)"
  value       = aws_sns_topic.kill_switch.arn
}

# ── CloudWatch Log Groups (per-service) ───────────────────────────────────────

output "service_log_group_names" {
  description = "Map of service name → CloudWatch log group name"
  value       = { for svc, lg in aws_cloudwatch_log_group.service : svc => lg.name }
}

output "service_log_group_arns" {
  description = "Map of service name → CloudWatch log group ARN"
  value       = { for svc, lg in aws_cloudwatch_log_group.service : svc => lg.arn }
}

# ── ECS Health Alarms ─────────────────────────────────────────────────────────

output "ecs_task_count_alarm_arns" {
  description = "Map of service name → ECS running-task-count alarm ARN"
  value       = { for svc, alarm in aws_cloudwatch_metric_alarm.ecs_task_count : svc => alarm.arn }
}

output "ecs_cpu_alarm_arns" {
  description = "Map of service name → ECS CPU utilisation alarm ARN"
  value       = { for svc, alarm in aws_cloudwatch_metric_alarm.ecs_cpu : svc => alarm.arn }
}

output "ecs_memory_alarm_arns" {
  description = "Map of service name → ECS memory utilisation alarm ARN"
  value       = { for svc, alarm in aws_cloudwatch_metric_alarm.ecs_memory : svc => alarm.arn }
}

output "service_error_alarm_arns" {
  description = "Map of service name → service error-rate alarm ARN"
  value       = { for svc, alarm in aws_cloudwatch_metric_alarm.service_errors : svc => alarm.arn }
}

# ── Trading Alarms ────────────────────────────────────────────────────────────

output "daily_pnl_loss_alert_alarm_arn" {
  description = "ARN of the daily P&L loss investigation alarm"
  value       = aws_cloudwatch_metric_alarm.daily_pnl_loss_alert.arn
}

output "daily_pnl_loss_halt_alarm_arn" {
  description = "ARN of the daily P&L loss automatic trading-halt alarm"
  value       = aws_cloudwatch_metric_alarm.daily_pnl_loss_halt.arn
}

output "order_rejection_alarm_arn" {
  description = "ARN of the order rejection rate alarm"
  value       = aws_cloudwatch_metric_alarm.order_rejection_rate.arn
}

output "no_orders_sentinel_alarm_arn" {
  description = "ARN of the no-orders-submitted sentinel alarm"
  value       = aws_cloudwatch_metric_alarm.no_orders_sentinel.arn
}

output "websocket_gap_alarm_arn" {
  description = "ARN of the WebSocket connectivity gap alarm"
  value       = aws_cloudwatch_metric_alarm.websocket_gap.arn
}

output "data_feed_stale_alarm_arn" {
  description = "ARN of the data feed staleness alarm"
  value       = aws_cloudwatch_metric_alarm.data_feed_stale.arn
}

output "risk_engine_health_alarm_arn" {
  description = "ARN of the risk engine health alarm (triggers kill switch on breach)"
  value       = aws_cloudwatch_metric_alarm.risk_engine_health.arn
}

# ── Infrastructure Alarms ─────────────────────────────────────────────────────

output "execution_latency_alarm_arn" {
  description = "ARN of the execution latency p99 alarm"
  value       = aws_cloudwatch_metric_alarm.execution_latency.arn
}

output "dynamodb_throttles_alarm_arn" {
  description = "ARN of the DynamoDB throttle alarm"
  value       = aws_cloudwatch_metric_alarm.dynamodb_throttles.arn
}

output "daily_cost_alarm_arn" {
  description = "ARN of the AWS estimated daily charges alarm"
  value       = aws_cloudwatch_metric_alarm.daily_cost.arn
}

# ── Dashboard ─────────────────────────────────────────────────────────────────

output "dashboard_name" {
  description = "CloudWatch dashboard name"
  value       = aws_cloudwatch_dashboard.trading.dashboard_name
}

output "dashboard_arn" {
  description = "CloudWatch dashboard ARN"
  value       = aws_cloudwatch_dashboard.trading.dashboard_arn
}
