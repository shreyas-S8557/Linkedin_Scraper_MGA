from __future__ import annotations

import logging
import re
import sys

from app.config import LOG_LEVEL

_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9]{10,})"),
    re.compile(r"(GMAIL_APP_PASSWORD\s*=\s*)\S+", re.IGNORECASE),
    re.compile(r"(MAILERLITE_API_TOKEN\s*[=:]\s*)\S+", re.IGNORECASE),
    re.compile(r"(api[_-]?key\s*[=:]\s*)\S+", re.IGNORECASE),
    re.compile(r"(password\s*[=:]\s*)\S+", re.IGNORECASE),
    # Catches "Authorization: Bearer <token>" appearing in any log line
    # (e.g. an httpx exception message that happens to echo request
    # headers) -- MailerLiteSender never logs its own headers, but this is
    # defense in depth so a token can never surface via a log line either.
    re.compile(r"(Bearer\s+)\S+", re.IGNORECASE),
]


def redact(text: str) -> str:
    """Best-effort redaction of anything that looks like a secret before it
    hits a log line or an API error response."""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: m.group(1) + "***REDACTED***" if m.lastindex else "***REDACTED***", out)
    return out


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(str(record.msg))
        except Exception:  # noqa: BLE001
            pass
        return True


def configure_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # already configured (e.g. under uvicorn --reload)
    root.setLevel(LOG_LEVEL)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
