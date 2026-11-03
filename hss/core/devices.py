"""Device history: LOCAL persistent USB history (SQLite) merged with the backend device registry.

Records are never deleted on disconnect. Dedup key: VID+PID+serial number, else VID+PID+port description.
Status: ACTIVE (connected now), DISCONNECTED (seen within INACTIVE_AFTER_DAYS), INACTIVE (older), REVOKED (backend).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .discovery import PortInfo
from .localdb import LocalDb

INACTIVE_AFTER_DAYS = 30
DEVICE_TYPE = "USB serial (STM32 HSS board)"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def device_key(p: PortInfo) -> str:
    vid, pid = (p.vid or 0), (p.pid or 0)
    if p.serial_number:
        return f"{vid:04x}:{pid:04x}:{p.serial_number}"
    return f"{vid:04x}:{pid:04x}:desc:{p.description}"


@dataclass
class DeviceRow:
    device_id: str
    name: str
    user: str
    dev_type: str
    ip: str
    first_seen: str
    last_seen: str
    status: str
    source: str          # "BACKEND" | "LOCAL"


class DeviceHistory:
    def __init__(self, db: LocalDb):
        self.db = db

    def connected(self, p: PortInfo, user: str | None = None, now: str | None = None) -> None:
        now, key = now or _now(), device_key(p)
        with self.db.lock:
            row = self.db.conn.execute("SELECT id FROM device_history WHERE dev_key=?", (key,)).fetchone()
            if row:
                self.db.conn.execute("UPDATE device_history SET last_seen=?, connected=1, port=?, description=? WHERE id=?",
                                     (now, p.device, p.description, row[0]))
            else:
                self.db.conn.execute(
                    "INSERT INTO device_history (dev_key,vid,pid,serial,description,port,name,device_type,first_seen,"
                    "last_seen,last_user,connected) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                    (key, p.vid, p.pid, p.serial_number, p.description, p.device,
                     f"HSS board on {p.device}", DEVICE_TYPE, now, now, user))

    def disconnected(self, p: PortInfo | None, now: str | None = None) -> None:
        if p is None:
            return
        with self.db.lock:
            self.db.conn.execute("UPDATE device_history SET connected=0, last_seen=? WHERE dev_key=?",
                                 (now or _now(), device_key(p)))

    def set_user(self, p: PortInfo | None, user: str) -> None:
        if p is not None:
            with self.db.lock:
                self.db.conn.execute("UPDATE device_history SET last_user=? WHERE dev_key=?", (user, device_key(p)))

    def local_rows(self, now: datetime | None = None) -> list[DeviceRow]:
        now = now or datetime.now(timezone.utc)
        with self.db.lock:
            recs = self.db.conn.execute("SELECT * FROM device_history ORDER BY last_seen DESC").fetchall()
        out = []
        for r in recs:
            try:
                seen = datetime.strptime(r["last_seen"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                seen = now
            status = ("ACTIVE" if r["connected"] else
                      "INACTIVE" if now - seen > timedelta(days=INACTIVE_AFTER_DAYS) else "DISCONNECTED")
            out.append(DeviceRow(r["dev_key"], r["name"] or "", r["last_user"] or "n/a", r["device_type"] or "",
                                 "n/a", r["first_seen"], r["last_seen"], status, "LOCAL"))
        return out


_BACKEND_STATUS = {"ACTIVE": "ACTIVE", "REVOKED": "REVOKED", "LOST": "INACTIVE", "PENDING": "INACTIVE"}


def backend_rows(devices: list[dict], users_by_id: dict[str, str] | None = None) -> list[DeviceRow]:
    users_by_id = users_by_id or {}
    out = []
    for d in devices:
        owner = d.get("owner_user_id") or d.get("owner_user")
        out.append(DeviceRow(
            str(d.get("id") or d.get("device_public_id") or ""), str(d.get("device_label") or ""),
            users_by_id.get(str(owner), str(owner)) if owner else "n/a", f"protocol {d.get('protocol_version', '?')}",
            str(d.get("ip") or d.get("last_ip") or "n/a"), str(d.get("created_at") or "n/a"),
            str(d.get("last_seen_at") or "n/a"), _BACKEND_STATUS.get(str(d.get("status")), "INACTIVE"), "BACKEND"))
    return out
