"""Admin service layer. Every function re-checks the PIN-session role HERE (controller layer), not in widgets.

Enforcement labels (documented in FEATURE_PARITY.md):
  * the role check in this module is CLIENT-ENFORCED-ONLY (defense in depth; anyone who patches the app skips it);
  * what the deployed backend enforces itself is BACKEND-ENFORCED only where source/live evidence says so.
Passwords are passed straight to the backend client (HTTPS only) and are never stored, logged or audited.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .audit import (DENIED, FAILURE, SUCCESS, AuditFilter, AuditRow, AuditStore, backend_row, matches, merge)
from .auth_fsm import AuthState
from .devices import DeviceRow, backend_rows

ROLES = ("USER", "ADMIN")
MAX_USERNAME = 64   # backend AdminUserCreateSerializer.validate_username
_USERNAME = re.compile(r"^[A-Za-z0-9_.@+\-]{3,64}$")
MIN_PASSWORD = 10   # backend serializer min_length=10 (+ Django validators decide the rest)


class ServiceError(Exception):
    code = "ERROR"

    def __init__(self, message: str = ""):
        super().__init__(message or self.code)


class PermissionDenied(ServiceError):
    code = "FORBIDDEN"


class NotSignedIn(ServiceError):
    code = "NOT_SIGNED_IN"


class ValidationError(ServiceError):
    code = "VALIDATION"

    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field


@dataclass(frozen=True)
class AccessChange:
    """Desired LOCAL access of one user to one SD file (Read = list + preview, Download = save a copy)."""
    user_id: str
    username: str
    read: bool
    download: bool = False


def validate_username(u: str) -> str:
    u = (u or "").strip()
    if not _USERNAME.match(u):
        raise ValidationError("username", "3-64 characters: letters, digits and _ . @ + -")
    return u


def validate_password(p: str, username: str = "") -> str:
    if not p or len(p) < MIN_PASSWORD:
        raise ValidationError("password", f"at least {MIN_PASSWORD} characters")
    if username and p.lower() == username.lower():
        raise ValidationError("password", "must differ from the username")
    return p


def validate_role(r: str) -> str:
    r = (r or "").strip().upper()
    if r not in ROLES:
        raise ValidationError("role", "must be USER or ADMIN")
    return r


class AdminService:
    def __init__(self, ctl):
        self.ctl = ctl

    # ---- guards ----
    def _admin(self, action: str, resource: str = "") -> None:
        ctl = self.ctl
        ident = ctl.session.identity
        if ctl.fsm.state is not AuthState.SESSION_ACTIVE or ident is None or ident.role != "admin":
            ctl.audit_event(action, resource=resource, status=DENIED, details="admin role required (client check)")
            raise PermissionDenied("This action requires an authenticated admin session.")

    def _client(self):
        c = self.ctl.backend
        if c is None or not c.is_authenticated:
            raise NotSignedIn("Not signed in to the backend (PIN test mode has no backend access; production needs the password login).")
        return c

    def _run(self, action: str, resource: str, fn, details: str = ""):
        """Execute a backend call, auditing success/failure (no secrets in `details`)."""
        try:
            res = fn()
        except Exception as exc:
            self.ctl.audit_event(action, resource=resource, status=FAILURE, details=f"{details} {type(exc).__name__}: {exc}".strip())
            raise
        self.ctl.audit_event(action, resource=resource, status=SUCCESS, details=details)
        return res

    # ---- users (BACKEND routes: GET/POST /admin/users/...; writes are LIVE and need approval before real use) ----
    def local_test_identities(self) -> list[tuple[int, str, str]]:
        """PIN-test-mode identities: read-only, LOCAL TEST. (slot, identity, role)."""
        self._admin("LOCAL_IDENTITIES_VIEW")
        return self.ctl.resolver.items()

    def list_users(self, page: int | None = None):
        self._admin("USER_LIST", "users")
        return self._run("USER_LIST", "users", lambda: self._client().list_users(page))

    def create_user(self, username: str, password: str, role: str = "USER") -> dict:
        self._admin("USER_CREATE", username)
        u, r = validate_username(username), validate_role(role)
        validate_password(password, u)
        return self._run("USER_CREATE", u, lambda: self._client().create_user(u, password, r), f"role={r}")

    def set_user_active(self, user_id: str, active: bool, label: str = "") -> dict:
        action = "USER_ENABLE" if active else "USER_DISABLE"
        self._admin(action, label or user_id)
        if not user_id:
            raise ValidationError("user_id", "required")
        c = self._client()
        fn = lambda: c.set_user_active(user_id, active)   # PATCH /admin/users/{id}/ {is_active} for both directions
        return self._run(action, label or user_id, fn)

    def set_user_role(self, user_id: str, role: str, label: str = "") -> dict:
        self._admin("USER_ROLE_CHANGE", label or user_id)
        r = validate_role(role)
        return self._run("USER_ROLE_CHANGE", label or user_id, lambda: self._client().set_user_role(user_id, r), f"role={r}")

    # ---- devices ----
    def list_devices(self) -> tuple[list[DeviceRow], str]:
        """(rows, note). Local USB history always; backend registry only when signed in."""
        self._admin("DEVICE_HISTORY_VIEW", "devices")
        rows = list(self.ctl.device_history.local_rows())
        note = ""
        c = self.ctl.backend
        if c is not None and c.is_authenticated:
            try:
                devs, users, page = [], {}, None
                for _ in range(20):
                    pg = c.list_devices(page)
                    devs += [d for d in pg.results if isinstance(d, dict)]
                    if not pg.next:
                        break
                    page = (page or 1) + 1
                try:
                    users = {str(u.get("id")): str(u.get("username")) for u in c.list_users().results if isinstance(u, dict)}
                except Exception:
                    users = {}
                rows += backend_rows(devs, users)
            except Exception as exc:
                note = f"Backend devices unavailable: {type(exc).__name__}: {exc}"
        else:
            note = "Backend devices not shown: not signed in to the backend (local USB history only)."
        return rows, note

    # ---- audit ----
    def list_audit(self, flt: AuditFilter, page: int = 1) -> tuple[list[AuditRow], int, str]:
        """Merged page = LOCAL rows (50/page, filtered in SQL) + the matching BACKEND page (<=50, read-only GET,
        filtered client-side). Returns (rows newest first, local total, note)."""
        self._admin("AUDIT_VIEW", "audit")
        local, total = self.ctl.audit.query(flt, page, 50)
        back, note = [], ""
        c = self.ctl.backend
        if c is not None and c.is_authenticated:
            try:
                pg = c.list_audit_events(page, None)
                back = [r for r in (backend_row(e) for e in pg.results if isinstance(e, dict)) if matches(r, flt)]
            except Exception as exc:
                note = f"Backend audit unavailable: {type(exc).__name__}: {exc}"
        else:
            note = "Backend audit not shown: not signed in to the backend (LOCAL rows only)."
        return merge(local, back), total, note

    def verify_local_audit(self) -> tuple[bool, int | None]:
        self._admin("AUDIT_VERIFY", "audit")
        return self.ctl.audit.verify()

    # ---- per-file access (SD card page): LOCAL permissions, CLIENT-ENFORCED-ONLY ----
    def permission_snapshot(self, display_name: str) -> list[dict]:
        """Users that can be given access (role USER) with their current Read/Download for this file."""
        self._admin("PERMISSIONS_VIEW", display_name)
        have = self.ctl.permissions.for_file(display_name)
        return [{"id": sid, "username": name, "role": "user", "read": have.get(sid, (False, False))[0],
                 "download": have.get(sid, (False, False))[1]} for sid, name in self.ctl.subjects()]

    def apply_permissions(self, display_name: str, changes: list["AccessChange"]) -> list[str]:
        self._admin("PERMISSION_CHANGE", display_name)
        done: list[str] = []
        valid = {sid for sid, _n in self.ctl.subjects()}
        for ch in changes:
            if ch.user_id not in valid:
                raise ValidationError("user", f"{ch.username!r} is not an enrolled user")
            read, dl = bool(ch.read), bool(ch.download and ch.read)   # Download needs Read in this UI
            cur = self.ctl.permissions.for_file(display_name).get(ch.user_id, (False, False))
            if (read, dl) == cur:
                continue
            self.ctl.permissions.set(ch.user_id, display_name, read, dl)
            self.ctl.audit_event("PERMISSION_CHANGE", resource=f"{display_name} -> {ch.username}",
                                 details=f"read={read}; download={dl} (LOCAL permission)")
            done.append(f"{ch.username}: read={'yes' if read else 'no'}, download={'yes' if dl else 'no'}")
        return done


class FilePermissions:
    """LOCAL per-user, per-file Read / Download (SQLite). Default = no row = NO access (fail closed). CLIENT-ENFORCED-ONLY:
    the firmware has no per-file permissions and the backend file-content endpoint is BLOCKED (503)."""

    def __init__(self, db):
        self.db = db

    def set(self, user_id: str, file: str, read: bool, download: bool) -> None:
        from . import clock
        with self.db.lock:
            if not read and not download:
                self.db.conn.execute("DELETE FROM file_permissions WHERE user_id=? AND file=?", (user_id, file.upper()))
            else:
                self.db.conn.execute(
                    "INSERT INTO file_permissions (user_id,file,can_read,can_download,updated_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(user_id,file) DO UPDATE SET can_read=excluded.can_read, can_download=excluded.can_download, "
                    "updated_at=excluded.updated_at", (user_id, file.upper(), int(read), int(download), clock.to_iso()))

    def for_user(self, user_id: str) -> dict[str, tuple[bool, bool]]:
        with self.db.lock:
            return {r[0]: (bool(r[1]), bool(r[2])) for r in self.db.conn.execute(
                "SELECT file, can_read, can_download FROM file_permissions WHERE user_id=?", (user_id,))}

    def for_file(self, file: str) -> dict[str, tuple[bool, bool]]:
        with self.db.lock:
            return {r[0]: (bool(r[1]), bool(r[2])) for r in self.db.conn.execute(
                "SELECT user_id, can_read, can_download FROM file_permissions WHERE file=?", (file.upper(),))}
