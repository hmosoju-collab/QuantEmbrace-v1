###############################################################################
# QuantEmbrace — Backtest Network Module
# Minimal VPC for the backtest worker: one public subnet, IGW.
# NO NAT gateway (saves ~$32/mo idle) — worker uses public egress.
# S3 + DynamoDB gateway endpoints (free) avoid internet data-transfer costs.
# Disjoint CIDR from live VPC (10.0.0.0/16) — no peering, no live access.
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

locals {
  common_tags = merge(var.tags, { Module = "network_backtest" })
}

resource "aws_vpc" "backtest" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.common_tags, { Name = "quantembrace-backtest-vpc" })
}

resource "aws_internet_gateway" "backtest" {
  vpc_id = aws_vpc.backtest.id
  tags   = merge(local.common_tags, { Name = "quantembrace-backtest-igw" })
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.backtest.id
  cidr_block              = var.public_subnet_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = true

  tags = merge(local.common_tags, { Name = "quantembrace-backtest-public" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.backtest.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.backtest.id
  }

  tags = merge(local.common_tags, { Name = "quantembrace-backtest-rt-public" })
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# Free S3 gateway endpoint — avoids internet data-transfer charges for lake reads
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.backtest.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]

  tags = merge(local.common_tags, { Name = "quantembrace-backtest-vpce-s3" })
}

# Free DynamoDB gateway endpoint — avoids internet charges for registry writes
resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.backtest.id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]

  tags = merge(local.common_tags, { Name = "quantembrace-backtest-vpce-dynamodb" })
}

# Egress-only security group — worker can reach internet (CloudWatch/SNS via public),
# but no inbound traffic allowed from outside.
resource "aws_security_group" "worker_egress" {
  name        = "quantembrace-backtest-worker-egress"
  description = "Backtest worker: outbound only, no inbound"
  vpc_id      = aws_vpc.backtest.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "All outbound (CloudWatch, SNS, S3, DynamoDB)"
  }

  tags = merge(local.common_tags, { Name = "quantembrace-backtest-worker-sg" })
}
