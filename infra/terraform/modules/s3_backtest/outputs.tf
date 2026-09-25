output "data_bucket_name" {
  value       = aws_s3_bucket.data.bucket
  description = "quantembrace-backtest-data bucket name"
}

output "data_bucket_arn" {
  value       = aws_s3_bucket.data.arn
  description = "quantembrace-backtest-data bucket ARN"
}

output "results_bucket_name" {
  value       = aws_s3_bucket.results.bucket
  description = "quantembrace-backtest-results bucket name"
}

output "results_bucket_arn" {
  value       = aws_s3_bucket.results.arn
  description = "quantembrace-backtest-results bucket ARN"
}
