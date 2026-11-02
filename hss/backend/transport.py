"""Injectable HTTP transport. Production = urllib (stdlib); tests inject a fake."""
from __future__ import annotations

import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from .errors import NetworkError, Timeout


@dataclass
class TransportResponse:
    status: int
    headers: dict = field(default_factory=dict)  # lower-case keys
    body: bytes = b""


class Transport(Protocol):
    def send(self, method: str, url: str, headers: dict, body: bytes | None,
             timeout: float) -> TransportResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # never follow (would leak Authorization)
        return None


class UrllibTransport:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect)

    def send(self, method, url, headers, body, timeout):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return TransportResponse(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
        except urllib.error.HTTPError as e:
            try:
                data = e.read()
            except Exception:
                data = b""
            return TransportResponse(e.code, {k.lower(): v for k, v in e.headers.items()}, data)
        except (socket.timeout, TimeoutError):
            raise Timeout()
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError)):
                raise Timeout()
            raise NetworkError()
        except OSError:
            raise NetworkError()
