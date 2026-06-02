# QuantEmbrace - Multi-stage Dockerfile
# ========================================
# Builds any service by passing SERVICE_NAME as build arg.
# Usage: docker build --build-arg SERVICE_NAME=data_ingestion -t quantembrace-data-ingestion .

# Stage 1: Dependencies
FROM python:3.11-slim AS builder

WORKDIR /app

COPY services/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim AS runtime

ARG SERVICE_NAME
ENV SERVICE_NAME=${SERVICE_NAME}
ENV PYTHONPATH=/app:/app/services
ENV PYTHONUNBUFFERED=1
ENV QE_HEALTH_CHECK_PORT=8080
ENV HEALTH_CHECK_PORT=8080

WORKDIR /app

# Copy installed dependencies from builder
COPY --from=builder /install /usr/local

# Copy the service monorepo. Some Phase 1-5 modules still import sibling
# services at runtime, so copying only one service produces fragile images.
COPY services/ /app/services/

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f\"http://localhost:{os.environ.get('HEALTH_CHECK_PORT', '8080')}/health\")" || exit 1

# Non-root user for security
RUN useradd -m -r appuser && chown -R appuser /app
USER appuser

EXPOSE 8080

# Entry point runs the specific service
CMD ["sh", "-c", "python -m services.${SERVICE_NAME}.service"]
