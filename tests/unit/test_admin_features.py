"""Admin services, permission matrix, local audit/device stores, preview. Status: MOCK-TESTED (no hardware, no network)."""
import io
import sqlite3
import zipfile

import pytest

from hss.backend import NotFound, Page
from hss.core.audit import AuditFilter, AuditStore, redact_audit
from hss.core.devices import DeviceHistory
from hss.core.discovery import PortInfo
from hss.core.localdb import SCHEMA_VERSION, LocalDb
from hss.core.preview import ZIP_MAX_MEMBER_BYTES, build_preview
from hss.core.sd_ops import SdErrorCode
from hss.core.sd_policy import GrantIndex, SdPolicy
from hss.core.services import (AccessChange, PermissionDenied, ValidationError, validate_password, validate_username)

from conftest import ST, Rig
from sd_rig import SdRig


# ---------------------------------------------------------------- fakes
class FakeBackend:
    is_authenticated = True
    current_user = {"id": "admin-1", "username": "root", "role": "ADMIN"}

    def __init__(self):
        self.calls = []

    def _rec(self, name, *a):
        self.calls.append((name, a))
        return {"id": "x"}

    def list_users(self, page=None):
        self.calls.append(("list_users", (page,)))
        return Page(results=[{"id": "u1", "username": "alice", "role": "USER", "is_active": True}], count=1, next=None, previous=None)

    def create_user(self, u, p, r=None):
        return self._rec("create_user", u, r)           # password deliberately not recorded by the fake

    def disable_user(self, uid):
        return self._rec("disable_user", uid)

    def set_user_active(self, uid, a):
        return self._rec("set_user_active", uid, a)

    def set_user_role(self, uid, r):
        return self._rec("set_user_role", uid, r)

    def list_devices(self, page=None):
        return Page(results=[{"id": "d1", "device_label": "Lab board", "owner_user_id": "u1", "status": "ACTIVE",
                              "protocol_version": 3, "created_at": "2026-01-01", "last_seen_at": "2026-02-01"}],
                    count=1, next=None, previous=None)

    def list_audit_events(self, page=None, event_type=None):
        return Page(results=[{"id": "e1", "occurred_at": "2026-02-01T10:00:00Z", "actor_username": "alice",
                              "event_type": "LOGIN_SUCCEEDED", "result": "SUCCESS", "safe_metadata": {"ip": "1.2.3.4"}}],
                    count=1, next=None, previous=None)

    def list_files(self, page=None):
        return Page(results=[{"id": "f1", "display_name": "A.TXT"}], count=1, next=None, previous=None)

    def create_file_metadata(self, owner, name):
        return self._rec("create_file_metadata", owner, name)

    def list_grants(self, fid):
        return []

    def create_grant(self, fid, uid, perm, expires_at=None):
        return self._rec("create_grant", fid, uid, perm)

    def revoke_grant(self, fid, gid):
        return self._rec("revoke_grant", fid, gid)


def login(rig, slot):
    rig.connect()
    rig.ctl.begin_auth()
    rig.device_says(f"AUTH_PIN_VERIFIED:{slot}")
    rig.ctl.backend = FakeBackend()
    return rig.ctl


# ---------------------------------------------------------------- admin vs user for EVERY admin service
ADMIN_CALLS = {
    "local_test_identities": lambda s: s.local_test_identities(),
    "list_users": lambda s: s.list_users(),
    "create_user": lambda s: s.create_user("newuser", "longenough1", "USER"),
    "set_user_active": lambda s: s.set_user_active("u1", False),
    "set_user_role": lambda s: s.set_user_role("u1", "ADMIN"),
    "list_devices": lambda s: s.list_devices(),
    "list_audit": lambda s: s.list_audit(AuditFilter()),
    "verify_local_audit": lambda s: s.verify_local_audit(),
    "file_access_snapshot": lambda s: s.file_access_snapshot("A.TXT"),
    "create_file_metadata": lambda s: s.create_file_metadata("A.TXT"),
    "apply_file_access": lambda s: s.apply_file_access("f1", "A.TXT", [], [AccessChange("u1", "alice", True)]),
}


