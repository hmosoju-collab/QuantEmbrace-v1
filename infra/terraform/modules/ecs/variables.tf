###############################################################################
# QuantEmbrace — ECS Module Variables
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

variable "vpc_id" {
  description = "VPC ID where ECS services will be deployed"
  type        = string
}

variable "private_subnet_ids" {
  description = "List of private subnet IDs for ECS tasks"
  type        = list(string)
}

variable "ecs_security_group_id" {
  description = "Security group ID for ECS tasks"
  type        = string
}

variable "service_configs" {
  description = "Configuration for each service (cpu, memory, desired_count, use_spot)"
  type = map(object({
    cpu           = number
    memory        = number
    desired_count = number
    use_spot      = bool
  }))
}

variable "enable_container_insights" {
  description = "Enable CloudWatch Container Insights on the ECS cluster"
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 30
}

variable "tags" {
  description = "Common tags applied to all resources"
  type        = map(string)
  default     = {}
}
