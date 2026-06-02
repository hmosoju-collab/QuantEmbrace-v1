###############################################################################
# QuantEmbrace — Kafka Module (Phase 2)
#
# Resources
# ─────────
#   MSK Serverless cluster      IAM-authenticated, 2-AZ private VPC
#   Security group              MSK cluster — allows port 9098 from EC2 SG
#   IAM policies (4 services)   data_ingestion, strategy_engine,
#                               risk_engine, execution_engine
#   IAM policy (ops admin)      Topic create/delete/describe — for setup script
#   IAM policy attachments      Attached to the EC2 instance role names supplied
#
# Authentication: IAM only (SASL/IAM over TLS on port 9098)
# Topics:  Created separately via scripts/kafka/create_topics.py
#          (auto.create.topics.enable=false is the MSK Serverless default)
#
# Topic map (from phase2_final_approved.md §2):
#   ticks.nse           4 parts · 24h  · instrument_id key
#   ticks.us            2 parts · 24h  · instrument_id key
#   signals.pending     2 parts ·  1h  · instrument_id key
#   signals.approved    2 parts · 30m  · instrument_id key
#   orders.events       4 parts ·  7d  · instrument_id key
#   risk.kill-switch    1 part  · 30d  · "GLOBAL" key
#   ops.audit           2 parts · 90d  · trace_id key
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

locals {
  cluster_name = var.cluster_name != "" ? var.cluster_name : "${var.project}-${var.environment}-kafka"
  common_tags = merge(var.tags, {
    Module      = "kafka"
    Environment = var.environment
    ManagedBy   = "terraform"
  })

  # Canonical topic names — used in IAM resource ARNs
  topic_ticks_nse        = "ticks.nse"
  topic_ticks_us         = "ticks.us"
  topic_signals_pending  = "signals.pending"
  topic_signals_enriched = "signals.enriched" # Phase 6 — ai_engine publishes here
  topic_signals_approved = "signals.approved"
  topic_orders_events    = "orders.events"
  topic_kill_switch      = "risk.kill-switch"
  topic_ops_audit        = "ops.audit"

  topic_ticks_nse_retry        = "${local.topic_ticks_nse}.retry"
  topic_ticks_nse_dlq          = "${local.topic_ticks_nse}.dlq"
  topic_ticks_us_retry         = "${local.topic_ticks_us}.retry"
  topic_ticks_us_dlq           = "${local.topic_ticks_us}.dlq"
  topic_signals_pending_retry  = "${local.topic_signals_pending}.retry"
  topic_signals_pending_dlq    = "${local.topic_signals_pending}.dlq"
  topic_signals_enriched_retry = "${local.topic_signals_enriched}.retry"
  topic_signals_enriched_dlq   = "${local.topic_signals_enriched}.dlq"
  topic_signals_approved_retry = "${local.topic_signals_approved}.retry"
  topic_signals_approved_dlq   = "${local.topic_signals_approved}.dlq"
  topic_orders_events_retry    = "${local.topic_orders_events}.retry"
  topic_orders_events_dlq      = "${local.topic_orders_events}.dlq"
  topic_kill_switch_retry      = "${local.topic_kill_switch}.retry"
  topic_kill_switch_dlq        = "${local.topic_kill_switch}.dlq"

  # Consumer group names — used in IAM resource ARNs
  cg_strategy              = "strategy-v1"
  cg_strategy_retry        = "strategy-retry-v1"
  cg_risk                  = "risk-v1"
  cg_risk_retry            = "risk-retry-v1"
  cg_execution             = "execution-v1"
  cg_execution_retry       = "execution-retry-v1"
  cg_execution_kill_switch = "execution-v1-kill-switch"
  cg_aiengine              = "aiengine-v1" # Phase 6 — ai_engine consumer group

  # MSK cluster ARN pattern for IAM resource scoping
  cluster_arn_pattern = "arn:aws:kafka:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:cluster/${local.cluster_name}/*"

  # Helper to build topic ARN patterns
  topic_arn = "arn:aws:kafka:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:topic/${local.cluster_name}/*"

  # Helper to build group ARN pattern
  group_arn = "arn:aws:kafka:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:group/${local.cluster_name}/*"
}

# ── Security Group: MSK Serverless ───────────────────────────────────────────

