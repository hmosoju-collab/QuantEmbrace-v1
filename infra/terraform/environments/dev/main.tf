# QuantEmbrace - Dev Environment
# ================================
# Cost-optimized development configuration.
# EC2 backbone (Phase 1+), Kafka MSK Serverless (Phase 2).
# Uses scheduled scaling, minimal retentions, single NAT gateway.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket         = "quantembrace-terraform-state"
    key            = "dev/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "quantembrace-terraform-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "quantembrace"
      Environment = "dev"
      ManagedBy   = "terraform"
    }
  }
}

# Secondary provider for US region (Alpaca-related resources)
provider "aws" {
  alias  = "us"
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = "quantembrace"
      Environment = "dev"
      ManagedBy   = "terraform"
    }
  }
}

# --- VPC ---
module "vpc" {
  source = "../../modules/vpc"

  environment = "dev"
  vpc_cidr    = "10.0.0.0/16"
  ha_nat      = false # Single NAT for dev (cost optimisation)
}

# --- S3 Buckets ---
module "s3" {
  source = "../../modules/s3"

  environment = "dev"
}

# --- DynamoDB Tables ---
module "dynamodb" {
  source = "../../modules/dynamodb"

  environment              = "dev"
  use_provisioned_capacity = false # On-demand (PAY_PER_REQUEST) — cheapest for dev
}

# --- Monitoring ---
# Monitoring module — provisions SNS topics and CloudWatch alarms.
module "monitoring" {
  source = "../../modules/monitoring"

  environment        = "dev"
  log_retention_days = 14
  alert_email        = var.alert_email

  kill_switch_dynamodb_table = module.dynamodb.risk_state_table_name

  daily_cost_threshold_usd = 10 # Dev: tight cost alarm
}


# --- EC2 Services (Phase 1 backbone) ---
# Replaces ECS Fargate. Each service runs on ARM64 EC2 (t4g/c6g) with ASGs.
# Secrets Manager ARNs must be provisioned manually before first apply.
module "ec2_services" {
  source = "../../modules/ec2_services"

  environment        = "dev"
  aws_region         = var.aws_region
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  primary_subnet_id  = module.vpc.private_subnet_ids[0]
  ecr_account_id     = var.ecr_account_id

  dynamodb_table_prefix = "quantembrace-dev"

  s3_bucket_tick_data       = module.s3.tick_data_bucket_name
  s3_bucket_ohlcv_data      = module.s3.ohlcv_data_bucket_name
  s3_bucket_trading_logs    = module.s3.trading_logs_bucket_name
  s3_bucket_model_artifacts = module.s3.model_artifacts_bucket_name


  secrets_zerodha_arn = var.secrets_zerodha_arn
  secrets_alpaca_arn  = var.secrets_alpaca_arn

  sns_alerts_topic_arn      = module.monitoring.alerts_sns_topic_arn
  sns_kill_switch_topic_arn = module.monitoring.kill_switch_sns_topic_arn

  # Dev: smallest viable instances, aggressive scheduled scaling
  data_ingestion_instance_type   = "t4g.small"
  strategy_engine_instance_type  = "t4g.medium"
  execution_engine_instance_type = "t4g.medium"
  enable_scheduled_scaling       = true
  enable_warm_pools              = false # No warm pools in dev (cost)
  log_level                      = "DEBUG"
  cloudwatch_log_retention_days  = 7
}

# --- Kafka (Phase 2) ---
# MSK Serverless cluster + per-service IAM policies + CloudWatch alarms.
# Topics are NOT created here — run: python scripts/kafka/create_topics.py
#   --bootstrap-servers <BROKERS> --short-retention
module "kafka" {
  source = "../../modules/kafka"

  environment = "dev"
  aws_region  = var.aws_region
  vpc_id      = module.vpc.vpc_id

  private_subnet_ids             = module.vpc.private_subnet_ids
  ec2_services_security_group_id = module.ec2_services.security_group_id

  data_ingestion_role_name   = module.ec2_services.data_ingestion_role_name
  strategy_engine_role_name  = module.ec2_services.strategy_engine_role_name
  risk_engine_role_name      = module.ec2_services.risk_engine_role_name
  execution_engine_role_name = module.ec2_services.execution_engine_role_name

  # Dev: minimum viable retentions to keep MSK Serverless cost minimal
  retention_ms_ticks_nse        = 3600000   # 1h
  retention_ms_ticks_us         = 3600000   # 1h
  retention_ms_signals_pending  = 600000    # 10m
  retention_ms_signals_approved = 300000    # 5m
  retention_ms_orders_events    = 86400000  # 24h
  retention_ms_kill_switch      = 86400000  # 24h
  retention_ms_ops_audit        = 604800000 # 7d

  tags = {
    Environment = "dev"
    Phase       = "2"
  }
}
