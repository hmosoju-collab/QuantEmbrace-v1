#!/bin/bash
# ============================================================
# bootstrap.sh — Shared OS setup for ALL QuantEmbrace EC2 instances
#
# Sourced by service-specific userdata scripts.
# Idempotent — safe to re-run after a restart.
#
# Responsibilities:
#   1. Install system dependencies (Docker, CloudWatch Agent, SSM Agent)
#   2. Apply kernel network parameters for low-latency trading
#   3. Configure ECR login helper for automatic image pulls
#   4. Set up systemd service template
# ============================================================
set -euo pipefail

export AWS_REGION="${aws_region}"
LOG_TAG="quantembrace-bootstrap"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [$LOG_TAG] $*" | tee -a /var/log/quantembrace-bootstrap.log; }

log "Starting QuantEmbrace bootstrap for environment: ${environment}"

# ── 1. System update and dependencies ───────────────────────────────────────

log "Updating system packages..."
dnf update -y --quiet

log "Installing Docker..."
dnf install -y docker
systemctl enable docker
systemctl start docker

# Add ec2-user to docker group (for SSM session access to docker CLI)
usermod -aG docker ec2-user

log "Installing CloudWatch Agent..."
dnf install -y amazon-cloudwatch-agent

log "Installing AWS CLI v2..."
# AWS CLI is pre-installed on AL2023; ensure it is current
dnf install -y aws-cli 2>/dev/null || true

log "Verifying SSM Agent (pre-installed on AL2023)..."
systemctl enable amazon-ssm-agent
systemctl start amazon-ssm-agent

# ── 2. Kernel network parameters for low-latency trading ────────────────────
# These settings improve WebSocket throughput, reduce TCP buffering,
# and tune connection handling for high-frequency broker API interactions.

log "Applying kernel network parameters..."
cat > /etc/sysctl.d/99-quantembrace-trading.conf << 'EOF'
# TCP buffer sizes: 128MB max for high-throughput WebSocket feeds
net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.ipv4.tcp_rmem = 4096 87380 134217728
net.ipv4.tcp_wmem = 4096 65536 134217728

# Disable Nagle algorithm: critical for low-latency order placement
net.ipv4.tcp_nodelay = 1

# Increase backlog for incoming connection queues
net.core.netdev_max_backlog = 5000
net.core.somaxconn = 1024

# Reduce TIME_WAIT: allows faster connection reuse to broker APIs
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_tw_reuse = 1

# Keepalive tuning: detect dead broker connections faster
net.ipv4.tcp_keepalive_time = 60
net.ipv4.tcp_keepalive_intvl = 10
net.ipv4.tcp_keepalive_probes = 5

# Disable slow-start restart: maintains throughput on idle-then-burst WebSocket
net.ipv4.tcp_slow_start_after_idle = 0
EOF

sysctl -p /etc/sysctl.d/99-quantembrace-trading.conf
log "Kernel parameters applied."

# ── 3. ECR credential helper ─────────────────────────────────────────────────

log "Configuring Docker ECR credential helper..."
dnf install -y amazon-ecr-credential-helper

mkdir -p /root/.docker /home/ec2-user/.docker
cat > /root/.docker/config.json << EOF
{
  "credHelpers": {
    "${ecr_base}": "ecr-login"
  }
}
EOF
cp /root/.docker/config.json /home/ec2-user/.docker/config.json
chown -R ec2-user:ec2-user /home/ec2-user/.docker

log "ECR credential helper configured."

# ── 4. CloudWatch Agent configuration ───────────────────────────────────────

log "Configuring CloudWatch Agent..."
mkdir -p /opt/aws/amazon-cloudwatch-agent/etc

# Log group name derives from service name (passed by calling script)
# This template is written here; the service-specific config is set
# in the calling script via CW_LOG_GROUP env variable.
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json << CWEOF
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/quantembrace-bootstrap.log",
            "log_group_name": "/quantembrace/${service_name}",
            "log_stream_name": "{instance_id}/bootstrap",
            "timestamp_format": "%Y-%m-%dT%H:%M:%SZ",
            "timezone": "UTC"
          },
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
      "cpu": {
        "measurement": ["cpu_usage_idle", "cpu_usage_user", "cpu_usage_system"],
        "metrics_collection_interval": 60
      },
      "mem": {
        "measurement": ["mem_used_percent"],
        "metrics_collection_interval": 60
      },
      "net": {
        "measurement": ["net_bytes_recv", "net_bytes_sent", "net_packets_recv", "net_packets_sent"],
        "metrics_collection_interval": 60
      }
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
  -a fetch-config \
  -m ec2 \
  -s \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json

log "CloudWatch Agent configured and started."

# ── 5. ECR login and image pull ──────────────────────────────────────────────
# Called by service-specific scripts after this bootstrap completes.
# Exported as function for reuse.

ecr_login_and_pull() {
  local image_uri="$1"
  log "Logging into ECR..."
  aws ecr get-login-password --region "${aws_region}" | \
    docker login --username AWS --password-stdin "${ecr_base}"
  log "Pulling image: $image_uri"
  docker pull "$image_uri"
  log "Image pull complete: $image_uri"
}

export -f ecr_login_and_pull

log "Bootstrap complete. Service-specific startup will now run."
