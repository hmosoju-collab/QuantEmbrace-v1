# ============================================================
# ec2_services/main.tf
# EC2 Launch Templates, Auto Scaling Groups, Placement Groups
# Phase 1 — EC2 Backbone Migration
#
# Services deployed here:
#   - data-ingestion-nse   (t4g.medium, ASG 1/1)
#   - data-ingestion-us    (t4g.medium, ASG 1/1)
#   - strategy-engine      (c6g.large,  ASG 1/2)
#   - risk-engine          (c6g.large,  ASG 1/1)
#   - execution-engine     (c6g.large,  ASG 1/1, cluster placement)
# ============================================================

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

# ── AMI: AL2023 ARM64 (auto-resolve if not pinned) ──────────────────────────

data "aws_ami" "al2023_arm64" {
  count       = var.ami_id == "" ? 1 : 0
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
  filter {
    name   = "state"
    values = ["available"]
  }
}

locals {
  ami_id = var.ami_id != "" ? var.ami_id : data.aws_ami.al2023_arm64[0].id

  common_tags = {
    Environment = var.environment
    Project     = "QuantEmbrace"
    ManagedBy   = "terraform"
    Phase       = "1-ec2-backbone"
  }

  # ECR image URI prefix
  ecr_base = "${var.ecr_account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
}

# ── Security Group: EC2 trading services ────────────────────────────────────

resource "aws_security_group" "ec2_trading" {
  name        = "quantembrace-${var.environment}-ec2-trading"
  description = "Security group for EC2 trading service instances (Phase 1)"
  vpc_id      = var.vpc_id

  # Health check from within the VPC
  ingress {
    description = "Health check from VPC"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }

  # Self-referencing: allow intra-service communication
  ingress {
    description = "Intra-service traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    self        = true
  }

  # Outbound: HTTPS to broker APIs and AWS services
  egress {
    description = "HTTPS outbound (broker APIs, AWS services)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, { Name = "quantembrace-${var.environment}-ec2-trading" })
}

# ── Placement Group: Execution Engine (cluster) ──────────────────────────────

resource "aws_placement_group" "execution_engine" {
  name     = "quantembrace-${var.environment}-execution-cluster"
  strategy = "cluster"

  tags = merge(local.common_tags, {
    Name    = "quantembrace-${var.environment}-execution-cluster"
    Service = "execution-engine"
    Note    = "Cluster placement for lowest-latency DynamoDB and MSK Kafka access"
  })
}

resource "aws_placement_group" "data_ingestion" {
  name     = "quantembrace-${var.environment}-data-ingestion-spread"
  strategy = "spread"

  tags = merge(local.common_tags, {
    Name    = "quantembrace-${var.environment}-data-ingestion-spread"
    Service = "data-ingestion"
    Note    = "Spread placement for fault isolation between NSE and US ingest"
  })
}

# ═══════════════════════════════════════════════════════════════
# DATA INGESTION — NSE
# ═══════════════════════════════════════════════════════════════

resource "aws_launch_template" "data_ingestion_nse" {
  name        = "quantembrace-${var.environment}-data-ingestion-nse"
  description = "Launch template for NSE data-ingestion EC2 service"

  image_id               = local.ami_id
  instance_type          = var.data_ingestion_instance_type
  key_name               = var.key_pair_name != "" ? var.key_pair_name : null
  update_default_version = true

  iam_instance_profile {
    arn = aws_iam_instance_profile.data_ingestion.arn
  }

  network_interfaces {
    associate_public_ip_address = false
    security_groups             = [aws_security_group.ec2_trading.id]
    delete_on_termination       = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = var.instance_metadata_http_tokens
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  monitoring {
    enabled = true # Detailed monitoring (1-min granularity)
  }

  user_data = base64encode(templatefile(
    "${path.module}/userdata/data_ingestion.sh",
    {
      environment           = var.environment
      market                = "NSE"
      service_name          = "data-ingestion-nse"
      ecr_base              = local.ecr_base
      aws_region            = var.aws_region
      log_level             = var.log_level
      dynamodb_table_prefix = var.dynamodb_table_prefix
      s3_bucket_tick_data   = var.s3_bucket_tick_data
      s3_bucket_ohlcv_data  = var.s3_bucket_ohlcv_data
      secrets_zerodha_arn   = var.secrets_zerodha_arn
      secrets_alpaca_arn    = var.secrets_alpaca_arn
    }
  ))

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.common_tags, {
      Name    = "quantembrace-${var.environment}-data-ingestion-nse"
      Service = "data-ingestion-nse"
    })
  }

  tag_specifications {
    resource_type = "volume"
    tags = merge(local.common_tags, {
      Name    = "quantembrace-${var.environment}-data-ingestion-nse-root"
      Service = "data-ingestion-nse"
    })
  }

  tags = merge(local.common_tags, { Name = "quantembrace-${var.environment}-data-ingestion-nse-lt" })
}

