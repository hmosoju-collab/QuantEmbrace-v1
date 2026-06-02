#!/bin/bash
# ============================================================
# risk_engine.sh - UserData for risk-engine EC2 service
# Rendered by Terraform templatefile() - do not edit directly.
#
# CRITICAL SERVICE: This service approves/rejects all live signals and owns
# portfolio-level risk state. It must run before execution-engine promotion.
# ============================================================
set -euo pipefail

export AWS_REGION="${aws_region}"
export SERVICE_NAME="${service_name}"
LOG_TAG="$SERVICE_NAME"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [$LOG_TAG] $*" | tee -a /var/log/quantembrace-bootstrap.log /var/log/quantembrace-service.log; }

log "=== RISK ENGINE STARTUP ==="
log "Environment: ${environment}"

# ── System dependencies ──────────────────────────────────────────────────────
dnf update -y --quiet
dnf install -y docker amazon-cloudwatch-agent amazon-ecr-credential-helper
systemctl enable docker && systemctl start docker
usermod -aG docker ec2-user
systemctl enable amazon-ssm-agent && systemctl start amazon-ssm-agent

# ── Kernel tuning ────────────────────────────────────────────────────────────
cat > /etc/sysctl.d/99-quantembrace-trading.conf << 'SYSCTL'
net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.ipv4.tcp_rmem = 4096 87380 134217728
net.ipv4.tcp_wmem = 4096 65536 134217728
net.ipv4.tcp_nodelay = 1
net.core.netdev_max_backlog = 5000
net.ipv4.tcp_keepalive_time = 60
net.ipv4.tcp_keepalive_intvl = 10
net.ipv4.tcp_keepalive_probes = 5
net.ipv4.tcp_slow_start_after_idle = 0
SYSCTL
sysctl -p /etc/sysctl.d/99-quantembrace-trading.conf

# ── ECR credential helper ────────────────────────────────────────────────────
mkdir -p /root/.docker
cat > /root/.docker/config.json << DOCKER_CFG
{
  "credHelpers": {
    "${ecr_base}": "ecr-login"
  }
}
DOCKER_CFG

# ── CloudWatch Agent ─────────────────────────────────────────────────────────
mkdir -p /opt/aws/amazon-cloudwatch-agent/etc
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json << CWEOF
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/quantembrace-service.log",
            "log_group_name": "/quantembrace/${service_name}",
            "log_stream_name": "{instance_id}/service",
            "timestamp_format": "%Y-%m-%dT%H:%M:%SZ",
            "timezone": "UTC"
          }
        ]
      }
    }
  },
  "metrics": {
    "namespace": "QuantEmbrace",
    "metrics_collected": {
      "cpu": { "measurement": ["cpu_usage_idle", "cpu_usage_user", "cpu_usage_system"], "metrics_collection_interval": 30 },
      "mem": { "measurement": ["mem_used_percent"], "metrics_collection_interval": 30 }
    },
    "append_dimensions": {
      "AutoScalingGroupName": "\$${aws:AutoScalingGroupName}",
      "InstanceId": "\$${aws:InstanceId}",
      "Service": "${service_name}",
      "Environment": "${environment}"
    }
  }
}
CWEOF
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -s \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
log "CloudWatch Agent started."

# ── ECR login and image pull ─────────────────────────────────────────────────
IMAGE_URI="${ecr_base}/quantembrace-risk_engine:latest-${environment}"
log "Pulling image: $IMAGE_URI"
aws ecr get-login-password --region "${aws_region}" | \
  docker login --username AWS --password-stdin "${ecr_base}"
docker pull "$IMAGE_URI"
log "Image pulled."

# ── Runtime discovery: Kafka bootstrap + app environment name ────────────────
APP_ENV="${environment}"
if [ "$APP_ENV" = "prod" ]; then
  APP_ENV="production"
fi

KAFKA_CLUSTER_ARN=$(aws kafka list-clusters-v2 \
  --region "${aws_region}" \
  --cluster-name-filter "quantembrace-${environment}-kafka" \
  --query 'ClusterInfoList[0].ClusterArn' \
  --output text)
