"""Local rotating diagnostic logs for the CLI, runner, and observability layer."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


_LOCK = threading.Lock()
_CONFIGURED: set[str] = set()


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "diagnostic_fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def diagnostic_log_dir() -> Path:
    configured = os.getenv("AUTOCODE_LOG_DIR", "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else Path.home() / ".autocode" / "logs"
    )


def get_diagnostic_logger(component: str) -> logging.Logger:
    name = f"autocode.{component}"
    logger = logging.getLogger(name)
    with _LOCK:
        if name in _CONFIGURED:
            return logger
        directory = diagnostic_log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / f"{component}.jsonl",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(getattr(logging, os.getenv("AUTOCODE_LOG_LEVEL", "INFO").upper(), logging.INFO))
        logger.propagate = False
        _CONFIGURED.add(name)
    return logger


def log_event(logger: logging.Logger, level: int, message: str, **fields) -> None:
    logger.log(level, message, extra={"diagnostic_fields": fields})
