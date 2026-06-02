# QuantEmbrace Production - Variables

variable "aws_region" {
  description = "Primary AWS region for deployment"
  type        = string
  default     = "ap-south-1"
}

variable "alert_email" {
  description = "Email address for monitoring alerts"
  type        = string
}

variable "ecr_account_id" {
  description = "AWS account ID hosting the ECR repositories (used by EC2 instances to pull images)"
  type        = string
}

variable "secrets_zerodha_arn" {
  description = "ARN of the Secrets Manager secret for Zerodha API credentials"
  type        = string
}

variable "secrets_alpaca_arn" {
  description = "ARN of the Secrets Manager secret for Alpaca API credentials"
  type        = string
}

variable "kill_switch_lambda_role_arn" {
  description = <<-EOT
    IAM role ARN for the auto-kill-switch Lambda.
    When a WebSocket-disconnect or data-staleness alarm fires, this Lambda
    writes kill_switch=ACTIVE to DynamoDB automatically (no human required).
    Leave empty to skip automatic kill-switch (alarm will still fire SNS).
  EOT
  type        = string
  default     = ""
}

variable "strategy_watchlist_nse" {
  description = <<-EOT
    Comma-separated NSE symbols for LiveQuotePoller and candle strategies.
    PAPER_SAFE_START: NIFTY 50 (~50 symbols).
    Example: "HDFCBANK,ICICIBANK,RELIANCE,TCS,INFY,WIPRO,AXISBANK,SBIN"
    Leave empty to skip spread gate (acceptable for first paper sessions).
  EOT
  type        = string
  default     = ""
}

variable "ops_admin_role_name" {
  description = <<-EOT
    IAM role name for ops/admin use (Kafka topic setup, monitoring tools).
    Attach to the bastion host role or CI/CD runner role.
    Leave empty to skip creating the ops-admin Kafka policy attachment.
    Example: "QuantEmbrace-prod-BastionRole"
  EOT
  type        = string
  default     = ""
}