resource "aws_autoscaling_group" "data_ingestion_nse" {
  name                      = "quantembrace-${var.environment}-data-ingestion-nse-asg"
  min_size                  = 1
  max_size                  = 1
  desired_capacity          = 1
  health_check_type         = "EC2"
  health_check_grace_period = 120
  vpc_zone_identifier       = [var.primary_subnet_id] # Singleton in AZ-a

  launch_template {
    id      = aws_launch_template.data_ingestion_nse.id
    version = "$Latest"
  }

  # Placement group for fault isolation from US ingest
  placement_group = aws_placement_group.data_ingestion.id

  dynamic "warm_pool" {
    for_each = var.enable_warm_pools ? [1] : []
    content {
      pool_state                  = "Stopped"
      min_size                    = 1
      max_group_prepared_capacity = 1
      instance_reuse_policy {
        reuse_on_scale_in = true
      }
    }
  }

  lifecycle {
    create_before_destroy = true
    ignore_changes        = [desired_capacity] # Allow scheduled scaling to manage this
  }

  tag {
    key                 = "Name"
    value               = "quantembrace-${var.environment}-data-ingestion-nse"
    propagate_at_launch = true
  }
  tag {
    key                 = "Service"
    value               = "data-ingestion-nse"
    propagate_at_launch = true
  }
  tag {
    key                 = "Environment"
    value               = var.environment
    propagate_at_launch = true
  }
}

# Scheduled scaling: on at NSE market open, off at close
resource "aws_autoscaling_schedule" "nse_market_open" {
  count                  = var.enable_scheduled_scaling ? 1 : 0
  scheduled_action_name  = "nse-market-open"
  autoscaling_group_name = aws_autoscaling_group.data_ingestion_nse.name
  recurrence             = var.nse_market_open_cron
  min_size               = 1
  max_size               = 1
  desired_capacity       = 1
}

resource "aws_autoscaling_schedule" "nse_market_close" {
  count                  = var.enable_scheduled_scaling ? 1 : 0
  scheduled_action_name  = "nse-market-close"
  autoscaling_group_name = aws_autoscaling_group.data_ingestion_nse.name
  recurrence             = var.nse_market_close_cron
  min_size               = 0
  max_size               = 1
  desired_capacity       = 0
}

# ═══════════════════════════════════════════════════════════════
# DATA INGESTION — US
# ═══════════════════════════════════════════════════════════════

