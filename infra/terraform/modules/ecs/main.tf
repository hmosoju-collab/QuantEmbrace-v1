###############################################################################
# QuantEmbrace — ECS Fargate Module
# ECS cluster, task definitions, services, IAM roles, Cloud Map service
# discovery, and CloudWatch log groups for all five microservices.
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

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name

  # Service definitions — single source of truth
  services = {
    data_ingestion = {
      cpu               = var.service_configs["data_ingestion"].cpu
      memory            = var.service_configs["data_ingestion"].memory
      desired_count     = var.service_configs["data_ingestion"].desired_count
      port              = 8001
      health_check_path = "/health"
      use_spot          = var.service_configs["data_ingestion"].use_spot
    }
    strategy_engine = {
      cpu               = var.service_configs["strategy_engine"].cpu
      memory            = var.service_configs["strategy_engine"].memory
      desired_count     = var.service_configs["strategy_engine"].desired_count
      port              = 8002
      health_check_path = "/health"
      use_spot          = var.service_configs["strategy_engine"].use_spot
    }
    execution_engine = {
      cpu               = var.service_configs["execution_engine"].cpu
      memory            = var.service_configs["execution_engine"].memory
      desired_count     = var.service_configs["execution_engine"].desired_count
      port              = 8003
      health_check_path = "/health"
      use_spot          = var.service_configs["execution_engine"].use_spot
    }
    risk_engine = {
      cpu               = var.service_configs["risk_engine"].cpu
      memory            = var.service_configs["risk_engine"].memory
      desired_count     = var.service_configs["risk_engine"].desired_count
      port              = 8004
      health_check_path = "/health"
      use_spot          = var.service_configs["risk_engine"].use_spot
    }
    ai_engine = {
      cpu               = var.service_configs["ai_engine"].cpu
      memory            = var.service_configs["ai_engine"].memory
      desired_count     = var.service_configs["ai_engine"].desired_count
      port              = 8005
      health_check_path = "/health"
      use_spot          = var.service_configs["ai_engine"].use_spot
    }
  }

  common_tags = merge(var.tags, {
    Module = "ecs"
  })
}

# ---------------------------------------------------------------------------
# ECS Cluster with Container Insights
# ---------------------------------------------------------------------------

resource "aws_ecs_cluster" "main" {
  name = "${var.project}-${var.environment}"

  setting {
    name  = "containerInsights"
    value = var.enable_container_insights ? "enabled" : "disabled"
  }

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-cluster"
  })
}

# Fargate capacity providers
resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name = aws_ecs_cluster.main.name

  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 1
  }
}

# ---------------------------------------------------------------------------
# Cloud Map Namespace for Service Discovery
# ---------------------------------------------------------------------------

resource "aws_service_discovery_private_dns_namespace" "main" {
  name        = "${var.project}.${var.environment}.local"
  description = "Service discovery namespace for ${var.project} ${var.environment}"
  vpc         = var.vpc_id

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-namespace"
  })
}

