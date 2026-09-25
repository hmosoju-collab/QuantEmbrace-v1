###############################################################################
# QuantEmbrace — DynamoDB Backtest Module
# Three backtest-only tables: run registry, checkpoint state, dataset registry.
# ALL names are qe-bt-* — the live-table guard in run_registry.py enforces this
# at the application layer too. Shared live modules are untouched.
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
  common_tags = merge(var.tags, { Module = "dynamodb_backtest" })
}

# ---------------------------------------------------------------------------
# qe-bt-runs — run registry (one item per run)
#   PK: run_id  (e.g. "bt_a1b2c3d4e5f6g7h8")
#   Fields: see run_registry.RUN_FIELDS
#   PITR: enabled (audit / reproducibility)
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "runs" {
  name         = "qe-bt-runs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"

  attribute {
    name = "run_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "updated_at"
    type = "S"
  }

  attribute {
    name = "strategy"
    type = "S"
  }

  attribute {
    name = "start_date"
    type = "S"
  }

  attribute {
    name = "config_hash"
    type = "S"
  }

  # GSI: list runs by status + recency (dashboard / monitoring)
  global_secondary_index {
    name            = "status-index"
    hash_key        = "status"
    range_key       = "updated_at"
    projection_type = "ALL"
  }

  # GSI: list runs by strategy + date (strategy-specific history)
  global_secondary_index {
    name            = "strategy-index"
    hash_key        = "strategy"
    range_key       = "start_date"
    projection_type = "ALL"
  }

  # GSI: idempotent lookup by config hash (same config => same run_id)
  global_secondary_index {
    name            = "config-hash-index"
    hash_key        = "config_hash"
    projection_type = "KEYS_ONLY"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name    = "qe-bt-runs"
    Purpose = "Backtest run registry - lifecycle/lineage/S3 output pointers"
  })
}

# ---------------------------------------------------------------------------
# qe-bt-checkpoints — per-shard resume state (composite key)
#   PK: run_id  SK: partition_id  (e.g. "RELIANCE#2022")
#   SK="#RUN" is the run-level meta item (resumable_command, retry_count).
#   Each shard writes independently — no hot-key contention under fleet workers.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "checkpoints" {
  name         = "qe-bt-checkpoints"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"
  range_key    = "partition_id"

  attribute {
    name = "run_id"
    type = "S"
  }

  attribute {
    name = "partition_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = false # ephemeral resume state; PITR not needed
  }

  tags = merge(local.common_tags, {
    Name    = "qe-bt-checkpoints"
    Purpose = "Per-shard checkpoint and resume cursors for resumable backtests"
  })
}

# ---------------------------------------------------------------------------
# qe-bt-datasets — training dataset registry
#   PK: dataset_id
#   Links source run_ids, schema version, split boundaries, S3 prefix.
#   Detail in model-dataset-spec.md.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "datasets" {
  name         = "qe-bt-datasets"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "dataset_id"

  attribute {
    name = "dataset_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  # GSI: list datasets by creation time (Phase 9 dataset browser)
  global_secondary_index {
    name            = "created-at-index"
    hash_key        = "dataset_id"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name    = "qe-bt-datasets"
    Purpose = "Model training dataset registry - links run outputs to training splits"
  })
}
