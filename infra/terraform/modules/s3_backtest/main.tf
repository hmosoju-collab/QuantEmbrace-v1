###############################################################################
# QuantEmbrace — S3 Backtest Module
# Two canonical backtest buckets.  Names use "backtest" to satisfy the
# _BACKTEST_MARKERS guard and are clearly disjoint from live bucket names.
# Shared live modules (s3/) are untouched.
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
  common_tags = merge(var.tags, { Module = "s3_backtest" })
}

# quantembrace-backtest-data  — raw drops, quarantine zone, curated lake, reference data
resource "aws_s3_bucket" "data" {
  bucket        = "quantembrace-backtest-data"
  force_destroy = false

  tags = merge(local.common_tags, {
    Name    = "quantembrace-backtest-data"
    Purpose = "Backtest data lake: raw/quarantine/lake-ohlcv/reference"
  })
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# lake/ and reference/ data: Standard → IA 30d → Glacier IR 365d
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    id     = "lake-tiering"
    status = "Enabled"
    filter {
      prefix = "lake/"
    }
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 365
      storage_class = "GLACIER_IR"
    }
  }

  rule {
    id     = "reference-tiering"
    status = "Enabled"
    filter {
      prefix = "reference/"
    }
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 365
      storage_class = "GLACIER_IR"
    }
  }

  rule {
    id     = "raw-tiering"
    status = "Enabled"
    filter {
      prefix = "raw/"
    }
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 365
      storage_class = "GLACIER_IR"
    }
  }
}

# quantembrace-backtest-results — run outputs, walk-forward studies, datasets, reports
resource "aws_s3_bucket" "results" {
  bucket        = "quantembrace-backtest-results"
  force_destroy = false

  tags = merge(local.common_tags, {
    Name    = "quantembrace-backtest-results"
    Purpose = "Backtest run outputs: trades/equity-curves/reports/datasets/walk-forward"
  })
}

resource "aws_s3_bucket_versioning" "results" {
  bucket = aws_s3_bucket.results.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "results" {
  bucket = aws_s3_bucket.results.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "results" {
  bucket                  = aws_s3_bucket.results.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# results are never auto-deleted (audit trail); no lifecycle expiry
resource "aws_s3_bucket_lifecycle_configuration" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    id     = "results-tiering"
    status = "Enabled"
    filter {
      prefix = "runs/"
    }
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
  }

  rule {
    id     = "datasets-tiering"
    status = "Enabled"
    filter {
      prefix = "datasets/"
    }
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
  }
}