if [ -z "$KAFKA_CLUSTER_ARN" ] || [ "$KAFKA_CLUSTER_ARN" = "None" ]; then
  log "ERROR: MSK cluster quantembrace-${environment}-kafka not found."
  exit 1
fi
KAFKA_BOOTSTRAP_SERVERS=$(aws kafka get-bootstrap-brokers \
  --region "${aws_region}" \
  --cluster-arn "$KAFKA_CLUSTER_ARN" \
  --query 'BootstrapBrokerStringSaslIam' \
  --output text)
if [ -z "$KAFKA_BOOTSTRAP_SERVERS" ] || [ "$KAFKA_BOOTSTRAP_SERVERS" = "None" ]; then
  log "ERROR: MSK bootstrap brokers are empty for $KAFKA_CLUSTER_ARN."
  exit 1
fi

# ── Environment file ─────────────────────────────────────────────────────────
mkdir -p /opt/quantembrace
cat > /opt/quantembrace/${service_name}.env << ENV
QE_ENVIRONMENT=$APP_ENV
QE_SERVICE_NAME=${service_name}
QE_LOG_LEVEL=${log_level}
QE_HEALTH_CHECK_PORT=8080
ENVIRONMENT=$APP_ENV
SERVICE_NAME=${service_name}
AWS_REGION=${aws_region}
AWS_DYNAMODB_TABLE_PREFIX=${dynamodb_table_prefix}
AWS_S3_BUCKET=${s3_bucket_trading_logs}
LOG_LEVEL=${log_level}
DYNAMODB_TABLE_PREFIX=${dynamodb_table_prefix}
KAFKA_BOOTSTRAP_SERVERS=$KAFKA_BOOTSTRAP_SERVERS
S3_BUCKET_TRADING_LOGS=${s3_bucket_trading_logs}
# Trading safety: signal age ceiling must match code hard-ceiling (30s).
# Candle signals are 7-12s old at risk_engine; default 5s would reject all of them.
RISK_MAX_SIGNAL_AGE_SECONDS=30
# Risk profile: paper for paper sessions, tiny-live for Stage-1 live validation.
# Change to tiny-live when QE_EXECUTION_LIVE_TRADING_ENABLED=true is set.
RISK_PROFILE=paper
ENV
chmod 600 /opt/quantembrace/${service_name}.env
log "Environment file written."

# ── systemd unit ─────────────────────────────────────────────────────────────
cat > /etc/systemd/system/quantembrace-${service_name}.service << UNIT
[Unit]
Description=QuantEmbrace ${service_name} - CRITICAL: Pre-trade Risk
After=docker.service network-online.target
Requires=docker.service
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
User=root
Restart=on-failure
RestartSec=15s
TimeoutStartSec=180
TimeoutStopSec=120

ExecStartPre=/bin/bash -c 'aws ecr get-login-password --region ${aws_region} | docker login --username AWS --password-stdin ${ecr_base}'
ExecStartPre=-/usr/bin/docker stop quantembrace-${service_name} 2>/dev/null
ExecStartPre=-/usr/bin/docker rm quantembrace-${service_name} 2>/dev/null
ExecStart=/usr/bin/docker run --rm \
  --name quantembrace-${service_name} \
  --env-file /opt/quantembrace/${service_name}.env \
  --log-driver awslogs \
  --log-opt awslogs-region=${aws_region} \
  --log-opt awslogs-group=/quantembrace/${service_name} \
  --log-opt awslogs-stream=service \
  --network host \
  --health-cmd="curl -sf http://localhost:8080/health || exit 1" \
  --health-interval=30s \
  --health-timeout=5s \
  --health-retries=3 \
  --stop-timeout=120 \
  $IMAGE_URI

ExecStop=/usr/bin/docker stop --time=120 quantembrace-${service_name}

[Install]
WantedBy=multi-user.target
UNIT

# ── Enable and start ─────────────────────────────────────────────────────────
systemctl daemon-reload
systemctl enable quantembrace-${service_name}
systemctl start quantembrace-${service_name}

log "Service ${service_name} started."
log "=== RISK ENGINE STARTUP COMPLETE ==="
