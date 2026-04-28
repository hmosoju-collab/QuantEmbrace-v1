###############################################################################
# QuantEmbrace — DynamoDB Module
# Tables for order state, positions, latest prices, risk state, and strategy
# state. Supports on-demand billing (dev) or provisioned with auto-scaling
# (prod). Point-in-time recovery enabled on all tables.
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
  common_tags = merge(var.tags, {
    Module = "dynamodb"
  })

  table_prefix = "${var.project}-${var.environment}"
  billing_mode = var.use_provisioned_capacity ? "PROVISIONED" : "PAY_PER_REQUEST"

  # Default provisioned throughput (only used when billing_mode = PROVISIONED)
  default_read_capacity  = 5
  default_write_capacity = 5
}

# ---------------------------------------------------------------------------
# Orders Table
#   PK:  PK  (S) — "ORDER#{order_id}"   (single-table design composite key)
#   SK:  SK  (S) — "META"
#
# GSIs
#   signal-index  : PK=signal_id        — dedup lookup in execute_approved_signal()
#   status-index  : PK=order_status, SK=created_at
#                                        — open-order reconciliation on startup
#   account-index : PK=account_id, SK=created_at
#                                        — per-account order history queries
#
# Python attribute naming note
#   The application stores status as `order_status` (not `status`) to avoid
#   collision with the reserved DynamoDB keyword.  The GSI key name mirrors this.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "orders" {
  name         = "${local.table_prefix}-orders"
  billing_mode = local.billing_mode
  hash_key     = "PK"
  range_key    = "SK"

  read_capacity  = var.use_provisioned_capacity ? local.default_read_capacity : null
  write_capacity = var.use_provisioned_capacity ? local.default_write_capacity : null

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  attribute {
    name = "signal_id"
    type = "S"
  }

  attribute {
    name = "order_status"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  attribute {
    name = "account_id"
    type = "S"
  }

  # GSI: Idempotent dedup — look up existing order for a signal_id without scan
  global_secondary_index {
    name            = "signal-index"
    hash_key        = "signal_id"
    projection_type = "ALL"

    read_capacity  = var.use_provisioned_capacity ? local.default_read_capacity : null
    write_capacity = var.use_provisioned_capacity ? local.default_write_capacity : null
  }

  # GSI: Startup reconciliation — fetch all open orders without scan
  global_secondary_index {
    name            = "status-index"
    hash_key        = "order_status"
    range_key       = "created_at"
    projection_type = "ALL"

    read_capacity  = var.use_provisioned_capacity ? local.default_read_capacity : null
    write_capacity = var.use_provisioned_capacity ? local.default_write_capacity : null
  }

  # GSI: Per-account order history
  global_secondary_index {
    name            = "account-index"
    hash_key        = "account_id"
    range_key       = "created_at"
    projection_type = "ALL"

    read_capacity  = var.use_provisioned_capacity ? local.default_read_capacity : null
    write_capacity = var.use_provisioned_capacity ? local.default_write_capacity : null
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name  = "${local.table_prefix}-orders"
    Table = "orders"
  })
}

# ---------------------------------------------------------------------------
# Positions Table — PK: account_id, SK: symbol
# Current open positions across all accounts and symbols
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "positions" {
  name         = "${local.table_prefix}-positions"
  billing_mode = local.billing_mode
  hash_key     = "account_id"
  range_key    = "symbol"

  read_capacity  = var.use_provisioned_capacity ? local.default_read_capacity : null
  write_capacity = var.use_provisioned_capacity ? local.default_write_capacity : null

  attribute {
    name = "account_id"
    type = "S"
  }

  attribute {
    name = "symbol"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name  = "${local.table_prefix}-positions"
    Table = "positions"
  })
}

# ---------------------------------------------------------------------------
# Latest Prices Table — PK: symbol
# Latest market prices with TTL for automatic expiry of stale data
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "latest_prices" {
  name         = "${local.table_prefix}-latest-prices"
  billing_mode = local.billing_mode
  hash_key     = "symbol"

  read_capacity  = var.use_provisioned_capacity ? local.default_read_capacity : null
  write_capacity = var.use_provisioned_capacity ? local.default_write_capacity : null

  attribute {
    name = "symbol"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false  # Ephemeral data, PITR not needed
  }

  tags = merge(local.common_tags, {
    Name  = "${local.table_prefix}-latest-prices"
    Table = "latest_prices"
  })
}

# ---------------------------------------------------------------------------
# Risk State Table — PK: account_id
# Daily P&L, exposure, kill switch state per account
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "risk_state" {
  name         = "${local.table_prefix}-risk-state"
  billing_mode = local.billing_mode
  hash_key     = "account_id"

  read_capacity  = var.use_provisioned_capacity ? local.default_read_capacity : null
  write_capacity = var.use_provisioned_capacity ? local.default_write_capacity : null

  attribute {
    name = "account_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name  = "${local.table_prefix}-risk-state"
    Table = "risk_state"
  })
}

# ---------------------------------------------------------------------------
# Strategy State Table — PK: strategy_name, SK: symbol
# Working state for each strategy-symbol pair
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "strategy_state" {
  name         = "${local.table_prefix}-strategy-state"
  billing_mode = local.billing_mode
  hash_key     = "strategy_name"
  range_key    = "symbol"

  read_capacity  = var.use_provisioned_capacity ? local.default_read_capacity : null
  write_capacity = var.use_provisioned_capacity ? local.default_write_capacity : null

  attribute {
    name = "strategy_name"
    type = "S"
  }

  attribute {
    name = "symbol"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name  = "${local.table_prefix}-strategy-state"
    Table = "strategy_state"
  })
}

