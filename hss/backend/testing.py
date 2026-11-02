"""Test helpers (no network): scripted FakeTransport and probe-fixture replay."""
from __future__ import annotations

import json
from pathlib import Path

from .transport import TransportResponse

PROBES = Path(__file__).resolve().parents[2] / "tests" / "evidence" / "probes"


def jresp(status: int, obj=None, headers: dict | None = None) -> TransportResponse:
    body = b"" if obj is None else json.dumps(obj).encode()
    return TransportResponse(status, {k.lower(): v for k, v in (headers or {}).items()}, body)


def probe_response(name: str) -> TransportResponse:
    """Replay a recorded probe fixture (tests/evidence/probes/<name>.json)."""
    d = json.loads((PROBES / f"{name}.json").read_text(encoding="utf-8"))
    return TransportResponse(d["status"], {k.lower(): v for k, v in d.get("headers", {}).items()},
                             (d.get("body") or "").encode())


def err_body(code: str, message: str = "msg", status_hint=None) -> dict:
    return {"error": {"code": code, "message": message, "request_id": "rid"}}


class FakeTransport:
    """Queue of responses or exceptions; or a handler(method, path, headers, body) callable."""

    def __init__(self, *script, handler=None):
        self.script = list(script)
        self.handler = handler
        self.calls: list[dict] = []

    def send(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": json.loads(body) if body else None, "timeout": timeout})
        if self.handler:
            item = self.handler(self.calls[-1])
        else:
            item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
