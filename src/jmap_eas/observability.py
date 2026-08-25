"""Structured logging and lightweight in-memory metrics (plan.md section 7's M4 note).

Log messages never include credentials or raw backend exception text --
`BackendError`'s message is already redacted (plan.md section 6) before it
reaches here, and callers must pass only account/method identifiers as
`fields`, never request bodies or tokens.
"""
from __future__ import annotations

import json
import logging
import sys
import threading

LOGGER_NAME = "jmap_eas"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(fields)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Idempotent: safe to call from `create_app()` even if it runs more than once (e.g. in tests)."""
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


class Metrics:
    """Process-local counters. Reset on restart; not persisted, not shared across workers."""

    _FIELDS = ("requests_total", "errors_total", "sync_failures_total")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests_total = 0
        self.errors_total = 0
        self.sync_failures_total = 0

    def increment(self, field: str) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + 1)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {field: getattr(self, field) for field in self._FIELDS}
