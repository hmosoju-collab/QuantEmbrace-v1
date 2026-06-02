#!/usr/bin/env bash
# QuantEmbrace — Deployment (EC2 ASG architecture)
# ==================================================
# IMPORTANT: This repository uses EC2 Auto Scaling Groups, NOT ECS Fargate.
# The previous version of this script called `aws ecs update-service` which
# no longer applies. ECS Fargate was removed in ADR-009.
#
# ──────────────────────────────────────────────────────────────────────────────
# CORRECT DEPLOYMENT WORKFLOW
# ──────────────────────────────────────────────────────────────────────────────
#
# Normal deployment (CI/CD — preferred):
#   Push to main → GitHub Actions build.yml builds image → deploy.yml promotes
#   to staging → manual approval → prod promotion via ASG instance refresh.
#
# Manual image promotion + ASG refresh:
#
#   1. Promote an existing ECR image to latest-prod:
#      scripts/deploy/promote_ecr_image.sh \
#        --repository quantembrace-<service> \
#        --source-tag <sha> \
#        --target-tag latest-prod
#
#   2. Trigger ASG instance refresh (replaces instances with the new image):
#      aws autoscaling start-instance-refresh \
#        --auto-scaling-group-name quantembrace-prod-<service>-asg \
#        --preferences '{"MinHealthyPercentage": 0, "InstanceWarmup": 180}'
#
#   3. Monitor refresh until all instances are healthy:
#      scripts/deploy/check_asg_health.py \
#        --asg quantembrace-prod-<service>-asg \
#        --min-healthy 1 \
#        --timeout 300
#
#   DEPLOY ORDER (must be respected):
#     risk_engine → execution_engine → data_ingestion → strategy_engine
#
# Rollback:
#   1. Identify last-known-good SHA from ECR or git log
#   2. Promote it: scripts/deploy/promote_ecr_image.sh --source-tag <old-sha> --target-tag latest-prod
#   3. Refresh ASG: aws autoscaling start-instance-refresh ...
#   4. If trading must halt immediately: scripts/kill_switch_cli.py activate --reason "rollback in progress"
#
# ──────────────────────────────────────────────────────────────────────────────

echo "ERROR: This script is a stub — ECS deploy path removed (ADR-009)."
echo ""
echo "Use GitHub Actions deploy.yml for normal deployments, or see the comments"
echo "at the top of this file for the manual EC2 ASG promotion procedure."
echo ""
echo "For a production paper session: follow docs/live-readiness/pre-live-runbook.md §2"
exit 1
