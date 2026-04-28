###############################################################################
# QuantEmbrace — ECS Module Outputs
###############################################################################

output "cluster_id" {
  description = "ECS cluster ID"
  value       = aws_ecs_cluster.main.id
}

output "cluster_name" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.main.name
}

output "service_arns" {
  description = "Map of service name to ECS service ARN"
  value       = { for k, v in aws_ecs_service.services : k => v.id }
}

output "task_definition_arns" {
  description = "Map of service name to task definition ARN"
  value       = { for k, v in aws_ecs_task_definition.services : k => v.arn }
}

output "ecr_repository_urls" {
  description = "Map of service name to ECR repository URL"
  value       = { for k, v in aws_ecr_repository.services : k => v.repository_url }
}

output "service_discovery_namespace_id" {
  description = "Cloud Map namespace ID"
  value       = aws_service_discovery_private_dns_namespace.main.id
}

output "task_execution_role_arn" {
  description = "ARN of the ECS task execution IAM role"
  value       = aws_iam_role.ecs_execution.arn
}

output "task_role_arn" {
  description = "ARN of the ECS task IAM role"
  value       = aws_iam_role.ecs_task.arn
}

output "log_group_names" {
  description = "Map of service name to CloudWatch log group name"
  value       = { for k, v in aws_cloudwatch_log_group.services : k => v.name }
}
