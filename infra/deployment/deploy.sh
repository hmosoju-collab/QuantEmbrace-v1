#!/usr/bin/env bash
# QuantEmbrace - Deployment Script
# ===================================
# Builds Docker images, pushes to ECR, and updates ECS services.
#
# Usage:
#   ./deploy.sh <environment> [service_name]
#   ./deploy.sh dev                    # Deploy all services to dev
#   ./deploy.sh prod risk_engine       # Deploy only risk_engine to prod
#   ./deploy.sh staging data_ingestion # Deploy only data_ingestion to staging

set -euo pipefail

# --- Configuration ---
AWS_REGION="${AWS_REGION:-ap-south-1}"
ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
PROJECT="quantembrace"
SERVICES=("data_ingestion" "strategy_engine" "execution_engine" "risk_engine" "ai_engine")

# --- Argument Parsing ---
ENVIRONMENT="${1:?Usage: deploy.sh <environment> [service_name]}"
TARGET_SERVICE="${2:-all}"

if [[ ! "$ENVIRONMENT" =~ ^(dev|staging|prod)$ ]]; then
    echo "ERROR: Environment must be dev, staging, or prod"
    exit 1
fi

# --- Safety Check for Prod ---
if [[ "$ENVIRONMENT" == "prod" ]]; then
    echo "WARNING: Deploying to PRODUCTION"
    echo "Press Ctrl+C within 10 seconds to abort..."
    sleep 10
fi

echo "=== QuantEmbrace Deployment ==="
echo "Environment: $ENVIRONMENT"
echo "Region:      $AWS_REGION"
echo "Target:      $TARGET_SERVICE"
echo ""

# --- ECR Login ---
echo "Authenticating with ECR..."
aws ecr get-login-password --region "$AWS_REGION" | \
    docker login --username AWS --password-stdin "$ECR_REGISTRY"

# --- Build and Push ---
deploy_service() {
    local service_name="$1"
    local image_tag="${PROJECT}-${ENVIRONMENT}-${service_name}:$(git rev-parse --short HEAD)"
    local full_image="${ECR_REGISTRY}/${image_tag}"

    echo "--- Building ${service_name} ---"
    docker build \
        --build-arg SERVICE_NAME="${service_name}" \
        -t "${image_tag}" \
        -f infra/deployment/Dockerfile \
        .

    echo "--- Pushing ${service_name} to ECR ---"
    docker tag "${image_tag}" "${full_image}"
    docker push "${full_image}"

    echo "--- Updating ECS service: ${service_name} ---"
    aws ecs update-service \
        --cluster "${PROJECT}-${ENVIRONMENT}" \
        --service "${PROJECT}-${ENVIRONMENT}-${service_name}" \
        --force-new-deployment \
        --region "${AWS_REGION}"

    echo "--- ${service_name} deployment initiated ---"
}

# --- Execute Deployment ---
if [[ "$TARGET_SERVICE" == "all" ]]; then
    for service in "${SERVICES[@]}"; do
        deploy_service "$service"
    done
else
    if printf '%s\n' "${SERVICES[@]}" | grep -qx "$TARGET_SERVICE"; then
        deploy_service "$TARGET_SERVICE"
    else
        echo "ERROR: Unknown service '${TARGET_SERVICE}'"
        echo "Valid services: ${SERVICES[*]}"
        exit 1
    fi
fi

echo ""
echo "=== Deployment Complete ==="
echo "Monitor at: https://${AWS_REGION}.console.aws.amazon.com/ecs/v2/clusters/${PROJECT}-${ENVIRONMENT}/services"
