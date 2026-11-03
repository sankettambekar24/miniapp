"""LOCAL append-only audit store (SQLite) + merge with read-only backend audit events.

* No UPDATE/DELETE code path exists, and SQL triggers abort both. Rows are hash-chained (sha256 over the previous
  hash and the row fields); verify() detects edits made outside the app. LOCAL rows are NOT tamper-proof against
  someone with file access (they can rebuild the whole chain): they are a convenience trail, not evidence.
* Every string is redacted (PINs, passwords, tokens, Authorization headers) BEFORE it is stored.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
import ipaddress
from datetime import datetime, timedelta, timezone

from ..backend.redact import redact_text
from . import clock
from .localdb import LocalDb

GENESIS = "0" * 64
_PIN = re.compile(r"(?i)\b(pin|passcode)\b(\s*(?:is|=|:)?\s*)\d{3,}")
NO_IP = "n/a"
NO_IP_TIP = ("n/a: the backend returned no IP for this record. Only the backend can see a client IP (as seen by Render's "
             "proxy, i.e. the public address, never this PC's private LAN IP); this app never invents one.")


def valid_ip(value) -> str:
    """An IP exactly as returned by the backend, or 'n/a'. Never fabricated."""
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except (ValueError, TypeError):
        return NO_IP


_COLS = ("id", "ts", "username", "user_id", "role", "action", "resource", "device", "status", "details")


def redact_audit(text) -> str:
    return _PIN.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", redact_text("" if text is None else text))


@dataclass
class AuditFilter:
    user: str = ""
    action: str = ""
    since: str = ""          # YYYY-MM-DD in IST (as displayed), inclusive
    until: str = ""          # YYYY-MM-DD in IST (as displayed), inclusive
    status: str = ""         # "", "success", "failure"
    text: str = ""


@dataclass
class AuditRow:
    ts: str
    username: str
    user_id: str
    role: str
    action: str
    resource: str
    device: str
    status: str
    details: str
    source: str              # "LOCAL" | "BACKEND"
    extra: dict = field(default_factory=dict)
    ip: str = NO_IP

    @property
    def display_time(self) -> str:
        return clock.format_ist(self.ts)

    @property
    def date(self) -> str:
        return self.display_time[:11]

    @property
    def time(self) -> str:
        return self.display_time[12:]


SUCCESS = "success"
FAILURE = "failure"
DENIED = "denied"


def _ist_day_bounds(since: str, until: str) -> tuple[str | None, str | None]:
    """IST calendar days -> [lo, hi) as UTC ISO strings (rows are stored in UTC)."""
    def day(t: str):
        try:
            return datetime.strptime(t.strip(), "%Y-%m-%d").replace(tzinfo=clock.IST)
        except ValueError:
            return None
    lo, hi = day(since), day(until)
    return (clock.to_iso(lo) if lo else None, clock.to_iso(hi + timedelta(days=1)) if hi else None)


class AuditStore:
    def __init__(self, db: LocalDb):
        self.db = db

    # ---- write (the ONLY mutating path) ----
    def append(self, action: str, *, username: str | None = None, user_id: str | None = None, role: str | None = None,
               resource: str | None = None, device: str | None = None, status: str = SUCCESS,
               details: str | None = None, ts: str | None = None) -> int:
        f = [redact_audit(x) if x is not None else "" for x in (username, user_id, role, action, resource, device,
                                                                  status, details)]
        ts = ts or clock.to_iso()  # laptop clock, UTC (the firmware has no RTC)
        with self.db.lock:
            row = self.db.conn.execute("SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            prev = row[0] if row else GENESIS
            h = self._hash(prev, ts, f)
            cur = self.db.conn.execute(
                "INSERT INTO audit_log (ts,username,user_id,role,action,resource,device,status,details,prev_hash,row_hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)", (ts, f[0], f[1], f[2], f[3], f[4], f[5], f[6], f[7], prev, h))
            return cur.lastrowid

    @staticmethod
    def _hash(prev: str, ts: str, fields: list[str]) -> str:
        return hashlib.sha256(json.dumps([prev, ts, *fields], ensure_ascii=True).encode()).hexdigest()

    # ---- read ----
    @staticmethod
    def _where(f: AuditFilter) -> tuple[str, list]:
        c, a = [], []
        if f.user:
            c.append("(username LIKE ? OR user_id LIKE ?)")
            a += [f"%{f.user}%"] * 2
        if f.action:
            c.append("action LIKE ?")
            a.append(f"%{f.action}%")
        lo, hi = _ist_day_bounds(f.since, f.until)
        if lo:
            c.append("ts >= ?")
            a.append(lo)
        if hi:
            c.append("ts < ?")
            a.append(hi)
        if f.status == "success":
            c.append("status = 'success'")
        elif f.status == "failure":
            c.append("status IN ('failure','denied')")
        if f.text:
            c.append("(username||' '||action||' '||resource||' '||device||' '||details) LIKE ?")
            a.append(f"%{f.text}%")
        return (" WHERE " + " AND ".join(c)) if c else "", a

    def query(self, f: AuditFilter | None = None, page: int = 1, page_size: int = 50) -> tuple[list[AuditRow], int]:
        f = f or AuditFilter()
        where, args = self._where(f)
        with self.db.lock:
            total = self.db.conn.execute("SELECT COUNT(*) FROM audit_log" + where, args).fetchone()[0]
            rows = self.db.conn.execute(
                f"SELECT {','.join(_COLS)} FROM audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*args, page_size, max(page - 1, 0) * page_size]).fetchall()
        return [AuditRow(r["ts"], r["username"], r["user_id"], r["role"], r["action"], r["resource"], r["device"],
                         r["status"], r["details"], "LOCAL", {"id": r["id"]}) for r in rows], total

    def verify(self) -> tuple[bool, int | None]:
        """(True, None) if the whole chain is intact, else (False, id of the first bad row)."""
        prev = GENESIS
        with self.db.lock:
            rows = self.db.conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        for r in rows:
            f = [r[k] for k in ("username", "user_id", "role", "action", "resource", "device", "status", "details")]
            if r["prev_hash"] != prev or r["row_hash"] != self._hash(prev, r["ts"], f):
                return False, r["id"]
            prev = r["row_hash"]
        return True, None


# ---- backend rows (read-only) ----
def backend_row(ev: dict) -> AuditRow:
    meta = ev.get("safe_metadata")
    dt = clock.parse_utc(ev.get("occurred_at"))  # backend times are UTC
    ts = clock.to_iso(dt) if dt else str(ev.get("occurred_at") or "")
    ip = valid_ip(meta.get("ip")) if isinstance(meta, dict) else NO_IP  # only LOGIN_LOCKED_OUT carries one (source)
    return AuditRow(ts, str(ev.get("actor_username") or ""), "n/a", "n/a",
                    str(ev.get("event_type") or ""), "n/a", "n/a", str(ev.get("result") or ""),
                    redact_audit(json.dumps(meta, sort_keys=True) if meta else ""), "BACKEND",
                    {"id": ev.get("id"), "correlation_id": ev.get("correlation_id")}, ip)


def matches(row: AuditRow, f: AuditFilter) -> bool:
    """Client-side filter used for BACKEND rows (the backend only filters by event_type)."""
    if f.user and f.user.lower() not in (row.username + " " + row.extra.get("user_id", "")).lower():
        return False
    if f.action and f.action.lower() not in row.action.lower():
        return False
    lo, hi = _ist_day_bounds(f.since, f.until)
    if (lo and row.ts < lo) or (hi and row.ts >= hi):
        return False
    ok = row.status.lower() in ("success", "ok", "succeeded", "allowed")
    if f.status == "success" and not ok:
        return False
    if f.status == "failure" and ok:
        return False
    if f.text and f.text.lower() not in " ".join((row.username, row.action, row.resource, row.details)).lower():
        return False
    return True


def merge(local: list[AuditRow], backend: list[AuditRow]) -> list[AuditRow]:
    return sorted(local + backend, key=lambda r: r.ts, reverse=True)