resource "aws_security_group" "msk" {
  name        = "${var.project}-${var.environment}-msk-kafka"
  description = "MSK Serverless cluster — allow IAM/TLS on 9098 from EC2 services"
  vpc_id      = var.vpc_id

  # Allow EC2 service instances to connect to MSK broker (IAM/TLS port)
  ingress {
    description     = "Kafka IAM/TLS from EC2 services"
    from_port       = 9098
    to_port         = 9098
    protocol        = "tcp"
    security_groups = [var.ec2_services_security_group_id]
  }

  # MSK Serverless needs egress for AWS control plane communication
  egress {
    description = "All egress"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, { Name = "${var.project}-${var.environment}-msk-kafka" })

  lifecycle {
    create_before_destroy = true
  }
}

# ── MSK Serverless Cluster ────────────────────────────────────────────────────

resource "aws_msk_serverless_cluster" "main" {
  cluster_name = local.cluster_name

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.msk.id]
  }

  client_authentication {
    sasl {
      iam {
        enabled = true
      }
    }
  }

  tags = merge(local.common_tags, { Name = local.cluster_name })
}

# ── IAM Policy: DATA INGESTION ────────────────────────────────────────────────
# Produces: ticks.nse, ticks.us, ops.audit
# No consuming.

resource "aws_iam_policy" "kafka_data_ingestion" {
  name        = "QuantEmbrace-${var.environment}-KafkaDataIngestion"
  description = "Kafka permissions for data-ingestion: produce ticks.nse, ticks.us, ops.audit"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Connect to the cluster
      {
        Sid      = "MSKConnect"
        Effect   = "Allow"
        Action   = ["kafka-cluster:Connect"]
        Resource = [local.cluster_arn_pattern]
      },
      # Describe + write on tick and audit topics
      {
        Sid    = "MSKProduceTicks"
        Effect = "Allow"
        Action = [
          "kafka-cluster:WriteData",
          "kafka-cluster:DescribeTopic",
        ]
        Resource = [
          "${local.topic_arn}/${local.topic_ticks_nse}",
          "${local.topic_arn}/${local.topic_ticks_us}",
          "${local.topic_arn}/${local.topic_ops_audit}",
        ]
      },
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "kafka_data_ingestion" {
  role       = var.data_ingestion_role_name
  policy_arn = aws_iam_policy.kafka_data_ingestion.arn
}

# ── IAM Policy: STRATEGY ENGINE ───────────────────────────────────────────────
# Consumes: ticks.nse, ticks.us, ticks.*.retry
# Produces: signals.pending, replayed ticks, and tick retry/DLQ records

resource "aws_iam_policy" "kafka_strategy_engine" {
  name        = "QuantEmbrace-${var.environment}-KafkaStrategyEngine"
  description = "Kafka permissions for strategy-engine: consume ticks, produce signals.pending"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "MSKConnect"
        Effect   = "Allow"
        Action   = ["kafka-cluster:Connect"]
        Resource = [local.cluster_arn_pattern]
      },
      # Consume ticks
      {
        Sid    = "MSKConsumeTicks"
        Effect = "Allow"
        Action = [
          "kafka-cluster:ReadData",
          "kafka-cluster:DescribeTopic",
        ]
        Resource = [
          "${local.topic_arn}/${local.topic_ticks_nse}",
          "${local.topic_arn}/${local.topic_ticks_us}",
          "${local.topic_arn}/${local.topic_ticks_nse_retry}",
          "${local.topic_arn}/${local.topic_ticks_us_retry}",
        ]
      },
      # Consumer group offset management
      {
        Sid    = "MSKConsumerGroup"
        Effect = "Allow"
        Action = [
          "kafka-cluster:DescribeGroup",
          "kafka-cluster:AlterGroup",
        ]
        Resource = [
          "${local.group_arn}/${local.cg_strategy}",
          "${local.group_arn}/${local.cg_strategy_retry}",
        ]
      },
      # Produce signals.pending, replayed ticks, and inspectable tick retry/DLQ records
      {
        Sid    = "MSKProduceSignals"
        Effect = "Allow"
        Action = [
          "kafka-cluster:WriteData",
          "kafka-cluster:DescribeTopic",
        ]
        Resource = [
          "${local.topic_arn}/${local.topic_signals_pending}",
          "${local.topic_arn}/${local.topic_ticks_nse}",
          "${local.topic_arn}/${local.topic_ticks_us}",
          "${local.topic_arn}/${local.topic_ticks_nse_retry}",
          "${local.topic_arn}/${local.topic_ticks_nse_dlq}",
          "${local.topic_arn}/${local.topic_ticks_us_retry}",
          "${local.topic_arn}/${local.topic_ticks_us_dlq}",
        ]
      },
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "kafka_strategy_engine" {
  role       = var.strategy_engine_role_name
  policy_arn = aws_iam_policy.kafka_strategy_engine.arn
}

# ── IAM Policy: RISK ENGINE ───────────────────────────────────────────────────
# Consumes: signals.pending, signals.enriched (Phase 6), orders.events,
#           their retry topics, ticks, kill-switch
# Produces: signals.approved, replayed pending/enriched/order events,
#           risk.kill-switch, ops.audit, signals.enriched.retry/dlq (Phase 7)

resource "aws_iam_policy" "kafka_risk_engine" {
  name        = "QuantEmbrace-${var.environment}-KafkaRiskEngine"
  description = "Kafka permissions for risk-engine: consume pending/orders/ticks, produce approved/kill-switch/audit"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "MSKConnect"
        Effect   = "Allow"
        Action   = ["kafka-cluster:Connect"]
        Resource = [local.cluster_arn_pattern]
      },
      # Consume: signals.pending, signals.enriched (Phase 6 primary path),
      #          orders.events, ticks (for lag/price monitoring), kill-switch
      {
        Sid    = "MSKConsumeRiskInputs"
        Effect = "Allow"
        Action = [
          "kafka-cluster:ReadData",
          "kafka-cluster:DescribeTopic",
        ]
        Resource = [
          "${local.topic_arn}/${local.topic_signals_pending}",
          "${local.topic_arn}/${local.topic_signals_pending_retry}",
          # Phase 6: primary enriched path; Phase 7: retry replayer reads .retry
          "${local.topic_arn}/${local.topic_signals_enriched}",
          "${local.topic_arn}/${local.topic_signals_enriched_retry}",
          "${local.topic_arn}/${local.topic_orders_events}",
          "${local.topic_arn}/${local.topic_orders_events_retry}",
          "${local.topic_arn}/${local.topic_ticks_nse}",
          "${local.topic_arn}/${local.topic_ticks_us}",
          # Risk engine's kill-switch-listener reads from risk.kill-switch too
          "${local.topic_arn}/${local.topic_kill_switch}",
        ]
      },
      # Consumer group offset management
      {
        Sid    = "MSKConsumerGroup"
        Effect = "Allow"
        Action = [
          "kafka-cluster:DescribeGroup",
          "kafka-cluster:AlterGroup",
        ]
        Resource = [
          "${local.group_arn}/${local.cg_risk}",
          "${local.group_arn}/${local.cg_risk_retry}",
        ]
      },
      # Produce: signals.approved, replayed signals.pending/enriched/orders.events,
      #          risk.kill-switch, ops.audit, enriched retry/DLQ (Phase 7)
      {
        Sid    = "MSKProduceRiskOutputs"
        Effect = "Allow"
        Action = [
          "kafka-cluster:WriteData",
          "kafka-cluster:DescribeTopic",
        ]
        Resource = [
          "${local.topic_arn}/${local.topic_signals_approved}",
          "${local.topic_arn}/${local.topic_signals_pending}",
          "${local.topic_arn}/${local.topic_orders_events}",
          "${local.topic_arn}/${local.topic_kill_switch}",
          "${local.topic_arn}/${local.topic_ops_audit}",
          "${local.topic_arn}/${local.topic_signals_pending_retry}",
          "${local.topic_arn}/${local.topic_signals_pending_dlq}",
          # Phase 7: retry replayer replays to signals.enriched; failure publisher writes .retry/.dlq
          "${local.topic_arn}/${local.topic_signals_enriched}",
          "${local.topic_arn}/${local.topic_signals_enriched_retry}",
          "${local.topic_arn}/${local.topic_signals_enriched_dlq}",
          "${local.topic_arn}/${local.topic_orders_events_retry}",
          "${local.topic_arn}/${local.topic_orders_events_dlq}",
          "${local.topic_arn}/${local.topic_kill_switch_retry}",
          "${local.topic_arn}/${local.topic_kill_switch_dlq}",
        ]
      },
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "kafka_risk_engine" {
  role       = var.risk_engine_role_name
  policy_arn = aws_iam_policy.kafka_risk_engine.arn
}

# ── IAM Policy: EXECUTION ENGINE ─────────────────────────────────────────────
# Consumes: signals.approved, signals.approved.retry, risk.kill-switch
# Produces: orders.events, replayed approvals, ops.audit

resource "aws_iam_policy" "kafka_execution_engine" {
  name        = "QuantEmbrace-${var.environment}-KafkaExecutionEngine"
  description = "Kafka permissions for execution-engine: consume approved/kill-switch, produce orders/audit"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "MSKConnect"
        Effect   = "Allow"
        Action   = ["kafka-cluster:Connect"]
        Resource = [local.cluster_arn_pattern]
      },
      # Consume: signals.approved + risk.kill-switch (dedicated listener)
      {
        Sid    = "MSKConsumeExecutionInputs"
        Effect = "Allow"
        Action = [
          "kafka-cluster:ReadData",
          "kafka-cluster:DescribeTopic",
        ]
        Resource = [
          "${local.topic_arn}/${local.topic_signals_approved}",
          "${local.topic_arn}/${local.topic_signals_approved_retry}",
          "${local.topic_arn}/${local.topic_kill_switch}",
        ]
      },
      # Consumer group offset management
      {
        Sid    = "MSKConsumerGroup"
        Effect = "Allow"
        Action = [
          "kafka-cluster:DescribeGroup",
          "kafka-cluster:AlterGroup",
        ]
        Resource = [
          "${local.group_arn}/${local.cg_execution}",
          "${local.group_arn}/${local.cg_execution_retry}",
          "${local.group_arn}/${local.cg_execution_kill_switch}",
        ]
      },
      # Produce: orders.events, replayed signals.approved, ops.audit
      {
        Sid    = "MSKProduceExecutionOutputs"
        Effect = "Allow"
        Action = [
          "kafka-cluster:WriteData",
          "kafka-cluster:DescribeTopic",
        ]
        Resource = [
          "${local.topic_arn}/${local.topic_orders_events}",
          "${local.topic_arn}/${local.topic_signals_approved}",
          "${local.topic_arn}/${local.topic_ops_audit}",
          "${local.topic_arn}/${local.topic_signals_approved_retry}",
          "${local.topic_arn}/${local.topic_signals_approved_dlq}",
        ]
      },
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "kafka_execution_engine" {
  role       = var.execution_engine_role_name
  policy_arn = aws_iam_policy.kafka_execution_engine.arn
}

# ── IAM Policy: AI ENGINE (Phase 6) ──────────────────────────────────────────
# Reads:   signals.pending  (consumer group aiengine-v1)
# Writes:  signals.enriched (4 partitions, key=symbol)
# Groups:  aiengine-v1

resource "aws_iam_policy" "kafka_ai_engine" {
  name        = "QuantEmbrace-${var.environment}-KafkaAIEngine"
  description = "Kafka permissions for ai_engine: consume signals.pending, produce signals.enriched"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "MSKConnect"
        Effect   = "Allow"
        Action   = ["kafka-cluster:Connect"]
        Resource = [local.cluster_arn_pattern]
      },
      # Consume from signals.pending (aiengine-v1 consumer group)
      {
        Sid    = "MSKConsumePending"
        Effect = "Allow"
        Action = [
          "kafka-cluster:ReadData",
          "kafka-cluster:DescribeTopic",
        ]
        Resource = [
          "arn:aws:kafka:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:topic/${local.cluster_name}/*/${local.topic_signals_pending}",
        ]
      },
      # Produce to signals.enriched
      {
        Sid    = "MSKProduceEnriched"
        Effect = "Allow"
        Action = [
          "kafka-cluster:WriteData",
          "kafka-cluster:DescribeTopic",
          "kafka-cluster:CreateTopic",
        ]
        Resource = [
          "arn:aws:kafka:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:topic/${local.cluster_name}/*/${local.topic_signals_enriched}",
        ]
      },
      # Consumer group: aiengine-v1
      {
        Sid    = "MSKConsumerGroupAIEngine"
        Effect = "Allow"
        Action = [
          "kafka-cluster:ReadData",
          "kafka-cluster:DescribeGroup",
          "kafka-cluster:AlterGroup",
        ]
        Resource = [
          "arn:aws:kafka:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:group/${local.cluster_name}/*/${local.cg_aiengine}",
        ]
      },
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "kafka_ai_engine" {
  count      = var.ai_engine_role_name != "" ? 1 : 0
  role       = var.ai_engine_role_name
  policy_arn = aws_iam_policy.kafka_ai_engine.arn
}

# ── IAM Policy: OPS ADMIN (topic setup, monitoring) ──────────────────────────
# Used by: scripts/kafka/create_topics.py, bastion host, CI/CD runners
# Only attached when ops_admin_role_name is provided.

resource "aws_iam_policy" "kafka_ops_admin" {
  name        = "QuantEmbrace-${var.environment}-KafkaOpsAdmin"
  description = "Kafka admin permissions: create/delete/describe topics and groups (ops use only)"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "MSKConnect"
        Effect   = "Allow"
        Action   = ["kafka-cluster:Connect"]
        Resource = [local.cluster_arn_pattern]
      },
      # Full topic management
      {
        Sid    = "MSKAdminTopics"
        Effect = "Allow"
        Action = [
          "kafka-cluster:CreateTopic",
          "kafka-cluster:DeleteTopic",
          "kafka-cluster:DescribeTopic",
          "kafka-cluster:AlterTopic",
          "kafka-cluster:WriteData",
          "kafka-cluster:ReadData",
        ]
        Resource = ["${local.topic_arn}/*"]
      },
      # Consumer group management (for lag inspection tools)
      {
        Sid    = "MSKAdminGroups"
        Effect = "Allow"
        Action = [
          "kafka-cluster:DescribeGroup",
          "kafka-cluster:AlterGroup",
          "kafka-cluster:DeleteGroup",
        ]
        Resource = ["${local.group_arn}/*"]
      },
      # Describe cluster (for kcat, kafka-topics.sh)
      {
        Sid    = "MSKDescribeCluster"
        Effect = "Allow"
        Action = [
          "kafka:DescribeClusterV2",
          "kafka:GetBootstrapBrokers",
          "kafka:ListClustersV2",
        ]
        Resource = ["*"]
      },
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "kafka_ops_admin" {
  count      = var.ops_admin_role_name != "" ? 1 : 0
  role       = var.ops_admin_role_name
  policy_arn = aws_iam_policy.kafka_ops_admin.arn
}

# ── CloudWatch Alarm: MSK write failures ─────────────────────────────────────
# Guards the kill-switch fallback path (§8 of phase2_final_approved.md):
# "3 consecutive delivery failures on orders.events or signals.pending → kill switch"
# The alarm here fires on KafkaDataLogs errors at the MSK level.
# Per-service delivery failure tracking is in the application (KafkaTickPublisher etc.)

resource "aws_cloudwatch_metric_alarm" "msk_client_connections_low" {
  alarm_name          = "${var.project}-${var.environment}-msk-client-connections-low"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  metric_name         = "ActiveClientConnections"
  namespace           = "AWS/Kafka"
  period              = 60
  statistic           = "Minimum"
  threshold           = 1
  alarm_description   = <<-EOT
    P1: MSK cluster has no active client connections for 3 consecutive minutes.
    All trading services have disconnected from Kafka. Investigate immediately.
    If confirmed: kill switch should already be active (application-level detection).
  EOT
  treat_missing_data  = "breaching"

  dimensions = {
    Cluster_Name = local.cluster_name
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "msk_bytes_in_low" {
  alarm_name          = "${var.project}-${var.environment}-msk-bytes-in-zero-during-market"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 5
  metric_name         = "BytesInPerSec"
  namespace           = "AWS/Kafka"
  period              = 60
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = <<-EOT
    P2: No bytes written to MSK for 5 consecutive minutes.
    Data ingestion may have stopped publishing ticks. Review data_ingestion service logs.
    Expected during pre-market and post-close hours — dismiss if outside market hours.
  EOT
  treat_missing_data  = "notBreaching"

  dimensions = {
    Cluster_Name = local.cluster_name
  }

  tags = local.common_tags
}
