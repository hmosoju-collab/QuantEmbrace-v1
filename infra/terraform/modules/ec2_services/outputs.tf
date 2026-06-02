# ============================================================
# ec2_services/outputs.tf
# Phase 1 — EC2 Backbone Migration
# ============================================================

# ── Security Group ───────────────────────────────────────────────────────────

output "security_group_id" {
  description = "ID of the security group shared by all EC2 trading service instances"
  value       = aws_security_group.ec2_trading.id
}

# ── Placement Groups ─────────────────────────────────────────────────────────

output "execution_engine_placement_group_id" {
  description = "ID of the cluster placement group for execution-engine"
  value       = aws_placement_group.execution_engine.id
}

output "data_ingestion_placement_group_id" {
  description = "ID of the spread placement group for data-ingestion services"
  value       = aws_placement_group.data_ingestion.id
}

# ── ASG Names ────────────────────────────────────────────────────────────────

output "data_ingestion_nse_asg_name" {
  description = "Auto Scaling Group name for data-ingestion-nse"
  value       = aws_autoscaling_group.data_ingestion_nse.name
}

output "data_ingestion_us_asg_name" {
  description = "Auto Scaling Group name for data-ingestion-us"
  value       = aws_autoscaling_group.data_ingestion_us.name
}

output "strategy_engine_asg_name" {
  description = "Auto Scaling Group name for strategy-engine"
  value       = aws_autoscaling_group.strategy_engine.name
}

output "risk_engine_asg_name" {
  description = "Auto Scaling Group name for risk-engine"
  value       = aws_autoscaling_group.risk_engine.name
}

output "execution_engine_asg_name" {
  description = "Auto Scaling Group name for execution-engine"
  value       = aws_autoscaling_group.execution_engine.name
}

output "ai_engine_asg_name" {
  description = "Auto Scaling Group name for ai-engine (Phase 6)"
  value       = aws_autoscaling_group.ai_engine.name
}

# ── Launch Template IDs ──────────────────────────────────────────────────────

output "data_ingestion_nse_launch_template_id" {
  description = "Launch template ID for data-ingestion-nse"
  value       = aws_launch_template.data_ingestion_nse.id
}

output "data_ingestion_us_launch_template_id" {
  description = "Launch template ID for data-ingestion-us"
  value       = aws_launch_template.data_ingestion_us.id
}

output "strategy_engine_launch_template_id" {
  description = "Launch template ID for strategy-engine"
  value       = aws_launch_template.strategy_engine.id
}

output "risk_engine_launch_template_id" {
  description = "Launch template ID for risk-engine"
  value       = aws_launch_template.risk_engine.id
}

output "execution_engine_launch_template_id" {
  description = "Launch template ID for execution-engine"
  value       = aws_launch_template.execution_engine.id
}

output "ai_engine_launch_template_id" {
  description = "Launch template ID for ai-engine (Phase 6)"
  value       = aws_launch_template.ai_engine.id
}

# ── IAM Instance Profile ARNs ────────────────────────────────────────────────

output "data_ingestion_instance_profile_arn" {
  description = "IAM instance profile ARN for data-ingestion instances"
  value       = aws_iam_instance_profile.data_ingestion.arn
}

output "strategy_engine_instance_profile_arn" {
  description = "IAM instance profile ARN for strategy-engine instances"
  value       = aws_iam_instance_profile.strategy_engine.arn
}

output "execution_engine_instance_profile_arn" {
  description = "IAM instance profile ARN for execution-engine instances"
  value       = aws_iam_instance_profile.execution_engine.arn
}

# ── IAM Role ARNs ────────────────────────────────────────────────────────────

output "data_ingestion_role_arn" {
  description = "IAM role ARN for data-ingestion instances"
  value       = aws_iam_role.data_ingestion.arn
}

output "strategy_engine_role_arn" {
  description = "IAM role ARN for strategy-engine instances"
  value       = aws_iam_role.strategy_engine.arn
}

output "execution_engine_role_arn" {
  description = "IAM role ARN for execution-engine instances"
  value       = aws_iam_role.execution_engine.arn
}

output "risk_engine_role_arn" {
  description = "IAM role ARN for risk-engine instances"
  value       = aws_iam_role.risk_engine.arn
}

output "risk_engine_instance_profile_arn" {
  description = "IAM instance profile ARN for risk-engine instances"
  value       = aws_iam_instance_profile.risk_engine.arn
}

output "ai_engine_instance_profile_arn" {
  description = "IAM instance profile ARN for ai-engine instances (Phase 6)"
  value       = aws_iam_instance_profile.ai_engine.arn
}

output "ai_engine_role_arn" {
  description = "IAM role ARN for ai-engine instances (Phase 6)"
  value       = aws_iam_role.ai_engine.arn
}

output "ai_engine_role_name" {
  description = "IAM role name for ai-engine instances (input to kafka module for policy attachment)"
  value       = aws_iam_role.ai_engine.name
}

# ── IAM Role Names (used by Kafka module for policy attachment) ───────────────

output "data_ingestion_role_name" {
  description = "IAM role name for data-ingestion instances (input to kafka module)"
  value       = aws_iam_role.data_ingestion.name
}

output "strategy_engine_role_name" {
  description = "IAM role name for strategy-engine instances (input to kafka module)"
  value       = aws_iam_role.strategy_engine.name
}

output "risk_engine_role_name" {
  description = "IAM role name for risk-engine instances (input to kafka module)"
  value       = aws_iam_role.risk_engine.name
}

output "execution_engine_role_name" {
  description = "IAM role name for execution-engine instances (input to kafka module)"
  value       = aws_iam_role.execution_engine.name
}

# ── AMI Used ─────────────────────────────────────────────────────────────────

output "ami_id_used" {
  description = "AMI ID resolved and used for all EC2 instances in this module"
  value       = local.ami_id
}
