# QuantEmbrace Dev - Variables

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
