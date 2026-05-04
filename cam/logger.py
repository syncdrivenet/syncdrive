"""
JSON file logger for camera node.
Writes structured logs to /data/logs/cam/app.log for Fluent Bit to tail.
"""

import json
import os
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from logging.handlers import RotatingFileHandler

from config import get_config


class JSONFormatter(logging.Formatter):
    """Format log records as JSON for Fluent Bit."""

    def __init__(self, node_name: str):
        super().__init__()
        self.node_name = node_name

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "node": self.node_name,
            "level": record.levelname,
            "component": record.name,
            "message": record.getMessage(),
        }

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Add extra fields
        if hasattr(record, "extra_fields"):
            log_entry.update(record.extra_fields)

        return json.dumps(log_entry)


class ExtraFieldsAdapter(logging.LoggerAdapter):
    """Adapter that allows passing extra fields to log messages."""

    def process(self, msg, kwargs):
        extra = kwargs.get("extra", {})
        if extra:
            kwargs["extra"] = {"extra_fields": extra}
        return msg, kwargs


_logger: Optional[logging.Logger] = None


def setup_logging() -> logging.Logger:
    """Setup JSON file logging."""
    global _logger

    if _logger is not None:
        return _logger

    config = get_config()

    # Ensure log directory exists
    log_dir = config.logging.log_path
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "app.log"

    # Create logger
    logger = logging.getLogger("cam")
    logger.setLevel(getattr(logging, config.logging.level.upper(), logging.INFO))

    # Remove existing handlers
    logger.handlers.clear()

    # File handler with rotation (10MB max, keep 5 files)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setFormatter(JSONFormatter(config.node.name))
    logger.addHandler(file_handler)

    # Also log to stdout for debugging
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(stdout_handler)

    _logger = logger
    return logger


def get_logger(component: str = "cam") -> ExtraFieldsAdapter:
    """Get a logger for a specific component."""
    base_logger = setup_logging()
    child_logger = base_logger.getChild(component)
    return ExtraFieldsAdapter(child_logger, {})


# Convenience functions
def log(component: str, message: str, level: str = "INFO", **extra):
    """Simple logging function."""
    logger = get_logger(component)
    log_func = getattr(logger, level.lower(), logger.info)
    log_func(message, extra=extra if extra else None)


def log_info(component: str, message: str, **extra):
    log(component, message, "INFO", **extra)


def log_error(component: str, message: str, **extra):
    log(component, message, "ERROR", **extra)


def log_warning(component: str, message: str, **extra):
    log(component, message, "WARNING", **extra)


def log_debug(component: str, message: str, **extra):
    log(component, message, "DEBUG", **extra)
