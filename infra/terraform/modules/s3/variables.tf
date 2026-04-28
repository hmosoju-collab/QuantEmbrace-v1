###############################################################################
# QuantEmbrace — S3 Module Variables
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

variable "tags" {
  description = "Common tags applied to all resources"
  type        = map(string)
  default     = {}
}
