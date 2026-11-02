"""Typed backend errors. Messages are user-safe and redacted; never carry tokens or passwords.

Backend error envelope (backend/common/exceptions.py):
    {"error": {"code": "SNAKE_CASE", "message": "...", "request_id": "..."}}
"""
from __future__ import annotations

from .redact import redact_text


class BackendError(Exception):
    default_message = "The backend reported an error."

    def __init__(self, message: str | None = None, *, status: int | None = None,
                 code: str | None = None, request_id: str | None = None,
                 retry_after: float | None = None, fields: dict | None = None):
        self.message = redact_text(message or self.default_message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.retry_after = retry_after
        self.fields = fields or {}
        super().__init__(self.message)

    def __str__(self) -> str:
        bits = [self.message]
        if self.status:
            bits.append(f"status={self.status}")
        if self.code:
            bits.append(f"code={self.code}")
        return " ".join(bits)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self.status!r}, code={self.code!r}, message={self.message!r})"


class NetworkError(BackendError):
    default_message = "Cannot reach the server. Check your internet connection."


class Timeout(NetworkError):
    default_message = "The server did not answer in time."


class ColdStart(BackendError):
    default_message = "The server is waking up (502/503/504). Try again in a moment."


class Unauthorized(BackendError):
    default_message = "Not signed in or the session expired."


class Forbidden(BackendError):
    default_message = "You do not have permission for this action."


class NotFound(BackendError):
    default_message = "Not found."


class MethodNotAllowed(BackendError):
    default_message = "The server does not allow this method here."


class Conflict(BackendError):
    default_message = "The request conflicts with the current state."


class Gone(BackendError):
    default_message = "This item has expired."


class Throttled(BackendError):
    default_message = "Too many requests. Please wait and retry."


class ValidationError(BackendError):
    default_message = "The server rejected the input."


class ServerError(BackendError):
    default_message = "The server had an internal error."


class NotImplementedByBackend(BackendError):
    """Feature deliberately unavailable in the backend (not an outage; never retry)."""
    default_message = "This feature is not available on the backend."


class InvalidBaseUrl(ValueError):
    pass


# codes that mean "feature unavailable by design"
UNAVAILABLE_CODES = {
    "FILE_CONTENT_UNAVAILABLE",
    "OFFLINE_POLICY_ISSUANCE_DISABLED",
    "FACTOR_VERIFICATION_UNAVAILABLE",
}
SESSION_DEAD_CODES = {"TOKEN_REVOKED", "ROLE_CHANGED"}


def error_from_response(status: int, body: object, headers: dict | None = None) -> BackendError:
    headers = headers or {}
    code = message = request_id = None
    fields: dict = {}
    if isinstance(body, dict):
        env = body.get("error")
        if isinstance(env, dict):
            code = env.get("code")
            message = env.get("message")
            request_id = env.get("request_id")
        elif "detail" in body:
            message = str(body["detail"])
            code = str(body.get("code") or "").upper() or None
        else:
            fields = {k: v for k, v in body.items() if isinstance(v, (list, str))}
    code = str(code).upper() if code else None
    if code == "VALIDATION_ERROR" and message:
        for part in str(message).split("; "):
            if ": " in part:
                k, v = part.split(": ", 1)
                fields[k] = v
    kw = dict(status=status, code=code, request_id=request_id, fields=fields)
    if code in UNAVAILABLE_CODES:
        return NotImplementedByBackend(message, **kw)
    ra = headers.get("retry-after")
    if status == 429:
        try:
            kw["retry_after"] = float(ra) if ra is not None else None
        except ValueError:
            kw["retry_after"] = None
        return Throttled(message, **kw)
    table = {400: ValidationError, 401: Unauthorized, 403: Forbidden, 404: NotFound,
             405: MethodNotAllowed, 409: Conflict, 410: Gone}
    if status in table:
        return table[status](message, **kw)
    if status in (502, 503, 504):
        return ServerError(message, **kw) if code else ColdStart(None, **kw)
    if status >= 500:
        return ServerError(message, **kw)
    return BackendError(message, **kw)
