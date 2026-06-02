# QuantEmbrace - Production Environment
# ========================================
# High-availability configuration with provisioned DynamoDB capacity,
# HA NAT gateways, warm pools, and no Spot for critical trading services.
# EC2 backbone (Phase 1+), Kafka MSK Serverless (Phase 2).

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
    key            = "prod/terraform.tfstate"
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
      Environment = "prod"
      ManagedBy   = "terraform"
    }
  }
}

provider "aws" {
  alias  = "us"
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = "quantembrace"
      Environment = "prod"
      ManagedBy   = "terraform"
    }
  }
}

# --- VPC ---
module "vpc" {
  source = "../../modules/vpc"

  environment = "prod"
  vpc_cidr    = "10.1.0.0/16"
  ha_nat      = true # HA: NAT per AZ in production
  # availability_zones resolved from data.aws_availability_zones in vpc module
}

# --- S3 Buckets ---
module "s3" {
  source = "../../modules/s3"

  environment = "prod"
  # glacier_transition_days and enable_versioning are hardcoded in the s3 module
  # (lifecycle tiers and versioning are not tunable per environment — see modules/s3/main.tf)
}

# --- DynamoDB Tables ---
module "dynamodb" {
  source = "../../modules/dynamodb"

  environment              = "prod"
  use_provisioned_capacity = true # Provisioned + auto-scaling for known prod workloads
  autoscaling_max_capacity = 100
}

# --- Monitoring ---
# Monitoring module — provisions SNS topics and CloudWatch alarms.
# kill_switch_dynamodb_table uses the dynamodb module's direct table-name output
# (not a prefix — the module outputs the full resolved table name).
module "monitoring" {
  source = "../../modules/monitoring"

  environment                 = "prod"
  log_retention_days          = 30 # 30 days hot, archived to S3 after
  alert_email                 = var.alert_email
  kill_switch_lambda_role_arn = var.kill_switch_lambda_role_arn
  kill_switch_dynamodb_table  = module.dynamodb.risk_state_table_name
}


# --- EC2 Services (Phase 1 backbone) ---
# Production uses c6g.xlarge for execution/risk (latency-critical).
# All critical services run on standard on-demand (never Spot).
# Warm pools enabled for fast failover on ASG replacements.
module "ec2_services" {
  source = "../../modules/ec2_services"

  environment        = "prod"
  aws_region         = var.aws_region
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  primary_subnet_id  = module.vpc.private_subnet_ids[0]
  ecr_account_id     = var.ecr_account_id

  dynamodb_table_prefix = "quantembrace-prod"

  s3_bucket_tick_data       = module.s3.tick_data_bucket_name
  s3_bucket_ohlcv_data      = module.s3.ohlcv_data_bucket_name
  s3_bucket_trading_logs    = module.s3.trading_logs_bucket_name
  s3_bucket_model_artifacts = module.s3.model_artifacts_bucket_name


  secrets_zerodha_arn = var.secrets_zerodha_arn
  secrets_alpaca_arn  = var.secrets_alpaca_arn

  strategy_watchlist_nse = var.strategy_watchlist_nse

  sns_alerts_topic_arn      = module.monitoring.alerts_sns_topic_arn
  sns_kill_switch_topic_arn = module.monitoring.kill_switch_sns_topic_arn

  # Production: right-sized instances, warm pools for failover
  data_ingestion_instance_type   = "t4g.medium"
  strategy_engine_instance_type  = "c6g.large"
  risk_engine_instance_type      = "c6g.xlarge" # Latency-critical: pre-trade risk path
  execution_engine_instance_type = "c6g.xlarge" # Latency-critical: larger instance
  strategy_engine_max_capacity   = 2            # Allow horizontal scale-out
  enable_scheduled_scaling       = true
  enable_warm_pools              = true # Fast failover in prod
  log_level                      = "INFO"
  cloudwatch_log_retention_days  = 30

  # SSH access disabled in prod — use SSM Session Manager
  key_pair_name                 = ""
  instance_metadata_http_tokens = "required" # IMDSv2 enforced
}

# --- Kafka (Phase 2) ---
# MSK Serverless cluster + per-service IAM policies + CloudWatch alarms.
# Topics are NOT created here — run: python scripts/kafka/create_topics.py
#   --bootstrap-servers <BROKERS>
# Prod uses the default retention values defined in kafka/variables.tf
# (approved durations from phase2_final_approved.md §2 — no overrides needed).
module "kafka" {
  source = "../../modules/kafka"

  environment = "prod"
  aws_region  = var.aws_region
  vpc_id      = module.vpc.vpc_id

  # Use at least 2 private subnets across AZs (MSK Serverless requirement)
  private_subnet_ids             = module.vpc.private_subnet_ids
  ec2_services_security_group_id = module.ec2_services.security_group_id

  # IAM role names from ec2_services module — Kafka policies are attached here
  data_ingestion_role_name   = module.ec2_services.data_ingestion_role_name
  strategy_engine_role_name  = module.ec2_services.strategy_engine_role_name
  risk_engine_role_name      = module.ec2_services.risk_engine_role_name
  execution_engine_role_name = module.ec2_services.execution_engine_role_name
  ops_admin_role_name        = var.ops_admin_role_name

  # Prod retentions: use module defaults (set in kafka/variables.tf)

  tags = {
    Environment = "prod"
    Phase       = "2"
  }
}