@pytest.mark.parametrize("name", sorted(ADMIN_CALLS))
def test_user_role_is_refused_by_every_admin_service_and_nothing_reaches_backend(name):
    ctl = login(Rig([ST]), 2)                                  # slot 2 = USER
    with pytest.raises(PermissionDenied):
        ADMIN_CALLS[name](ctl.admin)
    assert ctl.backend.calls == []                              # backend never contacted
    rows, _ = ctl.audit.query()
    assert any(r.status == "denied" for r in rows)              # and the refusal itself is audited


@pytest.mark.parametrize("name", sorted(ADMIN_CALLS))
def test_admin_role_is_accepted(name):
    ctl = login(Rig([ST]), 1)
    ADMIN_CALLS[name](ctl.admin)


def test_no_session_is_refused():
    rig = Rig([ST])
    rig.connect()
    with pytest.raises(PermissionDenied):
        rig.ctl.admin.list_users()


def test_validation_typed_errors_and_password_never_audited():
    ctl = login(Rig([ST]), 1)
    with pytest.raises(ValidationError):
        ctl.admin.create_user("x", "longenough1", "USER")
    with pytest.raises(ValidationError):
        ctl.admin.create_user("gooduser", "short", "USER")
    with pytest.raises(ValidationError):
        ctl.admin.create_user("gooduser", "longenough1", "ROOT")
    ctl.admin.create_user("gooduser", "Sup3r-secret-pw", "USER")
    rows, _ = ctl.audit.query()
    assert any(r.action == "USER_CREATE" and r.status == "success" for r in rows)
    assert all("Sup3r-secret-pw" not in (r.details + r.resource) for r in rows)
    with pytest.raises(ValidationError):
        validate_username("bad name!")
    assert validate_password("123456789x", "someone")


def test_apply_file_access_grants_list_and_read_and_local_download_flag():
    ctl = login(Rig([ST]), 1)
    users = [{"id": "u1", "username": "alice", "role": "USER", "read": False, "download_blocked": False, "grants": []}]
    done = ctl.admin.apply_file_access("f1", "A.TXT", users, [AccessChange("u1", "alice", True, True)])
    assert [c[1][2] for c in ctl.backend.calls if c[0] == "create_grant"] == ["LIST", "READ"]
    assert ctl.download_flags.blocked_for("u1") == {"A.TXT"} and done
    users = [{"id": "u1", "username": "alice", "role": "USER", "read": True, "download_blocked": True,
              "grants": [{"id": "g1", "permission": "LIST", "active": True}, {"id": "g2", "permission": "READ", "active": True}]}]
    ctl.admin.apply_file_access("f1", "A.TXT", users, [AccessChange("u1", "alice", False)])
    assert [c[1][1] for c in ctl.backend.calls if c[0] == "revoke_grant"] == ["g1", "g2"]
    assert ctl.download_flags.blocked_for("u1") == set()


# ---------------------------------------------------------------- permission matrix: none / read / read+download
FILES = {"a.txt": b"AAA", "b.txt": b"BBB", "c.txt": b"CCC"}
IDX = GrantIndex({"a.txt": frozenset({"LIST", "READ"}), "b.txt": frozenset({"LIST", "READ"}), "c.txt": frozenset({"LIST"})},
                 download_blocked={"b.txt"})


def sd_rig(role="user"):
    r = SdRig(dict(FILES))
    r.svc.policy = SdPolicy(lambda: role, lambda: IDX)
    return r


def preview(r, path):
    return r.run(lambda cb: r.svc.read_for_preview(path, 1024, None, cb))


def test_matrix_none_no_preview_no_download_nothing_sent(tmp_path):
    r = sd_rig()
    n = len(r.ser.written)
    assert preview(r, "c.txt").error.code is SdErrorCode.FORBIDDEN            # LIST only == no Read
    assert r.download("c.txt", tmp_path / "o").error.code is SdErrorCode.FORBIDDEN
    assert preview(r, "zzz.txt").error.code is SdErrorCode.FORBIDDEN          # no entry at all
    assert len(r.ser.written) == n


