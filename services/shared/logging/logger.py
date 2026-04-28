"""
Structured JSON logging with correlation IDs for QuantEmbrace.

Provides consistent, machine-parseable log output across all services.
Correlation IDs enable tracing a signal from strategy through risk to execution.
Designed for CloudWatch Logs ingestion on ECS Fargate.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Optional

# Context variable for request/correlation tracking across async boundaries
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def set_correlation_id(correlation_id: Optional[str] = None) -> str:
    """
    Set the correlation ID for the current context.

    Args:
        correlation_id: Explicit ID to set. Generates a new UUID if None.

    Returns:
        The correlation ID that was set.
    """
    cid = correlation_id or str(uuid.uuid4())
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str:
    """Return the current correlation ID, or empty string if not set."""
    return _correlation_id.get()


class StructuredJsonFormatter(logging.Formatter):
    """
    Formats log records as single-line JSON objects.

    Output format:
        {"timestamp": "...", "level": "INFO", "service": "...",
         "correlation_id": "...", "message": "...", "extra": {...}}
    """

    def __init__(self, service_name: str = "unknown") -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "correlation_id": get_correlation_id(),
            "message": record.getMessage(),
        }

        # Include exception info if present
        if record.exc_info and record.exc_info[1] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Include any extra fields passed via the `extra` kwarg
        standard_attrs = {
            "name", "msg", "args", "created", "relativeCreated", "exc_info",
            "exc_text", "stack_info", "lineno", "funcName", "pathname",
            "filename", "module", "levelno", "levelname", "msecs",
            "processName", "process", "threadName", "thread", "taskName",
            "message",
        }
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in standard_attrs and not k.startswith("_")
        }
        if extras:
            log_entry["extra"] = extras

        return json.dumps(log_entry, default=str, ensure_ascii=False)


def get_logger(
    name: str,
    service_name: str = "unknown",
    level: str = "INFO",
) -> logging.Logger:
    """
    Create or retrieve a structured JSON logger.

    Args:
        name: Logger name (typically __name__ of the calling module).
        service_name: Name of the microservice for log identification.
        level: Logging level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).

    Returns:
        Configured logging.Logger instance with JSON output to stdout.
    """
    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if logger already configured
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredJsonFormatter(service_name=service_name))
    logger.addHandler(handler)

    return logger