resource "aws_launch_template" "data_ingestion_us" {
  name        = "quantembrace-${var.environment}-data-ingestion-us"
  description = "Launch template for US data-ingestion EC2 service"

  image_id               = local.ami_id
  instance_type          = var.data_ingestion_instance_type
  key_name               = var.key_pair_name != "" ? var.key_pair_name : null
  update_default_version = true

  iam_instance_profile {
    arn = aws_iam_instance_profile.data_ingestion.arn
  }

  network_interfaces {
    associate_public_ip_address = false
    security_groups             = [aws_security_group.ec2_trading.id]
    delete_on_termination       = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = var.instance_metadata_http_tokens
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  monitoring {
    enabled = true
  }

  user_data = base64encode(templatefile(
    "${path.module}/userdata/data_ingestion.sh",
    {
      environment           = var.environment
      market                = "US"
      service_name          = "data-ingestion-us"
      ecr_base              = local.ecr_base
      aws_region            = var.aws_region
      log_level             = var.log_level
      dynamodb_table_prefix = var.dynamodb_table_prefix
      s3_bucket_tick_data   = var.s3_bucket_tick_data
      s3_bucket_ohlcv_data  = var.s3_bucket_ohlcv_data
      secrets_zerodha_arn   = var.secrets_zerodha_arn
      secrets_alpaca_arn    = var.secrets_alpaca_arn
    }
  ))

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.common_tags, {
      Name    = "quantembrace-${var.environment}-data-ingestion-us"
      Service = "data-ingestion-us"
    })
  }

  tag_specifications {
    resource_type = "volume"
    tags = merge(local.common_tags, {
      Name    = "quantembrace-${var.environment}-data-ingestion-us-root"
      Service = "data-ingestion-us"
    })
  }

  tags = merge(local.common_tags, { Name = "quantembrace-${var.environment}-data-ingestion-us-lt" })
}

resource "aws_autoscaling_group" "data_ingestion_us" {
  name                      = "quantembrace-${var.environment}-data-ingestion-us-asg"
  min_size                  = 1
  max_size                  = 1
  desired_capacity          = 1
  health_check_type         = "EC2"
  health_check_grace_period = 120
  vpc_zone_identifier       = [var.primary_subnet_id]

  launch_template {
    id      = aws_launch_template.data_ingestion_us.id
    version = "$Latest"
  }

  placement_group = aws_placement_group.data_ingestion.id

  dynamic "warm_pool" {
    for_each = var.enable_warm_pools ? [1] : []
    content {
      pool_state                  = "Stopped"
      min_size                    = 1
      max_group_prepared_capacity = 1
      instance_reuse_policy {
        reuse_on_scale_in = true
      }
    }
  }

  lifecycle {
    create_before_destroy = true
    ignore_changes        = [desired_capacity]
  }

  tag {
    key                 = "Name"
    value               = "quantembrace-${var.environment}-data-ingestion-us"
    propagate_at_launch = true
  }
  tag {
    key                 = "Service"
    value               = "data-ingestion-us"
    propagate_at_launch = true
  }
  tag {
    key                 = "Environment"
    value               = var.environment
    propagate_at_launch = true
  }
}

resource "aws_autoscaling_schedule" "us_market_open" {
  count                  = var.enable_scheduled_scaling ? 1 : 0
  scheduled_action_name  = "us-market-open"
  autoscaling_group_name = aws_autoscaling_group.data_ingestion_us.name
  recurrence             = var.us_market_open_cron
  min_size               = 1
  max_size               = 1
  desired_capacity       = 1
}

resource "aws_autoscaling_schedule" "us_market_close" {
  count                  = var.enable_scheduled_scaling ? 1 : 0
  scheduled_action_name  = "us-market-close"
  autoscaling_group_name = aws_autoscaling_group.data_ingestion_us.name
  recurrence             = var.us_market_close_cron
  min_size               = 0
  max_size               = 1
  desired_capacity       = 0
}

# ═══════════════════════════════════════════════════════════════
# STRATEGY ENGINE
# ═══════════════════════════════════════════════════════════════

