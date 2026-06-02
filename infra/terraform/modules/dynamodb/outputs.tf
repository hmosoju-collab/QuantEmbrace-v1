###############################################################################
# QuantEmbrace — DynamoDB Module Outputs
###############################################################################

output "orders_table_name" {
  description = "Name of the orders DynamoDB table"
  value       = aws_dynamodb_table.orders.name
}

output "orders_table_arn" {
  description = "ARN of the orders DynamoDB table"
  value       = aws_dynamodb_table.orders.arn
}

output "positions_table_name" {
  description = "Name of the positions DynamoDB table"
  value       = aws_dynamodb_table.positions.name
}

output "positions_table_arn" {
  description = "ARN of the positions DynamoDB table"
  value       = aws_dynamodb_table.positions.arn
}

output "latest_prices_table_name" {
  description = "Name of the latest prices DynamoDB table"
  value       = aws_dynamodb_table.latest_prices.name
}

output "latest_prices_table_arn" {
  description = "ARN of the latest prices DynamoDB table"
  value       = aws_dynamodb_table.latest_prices.arn
}

output "risk_state_table_name" {
  description = "Name of the risk state DynamoDB table"
  value       = aws_dynamodb_table.risk_state.name
}

output "risk_state_table_arn" {
  description = "ARN of the risk state DynamoDB table"
  value       = aws_dynamodb_table.risk_state.arn
}

output "strategy_state_table_name" {
  description = "Name of the strategy state DynamoDB table"
  value       = aws_dynamodb_table.strategy_state.name
}

output "strategy_state_table_arn" {
  description = "ARN of the strategy state DynamoDB table"
  value       = aws_dynamodb_table.strategy_state.arn
}

output "features_table_name" {
  description = "Name of the features DynamoDB table (Phase 5)"
  value       = aws_dynamodb_table.features.name
}

output "features_table_arn" {
  description = "ARN of the features DynamoDB table (Phase 5)"
  value       = aws_dynamodb_table.features.arn
}

output "strategy_config_table_name" {
  description = "Name of the strategy-config DynamoDB table (Phase 3)"
  value       = aws_dynamodb_table.strategy_config.name
}

output "strategy_config_table_arn" {
  description = "ARN of the strategy-config DynamoDB table (Phase 3)"
  value       = aws_dynamodb_table.strategy_config.arn
}

# ── Phase 6 — AI Engine Tables ───────────────────────────────────────────────

output "regime_log_table_name" {
  description = "Name of the regime-log DynamoDB table (Phase 6)"
  value       = aws_dynamodb_table.regime_log.name
}

output "regime_log_table_arn" {
  description = "ARN of the regime-log DynamoDB table (Phase 6)"
  value       = aws_dynamodb_table.regime_log.arn
}

output "strategy_recommendations_table_name" {
  description = "Name of the strategy-recommendations DynamoDB table (Phase 6)"
  value       = aws_dynamodb_table.strategy_recommendations.name
}

output "strategy_recommendations_table_arn" {
  description = "ARN of the strategy-recommendations DynamoDB table (Phase 6)"
  value       = aws_dynamodb_table.strategy_recommendations.arn
}

# ── Sessions Table (ADR-022) ─────────────────────────────────────────────────

output "sessions_table_name" {
  description = "Name of the sessions DynamoDB table (Zerodha daily token store)"
  value       = aws_dynamodb_table.sessions.name
}

output "sessions_table_arn" {
  description = "ARN of the sessions DynamoDB table"
  value       = aws_dynamodb_table.sessions.arn
}

# ── Phase 8 — Signal Inbox / Outbox ─────────────────────────────────────────

output "signal_inbox_table_name" {
  description = "Name of the signal-inbox DynamoDB table (Phase 8 ADR-015 F6)"
  value       = aws_dynamodb_table.signal_inbox.name
}

output "signal_inbox_table_arn" {
  description = "ARN of the signal-inbox DynamoDB table (Phase 8 ADR-015 F6)"
  value       = aws_dynamodb_table.signal_inbox.arn
}

output "signal_outbox_table_name" {
  description = "Name of the signal-outbox DynamoDB table (Phase 8 ADR-015 F6)"
  value       = aws_dynamodb_table.signal_outbox.name
}

output "signal_outbox_table_arn" {
  description = "ARN of the signal-outbox DynamoDB table (Phase 8 ADR-015 F6)"
  value       = aws_dynamodb_table.signal_outbox.arn
}

output "all_table_arns" {
  description = "List of all DynamoDB table ARNs"
  value = [
    aws_dynamodb_table.orders.arn,
    aws_dynamodb_table.positions.arn,
    aws_dynamodb_table.latest_prices.arn,
    aws_dynamodb_table.risk_state.arn,
    aws_dynamodb_table.sessions.arn,
    aws_dynamodb_table.strategy_state.arn,
    aws_dynamodb_table.candle_cache.arn,
    aws_dynamodb_table.strategy_config.arn,
    aws_dynamodb_table.features.arn,
    aws_dynamodb_table.regime_log.arn,
    aws_dynamodb_table.strategy_recommendations.arn,
    aws_dynamodb_table.signal_inbox.arn,
    aws_dynamodb_table.signal_outbox.arn,
  ]
}
