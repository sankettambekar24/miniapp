"""Redaction helpers. Everything that logs or formats backend data goes through here."""
from __future__ import annotations

import logging
import re
from typing import Any

REDACTED = "[REDACTED]"
SENSITIVE_KEYS = ("token", "authorization", "password", "refresh", "access", "cookie",
                  "secret", "capability", "credential", "passwd", "pin")
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]*\.[A-Za-z0-9_\-]+\.?[A-Za-z0-9_\-]*")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]+")
_KV = re.compile(
    r"(?i)(\"?[A-Za-z_\-]*(?:token|authorization|password|passwd|refresh|access|set-cookie|cookie|secret|capability)"
    r"[A-Za-z_\-]*\"?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}&]+)")


def is_sensitive_key(key: object) -> bool:
    k = str(key).lower()
    if k == "pin":
        return True
    return any(s in k for s in SENSITIVE_KEYS if s != "pin")


def redact_text(text: Any) -> str:
    s = str(text)
    s = _JWT.sub(REDACTED, s)
    s = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", s)
    s = _KV.sub(lambda m: m.group(1) + REDACTED, s)
    return s


def redact_obj(obj: Any) -> Any:
    """Deep copy of obj with sensitive keys masked and strings scrubbed."""
    if isinstance(obj, dict):
        return {k: (REDACTED if is_sensitive_key(k) and v not in (None, "") else redact_obj(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


class RedactingFilter(logging.Filter):
    """Attach to handlers/loggers: scrubs msg and args of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_text(record.getMessage())
            record.args = ()
        except Exception:  # never let logging crash the app
            record.msg = REDACTED
            record.args = ()
        return True
