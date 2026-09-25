#!/bin/bash
# QuantEmbrace backtest worker bootstrap
# Traps SIGTERM (Spot interruption 2-min notice) to checkpoint + clean exit.
# Bash vars that are NOT Terraform template vars use $${} to escape templatefile.
set -euo pipefail

# Environment for the worker process (Terraform-injected values)
export QE_BT_RUNS_TABLE="${runs_table}"
export QE_BT_CHECKPOINTS_TABLE="${checkpoints_table}"
export QE_BT_DATA_BUCKET="${data_bucket}"
export QE_BT_RESULTS_BUCKET="${results_bucket}"
export AWS_DEFAULT_REGION="${aws_region}"
export QE_BT_SNS_ALERT_ARN="${sns_alert_arn}"

# IMDSv2: get instance-id for worker tagging in registry
TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id \
  -H "X-aws-ec2-metadata-token: $${TOKEN}")
export QE_WORKER_INSTANCE_ID="$${INSTANCE_ID}"

# Poll for Spot interruption notice every 5s in background.
# On notice: send SIGTERM to the main worker process (triggers checkpoint).
( while true; do
    HTTP_CODE=$(curl -so /dev/null -w "%%{http_code}" \
      http://169.254.169.254/latest/meta-data/spot/termination-time \
      -H "X-aws-ec2-metadata-token: $${TOKEN}")
    if [ "$${HTTP_CODE}" = "200" ]; then
      echo "[worker-bootstrap] Spot interruption notice received — signalling worker" >&2
      kill -TERM $${WORKER_PID} 2>/dev/null || true
      break
    fi
    sleep 5
done ) &

# Install Python deps (pre-baked AMI preferred; this is the fallback)
pip3 install --quiet pyarrow pandas boto3 2>/dev/null || true

# The actual run command is injected by the run script via SSM Parameter Store
# or passed as an additional userdata segment.  The worker is bootstrapped here;
# the run entrypoint is services/backtesting/replay_engine.py.
echo "[worker-bootstrap] Instance $${INSTANCE_ID} ready"
