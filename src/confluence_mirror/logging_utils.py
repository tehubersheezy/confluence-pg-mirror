"""Logging setup with secret redaction.

Every handler gets a filter that replaces registered secrets (the PAT) and anything shaped
like an Authorization header value, so a token cannot reach a log even via an exception
message or a third-party library's debug output.
"""

from __future__ import annotations

import logging
import re

_SECRETS: set[str] = set()
_AUTH_PATTERN = re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)(bearer|basic)\s+[^\s'\",]+")
REDACTED = "***REDACTED***"


def register_secret(value: str) -> None:
    if value and len(value) >= 4:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for secret in _SECRETS:
        text = text.replace(secret, REDACTED)
    return _AUTH_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)} {REDACTED}", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if record.exc_info:
            formatted = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(formatted)
            record.exc_info = None
        if cleaned != message or record.args:
            record.msg = cleaned
            record.args = None
        return True


def setup_logging(verbosity: int = 0) -> None:
    level = logging.WARNING if verbosity < 0 else logging.INFO if verbosity == 0 else logging.DEBUG
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx/httpcore log request lines at INFO/DEBUG; keep them quiet regardless.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
