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
_USERNAME = re.compile(r"^[A-Za-z0-9_.@+\-]{3,150}$")
MIN_PASSWORD = 8


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
    """Desired access of one backend user to one file. read -> LIST+READ backend grants (both are needed: LIST to
    see the file, READ for detail/content). download_blocked is a LOCAL flag (backend has no download grant)."""
    user_id: str
    username: str
    read: bool
    download_blocked: bool = False


def validate_username(u: str) -> str:
    u = (u or "").strip()
    if not _USERNAME.match(u):
        raise ValidationError("username", "3-150 characters: letters, digits and _ . @ + -")
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
            raise NotSignedIn("Sign in to the backend first (Backend tab).")
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
        fn = (lambda: c.set_user_active(user_id, True)) if active else (lambda: c.disable_user(user_id))
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

    # ---- per-file access (SD card page) ----
    def file_access_snapshot(self, display_name: str) -> dict:
        """Blocking (network). {file, users:[{id,username,role,read,download_blocked}]} for the permissions dialog."""
        self._admin("PERMISSIONS_VIEW", display_name)
        c = self._client()
        file, page = None, None
        for _ in range(20):
            pg = c.list_files(page)
            for rec in pg.results:
                if isinstance(rec, dict) and str(rec.get("display_name", "")).upper() == display_name.upper():
                    file = rec
            if file or not pg.next:
                break
            page = (page or 1) + 1
        users, page = [], None
        for _ in range(20):
            pg = c.list_users(page)
            users += [u for u in pg.results if isinstance(u, dict)]
            if not pg.next:
                break
            page = (page or 1) + 1
        grants = c.list_grants(str(file["id"])) if file else []
        active = {}
        for g in grants:
            if isinstance(g, dict) and g.get("active", g.get("revoked_at") is None):
                active.setdefault(str(g.get("subject_user_id")), set()).add(str(g.get("permission")))
        blocked = self.ctl.download_flags.blocked_subjects(display_name)
        return {"file": file, "users": [
            {"id": str(u.get("id")), "username": str(u.get("username")), "role": str(u.get("role")),
             "read": "READ" in active.get(str(u.get("id")), ()) and "LIST" in active.get(str(u.get("id")), ()),
             "download_blocked": str(u.get("id")) in blocked, "grants": [g for g in grants if isinstance(g, dict)
                                                                          and str(g.get("subject_user_id")) == str(u.get("id"))]}
            for u in users]}

    def create_file_metadata(self, display_name: str) -> dict:
        """Create the backend record that the SD file maps to (display_name == SD path); owner = the signed-in admin."""
        self._admin("FILE_METADATA_CREATE", display_name)
        c = self._client()
        owner = (c.current_user or {}).get("id")
        if not owner:
            raise NotSignedIn("Backend user id unknown; sign in again.")
        return self._run("FILE_METADATA_CREATE", display_name, lambda: c.create_file_metadata(str(owner), display_name))

    def apply_file_access(self, file_id: str, display_name: str, snapshot_users: list[dict],
                          changes: list[AccessChange]) -> list[str]:
        """Blocking. Applies only DIFFERENCES vs the snapshot. Returns human-readable log lines."""
        self._admin("PERMISSION_CHANGE", display_name)
        c = self._client()
        by_id = {u["id"]: u for u in snapshot_users}
        done: list[str] = []
        for ch in changes:
            cur = by_id.get(ch.user_id, {"read": False, "download_blocked": False, "grants": []})
            res = f"{display_name} -> {ch.username}"
            if ch.read and not cur["read"]:
                have = {g.get("permission") for g in cur["grants"] if g.get("active", g.get("revoked_at") is None)}
                for perm in ("LIST", "READ"):
                    if perm not in have:
                        self._run("PERMISSION_GRANT", res, lambda p=perm: c.create_grant(file_id, ch.user_id, p), perm)
                done.append(f"granted Read (LIST+READ) to {ch.username}")
            elif not ch.read and cur["read"]:
                for g in cur["grants"]:
                    if g.get("active", g.get("revoked_at") is None) and g.get("permission") in ("LIST", "READ"):
                        self._run("PERMISSION_REVOKE", res, lambda gid=str(g["id"]): c.revoke_grant(file_id, gid),
                                  str(g.get("permission")))
                done.append(f"revoked Read from {ch.username}")
            want_block = ch.download_blocked if ch.read else False
            if want_block != cur["download_blocked"] or (not ch.read and cur["download_blocked"]):
                self.ctl.download_flags.set_blocked(ch.user_id, display_name, want_block)
                self.ctl.audit_event("PERMISSION_DOWNLOAD_FLAG", resource=res, status=SUCCESS,
                                     details=f"download {'blocked' if want_block else 'allowed'} (LOCAL flag)")
                done.append(f"download {'blocked' if want_block else 'allowed'} for {ch.username} (LOCAL)")
        return done


class DownloadFlags:
    """LOCAL download-blocked flags (CLIENT-ENFORCED-ONLY). Default: no row => download follows READ."""

    def __init__(self, db):
        self.db = db

    def set_blocked(self, subject: str, file: str, blocked: bool) -> None:
        from datetime import datetime, timezone
        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO download_flags (subject,file,download_blocked,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(subject,file) DO UPDATE SET download_blocked=excluded.download_blocked, updated_at=excluded.updated_at",
                (subject, file.upper(), int(blocked), datetime.now(timezone.utc).isoformat()))

    def blocked_for(self, subject: str) -> set[str]:
        with self.db.lock:
            return {r[0] for r in self.db.conn.execute(
                "SELECT file FROM download_flags WHERE subject=? AND download_blocked=1", (subject,))}

    def blocked_subjects(self, file: str) -> set[str]:
        with self.db.lock:
            return {r[0] for r in self.db.conn.execute(
                "SELECT subject FROM download_flags WHERE file=? AND download_blocked=1", (file.upper(),))}
