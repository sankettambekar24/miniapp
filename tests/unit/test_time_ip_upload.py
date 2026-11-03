"""Timestamps (IST display, UTC storage, injected clocks), IP 'n/a' handling, and the firmware upload contract.
Status: MOCK-TESTED (fake firmware; not run on a board)."""
from datetime import datetime, timedelta, timezone

import pytest

from hss.core import clock
from hss.core.audit import AuditStore, backend_row, valid_ip
from hss.core.devices import backend_rows
from hss.core.localdb import LocalDb
from hss.core.sd_ops import (SdErrorCode, check_upload_name, map_device_error, suggest_83_name, validate_upload_plan)
from hss.core.sd_policy import SdPolicy

from sd_rig import SdRig


@pytest.fixture(autouse=True)
def _reset_clock():
    yield
    clock.set_now_for_tests(None)


# ---------------------------------------------------------------- time
@pytest.mark.parametrize("src,want", [
    ("2026-11-03T12:15:32Z", "03-Nov-2026 05:45:32 PM IST"),
    ("2026-11-03T02:30:00Z", "03-Nov-2026 08:00:00 AM IST"),
    ("2026-11-03T18:30:00Z", "04-Nov-2026 12:00:00 AM IST"),      # crosses midnight
    ("2026-11-03T06:30:00Z", "03-Nov-2026 12:00:00 PM IST"),      # noon
    ("2026-11-03 12:15:32", "03-Nov-2026 05:45:32 PM IST"),       # naive = UTC (backend style, space separator)
    ("2026-11-03T17:45:32+05:30", "03-Nov-2026 05:45:32 PM IST"),  # already IST offset
    ("2026-01-05T00:00:00.123456+00:00", "05-Jan-2026 05:30:00 AM IST"),
])
def test_format_ist(src, want):
    assert clock.format_ist(src) == want


def test_format_edge_cases():
    assert clock.format_ist(None) == "n/a" and clock.format_ist("") == "n/a"
    assert clock.format_ist("garbage") == "garbage"
    assert clock.format_ist(datetime(2026, 11, 3, 12, 15, 32, tzinfo=timezone.utc)) == "03-Nov-2026 05:45:32 PM IST"
    assert clock.TZ_SOURCE in ("zoneinfo Asia/Kolkata", "fixed +05:30 fallback")


def test_iso_storage_is_utc_with_z():
    assert clock.to_iso(datetime(2026, 11, 3, 17, 45, 32, 123000, tzinfo=timezone(timedelta(hours=5, minutes=30)))) == "2026-11-03T12:15:32.123Z"
    assert clock.parse_utc("2026-11-03T12:15:32.123Z") == datetime(2026, 11, 3, 12, 15, 32, 123000, tzinfo=timezone.utc)


def test_injected_clock_drives_audit_and_device_timestamps(tmp_path):
    clock.set_now_for_tests(lambda: datetime(2026, 11, 3, 12, 15, 32, tzinfo=timezone.utc))
    a = AuditStore(LocalDb(tmp_path / "t.db"))
    a.append("X")
    row = a.query()[0][0]
    assert row.ts == "2026-11-03T12:15:32.000Z" and row.date == "03-Nov-2026" and row.time == "05:45:32 PM IST"
    assert clock.now_utc().tzinfo is not None


def test_audit_date_filters_use_ist_days(tmp_path):
    from hss.core.audit import AuditFilter
    a = AuditStore(LocalDb(tmp_path / "t.db"))
    a.append("LATE", ts="2026-11-03T20:00:00.000Z")        # = 04-Nov 01:30 IST
    a.append("EARLY", ts="2026-11-03T10:00:00.000Z")       # = 03-Nov 15:30 IST
    assert [r.action for r in a.query(AuditFilter(since="2026-11-04", until="2026-11-04"))[0]] == ["LATE"]
    assert [r.action for r in a.query(AuditFilter(since="2026-11-03", until="2026-11-03"))[0]] == ["EARLY"]


