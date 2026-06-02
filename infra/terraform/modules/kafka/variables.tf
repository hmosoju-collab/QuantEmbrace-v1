###############################################################################
# QuantEmbrace — Kafka Module Variables
#
# MSK Serverless cluster + IAM auth + per-service topic-level policies
# Phase 2 — Kafka Streaming Core
###############################################################################

variable "project" {
  description = "Project name used for resource naming and tagging"
  type        = string
  default     = "quantembrace"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
}

variable "aws_region" {
  description = "AWS region where MSK Serverless is provisioned"
  type        = string
  default     = "ap-south-1"
}

# ── VPC / Networking ──────────────────────────────────────────────────────────

variable "vpc_id" {
  description = "VPC ID where MSK Serverless ENIs will be placed"
  type        = string
}

variable "private_subnet_ids" {
  description = <<-EOT
    Private subnet IDs for MSK Serverless VPC configuration.
    Use subnets in at least 2 AZs (MSK Serverless requirement).
    These must be the same subnets used by EC2 service instances.
  EOT
  type        = list(string)
}

variable "ec2_services_security_group_id" {
  description = "Security group ID of EC2 service instances (allowed to connect to MSK)"
  type        = string
}

# ── MSK Serverless ────────────────────────────────────────────────────────────

variable "cluster_name" {
  description = "MSK Serverless cluster name. Defaults to quantembrace-{env}-kafka"
  type        = string
  default     = ""
}

# ── IAM Role ARNs (for attaching Kafka policies) ─────────────────────────────

variable "data_ingestion_role_name" {
  description = "IAM role name of the data-ingestion EC2 instance profile"
  type        = string
}

variable "strategy_engine_role_name" {
  description = "IAM role name of the strategy-engine EC2 instance profile"
  type        = string
}

variable "risk_engine_role_name" {
  description = "IAM role name of the risk-engine EC2 instance profile"
  type        = string
}

variable "execution_engine_role_name" {
  description = "IAM role name of the execution-engine EC2 instance profile"
  type        = string
}

variable "ai_engine_role_name" {
  description = "IAM role name of the ai-engine EC2 instance profile (Phase 6)"
  type        = string
  default     = ""
}

variable "ops_admin_role_name" {
  description = <<-EOT
    IAM role name for ops/admin use (topic setup script, monitoring).
    Attach to the bastion or CI/CD runner role.
  EOT
  type        = string
  default     = ""
}

# ── Topic Retention Overrides (optional) ─────────────────────────────────────
# Defaults match the approved Phase 2 design (§2 of phase2_final_approved.md).
# Override in staging/dev if you want shorter retention to save cost.

variable "retention_ms_ticks_nse" {
  description = "Retention for ticks.nse in milliseconds. Default 24h."
  type        = number
  default     = 86400000 # 24h
}

variable "retention_ms_ticks_us" {
  description = "Retention for ticks.us in milliseconds. Default 24h."
  type        = number
  default     = 86400000 # 24h
}

variable "retention_ms_signals_pending" {
  description = "Retention for signals.pending in milliseconds. Default 1h."
  type        = number
  default     = 3600000 # 1h
}

variable "retention_ms_signals_approved" {
  description = "Retention for signals.approved in milliseconds. Default 30m."
  type        = number
  default     = 1800000 # 30m
}

variable "retention_ms_orders_events" {
  description = "Retention for orders.events in milliseconds. Default 7d."
  type        = number
  default     = 604800000 # 7d
}

variable "retention_ms_kill_switch" {
  description = "Retention for risk.kill-switch in milliseconds. Default 30d."
  type        = number
  default     = 2592000000 # 30d
}

variable "retention_ms_ops_audit" {
  description = "Retention for ops.audit in milliseconds. Default 90d."
  type        = number
  default     = 7776000000 # 90d
}

# ── Tags ──────────────────────────────────────────────────────────────────────

variable "tags" {
  description = "Common tags applied to all resources"
  type        = map(string)
  default     = {}
}
