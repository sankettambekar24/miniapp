"""SerialManager.run_transaction semantics. Status: MOCK-TESTED (fake serial)."""
from hss.core.serial_manager import RequestStatus, TxDisconnected, TxTimeout
from sd_rig import SdRig


def run_tx(r, fn, timeout=30.0, guard=None):
    out = []
    r.manager.run_transaction(fn, timeout, out.append, guard)
    for _ in range(5):
        if out:
            break
        r.manager.pump()
    return out[0]


def test_transaction_returns_value_and_reads_binary_untouched():
    r = SdRig({})
    payload = b"\n\r\n\x00AB\nSD_ERROR:AUTH_REQUIRED\n"

    def fn(io):
        r.fw._emit(b"HDR\r\n" + payload + b"END\r\n")
        assert io.readline(2) == "HDR"
        data = io.read_exact(len(payload), 2)
        assert io.readline(2) == "END"
        return data

    resp = run_tx(r, fn)
    assert resp.status is RequestStatus.OK and resp.value == payload


def test_disconnect_fails_transaction_and_discards_partial_result():
    r = SdRig({})
    r.ser.fail_after_out_bytes = 0

    def fn(io):
        partial = "partial"
        io.readline(5)  # raises TxDisconnected (read error)
        return partial

    resp = run_tx(r, fn)
    assert resp.status is RequestStatus.DISCONNECTED and resp.value is None
    assert not r.manager.connected


def test_value_discarded_if_link_dropped_even_when_fn_swallows_error():
    r = SdRig({})

    def fn(io):
        try:
            io.readline(1)
        except TxDisconnected:
            pass
        return "looks fine"

    r.ser.fail_after_out_bytes = 0
    resp = run_tx(r, fn)
    assert resp.status is RequestStatus.DISCONNECTED and resp.value is None


def test_stale_connection_generation_detected():
    r = SdRig({})

    def fn(io):
        r.manager._drop("test unplug")   # conn dropped under the transaction
        io.write(b"X")                   # must refuse (connection changed)
        return "unreachable"

    resp = run_tx(r, fn)
    assert resp.status is RequestStatus.DISCONNECTED and resp.value is None


def test_timeout_status():
    r = SdRig({})
    resp = run_tx(r, lambda io: io.readline(1.0))
    assert resp.status is RequestStatus.TIMEOUT


def test_overall_timeout_bounds_waits():
    r = SdRig({})

    def fn(io):
        try:
            io.readline(1000)
        except TxTimeout as e:
            assert e.overall
            raise

    resp = run_tx(r, fn, timeout=3.0)
    assert resp.status is RequestStatus.TIMEOUT and r.clock() < 100 + 10


def test_guard_false_rejects_without_touching_port():
    r = SdRig({})
    called = []
    resp = run_tx(r, lambda io: called.append(1), guard=lambda: False)
    assert resp.status is RequestStatus.REJECTED and not called and r.ser.written == []


def test_queued_transaction_rejected_when_not_connected():
    r = SdRig({})
    r.ports = []
    r.clock.advance(2)
    r.manager.pump()
    resp = run_tx(r, lambda io: 1)
    assert resp.status is RequestStatus.REJECTED


def test_security_lines_forwarded_to_on_line_but_protocol_lines_consumed():
    r = SdRig({})

    def fn(io):
        for t in ("SD_FILE_LIST_BEGIN", "DBG: noise", "SESSION_EXPIRED", "AUTH_LOCKED", "SD_ERROR:LOCKED_OUT", "WRITE_CHUNK_OK:0:1"):
            r.fw._line_out(t)
        return [io.readline(1) for _ in range(6)]

    resp = run_tx(r, fn)
    assert resp.status is RequestStatus.OK and len(resp.value) == 6
    texts = [e.text for e in r.lines]
    assert texts == ["SESSION_EXPIRED", "AUTH_LOCKED", "SD_ERROR:LOCKED_OUT"]
    assert all(e.conn_gen == r.manager.conn_gen for e in r.lines)


def test_leftover_bytes_go_to_line_parser_after_success():
    r = SdRig({})

    def fn(io):
        r.fw._emit(b"ONE\r\nSESSION_EXPIRED\r\n")
        return io.readline(1)

    resp = run_tx(r, fn)
    assert resp.value == "ONE"
    assert [e.text for e in r.lines] == ["SESSION_EXPIRED"]


def test_legacy_line_requests_still_work_after_transactions():
    r = SdRig({"a": b""})
    run_tx(r, lambda io: 1)
    out = []
    r.manager.send("LIST_FILES", ok_prefixes=("SD_FILE_LIST_END",), callback=out.append)
    r.manager.pump()
    assert out[0].status is RequestStatus.OK and out[0].lines == ["SD_FILE_LIST_BEGIN", "a", "SD_FILE_LIST_END"]


def test_callback_exception_does_not_kill_worker():
    r = SdRig({})
    r.manager.run_transaction(lambda io: 1, 5, lambda resp: 1 / 0)
    r.manager.pump()
    assert r.manager.connected


def test_hot_unplug_detected_while_transaction_owns_port():
    r = SdRig({})

    def fn(io):
        r.ports = []
        io.readline(30)

    resp = run_tx(r, fn)
    assert resp.status is RequestStatus.DISCONNECTED
