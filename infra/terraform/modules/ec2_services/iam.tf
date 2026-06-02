# ============================================================
# ec2_services/iam.tf
# IAM Instance Profiles for EC2 trading services
# Phase 1 — EC2 Backbone Migration
#
# Each service gets its own role with least-privilege policies.
# EC2 instance roles for ARM64 ASG services (replaces the removed Fargate task roles)
# for EC2 instance profiles (includes SSM + CW Agent policies).
# ============================================================

# ── Shared trust policy for EC2 ─────────────────────────────────────────────

data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# ── Shared custom policy: CloudWatch agent ───────────────────────────────────

resource "aws_iam_policy" "cloudwatch_agent" {
  name        = "QuantEmbrace-${var.environment}-CloudWatchAgent"
  description = "Allow EC2 instances to publish metrics and logs via CloudWatch Agent"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData",
          "ec2:DescribeTags",
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
        ]
        Resource = "*"
      },
    ]
  })
}

# ── Shared custom policy: discover MSK bootstrap brokers at boot ─────────────

resource "aws_iam_policy" "kafka_bootstrap_discovery" {
  name        = "QuantEmbrace-${var.environment}-KafkaBootstrapDiscovery"
  description = "Allow EC2 userdata to resolve MSK Serverless bootstrap brokers"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "kafka:GetBootstrapBrokers",
          "kafka:ListClustersV2",
          "kafka:DescribeClusterV2",
        ]
        Resource = "*"
      },
    ]
  })
}

# ═══════════════════════════════════════════════════════════════
# DATA INGESTION — Role, Policy, Profile
# ═══════════════════════════════════════════════════════════════

resource "aws_iam_role" "data_ingestion" {
  name               = "QuantEmbrace-${var.environment}-DataIngestionInstanceRole"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
  description        = "EC2 instance role for data-ingestion service (NSE + US)"

  tags = {
    Environment = var.environment
    Service     = "data-ingestion"
    ManagedBy   = "terraform"
  }
}

