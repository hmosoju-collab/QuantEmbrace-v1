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
