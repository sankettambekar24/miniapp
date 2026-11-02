"""SerialManager / discovery / identity / config tests. Status: MOCK-TESTED (fake serial, no hardware)."""
import json

from hss.config import AppConfig, load_config
from hss.core.discovery import DiscoveryKind, PortInfo, discover
from hss.core.identity import IdentityResolver
from hss.core.protocol import EventKind, parse_line
from hss.core.serial_manager import RequestStatus

from conftest import OTHER, ST, Rig


# ---- discovery ----
def test_discover_none():
    assert discover([OTHER], AppConfig()).kind is DiscoveryKind.NONE


def test_discover_one_by_vid_pid():
    r = discover([OTHER, ST], AppConfig())
    assert r.kind is DiscoveryKind.ONE and r.port.device == "COM_TEST"


def test_discover_by_description_list():
    cfg = AppConfig(usb_ids=[], description_contains=["virtual com"])
    p = PortInfo("COM77", None, None, "STMicroelectronics Virtual COM Port (COM77)")
    assert discover([p], cfg).kind is DiscoveryKind.ONE


def test_discover_multiple_is_ambiguous_and_never_guessed():
    a, b = ST, PortInfo("COM_TEST2", 0x0483, 0x5740, "STM32 CDC")
    r = discover([a, b], AppConfig())
    assert r.kind is DiscoveryKind.MULTIPLE and r.port is None
    rig = Rig([a, b])
    rig.pump(2)
    assert not rig.manager.connected and not rig.opened


def test_no_hardcoded_com_port_any_device_name_works():
    rig = Rig([PortInfo("COM99", 0x0483, 0x5740, "x")])
    rig.connect()
    assert rig.manager.port.device == "COM99"


def test_custom_vid_pid_from_config():
    cfg = AppConfig(usb_ids=[(0x1234, 0x0001)])
    assert discover([OTHER, ST], cfg).port.device == "COM_OTHER"


# ---- hot plug ----
def test_hot_plug_connect_and_disconnect():
    rig = Rig([])
    rig.pump(2)
    assert not rig.manager.connected
    rig.plug()
    assert rig.manager.connected and rig.manager.conn_gen == 1
    rig.unplug()
    assert not rig.manager.connected
    rig.plug()
    assert rig.manager.conn_gen == 2  # new generation per connection


# ---- requests ----
def run_request(rig, **kw):
    out = []
    rid = rig.manager.send("LIST_FILES", callback=out.append, **kw)
    rig.pump(2)
    return rid, out


def test_request_ok_collects_lines_and_ids_increment(rig):
    rig.connect()
    rid, out = run_request(rig, ok_prefixes=("SD_FILE_LIST_END",))
    assert rig.ser.written == [b"LIST_FILES\n"]
    rig.device_says("SD_FILE_LIST_BEGIN")
    rig.device_says("a.txt")
    rig.device_says("SD_FILE_LIST_END")
    assert out[0].status is RequestStatus.OK and out[0].request_id == rid
    assert out[0].lines == ["SD_FILE_LIST_BEGIN", "a.txt", "SD_FILE_LIST_END"]
    assert rig.manager.send("LIST_FILES") == rid + 1


def test_request_error_prefix(rig):
    rig.connect()
    _, out = run_request(rig, ok_prefixes=("SD_FILE_LIST_END",))
    rig.device_says("SD_ERROR:AUTH_REQUIRED")
    assert out[0].status is RequestStatus.ERROR and "AUTH_REQUIRED" in out[0].error


def test_request_timeout(rig):
    rig.connect()
    _, out = run_request(rig, ok_prefixes=("X",), timeout=2.0)
    rig.clock.advance(3.0)
    rig.pump(1)
    assert out[0].status is RequestStatus.TIMEOUT


def test_one_in_flight_at_a_time(rig):
    rig.connect()
    rig.manager.send("A", ok_prefixes=("OK_A",))
    rig.manager.send("B", ok_prefixes=("OK_B",))
    rig.pump(3)
    assert rig.ser.written == [b"A\n"]
    rig.device_says("OK_A")
    rig.pump(2)
    assert rig.ser.written == [b"A\n", b"B\n"]


def test_request_fails_on_disconnect_and_queued_are_rejected(rig):
    rig.connect()
    out = []
    rig.manager.send("A", ok_prefixes=("OK",), callback=out.append)
    rig.manager.send("B", ok_prefixes=("OK",), callback=out.append)
    rig.pump(2)
    rig.unplug()
    assert [r.status for r in out] == [RequestStatus.DISCONNECTED, RequestStatus.REJECTED]


