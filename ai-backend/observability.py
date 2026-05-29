"""
Structured logging for the AI backend.

JSON lines on stdout. Each log line is one event — CloudWatch (Fargate)
captures stdout, then we forward via OTel collector → Grafana Cloud (per
the §13 infra decision).

Conventions:
  • Event names are dotted (`anthropic.client.stream.completed`)
  • Every line has: ts, level, event, session_id (when known)
  • Anthropic API keys are NEVER logged. Use fingerprint_key() if needed.
  • Numbers are numbers, not strings (so Grafana can graph them)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any


class JSONFormatter(logging.Formatter):
    """Emit one JSON object per log line. `extra={...}` fields are merged
    at the top level so they're directly queryable."""

    # Standard LogRecord attrs we ignore when pulling `extra` fields.
    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _iso8601(record.created),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        # Pull `extra={}` user fields
        for k, v in record.__dict__.items():
            if k in self._RESERVED:
                continue
            if k.startswith("_"):
                continue
            payload[k] = _coerce(v)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, separators=(",", ":"), default=str)


def _iso8601(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + f".{int((epoch % 1) * 1000):03d}Z"


def _coerce(value: Any) -> Any:
    """Make values JSON-safe. Bytes → length placeholder; sets → lists."""
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, set):
        return list(value)
    return value


_configured = False


def configure(level: str | int = "INFO") -> None:
    """Install the JSON formatter on the root logger. Idempotent."""
    global _configured
    if _configured:
        return

    # Allow LOG_LEVEL env override
    env_level = os.environ.get("ANIMORA_LOG_LEVEL")
    if env_level:
        level = env_level

    root = logging.getLogger()
    root.setLevel(level)

    # Strip any handlers FastAPI / uvicorn may have set up so we control output
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)

    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)

    _configured = True


def logger(name: str) -> logging.Logger:
    """Get a logger. Call configure() first (main.py does this on startup)."""
    if not _configured:
        configure()
    return logging.getLogger(name)