resource "aws_launch_template" "strategy_engine" {
  name        = "quantembrace-${var.environment}-strategy-engine"
  description = "Launch template for strategy-engine EC2 service"

  image_id               = local.ami_id
  instance_type          = var.strategy_engine_instance_type
  key_name               = var.key_pair_name != "" ? var.key_pair_name : null
  update_default_version = true

  iam_instance_profile {
    arn = aws_iam_instance_profile.strategy_engine.arn
  }

  network_interfaces {
    associate_public_ip_address = false
    security_groups             = [aws_security_group.ec2_trading.id]
    delete_on_termination       = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = var.instance_metadata_http_tokens
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  monitoring {
    enabled = true
  }

  user_data = base64encode(templatefile(
    "${path.module}/userdata/strategy_engine.sh",
    {
      environment               = var.environment
      service_name              = "strategy-engine"
      ecr_base                  = local.ecr_base
      aws_region                = var.aws_region
      log_level                 = var.log_level
      dynamodb_table_prefix     = var.dynamodb_table_prefix
      s3_bucket_model_artifacts = var.s3_bucket_model_artifacts
      strategy_watchlist_nse    = var.strategy_watchlist_nse
    }
  ))

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.common_tags, {
      Name    = "quantembrace-${var.environment}-strategy-engine"
      Service = "strategy-engine"
    })
  }

  tag_specifications {
    resource_type = "volume"
    tags = merge(local.common_tags, {
      Name    = "quantembrace-${var.environment}-strategy-engine-root"
      Service = "strategy-engine"
    })
  }

  tags = merge(local.common_tags, { Name = "quantembrace-${var.environment}-strategy-engine-lt" })
}

resource "aws_autoscaling_group" "strategy_engine" {
  name                      = "quantembrace-${var.environment}-strategy-engine-asg"
  min_size                  = 1
  max_size                  = var.strategy_engine_max_capacity
  desired_capacity          = 1
  health_check_type         = "EC2"
  health_check_grace_period = 180                    # Longer grace: model loading takes time
  vpc_zone_identifier       = var.private_subnet_ids # Multi-AZ for scale-out

  launch_template {
    id      = aws_launch_template.strategy_engine.id
    version = "$Latest"
  }

  # No placement group — allows ASG to distribute across AZs for scale-out

  dynamic "warm_pool" {
    for_each = var.enable_warm_pools ? [1] : []
    content {
      pool_state                  = "Stopped"
      min_size                    = 1
      max_group_prepared_capacity = 2
      instance_reuse_policy {
        reuse_on_scale_in = true
      }
    }
  }

  # CPU-based scaling policy
  lifecycle {
    create_before_destroy = true
    ignore_changes        = [desired_capacity]
  }

  tag {
    key                 = "Name"
    value               = "quantembrace-${var.environment}-strategy-engine"
    propagate_at_launch = true
  }
  tag {
    key                 = "Service"
    value               = "strategy-engine"
    propagate_at_launch = true
  }
  tag {
    key                 = "Environment"
    value               = var.environment
    propagate_at_launch = true
  }
}

# CPU-based scale-out for strategy engine
resource "aws_autoscaling_policy" "strategy_engine_scale_out" {
  count                  = var.strategy_engine_max_capacity > 1 ? 1 : 0
  name                   = "quantembrace-${var.environment}-strategy-engine-scale-out"
  autoscaling_group_name = aws_autoscaling_group.strategy_engine.name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ASGAverageCPUUtilization"
    }
    target_value = 70.0
  }
}

resource "aws_autoscaling_schedule" "strategy_engine_on" {
  count                  = var.enable_scheduled_scaling ? 1 : 0
  scheduled_action_name  = "strategy-engine-on"
  autoscaling_group_name = aws_autoscaling_group.strategy_engine.name
  recurrence             = var.strategy_engine_on_cron
  min_size               = 1
  max_size               = var.strategy_engine_max_capacity
  desired_capacity       = 1
}

resource "aws_autoscaling_schedule" "strategy_engine_off" {
  count                  = var.enable_scheduled_scaling ? 1 : 0
  scheduled_action_name  = "strategy-engine-off"
  autoscaling_group_name = aws_autoscaling_group.strategy_engine.name
  recurrence             = var.strategy_engine_off_cron
  min_size               = 0
  max_size               = var.strategy_engine_max_capacity
  desired_capacity       = 0
}

