output "sns_alert_arn" {
  value       = aws_sns_topic.alerts.arn
  description = "quantembrace-backtest-alerts SNS topic ARN"
}

output "log_group_name" {
  value       = aws_cloudwatch_log_group.worker.name
  description = "Worker CloudWatch log group name"
}

output "budget_name" {
  value       = aws_budgets_budget.backtest_monthly.name
  description = "Monthly cost budget name"
}
