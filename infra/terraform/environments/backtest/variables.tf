variable "aws_region" {
  description = "AWS region for all backtest resources."
  type        = string
  default     = "ap-south-1"
}

variable "availability_zone" {
  description = "AZ for the backtest subnet (e.g. ap-south-1a)."
  type        = string
  default     = "ap-south-1a"
}

variable "alert_email" {
  description = "Email for SNS alert subscriptions. Leave empty to skip."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly USD cost budget for the backtesting lab."
  type        = number
  default     = 100
}

variable "primary_instance_type" {
  description = "Primary ARM64 instance type for worker launch template."
  type        = string
  default     = "c6g.large"
}

variable "max_workers" {
  description = "Maximum ASG capacity (run scripts manage desired_capacity at runtime)."
  type        = number
  default     = 10
}

variable "root_volume_gb" {
  description = "Worker root EBS volume size in GB."
  type        = number
  default     = 30
}
