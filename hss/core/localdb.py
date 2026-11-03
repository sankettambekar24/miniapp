"""Local SQLite store under %LOCALAPPDATA%\\HSS (LOCAL data only; never claimed as production authorization).

Schema is versioned with PRAGMA user_version and migrated forward only (no destructive migrations).
v1: audit_log (hash-chained, append-only via triggers), device_history, download_flags.
v2: enrolled_users, face_consents (three-factor enrollment; face templates are DPAPI-encrypted).
v3: file_permissions (local per-user Read/Download; the backend cannot map hardware logins to backend users).
One connection guarded by a lock (the app writes from the GUI thread and the serial worker thread).
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .. import paths

SCHEMA_VERSION = 3

_V1 = """
CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,               -- UTC ISO-8601
  username TEXT, user_id TEXT, role TEXT,
  action TEXT NOT NULL, resource TEXT, device TEXT,
  status TEXT NOT NULL, details TEXT,
  prev_hash TEXT NOT NULL, row_hash TEXT NOT NULL
);
CREATE INDEX idx_audit_ts ON audit_log(ts);
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TABLE device_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dev_key TEXT NOT NULL UNIQUE,   -- VID:PID:serial, else VID:PID:desc:<description>
  vid INTEGER, pid INTEGER, serial TEXT, description TEXT, port TEXT,
  name TEXT, device_type TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  last_user TEXT, connected INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE download_flags (     -- LOCAL, CLIENT-ENFORCED-ONLY: 1 = download blocked (read-only access)
  subject TEXT NOT NULL, file TEXT NOT NULL, download_blocked INTEGER NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY (subject, file)
);
"""

_V2 = """
CREATE TABLE enrolled_users (
  user_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin','user')),
  fingerprint_id INTEGER UNIQUE,            -- device AS608 slot (1..16), NULL until the device confirmed ENROLL_OK
  face_template BLOB,                       -- DPAPI-encrypted SFace embedding; NEVER a photo
  pin_set INTEGER NOT NULL DEFAULT 0,       -- device confirmed PIN_SET_OK (the PIN/hash is never on the PC)
  status TEXT NOT NULL CHECK (status IN ('ENROLLED','INCOMPLETE','DISABLED','REVOKED')),
  device_cleanup_pending INTEGER NOT NULL DEFAULT 0,
  consent_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE face_consents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  captured_by TEXT,
  text_version TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  camera TEXT
);
"""

_V3 = """
CREATE TABLE file_permissions (   -- LOCAL, CLIENT-ENFORCED-ONLY: which enrolled user may Read (preview) / Download which SD file
  user_id TEXT NOT NULL,          -- enrolled user id (production) or 'slot:N' (PIN test mode)
  file TEXT NOT NULL,             -- upper-cased SD path (FAT is case-insensitive)
  can_read INTEGER NOT NULL,
  can_download INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, file)
);
"""

MIGRATIONS = {1: _V1, 2: _V2, 3: _V3}


class LocalDb:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else paths.data_dir() / "hss_local.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        with self.lock:
            cur = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if cur > SCHEMA_VERSION:
                raise RuntimeError(f"local database schema {cur} is newer than this app ({SCHEMA_VERSION})")
            for v in range(cur + 1, SCHEMA_VERSION + 1):
                self.conn.execute("BEGIN")
                try:
                    for stmt in _split(MIGRATIONS[v]):
                        self.conn.execute(stmt)
                    self.conn.execute(f"PRAGMA user_version = {v}")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise

    @property
    def version(self) -> int:
        with self.lock:
            return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        with self.lock:
            self.conn.close()


def _split(script: str) -> list[str]:
    """Split on ';' outside CREATE TRIGGER ... END blocks."""
    out, buf, in_trigger = [], [], False
    for line in script.splitlines():
        buf.append(line)
        up = line.strip().upper()
        if up.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if up.endswith("END;"):
                out.append("\n".join(buf))
                buf, in_trigger = [], False
        elif line.rstrip().endswith(";"):
            out.append("\n".join(buf))
            buf = []
    return [s for s in out if s.strip()]
