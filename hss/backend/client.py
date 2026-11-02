"""BackendClient: stdlib-only client for the HSS Django backend (/api/v1).

Security properties: access token lives only in SessionStore.backend_token; the refresh
token lives only in this object's memory and is dropped as soon as the SessionStore is
cleared (lock/disconnect). Nothing is written to disk. Logs carry method/path/status/duration.
Status of everything here: MOCK-TESTED (contract tests, fake transport) or IMPLEMENTED-UNTESTED
(source-derived write endpoints). Nothing has been run against the live server.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from .errors import (BackendError, ColdStart, InvalidBaseUrl, NetworkError, NotImplementedByBackend,
                     ServerError, SESSION_DEAD_CODES, Timeout, Unauthorized, error_from_response)
from .transport import Transport, TransportResponse, UrllibTransport

log = logging.getLogger("hss.backend")
API = "/api/v1"
SAFE = {"GET", "HEAD", "OPTIONS"}
CAPABILITY_HEADER = "X-Auth-Attempt-Capability"
FIRST_TIMEOUT = 90.0
NORMAL_TIMEOUT = 15.0
_RETRY = object()


@dataclass
class Page:
    results: list
    count: int = 0
    next: str | None = None
    previous: str | None = None


def _u(segment: str) -> str:
    """Quote a path segment (ids are UUIDs; this blocks path injection)."""
    return urllib.parse.quote(str(segment), safe="")


def validate_base_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise InvalidBaseUrl("Backend URL is empty.")
    u = urllib.parse.urlsplit(url.strip())
    if u.username or u.password:
        raise InvalidBaseUrl("Backend URL must not contain credentials.")
    host = (u.hostname or "").lower()
    if not host:
        raise InvalidBaseUrl("Backend URL has no host.")
    if not (u.scheme == "https" or (u.scheme == "http" and host in ("localhost", "127.0.0.1"))):
        raise InvalidBaseUrl("Backend URL must use https (http only for localhost).")
    if u.query or u.fragment:
        raise InvalidBaseUrl("Backend URL must not have a query or fragment.")
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path.rstrip("/"), "", ""))


class BackendClient:
    def __init__(self, base_url: str, session, transport: Transport | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 on_wait: Callable[[int, float, str], None] | None = None,
                 max_attempts: int = 4, backoff_base: float = 2.0, backoff_cap: float = 20.0,
                 first_timeout: float = FIRST_TIMEOUT, normal_timeout: float = NORMAL_TIMEOUT):
        self.base_url = validate_base_url(base_url)
        self.session = session
        self.transport = transport or UrllibTransport()
        self._clock, self._sleep = clock, sleep
        self.on_wait = on_wait
        self.max_attempts, self.backoff_base, self.backoff_cap = max_attempts, backoff_base, backoff_cap
        self.first_timeout, self.normal_timeout = first_timeout, normal_timeout
        self._contacted = False
        self._refresh_token: str | None = None
        self._user: dict | None = None

    def __repr__(self) -> str:
        return f"BackendClient(base_url={self.base_url!r}, authenticated={self.is_authenticated})"

    # ------------------------------------------------------------------ state
    def _sync(self) -> None:
        """If the SessionStore was cleared (lock/disconnect) drop everything we hold."""
        if self.session.backend_token is None:
            self._refresh_token = None
            self._user = None

    @property
    def is_authenticated(self) -> bool:
        self._sync()
        return self.session.backend_token is not None

    @property
    def current_user(self) -> dict | None:
        """{id, username, role} captured at login (advisory; me() is authoritative)."""
        self._sync()
        return dict(self._user) if self._user else None

    def _drop_tokens(self) -> None:
        self.session.backend_token = None
        self._refresh_token = None
        self._user = None

    # ------------------------------------------------------------- transport
    def _url(self, path: str, query: dict | None) -> str:
        url = self.base_url + API + path
        q = {k: v for k, v in (query or {}).items() if v is not None}
        return url + ("?" + urllib.parse.urlencode(q) if q else "")

    def _send_once(self, method, path, query, body, headers) -> tuple[TransportResponse, Any]:
        timeout = self.normal_timeout if self._contacted else self.first_timeout
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Accept": "application/json", **headers}
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        t0 = self._clock()
        status: object = "ERR"
        try:
            resp = self.transport.send(method, self._url(path, query), hdrs, data, timeout)
            status = resp.status
            self._contacted = True
        finally:
            log.info("%s %s -> %s (%.0f ms)", method, API + path, status, (self._clock() - t0) * 1000)
        parsed: Any = None
        if resp.body:
            try:
                parsed = json.loads(resp.body.decode("utf-8", "replace"))
            except ValueError:
                parsed = None
        return resp, parsed

    def _request(self, method: str, path: str, *, body: Any = None, query: dict | None = None,
                 auth: bool = True, capability: str | None = None, allow_refresh: bool = True) -> Any:
        method = method.upper()
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            hdrs: dict = {}
            if capability:
                hdrs[CAPABILITY_HEADER] = capability
            elif auth:
                self._sync()
                if self.session.backend_token:
                    hdrs["Authorization"] = "Bearer " + self.session.backend_token
            try:
                resp, parsed = self._send_once(method, path, query, body, hdrs)
                err: BackendError | None = None
            except NetworkError as e:  # includes Timeout
                resp, parsed, err = None, None, e
            if resp is not None:
                if resp.status < 400:
                    return parsed
                err = error_from_response(resp.status, parsed, resp.headers)
                if resp.status == 401 and auth and not capability and allow_refresh:
                    if self._handle_401(err, method, refreshed) is not _RETRY:
                        raise err
                    refreshed = True
                    continue
            assert err is not None
            if method in SAFE and isinstance(err, (NetworkError, ColdStart)) and attempt < self.max_attempts:
                delay = min(self.backoff_cap, self.backoff_base * (2 ** (attempt - 1)))
                reason = ("timeout" if isinstance(err, Timeout) else
                          "network" if isinstance(err, NetworkError) else "cold-start")
                if self.on_wait:
                    self.on_wait(attempt, delay, reason)
                self._sleep(delay)
                continue
            raise err

    def _handle_401(self, err: BackendError, method: str, already_refreshed: bool):
        """Return _RETRY to replay the (safe) request; None means the caller raises Unauthorized."""
        if (err.code or "") in SESSION_DEAD_CODES or already_refreshed:
            self._drop_tokens()
            return None
        if not self._refresh_token or not self.session.backend_token:
            self._drop_tokens()
            return None
        try:
            self.refresh()
        except BackendError:
            return None  # refresh() clears the session on a definitive failure
        return _RETRY if method == "GET" else None

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _page(data: Any) -> Page:
        if isinstance(data, list):
            return Page(results=data, count=len(data))
        data = data or {}
        return Page(results=list(data.get("results", [])), count=int(data.get("count", 0)),
                    next=data.get("next"), previous=data.get("previous"))

    def _list(self, path: str, page: int | None = None, **filters) -> Page:
        return self._page(self._request("GET", path, query={"page": page, **filters}))

    def check_same_origin(self, url: str | None) -> str | None:
        """Validate a pagination `next` link: returns it if same origin as base_url, else raises."""
        if url is None:
            return None
        a, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(self.base_url)
        if (a.scheme, a.netloc) != (b.scheme, b.netloc):
            raise BackendError("Pagination link points to a different server; ignored.")
        return url

    # ---------------------------------------------------------------- health
    def health_live(self) -> dict:
        """GET /health/live/ -> {status, service}. No auth. MOCK-TESTED against recorded probe."""
        return self._request("GET", "/health/live/", auth=False)

    def health_ready(self) -> dict:
        """GET /health/ready/ -> {status, database, cache}; persistent 503 raises ColdStart."""
        return self._request("GET", "/health/ready/", auth=False)

    # ------------------------------------------------------------------ auth
    def login(self, username: str, password: str) -> dict:
        """POST /auth/token/ -> {id, username, role}. Tokens go to SessionStore/memory only.
        Never retried, never auto-refreshed. 401 Unauthorized, 429 Throttled. IMPLEMENTED-UNTESTED live."""
        data = self._request("POST", "/auth/token/", body={"username": username, "password": password},
                             auth=False, allow_refresh=False)
        if not isinstance(data, dict) or not data.get("access") or not data.get("refresh"):
            raise ServerError("Unexpected login response from server.")
        self.session.backend_token = data["access"]
        self._refresh_token = data["refresh"]
        user = data.get("user") or {}
        self._user = {k: user.get(k) for k in ("id", "username", "role")}
        return dict(self._user)

    def refresh(self) -> None:
        """POST /auth/token/refresh/ {refresh} -> {access, refresh}. Rotation: both replaced.
        On 400/401/403 (blacklisted/expired/revoked) the session is cleared and Unauthorized raised."""
        if not self._refresh_token:
            self._drop_tokens()
            raise Unauthorized("No refresh token; please sign in again.")
        try:
            data = self._request("POST", "/auth/token/refresh/", body={"refresh": self._refresh_token},
                                 auth=False, allow_refresh=False)
        except BackendError as e:
            if e.status in (400, 401, 403):
                self._drop_tokens()
                if not isinstance(e, Unauthorized):
                    raise Unauthorized("Session expired; please sign in again.", status=e.status, code=e.code)
            raise
        if not isinstance(data, dict) or not data.get("access"):
            self._drop_tokens()
            raise ServerError("Unexpected refresh response from server.")
        self.session.backend_token = data["access"]
        if data.get("refresh"):
            self._refresh_token = data["refresh"]

    def logout(self, revoke_all: bool = False) -> None:
        """POST /auth/logout/ {refresh, revoke_all?}. Local tokens are ALWAYS cleared, even on failure.
        Best-effort: never raises for server/network failure."""
        refresh, had_access = self._refresh_token, self.session.backend_token is not None
        try:
            if refresh and had_access:
                body: dict = {"refresh": refresh}
                if revoke_all:
                    body["revoke_all"] = True
                self._request("POST", "/auth/logout/", body=body, allow_refresh=False)
        except BackendError:
            pass
        finally:
            self._drop_tokens()

    def change_password(self, current_password: str, new_password: str) -> None:
        """POST /auth/change-password/ (IMPLEMENTED-UNTESTED). Server bumps token_version => session cleared."""
        self._request("POST", "/auth/change-password/",
                      body={"current_password": current_password, "new_password": new_password})
        self._drop_tokens()

    def register(self, username: str, password: str) -> dict:
        """POST /auth/register/ -> 201 {id, username, role:'USER'} (IMPLEMENTED-UNTESTED; 3/min throttle)."""
        return self._request("POST", "/auth/register/", body={"username": username, "password": password},
                             auth=False)

    def me(self) -> dict:
        """GET /auth/me/ -> {id, username, role, is_active, created_at, last_login_at}."""
        return self._request("GET", "/auth/me/")

    # ----------------------------------------------------------------- users
    def list_users(self, page: int | None = None) -> Page:
        """GET /admin/users/ (admin) -> Page of users."""
        return self._list("/admin/users/", page)

    def get_user(self, user_id: str) -> dict:
        """GET /admin/users/{id}/ (admin)."""
        return self._request("GET", f"/admin/users/{_u(user_id)}/")

    def create_user(self, username: str, password: str, role: str | None = None) -> dict:
        """POST /admin/users/ {username,password,role?} -> 201 user (IMPLEMENTED-UNTESTED;
        accounts/views.py:79-105)."""
        body = {"username": username, "password": password}
        if role:
            body["role"] = role
        return self._request("POST", "/admin/users/", body=body)

    def set_user_active(self, user_id: str, is_active: bool) -> dict:
        """PATCH /admin/users/{id}/ {is_active} (IMPLEMENTED-UNTESTED; accounts/views.py:108-158)."""
        return self._request("PATCH", f"/admin/users/{_u(user_id)}/", body={"is_active": bool(is_active)})

    def disable_user(self, user_id: str) -> dict:
        """POST /admin/users/{id}/disable/ (no body) -> {id,is_active:false} (IMPLEMENTED-UNTESTED; :161-203)."""
        return self._request("POST", f"/admin/users/{_u(user_id)}/disable/")

    def set_user_role(self, user_id: str, role: str) -> dict:
        """POST /admin/users/{id}/role/ {role: ADMIN|USER} -> {id,role} (IMPLEMENTED-UNTESTED; :206-258)."""
        return self._request("POST", f"/admin/users/{_u(user_id)}/role/", body={"role": role})

    # --------------------------------------------------------------- devices
    def list_devices(self, page: int | None = None) -> Page:
        """GET /devices/ (admin: all, user: own)."""
        return self._list("/devices/", page)

    def get_device(self, device_id: str) -> dict:
        return self._request("GET", f"/devices/{_u(device_id)}/")

    def create_device(self, device_label: str, device_public_id: str, protocol_version: int | str,
                      owner_user: str | None = None) -> dict:
        """POST /devices/ (admin) -> 201 Device status PENDING (IMPLEMENTED-UNTESTED; devices/views.py:18-48)."""
        body: dict = {"device_label": device_label, "device_public_id": device_public_id,
                      "protocol_version": protocol_version}
        if owner_user:
            body["owner_user"] = owner_user
        return self._request("POST", "/devices/", body=body)

    def set_device_status(self, device_id: str, status: str) -> dict:
        """POST /devices/{id}/status/ {status: PENDING|ACTIVE|REVOKED|LOST} (admin; IMPLEMENTED-UNTESTED).
        REVOKED is terminal."""
        return self._request("POST", f"/devices/{_u(device_id)}/status/", body={"status": status})

    def assign_device(self, device_id: str, owner_user: str | None) -> dict:
        """POST /devices/{id}/assign/ {owner_user: uuid|null} (admin; only while PENDING; IMPLEMENTED-UNTESTED)."""
        return self._request("POST", f"/devices/{_u(device_id)}/assign/", body={"owner_user": owner_user})

    # ----------------------------------------------------------------- files
    def list_files(self, page: int | None = None) -> Page:
        """GET /files/ -> Page of metadata (content_available is always false)."""
        return self._list("/files/", page)

    def get_file(self, file_id: str) -> dict:
        return self._request("GET", f"/files/{_u(file_id)}/")

    def create_file_metadata(self, owner_user_id: str, display_name: str) -> dict:
        """POST /files/metadata/ (admin) strict {owner_user_id, display_name<=255} -> 201 (IMPLEMENTED-UNTESTED;
        storage/views.py:49-58). Metadata only; the backend has no upload."""
        return self._request("POST", "/files/metadata/",
                             body={"owner_user_id": owner_user_id, "display_name": display_name})

    def get_file_content(self, file_id: str):
        """GET /files/{id}/content/ -> never returns: raises NotImplementedByBackend (503
        FILE_CONTENT_UNAVAILABLE; storage/views.py:108-115). Unauthenticated -> Unauthorized;
        not owner/READ-grantee -> NotFound."""
        try:
            self._request("GET", f"/files/{_u(file_id)}/content/")
        except ColdStart as e:  # a bare 503 on this route is the by-design 503
            raise NotImplementedByBackend("File content is not available on the backend.",
                                          status=e.status, code="FILE_CONTENT_UNAVAILABLE")
        raise NotImplementedByBackend("File content is not available on the backend.",
                                      status=503, code="FILE_CONTENT_UNAVAILABLE")

    def list_grants(self, file_id: str) -> list:
        """GET /files/{id}/grants/ -> plain list (NOT paginated)."""
        data = self._request("GET", f"/files/{_u(file_id)}/grants/")
        return data if isinstance(data, list) else list((data or {}).get("results", []))

    def create_grant(self, file_id: str, subject_user_id: str, permission: str,
                     expires_at: str | None = None) -> dict:
        """POST /files/{id}/grants/ strict {subject_user_id, permission: LIST|READ, expires_at?} -> 201
        (IMPLEMENTED-UNTESTED; storage/views.py:69-90)."""
        body: dict = {"subject_user_id": subject_user_id, "permission": permission}
        if expires_at:
            body["expires_at"] = expires_at
        return self._request("POST", f"/files/{_u(file_id)}/grants/", body=body)

    def revoke_grant(self, file_id: str, grant_id: str) -> dict:
        """POST /files/{id}/grants/{gid}/revoke/ with EMPTY body (else 400) (IMPLEMENTED-UNTESTED; :93-105)."""
        return self._request("POST", f"/files/{_u(file_id)}/grants/{_u(grant_id)}/revoke/")

    # ----------------------------------------------------------------- audit
    def list_audit_events(self, page: int | None = None, event_type: str | None = None) -> Page:
        """GET /audit/events/ (admin) optional ?event_type=."""
        return self._list("/audit/events/", page, event_type=event_type)

    def get_audit_event(self, event_id: str) -> dict:
        return self._request("GET", f"/audit/events/{_u(event_id)}/")

    # -------------------------------------------------------- offline policy
    def list_offline_policies(self, page: int | None = None) -> Page:
        return self._list("/offline-policies/", page)

    def get_offline_policy(self, policy_id: str) -> dict:
        return self._request("GET", f"/offline-policies/{_u(policy_id)}/")

    def revoke_offline_policy(self, policy_id: str) -> dict:
        """POST /offline-policies/{id}/revoke/ no body (owner/admin; IMPLEMENTED-UNTESTED; views.py:174-194)."""
        return self._request("POST", f"/offline-policies/{_u(policy_id)}/revoke/")

    def create_offline_policy(self, *_a, **_k):
        """Always raises NotImplementedByBackend: backend POST /offline-policies/ is 503
        OFFLINE_POLICY_ISSUANCE_DISABLED by design. No request is sent."""
        raise NotImplementedByBackend("Offline policy issuance is disabled on the backend.", status=503,
                                      code="OFFLINE_POLICY_ISSUANCE_DISABLED")

    # ------------------------------------------------------- hardware auth
    # Backend limitation: factor reports are stored verified=false and complete never issues a
    # session (hardware_auth/services.py:113-129,197-202). Never present these as verified auth.
    def hw_preauth(self, username: str, password: str, device_id: str) -> dict:
        """POST /auth/preauth/ -> {attempt, attempt_capability, capability_header}. No JWT issued.
        The capability is a secret: keep in memory, pass it to hw_* calls via `capability=`."""
        return self._request("POST", "/auth/preauth/", auth=False,
                             body={"username": username, "password": password, "device_id": device_id})

    def hw_create_attempt(self, device_id: str) -> dict:
        """POST /auth-attempts/ {device_id} -> 201 AuthAttempt (JWT only). IMPLEMENTED-UNTESTED."""
        return self._request("POST", "/auth-attempts/", body={"device_id": device_id})

    def hw_get_attempt(self, attempt_id: str, capability: str | None = None) -> dict:
        """GET /auth-attempts/{id}/ (JWT or capability)."""
        return self._request("GET", f"/auth-attempts/{_u(attempt_id)}/", capability=capability)

    def hw_report_event(self, attempt_id: str, device_id: str, factor: str, event_id: str,
                        capability: str | None = None) -> dict:
        """POST /auth-attempts/{id}/events/ {contract_version:1,event_id,device_id,factor} -> 201 with
        reported_event.verified == false ALWAYS (backend cannot verify). IMPLEMENTED-UNTESTED."""
        return self._request("POST", f"/auth-attempts/{_u(attempt_id)}/events/", capability=capability,
                             body={"contract_version": 1, "event_id": event_id, "device_id": device_id,
                                   "factor": factor})

    def hw_cancel_attempt(self, attempt_id: str, device_id: str, capability: str | None = None) -> dict:
        """POST /auth-attempts/{id}/cancel/ {device_id} -> AuthAttempt CANCELLED (JWT or capability)."""
        return self._request("POST", f"/auth-attempts/{_u(attempt_id)}/cancel/", capability=capability,
                             body={"device_id": device_id})

    def hw_complete_attempt(self, attempt_id: str, device_id: str) -> dict:
        """POST /auth-attempts/{id}/complete/ {device_id}. UNSUPPORTED by the backend: always 409
        FACTOR_VERIFICATION_UNAVAILABLE => raises NotImplementedByBackend; no session is issued."""
        return self._request("POST", f"/auth-attempts/{_u(attempt_id)}/complete/", body={"device_id": device_id})
