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
  description = <<-EOT
    Seconds of WebSocket silence before firing connectivity alarm AND
    auto-activating the kill switch via DynamoDB write (EventBridge rule).

    Default: 10s. Rationale: 10 seconds of tick blindness with open positions
    is the maximum acceptable exposure. At 10s, NSE can move 1-2% on news;
    beyond that, holding positions without a live data feed is unacceptable.

    Do NOT increase above 15s in production. Was previously 30–60s (too slow).
  EOT
  type        = number
  default     = 10
}

variable "data_staleness_seconds" {
  description = "Seconds of data-feed staleness (no ticks per instrument) before alarm"
  type        = number
  default     = 15
}

variable "kill_switch_dynamodb_table" {
  description = "DynamoDB table name where the kill switch state is stored (risk-state table)"
  type        = string
  default     = ""
}

variable "kill_switch_lambda_role_arn" {
  description = <<-EOT
    IAM role ARN for the auto-kill-switch Lambda.
    Must have dynamodb:PutItem on the risk-state table and logs:CreateLogGroup/PutLogEvents.
    Leave empty to skip Lambda creation (alarm will still fire SNS; Lambda just makes
    the DynamoDB write automatic without a human in the loop).
  EOT
  type        = string
  default     = ""
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

# ── Risk portfolio thresholds ────────────────────────────────────────────────

variable "max_position_concentration_pct" {
  description = <<-EOT
    Alarm fires when a single-symbol position exceeds this percentage of total
    portfolio value (e.g. 20 = 20%). Emitted as the PositionConcentrationPct
    custom metric by the risk engine on every portfolio update.
    A reading above this value indicates dangerously high single-name exposure.
  EOT
  type        = number
  default     = 20
}

variable "total_exposure_warning_pct" {
  description = <<-EOT
    Alarm fires when total gross exposure (long + short notional) exceeds this
    percentage of the risk engine's configured exposure cap (e.g. 80 = 80% of cap).
    Emitted as TotalExposurePct by the risk engine. Provides early warning before
    the hard limit is reached and signals are rejected.
  EOT
  type        = number
  default     = 80
}

variable "kill_switch_activation_alarm_period" {
  description = "Evaluation window (seconds) for the kill-switch activation alarm"
  type        = number
  default     = 60
}

# ── Zerodha rate-limit thresholds (ADR-012) ───────────────────────────────────

variable "zerodha_token_depletion_threshold" {
  description = <<-EOT
    Token bucket level below which the sustained-depletion alarm fires (P1).
    Default: 1.0 token. At < 1 token the bucket is effectively empty and all
    callers queue behind the 10 req/sec refill cadence, adding latency to every
    order placement attempt.  Values below 0.5 risk priority-inversion where
    CRITICAL-tier cancels are delayed by backed-up LOW/MEDIUM waiters.
  EOT
  type        = number
  default     = 1.0
}

variable "zerodha_fill_latency_p95_threshold_ms" {
  description = <<-EOT
    P95 fill-detection latency threshold (ms) for the BulkOrderPoller alarm (P1).
    Default: 1000ms.  BulkOrderPoller polls at 300–2000ms depending on order count
    and market phase.  P95 > 1000ms indicates the poller is running behind schedule
    — likely due to rate-limit contention or DynamoDB latency spikes.
  EOT
  type        = number
  default     = 1000
}

variable "zerodha_rate_limit_error_evaluation_periods" {
  description = "Evaluation periods (×60s) for the Zerodha 429-error alarm"
  type        = number
  default     = 1
}