# ═══════════════════════════════════════════════════════════════
# RISK ENGINE
# ═══════════════════════════════════════════════════════════════

resource "aws_launch_template" "risk_engine" {
  name        = "quantembrace-${var.environment}-risk-engine"
  description = "Launch template for risk-engine EC2 service"

  image_id               = local.ami_id
  instance_type          = var.risk_engine_instance_type
  key_name               = var.key_pair_name != "" ? var.key_pair_name : null
  update_default_version = true

  iam_instance_profile {
    arn = aws_iam_instance_profile.risk_engine.arn
  }

  network_interfaces {
    associate_public_ip_address = false
    security_groups             = [aws_security_group.ec2_trading.id]
    delete_on_termination       = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = var.instance_metadata_http_tokens
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  monitoring {
    enabled = true
  }

  user_data = base64encode(templatefile(
    "${path.module}/userdata/risk_engine.sh",
    {
      environment            = var.environment
      service_name           = "risk-engine"
      ecr_base               = local.ecr_base
      aws_region             = var.aws_region
      log_level              = var.log_level
      dynamodb_table_prefix  = var.dynamodb_table_prefix
      s3_bucket_trading_logs = var.s3_bucket_trading_logs
    }
  ))

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.common_tags, {
      Name    = "quantembrace-${var.environment}-risk-engine"
      Service = "risk-engine"
    })
  }

  tag_specifications {
    resource_type = "volume"
    tags = merge(local.common_tags, {
      Name    = "quantembrace-${var.environment}-risk-engine-root"
      Service = "risk-engine"
    })
  }

  tags = merge(local.common_tags, { Name = "quantembrace-${var.environment}-risk-engine-lt" })
}

resource "aws_autoscaling_group" "risk_engine" {
  name                      = "quantembrace-${var.environment}-risk-engine-asg"
  min_size                  = 1
  max_size                  = 1
  desired_capacity          = 1
  health_check_type         = "EC2"
  health_check_grace_period = 180
  vpc_zone_identifier       = [var.primary_subnet_id]

  launch_template {
    id      = aws_launch_template.risk_engine.id
    version = "$Latest"
  }

  dynamic "warm_pool" {
    for_each = var.enable_warm_pools ? [1] : []
    content {
      pool_state                  = "Stopped"
      min_size                    = 1
      max_group_prepared_capacity = 1
      instance_reuse_policy {
        reuse_on_scale_in = true
      }
    }
  }

  lifecycle {
    create_before_destroy = true
    ignore_changes        = [desired_capacity]
  }

  tag {
    key                 = "Name"
    value               = "quantembrace-${var.environment}-risk-engine"
    propagate_at_launch = true
  }
  tag {
    key                 = "Service"
    value               = "risk-engine"
    propagate_at_launch = true
  }
  tag {
    key                 = "Environment"
    value               = var.environment
    propagate_at_launch = true
  }
}

# ═══════════════════════════════════════════════════════════════
# EXECUTION ENGINE
# ═══════════════════════════════════════════════════════════════

