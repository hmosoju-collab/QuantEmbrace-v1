###############################################################################
# QuantEmbrace — EC2 Backtest Worker Module
# ARM64 Spot ASG, min=desired=0 (scale-from-zero).  Safe for interruption
# because every shard checkpoints to qe-bt-checkpoints before writing output;
# a reclaim loses at most 1 partition of work.
# Shared ec2_services module is untouched — different IAM, different billing.
###############################################################################

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

locals {
  common_tags = merge(var.tags, { Module = "ec2_backtest_worker" })
  name_prefix = "quantembrace-backtest-worker"
}

# Latest Amazon Linux 2023 ARM64 AMI (ap-south-1)
data "aws_ami" "al2023_arm64" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-arm64"]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_launch_template" "worker" {
  name_prefix            = "${local.name_prefix}-"
  image_id               = data.aws_ami.al2023_arm64.id
  instance_type          = var.primary_instance_type
  vpc_security_group_ids = [var.security_group_id]

  iam_instance_profile {
    name = aws_iam_instance_profile.worker.name
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = var.root_volume_gb
      volume_type           = "gp3"
      delete_on_termination = true
      encrypted             = true
    }
  }

  user_data = base64encode(templatefile("${path.module}/userdata.sh.tpl", {
    runs_table         = "qe-bt-runs"
    checkpoints_table  = "qe-bt-checkpoints"
    data_bucket        = "quantembrace-backtest-data"
    results_bucket     = "quantembrace-backtest-results"
    aws_region         = var.aws_region
    sns_alert_arn      = var.sns_alert_arn
  }))

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.common_tags, {
      Name = local.name_prefix
      Role = "backtest-worker"
    })
  }

  tag_specifications {
    resource_type = "volume"
    tags = merge(local.common_tags, { Name = "${local.name_prefix}-root" })
  }

  tags = merge(local.common_tags, { Name = "${local.name_prefix}-lt" })
}

resource "aws_autoscaling_group" "workers" {
  name                = local.name_prefix
  min_size            = 0
  max_size            = var.max_workers
  desired_capacity    = 0
  vpc_zone_identifier = [var.subnet_id]

  capacity_rebalance = true

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0
      spot_allocation_strategy                 = "capacity-optimized"
    }

    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.worker.id
        version            = "$Latest"
      }

      # ARM64 instance type overrides (spot pools)
      override {
        instance_type = "c6g.large"
      }
      override {
        instance_type = "c6g.xlarge"
      }
      override {
        instance_type = "c7g.large"
      }
      override {
        instance_type = "c7g.xlarge"
      }
      override {
        instance_type = "m6g.large"
      }
      override {
        instance_type = "t4g.medium"
      }
    }
  }

  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 0 # allow full replacement for batch jobs
    }
  }

  tag {
    key                 = "Name"
    value               = local.name_prefix
    propagate_at_launch = true
  }

  dynamic "tag" {
    for_each = local.common_tags
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }

  lifecycle {
    ignore_changes = [desired_capacity] # externally managed by run scripts
  }
}
