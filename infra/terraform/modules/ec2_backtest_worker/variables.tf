variable "aws_region" {
  description = "AWS region."
  type        = string
}

variable "subnet_id" {
  description = "Public subnet ID for the worker ASG."
  type        = string
}

variable "security_group_id" {
  description = "Security group ID for worker instances (egress-only)."
  type        = string
}

variable "sns_alert_arn" {
  description = "SNS topic ARN for backtest alert notifications."
  type        = string
}

variable "primary_instance_type" {
  description = "Primary ARM64 instance type for the launch template."
  type        = string
  default     = "c6g.large"
}

variable "max_workers" {
  description = "Maximum ASG size (fleet cap — set per run script)."
  type        = number
  default     = 10
}

variable "root_volume_gb" {
  description = "Root EBS volume size in GB."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags applied to all resources in this module."
  type        = map(string)
  default     = {}
}
