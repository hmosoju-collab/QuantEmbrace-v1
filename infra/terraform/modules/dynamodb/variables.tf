###############################################################################
# QuantEmbrace — DynamoDB Module Variables
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

variable "use_provisioned_capacity" {
  description = "Use provisioned capacity with auto-scaling (true for prod, false for dev)"
  type        = bool
  default     = false
}

variable "autoscaling_max_capacity" {
  description = "Maximum auto-scaling capacity for DynamoDB tables (only used with provisioned capacity)"
  type        = number
  default     = 100
}

variable "tags" {
  description = "Common tags applied to all resources"
  type        = map(string)
  default     = {}
}
