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
#   signal-index       : PK=signal_id        — dedup lookup in execute_approved_signal()
#   status-index       : PK=order_status, SK=created_at
#                                             — open-order reconciliation on startup
#   account-index      : PK=account_id, SK=created_at
#                                             — per-account order history queries
#   symbol-status-index: PK=symbol, SK=order_status
#                                             — dirty-read race fix in PositionValidator:
#                                               sums in-flight (PENDING/PLACED/PARTIALLY_FILLED)
#                                               quantities per symbol before approving a signal
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

  attribute {
    name = "symbol"
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

  # GSI: In-flight position accounting — PositionValidator dirty-read fix
  # Queries all PENDING/PLACED/PARTIALLY_FILLED orders for a symbol in one
  # round-trip.  Projects only `quantity` (key attrs symbol+order_status are
  # always included). Read capacity is low: only hit on risk validation per
  # signal (not a high-throughput access pattern).
  global_secondary_index {
    name               = "symbol-status-index"
    hash_key           = "symbol"
    range_key          = "order_status"
    projection_type    = "INCLUDE"
    non_key_attributes = ["quantity"]

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
# Positions Table — canonical application key contract
#
#   PK: "POSITION#{symbol}" (S)
#   SK: "CURRENT"           (S)
#
# This matches services/shared/risk_state.py::position_key() and the LocalStack
# schema in scripts/setup_local_tables.py.  Risk checks, fill reconciliation, MIS
# square-off, and broker position audits all depend on this key shape.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "positions" {
  name         = "${local.table_prefix}-positions"
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

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name  = "${local.table_prefix}-positions"
    Table = "positions"
  })
}

# ---------------------------------------------------------------------------
# Latest Prices Table — canonical application key contract
#
#   PK: "QUOTE#{market}#{symbol}" (S)
#   SK: "LATEST"                  (S)
#
# This matches LiveQuotePoller writes and RiskContextBuilder reads.  The table is
# intentionally keyed by market+symbol so NSE/US symbols cannot collide.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "latest_prices" {
  name         = "${local.table_prefix}-latest-prices"
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

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false # Ephemeral data, PITR not needed
  }

  tags = merge(local.common_tags, {
    Name  = "${local.table_prefix}-latest-prices"
    Table = "latest_prices"
  })
}

# ---------------------------------------------------------------------------
# Risk State Table — canonical application key contract
#
#   PK/SK rows include:
#     KILLSWITCH / GLOBAL
#     NAV#CURRENT / STATE
#     RISK_DECISION#{signal_id} / DECISION
#       publish_status=PENDING until signals.approved is Kafka-acknowledged,
#       then PUBLISHED. Replays republish PENDING rows instead of suppressing.
#
# This matches services/shared/risk_state.py and the monitoring kill-switch
# Lambda.  The table is the durable safety state for kill switch propagation,
# NAV refresh, and risk-decision idempotency.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "risk_state" {
  name         = "${local.table_prefix}-risk-state"
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

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name  = "${local.table_prefix}-risk-state"
    Table = "risk_state"
  })
}

# ---------------------------------------------------------------------------
# Sessions Table — Zerodha daily access token store (ADR-022)
#
#   PK: "SESSION#{date}"  (S)   — one row per trading date
#   SK: "ZERODHA"         (S)   — broker discriminator
#
# ZerodhaTokenManager reads/writes this table after daily login
# (scripts/zerodha_login.py).  TTL expires old rows after 48 hours so the
# table never accumulates more than 2 rows under normal operation.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "sessions" {
  name         = "${local.table_prefix}-sessions"
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

  ttl {
    attribute_name = "expires_at_epoch"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name  = "${local.table_prefix}-sessions"
    Table = "sessions"
  })
}

# ---------------------------------------------------------------------------
# Candle Cache Table (Phase 3)
#
#   Written by data_ingestion/IntradayCandleStream on every confirmed candle.
#   Read by strategy_engine/DynamoCandleConsumer via Query every 500ms.
#
#   PK:  "{market}#{instrument}#{interval}#{candle_open_time_iso}"  (S)
#   Attributes (written by data_ingestion):
#     cache_bucket, market, instrument, interval, candle_open_time, captured_at
#     open, high, low, close (Decimal), volume (Number)
#     expires_at (Number, DynamoDB TTL epoch seconds)
#   TTL: 2 hours via expires_at (candles older than 2h are irrelevant and auto-deleted)
#
#   GSI candle-open-time-index:
#     PK=cache_bucket ("ACTIVE"), SK=candle_open_time.
#     Enables Query-based lookup in DynamoCandleConsumer, reducing read cost
#     from full-table scans to only the active 3-minute lookback window.
#     Projected: ALL (candle values needed, not just keys).
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "candle_cache" {
  name         = "${local.table_prefix}-candle-cache"
  billing_mode = local.billing_mode
  hash_key     = "PK"

  read_capacity  = var.use_provisioned_capacity ? local.default_read_capacity : null
  write_capacity = var.use_provisioned_capacity ? local.default_write_capacity : null

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "cache_bucket"
    type = "S"
  }

  attribute {
    name = "candle_open_time"
    type = "S"
  }

  # GSI: cache_bucket + candle_open_time — enables O(lookback_window) query
  global_secondary_index {
    name            = "candle-open-time-index"
    hash_key        = "cache_bucket"
    range_key       = "candle_open_time"
    projection_type = "ALL"

    read_capacity  = var.use_provisioned_capacity ? local.default_read_capacity : null
    write_capacity = var.use_provisioned_capacity ? local.default_write_capacity : null
  }

  # TTL: 2 hours. Candles older than this have been processed; auto-delete saves cost.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name    = "${local.table_prefix}-candle-cache"
    Table   = "candle_cache"
    Phase   = "3"
    Purpose = "OHLCV candle cache — written by data_ingestion, read by strategy_engine"
  })
}

