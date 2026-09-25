output "asg_name" {
  value       = aws_autoscaling_group.workers.name
  description = "Backtest worker ASG name"
}

output "launch_template_id" {
  value       = aws_launch_template.worker.id
  description = "Backtest worker launch template ID"
}

output "worker_role_arn" {
  value       = aws_iam_role.worker.arn
  description = "Backtest worker IAM role ARN"
}

output "worker_instance_profile_name" {
  value       = aws_iam_instance_profile.worker.name
  description = "Backtest worker IAM instance profile name"
}