resource "aws_launch_template" "execution_engine" {
  name        = "quantembrace-${var.environment}-execution-engine"
  description = "Launch template for execution-engine EC2 service (cluster placement)"

  image_id               = local.ami_id
  instance_type          = var.execution_engine_instance_type
  key_name               = var.key_pair_name != "" ? var.key_pair_name : null
  update_default_version = true

  iam_instance_profile {
    arn = aws_iam_instance_profile.execution_engine.arn
  }

  network_interfaces {
    associate_public_ip_address = false
    security_groups             = [aws_security_group.ec2_trading.id]
    delete_on_termination       = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = var.instance_metadata_http_tokens
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  monitoring {
    enabled = true
  }

  user_data = base64encode(templatefile(
    "${path.module}/userdata/execution_engine.sh",
    {
      environment            = var.environment
      service_name           = "execution-engine"
      ecr_base               = local.ecr_base
      aws_region             = var.aws_region
      log_level              = var.log_level
      dynamodb_table_prefix  = var.dynamodb_table_prefix
      s3_bucket_trading_logs = var.s3_bucket_trading_logs
      secrets_zerodha_arn    = var.secrets_zerodha_arn
      secrets_alpaca_arn     = var.secrets_alpaca_arn
      strategy_watchlist_nse = var.strategy_watchlist_nse
    }
  ))

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.common_tags, {
      Name    = "quantembrace-${var.environment}-execution-engine"
      Service = "execution-engine"
    })
  }

  tag_specifications {
    resource_type = "volume"
    tags = merge(local.common_tags, {
      Name    = "quantembrace-${var.environment}-execution-engine-root"
      Service = "execution-engine"
    })
  }

  tags = merge(local.common_tags, { Name = "quantembrace-${var.environment}-execution-engine-lt" })
}

resource "aws_autoscaling_group" "execution_engine" {
  name                      = "quantembrace-${var.environment}-execution-engine-asg"
  min_size                  = 1
  max_size                  = 1 # HARD LIMIT — singleton enforcement
  desired_capacity          = 1
  health_check_type         = "EC2"
  health_check_grace_period = 120
  vpc_zone_identifier       = [var.primary_subnet_id] # Singleton in AZ-a only

  launch_template {
    id      = aws_launch_template.execution_engine.id
    version = "$Latest"
  }

  # Cluster placement group for lowest-latency DynamoDB and MSK Kafka access
  placement_group = aws_placement_group.execution_engine.id

  dynamic "warm_pool" {
    for_each = var.enable_warm_pools ? [1] : []
    content {
      pool_state                  = "Stopped"
      min_size                    = 1
      max_group_prepared_capacity = 1
      instance_reuse_policy {
        reuse_on_scale_in = true
      }
    }
  }

  # Termination lifecycle hook: wait for inflight orders to complete
  initial_lifecycle_hook {
    name                 = "execution-engine-drain"
    default_result       = "CONTINUE"
    heartbeat_timeout    = 300 # 5 minutes max drain time
    lifecycle_transition = "autoscaling:EC2_INSTANCE_TERMINATING"
  }

  lifecycle {
    create_before_destroy = true
    ignore_changes        = [desired_capacity]
  }

  tag {
    key                 = "Name"
    value               = "quantembrace-${var.environment}-execution-engine"
    propagate_at_launch = true
  }
  tag {
    key                 = "Service"
    value               = "execution-engine"
    propagate_at_launch = true
  }
  tag {
    key                 = "Environment"
    value               = var.environment
    propagate_at_launch = true
  }
}

# ═══════════════════════════════════════════════════════════════
# AI ENGINE  (Phase 6 — ML + Agentic Layer)
#
# Runs continuously while any market session is open.
# Consumes signals.pending → enriches with regime + quality →
# publishes to signals.enriched for risk_engine.
#
# Instance sizing: c6g.large
#   joblib HMM inference is CPU-bound (~1ms per signal at batch).
#   At NSE peak rate (~200 signals/min), sustained CPU load is real.
#   c6g.large (2 vCPU, 4 GiB) keeps P99 enrichment latency < 15ms.
#
# ASG: min=1, max=2
#   Kafka consumer group (aiengine-v1) partitions across instances.
#   Max 2 to bound model-load memory and Kafka rebalance latency.
#   Single AZ (primary_subnet_id) for stable MSK partition assignment.
# ═══════════════════════════════════════════════════════════════

