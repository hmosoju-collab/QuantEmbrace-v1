#!/usr/bin/env bash
set -euo pipefail

REPOSITORY=""
SOURCE_TAG=""
TARGET_TAG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repository)
      REPOSITORY="$2"
      shift 2
      ;;
    --source-tag)
      SOURCE_TAG="$2"
      shift 2
      ;;
    --target-tag)
      TARGET_TAG="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$REPOSITORY" || -z "$SOURCE_TAG" || -z "$TARGET_TAG" ]]; then
  echo "Usage: $0 --repository <repo> --source-tag <tag> --target-tag <tag>" >&2
  exit 2
fi

if ! aws ecr describe-images \
  --repository-name "$REPOSITORY" \
  --image-ids imageTag="$SOURCE_TAG" >/dev/null 2>&1; then
  echo "Image $REPOSITORY:$SOURCE_TAG not found; leaving $TARGET_TAG unchanged."
  exit 0
fi

MANIFEST=$(aws ecr batch-get-image \
  --repository-name "$REPOSITORY" \
  --image-ids imageTag="$SOURCE_TAG" \
  --query 'images[0].imageManifest' \
  --output text)

if [[ -z "$MANIFEST" || "$MANIFEST" == "None" ]]; then
  echo "No image manifest returned for $REPOSITORY:$SOURCE_TAG" >&2
  exit 1
fi

aws ecr put-image \
  --repository-name "$REPOSITORY" \
  --image-tag "$TARGET_TAG" \
  --image-manifest "$MANIFEST" >/dev/null

echo "Promoted $REPOSITORY:$SOURCE_TAG -> $TARGET_TAG"
