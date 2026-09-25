###############################################################################
# QuantEmbrace — Backtest Environment
#
# BACKTEST-ONLY.  This Terraform environment is completely isolated from
# the live/paper environments.  State lives under a separate S3 key.
# NEVER run `terraform apply` against the live environment from here.
#
# Safety invariants:
#   • Backend key = "backtest/terraform.tfstate" (disjoint from live)
#   • All resource names carry "backtest" or "qe-bt-" prefix
#   • Worker IAM role has explicit Deny on live S3/DDB/Secrets Manager
#   • Never creates or modifies: quantembrace-dev-*, quantembrace-prod-*,
#     quantembrace-staging-*, or any live DynamoDB table
###############################################################################

terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    bucket         = "quantembrace-tf-state-343218182861"
    key            = "backtest/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "quantembrace-terraform-locks"
    encrypt        = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "QuantEmbrace"
      Environment = "backtest"
      ManagedBy   = "terraform"
      CostCenter  = "backtesting-lab"
    }
  }
}

locals {
  common_tags = {
    Project     = "QuantEmbrace"
    Environment = "backtest"
    ManagedBy   = "terraform"
    CostCenter  = "backtesting-lab"
  }
}

# ── Network ───────────────────────────────────────────────────────────────────

module "network" {
  source            = "../../modules/network_backtest"
  aws_region        = var.aws_region
  availability_zone = var.availability_zone
  tags              = local.common_tags
}

# ── DynamoDB Run Registry ─────────────────────────────────────────────────────

module "dynamodb" {
  source = "../../modules/dynamodb_backtest"
  tags   = local.common_tags
}

# ── S3 Data Lake & Results ────────────────────────────────────────────────────

module "s3" {
  source = "../../modules/s3_backtest"
  tags   = local.common_tags
}

# ── Monitoring (SNS + CloudWatch alarms + log group) ─────────────────────────

module "monitoring" {
  source             = "../../modules/monitoring_backtest"
  alert_email        = var.alert_email
  monthly_budget_usd = var.monthly_budget_usd
  tags               = local.common_tags
}

# ── EC2 Worker ASG + IAM ─────────────────────────────────────────────────────

module "ec2_worker" {
  source                = "../../modules/ec2_backtest_worker"
  aws_region            = var.aws_region
  subnet_id             = module.network.public_subnet_id
  security_group_id     = module.network.worker_security_group_id
  sns_alert_arn         = module.monitoring.sns_alert_arn
  primary_instance_type = var.primary_instance_type
  max_workers           = var.max_workers
  root_volume_gb        = var.root_volume_gb
  tags                  = local.common_tags
}