resource "aws_launch_template" "ai_engine" {
  name        = "quantembrace-${var.environment}-ai-engine"
  description = "Launch template for ai-engine EC2 service (Phase 6 ML enrichment)"

  image_id               = local.ami_id
  instance_type          = var.ai_engine_instance_type
  key_name               = var.key_pair_name != "" ? var.key_pair_name : null
  update_default_version = true

  iam_instance_profile {
    arn = aws_iam_instance_profile.ai_engine.arn
  }

  network_interfaces {
    associate_public_ip_address = false
    security_groups             = [aws_security_group.ec2_trading.id]
    delete_on_termination       = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = var.instance_metadata_http_tokens
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  monitoring {
    enabled = true # 1-min granularity — latency alerting needs fine resolution
  }

  user_data = base64encode(templatefile(
    "${path.module}/userdata/ai_engine.sh",
    {
      environment               = var.environment
      service_name              = "ai-engine"
      ecr_base                  = local.ecr_base
      aws_region                = var.aws_region
      log_level                 = var.log_level
      dynamodb_table_prefix     = var.dynamodb_table_prefix
      s3_bucket_model_artifacts = var.s3_bucket_model_artifacts
    }
  ))

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.common_tags, {
      Name    = "quantembrace-${var.environment}-ai-engine"
      Service = "ai-engine"
      Phase   = "6-ml-agentic"
    })
  }

  tag_specifications {
    resource_type = "volume"
    tags = merge(local.common_tags, {
      Name    = "quantembrace-${var.environment}-ai-engine-root"
      Service = "ai-engine"
    })
  }

  tags = merge(local.common_tags, { Name = "quantembrace-${var.environment}-ai-engine-lt" })
}

resource "aws_autoscaling_group" "ai_engine" {
  name                      = "quantembrace-${var.environment}-ai-engine-asg"
  min_size                  = 1
  max_size                  = 2
  desired_capacity          = 1
  health_check_type         = "EC2"
  health_check_grace_period = 180                     # Model download from S3 on cold boot takes ~30s
  vpc_zone_identifier       = [var.primary_subnet_id] # Single AZ for stable Kafka partition assignment

  launch_template {
    id      = aws_launch_template.ai_engine.id
    version = "$Latest"
  }

  # No placement group: enrichment is not latency-critical for the cluster path.
  # Spreading to a second AZ for scale-out is acceptable (adds ~1ms intra-AZ).

  dynamic "warm_pool" {
    for_each = var.enable_warm_pools ? [1] : []
    content {
      pool_state                  = "Stopped"
      min_size                    = 1
      max_group_prepared_capacity = 2
      instance_reuse_policy {
        reuse_on_scale_in = true
      }
    }
  }

  lifecycle {
    create_before_destroy = true
    ignore_changes        = [desired_capacity]
  }

  tag {
    key                 = "Name"
    value               = "quantembrace-${var.environment}-ai-engine"
    propagate_at_launch = true
  }
  tag {
    key                 = "Service"
    value               = "ai-engine"
    propagate_at_launch = true
  }
  tag {
    key                 = "Environment"
    value               = var.environment
    propagate_at_launch = true
  }
  tag {
    key                 = "Phase"
    value               = "6-ml-agentic"
    propagate_at_launch = true
  }
}

# CPU-based scale-out: second instance joins when enrichment backpressure builds
resource "aws_autoscaling_policy" "ai_engine_scale_out" {
  name                   = "quantembrace-${var.environment}-ai-engine-scale-out"
  autoscaling_group_name = aws_autoscaling_group.ai_engine.name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ASGAverageCPUUtilization"
    }
    target_value = 70.0 # HMM inference keeps CPU 40–60% normally; 70% means lag is building
  }
}

# Market-hours schedule: align with strategy engine (ai_engine must be up before first signal)
resource "aws_autoscaling_schedule" "ai_engine_on" {
  count                  = var.enable_scheduled_scaling ? 1 : 0
  scheduled_action_name  = "ai-engine-on"
  autoscaling_group_name = aws_autoscaling_group.ai_engine.name
  recurrence             = var.strategy_engine_on_cron # 03:00 UTC (08:30 IST) Mon-Fri
  min_size               = 1
  max_size               = 2
  desired_capacity       = 1
}

