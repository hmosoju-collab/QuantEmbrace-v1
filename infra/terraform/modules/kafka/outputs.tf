###############################################################################
# QuantEmbrace — Kafka Module Outputs
###############################################################################

output "cluster_arn" {
  description = "ARN of the MSK Serverless cluster"
  value       = aws_msk_serverless_cluster.main.arn
}

output "cluster_name" {
  description = "Name of the MSK Serverless cluster"
  value       = aws_msk_serverless_cluster.main.cluster_name
}

output "bootstrap_brokers_sasl_iam" {
  description = <<-EOT
    Bootstrap broker string for IAM/SASL auth (port 9098).
    Use this as KAFKA_BOOTSTRAP_SERVERS in service environment variables.
    Format: b-X.{cluster}.{uuid}.kafka.{region}.amazonaws.com:9098,...
  EOT
  value       = aws_msk_serverless_cluster.main.bootstrap_brokers_sasl_iam
  sensitive   = false
}

output "security_group_id" {
  description = "Security group ID attached to the MSK Serverless cluster"
  value       = aws_security_group.msk.id
}

# ── IAM Policy ARNs ───────────────────────────────────────────────────────────

output "kafka_policy_arn_data_ingestion" {
  description = "IAM policy ARN for data-ingestion Kafka permissions"
  value       = aws_iam_policy.kafka_data_ingestion.arn
}

output "kafka_policy_arn_strategy_engine" {
  description = "IAM policy ARN for strategy-engine Kafka permissions"
  value       = aws_iam_policy.kafka_strategy_engine.arn
}

output "kafka_policy_arn_risk_engine" {
  description = "IAM policy ARN for risk-engine Kafka permissions"
  value       = aws_iam_policy.kafka_risk_engine.arn
}

output "kafka_policy_arn_execution_engine" {
  description = "IAM policy ARN for execution-engine Kafka permissions"
  value       = aws_iam_policy.kafka_execution_engine.arn
}

output "kafka_policy_arn_ops_admin" {
  description = "IAM policy ARN for ops-admin Kafka permissions (topic setup, monitoring)"
  value       = aws_iam_policy.kafka_ops_admin.arn
}

output "kafka_policy_arn_ai_engine" {
  description = "IAM policy ARN for ai-engine Kafka permissions (consume signals.pending, produce signals.enriched)"
  value       = var.ai_engine_role_name != "" ? aws_iam_policy.kafka_ai_engine.arn : null
}

# ── CloudWatch Alarm ARNs ─────────────────────────────────────────────────────

output "alarm_arn_msk_no_connections" {
  description = "CloudWatch alarm ARN — MSK no client connections (P1)"
  value       = aws_cloudwatch_metric_alarm.msk_client_connections_low.arn
}

output "alarm_arn_msk_no_bytes_in" {
  description = "CloudWatch alarm ARN — MSK no bytes in during market hours (P2)"
  value       = aws_cloudwatch_metric_alarm.msk_bytes_in_low.arn
}

# ── Topic Config Summary (for reference in CI / documentation) ───────────────

output "topic_config" {
  description = <<-EOT
    Topic configuration summary.
    Topics are NOT created by Terraform — use scripts/kafka/create_topics.py.
    This output documents the intended configuration for cross-reference.
  EOT
  value = {
    "ticks.nse"              = { partitions = 4, retention_ms = 86400000, key = "instrument_id" }
    "ticks.us"               = { partitions = 2, retention_ms = 86400000, key = "instrument_id" }
    "signals.pending"        = { partitions = 2, retention_ms = 3600000, key = "instrument_id" }
    "signals.enriched"       = { partitions = 2, retention_ms = 3600000, key = "symbol", note = "Phase 6 — ai_engine publishes; risk_engine consumes" }
    "signals.approved"       = { partitions = 2, retention_ms = 1800000, key = "instrument_id" }
    "orders.events"          = { partitions = 4, retention_ms = 604800000, key = "instrument_id" }
    "risk.kill-switch"       = { partitions = 1, retention_ms = 2592000000, key = "GLOBAL" }
    "ops.audit"              = { partitions = 2, retention_ms = 7776000000, key = "trace_id" }
    "ticks.nse.retry"        = { partitions = 4, retention_ms = 86400000, key = "instrument_id" }
    "ticks.nse.dlq"          = { partitions = 4, retention_ms = 86400000, key = "instrument_id" }
    "ticks.us.retry"         = { partitions = 2, retention_ms = 86400000, key = "instrument_id" }
    "ticks.us.dlq"           = { partitions = 2, retention_ms = 86400000, key = "instrument_id" }
    "signals.pending.retry"  = { partitions = 2, retention_ms = 3600000, key = "instrument_id" }
    "signals.pending.dlq"    = { partitions = 2, retention_ms = 3600000, key = "instrument_id" }
    "signals.approved.retry" = { partitions = 2, retention_ms = 1800000, key = "instrument_id" }
    "signals.approved.dlq"   = { partitions = 2, retention_ms = 1800000, key = "instrument_id" }
    "orders.events.retry"    = { partitions = 4, retention_ms = 604800000, key = "instrument_id" }
    "orders.events.dlq"      = { partitions = 4, retention_ms = 604800000, key = "instrument_id" }
    "risk.kill-switch.retry" = { partitions = 1, retention_ms = 2592000000, key = "GLOBAL" }
    "risk.kill-switch.dlq"   = { partitions = 1, retention_ms = 2592000000, key = "GLOBAL" }
    "ops.audit.retry"        = { partitions = 2, retention_ms = 7776000000, key = "trace_id" }
    "ops.audit.dlq"          = { partitions = 2, retention_ms = 7776000000, key = "trace_id" }
  }
}
