# QuantEmbrace - Dev Environment
# ================================
# Cost-optimized development configuration.
# Uses Fargate Spot, on-demand DynamoDB, single NAT gateway.

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

  environment       = "dev"
  vpc_cidr          = "10.0.0.0/16"
  availability_zones = ["ap-south-1a", "ap-south-1b"]
  single_nat_gateway = true  # Cost optimization: single NAT for dev
}

# --- ECS Cluster & Services ---
module "ecs" {
  source = "../../modules/ecs"

  environment           = "dev"
  vpc_id                = module.vpc.vpc_id
  private_subnet_ids    = module.vpc.private_subnet_ids
  ecs_security_group_id = module.vpc.ecs_security_group_id

  enable_container_insights = false
  log_retention_days        = 7

  service_configs = {
    data_ingestion = {
      cpu           = 256
      memory        = 512
      desired_count = 1
      use_spot      = true   # Non-critical in dev
    }
    strategy_engine = {
      cpu           = 256
      memory        = 512
      desired_count = 1
      use_spot      = true
    }
    execution_engine = {
      cpu           = 256
      memory        = 512
      desired_count = 1
      use_spot      = false  # Keep execution on standard Fargate
    }
    risk_engine = {
      cpu           = 256
      memory        = 512
      desired_count = 1
      use_spot      = false  # Risk engine must be reliable
    }
    ai_engine = {
      cpu           = 512
      memory        = 1024
      desired_count = 1
      use_spot      = true
    }
  }
}

# --- S3 Buckets ---
module "s3" {
  source = "../../modules/s3"

  environment         = "dev"
  glacier_transition_days = 90
  enable_versioning   = true
}

# --- DynamoDB Tables ---
module "dynamodb" {
  source = "../../modules/dynamodb"

  environment              = "dev"
  use_provisioned_capacity = false  # On-demand (PAY_PER_REQUEST) — cheapest for dev
}

# --- Monitoring ---
module "monitoring" {
  source = "../../modules/monitoring"

  environment        = "dev"
  ecs_cluster_name   = module.ecs.cluster_name
  log_retention_days = 14  # Shorter retention in dev
  alert_email        = var.alert_email
}
