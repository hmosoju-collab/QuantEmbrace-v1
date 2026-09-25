###############################################################################
# IAM — Backtest Worker Role (least-privilege)
#
# Allowed: S3 backtest buckets (read data, write results), qe-bt-* DynamoDB,
#          CloudWatch metrics/logs, SNS publish.
# EXPLICIT DENY: live/paper S3 buckets, live DynamoDB tables, Secrets Manager
#                (no broker credentials ever accessible to a backtest worker).
###############################################################################

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

resource "aws_iam_role" "worker" {
  name               = "quantembrace-backtest-worker"
  description        = "Least-privilege role for the QuantEmbrace backtest EC2 worker"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json

  tags = merge(var.tags, { Module = "ec2_backtest_worker" })
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_instance_profile" "worker" {
  name = "quantembrace-backtest-worker"
  role = aws_iam_role.worker.name
}

# ── Allow policy ─────────────────────────────────────────────────────────────

resource "aws_iam_role_policy" "allow" {
  name   = "backtest-worker-allow"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.allow.json
}

data "aws_iam_policy_document" "allow" {
  # S3: read the data lake, write run outputs
  statement {
    sid    = "S3BacktestData"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:ListBucket",
    ]
    resources = [
      "arn:aws:s3:::quantembrace-backtest-data",
      "arn:aws:s3:::quantembrace-backtest-data/*",
    ]
  }

  statement {
    sid    = "S3BacktestResults"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      "arn:aws:s3:::quantembrace-backtest-results",
      "arn:aws:s3:::quantembrace-backtest-results/*",
    ]
  }

  # DynamoDB: backtest tables only (qe-bt-*)
  statement {
    sid    = "DynamoDBBacktest"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem",
    ]
    resources = [
      "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/qe-bt-runs",
      "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/qe-bt-runs/index/*",
      "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/qe-bt-checkpoints",
      "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/qe-bt-checkpoints/index/*",
      "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/qe-bt-datasets",
      "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/qe-bt-datasets/index/*",
    ]
  }

  # CloudWatch: emit backtest metrics and logs
  statement {
    sid    = "CloudWatchBacktest"
    effect = "Allow"
    actions = [
      "cloudwatch:PutMetricData",
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["QuantEmbrace/Backtest"]
    }
  }

  statement {
    sid       = "CloudWatchLogsBacktest"
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/quantembrace/backtest/*"]
  }

  # SNS: publish alerts to backtest topic only
  statement {
    sid     = "SNSBacktest"
    effect  = "Allow"
    actions = ["sns:Publish"]
    resources = [
      "arn:aws:sns:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:quantembrace-backtest-alerts",
    ]
  }

  # EC2 metadata (IMDSv2 only — needed for instance identity in worker userdata)
  statement {
    sid     = "IMDSv2"
    effect  = "Allow"
    actions = ["ec2:DescribeInstances"]
    resources = ["*"]
  }
}

# ── Explicit Deny policy (defence-in-depth) ───────────────────────────────────

resource "aws_iam_role_policy" "deny_live" {
  name   = "backtest-worker-deny-live"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.deny_live.json
}

data "aws_iam_policy_document" "deny_live" {
  # Hard-deny all Secrets Manager — no broker creds accessible to backtest workers
  statement {
    sid       = "DenySecretsManager"
    effect    = "Deny"
    actions   = ["secretsmanager:*"]
    resources = ["*"]
  }

  # Hard-deny live/paper DynamoDB tables (quantembrace-{dev,staging,prod}-*)
  statement {
    sid     = "DenyLiveDynamoDB"
    effect  = "Deny"
    actions = ["dynamodb:*"]
    resources = [
      "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/quantembrace-*",
    ]
  }

  # Hard-deny live/paper S3 buckets
  statement {
    sid     = "DenyLiveS3"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      "arn:aws:s3:::quantembrace-dev-*",
      "arn:aws:s3:::quantembrace-dev-*/*",
      "arn:aws:s3:::quantembrace-staging-*",
      "arn:aws:s3:::quantembrace-staging-*/*",
      "arn:aws:s3:::quantembrace-prod-*",
      "arn:aws:s3:::quantembrace-prod-*/*",
    ]
  }
}
