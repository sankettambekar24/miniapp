"""LIST_FILES refresh regression, driven through the real SdPage with a scripted serial port that speaks the firmware's
exact wire format (SD_FILE_LIST_BEGIN/END, SD_ERROR:*, DBG: lines, '@id' REQ_BEGIN/REQ_END framing).

Status: MOCK-TESTED. The SD listing has NEVER been verified on hardware, so "it used to work" only ever meant the mocks.
Cause of the reported breakage (commit 408ec4d): SD commands were gated on a "device PIN unlock observed" flag that the
app can only know if it saw AUTH_PIN_VERIFIED after (re)connecting; a PIN typed before the app connected left Refresh
disabled. The gate was removed: the device answers SD_ERROR:AUTH_REQUIRED itself and that real error is shown.
"""
import re

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from hss.config import AppConfig, MODE_PIN_TEST
from hss.controller import AppController
from hss.core.discovery import PortInfo
from hss.core.identity import IdentityResolver
from hss.core.serial_manager import LineEvent, SerialManager
from hss.core.session import Identity
from hss.ui.sd_page import SdPage

from conftest import Clock

VERSION = "HSS_FW:0.3.0-stage3:AUTH=PIN_ONLY:SLOTS=2:REQID=1"
LIST_OK = ["SD_FILE_LIST_BEGIN", "DBG: Skipped path that is too long or unsafe", "HELLO.TXT", "DOCS/NOTES.TXT",
           "SD_FILE_LIST_END"]


class ScriptedSerial:
    """Answers host lines like the firmware: unframed VERSION, '@id CMD' framed with REQ_BEGIN/REQ_END."""

    def __init__(self, replies: dict[str, list[str]]):
        self.replies, self._rx, self.written = replies, bytearray(), []
        self.closed = False

    @property
    def in_waiting(self):
        return len(self._rx)

    def read(self, n=1):
        out, self._rx = bytes(self._rx[:n]), self._rx[n:]
        return out

    def write(self, data):
        self.written.append(bytes(data))
        for raw in bytes(data).split(b"\n"):
            line = raw.decode("ascii", "replace").strip()
            if not line:
                continue
            m = re.match(r"^@(\d+) (.+)$", line)
            wid, cmd = (m.group(1), m.group(2)) if m else (None, line)
            body = [VERSION] if cmd == "VERSION" else self.replies.get(cmd, ["SD_ERROR:UNKNOWN_COMMAND"])
            lines = ([f"REQ_BEGIN:{wid}"] + body + [f"REQ_END:{wid}"]) if wid else body
            self._rx += ("\r\n".join(lines) + "\r\n").encode()
        return len(data)

    def reset_input_buffer(self):
        self._rx.clear()

    def flush(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class Env:
    def __init__(self, replies, connected=True, session=True):
        self.clock = Clock()
        self.ports = [PortInfo("COM15", 0x0483, 0x5740, "USB Serial Device")] if connected else []
        self.serial = ScriptedSerial(replies)
        cfg = AppConfig(mode=MODE_PIN_TEST, post_open_delay=0, scan_interval=1.0)
        self.manager = SerialManager(cfg, port_lister=lambda: list(self.ports), serial_factory=lambda d, b: self.serial,
                                     clock=self.clock, sleep=lambda s: None)
        self.manager.pump()
        self.ctl = AppController(cfg, IdentityResolver({1: Identity(1, "test-admin", "admin")}), self.manager)
        if connected:
            self.ctl.fsm.on_connected(self.manager.conn_gen)
            if session:
                self.ctl.fsm.start_auth()
                self.ctl.fsm.handle_line(LineEvent(self.manager.conn_gen, "AUTH_PIN_VERIFIED:1", 0.0))
        self.page = SdPage(self.ctl)

    def refresh(self, app, pumps=300):
        self.page.update_controls()
        assert self.page.btn_list.isEnabled(), self.page.btn_list.toolTip()
        self.page.btn_list.click()
        for _ in range(pumps):
            self.manager.pump()
            self.clock.advance(0.05)
            app.processEvents()
            if not self.page.busy:
                break


def test_refresh_shows_real_listing_in_wire_format(app):
    e = Env({"LIST_FILES": LIST_OK})
    e.refresh(app)
    assert e.page.paths == ["HELLO.TXT", "DOCS/NOTES.TXT"] and e.page.files.rowCount() == 2
    assert "2 file(s)" in e.page.status.text()
    sent = b"".join(e.serial.written).decode()
    assert "VERSION" in sent and re.search(r"@\d+ LIST_FILES", sent)           # probe, then the framed command
    e.refresh(app)                                                              # a second refresh works too
    assert e.page.paths == ["HELLO.TXT", "DOCS/NOTES.TXT"]


def test_refresh_empty_card_is_empty_not_dummy(app):
    e = Env({"LIST_FILES": ["SD_FILE_LIST_BEGIN", "SD_FILE_LIST_END"]})
    e.refresh(app)
    assert e.page.paths == [] and e.page.files.rowCount() == 0 and "0 file(s)" in e.page.status.text()


@pytest.mark.parametrize("code,needle", [
    ("AUTH_REQUIRED", "keypad"), ("CARD_UNAVAILABLE", "SD card is missing"), ("ROOT_OPEN_FAILED", "ROOT_OPEN_FAILED")])
def test_device_errors_are_shown_for_real_and_no_dummy_rows(app, code, needle):
    e = Env({"LIST_FILES": [f"SD_ERROR:{code}"]})
    e.refresh(app)
    assert e.page.files.rowCount() == 0 and e.page.paths == []
    assert "failed" in e.page.status.text() and needle in e.page.status.text()


def test_error_then_success_refresh_recovers(app):
    e = Env({"LIST_FILES": ["SD_ERROR:CARD_UNAVAILABLE"]})
    e.refresh(app)
    assert e.page.files.rowCount() == 0
    e.serial.replies["LIST_FILES"] = LIST_OK
    e.refresh(app)
    assert e.page.paths == ["HELLO.TXT", "DOCS/NOTES.TXT"]


def test_no_port_disables_refresh_with_reason_and_shows_nothing(app):
    e = Env({"LIST_FILES": LIST_OK}, connected=False)
    e.page.update_controls()
    assert not e.page.btn_list.isEnabled() and "not connected" in e.page.btn_list.toolTip().lower()
    assert e.page.files.rowCount() == 0 and not e.serial.written


def test_no_session_disables_refresh_and_sends_nothing(app):
    e = Env({"LIST_FILES": LIST_OK}, session=False)
    e.page.update_controls()
    assert not e.page.btn_list.isEnabled() and "session" in e.page.btn_list.toolTip().lower()
    assert not e.serial.written


def test_keypad_unlock_state_does_not_gate_refresh(app):
    """The regression: a session exists but the app never saw AUTH_PIN_VERIFIED after connecting."""
    e = Env({"LIST_FILES": LIST_OK})
    assert e.ctl.fsm.state.value == "SESSION_ACTIVE"
    e.refresh(app)
    assert e.page.paths