def test_backend_rows_are_parsed_as_utc_and_converted():
    r = backend_row({"id": "e", "occurred_at": "2026-11-03T12:15:32.5Z", "actor_username": "a", "event_type": "T", "result": "SUCCESS"})
    assert r.ts == "2026-11-03T12:15:32.500Z" and r.display_time == "03-Nov-2026 05:45:32 PM IST"
    naive = backend_row({"occurred_at": "2026-11-03 12:15:32", "event_type": "T", "result": "SUCCESS"})
    assert naive.display_time == "03-Nov-2026 05:45:32 PM IST"


# ---------------------------------------------------------------- IP
@pytest.mark.parametrize("v,want", [("203.0.113.7", "203.0.113.7"), ("2001:db8::1", "2001:db8::1"), (" 10.0.0.2 ", "10.0.0.2"),
                                    (None, "n/a"), ("", "n/a"), ("999.1.1.1", "n/a"), ("<script>", "n/a"), ("unknown", "n/a")])
def test_valid_ip(v, want):
    assert valid_ip(v) == want


def test_ip_only_from_backend_fields_never_fabricated():
    lockout = backend_row({"occurred_at": "2026-11-03T12:00:00Z", "event_type": "LOGIN_LOCKED_OUT", "result": "FAILURE",
                           "safe_metadata": {"ip": "198.51.100.9", "username": "x"}})
    plain = backend_row({"occurred_at": "2026-11-03T12:00:00Z", "event_type": "ADMIN_USER_CREATED", "result": "SUCCESS",
                         "safe_metadata": {"created_username": "u"}})
    nometa = backend_row({"occurred_at": "2026-11-03T12:00:00Z", "event_type": "X", "result": "SUCCESS"})
    assert lockout.ip == "198.51.100.9" and plain.ip == "n/a" and nometa.ip == "n/a"
    dev = backend_rows([{"id": "d1", "device_label": "L", "status": "ACTIVE", "protocol_version": 3},
                        {"id": "d2", "device_label": "M", "status": "ACTIVE", "ip": "192.0.2.1"}])
    assert [d.ip for d in dev] == ["n/a", "192.0.2.1"]


def test_local_rows_never_carry_an_ip(tmp_path):
    a = AuditStore(LocalDb(tmp_path / "t.db"))
    a.append("PASSWORD_LOGIN")
    assert a.query()[0][0].ip == "n/a"


# ---------------------------------------------------------------- upload
def admin_rig(files=None):
    r = SdRig(dict(files or {}))
    r.svc.policy = SdPolicy(lambda: "admin", lambda: None)
    return r


def instrument(r):
    """Record, for every host write, how many device lines had been emitted by then."""
    emitted, log = [], []
    orig = r.fw._line_out
    r.fw._line_out = lambda t: (emitted.append(t), orig(t))[1]
    ser = r.ser
    orig_w = ser.write
    ser.write = lambda d: (log.append((bytes(d), list(emitted))), orig_w(d))[1]
    return log


def test_payload_is_sent_only_after_write_chunk_ready(tmp_path):
    r = admin_rig()
    log = instrument(r)
    src = tmp_path / "f.bin"
    src.write_bytes(bytes(range(256)) * 5)                 # 1280 bytes = 3 chunks
    res = r.upload(src, "F.BIN")
    assert res.ok and r.fw.files["F.BIN"] == src.read_bytes()
    seq = 0
    for data, seen in log:
        if data.startswith(b"WRITE_CHUNK:"):
            hdr = data.decode().strip()
            assert not any(s.startswith(f"WRITE_CHUNK_READY:{seq}:") for s in seen), "READY seen before header?!"
            continue
        if data.startswith((b"@", b"WRITE_FILE", b"LIST", b"VERSION", b"WRITE_ABORT")):
            continue
        # a payload write: the READY for this chunk must already have been received
        assert any(s.startswith(f"WRITE_CHUNK_READY:{seq}:") for s in seen), f"payload {seq} sent before READY"
        seq += 1
    assert seq == 3


def test_no_ready_means_no_payload_and_a_clear_timeout(tmp_path):
    r = admin_rig()
    log = instrument(r)
    r.fw._line_out_orig = r.fw._line_out
    r.fw._line_out = lambda t: None if t.startswith("WRITE_CHUNK_READY") else r.fw._line_out_orig(t)
    src = tmp_path / "f.bin"
    src.write_bytes(b"x" * 100)
    res = r.run(lambda cb: r.svc.upload(src, "F.BIN", None, cb), max_pumps=3000)
    assert not res.ok
    assert not any(not d.startswith((b"@", b"WRITE_FILE", b"WRITE_CHUNK:", b"WRITE_ABORT", b"LIST")) for d, _ in log if d), \
        "payload bytes were written although the device never said READY"


