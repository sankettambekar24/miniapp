"""SerialManager wire-ID mode. Status: MOCK-TESTED (fake serial)."""
from hss.core.serial_manager import RequestStatus

from conftest import Rig, ST


def wired(rig, **kw):
    out = []
    rid = rig.manager.send("LIST_FILES", callback=out.append, wire=True, **kw)
    rig.pump(2)
    return rid, out


def test_wire_frame_written_and_completes_on_req_end(rig):
    rig.connect()
    rid, out = wired(rig)
    assert rig.ser.written == [f"@{rid} LIST_FILES\n".encode()]
    rig.device_says(f"REQ_BEGIN:{rid}")
    rig.device_says("SD_FILE_LIST_BEGIN")
    rig.device_says("a.txt")
    rig.device_says("SD_FILE_LIST_END")
    assert out == []  # list-end alone is not completion in wire mode
    rig.device_says(f"REQ_END:{rid}")
    assert out[0].status is RequestStatus.OK
    assert out[0].lines == ["SD_FILE_LIST_BEGIN", "a.txt", "SD_FILE_LIST_END"]


def test_wire_error_reported_at_req_end(rig):
    rig.connect()
    rid, out = wired(rig)
    rig.device_says(f"REQ_BEGIN:{rid}")
    rig.device_says("SD_ERROR:AUTH_REQUIRED")
    rig.device_says(f"REQ_END:{rid}")
    assert out[0].status is RequestStatus.ERROR and "AUTH_REQUIRED" in out[0].error


def test_late_output_of_timed_out_request_is_not_attributed_to_next_request(rig):
    rig.connect()
    rid1, out1 = wired(rig, timeout=2.0)
    rig.clock.advance(3.0)
    rig.pump(1)
    assert out1[0].status is RequestStatus.TIMEOUT
    out2 = []
    rid2 = rig.manager.send("LIST_FILES", callback=out2.append, wire=True)
    rig.pump(2)
    # late reply to request 1 arrives while request 2 is in flight
    rig.device_says(f"REQ_BEGIN:{rid1}")
    rig.device_says("old.txt")
    rig.device_says(f"REQ_END:{rid1}")
    assert out2 == []  # not completed by the stale REQ_END
    rig.device_says(f"REQ_BEGIN:{rid2}")
    rig.device_says("new.txt")
    rig.device_says(f"REQ_END:{rid2}")
    assert out2[0].status is RequestStatus.OK and out2[0].lines == ["new.txt"]


def test_output_before_req_begin_is_ignored(rig):
    rig.connect()
    rid, out = wired(rig)
    rig.device_says("stray.txt")
    rig.device_says(f"REQ_BEGIN:{rid}")
    rig.device_says(f"REQ_END:{rid}")
    assert out[0].lines == []


def test_default_is_legacy_framing_and_wire_ids_flag(rig):
    rig.connect()
    rig.manager.send("LIST_FILES", ok_prefixes=("SD_FILE_LIST_END",))
    rig.pump(2)
    assert rig.ser.written == [b"LIST_FILES\n"]
    rig.manager.wire_ids = True
    rig.manager.send("END_SESSION", ok_prefixes=("SESSION_ENDED",))
    rig.device_says("SD_FILE_LIST_END")  # completes the legacy in-flight request first
    rig.pump(2)
    assert rig.ser.written[-1].startswith(b"@")


def test_request_id_wraps_below_nine_digits():
    rig = Rig([ST])
    rig.manager._next_id = 999_999_999
    assert rig.manager.send("A") == 999_999_999
    assert rig.manager.send("A") == 1
