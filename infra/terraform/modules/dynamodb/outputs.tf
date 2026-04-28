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

output "all_table_arns" {
  description = "List of all DynamoDB table ARNs"
  value = [
    aws_dynamodb_table.orders.arn,
    aws_dynamodb_table.positions.arn,
    aws_dynamodb_table.latest_prices.arn,
    aws_dynamodb_table.risk_state.arn,
    aws_dynamodb_table.strategy_state.arn,
  ]
}