def test_matrix_read_only_previews_but_download_denied(tmp_path):
    r = sd_rig()
    assert preview(r, "b.txt").value.data == b"BBB"
    n = len(r.ser.written)
    res = r.download("b.txt", tmp_path / "o")
    assert res.error.code is SdErrorCode.FORBIDDEN and "Read-only" in res.error.message
    assert len(r.ser.written) == n and not (tmp_path / "o").exists()


def test_matrix_read_and_download_both_work(tmp_path):
    r = sd_rig()
    assert preview(r, "a.txt").value.data == b"AAA"
    assert r.download("a.txt", tmp_path / "o").ok and (tmp_path / "o").read_bytes() == b"AAA"


def test_preview_cap_is_enforced_on_device_stream():
    r = SdRig({"big.bin": b"x" * 5000})
    res = r.run(lambda cb: r.svc.read_for_preview("big.bin", 1024, None, cb))
    assert res.error.code is SdErrorCode.TOO_LARGE


def test_preview_without_session_sends_nothing():
    r = SdRig(dict(FILES), session=False)
    n = len(r.ser.written)
    assert preview(r, "a.txt").error.code is SdErrorCode.NOT_AUTHENTICATED and len(r.ser.written) == n


# ---------------------------------------------------------------- local audit
def test_audit_redaction_of_pins_passwords_tokens_headers(tmp_path):
    a = AuditStore(LocalDb(tmp_path / "a.db"))
    a.append("X", username="bob", details="pin=123456 password: hunter2 token=abc.def Authorization: Bearer eyJhbGciOi.eyJzdWIi.sig "
                                         "access_token=\"zzz\" PIN is 9999")
    a.append("PIN_LOGIN", resource="pin: 4321")
    rows, _ = a.query()
    blob = " ".join(r.details + r.resource for r in rows)
    for secret in ("123456", "hunter2", "abc.def", "eyJhbGciOi", "zzz", "9999", "4321"):
        assert secret not in blob, secret
    assert "[REDACTED]" in blob
    raw = sqlite3.connect(tmp_path / "a.db").execute("SELECT group_concat(details||resource) FROM audit_log").fetchone()[0]
    assert "hunter2" not in raw and "123456" not in raw            # redacted BEFORE it reaches the file
    assert "[REDACTED]" in redact_audit("pin 000000")


def test_audit_is_append_only_and_hash_chain_detects_tampering(tmp_path):
    db = LocalDb(tmp_path / "a.db")
    a = AuditStore(db)
    for i in range(3):
        a.append("A", username=f"u{i}")
    assert a.verify() == (True, None)
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("UPDATE audit_log SET action='B'")
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("DELETE FROM audit_log")
    raw = sqlite3.connect(tmp_path / "a.db")                       # an outside editor drops the triggers and edits a row
    raw.execute("DROP TRIGGER audit_log_no_update")
    raw.execute("UPDATE audit_log SET username='mallory' WHERE id=2")
    raw.commit()
    assert a.verify() == (False, 2)


def test_audit_persists_across_restart_and_filters_paginate(tmp_path):
    path = tmp_path / "a.db"
    a = AuditStore(LocalDb(path))
    for i in range(120):
        a.append("FILE_PREVIEW" if i % 2 else "PIN_LOGIN", username="alice" if i % 3 else "bob",
                 status="success" if i % 4 else "failure", resource=f"f{i}", ts=f"2026-03-{1 + i % 28:02d}T10:00:00.000Z")
    b = AuditStore(LocalDb(path))                                   # "restart"
    rows, total = b.query(page=1, page_size=50)
    assert total == 120 and len(rows) == 50 and rows[0].resource == "f119"       # newest first
    assert len(b.query(page=3)[0]) == 20
    assert b.query(AuditFilter(user="bob"))[1] == 40
    assert b.query(AuditFilter(action="PREVIEW"))[1] == 60
    assert b.query(AuditFilter(status="failure"))[1] == 30
    assert b.query(AuditFilter(since="2026-03-10", until="2026-03-10"))[1] == sum(1 for i in range(120) if 1 + i % 28 == 10)
    assert b.query(AuditFilter(text="f77"))[1] == 1