@pytest.mark.parametrize("name", ["LONGFILENAME.TXT", "report 1.pdf", "a.b.c", "x" * 9, "a.html", "sub/A.TXT", "CON.TXT", ""])
def test_long_or_illegal_names_are_rejected_locally_and_nothing_is_sent(tmp_path, name):
    r = admin_rig()
    src = tmp_path / "f.txt"
    src.write_bytes(b"x")
    n = len(r.ser.written)
    res = r.upload(src, name)
    assert not res.ok and res.error.code is SdErrorCode.VALIDATION and len(r.ser.written) == n
    assert check_upload_name(name)


def test_plan_validation_limits_and_collision(tmp_path):
    src = tmp_path / "ok.txt"
    src.write_bytes(b"x")
    assert validate_upload_plan(src, "OK.TXT") is None
    assert "already exists" in validate_upload_plan(src, "ok.txt", ["OK.TXT"])       # FAT is case-insensitive
    assert "8.3" in validate_upload_plan(src, "toolongname.txt")
    assert "not a readable file" in validate_upload_plan(tmp_path / "missing", "A.TXT")
    big = tmp_path / "big.bin"
    with open(big, "wb") as fh:
        fh.truncate(8 * 1024 * 1024 + 1)
    assert "8 MB" in validate_upload_plan(big, "BIG.BIN")
    exact = tmp_path / "exact.bin"
    with open(exact, "wb") as fh:
        fh.truncate(8 * 1024 * 1024)
    assert validate_upload_plan(exact, "EXACT.BIN") is None


def test_suggest_83_name():
    assert suggest_83_name("Quarterly Report 2026.pdf") == "QUARTERL.PDF"
    assert suggest_83_name("notes.txt") == "NOTES.TXT"
    assert suggest_83_name("a.b.c.html") == "ABC.HTM"
    assert suggest_83_name("noext") == "NOEXT" and suggest_83_name("???.x") == "FILE.X"
    for n in ("Quarterly Report 2026.pdf", "notes.txt", "a.b.c.html", "???.x"):
        assert check_upload_name(suggest_83_name(n)) is None


def test_upload_is_admin_only_in_the_service_and_sends_nothing(tmp_path):
    r = SdRig({})
    r.svc.policy = SdPolicy(lambda: "user", lambda: None)
    src = tmp_path / "f.txt"
    src.write_bytes(b"x")
    n = len(r.ser.written)
    res = r.upload(src, "F.TXT")
    assert res.error.code is SdErrorCode.FORBIDDEN and len(r.ser.written) == n and not r.fw.files


def test_existing_file_is_never_overwritten(tmp_path):
    r = admin_rig({"F.TXT": b"old"})
    src = tmp_path / "f.txt"
    src.write_bytes(b"new")
    res = r.upload(src, "F.TXT")
    assert res.error.code is SdErrorCode.COLLISION and r.fw.files["F.TXT"] == b"old"


def test_missing_sd_card_and_locked_device_messages():
    from hss.ui.sd_page import describe
    assert "SD card is missing" in describe(map_device_error("CARD_UNAVAILABLE"))
    assert "keypad" in describe(map_device_error("AUTH_REQUIRED"))
    assert "COLLISION" in describe(map_device_error("DESTINATION_EXISTS"))


def test_disconnect_mid_upload_reports_disconnected(tmp_path):
    r = admin_rig()
    src = tmp_path / "f.bin"
    src.write_bytes(b"y" * 4096)
    out = []
    def yank(d, t):
        if d >= 1024:                                   # cable pulled: every further read fails
            r.ser.read = lambda n=1: (_ for _ in ()).throw(OSError("device gone"))
    r.svc.upload(src, "F.BIN", yank, out.append)
    for _ in range(400):
        if out:
            break
        r.clock.advance(1.0)
        r.manager.pump()
    assert out and out[0].error.code in (SdErrorCode.DISCONNECTED, SdErrorCode.TIMEOUT)