# ---------------------------------------------------------------------------
# Strategy Config Table (Phase 3)
#
#   Written by operators (scripts/strategy/config.py) and the StrategyConfigLoader
#   (clears circuit_breaker_reset flag after applying).
#   Read by strategy_engine/StrategyConfigLoader every 60s for hot-reload.
#
#   PK:  "STRATEGY#{strategy_name}"  (S)
#   SK:  "CONFIG#{env}"              (S) — e.g. "CONFIG#production"
#   Attributes (ADR-013 §9.1):
#     enabled, paper_trade (Bool)
#     max_signals_per_day, circuit_breaker_threshold_consecutive,
#     circuit_breaker_threshold_rate (Number)
#     circuit_breaker_state (S — CLOSED/OPEN/HALF_OPEN, written by runner)
#     circuit_breaker_reset (Bool — operator sets True to force-reset)
#     updated_at, updated_by (S)
#
#   No TTL: config rows are permanent (operators delete/seed as needed).
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "strategy_config" {
  name         = "${local.table_prefix}-strategy-config"
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

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name    = "${local.table_prefix}-strategy-config"
    Table   = "strategy_config"
    Phase   = "3"
    Purpose = "Per-strategy hot-reload config — operators write, strategy_engine reads every 60s"
  })
}

# ---------------------------------------------------------------------------
# Strategy State Table
#
#   PK: strategy_name
#   SK: symbol
#
# Current service state is persisted as one aggregate row per strategy using
# symbol="__GLOBAL__". The range key remains available for future per-symbol
# state rows without another table migration.
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
# Signal Inbox Table (Phase 8 / ADR-015 F6)
#
# Written by risk_engine when a signal arrives from signals.pending and
# acknowledged before processing.  Used for deduplication and replay on
# risk_engine restart — ensures no signal is processed twice and no signal
# is silently dropped if the engine crashes mid-validation.
#
#   PK: "SIGNAL_INBOX#{signal_id}"  (S)
#   SK: "META"                       (S)
#
# Attributes (written on intake, updated on outcome):
#   signal_id, strategy_id, symbol, side, quantity  (from signal payload)
#   received_at, processed_at                       (ISO timestamps)
#   status: RECEIVED | PROCESSING | APPROVED | REJECTED | EXPIRED
#   risk_decision_id                                (set on APPROVED/REJECTED)
#   expires_at                                      (TTL epoch — 24h)
#
# TTL: 24 hours. Inbox rows older than this have been processed; auto-delete
# saves cost and keeps the table small.  The 24h window covers the full
# trading day plus overnight so recovery-replay is always available.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "signal_inbox" {
  name         = "${local.table_prefix}-signal-inbox"
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

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name    = "${local.table_prefix}-signal-inbox"
    Table   = "signal_inbox"
    Phase   = "8"
    Purpose = "risk_engine signal dedup and replay buffer (ADR-015 F6)"
  })
}

# ---------------------------------------------------------------------------
# Signal Outbox Table (Phase 8 / ADR-015 F6)
#
# Written by risk_engine after approving a signal.  The approved signal is
# first written to the outbox (status=PENDING_PUBLISH), then published to
# signals.approved Kafka topic.  On Kafka delivery confirmation the row is
# updated to status=PUBLISHED.  PENDING_PUBLISH rows on restart are
# re-published to Kafka, ensuring approved signals are not silently lost
# if the producer crashes before ACK.
#
#   PK: "SIGNAL_OUTBOX#{signal_id}"  (S)
#   SK: "META"                        (S)
#
# Attributes:
#   signal_id, risk_decision_id, approved_at
#   status: PENDING_PUBLISH | PUBLISHED | FAILED
#   publish_attempts                         (incremented on each retry)
#   published_at                             (set on PUBLISHED)
#   expires_at                               (TTL epoch — 48h for replay window)
#
# TTL: 48 hours.  Longer than inbox TTL so published signals are queryable
# for the next-day audit without hitting S3 audit logs.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "signal_outbox" {
  name         = "${local.table_prefix}-signal-outbox"
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
    name = "status"
    type = "S"
  }

  attribute {
    name = "approved_at"
    type = "S"
  }

  # GSI: recover un-published rows on restart without full scan
  global_secondary_index {
    name            = "status-approved-index"
    hash_key        = "status"
    range_key       = "approved_at"
    projection_type = "ALL"

    read_capacity  = var.use_provisioned_capacity ? local.default_read_capacity : null
    write_capacity = var.use_provisioned_capacity ? local.default_write_capacity : null
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name    = "${local.table_prefix}-signal-outbox"
    Table   = "signal_outbox"
    Phase   = "8"
    Purpose = "risk_engine approved-signal durable publish buffer (ADR-015 F6)"
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
