# QuantEmbrace - Production Environment
# ========================================
# High-availability configuration with provisioned capacity,
# HA NAT gateways, and no Spot for critical trading services.

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

  environment        = "prod"
  vpc_cidr           = "10.1.0.0/16"
  availability_zones = ["ap-south-1a", "ap-south-1b"]
  single_nat_gateway = false  # HA: NAT per AZ in production
}

# --- ECS Cluster & Services ---
module "ecs" {
  source = "../../modules/ecs"

  environment           = "prod"
  vpc_id                = module.vpc.vpc_id
  private_subnet_ids    = module.vpc.private_subnet_ids
  # Module expects a single security group ID (the ECS task SG).
  # The VPC module exposes ecs_security_group_id for exactly this purpose.
  ecs_security_group_id = module.vpc.ecs_security_group_id

  enable_container_insights = true
  log_retention_days        = 30

  # Module input is service_configs, not services.
  service_configs = {
    data_ingestion = {
      cpu           = 512
      memory        = 1024
      desired_count = 2      # Redundancy for market data
      use_spot      = false  # Must not lose WebSocket connections
    }
    strategy_engine = {
      cpu           = 512
      memory        = 1024
      desired_count = 2
      use_spot      = false  # Strategies need consistent compute
    }
    execution_engine = {
      cpu           = 512
      memory        = 1024
      desired_count = 2
      use_spot      = false  # CRITICAL: never Spot for order execution
    }
    risk_engine = {
      cpu           = 512
      memory        = 1024
      desired_count = 2
      use_spot      = false  # CRITICAL: never Spot for risk validation
    }
    ai_engine = {
      cpu           = 1024
      memory        = 2048
      desired_count = 2
      use_spot      = true   # AI inference is non-critical, can tolerate interruption
    }
  }
}

# --- S3 Buckets ---
module "s3" {
  source = "../../modules/s3"

  environment             = "prod"
  glacier_transition_days = 365
  enable_versioning       = true
}

# --- DynamoDB Tables ---
module "dynamodb" {
  source = "../../modules/dynamodb"

  environment              = "prod"
  use_provisioned_capacity = true   # Provisioned + auto-scaling for known prod workloads
  autoscaling_max_capacity = 100
}

# --- Monitoring ---
module "monitoring" {
  source = "../../modules/monitoring"

  environment        = "prod"
  ecs_cluster_name   = module.ecs.cluster_name
  log_retention_days = 30  # 30 days hot, archived to S3 after
  alert_email        = var.alert_email
}
