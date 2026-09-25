output "vpc_id" {
  value       = module.network.vpc_id
  description = "Backtest VPC ID"
}

output "public_subnet_id" {
  value       = module.network.public_subnet_id
  description = "Backtest public subnet ID"
}

output "worker_security_group_id" {
  value       = module.network.worker_security_group_id
  description = "Worker security group ID"
}

output "runs_table_name" {
  value       = module.dynamodb.runs_table_name
  description = "qe-bt-runs DynamoDB table name"
}

output "checkpoints_table_name" {
  value       = module.dynamodb.checkpoints_table_name
  description = "qe-bt-checkpoints DynamoDB table name"
}

output "datasets_table_name" {
  value       = module.dynamodb.datasets_table_name
  description = "qe-bt-datasets DynamoDB table name"
}

output "data_bucket_name" {
  value       = module.s3.data_bucket_name
  description = "quantembrace-backtest-data S3 bucket"
}

output "results_bucket_name" {
  value       = module.s3.results_bucket_name
  description = "quantembrace-backtest-results S3 bucket"
}

output "sns_alert_arn" {
  value       = module.monitoring.sns_alert_arn
  description = "Backtest alerts SNS topic ARN"
}

output "asg_name" {
  value       = module.ec2_worker.asg_name
  description = "Backtest worker ASG name"
}

output "worker_role_arn" {
  value       = module.ec2_worker.worker_role_arn
  description = "Backtest worker IAM role ARN"
}
