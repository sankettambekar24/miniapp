"""Local enrollment store (SQLite, %LOCALAPPDATA%\\HSS\\hss_local.db, schema v2).

A user is ENROLLED only when ALL THREE exist: a fingerprint ID on the device, an encrypted face template here, and a PIN
record on the device. The PIN itself/its hash never reaches the PC (the device derives and stores it); `pin_set` is only the
host's note that the device confirmed `PIN_SET_OK`. Face templates are DPAPI-encrypted 128-float SFace embeddings
(no raw photos). Role comes ONLY from this table (record of the `AUTH_OK:<id>` user).
"""
from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Callable

from . import clock, dpapi
from .localdb import LocalDb
from .session import Identity

ENROLLED, INCOMPLETE, DISABLED, REVOKED = "ENROLLED", "INCOMPLETE", "DISABLED", "REVOKED"
ROLES = ("admin", "user")

CONSENT_VERSION = "2026-11-03-v1"
CONSENT_TEXT = (
    "Face enrollment consent. This app will use the LAPTOP WEBCAM (development/test camera) to capture several frames of your "
    "face and compute a numeric face template. No photo or video is stored. The template is encrypted for this Windows user "
    "account and kept on this PC until an administrator revokes you. It is used only to compare your face at login. "
    "This is a test-grade face check: it has no liveness / anti-spoofing protection and does not prove your identity by itself. "
    "You can ask an administrator to revoke and delete your template at any time."
)


class EnrollmentError(Exception):
    pass


@dataclass(frozen=True)
class UserRecord:
    user_id: str
    name: str
    role: str
    fingerprint_id: int | None
    has_face: bool
    pin_set: bool
    status: str
    device_cleanup_pending: bool
    created_at: str
    updated_at: str

    @property
    def complete(self) -> bool:
        return self.fingerprint_id is not None and self.has_face and self.pin_set