resource "aws_service_discovery_service" "services" {
  for_each = local.services

  name = replace(each.key, "_", "-")

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.main.id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# IAM — Task Execution Role (used by ECS agent to pull images, push logs)
# ---------------------------------------------------------------------------

resource "aws_iam_role" "ecs_execution" {
  name = "${var.project}-${var.environment}-ecs-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Allow pulling images from ECR and reading secrets
resource "aws_iam_role_policy" "ecs_execution_extra" {
  name = "${var.project}-${var.environment}-ecs-execution-extra"
  role = aws_iam_role.ecs_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ssm:GetParameters",
          "secretsmanager:GetSecretValue"
        ]
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/${var.project}/${var.environment}/*"
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# IAM — Task Role (used by the application container at runtime)
# ---------------------------------------------------------------------------

resource "aws_iam_role" "ecs_task" {
  name = "${var.project}-${var.environment}-ecs-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy" "ecs_task" {
  name = "${var.project}-${var.environment}-ecs-task-policy"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:BatchGetItem",
          "dynamodb:BatchWriteItem",
          "dynamodb:ConditionCheckItem"
        ]
        Resource = [
          "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.project}-${var.environment}-*",
          "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.project}-${var.environment}-*/index/*"
        ]
      },
      {
        Sid    = "S3Access"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:ListBucket",
          "s3:DeleteObject"
        ]
        Resource = [
          "arn:aws:s3:::${var.project}-${var.environment}-*",
          "arn:aws:s3:::${var.project}-${var.environment}-*/*"
        ]
      },
      {
        Sid    = "SQSAccess"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl"
        ]
        Resource = "arn:aws:sqs:${local.region}:${local.account_id}:${var.project}-${var.environment}-*"
      },
      {
        Sid    = "SNSPublish"
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = "arn:aws:sns:${local.region}:${local.account_id}:${var.project}-${var.environment}-*"
      },
      {
        Sid    = "CloudWatchMetrics"
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData"
        ]
        Resource = "*"
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# CloudWatch Log Groups (one per service)
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "services" {
  for_each = local.services

  name              = "/ecs/${var.project}-${var.environment}/${each.key}"
  retention_in_days = var.log_retention_days

  tags = merge(local.common_tags, {
    Service = each.key
  })
}

# ---------------------------------------------------------------------------
# ECR Repositories (one per service)
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "services" {
  for_each = local.services

  name                 = "${var.project}/${each.key}"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = merge(local.common_tags, {
    Service = each.key
  })
}

resource "aws_ecr_lifecycle_policy" "services" {
  for_each   = local.services
  repository = aws_ecr_repository.services[each.key].name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = {
        type = "expire"
      }
    }]
  })
}

# ---------------------------------------------------------------------------
# Task Definitions
# ---------------------------------------------------------------------------

resource "aws_ecs_task_definition" "services" {
  for_each = local.services

  family                   = "${var.project}-${var.environment}-${each.key}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = each.key
    image     = "${aws_ecr_repository.services[each.key].repository_url}:latest"
    essential = true

    portMappings = [{
      containerPort = each.value.port
      protocol      = "tcp"
    }]

    environment = [
      { name = "SERVICE_NAME", value = each.key },
      { name = "ENVIRONMENT", value = var.environment },
      { name = "AWS_REGION", value = local.region },
      { name = "DYNAMODB_TABLE_PREFIX", value = "${var.project}-${var.environment}" },
      { name = "LOG_LEVEL", value = var.environment == "prod" ? "INFO" : "DEBUG" },
      { name = "SERVICE_PORT", value = tostring(each.value.port) },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.services[each.key].name
        "awslogs-region"        = local.region
        "awslogs-stream-prefix" = "ecs"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "curl -f http://localhost:${each.value.port}${each.value.health_check_path} || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
  }])

  tags = merge(local.common_tags, {
    Service = each.key
  })
}

# ---------------------------------------------------------------------------
# ECS Services
# ---------------------------------------------------------------------------

resource "aws_ecs_service" "services" {
  for_each = local.services

  name            = "${var.project}-${var.environment}-${replace(each.key, "_", "-")}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.services[each.key].arn
  desired_count   = each.value.desired_count
  launch_type     = each.value.use_spot ? null : "FARGATE"

  # Use capacity provider strategy for Spot
  dynamic "capacity_provider_strategy" {
    for_each = each.value.use_spot ? [1] : []
    content {
      capacity_provider = "FARGATE_SPOT"
      weight            = 4
      base              = 0
    }
  }

  dynamic "capacity_provider_strategy" {
    for_each = each.value.use_spot ? [1] : []
    content {
      capacity_provider = "FARGATE"
      weight            = 1
      base              = 1
    }
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.ecs_security_group_id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.services[each.key].arn
  }

  deployment_configuration {
    maximum_percent         = 200
    minimum_healthy_percent = 100
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  propagate_tags = "SERVICE"

  tags = merge(local.common_tags, {
    Service = each.key
  })

  lifecycle {
    ignore_changes = [desired_count]
  }
}