# ---------------------------------------------------------------------------
# Auto Scaling (only when using provisioned capacity — prod)
# ---------------------------------------------------------------------------

resource "aws_appautoscaling_target" "orders_read" {
  count              = var.use_provisioned_capacity ? 1 : 0
  max_capacity       = var.autoscaling_max_capacity
  min_capacity       = local.default_read_capacity
  resource_id        = "table/${aws_dynamodb_table.orders.name}"
  scalable_dimension = "dynamodb:table:ReadCapacityUnits"
  service_namespace  = "dynamodb"
}

resource "aws_appautoscaling_policy" "orders_read" {
  count              = var.use_provisioned_capacity ? 1 : 0
  name               = "${local.table_prefix}-orders-read-autoscale"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.orders_read[0].resource_id
  scalable_dimension = aws_appautoscaling_target.orders_read[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.orders_read[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "DynamoDBReadCapacityUtilization"
    }
    target_value = 70.0
  }
}

resource "aws_appautoscaling_target" "orders_write" {
  count              = var.use_provisioned_capacity ? 1 : 0
  max_capacity       = var.autoscaling_max_capacity
  min_capacity       = local.default_write_capacity
  resource_id        = "table/${aws_dynamodb_table.orders.name}"
  scalable_dimension = "dynamodb:table:WriteCapacityUnits"
  service_namespace  = "dynamodb"
}

resource "aws_appautoscaling_policy" "orders_write" {
  count              = var.use_provisioned_capacity ? 1 : 0
  name               = "${local.table_prefix}-orders-write-autoscale"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.orders_write[0].resource_id
  scalable_dimension = aws_appautoscaling_target.orders_write[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.orders_write[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "DynamoDBWriteCapacityUtilization"
    }
    target_value = 70.0
  }
}

resource "aws_appautoscaling_target" "positions_read" {
  count              = var.use_provisioned_capacity ? 1 : 0
  max_capacity       = var.autoscaling_max_capacity
  min_capacity       = local.default_read_capacity
  resource_id        = "table/${aws_dynamodb_table.positions.name}"
  scalable_dimension = "dynamodb:table:ReadCapacityUnits"
  service_namespace  = "dynamodb"
}

resource "aws_appautoscaling_policy" "positions_read" {
  count              = var.use_provisioned_capacity ? 1 : 0
  name               = "${local.table_prefix}-positions-read-autoscale"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.positions_read[0].resource_id
  scalable_dimension = aws_appautoscaling_target.positions_read[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.positions_read[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "DynamoDBReadCapacityUtilization"
    }
    target_value = 70.0
  }
}

resource "aws_appautoscaling_target" "positions_write" {
  count              = var.use_provisioned_capacity ? 1 : 0
  max_capacity       = var.autoscaling_max_capacity
  min_capacity       = local.default_write_capacity
  resource_id        = "table/${aws_dynamodb_table.positions.name}"
  scalable_dimension = "dynamodb:table:WriteCapacityUnits"
  service_namespace  = "dynamodb"
}

resource "aws_appautoscaling_policy" "positions_write" {
  count              = var.use_provisioned_capacity ? 1 : 0
  name               = "${local.table_prefix}-positions-write-autoscale"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.positions_write[0].resource_id
  scalable_dimension = aws_appautoscaling_target.positions_write[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.positions_write[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "DynamoDBWriteCapacityUtilization"
    }
    target_value = 70.0
  }
}

resource "aws_appautoscaling_target" "risk_state_read" {
  count              = var.use_provisioned_capacity ? 1 : 0
  max_capacity       = var.autoscaling_max_capacity
  min_capacity       = local.default_read_capacity
  resource_id        = "table/${aws_dynamodb_table.risk_state.name}"
  scalable_dimension = "dynamodb:table:ReadCapacityUnits"
  service_namespace  = "dynamodb"
}

resource "aws_appautoscaling_policy" "risk_state_read" {
  count              = var.use_provisioned_capacity ? 1 : 0
  name               = "${local.table_prefix}-risk-state-read-autoscale"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.risk_state_read[0].resource_id
  scalable_dimension = aws_appautoscaling_target.risk_state_read[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.risk_state_read[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "DynamoDBReadCapacityUtilization"
    }
    target_value = 70.0
  }
}

resource "aws_appautoscaling_target" "risk_state_write" {
  count              = var.use_provisioned_capacity ? 1 : 0
  max_capacity       = var.autoscaling_max_capacity
  min_capacity       = local.default_write_capacity
  resource_id        = "table/${aws_dynamodb_table.risk_state.name}"
  scalable_dimension = "dynamodb:table:WriteCapacityUnits"
  service_namespace  = "dynamodb"
}

resource "aws_appautoscaling_policy" "risk_state_write" {
  count              = var.use_provisioned_capacity ? 1 : 0
  name               = "${local.table_prefix}-risk-state-write-autoscale"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.risk_state_write[0].resource_id
  scalable_dimension = aws_appautoscaling_target.risk_state_write[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.risk_state_write[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "DynamoDBWriteCapacityUtilization"
    }
    target_value = 70.0
  }
}
