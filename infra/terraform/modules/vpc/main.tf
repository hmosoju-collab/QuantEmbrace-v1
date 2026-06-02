###############################################################################
# QuantEmbrace — VPC Module
# Provides networking foundation: VPC, subnets, NAT, VPC endpoints, and
# security groups for EC2 ASG instances.
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

# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)

  common_tags = merge(var.tags, {
    Module = "vpc"
  })
}

# ---------------------------------------------------------------------------
# VPC
# ---------------------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-vpc"
  })
}

# ---------------------------------------------------------------------------
# Internet Gateway
# ---------------------------------------------------------------------------

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-igw"
  })
}

# ---------------------------------------------------------------------------
# Public Subnets
# ---------------------------------------------------------------------------

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-public-${local.azs[count.index]}"
    Tier = "public"
  })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-public-rt"
  })
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ---------------------------------------------------------------------------
# Private Subnets
# ---------------------------------------------------------------------------

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone = local.azs[count.index]

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-private-${local.azs[count.index]}"
    Tier = "private"
  })
}

# ---------------------------------------------------------------------------
# NAT Gateway — single for dev, one per AZ for prod (HA)
# ---------------------------------------------------------------------------

resource "aws_eip" "nat" {
  count  = var.ha_nat ? 2 : 1
  domain = "vpc"

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-nat-eip-${count.index}"
  })
}

resource "aws_nat_gateway" "main" {
  count         = var.ha_nat ? 2 : 1
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-nat-${count.index}"
  })

  depends_on = [aws_internet_gateway.main]
}

resource "aws_route_table" "private" {
  count  = var.ha_nat ? 2 : 1
  vpc_id = aws_vpc.main.id

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-private-rt-${count.index}"
  })
}

resource "aws_route" "private_nat" {
  count                  = var.ha_nat ? 2 : 1
  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.main[count.index].id
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[var.ha_nat ? count.index : 0].id
}

# ---------------------------------------------------------------------------
# VPC Endpoints — avoid NAT Gateway data transfer charges
# ---------------------------------------------------------------------------

# Gateway endpoint: S3 (free)
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"

  route_table_ids = concat(
    [aws_route_table.public.id],
    aws_route_table.private[*].id
  )

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-vpce-s3"
  })
}

# Gateway endpoint: DynamoDB (free)
resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.dynamodb"
  vpc_endpoint_type = "Gateway"

  route_table_ids = concat(
    [aws_route_table.public.id],
    aws_route_table.private[*].id
  )

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-vpce-dynamodb"
  })
}

# Interface endpoint: ECR API
resource "aws_vpc_endpoint" "ecr_api" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${data.aws_region.current.name}.ecr.api"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-vpce-ecr-api"
  })
}

# Interface endpoint: ECR Docker
resource "aws_vpc_endpoint" "ecr_dkr" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${data.aws_region.current.name}.ecr.dkr"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-vpce-ecr-dkr"
  })
}

# Interface endpoint: CloudWatch Logs
resource "aws_vpc_endpoint" "logs" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${data.aws_region.current.name}.logs"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-vpce-logs"
  })
}



# Interface endpoint: Secrets Manager
# Removes secrets retrieval from NAT path — also removes a startup dependency
# on internet connectivity when containers are initializing.
resource "aws_vpc_endpoint" "secretsmanager" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${data.aws_region.current.name}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-vpce-secretsmanager"
  })
}

# MSK (Kafka) — Interface endpoint.
# MSK Serverless uses port 9098 (SASL/OAUTHBEARER IAM).  Without this endpoint
# all broker traffic would egress through the NAT Gateway, adding ~$0.045/GB
# in NAT data-processing charges and routing Kafka traffic over the public
# internet (SASL_SSL is still encrypted, but VPC-private is always preferred).
#
# NOTE: The MSK Serverless VPC connectivity feature requires an additional
# vpc_id attachment on the MSK cluster itself.  That attachment is managed by
# the kafka module (aws_msk_serverless_cluster → vpc_config block) which
# creates its own private ENI.  This VPC endpoint is the client-side binding
# so EC2 instances resolve the MSK bootstrap hostname to private IPs.
resource "aws_vpc_endpoint" "msk" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${data.aws_region.current.name}.kafka-broker"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.msk_endpoint.id]

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-vpce-msk"
  })
}

# ---------------------------------------------------------------------------
# Security Groups
# ---------------------------------------------------------------------------

# SG for VPC endpoints
resource "aws_security_group" "vpc_endpoints" {
  name_prefix = "${var.project}-${var.environment}-vpce-"
  description = "Allow HTTPS from private subnets to VPC endpoints"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS from VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-vpce-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# SG for MSK VPC endpoint — allows port 9098 (SASL/OAUTHBEARER) from EC2 instances.
# Separate from the generic vpce SG (port 443) because MSK uses a non-standard port.
resource "aws_security_group" "msk_endpoint" {
  name_prefix = "${var.project}-${var.environment}-vpce-msk-"
  description = "Allow MSK Serverless broker port 9098 from EC2 instances"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "MSK Serverless SASL/OAUTHBEARER from EC2 ASG instances"
    from_port       = 9098
    to_port         = 9098
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_tasks.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-vpce-msk-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# SG for ECS tasks
resource "aws_security_group" "ecs_tasks" {
  name_prefix = "${var.project}-${var.environment}-ecs-tasks-"
  description = "Security group for EC2 ARM64 ASG instances"
  vpc_id      = aws_vpc.main.id

  # Allow all traffic between ECS tasks (inter-service communication)
  ingress {
    description = "Allow traffic from other ECS tasks"
    from_port   = 0
    to_port     = 65535
    protocol    = "tcp"
    self        = true
  }

  # Allow inbound on service ports from within VPC
  ingress {
    description = "HTTP from VPC"
    from_port   = 8000
    to_port     = 8099
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  # Allow all outbound (broker APIs, AWS services)
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.environment}-ecs-tasks-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}
