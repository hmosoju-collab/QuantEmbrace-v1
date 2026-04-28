# QuantEmbrace - Staging Environment
# =====================================
# Mirrors production configuration at reduced scale.
# Used for pre-production validation with paper trading.

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
    key            = "staging/terraform.tfstate"
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
      Environment = "staging"
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
      Environment = "staging"
      ManagedBy   = "terraform"
    }
  }
}

# --- VPC ---
module "vpc" {
  source = "../../modules/vpc"

  environment        = "staging"
  vpc_cidr           = "10.2.0.0/16"
  availability_zones = ["ap-south-1a", "ap-south-1b"]
  single_nat_gateway = true  # Single NAT for staging (cost saving)
}

# --- ECS Cluster & Services ---
module "ecs" {
  source = "../../modules/ecs"

  environment           = "staging"
  vpc_id                = module.vpc.vpc_id
  private_subnet_ids    = module.vpc.private_subnet_ids
  ecs_security_group_id = module.vpc.ecs_security_group_id

  enable_container_insights = false
  log_retention_days        = 14

  service_configs = {
    data_ingestion = {
      cpu           = 256
      memory        = 512
      desired_count = 1
      use_spot      = true
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
      use_spot      = false  # Standard Fargate for execution testing
    }
    risk_engine = {
      cpu           = 256
      memory        = 512
      desired_count = 1
      use_spot      = false  # Standard Fargate for risk validation
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

  environment             = "staging"
  glacier_transition_days = 90
  enable_versioning       = true
}

# --- DynamoDB Tables ---
module "dynamodb" {
  source = "../../modules/dynamodb"

  environment              = "staging"
  use_provisioned_capacity = false  # On-demand for staging — mirrors PAY_PER_REQUEST
}

# --- Monitoring ---
module "monitoring" {
  source = "../../modules/monitoring"

  environment        = "staging"
  ecs_cluster_name   = module.ecs.cluster_name
  log_retention_days = 14
  alert_email        = var.alert_email
}
