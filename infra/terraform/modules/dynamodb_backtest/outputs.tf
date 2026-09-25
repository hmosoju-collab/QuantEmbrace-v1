output "runs_table_name" {
  value       = aws_dynamodb_table.runs.name
  description = "qe-bt-runs DynamoDB table name"
}

output "runs_table_arn" {
  value       = aws_dynamodb_table.runs.arn
  description = "qe-bt-runs DynamoDB table ARN"
}

output "checkpoints_table_name" {
  value       = aws_dynamodb_table.checkpoints.name
  description = "qe-bt-checkpoints DynamoDB table name"
}

output "checkpoints_table_arn" {
  value       = aws_dynamodb_table.checkpoints.arn
  description = "qe-bt-checkpoints DynamoDB table ARN"
}

output "datasets_table_name" {
  value       = aws_dynamodb_table.datasets.name
  description = "qe-bt-datasets DynamoDB table name"
}

output "datasets_table_arn" {
  value       = aws_dynamodb_table.datasets.arn
  description = "qe-bt-datasets DynamoDB table ARN"
}
