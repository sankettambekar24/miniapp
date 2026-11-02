"""Upload against FakeFirmware: success, failure cleanup, no blind re-send. Status: MOCK-TESTED (no hardware)."""
import os

import pytest

from hss.core.sd_ops import MAX_UPLOAD_BYTES, SdErrorCode
from sd_rig import SdRig


@pytest.fixture
def src(tmp_path):
    def make(data: bytes, name="src.bin"):
        p = tmp_path / name
        p.write_bytes(data)
        return p
    return make


def chunk_cmds(r):
    return [h for h in r.fw.chunk_headers if h.startswith("WRITE_CHUNK:")]


@pytest.mark.parametrize("wire", [False, True])
@pytest.mark.parametrize("size", [1, 511, 512, 513, 1300, 5000])
def test_upload_ok(src, wire, size):
    data = os.urandom(size)
    r = SdRig({}, wire_supported=wire)
    r.probe()
    prog = []
    res = r.upload(src(data), "up.bin", lambda a, b: prog.append((a, b)))
    assert res.ok and res.value.name == "up.bin" and res.value.size == size
    assert r.fw.files["up.bin"] == data
    assert prog[0] == (0, size) and prog[-1] == (size, size)
    seqs = [int(h.split(":")[1]) for h in chunk_cmds(r)]
    assert seqs == list(range(len(seqs))) and all(int(h.split(":")[2]) <= 512 for h in chunk_cmds(r))
    assert any(c.startswith("@") for c in r.fw.commands) is wire


def test_upload_empty_file_allowed(src):
    r = SdRig({})
    res = r.upload(src(b""), "empty.txt")
    assert res.ok and r.fw.files["empty.txt"] == b"" and chunk_cmds(r) == []
    assert "WRITE_FILE:empty.txt:0" in r.fw.commands


def test_upload_full_8mib(src):
    data = os.urandom(MAX_UPLOAD_BYTES)
    r = SdRig({})
    res = r.upload(src(data), "max.bin")
    assert res.ok and r.fw.files["max.bin"] == data


def test_upload_over_8mib_rejected_locally(src):
    r = SdRig({})
    res = r.upload(src(b"x" * (MAX_UPLOAD_BYTES + 1)), "big.bin")
    assert res.error.code is SdErrorCode.TOO_LARGE and r.ser.written == []


@pytest.mark.parametrize("bad", ["a/b.txt", "..", "CON", "", "x" * 121, "c:f", "a\\b"])
def test_upload_bad_name_writes_nothing(src, bad):
    r = SdRig({})
    res = r.upload(src(b"abc"), bad)
    assert res.error.code is SdErrorCode.VALIDATION and r.ser.written == [] and r.fw.files == {}


def test_upload_missing_local_file(tmp_path):
    r = SdRig({})
    res = r.upload(tmp_path / "nope", "n.txt")
    assert res.error.code is SdErrorCode.LOCAL_IO and r.ser.written == []


def test_upload_no_session_writes_nothing(src):
    r = SdRig({}, session=False)
    res = r.upload(src(b"abc"), "n.txt")
    assert res.error.code is SdErrorCode.NOT_AUTHENTICATED and r.ser.written == [] and r.fw.files == {}


def test_upload_device_auth_required(src):
    r = SdRig({}, authed=False)
    res = r.upload(src(b"abc"), "n.txt")
    assert res.error.code is SdErrorCode.NOT_AUTHENTICATED and res.error.partial_left is False and r.fw.files == {}


def test_upload_locked(src):
    r = SdRig({}, locked=True)
    res = r.upload(src(b"abc"), "n.txt")
    assert res.error.code is SdErrorCode.LOCKED_OUT
    assert any(e.text == "SD_ERROR:LOCKED_OUT" for e in r.lines)


@pytest.mark.parametrize("wire", [False, True])
def test_upload_collision_no_overwrite_no_cleanup_list(src, wire):
    r = SdRig({"n.txt": b"ORIGINAL"}, wire_supported=wire)
    r.probe()
    res = r.upload(src(b"new data"), "n.txt")
    assert res.error.code is SdErrorCode.COLLISION and res.error.device_code == "DESTINATION_EXISTS"
    assert res.error.partial_left is False
    assert r.fw.files["n.txt"] == b"ORIGINAL"
    assert not any(c.endswith("LIST_FILES") for c in r.fw.commands)  # existing file must not be misreported as partial


def test_upload_device_size_verification_failure(src):
    r = SdRig({})
    r.fw.verify_mismatch = True
    res = r.upload(src(b"abc" * 300), "n.txt")
    assert res.error.code is SdErrorCode.SIZE_MISMATCH and res.error.device_code == "VERIFY_SIZE_MISMATCH"
    assert res.error.partial_left is False and "n.txt" not in r.fw.files


def test_upload_device_rejects_too_large_size_field(src):
    r = SdRig({})
    r.fw.files = {}
    # device-side limit is also enforced by the fake (INVALID_FILE_SIZE for > 8 MiB); local check normally prevents it
    r.ser.write(b"WRITE_FILE:z:9999999999\n")
    assert "SD_ERROR:INVALID_FILE_SIZE" in bytes(r.fw.take(10000)).decode()