def test_controller_writes_real_operations_to_local_audit():
    rig = Rig([ST])
    rig.connect()
    rig.ctl.begin_auth()
    rig.device_says("AUTH_PIN_FAILED:1")
    rig.device_says("AUTH_PIN_VERIFIED:1")
    rig.device_says("SESSION_ENDED")
    rig.connect_check = rig.unplug()
    actions = [(r.action, r.status) for r in rig.ctl.audit.query()[0]]
    for want in (("DEVICE_CONNECTED", "success"), ("PIN_LOGIN", "failure"), ("PIN_LOGIN", "success"), ("LOGOUT", "success"),
                 ("DEVICE_DISCONNECTED", "success")):
        assert want in actions, (want, actions)


def test_merged_audit_view_labels_sources_and_admin_only():
    ctl = login(Rig([ST]), 1)
    ctl.audit_event("FILE_PREVIEW", resource="A.TXT")
    rows, total, note = ctl.admin.list_audit(AuditFilter())
    assert {r.source for r in rows} == {"LOCAL", "BACKEND"} and rows == sorted(rows, key=lambda r: r.ts, reverse=True)
    assert [r for r in rows if r.source == "BACKEND"][0].username == "alice"


# ---------------------------------------------------------------- device history
def test_device_history_dedup_persist_never_deleted(tmp_path):
    path = tmp_path / "d.db"
    h = DeviceHistory(LocalDb(path))
    p1 = PortInfo("COM15", 0x0483, 0x5740, "USB Serial Device", "366F344C3038")
    p1b = PortInfo("COM9", 0x0483, 0x5740, "USB Serial Device", "366F344C3038")     # same board, other port
    p2 = PortInfo("COM3", 0x0483, 0x5740, "USB Serial Device", None)                # no serial: falls back to description
    p2b = PortInfo("COM4", 0x0483, 0x5740, "USB Serial Device", None)
    h.connected(p1, now="2026-01-01T00:00:00Z")
    h.set_user(p1, "test-admin")
    h.disconnected(p1, now="2026-01-01T01:00:00Z")
    h.connected(p1b, now="2026-01-02T00:00:00Z")
    h.connected(p2)
    h.connected(p2b)
    rows = DeviceHistory(LocalDb(path)).local_rows()               # reopened = restart
    assert len(rows) == 2 and all(r.source == "LOCAL" for r in rows)
    board = [r for r in rows if "366F344C3038" in r.device_id][0]
    assert board.first_seen == "2026-01-01T00:00:00Z" and board.last_seen == "2026-01-02T00:00:00Z"
    assert board.user == "test-admin" and board.ip == "n/a" and board.status == "ACTIVE"
    h.disconnected(p1b)
    again = [r for r in h.local_rows() if "366F344C3038" in r.device_id][0]
    assert again.status == "DISCONNECTED"                           # row kept, not deleted


def test_admin_device_view_merges_backend_and_local():
    ctl = login(Rig([ST]), 1)
    rows, note = ctl.admin.list_devices()
    assert {r.source for r in rows} == {"BACKEND", "LOCAL"}
    b = [r for r in rows if r.source == "BACKEND"][0]
    assert b.user == "alice" and b.status == "ACTIVE" and b.ip == "n/a"


# ---------------------------------------------------------------- local db versioning
def test_schema_version_and_safe_reopen(tmp_path):
    db = LocalDb(tmp_path / "v.db")
    assert db.version == SCHEMA_VERSION == 1
    AuditStore(db).append("A")
    db.close()
    db2 = LocalDb(tmp_path / "v.db")                                # re-running migrations is a no-op, data kept
    assert db2.version == 1 and AuditStore(db2).query()[1] == 1
    db2.conn.execute("PRAGMA user_version = 99")
    db2.close()
    with pytest.raises(RuntimeError):
        LocalDb(tmp_path / "v.db")                                  # newer schema: refuse instead of corrupting


# ---------------------------------------------------------------- preview of every format
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82")


