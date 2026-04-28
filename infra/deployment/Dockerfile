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
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy installed dependencies from builder
COPY --from=builder /install /usr/local

# Copy shared code first (changes less frequently)
COPY services/shared/ /app/services/shared/
COPY services/__init__.py /app/services/__init__.py

# Copy the specific service
COPY services/${SERVICE_NAME}/ /app/services/${SERVICE_NAME}/

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Non-root user for security
RUN useradd -m -r appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Entry point runs the specific service
CMD ["python", "-m", "services.${SERVICE_NAME}.service"]
