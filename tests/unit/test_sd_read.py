"""probe / list / download against FakeFirmware. Status: MOCK-TESTED (no real port, no hardware)."""
import os

import pytest

from hss.core.sd_ops import SdErrorCode
from sd_rig import SdRig

BLOB = bytes(range(256)) * 4 + b"\r\n\x00\n tail"   # nulls, CR/LF inside the payload


def tmp_left(d):
    return [p for p in os.listdir(d) if p.endswith(".part")]


# ---- probe ----
def test_probe_stores_capabilities_and_enables_wire_ids():
    r = SdRig(auth_mode="PIN_ONLY")
    res = r.probe()
    assert res.ok and res.value.version == "0.3.0-stage3"
    assert r.svc.capabilities.auth_mode == "PIN_ONLY" and r.svc.capabilities.wire_ids
    assert r.manager.wire_ids is True


def test_probe_legacy_firmware_keeps_wire_ids_off():
    r = SdRig(wire_supported=False)
    assert r.probe().ok
    assert r.manager.wire_ids is False and not r.svc.capabilities.wire_ids


def test_probe_unsupported_firmware_typed_error():
    r = SdRig(version_supported=False)
    res = r.probe()
    assert not res.ok and res.error.code is SdErrorCode.UNSUPPORTED and r.svc.capabilities is None


def test_probe_works_without_session_and_when_locked():
    r = SdRig(session=False, locked=True)
    assert r.probe().ok


def test_probe_retries_after_dropped_reply():
    r = SdRig()
    r.fw.drop_once.add("HSS_FW:")
    assert r.probe().ok
    assert r.fw.commands.count("VERSION") == 2


def test_disconnect_resets_capabilities_and_wire_ids():
    r = SdRig()
    r.probe()
    r.ports = []
    r.clock.advance(2.0)
    r.manager.pump()
    assert not r.manager.connected and r.svc.capabilities is None and r.manager.wire_ids is False


# ---- list ----
@pytest.mark.parametrize("wire", [False, True])
def test_list_small_empty_many(wire):
    r = SdRig({"a.txt": b"1", "dir/b.bin": b"2"}, wire_supported=wire)
    r.probe()
    assert r.manager.wire_ids is wire
    res = r.list()
    assert res.ok and res.value.paths == ("a.txt", "dir/b.bin") and res.value.skipped == 0
    assert (r.ser.written[-1].startswith(b"@")) is wire
    r2 = SdRig({})
    assert r2.list().value.paths == ()
    r3 = SdRig({f"f{i:05d}.dat": b"" for i in range(3000)})
    assert len(r3.list().value.paths) == 3000


def test_list_skips_unsafe_names_from_device():
    r = SdRig({"ok.txt": b"", "bad:name": b"", "a//b": b""})
    res = r.list()
    assert res.value.paths == ("ok.txt",) and res.value.skipped == 2


def test_list_retries_on_timeout_then_succeeds():
    r = SdRig({"a": b""})
    r.fw.drop_once.add("SD_FILE_LIST_BEGIN")
    res = r.list()
    assert res.ok and r.fw.commands.count("LIST_FILES") == 2


def test_list_timeout_after_retries():
    r = SdRig({"a": b""})
    r.fw.reply_delay = 10_000
    res = r.list()
    assert not res.ok and res.error.code is SdErrorCode.TIMEOUT
    assert r.fw.commands.count("LIST_FILES") == 3  # 1 + 2 retries


def test_list_no_session_writes_nothing():
    r = SdRig(session=False)
    res = r.list()
    assert res.error.code is SdErrorCode.NOT_AUTHENTICATED and r.ser.written == []


def test_list_device_auth_required_and_locked():
    r = SdRig(authed=False)
    assert r.list().error.code is SdErrorCode.NOT_AUTHENTICATED
    r = SdRig(locked=True)
    res = r.list()
    assert res.error.code is SdErrorCode.LOCKED_OUT
    assert any(e.text == "SD_ERROR:LOCKED_OUT" for e in r.lines)  # forwarded so the auth FSM can lock


def test_session_expired_line_is_forwarded_and_aborts_list():
    r = SdRig({"a": b""})
    r.fw._list = lambda: r.fw._line_out("SESSION_EXPIRED")  # device reports expiry instead of the listing
    res = r.list()
    assert res.error.code is SdErrorCode.NOT_AUTHENTICATED
    assert any(e.text == "SESSION_EXPIRED" for e in r.lines)


def test_card_unavailable_is_device_error():
    r = SdRig()
    r.fw.sd_available = False
    res = r.list()
    assert res.error.code is SdErrorCode.DEVICE_ERROR and res.error.device_code == "CARD_UNAVAILABLE"


def test_busy_rejects_second_operation_locally():
    r = SdRig()
    out = []
    r.svc.list_files(out.append)
    r.svc.list_files(out.append)
    assert out[0].error.code is SdErrorCode.BUSY
    r.manager.pump()
    assert out[1].ok


# ---- download ----
@pytest.mark.parametrize("wire", [False, True])
@pytest.mark.parametrize("data", [b"hello", b"", BLOB])
def test_download_ok(tmp_path, wire, data):
    r = SdRig({"d/f.bin": data}, wire_supported=wire)
    r.probe()
    prog = []
    dest = tmp_path / "out.bin"
    res = r.download("d/f.bin", dest, lambda a, b: prog.append((a, b)))
    assert res.ok and res.value.size == len(data) and dest.read_bytes() == data
    assert prog[-1] == (len(data), len(data)) and tmp_left(tmp_path) == []
    assert r.fw.commands[-1].endswith("READ_FILE:d/f.bin")


