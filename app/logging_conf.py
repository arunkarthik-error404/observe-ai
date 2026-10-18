"""Structured JSON logging, correlated by call_id.

Every line a single call produces can be pulled with
`grep '"call_id":"<id>"'` — which is the whole debugging story for a voice
agent, where you cannot step through a conversation in a debugger.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_STANDARD = set(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(level: int = logging.INFO) -> None:
    from .config import settings
    from .redaction import RedactingFilter

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    # On the handler, not on one logger: a library that logs a caller's number
    # is exactly the case a per-logger filter would miss.
    if settings().redact_logs:
        handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn's access log is noise next to the call timeline
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