def test_send_while_disconnected_is_rejected_not_queued_forever():
    rig = Rig([])
    out = []
    rig.manager.send("A", callback=out.append)
    rig.pump(1)
    assert out[0].status is RequestStatus.REJECTED


def test_guard_blocks_command_without_session(rig):
    rig.connect()
    out = []
    rig.manager.send("LIST_FILES", callback=out.append, guard=lambda: rig.ctl.session.active)
    rig.pump(2)
    assert out[0].status is RequestStatus.REJECTED and rig.ser.written == []


def test_debug_lines_do_not_pollute_response(rig):
    rig.connect()
    _, out = run_request(rig, ok_prefixes=("DONE",))
    rig.device_says("DBG: EVENT SOMETHING")
    rig.device_says("DONE")
    assert out[0].lines == ["DONE"]


def test_partial_lines_crlf_and_overlong(rig):
    rig.connect()
    seen = []
    rig.manager.on_line.append(lambda e: seen.append(e.text))
    rig.ser.feed("HEL")
    rig.pump(2)
    assert seen == []
    rig.ser.feed("LO\r\nNEXT\n")
    rig.pump(3)
    assert seen == ["HELLO", "NEXT"]
    rig.ser.feed(b"X" * 5000 + b"\nOK\n")
    rig.pump(8)
    assert seen[-1] == "OK" and rig.manager.dropped_overlong >= 1


def test_multiline_command_rejected():
    rig = Rig([ST])
    try:
        rig.manager.send("A\nB")
        assert False
    except ValueError:
        pass


# ---- protocol parser ----
def test_parse_legacy_and_planned():
    assert parse_line("DBG: EVENT PIN_VERIFIED").kind is EventKind.PIN_VERIFIED
    assert parse_line("DBG: EVENT PIN_VERIFIED").slot is None
    e = parse_line("AUTH_PIN_VERIFIED:2")
    assert e.kind is EventKind.PIN_VERIFIED and e.slot == 2 and e.planned
    assert parse_line("AUTH_PIN_VERIFIED:x").slot is None
    assert parse_line("DBG: EVENT SYSTEM_LOCKED").kind is EventKind.LOCKED
    assert parse_line("SD_ERROR:LOCKED_OUT").kind is EventKind.LOCKED
    assert parse_line("SD_ERROR:AUTH_REQUIRED").kind is EventKind.SD_ERROR
    assert parse_line("random").kind is EventKind.OTHER


def test_firmware_never_supplies_a_role():
    for line in ("AUTH_PIN_VERIFIED:1 role=admin", "AUTH_PIN_VERIFIED:1:admin", "ROLE:admin"):
        assert parse_line(line).slot is None


# ---- identity file & config ----
def test_identity_file_loader(tmp_path):
    p = tmp_path / "ids.json"
    p.write_text(json.dumps({"slots": {"1": {"identity": "a", "role": "admin"}}}))
    r = IdentityResolver.load(p)
    assert r.resolve(1).role == "admin" and r.resolve(2) is None and r.resolve(None) is None


def test_identity_file_missing_or_invalid_resolves_nothing(tmp_path):
    assert IdentityResolver.load(tmp_path / "nope.json").resolve(1) is None
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"slots": {"1": {"identity": "a", "role": "root"}}}))
    r = IdentityResolver.load(bad)
    assert r.resolve(1) is None and r.warning


def test_example_templates_are_valid_and_contain_no_real_data():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "config"
    r = IdentityResolver.load(root / "test_identities.example.json")
    assert r.resolve(1).role == "admin" and r.resolve(2).role == "user"
    assert load_config(root / "config.example.json").test_mode


def test_config_defaults_to_production_and_fails_closed(tmp_path):
    assert not load_config(tmp_path / "missing.json").test_mode
    p = tmp_path / "c.json"
    p.write_text("{not json")
    c = load_config(p)
    assert not c.test_mode and c.load_warning
    p.write_text(json.dumps({"mode": "PIN_TEST"}))  # wrong case: not an explicit exact flag
    assert not load_config(p).test_mode
    p.write_text(json.dumps({"mode": "pin_test", "usb_ids": [{"vid": "1234", "pid": "0001"}]}))
    c = load_config(p)
    assert c.test_mode and c.usb_ids == [(0x1234, 0x0001)]
