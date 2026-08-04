"""
Logging setup.

Replaces the `print(f"  [warn] ...")` pattern that was scattered through the
adapters and scripts. Two reasons this matters once the project runs anywhere
other than a terminal:

  * a print to stdout inside a web request is invisible; a log line is not
  * `LOG_FORMAT=json` gives one JSON object per line, which is what any log
    aggregator (CloudWatch, Loki, Datadog) expects to ingest

Call `configure_logging()` once at every entry point (script `main()`, API
startup). Library modules just do `log = get_logger(__name__)` and never
configure anything themselves.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from .config import settings

_CONFIGURED = False

# Attributes present on every LogRecord. Anything not in here was attached by
# the caller via `extra=` and belongs in the structured payload.
_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with `extra=` fields merged in at the top level."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = val
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable, with `extra=` fields appended as key=value pairs."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
                         datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        return base


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Idempotent. Safe to call from several entry points in one process."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = (level or settings.log_level).upper()
    fmt = (fmt or settings.log_format).lower()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))

    # These three are chatty on import and during HTTP calls; we don't need
    # their DEBUG output even when ours is turned up.
    for noisy in ("urllib3", "sentence_transformers", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
