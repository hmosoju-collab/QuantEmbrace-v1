###############################################################################
# QuantEmbrace — S3 Module
#
# Five purpose-built buckets with per-bucket lifecycle policies:
#
#   tick-data       Raw tick data (high volume, aggressive archival)
#   ohlcv-data      Daily/minute OHLCV bars (medium volume)
#   trading-logs    Audit/execution/risk-decision logs (compliance retention)
#   backtest-results Strategy backtest outputs (never auto-deleted)
#   model-artifacts  ML model artifacts (versioned, noncurrent expiry)
#
# Lifecycle cost rationale
# ────────────────────────
# tick-data:        30d Standard  →  90d Standard-IA  →  365d Glacier Instant
#                   →  Glacier Deep Archive (long-term compliance)
# ohlcv-data:       90d Standard  →  365d Standard-IA  →  Glacier Instant
# trading-logs:     90d Standard  →  365d Standard-IA  →  Glacier (compliance)
# backtest-results: 90d Standard  →  Standard-IA (no expiry — retained forever)
# model-artifacts:  versioned; noncurrent 90d→IA, 365d→Glacier, expire at 730d
#
# VPC endpoints for S3 (in vpc module) prevent NAT Gateway charges.
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
  common_tags = merge(var.tags, { Module = "s3" })
}

# ─────────────────────────────────────────────────────────────────────────────
# Helper: standard encryption + public-access-block used by every bucket
# ─────────────────────────────────────────────────────────────────────────────

# (Resources below all follow the same pattern — one section per bucket.)

# =============================================================================
# 1. tick-data — raw intraday tick history
# =============================================================================

resource "aws_s3_bucket" "tick_data" {
  bucket = "${var.project}-${var.environment}-tick-data"
  tags   = merge(local.common_tags, { Name = "${var.project}-${var.environment}-tick-data", Purpose = "Raw tick data partitioned by date/market/symbol" })
}

resource "aws_s3_bucket_versioning" "tick_data" {
  bucket = aws_s3_bucket.tick_data.id
  versioning_configuration { status = "Suspended" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tick_data" {
  bucket = aws_s3_bucket.tick_data.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "tick_data" {
  bucket                  = aws_s3_bucket.tick_data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "tick_data" {
  bucket = aws_s3_bucket.tick_data.id

  # Days 0–30:  S3 Standard (frequent access for recent backtesting)
  # Days 31–90: S3 Standard-IA (less frequent, lower cost)
  # Days 91–365: S3 Glacier Instant Retrieval (archival, fast retrieval for backtests)
  # Days 366+:  S3 Glacier Deep Archive (long-term compliance, slow retrieval)
  rule {
    id     = "tick-data-tiering"
    status = "Enabled"

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
    transition {
      days          = 365
      storage_class = "DEEP_ARCHIVE"
    }
  }
}

# =============================================================================
# 2. ohlcv-data — daily and minute OHLCV bars
# =============================================================================

resource "aws_s3_bucket" "ohlcv_data" {
  bucket = "${var.project}-${var.environment}-ohlcv-data"
  tags   = merge(local.common_tags, { Name = "${var.project}-${var.environment}-ohlcv-data", Purpose = "OHLCV bars (daily, hourly, minute) for all instruments" })
}

resource "aws_s3_bucket_versioning" "ohlcv_data" {
  bucket = aws_s3_bucket.ohlcv_data.id
  versioning_configuration { status = "Suspended" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ohlcv_data" {
  bucket = aws_s3_bucket.ohlcv_data.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "ohlcv_data" {
  bucket                  = aws_s3_bucket.ohlcv_data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "ohlcv_data" {
  bucket = aws_s3_bucket.ohlcv_data.id

  # Days 0–90:   S3 Standard
  # Days 91–365: S3 Standard-IA
  # Days 366+:   S3 Glacier Instant Retrieval
  rule {
    id     = "ohlcv-data-tiering"
    status = "Enabled"

    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 365
      storage_class = "GLACIER_IR"
    }
  }
}

# =============================================================================
# 3. trading-logs — audit, execution, and risk-decision logs
# =============================================================================

resource "aws_s3_bucket" "trading_logs" {
  bucket = "${var.project}-${var.environment}-trading-logs"
  tags   = merge(local.common_tags, { Name = "${var.project}-${var.environment}-trading-logs", Purpose = "Audit logs, execution records, risk decisions (compliance retention)" })
}

resource "aws_s3_bucket_versioning" "trading_logs" {
  bucket = aws_s3_bucket.trading_logs.id
  versioning_configuration { status = "Suspended" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "trading_logs" {
  bucket = aws_s3_bucket.trading_logs.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "trading_logs" {
  bucket                  = aws_s3_bucket.trading_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "trading_logs" {
  bucket = aws_s3_bucket.trading_logs.id

  # Days 0–90:   S3 Standard (hot for recent audit queries)
  # Days 91–365: S3 Standard-IA
  # Days 366+:   S3 Glacier Instant Retrieval (7-year compliance retention)
  rule {
    id     = "logs-tiering"
    status = "Enabled"

    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 365
      storage_class = "GLACIER_IR"
    }
  }
}

# =============================================================================
# 4. backtest-results — strategy backtest outputs (NEVER auto-deleted)
# =============================================================================

resource "aws_s3_bucket" "backtest_results" {
  bucket = "${var.project}-${var.environment}-backtest-results"
  tags   = merge(local.common_tags, { Name = "${var.project}-${var.environment}-backtest-results", Purpose = "Backtest reports, performance metrics, signal logs — retained indefinitely" })
}

resource "aws_s3_bucket_versioning" "backtest_results" {
  bucket = aws_s3_bucket.backtest_results.id
  # Versioning ON so older runs are never overwritten, only superseded
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backtest_results" {
  bucket = aws_s3_bucket.backtest_results.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "backtest_results" {
  bucket                  = aws_s3_bucket.backtest_results.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "backtest_results" {
  bucket = aws_s3_bucket.backtest_results.id

  # No automatic deletion — results are retained permanently.
  # Move to Standard-IA at 91 days (older runs accessed infrequently).
  # Noncurrent (overwritten) versions expire at 365 days.
  rule {
    id     = "backtest-results-ia"
    status = "Enabled"

    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }

    noncurrent_version_expiration {
      noncurrent_days = 365
    }
  }
}

# =============================================================================
# 5. model-artifacts — ML model binaries, feature definitions, training outputs
# =============================================================================

resource "aws_s3_bucket" "model_artifacts" {
  bucket = "${var.project}-${var.environment}-model-artifacts"
  tags   = merge(local.common_tags, { Name = "${var.project}-${var.environment}-model-artifacts", Purpose = "ML models, feature store definitions, training outputs" })
}

# Versioning ENABLED — model rollback requires previous versions
resource "aws_s3_bucket_versioning" "model_artifacts" {
  bucket = aws_s3_bucket.model_artifacts.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "model_artifacts" {
  bucket = aws_s3_bucket.model_artifacts.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "model_artifacts" {
  bucket                  = aws_s3_bucket.model_artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "model_artifacts" {
  bucket = aws_s3_bucket.model_artifacts.id

  rule {
    id     = "model-artifacts-noncurrent"
    status = "Enabled"

    noncurrent_version_transition {
      noncurrent_days = 90
      storage_class   = "STANDARD_IA"
    }
    noncurrent_version_transition {
      noncurrent_days = 365
      storage_class   = "GLACIER_IR"
    }
    noncurrent_version_expiration {
      noncurrent_days = 730
    }
  }
}