resource "aws_autoscaling_schedule" "ai_engine_off" {
  count                  = var.enable_scheduled_scaling ? 1 : 0
  scheduled_action_name  = "ai-engine-off"
  autoscaling_group_name = aws_autoscaling_group.ai_engine.name
  recurrence             = var.strategy_engine_off_cron # 01:30 UTC (07:00 IST+1) Tue-Sat
  # Scale to 0 after US close: enrichment has no consumers once execution halts.
  # StrategySelector post-market run (10:15 UTC) must complete before this fires.
  # strategy_engine_off_cron defaults to 01:30 UTC — well after 10:15 UTC.
  min_size         = 0
  max_size         = 2
  desired_capacity = 0
}

# Execution engine uses same market-hours schedule as strategy engine
resource "aws_autoscaling_schedule" "execution_engine_on" {
  count                  = var.enable_scheduled_scaling ? 1 : 0
  scheduled_action_name  = "execution-engine-on"
  autoscaling_group_name = aws_autoscaling_group.execution_engine.name
  recurrence             = var.strategy_engine_on_cron
  min_size               = 1
  max_size               = 1
  desired_capacity       = 1
}

resource "aws_autoscaling_schedule" "execution_engine_off" {
  count                  = var.enable_scheduled_scaling ? 1 : 0
  scheduled_action_name  = "execution-engine-off"
  autoscaling_group_name = aws_autoscaling_group.execution_engine.name
  recurrence             = var.strategy_engine_off_cron
  # ALWAYS-ON: execution engine must never scale to zero. Even outside market
  # hours, the service must be reachable for emergency order cancellations,
  # position reconciliation, and unexpected fill events from the broker.
  # One t3.small instance overnight costs ~$0.025/hr — trivial vs. the risk
  # of a dangling position with no cancellation path.
  min_size         = 1
  max_size         = 1
  desired_capacity = 1
}

# ── CloudWatch Alarms: EC2 ASG health ───────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "asg_unhealthy" {
  for_each = {
    "data-ingestion-nse" = aws_autoscaling_group.data_ingestion_nse.name
    "data-ingestion-us"  = aws_autoscaling_group.data_ingestion_us.name
    "strategy-engine"    = aws_autoscaling_group.strategy_engine.name
    "risk-engine"        = aws_autoscaling_group.risk_engine.name
    "execution-engine"   = aws_autoscaling_group.execution_engine.name
    "ai-engine"          = aws_autoscaling_group.ai_engine.name
  }

  alarm_name          = "quantembrace-${var.environment}-${each.key}-unhealthy"
  alarm_description   = "EC2 ASG has unhealthy instance for ${each.key}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "GroupTotalInstances"
  namespace           = "AWS/AutoScaling"
  period              = 60
  statistic           = "Minimum"
  threshold           = 0
  treat_missing_data  = "breaching"

  dimensions = {
    AutoScalingGroupName = each.value
  }

  alarm_actions = [var.sns_alerts_topic_arn]
  ok_actions    = [var.sns_alerts_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "ec2_cpu_high" {
  for_each = {
    "strategy-engine"  = aws_autoscaling_group.strategy_engine.name
    "risk-engine"      = aws_autoscaling_group.risk_engine.name
    "execution-engine" = aws_autoscaling_group.execution_engine.name
    "ai-engine"        = aws_autoscaling_group.ai_engine.name
  }

  alarm_name          = "quantembrace-${var.environment}-${each.key}-cpu-high"
  alarm_description   = "CPU > 85% for ${each.key} — consider instance type upgrade"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = 300
  statistic           = "Average"
  threshold           = 85

  dimensions = {
    AutoScalingGroupName = each.value
  }

  alarm_actions = [var.sns_alerts_topic_arn]
}
