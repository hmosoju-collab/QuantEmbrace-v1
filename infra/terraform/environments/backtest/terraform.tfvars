# QuantEmbrace Backtest Environment — variable overrides
# DO NOT commit secrets here. All sensitive values come from AWS SSM / env.

aws_region            = "ap-south-1"
availability_zone     = "ap-south-1a"
monthly_budget_usd    = 100
alert_email           = "hari.mosoju@gmail.com"
primary_instance_type = "c6g.large"
max_workers           = 10
root_volume_gb        = 30