class EnrollmentStore:
    def __init__(self, db: LocalDb, protect: Callable[[bytes], bytes] = dpapi.protect,
                 unprotect: Callable[[bytes], bytes] = dpapi.unprotect):
        self.db, self._protect, self._unprotect = db, protect, unprotect

    # ---- helpers ----
    @staticmethod
    def _rec(r: sqlite3.Row) -> UserRecord:
        return UserRecord(r["user_id"], r["name"], r["role"], r["fingerprint_id"], r["face_template"] is not None,
                          bool(r["pin_set"]), r["status"], bool(r["device_cleanup_pending"]), r["created_at"], r["updated_at"])

    def _one(self, sql: str, args=()) -> UserRecord | None:
        with self.db.lock:
            r = self.db.conn.execute(sql, args).fetchone()
        return self._rec(r) if r else None

    def _touch(self, user_id: str, **cols) -> None:
        sets = ", ".join(f"{k}=?" for k in cols) + (", " if cols else "") + "updated_at=?"
        with self.db.lock:
            cur = self.db.conn.execute(f"UPDATE enrolled_users SET {sets} WHERE user_id=?", [*cols.values(), clock.to_iso(), user_id])
            if cur.rowcount != 1:
                raise EnrollmentError("unknown user")

    def _recompute(self, user_id: str) -> UserRecord:
        rec = self.get(user_id)
        if rec and rec.status == INCOMPLETE and rec.complete:
            self._touch(user_id, status=ENROLLED)       # the ONLY path to ENROLLED
        return self.get(user_id)

    # ---- queries ----
    def get(self, user_id: str) -> UserRecord | None:
        return self._one("SELECT * FROM enrolled_users WHERE user_id=?", (user_id,))

    def by_fingerprint_id(self, fp_id: int) -> UserRecord | None:
        return self._one("SELECT * FROM enrolled_users WHERE fingerprint_id=?", (int(fp_id),))

    def list_all(self) -> list[UserRecord]:
        with self.db.lock:
            rows = self.db.conn.execute("SELECT * FROM enrolled_users ORDER BY created_at, user_id").fetchall()
        return [self._rec(r) for r in rows]

    def has_enrolled_admin(self) -> bool:
        with self.db.lock:
            return self.db.conn.execute("SELECT 1 FROM enrolled_users WHERE role='admin' AND status=?", (ENROLLED,)).fetchone() is not None

    # ---- lifecycle ----
    def create(self, name: str, role: str) -> UserRecord:
        name = (name or "").strip()
        if not 2 <= len(name) <= 64:
            raise EnrollmentError("name must be 2-64 characters")
        if role not in ROLES:
            raise EnrollmentError("role must be admin or user")
        uid, now = uuid.uuid4().hex, clock.to_iso()
        with self.db.lock:
            self.db.conn.execute("INSERT INTO enrolled_users (user_id,name,role,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                                 (uid, name, role, INCOMPLETE, now, now))
        return self.get(uid)

    def set_fingerprint(self, user_id: str, fp_id: int) -> UserRecord:
        other = self.by_fingerprint_id(fp_id)
        if other and other.user_id != user_id:
            raise EnrollmentError("fingerprint id already assigned to another user")
        self._touch(user_id, fingerprint_id=int(fp_id))
        return self._recompute(user_id)

    def add_consent(self, user_id: str, captured_by: str, camera: str) -> int:
        with self.db.lock:
            cur = self.db.conn.execute(
                "INSERT INTO face_consents (user_id,ts,captured_by,text_version,text_sha256,camera) VALUES (?,?,?,?,?,?)",
                (user_id, clock.to_iso(), captured_by, CONSENT_VERSION, hashlib.sha256(CONSENT_TEXT.encode()).hexdigest(), camera))
            return cur.lastrowid

    def consents(self, user_id: str) -> list[dict]:
        with self.db.lock:
            return [dict(r) for r in self.db.conn.execute("SELECT * FROM face_consents WHERE user_id=? ORDER BY id", (user_id,))]

    def set_face_template(self, user_id: str, template: bytes, consent_id: int) -> UserRecord:
        """Encrypts with DPAPI BEFORE it touches the database. Requires a recorded consent."""
        with self.db.lock:
            ok = self.db.conn.execute("SELECT 1 FROM face_consents WHERE id=? AND user_id=?", (consent_id, user_id)).fetchone()
        if not ok:
            raise EnrollmentError("no consent record for this user: face enrollment refused")
        self._touch(user_id, face_template=self._protect(bytes(template)), consent_id=consent_id)
        return self._recompute(user_id)

    def face_template(self, user_id: str) -> bytes | None:
        with self.db.lock:
            r = self.db.conn.execute("SELECT face_template FROM enrolled_users WHERE user_id=?", (user_id,)).fetchone()
        return self._unprotect(bytes(r[0])) if r and r[0] is not None else None

    def set_pin_confirmed(self, user_id: str) -> UserRecord:
        self._touch(user_id, pin_set=1)
        return self._recompute(user_id)

    def disable(self, user_id: str) -> UserRecord:
        rec = self.get(user_id)
        if not rec or rec.status in (REVOKED,):
            raise EnrollmentError("cannot disable this user")
        self._touch(user_id, status=DISABLED)
        return self.get(user_id)

    def enable(self, user_id: str) -> UserRecord:
        rec = self.get(user_id)
        if not rec or rec.status != DISABLED:
            raise EnrollmentError("only a DISABLED user can be enabled")
        self._touch(user_id, status=ENROLLED if rec.complete else INCOMPLETE)
        return self.get(user_id)

    def revoke(self, user_id: str, device_cleanup_pending: bool = False) -> UserRecord:
        """Terminal. Face template and fingerprint mapping are deleted from the PC immediately."""
        if not self.get(user_id):
            raise EnrollmentError("unknown user")
        self._touch(user_id, status=REVOKED, fingerprint_id=None, face_template=None, pin_set=0,
                    device_cleanup_pending=int(device_cleanup_pending))
        return self.get(user_id)

    def clear_cleanup_pending(self, user_id: str) -> None:
        self._touch(user_id, device_cleanup_pending=0)

    def reset_for_reenroll(self, user_id: str) -> UserRecord:
        rec = self.get(user_id)
        if not rec or rec.status == REVOKED:
            raise EnrollmentError("a REVOKED user cannot be re-enrolled: create a new user")
        self._touch(user_id, status=INCOMPLETE, fingerprint_id=None, face_template=None, pin_set=0)
        return self.get(user_id)

    def mark_all_incomplete_except(self, keep_user_id: str) -> int:
        """After a device store reset (bootstrap): the device holds no PIN records, so nobody else can log in."""
        with self.db.lock:
            cur = self.db.conn.execute(
                "UPDATE enrolled_users SET status=?, fingerprint_id=NULL, pin_set=0, updated_at=? WHERE user_id<>? AND status IN (?,?)",
                (INCOMPLETE, clock.to_iso(), keep_user_id, ENROLLED, DISABLED))
            return cur.rowcount


class EnrollmentResolver:
    """AuthFSM identity source for the three-factor build: fingerprint id -> Identity, ONLY for ENROLLED users."""

    warning = None

    def __init__(self, store: EnrollmentStore):
        self.store = store

    def resolve(self, fp_id: int | None) -> Identity | None:
        if fp_id is None:
            return None
        rec = self.store.by_fingerprint_id(fp_id)
        if rec is None or rec.status != ENROLLED or not rec.complete:
            return None
        return Identity(int(fp_id), rec.name, rec.role, rec.user_id)

    def items(self) -> list[tuple[int, str, str]]:
        return [(r.fingerprint_id, r.name, r.role) for r in self.store.list_all() if r.status == ENROLLED and r.fingerprint_id]
