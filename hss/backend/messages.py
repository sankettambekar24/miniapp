"""User-facing text for backend/service failures. Never includes tokens, passwords or request bodies."""
from __future__ import annotations

from .errors import (ColdStart, Conflict, Forbidden, NetworkError, NotFound, NotImplementedByBackend, ServerError,
                     Throttled, Timeout, Unauthorized, ValidationError)


def _field_text(fields: dict) -> str:
    parts = []
    for k, v in fields.items():
        v = "; ".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)
        parts.append(v if k in ("non_field_errors", "detail") else f"{k}: {v}")
    return " ".join(parts)


def friendly(exc: BaseException) -> str:
    """Short, actionable text for a login / admin-action failure."""
    from ..core.services import ServiceError
    if isinstance(exc, ServiceError):
        return str(exc)
    low = str(getattr(exc, "message", exc)).lower()
    if isinstance(exc, ValidationError):
        text = _field_text(exc.fields) or exc.message
        if "already exists" in text.lower() or "unique" in text.lower():
            return "That username already exists. Choose a different one."
        if "designated administrator" in text.lower():
            return "An administrator already exists; the backend allows only one. Create the user as USER."
        return f"The backend rejected the input: {text}"
    if isinstance(exc, Unauthorized):
        return "Wrong username or password, or the session expired. Sign in again."
    if isinstance(exc, Forbidden):
        return "The backend refused this action: administrator permission is required."
    if isinstance(exc, Throttled):
        wait = f" Wait about {int(exc.retry_after)} seconds." if getattr(exc, "retry_after", None) else ""
        return "Too many attempts." + wait
    if isinstance(exc, Conflict):
        return f"Not allowed in the current state: {exc.message}"
    if isinstance(exc, NotFound):
        return "The backend could not find that record."
    if isinstance(exc, ColdStart):
        return "The backend is waking up (free hosting cold start). Wait about a minute and try again."
    if isinstance(exc, (NetworkError, Timeout)):
        return "Cannot reach the backend. Check the internet connection. Nothing was changed and no access was granted."
    if isinstance(exc, NotImplementedByBackend):
        return f"Not available on the backend: {exc.message}"
    if isinstance(exc, ServerError):
        return f"The backend had an internal error ({getattr(exc, 'status', '?')}). Try again later."
    return f"{type(exc).__name__}: {exc}" if low else type(exc).__name__
