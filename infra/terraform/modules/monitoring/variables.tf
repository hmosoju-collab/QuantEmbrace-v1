###############################################################################
# QuantEmbrace — Monitoring Module Variables
###############################################################################

variable "project" {
  description = "Project name used for resource naming"
  type        = string
  default     = "quantembrace"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
}

variable "alert_email" {
  description = "Email address for alarm notifications (SNS subscription)"
  type        = string
  default     = ""
}

variable "service_names" {
  description = "ECS service names for per-service alarms"
  type        = list(string)
  default     = ["data_ingestion", "strategy_engine", "execution_engine", "risk_engine", "ai_engine"]
}

variable "ecs_cluster_name" {
  description = "ECS cluster name for task-count and utilisation alarms"
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days (hot tier before archival to S3)"
  type        = number
  default     = 30
}

# ── ECS health thresholds ──────────────────────────────────────────────────

variable "ecs_cpu_threshold_pct" {
  description = "ECS service CPU utilisation threshold (%)"
  type        = number
  default     = 90
}

variable "ecs_memory_threshold_pct" {
  description = "ECS service memory utilisation threshold (%)"
  type        = number
  default     = 85
}

variable "ecs_min_running_tasks" {
  description = "Minimum number of running ECS tasks before firing an alarm"
  type        = number
  default     = 1
}

# ── Trading-specific thresholds ────────────────────────────────────────────

variable "error_rate_threshold" {
  description = "Error count per 5-minute window to trigger alarm"
  type        = number
  default     = 10
}

variable "execution_latency_threshold_ms" {
  description = "P99 order placement latency threshold (ms)"
  type        = number
  default     = 500
}

variable "order_rejection_rate_threshold_pct" {
  description = "Order rejection rate (%) per 5-minute window to trigger alarm"
  type        = number
  default     = 20
}

variable "daily_pnl_loss_alert_threshold" {
  description = "Daily P&L loss value (absolute, e.g. 50000) for investigation alarm"
  type        = number
  default     = 50000
}

variable "daily_pnl_loss_halt_threshold" {
  description = "Daily P&L loss value triggering automatic trading halt alarm"
  type        = number
  default     = 100000
}

variable "websocket_gap_seconds" {
  description = "Seconds of WebSocket silence before firing connectivity alarm"
  type        = number
  default     = 30
}

variable "data_staleness_seconds" {
  description = "Seconds of data-feed silence during market hours before alarm"
  type        = number
  default     = 60
}

# ── Cost threshold ─────────────────────────────────────────────────────────

variable "daily_cost_threshold_usd" {
  description = "Daily estimated AWS charges threshold (USD)"
  type        = number
  default     = 50
}

variable "tags" {
  description = "Common tags applied to all resources"
  type        = map(string)
  default     = {}
}