def make_zip(files: dict[str, bytes]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()


DOCX = make_zip({"[Content_Types].xml": b"<x/>", "word/document.xml":
                 b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                 b"<w:p><w:r><w:t>Hello</w:t></w:r><w:r><w:t> docx</w:t></w:r></w:p><w:p><w:r><w:t>Line2</w:t></w:r></w:p></w:body></w:document>"})
XLSX = make_zip({"xl/sharedStrings.xml": b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Name</t></si><si><t>Bob</t></si></sst>',
                 "xl/worksheets/sheet1.xml": b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                 b'<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42</v></c></row><row r="2"><c r="A2" t="s"><v>1</v></c></row></sheetData></worksheet>'})


@pytest.mark.parametrize("name,data,kind,needle", [
    ("a.txt", b"hello", "text", "hello"), ("a.LOG", b"x\ny", "text", "x"), ("a.md", b"# T", "text", "# T"),
    ("a.csv", b"a,b\n1,2", "text", "a,b"), ("a.json", b'{"k": 1}', "text", '"k"'),
    ("a.xml", b"<a>t</a>", "text", "<a>"), ("a.html", b"<script>alert(1)</script>", "text", "<script>"),
    ("a.png", PNG, "image", ""), ("a.jpg", b"\xff\xd8\xff\xe0" + b"0" * 20, "image", ""),
    ("a.gif", b"GIF89a" + b"0" * 20, "image", ""), ("a.bmp", b"BM" + b"0" * 30, "image", ""),
    ("a.pdf", b"%PDF-1.4\n%%EOF", "pdf", ""), ("a.docx", DOCX, "office", "Hello docx\nLine2"),
    ("a.xlsx", XLSX, "office", "Name\t42"), ("a.exe", b"MZ\x90\x00", "unsupported", "not supported"),
    ("noext", b"abc", "unsupported", "not supported"),
])
def test_preview_formats(name, data, kind, needle):
    p = build_preview(name, data)
    assert p.kind == kind and needle in (p.text + p.note)


def test_html_is_text_never_rendered():
    p = build_preview("x.html", b"<img src=x onerror=alert(1)>")
    assert p.kind == "text" and "escaped source" in p.note and p.data == b""


def test_preview_empty_corrupt_oversized_and_mismatched():
    assert build_preview("a.txt", b"").kind == "empty"
    assert build_preview("a.png", b"not a png").kind == "corrupt"
    assert build_preview("a.png", b"%PDF-1.4").kind == "corrupt"                 # extension AND magic must agree
    assert build_preview("a.pdf", b"hello").kind == "corrupt"
    assert build_preview("a.docx", b"hello").kind == "corrupt"
    assert build_preview("a.docx", b"PK\x03\x04 garbage garbage").kind == "corrupt"
    assert build_preview("a.xlsx", make_zip({"junk.txt": b"x"})).kind == "corrupt"
    assert build_preview("a.txt", b"ab\x00cd").kind == "corrupt"
    assert build_preview("a.txt", b"\xc3\x28\xa0\xa1").kind == "corrupt"
    assert build_preview("a.txt", b"x" * 100, cap=50).kind == "too_large"
    assert build_preview("a.txt", b"x" * 100, cap=50).note.startswith("Too large to preview")
    assert build_preview("a.txt", b"%PDF-1.4 text").kind == "unsupported"


def test_preview_zip_bomb_and_xml_entity_guards():
    big = make_zip({"word/document.xml": b"<w:document xmlns:w='x'>" + b"A" * (ZIP_MAX_MEMBER_BYTES + 10) + b"</w:document>"})
    p = build_preview("a.docx", big, cap=100 * 1024 * 1024)
    assert p.kind == "corrupt" and "zip bomb" in p.note.lower()
    xxe = make_zip({"word/document.xml": b'<!DOCTYPE d [<!ENTITY a "aaaa">]><w:document xmlns:w="x"/>'})
    assert build_preview("a.docx", xxe).kind == "corrupt"
    many = make_zip({f"f{i}.txt": b"x" for i in range(2100)})
    assert build_preview("a.xlsx", many).kind == "corrupt"
