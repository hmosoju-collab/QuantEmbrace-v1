###############################################################################
# Phase 5 — Features Table
#
# DynamoDB table for the pre-computed technical feature store (online layer).
#
# Key schema:
#   PK  = FEATURE#{market}#{symbol}#{interval}  e.g. FEATURE#NSE#RELIANCE#1m
#   SK  = LATEST                                Online read path (FeatureReader)
#   SK  = CANDLE#{yyyy-mm-ddTHH:MM:SSZ}         Intraday archive source (FeatureArchiver)
#
# Written by:   data_ingestion/features/feature_writer.py (FeatureWriter)
# Read by:      shared/features/feature_reader.py (strategy_engine, risk_engine, ai_engine)
# Archived by:  data_ingestion/features/feature_archiver.py → S3 parquet at POST_CLOSE
#
# TTL:
#   SK=LATEST       — 24 hours (expires before next trading day)
#   SK=CANDLE#{ts}  — 7 days   (recovery window for EOD archiver after crash)
###############################################################################

# ---------------------------------------------------------------------------
# Features Table
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "features" {
  name         = "${local.table_prefix}-features"
  billing_mode = local.billing_mode
  hash_key     = "PK"
  range_key    = "SK"

  read_capacity  = var.use_provisioned_capacity ? local.default_read_capacity : null
  write_capacity = var.use_provisioned_capacity ? local.default_write_capacity : null

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  # TTL attribute — auto-expiry managed per-row
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = var.enable_pitr
  }

  tags = merge(local.common_tags, {
    Name    = "${local.table_prefix}-features"
    Service = "data_ingestion"
    Phase   = "5"
  })
}

# ---------------------------------------------------------------------------
# Auto-scaling for features table (provisioned capacity mode only)
# ---------------------------------------------------------------------------

resource "aws_appautoscaling_target" "features_read" {
  count              = var.use_provisioned_capacity ? 1 : 0
  max_capacity       = var.autoscaling_max_capacity
  min_capacity       = local.default_read_capacity
  resource_id        = "table/${aws_dynamodb_table.features.name}"
  scalable_dimension = "dynamodb:table:ReadCapacityUnits"
  service_namespace  = "dynamodb"
}

resource "aws_appautoscaling_policy" "features_read" {
  count              = var.use_provisioned_capacity ? 1 : 0
  name               = "${local.table_prefix}-features-read-autoscale"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.features_read[0].resource_id
  scalable_dimension = aws_appautoscaling_target.features_read[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.features_read[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "DynamoDBReadCapacityUtilization"
    }
    target_value = 70.0
  }
}

resource "aws_appautoscaling_target" "features_write" {
  count              = var.use_provisioned_capacity ? 1 : 0
  max_capacity       = var.autoscaling_max_capacity
  min_capacity       = local.default_write_capacity
  resource_id        = "table/${aws_dynamodb_table.features.name}"
  scalable_dimension = "dynamodb:table:WriteCapacityUnits"
  service_namespace  = "dynamodb"
}

resource "aws_appautoscaling_policy" "features_write" {
  count              = var.use_provisioned_capacity ? 1 : 0
  name               = "${local.table_prefix}-features-write-autoscale"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.features_write[0].resource_id
  scalable_dimension = aws_appautoscaling_target.features_write[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.features_write[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "DynamoDBWriteCapacityUtilization"
    }
    target_value = 70.0
  }
}

# ---------------------------------------------------------------------------
# IAM Policies
#
# data_ingestion  — PutItem + Query (FeatureWriter + FeatureArchiver)
# strategy_engine — GetItem (FeatureReader)
# risk_engine     — GetItem (FeatureReader)
# ai_engine       — GetItem + Query (FeatureReader + historical scan)
#
# NOTE (PHASE5-FU-001): The policies below are created as standalone resources
# for reference and future use (e.g. cross-account access).  The actual role
# attachments for EC2 services are handled INLINE in
# infra/terraform/modules/ec2_services/iam.tf, which adds DynamoDBFeaturesWrite
# and DynamoDBFeaturesRead statements directly to the existing service policies.
# This avoids Terraform cross-module resource references and keeps each service's
# permissions self-contained in the ec2_services module.
# ---------------------------------------------------------------------------

resource "aws_iam_policy" "features_data_ingestion_write" {
  name        = "${local.table_prefix}-features-data-ingestion-write"
  description = "Allow data_ingestion to write feature items to the features table"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:BatchWriteItem",
        ]
        Resource = aws_dynamodb_table.features.arn
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_policy" "features_strategy_engine_read" {
  name        = "${local.table_prefix}-features-strategy-engine-read"
  description = "Allow strategy_engine to read LATEST feature rows from the features table"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = aws_dynamodb_table.features.arn
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_policy" "features_risk_engine_read" {
  name        = "${local.table_prefix}-features-risk-engine-read"
  description = "Allow risk_engine to read LATEST feature rows from the features table"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = aws_dynamodb_table.features.arn
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_policy" "features_ai_engine_read" {
  name        = "${local.table_prefix}-features-ai-engine-read"
  description = "Allow ai_engine to read and query feature rows (LATEST + CANDLE# SK prefix)"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.features.arn
      }
    ]
  })

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# S3 lifecycle rule for the features/ prefix
#
# MOVED (PHASE5-FU-001): This rule previously lived here as a standalone
# aws_s3_bucket_lifecycle_configuration resource.  That caused a Terraform
# conflict because infra/terraform/modules/s3/main.tf already manages a
# lifecycle configuration on the same bucket (ohlcv_data), and AWS only
# allows one aws_s3_bucket_lifecycle_configuration resource per bucket.
#
# The features/ lifecycle rule is now consolidated into the ohlcv-data
# lifecycle config in infra/terraform/modules/s3/main.tf.
# Tiering: Standard → Standard-IA at 90d → Glacier IR at 365d.
# ---------------------------------------------------------------------------
