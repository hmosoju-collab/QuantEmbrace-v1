###############################################################################
# QuantEmbrace — S3 Module Outputs
###############################################################################

output "tick_data_bucket_name" {
  description = "Name of the tick-data S3 bucket"
  value       = aws_s3_bucket.tick_data.id
}

output "tick_data_bucket_arn" {
  description = "ARN of the tick-data S3 bucket"
  value       = aws_s3_bucket.tick_data.arn
}

output "ohlcv_data_bucket_name" {
  description = "Name of the OHLCV data S3 bucket"
  value       = aws_s3_bucket.ohlcv_data.id
}

output "ohlcv_data_bucket_arn" {
  description = "ARN of the OHLCV data S3 bucket"
  value       = aws_s3_bucket.ohlcv_data.arn
}

output "trading_logs_bucket_name" {
  description = "Name of the trading logs S3 bucket"
  value       = aws_s3_bucket.trading_logs.id
}

output "trading_logs_bucket_arn" {
  description = "ARN of the trading logs S3 bucket"
  value       = aws_s3_bucket.trading_logs.arn
}

output "backtest_results_bucket_name" {
  description = "Name of the backtest results S3 bucket"
  value       = aws_s3_bucket.backtest_results.id
}

output "backtest_results_bucket_arn" {
  description = "ARN of the backtest results S3 bucket"
  value       = aws_s3_bucket.backtest_results.arn
}

output "model_artifacts_bucket_name" {
  description = "Name of the model artifacts S3 bucket"
  value       = aws_s3_bucket.model_artifacts.id
}

output "model_artifacts_bucket_arn" {
  description = "ARN of the model artifacts S3 bucket"
  value       = aws_s3_bucket.model_artifacts.arn
}