def test_upload_lost_ack_times_out_without_resending_and_device_cleans_up(src):
    r = SdRig({})
    r.fw.drop_once.add("WRITE_CHUNK_OK:")
    res = r.upload(src(b"y" * 2000), "n.txt")
    assert res.error.code is SdErrorCode.TIMEOUT and res.error.partial_left is False
    assert len(chunk_cmds(r)) == 1                       # never re-sent
    assert "n.txt" not in r.fw.files                      # firmware deleted its partial file after its 30 s wait
    assert r.fw.commands[-1] == "LIST_FILES"              # verification


def test_upload_partial_left_reported_when_device_cannot_delete(src):
    r = SdRig({})
    r.fw.drop_once.add("WRITE_CHUNK_OK:")
    r.fw.remove_fails = True
    res = r.upload(src(b"y" * 2000), "n.txt")
    assert res.error.code is SdErrorCode.PARTIAL_LEFT and res.error.cause is SdErrorCode.TIMEOUT
    assert res.error.partial_left is True and "n.txt" in r.fw.files


def test_upload_timeout_waiting_for_ready_is_not_blindly_retried(src):
    r = SdRig({})
    r.fw.reply_delay = 10_000
    res = r.upload(src(b"abc"), "n.txt")
    assert res.error.code is SdErrorCode.TIMEOUT
    assert r.fw.commands.count("WRITE_FILE:n.txt:3") == 1


def test_upload_slow_device_within_ack_timeout_succeeds(src):
    r = SdRig({})
    r.fw.reply_delay = 6.0
    assert r.upload(src(b"q" * 1100), "n.txt").ok


def test_upload_lost_final_ok_is_not_claimed_as_success(src):
    r = SdRig({})
    r.fw.drop_once.add("SD_WRITE_OK:")
    res = r.upload(src(b"q" * 10), "n.txt")
    # SD_WRITE_OK was lost: device completed, file present, we cannot claim success -> verified via LIST as PARTIAL_LEFT
    assert not res.ok and res.error.code is SdErrorCode.PARTIAL_LEFT and res.error.cause is SdErrorCode.TIMEOUT
    assert r.fw.files["n.txt"] == b"q" * 10


def test_upload_cancel_midway_aborts_device_and_verifies_clean(src):
    r = SdRig({})

    def prog(done, total):
        if done >= 1024:
            r.svc.cancel()

    res = r.upload(src(b"c" * 5000), "n.txt", prog)
    assert res.error.code is SdErrorCode.ABORTED and res.error.partial_left is False
    assert "n.txt" not in r.fw.files
    assert "WRITE_ABORT" in r.fw.chunk_headers            # fast abort: device deleted at once, no 30 s wait
    assert r.clock() < 100.0 + 25                          # did not sit through the 30 s header timeout


def test_upload_cancel_with_stuck_partial_reports_partial_left(src):
    r = SdRig({})
    r.fw.remove_fails = True

    def prog(done, total):
        if done >= 512:
            r.svc.cancel()

    res = r.upload(src(b"c" * 5000), "n.txt", prog)
    assert res.error.code is SdErrorCode.PARTIAL_LEFT and res.error.cause is SdErrorCode.ABORTED


def test_upload_session_lost_midway(src):
    r = SdRig({})

    def prog(done, total):
        if done >= 512:
            r.session = False

    res = r.upload(src(b"c" * 5000), "n.txt", prog)
    assert res.error.code is SdErrorCode.NOT_AUTHENTICATED and "n.txt" not in r.fw.files


def test_upload_disconnect_mid_transfer(src):
    r = SdRig({})
    r.probe()
    r.ser.fail_after_in_bytes = r.ser._in_total + 700
    res = r.upload(src(b"d" * 5000), "n.txt")
    assert res.error.code is SdErrorCode.DISCONNECTED and res.error.partial_left is None
    assert res.value is None and not r.manager.connected


def test_upload_disconnect_reading_ack(src):
    r = SdRig({})
    r.probe()
    r.ser.fail_after_out_bytes = r.ser._out_total + 80
    res = r.upload(src(b"d" * 5000), "n.txt")
    assert res.error.code is SdErrorCode.DISCONNECTED and not r.manager.connected


def test_upload_chunk_sequence_error_reported_by_device(src):
    """If the host ever desynchronised, the firmware aborts and deletes (INVALID_CHUNK_HEADER -> WRITE_INCOMPLETE)."""
    r = SdRig({})
    r.ser.write(b"WRITE_FILE:s.bin:600\n")
    r.ser.write(b"WRITE_CHUNK:1:512\n")  # wrong first sequence
    out = bytes(r.fw.take(10000)).decode()
    assert "SD_ERROR:WRITE_INCOMPLETE" in out and "s.bin" not in r.fw.files


def test_firmware_header_timeout_deletes_partial_file():
    r = SdRig({})
    r.ser.write(b"WRITE_FILE:s.bin:600\n")
    assert "s.bin" in r.fw.files
    r.clock.advance(31)
    r.ser.in_waiting
    assert "s.bin" not in r.fw.files


def test_two_uploads_in_a_row_and_list_shows_file(src):
    r = SdRig({})
    assert r.upload(src(b"1" * 600, "a"), "a.bin").ok and r.upload(src(b"2" * 10, "b"), "b.bin").ok
    assert r.list().value.paths == ("a.bin", "b.bin")