resource "aws_iam_policy" "data_ingestion" {
  name        = "QuantEmbrace-${var.environment}-DataIngestionPolicy"
  description = "Least-privilege policy for data-ingestion EC2 instances"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3: write tick data and OHLCV data
      {
        Sid    = "S3WriteTickData"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject"]
        Resource = [
          "arn:aws:s3:::${var.s3_bucket_tick_data}/*",
          "arn:aws:s3:::${var.s3_bucket_ohlcv_data}/*",
        ]
      },
      {
        Sid    = "S3ListBuckets"
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
        ]
        Resource = [
          "arn:aws:s3:::${var.s3_bucket_tick_data}",
          "arn:aws:s3:::${var.s3_bucket_ohlcv_data}",
        ]
      },
      # DynamoDB: write latest prices, read instruments
      {
        Sid    = "DynamoDBLatestPrices"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:GetItem",
          "dynamodb:BatchWriteItem",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-latest-prices",
        ]
      },
      # DynamoDB: write candle-cache (Phase 3)
      # IntradayCandleStream writes a row per confirmed OHLCV candle (TTL 2h).
      # strategy_engine reads this table via DynamoCandleConsumer every 500ms.
      {
        Sid    = "DynamoDBCandleCacheWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:BatchWriteItem",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-candle-cache",
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-candle-cache/index/*",
        ]
      },
      # DynamoDB: write feature store (Phase 5)
      # FeatureWriter writes LATEST + CANDLE#{ts} rows per confirmed candle.
      # FeatureArchiver queries CANDLE# rows at POST_CLOSE for S3 parquet export.
      {
        Sid    = "DynamoDBFeaturesWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:BatchWriteItem",
          "dynamodb:Query",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-features",
        ]
      },
      # Secrets Manager: read broker credentials
      {
        Sid    = "SecretsReadBrokerCreds"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ]
        Resource = [var.secrets_zerodha_arn, var.secrets_alpaca_arn]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "data_ingestion_custom" {
  role       = aws_iam_role.data_ingestion.name
  policy_arn = aws_iam_policy.data_ingestion.arn
}

resource "aws_iam_role_policy_attachment" "data_ingestion_ssm" {
  role       = aws_iam_role.data_ingestion.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "data_ingestion_cw" {
  role       = aws_iam_role.data_ingestion.name
  policy_arn = aws_iam_policy.cloudwatch_agent.arn
}

resource "aws_iam_role_policy_attachment" "data_ingestion_kafka_bootstrap" {
  role       = aws_iam_role.data_ingestion.name
  policy_arn = aws_iam_policy.kafka_bootstrap_discovery.arn
}

resource "aws_iam_instance_profile" "data_ingestion" {
  name = "QuantEmbrace-${var.environment}-DataIngestion"
  role = aws_iam_role.data_ingestion.name
}

# ═══════════════════════════════════════════════════════════════
# STRATEGY ENGINE — Role, Policy, Profile
# ═══════════════════════════════════════════════════════════════

resource "aws_iam_role" "strategy_engine" {
  name               = "QuantEmbrace-${var.environment}-StrategyEngineInstanceRole"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
  description        = "EC2 instance role for strategy-engine service"

  tags = {
    Environment = var.environment
    Service     = "strategy-engine"
    ManagedBy   = "terraform"
  }
}

resource "aws_iam_policy" "strategy_engine" {
  name        = "QuantEmbrace-${var.environment}-StrategyEnginePolicy"
  description = "Least-privilege policy for strategy-engine EC2 instances"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # DynamoDB: read latest prices, read/write strategy state
      {
        Sid    = "DynamoDBStrategyRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:BatchGetItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-latest-prices",
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-positions",
        ]
      },
      {
        Sid    = "DynamoDBStrategyState"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-strategy-state",
        ]
      },
      # DynamoDB: read candle-cache (Phase 3)
      # DynamoCandleConsumer polls via Query on candle-open-time-index every 500ms.
      # Scan remains only as a migration/local fallback; production steady-state is Query.
      {
        Sid    = "DynamoDBCandleCacheRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:BatchGetItem",
          "dynamodb:Scan",
          "dynamodb:Query",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-candle-cache",
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-candle-cache/index/*",
        ]
      },
      # DynamoDB: read/write strategy-config (Phase 3)
      # StrategyConfigLoader reads every 60s; also writes circuit_breaker_reset=False
      # after applying a manual reset (UpdateItem only on that attribute).
      {
        Sid    = "DynamoDBStrategyConfig"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:BatchGetItem",
          "dynamodb:Query",
          "dynamodb:UpdateItem",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-strategy-config",
        ]
      },
      # DynamoDB: read risk-state (kill switch fast-path)
      # strategy_engine checks KILLSWITCH#GLOBAL every ~1s.
      {
        Sid    = "DynamoDBKillSwitchRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-risk-state",
        ]
      },
      # DynamoDB: read feature store (Phase 5)
      # FeatureReader performs GetItem on SK=LATEST for pre-trade signal enrichment.
      {
        Sid    = "DynamoDBFeaturesRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-features",
        ]
      },
      # S3: read model artifacts and strategy configs
      {
        Sid    = "S3ReadModels"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::${var.s3_bucket_model_artifacts}",
          "arn:aws:s3:::${var.s3_bucket_model_artifacts}/*",
        ]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "strategy_engine_custom" {
  role       = aws_iam_role.strategy_engine.name
  policy_arn = aws_iam_policy.strategy_engine.arn
}

resource "aws_iam_role_policy_attachment" "strategy_engine_ssm" {
  role       = aws_iam_role.strategy_engine.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "strategy_engine_cw" {
  role       = aws_iam_role.strategy_engine.name
  policy_arn = aws_iam_policy.cloudwatch_agent.arn
}

resource "aws_iam_role_policy_attachment" "strategy_engine_kafka_bootstrap" {
  role       = aws_iam_role.strategy_engine.name
  policy_arn = aws_iam_policy.kafka_bootstrap_discovery.arn
}

resource "aws_iam_instance_profile" "strategy_engine" {
  name = "QuantEmbrace-${var.environment}-StrategyEngine"
  role = aws_iam_role.strategy_engine.name
}

# ═══════════════════════════════════════════════════════════════
# EXECUTION ENGINE — Role, Policy, Profile
# ═══════════════════════════════════════════════════════════════

resource "aws_iam_role" "execution_engine" {
  name               = "QuantEmbrace-${var.environment}-ExecutionEngineInstanceRole"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
  description        = "EC2 instance role for execution-engine service"

  tags = {
    Environment = var.environment
    Service     = "execution-engine"
    ManagedBy   = "terraform"
  }
}

resource "aws_iam_policy" "execution_engine" {
  name        = "QuantEmbrace-${var.environment}-ExecutionEnginePolicy"
  description = "Least-privilege policy for execution-engine EC2 instances"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # DynamoDB: read/write orders, read positions and risk state
      {
        Sid    = "DynamoDBOrders"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:ConditionCheckItem",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-orders",
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-orders/index/*",
        ]
      },
      {
        Sid    = "DynamoDBPositionsRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-positions",
        ]
      },
      {
        Sid    = "DynamoDBSessionsRead"
        Effect = "Allow"
        Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-sessions",
        ]
      },
      # S3: write execution logs
      {
        Sid    = "S3WriteExecutionLogs"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
        ]
        Resource = [
          "arn:aws:s3:::${var.s3_bucket_trading_logs}/*",
        ]
      },
      # Secrets Manager: read broker credentials
      {
        Sid    = "SecretsReadBrokerCreds"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ]
        Resource = [var.secrets_zerodha_arn, var.secrets_alpaca_arn]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "execution_engine_custom" {
  role       = aws_iam_role.execution_engine.name
  policy_arn = aws_iam_policy.execution_engine.arn
}

resource "aws_iam_role_policy_attachment" "execution_engine_ssm" {
  role       = aws_iam_role.execution_engine.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "execution_engine_cw" {
  role       = aws_iam_role.execution_engine.name
  policy_arn = aws_iam_policy.cloudwatch_agent.arn
}

resource "aws_iam_role_policy_attachment" "execution_engine_kafka_bootstrap" {
  role       = aws_iam_role.execution_engine.name
  policy_arn = aws_iam_policy.kafka_bootstrap_discovery.arn
}

resource "aws_iam_instance_profile" "execution_engine" {
  name = "QuantEmbrace-${var.environment}-ExecutionEngine"
  role = aws_iam_role.execution_engine.name
}

# ═══════════════════════════════════════════════════════════════
# RISK ENGINE — Role, Policy, Profile
# Added Phase 2: risk-engine was missing from Phase 1 IAM setup.
# ═══════════════════════════════════════════════════════════════

resource "aws_iam_role" "risk_engine" {
  name               = "QuantEmbrace-${var.environment}-RiskEngineInstanceRole"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
  description        = "EC2 instance role for risk-engine service"

  tags = {
    Environment = var.environment
    Service     = "risk-engine"
    ManagedBy   = "terraform"
  }
}

resource "aws_iam_policy" "risk_engine" {
  name        = "QuantEmbrace-${var.environment}-RiskEnginePolicy"
  description = "Least-privilege policy for risk-engine EC2 instances"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # DynamoDB: read/write risk state (kill switch, NAV, risk-decision idempotency,
      #           reconciliation flag — all stored in risk-state table)
      {
        Sid    = "DynamoDBRiskState"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:ConditionCheckItem",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-risk-state",
        ]
      },
      {
        Sid    = "DynamoDBPositionsRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:UpdateItem",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-positions",
        ]
      },
      {
        Sid    = "DynamoDBSessionsKillSwitch"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-sessions",
        ]
      },
      # DynamoDB: read feature store (Phase 5)
      # FeatureReader performs GetItem on SK=LATEST for pre-signal risk validation.
      {
        Sid    = "DynamoDBFeaturesRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-features",
        ]
      },
      # S3: write risk-decision audit logs (every approval and rejection)
      {
        Sid    = "S3WriteRiskAuditLogs"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
        ]
        Resource = [
          "arn:aws:s3:::${var.s3_bucket_trading_logs}/risk-decisions/*",
        ]
      },
      # SNS: publish kill switch events and P0 alerts
      {
        Sid    = "SNSPublishKillSwitch"
        Effect = "Allow"
        Action = [
          "sns:Publish",
        ]
        Resource = [var.sns_kill_switch_topic_arn, var.sns_alerts_topic_arn]
      },
      # CloudWatch: publish risk metrics (daily P&L, exposure, rejection rate)
      {
        Sid    = "CloudWatchPutMetrics"
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData",
        ]
        Resource = [
          "*",
        ]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "risk_engine_custom" {
  role       = aws_iam_role.risk_engine.name
  policy_arn = aws_iam_policy.risk_engine.arn
}

resource "aws_iam_role_policy_attachment" "risk_engine_ssm" {
  role       = aws_iam_role.risk_engine.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "risk_engine_cw" {
  role       = aws_iam_role.risk_engine.name
  policy_arn = aws_iam_policy.cloudwatch_agent.arn
}

resource "aws_iam_role_policy_attachment" "risk_engine_kafka_bootstrap" {
  role       = aws_iam_role.risk_engine.name
  policy_arn = aws_iam_policy.kafka_bootstrap_discovery.arn
}

resource "aws_iam_instance_profile" "risk_engine" {
  name = "QuantEmbrace-${var.environment}-RiskEngine"
  role = aws_iam_role.risk_engine.name
}

# ═══════════════════════════════════════════════════════════════
# AI ENGINE — Role, Policy, Profile  (Phase 6)
#
# ai_engine reads signals.pending, enriches with regime + quality,
# publishes to signals.enriched. Kafka IAM is handled by the
# kafka module (aws_iam_policy.kafka_ai_engine attachment).
# This profile covers DynamoDB, S3, and CloudWatch access only.
# ═══════════════════════════════════════════════════════════════

resource "aws_iam_role" "ai_engine" {
  name               = "QuantEmbrace-${var.environment}-AIEngineInstanceRole"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
  description        = "EC2 instance role for ai-engine service (Phase 6)"

  tags = {
    Environment = var.environment
    Service     = "ai-engine"
    ManagedBy   = "terraform"
    Phase       = "6-ml-agentic"
  }
}

resource "aws_iam_policy" "ai_engine" {
  name        = "QuantEmbrace-${var.environment}-AIEnginePolicy"
  description = "Least-privilege policy for ai-engine EC2 instances (Phase 6)"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3: read ML model artifacts (regime classifier, quality scorer)
      # ModelRegistry downloads model.joblib from s3://<bucket>/models/<name>/<version>/
      {
        Sid    = "S3ReadModelArtifacts"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::${var.s3_bucket_model_artifacts}",
          "arn:aws:s3:::${var.s3_bucket_model_artifacts}/models/*",
        ]
      },
      # DynamoDB: read feature store (online features for enrichment)
      # FeatureReader performs GetItem on SK=LATEST ~every signal.
      {
        Sid    = "DynamoDBFeaturesRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:BatchGetItem",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-features",
        ]
      },
      # DynamoDB: read strategy-config (quality thresholds, enrichment_required flag,
      # hot-reload model version pointers at 60s interval)
      {
        Sid    = "DynamoDBStrategyConfigRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:BatchGetItem",
          "dynamodb:Query",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-strategy-config",
        ]
      },
      # DynamoDB: write regime-log (PK=REGIME#{market}#{symbol}, TTL 30d)
      # Written non-blocking via asyncio.create_task from SignalEnricher.
      {
        Sid    = "DynamoDBRegimeLogWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-regime-log",
        ]
      },
      # DynamoDB: write strategy-recommendations (StrategySelector post-market)
      {
        Sid    = "DynamoDBStrategyRecommendationsWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:Query",
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.dynamodb_table_prefix}-strategy-recommendations",
        ]
      },
      # CloudWatch: publish enrichment-path metrics
      # - QuantEmbrace/AIEngine/EnrichmentLatencyMs
      # - QuantEmbrace/AIEngine/RegimeClassificationErrors
      # - QuantEmbrace/AIEngine/QualityFilterRate
      # - QuantEmbrace/AIEngine/SignalsEnrichedCount
      # - QuantEmbrace/AIEngine/ModelHotReloadCount
      {
        Sid    = "CloudWatchPutMetrics"
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData",
        ]
        Resource = ["*"]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ai_engine_custom" {
  role       = aws_iam_role.ai_engine.name
  policy_arn = aws_iam_policy.ai_engine.arn
}

resource "aws_iam_role_policy_attachment" "ai_engine_ssm" {
  role       = aws_iam_role.ai_engine.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "ai_engine_cw" {
  role       = aws_iam_role.ai_engine.name
  policy_arn = aws_iam_policy.cloudwatch_agent.arn
}

resource "aws_iam_role_policy_attachment" "ai_engine_kafka_bootstrap" {
  role       = aws_iam_role.ai_engine.name
  policy_arn = aws_iam_policy.kafka_bootstrap_discovery.arn
}

# Optional: Secrets Manager access for Anthropic API key (StrategySelector agent)
# Only attached when secrets_anthropic_arn is provided.
resource "aws_iam_policy" "ai_engine_secrets" {
  count       = var.secrets_anthropic_arn != "" ? 1 : 0
  name        = "QuantEmbrace-${var.environment}-AIEngineSecretsPolicy"
  description = "Allows ai-engine to read Anthropic API key for StrategySelector"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SecretsReadAnthropicKey"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ]
        Resource = [var.secrets_anthropic_arn]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ai_engine_secrets" {
  count      = var.secrets_anthropic_arn != "" ? 1 : 0
  role       = aws_iam_role.ai_engine.name
  policy_arn = aws_iam_policy.ai_engine_secrets[0].arn
}

resource "aws_iam_instance_profile" "ai_engine" {
  name = "QuantEmbrace-${var.environment}-AIEngine"
  role = aws_iam_role.ai_engine.name
}
