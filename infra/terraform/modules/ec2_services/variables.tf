# ============================================================
# ec2_services/variables.tf
# Phase 1 — EC2 Backbone Migration
# ============================================================

variable "environment" {
  description = "Deployment environment (dev | staging | prod)"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

variable "aws_region" {
  description = "AWS region to deploy into (e.g. ap-south-1)"
  type        = string
  default     = "ap-south-1"
}

variable "vpc_id" {
  description = "ID of the VPC to deploy instances into"
  type        = string
}

variable "private_subnet_ids" {
  description = "List of private subnet IDs for ASG (at least 2 AZs recommended)"
  type        = list(string)
}

variable "primary_subnet_id" {
  description = "Primary private subnet ID (AZ-a) for singleton services and placement group"
  type        = string
}

variable "ecr_account_id" {
  description = "AWS account ID hosting the ECR repositories"
  type        = string
}

variable "log_level" {
  description = "Application log level (DEBUG | INFO | WARNING | ERROR)"
  type        = string
  default     = "INFO"
}

variable "dynamodb_table_prefix" {
  description = "Prefix for all DynamoDB table names (e.g. 'quantembrace-prod')"
  type        = string
}

variable "s3_bucket_tick_data" {
  description = "S3 bucket name for tick data (data-ingestion writes here)"
  type        = string
}

variable "s3_bucket_ohlcv_data" {
  description = "S3 bucket name for OHLCV data"
  type        = string
}

variable "s3_bucket_trading_logs" {
  description = "S3 bucket name for audit and execution logs"
  type        = string
}

variable "s3_bucket_model_artifacts" {
  description = "S3 bucket name for ML models and strategy configs"
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

variable "sns_alerts_topic_arn" {
  description = "ARN of the SNS topic for critical trading alerts"
  type        = string
}

variable "sns_kill_switch_topic_arn" {
  description = "ARN of the SNS topic for kill switch events"
  type        = string
}

variable "cloudwatch_log_retention_days" {
  description = "Retention period for CloudWatch log groups in days"
  type        = number
  default     = 30
}

variable "ami_id" {
  description = <<-EOT
    AMI ID for AL2023 ARM64. Leave empty to auto-resolve latest AL2023 ARM64 AMI.
    Pin this to a specific AMI ID in production for immutable infrastructure.
    Example: ami-0xxxxxxxxxxxxxxxxx
  EOT
  type        = string
  default     = ""
}

variable "key_pair_name" {
  description = <<-EOT
    EC2 key pair name for emergency SSH access (optional).
    Leave empty to disable SSH key-based access (use SSM Session Manager instead).
    Recommended: leave empty in production.
  EOT
  type        = string
  default     = ""
}

variable "instance_metadata_http_tokens" {
  description = "IMDSv2 enforcement: 'required' (secure) or 'optional' (legacy)"
  type        = string
  default     = "required"

  validation {
    condition     = contains(["required", "optional"], var.instance_metadata_http_tokens)
    error_message = "instance_metadata_http_tokens must be 'required' or 'optional'."
  }
}

variable "strategy_watchlist_nse" {
  description = <<-EOT
    Comma-separated NSE symbols for LiveQuotePoller and candle strategy universe.
    Required for spread data and candle signal generation.
    PAPER_SAFE_START: NIFTY 50 (~50 symbols).
    PAPER_EXPAND / LIVE_ADVANCED: expand to NIFTY 100+ as needed.
    Example: "HDFCBANK,ICICIBANK,RELIANCE,TCS,INFY"
    Leave empty to skip LiveQuotePoller (signals will generate but without spread gate).
  EOT
  type        = string
  default     = ""
}

# ── Per-service instance type overrides ─────────────────────────────────────

variable "data_ingestion_instance_type" {
  description = "EC2 instance type for data-ingestion services"
  type        = string
  default     = "t4g.medium"
}

variable "strategy_engine_instance_type" {
  description = "EC2 instance type for strategy-engine service"
  type        = string
  default     = "c6g.large"
}

variable "execution_engine_instance_type" {
  description = "EC2 instance type for execution-engine service"
  type        = string
  default     = "c6g.large"
}

variable "risk_engine_instance_type" {
  description = "EC2 instance type for risk-engine service"
  type        = string
  default     = "c6g.large"
}

variable "ai_engine_instance_type" {
  description = <<-EOT
    EC2 instance type for ai-engine service.
    c6g.large chosen over t4g because joblib HMM inference is CPU-bound;
    sustained throughput at market tick rate requires dedicated vCPU.
  EOT
  type        = string
  default     = "c6g.large"
}

variable "secrets_anthropic_arn" {
  description = <<-EOT
    ARN of the Secrets Manager secret containing the Anthropic API key.
    Used by StrategySelector (post-market Claude Haiku advisory agent).
    Leave empty to disable Secrets Manager IAM grant (StrategySelector will
    fail to authenticate but all other ai_engine functions remain operational).
  EOT
  type        = string
  default     = ""
}

# ── ASG sizing ───────────────────────────────────────────────────────────────

variable "strategy_engine_max_capacity" {
  description = "Maximum ASG capacity for strategy-engine (set to 2 for horizontal scale)"
  type        = number
  default     = 2

  validation {
    condition     = var.strategy_engine_max_capacity >= 1 && var.strategy_engine_max_capacity <= 4
    error_message = "strategy_engine_max_capacity must be between 1 and 4."
  }
}

variable "enable_warm_pools" {
  description = "Enable ASG warm pools for faster failover (recommended for prod)"
  type        = bool
  default     = true
}

# ── Scheduled scaling (market hours) ────────────────────────────────────────

variable "enable_scheduled_scaling" {
  description = <<-EOT
    Stop/start EC2 instances outside market hours to reduce cost.
    When true, data-ingestion and strategy-engine ASGs scale to 0 outside
    market hours. The execution-engine ASG is ALWAYS kept at min_size=1
    regardless of this flag — it must remain reachable for emergency order
    cancellations and post-close position reconciliation.
  EOT
  type        = bool
  default     = true
}

variable "nse_market_open_cron" {
  description = "Cron expression (UTC) to start NSE data-ingestion (08:45 IST = 03:15 UTC)"
  type        = string
  default     = "cron(15 3 ? * MON-FRI *)"
}

variable "nse_market_close_cron" {
  description = "Cron expression (UTC) to stop NSE data-ingestion (16:15 IST = 10:45 UTC)"
  type        = string
  default     = "cron(45 10 ? * MON-FRI *)"
}

variable "us_market_open_cron" {
  description = "Cron expression (UTC) to start US data-ingestion (19:00 IST = 13:30 UTC)"
  type        = string
  default     = "cron(30 13 ? * MON-FRI *)"
}

variable "us_market_close_cron" {
  description = "Cron expression (UTC) to stop US data-ingestion (06:30 IST = 01:00 UTC)"
  type        = string
  default     = "cron(0 1 ? * TUE-SAT *)"
}

variable "strategy_engine_on_cron" {
  description = "Cron expression (UTC) to start strategy-engine (before NSE open)"
  type        = string
  default     = "cron(0 3 ? * MON-FRI *)"
}

variable "strategy_engine_off_cron" {
  description = "Cron expression (UTC) to stop strategy-engine (after US close)"
  type        = string
  default     = "cron(30 1 ? * TUE-SAT *)"
}