def test_download_large_1mib(tmp_path):
    data = os.urandom(1024 * 1024)
    r = SdRig({"big.bin": data})
    res = r.download("big.bin", tmp_path / "big.bin")
    assert res.ok and (tmp_path / "big.bin").read_bytes() == data


def test_download_bad_paths_write_nothing(tmp_path):
    r = SdRig({"a": b"x"})
    for bad in ["../x", "/abs", "a\\b", "", "a/../b", "c:x"]:
        res = r.download(bad, tmp_path / "o")
        assert res.error.code is SdErrorCode.VALIDATION
    assert r.ser.written == [] and not (tmp_path / "o").exists()


def test_download_missing_dest_folder_and_collision(tmp_path):
    r = SdRig({"a": b"new"})
    assert r.download("a", tmp_path / "nodir" / "o").error.code is SdErrorCode.LOCAL_IO
    (tmp_path / "o").write_bytes(b"old")
    assert r.download("a", tmp_path / "o").error.code is SdErrorCode.COLLISION
    assert (tmp_path / "o").read_bytes() == b"old" and r.ser.written == []
    assert r.download("a", tmp_path / "o", overwrite=True).ok and (tmp_path / "o").read_bytes() == b"new"


def test_download_not_found(tmp_path):
    r = SdRig({})
    res = r.download("nope.txt", tmp_path / "o")
    assert res.error.code is SdErrorCode.NOT_FOUND and not (tmp_path / "o").exists()


def test_download_no_session(tmp_path):
    r = SdRig({"a": b"x"}, session=False)
    res = r.download("a", tmp_path / "o")
    assert res.error.code is SdErrorCode.NOT_AUTHENTICATED and r.ser.written == []


def test_download_interrupted_device_hang_times_out_and_cleans(tmp_path):
    r = SdRig({"a": b"z" * 1000})
    r.fw.read_stop_after = 400
    res = r.download("a", tmp_path / "o")
    assert res.error.code is SdErrorCode.TIMEOUT
    assert not (tmp_path / "o").exists() and tmp_left(tmp_path) == []
    assert r.fw.commands.count("READ_FILE:a") == 3


def test_download_disconnect_mid_transfer(tmp_path):
    r = SdRig({"a": b"z" * 1000})
    r.probe()
    r.ser.fail_after_out_bytes = r.ser._out_total + 300
    res = r.download("a", tmp_path / "o")
    assert res.error.code is SdErrorCode.DISCONNECTED and res.value is None
    assert not r.manager.connected
    assert not (tmp_path / "o").exists() and tmp_left(tmp_path) == []


@pytest.mark.parametrize("delta", [5, 20, -5])
def test_download_wrong_announced_size(tmp_path, delta):
    r = SdRig({"a": b"q" * 100})
    r.fw.read_announce_delta = delta
    res = r.download("a", tmp_path / "o")
    assert res.error.code is SdErrorCode.SIZE_MISMATCH
    assert not (tmp_path / "o").exists() and tmp_left(tmp_path) == []


def test_download_extra_bytes_before_end_marker(tmp_path):
    r = SdRig({"a": b"q" * 100})
    r.fw.read_extra = 7
    assert r.download("a", tmp_path / "o").error.code is SdErrorCode.SIZE_MISMATCH


def test_download_device_read_failed_midfile(tmp_path):
    r = SdRig({"a": b"q" * 200})
    r.fw.read_fail_at = 50
    res = r.download("a", tmp_path / "o")
    assert res.error.code is SdErrorCode.DEVICE_ERROR and res.error.device_code == "READ_FAILED"
    assert not (tmp_path / "o").exists() and tmp_left(tmp_path) == []


def test_download_timeout_no_reply(tmp_path):
    r = SdRig({"a": b"q"})
    r.fw.reply_delay = 10_000
    res = r.download("a", tmp_path / "o")
    assert res.error.code is SdErrorCode.TIMEOUT and tmp_left(tmp_path) == []


def test_download_retry_after_dropped_begin_then_ok(tmp_path):
    r = SdRig({"a": b"hello"})
    r.fw.drop_once.add("SD_FILE_BEGIN:")
    res = r.download("a", tmp_path / "o")
    assert res.ok and (tmp_path / "o").read_bytes() == b"hello"
    assert r.fw.commands.count("READ_FILE:a") == 2


def test_download_over_cap_rejected_and_port_stays_clean(tmp_path):
    r = SdRig({"a": b"x" * 5000, "b": b"y"})
    r.svc.download_cap = 1000
    res = r.download("a", tmp_path / "o")
    assert res.error.code is SdErrorCode.TOO_LARGE and not (tmp_path / "o").exists()
    assert r.download("b", tmp_path / "ob").ok  # stream was fully discarded, no desync


def test_download_cancel_midway(tmp_path):
    r = SdRig({"a": b"x" * 100_000, "b": b"ok"})

    def prog(done, total):
        if done > 0:
            r.svc.cancel()

    res = r.download("a", tmp_path / "o", prog)
    assert res.error.code is SdErrorCode.ABORTED
    assert not (tmp_path / "o").exists() and tmp_left(tmp_path) == []
    assert r.download("b", tmp_path / "ob").ok  # service usable again, port resynchronised


def test_download_session_lost_midway(tmp_path):
    r = SdRig({"a": b"x" * 100_000})

    def prog(done, total):
        r.session = False

    res = r.download("a", tmp_path / "o", prog)
    assert res.error.code is SdErrorCode.NOT_AUTHENTICATED and not (tmp_path / "o").exists()


def test_device_busy_slow_reply_still_succeeds(tmp_path):
    r = SdRig({"a": b"hi"})
    r.fw.reply_delay = 4.0
    assert r.download("a", tmp_path / "o").ok
